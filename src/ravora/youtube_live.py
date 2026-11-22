#!/usr/bin/env python3
"""Run the headless Ray program locally or publish it to YouTube Studio."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import replace
import errno
import io
import json
import os
from pathlib import Path
import secrets
import signal
import sys
import tempfile
import threading
import time
import tomllib

from ravora.conversation import ConversationBridge, MemoryStore
from ravora.developer_http import DeveloperHTTP
from ravora.input_pipeline import InputPipeline
from ravora.live_http import LiveHandler
from ravora.safety import TurnFence
from ravora.settings import load_settings, read_env, SettingsError
from ravora.youtube_auth import authorize, ChannelAPI, publisher_lock, YouTubeError


ROOT = Path.cwd()


def run(args):
    directory = ROOT / 'state/youtube'
    if args.authorize_youtube:
        authorize(directory)
        return
    from ravora.live_media import DEFAULT_INGEST, FPS, ProgramWriter, Scene, SILENCE, Stage, av, publish_target, validate_video_encoder
    from ravora.youtube_broadcast import Broadcast
    from ravora.live_program import LiveProgram
    s = load_settings(args.env, mode=args.mode)
    publishing = args.publish or args.auto_broadcast
    if args.host_only and publishing:
        raise SettingsError('HOST_ONLY_CANNOT_PUBLISH')
    config = tomllib.loads(args.config.read_text()) if args.config else {}
    video_config = config.get('youtube_live', {})
    if not isinstance(video_config, dict):
        raise SettingsError('INVALID_PROGRAM_VIDEO_ENCODER')
    video_encoder = validate_video_encoder(video_config.get('video_encoder', 'libx265'))
    configured_target = bool(s.youtube_chat_id or s.youtube_video_id)
    managed = args.auto_broadcast or (args.publish and not configured_target)
    if (args.privacy or args.title) and not managed:
        raise SettingsError('YOUTUBE_TITLE_AND_PRIVACY_REQUIRE_AUTO_BROADCAST')
    title = args.title or 'Ray Live'
    if args.check or publishing:
        # Other services serialize ProviderSettings for their workers. Keep the
        # RTMPS publish credential exclusively in this process.
        values = read_env(args.env)
        stream_key = os.environ.get('YOUTUBE_STREAM_KEY', values.get('YOUTUBE_STREAM_KEY', ''))
        base = os.environ.get('YOUTUBE_RTMPS_URL', values.get('YOUTUBE_RTMPS_URL', DEFAULT_INGEST))
        del values
    if args.check:
        print(json.dumps({'conversation_api_enabled': s.api_enabled, 'missing_conversation_keys': s.missing(),
                          'youtube_read_enabled': s.youtube_read_enabled, 'youtube_api_key_set': bool(s.youtube_key),
                          'chat_target_set': bool(s.youtube_chat_id or s.youtube_video_id),
                          'stream_key_set': bool(stream_key), 'video_encoder': video_encoder,
                          'oauth_client_file': (directory/'client.json').is_file(),
                          'oauth_token_file': (directory/'token.json').is_file()}))
        return
    if publishing and args.mode != 'cloud':
        raise SettingsError('PUBLISH_REQUIRES_CLOUD_MODE')
    if args.mode == 'cloud':
        if not s.api_enabled:
            raise SettingsError('RAVORA_API_ENABLED_REQUIRED')
        s.require_ready()
        if not args.host_only and not s.youtube_read_enabled:
            raise SettingsError('YOUTUBE_READ_ENABLED_REQUIRED')
        if not args.host_only and not managed and not (s.youtube_key and configured_target):
            raise SettingsError('CONFIGURE_YOUTUBE_READ_ENABLED_API_KEY_AND_VIDEO_OR_CHAT_ID')
        s = replace(s, comment_source='developer' if args.host_only else 'youtube',
                    youtube_read_enabled=False if args.host_only else s.youtube_read_enabled)
    else:
        s = replace(s, api_enabled=False, memory_api_enabled=False, stt_api_enabled=False,
                    youtube_read_enabled=False, comment_source='developer' if args.host_only else 'youtube-fixture')
    if not s.character_enabled:
        raise SettingsError('RAVORA_CHARACTER_ENABLED_REQUIRED')
    if not title.strip() or len(title) > 100 or any(c in title for c in '<>\r\n'):
        raise SettingsError('INVALID_YOUTUBE_TITLE')
    url = publish_target(base, stream_key) if publishing and not managed else None
    # Native libav errors can embed its destination. Report stable codes only.
    av.logging.set_level(av.logging.PANIC)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: stop.set())
    runtime = ROOT / '.runtime'
    runtime.mkdir(mode=0o700, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix='youtube-live-', dir=runtime))
    print('Run:', run_dir, flush=True)
    print('Replies:', 'offline English fixture' if args.mode == 'offline' else s.gemini_model, flush=True)
    with ExitStack() as stack:
        broadcast = None
        if managed:
            # The managed ingest destination exists only after prepare(). Check the GPU
            # with the actual stream settings before creating or binding a remote event.
            if video_encoder == 'hevc_nvenc':
                try:
                    with io.BytesIO() as probe:
                        ProgramWriter(probe, None, video_encoder=video_encoder).close()
                except Exception:
                    raise SettingsError(f'PROGRAM_ENCODER_OPEN_FAILED ({video_encoder})') from None
            stack.enter_context(publisher_lock(directory))
            api, channel = ChannelAPI.connect(directory)
            stack.callback(api.http.close)
            broadcast = Broadcast(api, directory, channel, title, args.privacy or 'unlisted', stop)
        audio = Path(stack.enter_context(tempfile.TemporaryDirectory(dir=run_dir)))
        store = MemoryStore(ROOT / 'state/conversation.sqlite3' if args.mode == 'cloud' else run_dir / 'offline.sqlite3')
        language = args.host_language or s.stt_language
        default_locale = s.voice_input_language if s.voice_profile == 'fast_streaming' else s.language
        if args.host_language is None and language == 'auto' and (args.dell_mic or s.mode == 'cloud'):
            language = default_locale[:2]
        providers = profiles = None
        if s.mode == 'cloud':
            from ravora.live_voice import live_reply_profiles
            from ravora.providers import JevReaction
            if s.voice_profile == 'fast_streaming' and not s.tts_key:
                raise SettingsError('GOOGLE_TTS_API_KEY_REQUIRED_FOR_CHIRP')
            profiles = live_reply_profiles(s)
            s, text, speech = profiles[default_locale[:2] if language == 'auto' else language]
            providers = text, JevReaction(s), speech
        bridge = ConversationBridge(s, store, audio, TurnFence(), threading.RLock(), providers=providers)
        pipeline = InputPipeline(s, store, bridge, [])
        voice = config.get('developer_voice', {})
        from ravora.settings import validate_handsfree_onset_dbfs
        onset = validate_handsfree_onset_dbfs(voice.get('handsfree_onset_dbfs', s.handsfree_onset_dbfs))
        silence = voice.get('end_silence_ms', 800)
        if type(silence) is not int or not 300 <= silence <= 2500:
            raise SettingsError('INVALID_END_SILENCE_MS')
        program = LiveProgram(pipeline, audio, host_language=language, reply_profiles=profiles,
                              onset_dbfs=onset, silence_ms=silence)
        stack.callback(program.close)
        secret = secrets.token_urlsafe(32)
        try:
            server = DeveloperHTTP(program, secret, args.port, handler=LiveHandler)
        except OSError as exc:
            code = ('HOST_CONSOLE_PORT_IN_USE' if exc.errno == errno.EADDRINUSE
                    else 'HOST_CONSOLE_BIND_FAILED')
            raise SettingsError(f'{code} 127.0.0.1:{args.port} ({errno.errorcode.get(exc.errno, "UNKNOWN")})') from None
        stack.callback(server.server_close)
        server.dell_mic = None
        if args.dell_mic:
            from ravora.live_dell_mic import DellCapture
            from ravora.dell_microphone import parse_device
            device = voice.get('dell_device', '')
            parse_device(device)
            server.dell_mic = DellCapture(device, program.mic, s.stt_max_seconds)
            server.dell_mic.prepare()
            stack.callback(server.dell_mic.close)
        scene = Scene(args.scene_dir, live=publishing)
        server.scene = scene
        if args.scene_dir is None:
            server.avatar_assets, server.avatar_manifest = {}, None
            if 'pngtuber' in config:
                from ravora.pngtuber import pngtuber_binding
                server.avatar_assets, server.avatar_manifest = pngtuber_binding(args.config.resolve().parent, config)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        stack.callback(thread.join)
        stack.callback(server.shutdown)
        if args.scene_dir is None:
            from ravora.live_avatar import ProgramAvatar
            scene.avatar = ProgramAvatar(server.server_port, run_dir)
            stack.callback(scene.avatar.close)
            print('Conversation avatar:', scene.avatar.renderer, flush=True)
            if server.avatar_manifest:
                print('Avatar model:', server.avatar_manifest['name'], flush=True)
        if broadcast:
            url = broadcast.prepare()
            video_id = broadcast.state['broadcast']
            print('YouTube watch URL: https://www.youtube.com/watch?v=' + video_id, flush=True)
        if stop.is_set():
            return
        try:
            writer = ProgramWriter(url or run_dir / 'program.flv', scene, publish=publishing, video_encoder=video_encoder)
        except Exception:
            raise SettingsError(f'PROGRAM_ENCODER_OPEN_FAILED ({video_encoder})') from None
        stack.callback(writer.close)
        active_encoder = writer.video.codec_context.name
        print('Video encoder:', active_encoder, flush=True)
        operator_url = run_dir / 'operator.url'
        console_url = f'http://127.0.0.1:{server.server_port}/#{secret}'
        operator_url.write_text(console_url + '\n')
        stack.callback(operator_url.unlink, missing_ok=True)
        print('Host console URL file:', operator_url, flush=True)
        if sys.stdout.isatty():
            print('Host console URL:', console_url, flush=True)
        stack.callback(program.stopped.set)
        stack.callback(pipeline.stop.set)
        startup = None
        chat_lookup = None
        chat = None
        live = False
        if broadcast:
            executor = stack.enter_context(ThreadPoolExecutor(max_workers=1, thread_name_prefix='youtube-start'))
            startup = executor.submit(broadcast.wait_live)
            stack.callback(stop.set)
        else:
            pipeline.start(args.seconds, wait_for_chat=True)
        start = time.monotonic()
        max_lag = 0.0
        try:
            while not stop.is_set() and time.monotonic() < start + args.seconds:
                if startup and startup.done():
                    event = startup.result()
                    if event is None:
                        break
                    live = True
                    print('YouTube live confirmed:', video_id, flush=True)
                    program.chat_status = {'status':'waiting_for_chat'}
                    chat_lookup = executor.submit(broadcast.chat_id, event)
                    startup = None
                if chat_lookup and chat_lookup.done():
                    try:
                        chat = chat_lookup.result()
                    except YouTubeError as exc:
                        program.chat_status = {'status':'failed', 'code':str(exc)}
                        print('YouTube chat:', str(exc), flush=True)
                    if chat:
                        pipeline.s = replace(s, youtube_video_id='', youtube_chat_id=chat)
                        pipeline.start(max(1, args.seconds - (time.monotonic() - start)), access_token=api.access_token)
                    chat_lookup = None
                stage, pcm = program.next_frame() if not broadcast or live else (Stage(), SILENCE)
                scene.clip = program.clip
                writer.write(stage, pcm)
                program.encoded(stage)
                remaining = start + writer.frames / FPS - time.monotonic()
                max_lag = max(max_lag, -remaining)
                stop.wait(max(0, remaining))
            if broadcast and not live and not stop.is_set():
                raise YouTubeError('YOUTUBE_DURATION_ENDED_BEFORE_LIVE')
        finally:
            stop.set()
            if broadcast:
                executor.shutdown(wait=True)
                broadcast.finish()
        summary = program.status()
        summary.pop('stage')
        summary.pop('monitor_audio')
        summary.pop('monitor_offset_seconds')
        summary['elapsed_seconds'] = round(time.monotonic() - start, 3)
        summary['average_fps'] = round(writer.frames / max(summary['elapsed_seconds'], .001), 2)
        summary['max_encoder_lag_ms'] = round(max_lag * 1000)
        summary['publish'] = publishing
        summary['video_encoder'] = active_encoder
        summary['avatar_renderer'] = scene.avatar.renderer if scene.avatar else 'custom_scene'
        if scene.avatar and server.avatar_manifest:
            summary['avatar_name'] = server.avatar_manifest['name']
            summary['avatar_sha256'] = server.avatar_manifest['sha256']
        summary['avatar_frames_rendered'] = scene.avatar.rendered if scene.avatar else 0
        summary['reply_voice'] = getattr(bridge.controller.speech, 'voice', bridge.controller.s.voice) if s.mode == 'cloud' else 'flite-slt'
        if broadcast:
            summary['video_id'] = video_id
            summary['live_chat_id'] = chat
            summary['live_confirmed'] = live
    (run_dir / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('offline', 'cloud'), default='offline')
    parser.add_argument('--config', type=Path, help='TOML video encoder, microphone and optional portrait configuration')
    parser.add_argument('--env', type=Path, default=ROOT / '.env')
    parser.add_argument('--seconds', type=int, default=180)
    parser.add_argument('--port', type=int, default=8790)
    parser.add_argument('--dell-mic', action='store_true', help='Control the configured Dell microphone from the host console')
    parser.add_argument('--host-only', action='store_true', help='Record the conversation locally without YouTube or sample chat')
    parser.add_argument('--host-language', choices=('ja','en','auto'), help='Initial conversation language (auto detects microphone input)')
    parser.add_argument('--check', action='store_true', help='Check configuration without connecting or opening an encoder')
    parser.add_argument('--publish', action='store_true', help='Send the program to the configured YouTube RTMPS ingest')
    parser.add_argument('--auto-broadcast', action='store_true', help='Create or resume a Ray broadcast, then go live over RTMPS')
    parser.add_argument('--authorize-youtube', action='store_true', help='Authorize the Ray channel on loopback port 8791')
    parser.add_argument('--privacy', choices=('unlisted', 'private', 'public'), help='Managed broadcast visibility (default: unlisted)')
    parser.add_argument('--title', help='Managed broadcast title (default: Ray Live)')
    parser.add_argument('--scene-dir', type=Path)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 86400 or not 0 <= args.port <= 65535:
        parser.error('invalid duration or port')
    if args.authorize_youtube and (args.publish or args.auto_broadcast or args.check or args.host_only):
        parser.error('--authorize-youtube runs separately from checking or publishing')
    os.umask(0o077)
    try:
        run(args)
    except (SettingsError, ModuleNotFoundError) as exc:
        sys.exit(str(exc))
    except ValueError as exc:
        code = str(exc)
        sys.exit(code if code.isupper() and len(code) < 100 else 'PROGRAM_CONFIGURATION_FAILED')
    except YouTubeError as exc:
        sys.exit(str(exc))
    except RuntimeError as exc:
        sys.exit('PROGRAM_ENCODER_OPEN_FAILED' if str(exc) == 'PROGRAM_ENCODER_OPEN_FAILED' else 'PROGRAM_FAILED')
    except Exception:
        sys.exit('PROGRAM_IO_FAILED')


if __name__ == '__main__':
    main()
