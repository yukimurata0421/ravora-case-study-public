# RAVORA case study

RAVORA explores a practical question: how can a spoken conversation stay responsive
while reacting before a speaker finishes and deciding which past statements are
allowed to influence the next reply?

This snapshot contains the streaming controller, a source-backed SQLite memory
policy, viewer contact state, two-stage transcript reactions, configurable reply
instructions, and synthetic replays. It includes model-independent mouth and
capture timing code. The avatar source assets, private recordings, conversation
database, and deployment environment are excluded.

## Watch RAVORA in conversation

A synthetic speaker tells RAVORA about the first ripe tomato on their balcony. The opening
line is fixed. For the next three turns, the partner waits for RAVORA to finish
speaking and uses the exchange so far to decide what to say next. Both voices,
the speech-recognition captions, and the pauses are captured in one take.

https://github.com/user-attachments/assets/30952fab-4a84-477b-b390-63726973f02d

[Download the 49-second demo (MP4, 5.5 MiB)](https://github.com/yukimurata0421/ravora-case-study-public/raw/refs/heads/main/docs/media/balcony-tomato-conversation.mp4)

RAVORA starts her first reply at **0:08.4**. Her mouth follows the audio
immediately, then picks up the pronunciation of *just* at **0:10**. Across the
four exchanges, the average pause before her reply fell from 4.48 s to 2.20 s.
See the [per-turn measurements and mouth timing](docs/audio-timing.md#reply-timing-in-the-balcony-conversation).

## Start here

```bash
python3 scripts/demo.py replay
python3 scripts/demo.py early-reaction
python3 scripts/demo.py relationships
python3 scripts/demo.py media-timing
```

Python 3.11+ with SQLite FTS5 and JSON support is sufficient. The replay uses only
the standard library, makes no cloud requests, and removes its temporary database
and generated audio on exit. All sample conversations are fictional.

The replay checks:

- PCM reaches the output sink before text generation finishes;
- completed, acknowledged replies become eligible for later retrieval;
- developer dialogue, comment exchanges, and prior answers are retrieved in that order;
- a public reply receives only a reviewed developer summary;
- revoking that review removes the summary from subsequent retrieval;
- cancelling a turn invalidates late audio and prevents a playback acknowledgement.

Playback acknowledgements in the command-line replay are simulated. This does not
prove that a person heard audio or that a character animation stayed synchronized.
See the [sample result](examples/replay-report.json).

The second replay sends repeated low-stability transcript hypotheses through the
real controller. It checks that a provisional reaction arrives while the input is
still open, generation receives only the final transcript, and each of the two
reaction stages uses a distinct durable reservation. It makes no API requests and does
not measure real transcription latency. See the
[early-reaction result](examples/early-reaction-report.json).

The `relationships` replay builds fictional new, regular, and returning contacts.
It checks daily caps, receipt and playback idempotency, unplayed replies, and
owner/scope separation. It shows qualitative model background without generating
a reply or claiming conversation quality. See the
[relationship result](examples/relationship-report.json).

The `media-timing` replay uses a fictional phone sequence and PCM sample
positions to produce mouth cues and video timestamps. It includes a missing
frame so that the later frames can be checked against their original audio clock.
See the [timing result](examples/media-timing-report.json).

## Try the console

```bash
python3 -m venv .venv
.venv/bin/python -m pip install '.[console]'
.venv/bin/python -m ravora.case_study serve --seconds 120
```

Open the loopback URL printed by the command. Text works without microphone access.
Microphone transcription is canned and reply audio is a test tone. This console
exercises recording, streaming PCM, provisional and final reaction labels,
local memory, playback acknowledgement, and cancellation. It does not perform real speech
recognition, model inference, or avatar rendering.
The server expires after the requested interval and removes session files at exit.

![Offline console using a fictional conversation](docs/images/console.png)

## Read the case study

1. [Problem, architecture, and decisions](docs/case-study.md)
2. [Implementation history: causes, decisions, and outcomes](docs/implementation-history.md)
3. [Provisional and final reaction stages](docs/early-reactions.md)
4. [Memory ownership and sharing](docs/privacy.md)
5. [Historical measurements and their limits](docs/measurements.md)
6. [Reproduction and validation](docs/reproduce.md)
7. [Viewer relationships and deterministic contact state](docs/relationships.md)
8. [Live relationship, expression, and latency evaluation](docs/live-evaluation-20261117.md)
9. [Speech articulation and audio-clock capture](docs/audio-timing.md)
10. [What was ported](publication/README.md)

## Scope

The portable slice retains the schema, turn fencing, provider boundaries, mock
streaming, up to one reservation per reaction stage, character and reply-length policies,
sharing approvals, and retrieval provenance. Its ten schema migrations retain
earlier reaction attempts and add source-bound contact accounting. The reference API adapters
are available for inspection. The replay commands and console use mock providers
and ignore dotenv files and inherited API credentials.

Device-specific storage checks, production-stream supervision, cloud deployment,
CPU recognizer installation, and avatar rendering remain outside this snapshot.
No production system or remote service is controlled by these entrypoints.

The additional retrieved context is capped at eight items and 3,600 serialized
characters. Short-term conversation history and the system prompt are separate
inputs. Retrieval supplies quoted background, not verified facts or executable
instructions. A previous answer can itself be wrong.

## Validation and publication state

```bash
PYTHONPATH=src python3 -B -m unittest discover -s tests -v
python3 scripts/validate_public.py
```

The dated [local validation record](publication/validation.json) separates checks
run on the archive snapshot from historical runtime measurements. Its publication
fields describe that local preparation stage. Current remote workflow results are
available in the repository's Actions tab.

A project-wide redistribution license has not been selected. See
[NOTICE.md](NOTICE.md) before reuse or public distribution.
