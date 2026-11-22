# Ray live studio

The Linux studio mixes the host microphone and Ray's voice into one 1920 by 1080, 30 fps program. It draws chat, expressions and subtitles into the encoded video. The host can speak over Ray, mute the microphone independently, or switch between Japanese and English in the operator console. A local recording exercises the same HEVC and AAC encoder used for Enhanced RTMP over TLS.

## Start a local recording

Use Python 3.12 or newer for the media extra. On Debian or Ubuntu install Chromium or Google Chrome, FFmpeg, libflite (including the US SLT voice), DejaVu and Noto CJK fonts. From the repository root, create a private working directory for recordings and credentials.

```bash
studio="$HOME/ray-studio"
install -d -m 700 "$studio"
python3 -m venv "$studio/.venv"
"$studio/.venv/bin/python" -m pip install '.[youtube-live,local-stt]'
install -m 600 .env.example "$studio/.env"
cd "$studio"
.venv/bin/ravora-youtube-live --mode offline --seconds 30
```

Open the printed host console URL while the command is running. A redirected terminal writes the URL to `operator.url` in the run directory. The operator token expires on exit. The studio records to `.runtime/youtube-live-*/program.flv`. Offline mode uses synthetic chat and fixed English speech. It needs no provider credentials. The public canvas draws a synthetic face by default. To animate your own registered portrait, pass the same `--config PATH` and `[pngtuber]` manifest as the [conversation console](reproduce.md). Alternatively pass `--scene-dir PATH` with a 1920 by 1080 `neutral.png` and optional `joy.png`, `surprise.png`, `thinking.png` and `concerned.png`.

## Select the HEVC encoder

For NVIDIA hardware with HEVC encoding support, copy the [NVENC example](../examples/studio-nvenc.toml) to your working directory as `studio.toml`, or add its section to your existing microphone and portrait configuration.

```toml
[youtube_live]
video_encoder = "hevc_nvenc"
```

```bash
.venv/bin/ravora-youtube-live --config studio.toml --check
.venv/bin/ravora-youtube-live --config studio.toml --mode offline --seconds 30
```

`--check` validates the configured name. The recording opens the encoder and reports `Video encoder: hevc_nvenc` when it succeeds. The same setting applies to `--publish` and `--auto-broadcast`. A missing driver or encoder stops GPU startup. To run on CPU, set `video_encoder = "libx265"`. Omitting the setting also selects the CPU encoder. Both produce 1080p30 HEVC with AAC audio. The run summary records the encoder actually opened.

The WebGL renderer applies the November 22 mouth and jaw revision to compatible registered portraits. With a portrait loaded, the launcher prints its manifest name and records its binding digest in `summary.json`. The public example can also run with the built-in synthetic face. [Reference render and comparison](audio-timing.md#november-22-mouth-and-jaw)

## Talk to Ray

In the private working directory, install the four files for the pinned multilingual recognizer. The studio checks their SHA-256 digests at worker startup. Microphone audio is transcribed locally by faster-whisper on CPU using its built-in speech filter. Model download is an explicit preparation step.

```bash
.venv/bin/hf download Systran/faster-whisper-base config.json tokenizer.json vocabulary.txt model.bin \
  --revision ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66 --local-dir assets/stt/local-model
```

Enter your conversation, reaction and speech credentials in the private `.env` using `GEMINI_API_KEY`, `JEV_API_KEY` and `GOOGLE_TTS_API_KEY`. The studio sends recognized text and selected conversation context to the configured services. Cloud mode stores acknowledged dialogue and source-backed character memories in `state/conversation.sqlite3`. The transient character state lives in process memory.

```bash
RAVORA_API_ENABLED=true .venv/bin/ravora-youtube-live --mode cloud --host-only --seconds 300
```

The default microphone is the browser's input. To use a microphone attached to the Linux host, set `[developer_voice].dell_device` in a local TOML file to the ALSA card ID and PCM number (for example `"USB,0"`), then append `--config studio.toml --dell-mic`. On Windows, the same studio runs through an SSH tunnel. Replace `ray-host` with your SSH alias.

```powershell
ssh.exe -t -o ExitOnForwardFailure=yes -L 127.0.0.1:8790:127.0.0.1:8790 ray-host 'cd ~/ray-studio && RAVORA_API_ENABLED=true .venv/bin/ravora-youtube-live --mode cloud --host-only --seconds 300'
```

Select the Japanese or English tab and turn the microphone on once. Hands-free mode commits an utterance after a pause. The tab selects the language for recognition, reply text, subtitles and speech. A turn already being captured or answered retains its language. History is shared across languages. Manual mode lets the host mark the start and end of a question. The microphone remains in the mix during Ray's replies, while automatic recognition pauses to avoid treating playback as another question.

Host auto gain tracks Ray's active speech level on the program bus. The gain adjustment is bounded to 18 dB in either direction and releases in quiet passages. The recognizer receives the original microphone samples. The monitor plays Ray's reply. Listen to the local recording to check the balance of both speakers.

## Start an unlisted broadcast

Enable live streaming on the destination channel and the YouTube Data API v3 in its Google Cloud project. Create a Desktop OAuth client in Google Auth Platform, configure the consent screen and add the channel operator as a test user when the app is in testing. The studio requests the `youtube.force-ssl` scope. Store the downloaded Desktop client file and complete the loopback browser authorization from the studio directory.

```bash
install -d -m 700 state/youtube
install -m 600 /path/to/downloaded-client.json state/youtube/client.json
.venv/bin/ravora-youtube-live --authorize-youtube
```

For SSH authorization forward `127.0.0.1:8791` to the same port on the host, open the consent link on Windows and leave SSH connected until authorization completes. To create the event, bind an ingestion stream and start publishing, run the following in the studio directory with the console tunnel on port 8790.

```bash
RAVORA_API_ENABLED=true YOUTUBE_READ_ENABLED=true .venv/bin/ravora-youtube-live \
  --mode cloud --auto-broadcast --privacy unlisted --seconds 300
```

The managed command obtains the video ID, stream destination and chat ID from the authorized channel. OAuth covers chat in this mode. The reader subscribes to gRPC `streamList`, persists resume tokens and deduplicates deliveries. Ray answers selected comments in the program audio and subtitles. The command waits for active ingest before transitioning live and ends the owned event on normal shutdown. An uncertain provider write is reconciled against the stored event before another write. Preserve `state/youtube/` between runs.

The separate Studio-managed route accepts `YOUTUBE_STREAM_KEY`, `YOUTUBE_API_KEY` and either `YOUTUBE_LIVE_CHAT_ID` or `YOUTUBE_VIDEO_ID` with `--publish`. The program summary records encoder-side completion. Use YouTube Studio and the watch player to check actual delivery.

Recordings can contain room audio, display names, comments and captions. Keep the working directory private. The public tests use a local TLS chat server, a simulated channel and local encoded media.
