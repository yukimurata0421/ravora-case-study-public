# Twitch delivery and comment reply measurement

On 2026-11-24 JST, Ray sent a 1080p stream to Twitch using H.264 NVENC and
answered real chat messages in program audio. A browser measurement on one
test comment found **4.44 seconds from Twitch's chat event to the start of
Ray's reply audio in the watch player**. Of that time, about 2.13 seconds
elapsed before the reply reached the sender's program output.

## Implementation and evidence

The Twitch branch uses a bounded packet FIFO and a single RTMPS muxing
worker. After a dropped connection or queue overflow, the sender discards
stale packets and resumes on a fresh video keyframe. Twitch EventSub gets
channel messages and individual deletions using the broadcaster's own
authorization. The same inbox deduplicates deliveries, applies expiry and
passes selected messages to Ray's reply pipeline. English and Japanese chat
use the matching voice and captions. The host conversation uses its own
language selection. [Studio setup](twitch-live.md)

| Evidence | Observation |
| --- | --- |
| Local encoded receiver | The development test decoded H.264 video at 1920×1080 and 30 fps with 48 kHz stereo AAC from a local TLS receiver. That trial used the CPU encoder and a synthetic confirmation tone. |
| Live sender and provider | At 02:16:14 the Linux studio reported H.264 NVENC and Twitch output. At 02:16:16 EventSub connected. At 02:17:27 the Twitch API returned a live stream. |
| Live chat and program | At 02:17:53 a real comment entered the inbox. Ray's audio entered the program 2.23 s after receipt and finished 4.89 s after receipt. The next comment waited 6.31 s for its turn while the previous speech completed. |
| Reply admission | Three messages at 02:57–02:58 reached the inbox and stopped at `LOCAL_MONTHLY_REPLY_LLM_LIMIT`. The failure was recorded after receipt and before a reply was sent. Restarting with an adjusted reply budget restored program replies. |
| Budget update | At 03:13:27 the studio restarted with a 20 USD local monthly reply reservation, 6 hours of metered STT input and 900,000 TTS characters. EventSub reconnected at 03:13:33, and the Twitch API reported live at 03:13:34. The settings were sized against a 10,000 JPY monthly operating target. Independent provider usage and currency conversion affect the invoice. |
| Post-restart chat | At 03:33:47 another real chat message entered the inbox. Its reply reached program audio after 2.86 s and completed after 5.25 s from receipt. |
| Watch player | A separate test at 03:39 measured the 4.44 s result below from an actual Twitch playback session. |

The observation window runs from 02:16 to 03:40 JST. The local inbox
contained ten Twitch receipts in that window, seven marked played and three
limited by the earlier reply budget. This count covers received records.
Provider live state, sender playout and player audio were checked separately.
The historical service was running a development checkout with local changes.

## What the viewer received

The operator sent a test comment while the public Twitch watch page was
playing at 1920×1080 in Chromium on the same Linux host as the sender.
Audio was captured from the browser's independent PulseAudio output.
The captured waveform was matched against the beginning of Ray's reply from
the local operator monitor for the same turn. The first two aligned windows
had normalized correlations of 0.978 and 0.982. The browser capture began
before the test comment arrived.

| Stage | Elapsed | Evidence |
| --- | ---: | --- |
| Twitch chat event to studio receipt | 0.209 s | EventSub `message_timestamp` at 03:39:44.691 to inbox receipt at 03:39:44.900. |
| Studio receipt to turn start | 0.041 s | Turn creation timestamp. |
| Turn start to validated public text | 1.155 s | `public_output_ready_ms` includes prompt preparation, generation and output validation. The 0.363 s reaction ran in parallel. |
| Public text to initial TTS PCM | 0.547 s | Difference between `first_pcm_ms` and `public_output_ready_ms`. |
| First PCM to program audio | About 0.181 s | Difference between first PCM and the program audio-onset metric, using nearby worker and program clock origins. |
| Program audio to watch-player audio | About 2.303 s | First program audio at approximately 03:39:46.825 to the matched browser audio at 03:39:49.128. |
| **Twitch event to watch-player audio** | **About 4.44 s** | Event timestamp to observed browser audio. |

The delivery and playback interval accounts for about 52% of this sample.
It includes encoding, ingest, Twitch distribution and player buffering.
Their individual times and the gap from pressing Send to the Twitch event
are unmeasured. The capture measures one browser and network path. Windows
playback, latency percentiles and long-run continuity remain unverified.
The first capture attempt collided with an existing audio bus name, and the
second failed to start a browser renderer. The result uses the subsequent
isolated capture, in which video and audio advanced.

## Reproduce and interpret

- Exercise the portable implementation with the installed-package tests
  for EventSub handoff, duplicate events and deletion, bilingual replies,
  H.264 and AAC decode, and reconnect to a loopback receiver.
- For a fresh live measurement, start capturing the watch player's audio
  before posting a distinctive test comment. Find the corresponding
  `comment_inbox` and `conversation_turns` records and their
  `utterances.playback_ack_at` values. Align the same reply's audio with
  the viewer capture. Record playback resolution and the measurement host
  with the result.
- The [capture and alignment tool](../scripts/measure_player_audio.py) opens
  an anonymous watch session and collects PCM in a new private directory.
  Run `dbus-run-session -- python3 -B scripts/measure_player_audio.py capture CHANNEL CAPTURE_DIR`
  with Chromium, PulseAudio and the studio extra installed. Wait for the
  playing checkpoint before posting. Use the beginning of the same turn's
  48 kHz mono operator monitor clip as the reference, and then run
  `python3 -B scripts/measure_player_audio.py align CAPTURE_DIR REFERENCE_WAV --event-time EVENT_ISO8601`.
  Use the exact EventSub time with an offset. A weak or ambiguous audio
  match fails. The capture directory is operator data and belongs outside
  the repository.
- Keep the runtime revision with each latency measurement. The sender and
  reply milestones above come from the historical development runtime. The
  subsequent public port includes progressive studio PCM and onset timing,
  verified with local fixtures. The 4.44 s figure remains one historical
  live observation. [Studio operations](studio-operations.md)

[Public payload and validation](../publication/README.md)
