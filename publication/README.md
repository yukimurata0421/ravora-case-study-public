# Public case-study boundary

This directory records an allowlisted source port, not a wholesale copy of the
development workspace. [source-map.json](source-map.json) lists exact, adapted, and
extracted source files with hashes. Generated case-study files are also listed.

Included:

- source-backed SQLite memory and its thirteen schema migrations;
- three-tier retrieval, explicit summary sharing, and revocation;
- deterministic viewer contact state, daily caps, and qualitative request background;
- streaming orchestration, input ledgers, and session-bound playback fences;
- provisional and final transcript reactions with a durable reservation for each stage;
- phone timing, mouth geometry, and PCM-based video timestamps without avatar assets;
- a registered WebGL face renderer and a masked gaze-frame builder for user-supplied artwork;
- common character and concise-reply instruction policies;
- reference provider interfaces and a mock-only portable entrypoint;
- a packaged browser console, synthetic fixtures, and focused tests;
- automatic local evaluation capture, condition-specific latency percentiles,
  human review labels, transcription edit counts, and an installed review command;
- aggregate historical observations with explicit measurement limits;
- rendered [public conversations](../README.md#watch-ravora-in-conversation) with both voices, plus a same-audio comparison of jaw motion;
- the prepared-audio adapter and the live renderer's default articulation, with [measurements](../docs/audio-timing.md#concept-02-default-motion).

The reaction replay and tests cover provisional and final results, prefix
agreement, correction, late completion, cancellation, and preservation of prior
reservations during migration. The implementation history documents causes,
decisions, outcomes, and the separately maintained v13 rendering change.

Excluded:

- dotenv secrets, existing databases, raw evidence, caches, and virtual environments;
- avatar source assets, model weights, raw capture files, private recordings, and renderer vendor bundles;
- host storage identifiers, private project defaults, deployment tooling, and
  production-stream observation/control paths.

The dated [local validation record](validation.json) describes the archive-stage
checks. The repository's Actions tab shows the status of remote workflow runs.
The recorded measurements describe a finite development run. The package provides
the replay and reference modules for separate reproduction.

The [previous validation record](history/2026-11-16-validation.json) is retained
unchanged. The current local record describes checks before delivery and does not
assert the outcome of a later push or remote workflow. Historical runtime test
counts and API measurements are not substituted for portable-package validation.

The [earlier reaction-port validation](history/2026-11-17-early-reaction-validation.json)
and [relationship-port validation](history/2026-11-17-relationship-port-validation.json)
record the earlier snapshot boundaries. The
[two-stage and media timing validation](history/2026-11-18-reaction-and-media-timing-validation.json)
records the initial public snapshot. The
[reaction policy update](../docs/early-reactions.md#expression-selection) adds
speaker-grounded classification and an explicit rule for retaining a provisional
expression. The original artwork and recordings are excluded from the package.

The [November 18 expression-policy validation](history/2026-11-18-expression-policy-validation.json) records the checks before the dinner recording was added.

The [previous dinner recording validation](history/2026-11-19-thinking-conversation-validation.json) covers the earlier mouth motion.

Run `python3 scripts/validate_public.py` to check the declared public payload.
It validates local files and hashes; it does not contact the development workspace.

The November 20 port includes the speaker audition, rendered lip comparison,
hands-free input, caller-supplied avatar binding, source-backed episodes,
developer ownership, and transactional reply reservations. The six-voice audio
file contains one scripted audition line and is the only audio-only media
exception. The newly rendered conversation uses the same scripted input as
the dinner demo. Original portraits, microphone recordings and database
contents remain private. [Previous motion snapshot](history/2026-11-20-default-motion-validation.json)

The November 21 snapshot includes the registered upper-lip renderer, manifest
validation, and two finished clips of the scripted dinner conversation.
The current reference and the frame-spacing result are documented together.
[Conversational smile](../docs/audio-timing.md#november-21-conversational-smile) · [Previous voice and conversation validation](history/2026-11-20-conversation-and-voice-validation.json)

The voice-evaluation port adds the recorder, authenticated browser review,
startup defaults and installed command described in [voice evaluation](../docs/voice-evaluation.md).
Only code, synthetic tests and documentation are included. No microphone recordings,
personal transcripts or local evaluation directories were copied.
The [previous upper-lip validation](history/2026-11-21-upper-lip-validation.json)
retains the earlier media results and their frame-spacing limitation.
