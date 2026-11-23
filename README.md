# RAVORA case study

RAVORA explores a practical question: how can a spoken conversation stay responsive
while reacting before a speaker finishes and deciding which past statements are
allowed to influence the next reply?

RAVORA is the conversation system. Ray is the character it voices and animates.

If a reply is cancelled while its audio is still arriving, the controller rejects
late audio from that turn. A completed reply becomes eligible for recall from
SQLite only after playback is acknowledged. You can exercise both paths offline.
[Reliability checks and commands](#what-this-demonstrates-and-how-to-check-it)

## See it in 30 seconds

[![Ray smiling as she answers the second question](docs/images/ray-dinner-conversation.png)](https://github.com/yukimurata0421/ravora-case-study-public/raw/refs/heads/main/docs/media/ray-upper-lip-conversation.mp4)

[Watch the 30-second dinner conversation (1080p MP4, 5.3 MiB)](https://github.com/yukimurata0421/ravora-case-study-public/raw/refs/heads/main/docs/media/ray-upper-lip-conversation.mp4)

A synthetic speaker asks Ray three scripted questions about dinner in one uncut
take. The panel on the right shows what the system decided and when.

- **Reacting while listening.** In the second turn, the panel shows a provisional
  *neutral* reaction 1.36 s into the speaker's voice. A final *smile* follows
  0.83 s after the speaker stops, once the full transcript is available.
- **Reply start.** The panel shows the time from the end of the speaker's voice to Ray's
  first sound. It reads 1.80 s for the second turn.
- **Visible reaction.** Around **0:17–0:19**, Ray smiles as she answers
  “That freezer find is pure magic.”

At the first reply onset, the browser recorded an **83.3 ms** gap between draws,
so that reply failed its frame-spacing check. [Later jaw revision and same-audio comparison](docs/audio-timing.md#current-mouth-and-jaw) ·
[Earlier recordings and their measurements](docs/recording-history.md)

## What this demonstrates, and how to check it

| Claim | Evidence | Check it yourself |
| --- | --- | --- |
| Cancelling a turn invalidates its generation. Late audio is rejected and the turn stays unplayed. | The [replay result](examples/replay-report.json) records the cancellation check. | `python3 scripts/demo.py replay` |
| A completed reply must receive a playback acknowledgement before it can be recalled. | [Retrieval tests](tests/test_conversation_context.py) exercise completed, unplayed, cancelled and deleted turns. | `PYTHONPATH=src python3 -m unittest discover -s tests -p test_conversation_context.py -k failed_unplayed` |
| Speech output starts before text generation finishes. | Offline replay with mock providers | `python3 scripts/demo.py replay` |
| A provisional reaction can arrive before the input ends; generation uses only the final transcript. | Replay with low-stability transcript hypotheses | `python3 scripts/demo.py early-reaction` |
| Private developer dialogue reaches a public reply only as a reviewed summary, and revocation removes it. | Replay and unit tests | `python3 scripts/demo.py replay` |
| Viewer contact state respects daily caps, playback acknowledgements and owner/scope separation. | Replay with fictional contacts | `python3 scripts/demo.py relationships` |
| Mouth shapes and video timestamps follow the audio sample clock. | Synthetic phone sequence and browser module tests | `python3 scripts/demo.py media-timing` |
| Live reply timing and reaction behavior | Finite recorded runs only | [Measurements and their limits](docs/measurements.md) |

The video shows how the result looks and sounds. The commands check the
mechanisms. Live timing figures describe specific finite runs, not general
latency guarantees.

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

## How it works

Input chunks pass through an input ledger and streaming recognition. The turn
controller streams text into speech synthesis and bounded PCM playout. Only a
playback acknowledgement makes a reply eligible for memory. An ownership and
sharing policy then decides which stored statements can return as context.
[Architecture and decisions](docs/case-study.md)

## Run it yourself

### Offline console

```bash
python3 -m venv .venv
.venv/bin/python -m pip install '.[console]'
.venv/bin/python -m ravora.case_study serve --seconds 120
```

Open the loopback URL printed by the command. Text works without microphone access.
The offline console uses a canned transcript, scripted replies, and a test tone.
Watch the synthetic face as Ray listens and then starts speaking. A specific
persona expression takes over at playback onset; a neutral cue keeps the
current listening reaction. **Mute microphone** stops capture while text input
and reply playback remain available. [Conversation controls](docs/conversation-controls.md)

The server expires after the requested interval and removes its temporary session
database and reply audio. Evaluation starts automatically and retains input audio,
transcripts and timing reports in a new local directory under `.runtime/`.
Use `--evaluation-dir PATH` to choose a new directory or `--no-evaluation` to
disable retention. See [automatic voice evaluation](docs/voice-evaluation.md).

![Offline console using a fictional conversation](docs/images/console.png)

### Live studio

Record Ray and a host microphone with synchronized subtitles, or create an unlisted
YouTube broadcast with channel OAuth. The studio supports Japanese and English
conversation tabs, hands-free input, microphone mute and automatic gain matching.
Chat arrives through gRPC `streamList`. The studio can record or publish 1080p30
HEVC using NVIDIA NVENC. Start with the local HEVC/AAC preview in the
[live studio guide](docs/youtube-live.md).

Ray starts with an interest in stargazing, puzzles and speech technology. The cloud
studio can remember source-backed details from Japanese and English conversations
when the operator enables memory extraction. Ray can follow up on a co-host's news
and shift a repeated topic through a related detail.
[Character interests and viewer memory](docs/conversation-controls.md#rays-interests-and-viewer-memories) ·
[Topic changes and recall](docs/conversation-controls.md#topic-changes-and-recall) ·
[Voice selection and the six-voice audition](docs/conversation-controls.md)

## Read further

**Overview**

- [Problem, architecture, and decisions](docs/case-study.md)
- [Implementation history: causes, decisions, and outcomes](docs/implementation-history.md)

**Mechanisms**

- [Provisional and final reaction stages](docs/early-reactions.md)
- [Memory ownership and sharing](docs/privacy.md)
- [Viewer relationships and deterministic contact state](docs/relationships.md)
- [Voice, memory, topic and budget controls](docs/conversation-controls.md)
- [Speech articulation and audio-clock capture](docs/audio-timing.md)

**Evaluation**

- [Historical measurements and their limits](docs/measurements.md)
- [Live relationship, expression, and latency evaluation](docs/live-evaluation-20261117.md)
- [Automatic voice timing and human-reviewed transcription errors](docs/voice-evaluation.md)
- [Earlier recordings and their measurements](docs/recording-history.md)

**Operation**

- [Reproduction and validation](docs/reproduce.md)
- [Live studio and YouTube broadcast](docs/youtube-live.md)
- [What was ported](publication/README.md)

## Scope

The avatar source assets, private recordings, conversation database and deployment
environment are excluded from this snapshot.

The portable slice retains the schema, turn fencing, provider boundaries, mock
streaming, up to one reservation per reaction stage, character and reply-length policies,
sharing approvals, and retrieval provenance. Its sixteen schema migrations retain
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

The [reproduction guide](docs/reproduce.md#test-results) lists the Python suite
and its skips for each tested environment, alongside the Node.js module tests.
Each push runs the replays, installed-package tests and publication checks.
[CI runs on main](https://github.com/yukimurata0421/ravora-case-study-public/actions/workflows/ci.yml?query=branch%3Amain)

A project-wide redistribution license has not been selected. See
[NOTICE.md](NOTICE.md) before reuse or public distribution.
