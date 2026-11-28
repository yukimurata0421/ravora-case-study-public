#!/usr/bin/env python3
"""Run the headless Ray program locally or publish it to YouTube or Twitch."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import replace
import errno
import json
import logging
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
from ravora.ray_memory import CharacterMemory
from ravora.safety import TurnFence
from ravora.settings import boolean, load_settings, read_env, SettingsError
from ravora.twitch_identity import TwitchError
from ravora.youtube_auth import authorize, ChannelAPI, publisher_lock, YouTubeError


ROOT = Path.cwd()


def run(args, *, scheduled=None):
    directory = ROOT / 'state/youtube'
    if args.authorize_youtube:
        authorize(directory)
        return
    from ravora.live_media import (
        DEFAULT_INGEST, TWITCH_INGEST, FPS, ProgramWriter, Scene, SILENCE, Stage, av,
        publish_target, twitch_publish_target, validate_video_encoder,
    )
    from ravora.youtube_broadcast import Broadcast, DEFAULT_LATENCY_PREFERENCE, validate_latency_preference, video_metadata
    from ravora.live_program import LiveProgram
    from ravora.live_bgm import DEFAULT_BGM_DB, LoopingMusic, bgm_tracks, validate_bgm_db
    from ravora.live_mic import validate_host_trim_db
    from ravora.live_reverb import DEFAULT_RAY_REVERB_MIX, validate_ray_reverb_mix
    from ravora.live_metrics import StudioMetrics
    s = load_settings(args.env, mode=args.mode, live_studio=True)
    twitch = args.publish_twitch
    google_speech = not twitch or 'google' in (s.twitch_en_tts, s.twitch_ja_tts)
    publishing = args.publish or args.auto_broadcast or twitch
    destination = ('twitch' if twitch else 'youtube') if publishing else None
    host_input_only = args.host_only or twitch
    if args.host_only and publishing:
        raise SettingsError('HOST_ONLY_CANNOT_PUBLISH')
    config = tomllib.loads(args.config.read_text()) if args.config else {}
    live_config = config.get('youtube_live', {})
    if not isinstance(live_config, dict):
        raise SettingsError('INVALID_PROGRAM_VIDEO_ENCODER')
    scheduled_idle = None
    if twitch:
        twitch_config = config.get('twitch_live', {})
        if not isinstance(twitch_config, dict):
            raise SettingsError('INVALID_PROGRAM_VIDEO_ENCODER')
        if scheduled:
            from ravora.budget_status import scheduled_limits
            limits = scheduled_limits(s)
            s = replace(s, monthly_reply_llm_usd=limits['reply_llm_usd'],
                        monthly_tts_chars=limits['tts_characters'], stt_monthly_audio_seconds=limits['cloud_stt_seconds']).validate()
        scheduled_idle = twitch_config.get('scheduled_audience_idle_seconds', 120) if scheduled else None
        if scheduled_idle is not None:
            s = replace(s, character_idle_seconds=scheduled_idle).validate()
        video_encoder = validate_video_encoder(twitch_config.get('video_encoder', 'libx264'), twitch=True)
    else:
        made_for_kids = live_config.get('made_for_kids')
        if made_for_kids is not None and type(made_for_kids) is not bool:
            raise SettingsError('INVALID_YOUTUBE_MADE_FOR_KIDS')
        latency_preference = validate_latency_preference(live_config.get('latency_preference', DEFAULT_LATENCY_PREFERENCE))
        video_encoder = validate_video_encoder(live_config.get('video_encoder', 'libx265'))
    host_trim_db = validate_host_trim_db(live_config.get('host_trim_db', 0.0))
    playlist = bgm_tracks(live_config, args.config.resolve().parent if args.config else ROOT)
    bgm_db = validate_bgm_db(live_config.get('bgm_volume_db', DEFAULT_BGM_DB))
    ray_reverb_mix = validate_ray_reverb_mix(live_config.get('ray_reverb_mix', DEFAULT_RAY_REVERB_MIX))
    host_mic_cleanup = live_config.get('host_mic_cleanup', True)
    if type(host_mic_cleanup) is not bool:
        raise SettingsError('INVALID_HOST_MIC_CLEANUP')
    configured_target = bool(s.youtube_chat_id or s.youtube_video_id)
    managed = args.auto_broadcast or (args.publish and not configured_target)
    if (args.privacy or args.title) and not managed:
        raise SettingsError('YOUTUBE_TITLE_AND_PRIVACY_REQUIRE_AUTO_BROADCAST')
    metadata = video_metadata(live_config.get('metadata', {}), args.title) if managed or (args.check and not twitch) else None
    twitch_chat = False
    if args.check or publishing:
        # Other services serialize ProviderSettings for their workers. Keep the
        # RTMPS publish credential exclusively in this process.
        values = read_env(args.env)
        key_var, url_var, default_ingest = (
            ('TWITCH_STREAM_KEY', 'TWITCH_RTMPS_URL', TWITCH_INGEST) if twitch
            else ('YOUTUBE_STREAM_KEY', 'YOUTUBE_RTMPS_URL', DEFAULT_INGEST)
        )
        stream_key = os.environ.get(key_var, values.get(key_var, ''))
        base = os.environ.get(url_var, values.get(url_var, default_ingest))
        if twitch:
            try:
                twitch_chat = boolean(os.environ.get('TWITCH_CHAT_ENABLED', values.get('TWITCH_CHAT_ENABLED', 'true')))
            except ValueError as exc:
                raise SettingsError('INVALID_TWITCH_CHAT_ENABLED') from exc
        del values
    if scheduled and not twitch_chat:
        raise SettingsError('TWITCH_SCHEDULE_CHAT_REQUIRED')
    if args.check:
        if twitch and stream_key:
            twitch_publish_target(base, stream_key)
        report = {'conversation_api_enabled': s.api_enabled, 'missing_conversation_keys': s.missing(google_tts=google_speech),
                  'stream_key_set': bool(stream_key), 'video_encoder': video_encoder, 'host_trim_db': host_trim_db,
                  'host_mic_cleanup': host_mic_cleanup, 'ray_reverb_mix': ray_reverb_mix,
                  'character_initiative_enabled': s.character_enabled and s.character_initiative_enabled and s.character_max_initiatives != 0,
                  'character_idle_seconds': s.character_idle_seconds, 'character_max_initiatives': s.character_max_initiatives,
                  'bgm_volume_db': bgm_db, 'bgm_tracks': [track.stem for track in playlist],
                  'bgm_missing_tracks': [track.stem for track in playlist if not track.is_file()]}
        if twitch:
            from ravora.twitch_chat import cli_config_path
            report.update(twitch_ja_tts=s.twitch_ja_tts, twitch_en_tts=s.twitch_en_tts,
                          tts_language_confidence=s.tts_language_confidence)
            report.update(publish_destination=destination, chat_input='twitch' if twitch_chat else 'host_only',
                          scheduled_audience_required=scheduled_idle is not None, interrupt_initiatives=bool(scheduled),
                          twitch_chat_cli_config_exists=cli_config_path().is_file())
        else:
            report.update(youtube_read_enabled=s.youtube_read_enabled, youtube_api_key_set=bool(s.youtube_key),
                          chat_requests_per_hour=s.youtube_chat_requests_per_hour,
                          chat_daily_unit_limit=s.youtube_chat_daily_units, chat_target_set=configured_target,
                          broadcast_metadata=metadata, self_declared_made_for_kids=made_for_kids,
                          latency_preference=latency_preference, oauth_client_file=(directory/'client.json').is_file(),
                          oauth_token_file=(directory/'token.json').is_file())
        print(json.dumps(report, ensure_ascii=False))
        return
    if publishing and args.mode != 'cloud':
        raise SettingsError('PUBLISH_REQUIRES_CLOUD_MODE')
    if args.mode == 'cloud':
        if not s.api_enabled:
            raise SettingsError('RAVORA_API_ENABLED_REQUIRED')
        s.require_ready(google_tts=google_speech)
        if not host_input_only and not s.youtube_read_enabled:
            raise SettingsError('YOUTUBE_READ_ENABLED_REQUIRED')
        if not host_input_only and not managed and not (s.youtube_key and configured_target):
            raise SettingsError('CONFIGURE_YOUTUBE_READ_ENABLED_API_KEY_AND_VIDEO_OR_CHAT_ID')
        comment_source = 'twitch' if twitch_chat else ('developer' if host_input_only else 'youtube')
        s = replace(s, comment_source=comment_source,
                    youtube_read_enabled=False if host_input_only else s.youtube_read_enabled)
    else:
        s = replace(s, api_enabled=False, memory_api_enabled=False, stt_api_enabled=False,
                    youtube_read_enabled=False, comment_source='developer' if args.host_only else 'youtube-fixture')
    if not s.character_enabled:
        raise SettingsError('RAVORA_CHARACTER_ENABLED_REQUIRED')
    url = None
    if publishing and not managed:
        target_for = twitch_publish_target if twitch else publish_target
        url = target_for(base, stream_key)
    # Native libav errors can embed its destination. Report stable codes only.
    av.logging.set_level(av.logging.PANIC)
    stop = threading.Event()
    def request_stop(*_):
        stop.set()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, request_stop)
    signal.signal(signal.SIGHUP, signal.SIG_IGN if args.seconds is None else request_stop)
    reload_policy = threading.Event()
    if scheduled:
        signal.signal(signal.SIGUSR1, lambda *_: reload_policy.set())
    runtime = ROOT / '.runtime'
    runtime.mkdir(mode=0o700, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix='twitch-live-' if twitch else 'youtube-live-', dir=runtime))
    print('Run:', run_dir, flush=True)
    print('Replies:', 'offline English fixture' if args.mode == 'offline' else s.gemini_model, flush=True)
    with ExitStack() as stack:
        bgm = LoopingMusic(playlist, bgm_db) if playlist else None
        if bgm is not None:
            stack.callback(bgm.close)
        broadcast = None
        previous_id = None
        if scheduled:
            from ravora.database import Database
            from ravora.twitch_schedule import START_GRACE, predecessor, scheduled_lock, slot_at
            previous = predecessor(Database.existing(ROOT/'state/conversation.sqlite3'), scheduled)
            previous_id = previous[0] if previous else None
            stack.enter_context(scheduled_lock(directory, scheduled, previous))
        elif twitch:
            stack.enter_context(publisher_lock(directory, busy_code='RAY_PUBLISHER_ALREADY_RUNNING'))
        elif publishing:
            stack.enter_context(publisher_lock(directory))
        if scheduled:
            from ravora.twitch_schedule_api import prepare_channel
            if not twitch or slot_at(time.time(), launching=True) != scheduled:
                raise SettingsError('TWITCH_SLOT_EXPIRED')
            prepare_channel(ROOT/'state/twitch', scheduled.language, update=True, independent_chat=True, previous=previous_id,
                            until=scheduled.start+START_GRACE)
        if managed:
            api, channel = ChannelAPI.connect(directory)
            stack.callback(api.http.close)
            broadcast = Broadcast(api, directory, channel, args.title, args.privacy or 'unlisted', stop,
                                  metadata=metadata, made_for_kids=made_for_kids, latency_preference=latency_preference)
        audio = Path(stack.enter_context(tempfile.TemporaryDirectory(dir=run_dir)))
        store = MemoryStore(ROOT / 'state/conversation.sqlite3' if args.mode == 'cloud' else run_dir / 'offline.sqlite3')
        store.scheduled_budget = bool(scheduled)
        CharacterMemory(store, s.mode).seed_initial_interests()
        language = args.host_language or s.stt_language
        default_locale = s.voice_input_language if s.voice_profile == 'fast_streaming' else s.language
        if args.host_language is None and language == 'auto' and (args.dell_mic or s.mode == 'cloud'):
            language = default_locale[:2]
        providers = profiles = None
        if s.mode == 'cloud':
            from ravora.live_voice import live_reply_profiles
            from ravora.providers import JevReaction
            if google_speech and s.voice_profile == 'fast_streaming' and not s.tts_key:
                raise SettingsError('GOOGLE_TTS_API_KEY_REQUIRED_FOR_CHIRP')
            profiles = live_reply_profiles(s, twitch=twitch)
            if twitch:
                from ravora.twitch_voice import warm_twitch_voices
                warm_twitch_voices(profiles, allow_unavailable=True)
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
                              onset_dbfs=onset, silence_ms=silence, host_trim_db=host_trim_db,
                              host_mic_cleanup=host_mic_cleanup, ray_reverb_mix=ray_reverb_mix, bgm=bgm, prefetch=args.prefetch,
                              initiative_language=scheduled.language if scheduled else None, interrupt_initiatives=bool(scheduled),
                              prompt_title=scheduled.prompt_title if twitch and scheduled else '')
        program.voice_env_file = args.env
        stack.callback(program.close)
        from ravora.studio_operator import configure
        configure(program, voice)
        if args.dell_mic:
            from ravora.studio_dell_stt import configure as configure_local_microphone
            configure_local_microphone(program, voice)
        print('Host STT:', program.host_stt_provider, flush=True)
        if program.cloud_stt and language != 'auto':
            from ravora.providers import ProviderError
            try:
                program.cloud_stt.access_token()
            except ProviderError as exc:
                print('Host STT authentication:', str(exc), flush=True)
        secret = secrets.token_urlsafe(32)
        try:
            server = DeveloperHTTP(program, secret, args.port, handler=LiveHandler)
        except OSError as exc:
            code = ('HOST_CONSOLE_PORT_IN_USE' if exc.errno == errno.EADDRINUSE
                    else 'HOST_CONSOLE_BIND_FAILED')
            raise SettingsError(f'{code} 127.0.0.1:{args.port} ({errno.errorcode.get(exc.errno, "UNKNOWN")})') from None
        stack.callback(server.server_close)
        server.metrics = StudioMetrics(program)
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
        if stop.is_set():
            return
        # Open the selected encoder before a managed broadcast is created or bound.
        if scheduled:
            # Recheck just before opening the RTMPS destination after avatar setup.
            if slot_at(time.time(), launching=True) != scheduled:
                raise SettingsError('TWITCH_SLOT_EXPIRED')
            channel = prepare_channel(ROOT/'state/twitch',scheduled.language,update=False,independent_chat=True)
            if not channel['matches_slot']:
                raise SettingsError('TWITCH_CHANNEL_UPDATE_UNCONFIRMED')
        try:
            writer = ProgramWriter(url if publishing else run_dir / 'program.flv', scene,
                                   publish=publishing, video_encoder=video_encoder)
        except Exception:
            raise SettingsError(f'PROGRAM_ENCODER_OPEN_FAILED ({video_encoder})') from None
        stack.callback(writer.close)
        if not broadcast:
            writer.bgm = bgm
        active_encoder = writer.video.codec_context.name
        print('Video encoder:', active_encoder, flush=True)
        if twitch:
            print('Publish destination: Twitch', flush=True)
        if broadcast:
            url = broadcast.resume_live(args.resume_live) if args.resume_live else broadcast.prepare()
            video_id = broadcast.state['broadcast']
            print('YouTube watch URL: https://www.youtube.com/watch?v=' + video_id, flush=True)
            if stop.is_set():
                return
            writer.start_publish(url)
        if writer.transport:
            program.transport = writer.transport
            def save_transport():
                (run_dir / 'transport.json').write_text(json.dumps(writer.transport.status(), indent=2) + '\n')
            stack.callback(save_transport)
        if broadcast:
            observations = program.observe('youtube', video_id, 'managed_youtube')
        elif destination == 'youtube' and s.youtube_video_id:
            observations = program.observe('youtube', s.youtube_video_id, 'configured_youtube_video')
        else:
            observations = program.observe(destination or 'local')
        if scheduled_idle is not None:
            pipeline.audience_present = observations.has_audience
            pipeline.audience_size = observations.audience_size
        if scheduled:
            with store.connect() as db:
                if db.execute('UPDATE twitch_schedule_runs SET session=? WHERE scheduled_start=? AND session IS NULL',
                              (pipeline.inbox.session,scheduled.start)).rowcount != 1:
                    raise SettingsError('TWITCH_SLOT_BINDING_FAILED')
        print('Studio session:', pipeline.inbox.session, flush=True)
        server.metrics.start(writer)
        stack.callback(server.metrics.close)
        operator_url = run_dir / 'operator.url'
        console_url = f'http://127.0.0.1:{server.server_port}/#{secret}'
        operator_url.write_text(console_url + '\n')
        stack.callback(operator_url.unlink, missing_ok=True)
        if args.operator_url_link:
            args.operator_url_link.symlink_to(operator_url)
            stack.callback(args.operator_url_link.unlink, missing_ok=True)
        print('Host console URL file:', operator_url, flush=True)
        if sys.stdout.isatty():
            print('Host console URL:', console_url, flush=True)
        stack.callback(program.stopped.set)
        stack.callback(pipeline.stop.set)
        startup = None
        chat_lookup = None
        chat = None
        live = False
        reused_stream = threading.Event()
        if broadcast:
            executor = stack.enter_context(ThreadPoolExecutor(max_workers=1, thread_name_prefix='youtube-start'))
            startup = (executor.submit(broadcast.wait_live, resume_only=True) if args.resume_live
                       else executor.submit(broadcast.wait_live))
            stack.callback(stop.set)
        elif twitch_chat:
            from ravora.twitch_chat import TwitchReader
            welcome = None
            if scheduled:
                from ravora.twitch_welcome import ScheduledWelcome
                welcome = ScheduledWelcome(store, ROOT/'state/twitch', scheduled, pipeline.inbox.session, stop)

            def record_audience(observed, status, stream_id=None, viewers=None, started=None):
                if scheduled and status=='live' and previous_id and stream_id==previous_id:
                    reused_stream.set()
                    stop.set()
                    return
                observations.audience(observed,status,stream_id,viewers,started, transport=writer.transport.status() if scheduled and writer.transport and status == 'live' else None)
                if welcome and status == 'live':
                    welcome.on_live(stream_id)
            from ravora.twitch_audience import TwitchAudience
            account = {'user_id':channel['user_id'],'client_id':channel['client_id']} if scheduled else {}
            stack.enter_context(TwitchAudience(ROOT/'state/twitch',record_audience,**account))
            reader = TwitchReader(pipeline.inbox, ROOT / 'state/twitch/oauth.json', on_removed=pipeline.check_removed,
                                  is_announcement=welcome.is_announcement if welcome else None,
                                  expected_user_id=account.get('user_id'), irc_token_path=ROOT/'state/twitch/irc-oauth.json')
            from ravora.twitch_intake import attach_intake
            attach_intake(program, reader)
            pipeline.start(args.seconds, twitch_reader=reader)
        else:
            pipeline.start(args.seconds, wait_for_chat=True)
        start = time.monotonic()
        deadline = start + args.seconds if args.seconds is not None else float('inf')
        if scheduled:
            deadline = min(deadline,start + max(0.0,scheduled.end-time.time()))
        max_lag = 0.0
        finish_event = False
        try:
            while not stop.is_set():
                now = time.monotonic()
                if now >= deadline:
                    from ravora.twitch_schedule import DRAIN_SECONDS
                    if not scheduled or now >= deadline + DRAIN_SECONDS or not program.finish_slot_turn():
                        break
                if startup and startup.done():
                    event = startup.result()
                    if event is None:
                        break
                    live = True
                    writer.bgm = bgm
                    observations.provider_event('live')
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
                        remaining_chat = None if args.seconds is None else max(1, deadline - time.monotonic())
                        pipeline.start(remaining_chat, access_token=api.access_token)
                    chat_lookup = None
                if reload_policy.is_set():
                    reload_policy.clear()
                    try:
                        program.restart_speech_policy()
                    except Exception:
                        print('SPEECH_POLICY_RELOAD_FAILED', flush=True)
                stage, pcm = program.next_frame() if not broadcast or live else (Stage(), SILENCE)
                scene.clip = program.clip
                writer.write(stage, pcm)
                program.encoded(stage)
                remaining = start + writer.frames / FPS - time.monotonic()
                max_lag = max(max_lag, -remaining)
                stop.wait(max(0, remaining))
            finish_event = live or not args.resume_live
            if broadcast and not live and not stop.is_set():
                raise YouTubeError('YOUTUBE_DURATION_ENDED_BEFORE_LIVE')
        finally:
            playout_seconds = time.monotonic() - start
            stop.set()
            program.stopped.set()
            pipeline.stop.set()
            try:
                writer.close()
            finally:
                if broadcast:
                    executor.shutdown(wait=True)
                    # Only an operator stop or an explicit session deadline ends an owned event.
                    if finish_event and broadcast.finish() == 'complete':
                        observations.provider_event('ended')
        if reused_stream.is_set():
            raise SettingsError('TWITCH_PREVIOUS_STREAM_ID_REUSED')
        summary = program.status()
        summary.pop('stage')
        summary.pop('monitor_audio')
        summary.pop('monitor_offset_seconds')
        summary['elapsed_seconds'] = round(time.monotonic() - start, 3)
        summary['playout_seconds'] = round(playout_seconds, 3)
        summary['average_fps'] = round(writer.frames / max(playout_seconds, .001), 2)
        summary['video_encoder'] = active_encoder
        summary['max_encoder_lag_ms'] = round(max_lag * 1000)
        summary['studio_session'] = pipeline.inbox.session
        summary['prefetch'] = args.prefetch
        summary['publish'] = publishing
        summary['publish_destination'] = destination
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


def main(argv=None, *, scheduled=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('offline', 'cloud'), default='offline')
    parser.add_argument('--config', type=Path, help='TOML encoders, mixer, microphone and optional portrait settings')
    parser.add_argument('--env', type=Path, default=ROOT / '.env')
    duration = parser.add_mutually_exclusive_group()
    duration.add_argument('--seconds', type=int, default=180)
    duration.add_argument('--until-stopped', dest='seconds', action='store_const', const=None,
                          help='Publish until explicitly stopped, without a session timer')
    parser.add_argument('--operator-url-link', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--port', type=int, default=8790)
    parser.add_argument('--dell-mic', action='store_true', help='Control the configured Dell microphone from the host console')
    parser.add_argument('--prefetch', action=argparse.BooleanOptionalAction, default=False,
                        help='Prepare one queued viewer reply during Ray playback')
    parser.add_argument('--host-only', action='store_true', help='Record the conversation locally without YouTube or sample chat')
    parser.add_argument('--host-language', choices=('ja','en','auto'), help='Initial conversation language (auto detects microphone input)')
    parser.add_argument('--check', action='store_true', help='Check configuration without connecting or opening an encoder')
    parser.add_argument('--publish', action='store_true', help='Send the program to the configured YouTube RTMPS ingest')
    parser.add_argument('--publish-twitch', action='store_true', help='Send the program to Twitch using TWITCH_STREAM_KEY')
    parser.add_argument('--auto-broadcast', action='store_true', help='Create or resume a Ray broadcast, then go live over RTMPS')
    parser.add_argument('--resume-live', metavar='VIDEO_ID',
                        help='Reattach only to this already-live managed event, preserving its visibility')
    parser.add_argument('--authorize-youtube', action='store_true', help='Authorize the Ray channel on loopback port 8791')
    parser.add_argument('--privacy', choices=('unlisted', 'private', 'public'), help='Managed broadcast visibility (default: unlisted)')
    parser.add_argument('--title', help='Override the configured title for a new managed broadcast')
    parser.add_argument('--scene-dir', type=Path)
    args = parser.parse_args(argv)
    if args.publish_twitch and (args.publish or args.auto_broadcast or args.authorize_youtube):
        parser.error('--publish-twitch cannot be combined with YouTube publishing or authorization')
    if args.resume_live is not None and (not args.resume_live or not args.auto_broadcast or not args.privacy or args.title):
        parser.error('--resume-live requires --auto-broadcast and the current --privacy, without --title')
    if (args.seconds is not None and not 1 <= args.seconds <= 86400) or not 0 <= args.port <= 65535:
        parser.error('invalid duration or port')
    if args.seconds is None and not (args.auto_broadcast or args.publish or args.publish_twitch):
        parser.error('--until-stopped requires publishing')
    if args.authorize_youtube and (args.publish or args.auto_broadcast or args.check or args.host_only):
        parser.error('--authorize-youtube runs separately from checking or publishing')
    os.umask(0o077)
    logging.basicConfig(format='%(message)s')
    logging.getLogger('ravora.youtube').setLevel(logging.INFO)
    logging.getLogger('ravora.twitch_chat').setLevel(logging.INFO)
    try:
        if scheduled is None:
            run(args)
        else:
            run(args, scheduled=scheduled)
    except (SettingsError, TwitchError, ModuleNotFoundError) as exc:
        sys.exit(str(exc))
    except ValueError as exc:
        code = str(exc)
        sys.exit(code if code.isupper() and len(code) < 100 else 'PROGRAM_CONFIGURATION_FAILED')
    except YouTubeError as exc:
        sys.exit(str(exc))
    except RuntimeError:
        sys.exit('PROGRAM_FAILED')
    except Exception:
        sys.exit('PROGRAM_IO_FAILED')


if __name__ == '__main__':
    main()
