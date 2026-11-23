# YouTube same-event recovery and chat intake

**Window** November 23, 21:10 to November 24, 00:55 JST. The historical Ray
studio ran on a Linux host with NVIDIA HEVC encoding and a locally modified
development checkout. It reattached to the same public YouTube video
during the maintenance work. The sampled chat path delivered six recorded
comments between 00:25 and 00:47. Three played and three arrived outside
the studio's 45-second freshness window.

The key diagnostic was the difference between a completed gRPC connection
and time spent waiting for the next connection. At 00:55 the sender
continued to mux video and audio, and the bound YouTube input reported
active and healthy. There is no player capture for that observation window.

## Integration and operating decisions

| Concern | Decision and evidence |
| --- | --- |
| Layout and voice | Render 1080p30 with GPU HEVC and stream AAC. Put decoded host audio and Ray speech on one program timeline. Apply host auto gain and mild noise cleanup on the program mix. Recognition receives the original samples. |
| Caption and reply gaps | Size and paginate captions against measured pixel widths. The operator monitor follows numbered audio clips and can recover a missed poll. The operator previously traced duplicated monitor audio to opening two console tabs. |
| Resuming a broadcast | Verify the authorized channel, the saved event and bound stream, live state, privacy and automatic-stop setting. Use the pinned video ID for maintenance. New event metadata and visibility checks apply to the event owned by the studio. |
| Chat retention | Commit delivered comments and the resume cursor together. Deduplicate by source identity, expire stale comments, and keep the host route available during chat backoff. |
| Speech timing | The studio can air validated TTS PCM before WAV completion. Record the first audible frame, provider milestones and acknowledgement separately. A partial or cancelled reply cannot create a successful playback record. |

[Studio operations](studio-operations.md) · [YouTube recovery procedure](youtube-live.md#recover-the-same-live-event)

## Timeline and observed effect

| Time (JST) | Action and result |
| --- | --- |
| Nov 23 21:10:55 | The sender opened `hevc_nvenc` and YouTube showed the broadcast live. A 21:34:49 check observed the same video after reconnect. |
| 22:25:44 → 22:25:57 | Restarted the sender on the same event to enable an English initiative after 60 quiet seconds. At 22:26:57 the first initiative started, and at 22:27:16 its 17-second audio was acknowledged. |
| 23:04:19 → 23:04:37 | Restored the same live event after changing theme selection. The first three new initiatives, at 23:05:37, 23:06:50 and 23:08:03, used distinct themes and all played. |
| Nov 24 00:25–00:47 | The receiver observed both fresh and old chat events after gaps in its connection. Three of six recorded comments were too old to select. |
| 00:46:58 → 00:47:12 | Restarted the sender on the same event with connection-duration and backoff diagnostics. From 00:47:23 the receiver logged a stable exit reason for each connection. |
| 00:55:09–00:55:19 | Sender frames increased by 300 and muxed data by 7,631,367 bytes in 10.003 seconds. This is 29.99 fps and 6.10 Mbit/s over the window. At 00:55:13 the bound input reported active and healthy. Both sender snapshots showed zero I/O errors. |

These times delimit API and sender observations. The length of any viewer-side
interruption is unknown. The operator reported that video and audio had
become smooth after an earlier insufficient-video warning. That report is
a separate playback observation, and the cause and duration of that warning
were unresolved.

## Chat arrival and playback results

The table uses the provider event timestamp and the inbox receipt. A played
row required a completed turn and at least one playback acknowledgement for
that same turn.

| Posted (JST) | Received | Delivery gap | Result |
| --- | --- | ---: | --- |
| 00:25:58 | 00:34:04 | 485.9 s | Expired |
| 00:33:58 | 00:34:04 | 6.0 s | Played by 00:34:16 |
| 00:34:19 | 00:35:15 | 55.2 s | Expired |
| 00:34:30 | 00:35:15 | 44.5 s | Played by 00:35:29 |
| 00:35:25 | 00:36:25 | 60.0 s | Expired |
| 00:36:11 | 00:36:25 | 14.1 s | Played by 00:36:37 |

The 45-second freshness check applies to admission for a reply. A reply
already selected within that window can finish afterwards. These counts
cover saved inbox receipts. The data cannot tell how many other posts
were attempted during a connection gap.

## Why the chat budget changed

The earlier deployment used an operator-selected limit of 40 request
starts per rolling hour. A connection reserves its slot before dispatch.
The receiver continues when an older reservation ages out. During
00:47:12–00:52:59, eight gRPC connections each returned one batch and closed
with normal EOF. Their durations were 10.504–10.815 s, averaging 10.651 s.
The ordinary retry wait was five seconds, and five additional waits of
about 47–55 seconds came from the local budget. No provider-limit error
was observed in those eight connections. The reason for the short normal
closures remains unknown.

Forty starts per hour spread evenly imply a 90-second start interval.
With 10.65-second connections, that could leave about 79 seconds uncovered.
Maintaining the observed connection length plus a five-second wait would
instead need about 230 starts per hour. This comparison motivated an
explicit rolling limit and daily ledger. The revised studio defaults to
240 starts per hour, at least 15 seconds between starts at that rate, and
4,800 local chat units per Pacific day. A four-hour window at the maximum
uses up to 4,800 chat units for 960 `streamList` attempts, before any
video lookup. Busy and quiet EOF histories select different wait intervals.
This is an operator budget. The provider publishes five units per
`streamList` request and one per `videos.list` lookup, while a single open
stream can carry many comments.
[gRPC chat](https://developers.google.com/youtube/data/live/streaming-live-chat) ·
[Method quota table](https://developers.google.com/youtube/data/determine_quota_cost)

No new comment arrived during the eight-connection post-change sample.
A simulated four-hour run, transaction tests and local TLS gRPC tests
exercise the later budget logic. They leave the provider's sustained
connection behavior, live reply rate under the newer budget, eight-hour
delivery and viewer-side buffering unmeasured.

## Initiative and playback gates

The first configuration produced 25 acknowledged initiatives in the
22:25:44–22:57:00 window. All 25 full replies differed, yet 20 began with
the same phrase, and the three themes cycled in fixed order. The later
implementation chooses a variation from eight themes and 32 prompts,
avoids the recently played themes and angles, and makes up to two recent
public openings available to the generator. The first three observed
initiatives after the change used distinct themes and opening phrases.
Source removal, quiet-period reset and preemption by a human input have
separate fixture tests.
[Conversation controls](conversation-controls.md#from-a-pause-to-the-next-turn)

## How to check a later run

1. Confirm the same authorized event, visibility, stream binding and the
   loaded sender's encoder. Sample frame and mux-byte differences over an
   explicit interval. Separate reconnect, healthy ingest and actual watch
   playback.
2. Compare connection time, batch count, EOF or provider error, and the
   reason and length of each wait. Count received, expired and played
   comments from their inbox rows and acknowledge audio on the same turn.
3. Check the watch player independently when the task requires an audible
   answer. The [Twitch measurement](twitch-live-operations-20261124.md) shows
   the method used for one public playback sample.

[Connection and ledger tests](../tests/test_comments.py) ·
[Local TLS and owned-event tests](../tests/test_youtube_stream.py) ·
[Public validation](../publication/validation.json)
