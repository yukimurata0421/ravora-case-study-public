# Operating the live studio

The studio keeps one program clock for 1080p30 video, host audio and Ray's
voice. Each publisher owns a bounded packet queue and a single RTMPS muxing
worker. The producer requests a fresh keyframe after a broken connection or
queue overflow, and each new connection gets timestamps relative to that
keyframe. The sender drops stale media and reports failed or uncertain
writes with stable error codes. The transport counters describe progress on
the sender. Use provider state and a watch player to check receipt and playout.

## Configure the mix

Use the Linux studio directory and pass `--config studio.toml`. Both destinations
use these shared controls from `[youtube_live]` for compatibility with earlier
studio configurations. The encoders remain destination-specific.

```toml
[youtube_live]
host_trim_db = 0.0
host_mic_cleanup = true
ray_reverb_mix = 0.06
bgm_tracks = []
bgm_volume_db = -24.0
```

Set `bgm_tracks` to an ordered list of your own MP3 files, relative to the
configuration file. The last track loops back to the first. An empty list
disables music. The authenticated console controls the program's host trim and
BGM volume without restarting the stream. Host trim follows auto gain. Breath
and click reduction applies to the outgoing microphone mix. Recognition and
end-of-utterance detection use the original microphone audio.

The first validated speech segment can begin on the program clock before the
streaming TTS call completes. A failed or cancelled prefix cannot produce a
successful playback acknowledgment. Subtitles use width-aware font sizing
and pagination with the word highlight tied to audio position. The Windows
reply monitor tracks numbered audio segments and buffers small delivery gaps.
It plays Ray's voice. Use the watch page to judge the complete program mix.

Use local STT with the [pinned recognizer](youtube-live.md#talk-to-ray), or enable
the cloud route explicitly with `RAVORA_API_ENABLED=true`,
`RAVORA_STT_API_ENABLED=true` and `GOOGLE_CLOUD_PROJECT` in the private environment.
Provide a valid `GOOGLE_CLOUD_ACCESS_TOKEN` locally or sign in with the Google
Cloud CLI in the service user's context. An explicit English or Japanese host
tab selects cloud recognition. Automatic language selection uses local STT.
The local recognizer applies a coarse RMS gate before its speech model and
maps compact audio timestamps back to the original capture.

## Run the studio as a Linux user service

On a systemd host, prepare `~/ray-studio` with the installed package, a
private environment and any optional `studio.toml`. Complete the destination's
setup first. Copy the user unit and scripts from the repository. The
service is opt-in and has no automatic restart policy.

```bash
repo="$PWD"
install -d -m 700 "$HOME/.config/systemd/user"
install -m 644 "$repo/examples/ray-studio@.service" "$HOME/.config/systemd/user/"
install -m 700 "$repo"/scripts/ray-studio-{console,service}.sh "$HOME/ray-studio/"
systemctl --user daemon-reload
```

Use `RAY_STUDIO_HOST_MIC=dell` in a user-unit override to select the Linux
microphone. Set its ALSA device in `[developer_voice].dell_device` and give the
service user access to that device or the local audio session. The user unit
does not grant extra privileges. Keeping the service up after every login
session closes requires the host's user-manager persistence policy.

From the repository on Windows with PowerShell 7, run the following with
your own SSH alias and working key authentication. The launcher opens
or reuses a matching SSH tunnel, starts an inactive service or attaches
to the active one, authenticates to its loopback status endpoint and
opens the browser. The private console link is held in the per-user runtime
directory and expires with that session.

```powershell
pwsh -NoProfile -File .\scripts\Start-RayLive.ps1 ray-host -Destination twitch
```

Use `-Destination youtube` for an unlisted managed YouTube broadcast.
Stop a destination using its user unit on the studio host. Normal YouTube
shutdown completes the owned live event.

```bash
systemctl --user stop ray-studio@twitch.service
```

For a maintenance recovery of an already-live YouTube event, use the
[pinned event procedure](youtube-live.md#recover-the-same-live-event).
The shared publisher lock permits one publisher per studio directory.

## Read the console and verify delivery

[Per-broadcast observations, the public dashboard and its offline reproduction](studio-observations.md) cover first commenters, viewer counts, reply timing and API usage across sessions.

The operator page samples FPS, encoded and sent rate, queue occupancy and
errors. Its API view counts dispatched calls, duration, characters and
reported tokens for the current session. A missing provider usage field is
displayed as unknown. These are local observations. Compare an advancing
sender counter with the provider's live/ingest state and inspect the watch page.

In a local reproduction, use synthetic input to compare the developer and
studio milestones. The report command reads a local SQLite file supplied
by the operator and outputs aggregate timings only.

```bash
python3 -B scripts/compare_voice_paths.py
python3 -B scripts/report_voice_latency.py state/conversation.sqlite3 --limit 60
```

One synthetic check of the public port measured 627.4 ms from turn start
to program audio with complete-WAV buffering and 366.3 ms with progressive
PCM. The two runs used the same generated input and synthetic TTS packet
cadence. This comparison exercises the local path. It does not estimate
network or provider latency.

The historical observations appear in the
[YouTube operations record](youtube-live-operations-20261124.md) and the
[Twitch operations record](twitch-live-operations-20261124.md). They distinguish
encoder progress, provider status, program audio and viewer playback.
