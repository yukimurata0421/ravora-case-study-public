# Public case-study boundary

This directory records an allowlisted source port, not a wholesale copy of the
development workspace. [source-map.json](source-map.json) lists exact, adapted, and
extracted source files with hashes. Generated case-study files are also listed.

## Included

- source-backed SQLite memory, session usage and chat admission across thirty-two schema migrations;
- three-tier retrieval, explicit summary sharing, and revocation;
- deterministic viewer contact state, daily caps, and qualitative request background;
- streaming orchestration, input ledgers, and session-bound playback fences;
- provisional and final transcript reactions with a durable reservation for each stage;
- phone timing, mouth geometry, and PCM-based video timestamps without avatar assets;
- a registered WebGL face renderer and a masked gaze-frame builder for user-supplied artwork;
- authored bilingual character interviews, topic selection and viewer-scoped recall of acknowledged replies;
- reference provider interfaces, offline replays and an optional live studio;
- a packaged browser console, synthetic fixtures, and focused tests;
- automatic local evaluation capture, condition-specific latency percentiles,
  human review labels, transcription edit counts, and an installed review command;
- aggregate historical observations with explicit measurement limits;
- sanitized operations records for the YouTube and Twitch studio runs;
- a count-only public observation schema, static page and fictional broadcast fixture;
- bilingual GPU speech workers, language routing, program-onset measurements and a k3s public-player observer;
- portable, opt-in per-slot, market and publication timers, user-service and launcher examples.

