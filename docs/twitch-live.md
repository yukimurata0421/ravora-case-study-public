# Ray on Twitch

The Linux studio can send its video, burned-in captions, Ray's voice and the
host microphone to Twitch over RTMPS. The Twitch profile uses 1080p30 H.264
at 4.5 Mbps with a two-second keyframe interval and 48 kHz stereo AAC at
128 kbps. Start one publisher per studio directory. Both cloud destinations
use the same conversation and memory database. Viewer history is keyed by
platform and account.

The [scheduled broadcast and observation guide](studio-observations.md#scheduled-twitch-shows-and-prepared-replies) covers the weekly slots, market sampling, welcome pin and overlapping reply preparation.

See [bilingual GPU speech and the k3s player observer](twitch-local-tts.md) for voice setup, fallback settings and observed reply times.

## Configure the channel

Install the studio and prepare the private working directory as described in
[the live studio guide](youtube-live.md). Put the Twitch stream key in
`TWITCH_STREAM_KEY` in the private `.env` with mode 0600. Use the default
Twitch ingest endpoint or set `TWITCH_RTMPS_URL` to the secure Twitch ingest
template with the final stream key placeholder removed. Set the title,
category and stream language in the Twitch Stream Manager.
[Twitch ingest reference](https://dev.twitch.tv/docs/video-broadcast/reference/ingest/)

For chat, install the Twitch CLI on the Linux studio host and configure your
own Twitch application. Authorize the broadcaster's account in a browser
using the URL shown by the CLI. The client uses the CLI credentials of the
Linux user running the studio and keeps refreshed grants in a private
`state/twitch/oauth.json` file.
[Twitch CLI device authorization](https://github.com/twitchdev/twitch-cli/blob/main/docs/token.md#device-code-flow)

```bash
umask 077
twitch configure
twitch token --user-token --scopes 'user:read:chat' --dcf
```

Ray subscribes to chat messages and individual deletions through EventSub.
Replies appear in the stream's audio and captions, with the language chosen
from each English or Japanese message. The host language tab continues to
control microphone conversation and supplies the fallback for ambiguous chat.
Set `TWITCH_CHAT_ENABLED=false` for a stream with host conversation alone.
[Twitch EventSub WebSocket lifecycle](https://dev.twitch.tv/docs/eventsub/handling-websocket-events/)

## Select the H.264 encoder and start

The default Twitch encoder is `libx264`. Add this section to your local
`studio.toml` to use NVIDIA NVENC.

```toml
[twitch_live]
video_encoder = "h264_nvenc"
```

Run the configuration check in the private studio directory. It reports key
presence, the selected encoder and whether the CLI configuration exists.
The check opens neither a device nor a network connection.

```bash
.venv/bin/ravora-youtube-live --publish-twitch --config studio.toml --check
```

The next command starts a real Twitch broadcast for 300 seconds. Provide the
conversation credentials and follow the [microphone setup](youtube-live.md#talk-to-ray)
if you are using a microphone. Open the operator URL and wait for the chat
indicator to show **Connected** before posting a test message.

```bash
RAVORA_API_ENABLED=true .venv/bin/ravora-youtube-live --mode cloud \
  --publish-twitch --config studio.toml --port 8793 --seconds 300
```

On Windows, PowerShell can forward the port and open the console when the
studio emits its operator URL. Replace `ray-host` with your SSH alias.
Append `--dell-mic` to the remote command if you configured the Linux host
microphone. Ctrl+C ends the foreground studio session.

```powershell
ssh.exe -tt -o ExitOnForwardFailure=yes -L 127.0.0.1:8793:127.0.0.1:8793 ray-host 'cd ~/ray-studio && RAVORA_API_ENABLED=true .venv/bin/ravora-youtube-live --mode cloud --publish-twitch --config studio.toml --port 8793 --seconds 300' |
    ForEach-Object {
        if ($_ -cmatch '^Host console URL: (http://127\.0\.0\.1:8793/#[A-Za-z0-9_-]{43})\s*$') { Start-Process -FilePath $Matches[1] }
        else { $_ }
    }
```

The summary in `.runtime/twitch-live-*/summary.json` identifies the encoder,
Twitch destination, chat state, local playback and transport counters. Check
Twitch Stream Manager and the watch player for delivery. The
[operations record](twitch-live-operations-20261124.md) shows how a real
reply was timed at both the sender and the player.

For continuous publishing and reopening the same operator screen, use the
[user-service setup and Windows launcher](studio-operations.md#run-the-studio-as-a-linux-user-service).