The working tree keeps three media files: the
[README conversation](../README.md#see-it-in-30-seconds), a same-audio
[jaw comparison](../docs/audio-timing.md#current-mouth-and-jaw), and the six-voice
[audition](../docs/media/voice-audition.m4a), which is the only audio-only file.
Earlier recordings are linked from the [recording history](../docs/recording-history.md)
at commit `9af86c6` instead of being kept in the clone.

## Excluded

- dotenv secrets, existing databases, raw evidence, caches, and virtual environments;
- avatar source assets, model weights, raw capture files, private recordings, and renderer vendor bundles;
- microphone recordings, personal transcripts and local evaluation directories;
- host storage identifiers, private project defaults, installed production
  units, operator credentials, and production-only controls.

## Changes since the CPU speech snapshot

At `8e39571`, 63 public paths referred to source files whose current development
hash had changed. This port updates 50 of those public paths and adds the modules,
migrations, tests and examples needed to run them. The other 13 keep their
previous public versions. They cover the approved comparison movie, the portable
slot service and speech policy, the extracted output validator, public renderer
modules and styles, and public test helpers and suites. Their source hashes
continue to identify the revisions actually used in the public tree.

| Area | Public change | Where to check |
| --- | --- | --- |
| Character and memory | First-person answers with reasons and examples, four acknowledged exchanges per viewer, and source revalidation | [Interview walkthrough](../docs/character-interview.md) |
| Intake and speech | Commit before playout, deduplicate two chat routes, reuse local clarification audio, and split an English synthesis request once on HTTP 503 | [Chat setup](../docs/twitch-live.md) and [speech behavior](../docs/twitch-local-tts.md) |
| Studio observations | Daily slot calendar, measured speech and budget counts, and a versioned public export | [Observation guide](../docs/studio-observations.md) |

## Check the payload

```bash
python3 scripts/validate_public.py
```

The script validates local files, hashes, private-path patterns and local
Markdown links. It does not contact the development workspace.

## Validation records

Each record describes the local checks made before that snapshot was delivered.
It does not assert the outcome of a later push or remote workflow; the
repository's Actions tab shows those. Historical runtime test counts and API
measurements are kept separate from portable-package validation. Older records
are retained unchanged.

| Validated (JST) | Snapshot | Record |
| --- | --- | --- |
| 2026-11-28 | Keep the selected reply and latest chat message visible under the scheduled topic. [Chat layout](../docs/twitch-live.md#what-viewers-see-in-chat) | [Current](validation.json) |
| 2026-11-28 | Authored character interviews, playback-bound viewer recall, durable chat intake, bounded speech retries and the isolated program-audio measurements. [Interview](../docs/character-interview.md) | [Record](history/2026-11-28-character-interview-validation.json) |
| 2026-11-27 | CPU synthesis for five English and Japanese passages, with matched GPU timings. [Measurements](../docs/twitch-local-tts.md#cpu-synthesis-on-the-broadcast-host) | [Record](history/2026-11-27-cpu-speech-validation.json) |
| 2026-11-27 | Bilingual GPU voice routing, response times and a portable k3s observer. [Guide](../docs/twitch-local-tts.md) | [Record](history/2026-11-27-bilingual-gpu-validation.json) |
| 2026-11-25 | Short spontaneous turns, priority for incoming speech, independent audience sampling and broadcast reply coverage. [Broadcast observations](../docs/studio-observations.md) | [Record](history/2026-11-25-scheduled-speech-validation.json) |
| 2026-11-24 | Per-speaker admission, deadline-aware rotation and shared music questions. [Selection policy](../docs/studio-observations.md#selecting-speakers-when-chat-is-busy) | [Record](history/2026-11-24-chat-selection-validation.json) |
| 2026-11-24 | Broadcast reporting, prepared replies, scheduled shows and a validated static observation page. [Observations](../docs/studio-observations.md) | [Record](history/2026-11-24-broadcast-observations-validation.json) |
| 2026-11-24 | Progressive studio PCM, program audio and subtitle controls, transport and API graphs, conversational initiative and recall, YouTube same-event recovery and chat admission. [Studio operations](../docs/studio-operations.md) | [Record](history/2026-11-24-studio-operations-validation.json) |
| 2026-11-24 | Twitch H.264 output, bounded RTMPS reconnect, EventSub chat and bilingual program replies. [Operations record](../docs/twitch-live-operations-20261124.md) | [Record](history/2026-11-24-twitch-studio-validation.json) |
| 2026-11-23 | Media consolidation: one conversation, one same-audio comparison and the audition. No source code or new recordings. | [Record](history/2026-11-23-media-consolidation-validation.json) |
| 2026-11-23 | Japanese topic-tag retrieval, opt-out checks and three initial character interests. [Settings](../docs/conversation-controls.md#rays-interests-and-viewer-memories) | [Record](history/2026-11-23-bilingual-memory-validation.json) |
| 2026-11-22 | Portrait mesh, texture registration, jaw articulation and selectable NVIDIA or CPU HEVC encoding. [Encoder](../docs/youtube-live.md#select-the-hevc-encoder) | [Record](history/2026-11-22-avatar-hevc-validation.json) |
| 2026-11-22 | Topic saturation, per-turn retrieval control and source-linked topic suggestions. [Routing](../docs/conversation-controls.md#topic-changes-and-recall) | [Record](history/2026-11-22-dialogue-routing-validation.json) |
| 2026-11-22 | Live studio, channel authorization, chat streaming, microphone mixing, local STT with a model digest manifest. [Studio guide](../docs/youtube-live.md) | [Record](history/2026-11-22-live-studio-validation.json) |
| 2026-11-21 | Listener-centered questions, persona cues at playback onset and independent microphone mute. [Controls](../docs/conversation-controls.md#listening-speaking-and-muting) | [Record](history/2026-11-21-listener-and-mute-validation.json) |
| 2026-11-21 | Evaluation recorder, authenticated browser review and installed command. [Voice evaluation](../docs/voice-evaluation.md) | [Record](history/2026-11-21-voice-evaluation-validation.json) |
| 2026-11-21 | Registered upper-lip renderer, manifest validation and the dinner recording with its frame-spacing result. [Conversational smile](../docs/audio-timing.md#conversational-smile) | [Record](history/2026-11-21-upper-lip-validation.json) |
| 2026-11-20 | Speaker audition, hands-free input, caller-supplied avatar binding, source-backed episodes, developer ownership and transactional reply reservations. | [Record](history/2026-11-20-conversation-and-voice-validation.json) |
| 2026-11-20 | Prepared-audio adapter and the live renderer's default articulation. [Measurements](../docs/recording-history.md#concept-02-default-motion) | [Record](history/2026-11-20-default-motion-validation.json) |
| 2026-11-19 | The earlier dinner recording and its mouth motion. | [Record](history/2026-11-19-thinking-conversation-validation.json) |
| 2026-11-18 | Speaker-grounded expression classification and the rule for keeping a provisional expression. [Expression selection](../docs/early-reactions.md#expression-selection) | [Record](history/2026-11-18-expression-policy-validation.json) |
| 2026-11-18 | Two-stage reactions and media timing; the initial public snapshot. | [Record](history/2026-11-18-reaction-and-media-timing-validation.json) |
| 2026-11-17 | Viewer relationship port. | [Record](history/2026-11-17-relationship-port-validation.json) |
| 2026-11-17 | Provisional reaction port. | [Record](history/2026-11-17-early-reaction-validation.json) |
| 2026-11-16 | Original archive stage. | [Record](history/2026-11-16-validation.json) |

The recorded measurements describe finite development runs. The package provides
the replays and reference modules for separate reproduction.
