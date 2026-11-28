# What Ray means when she says she is competitive

Ray wants to know why her guess was wrong. A lucky second guess would leave that
question open. This gives a viewer somewhere to go after asking whether Ray is
competitive. A different answer anchors her reason for streaming. A viewer can
suggest a use for an ordinary object that would never have occurred to her.

## Try the interview

```bash
python3 scripts/demo.py character-interview
```

The replay prints ten turns. It switches subjects halfway through and asks for
reasons and examples in different orders. These lines come from the English
replay. Both language versions have their own authored text.

| Viewer | Ray |
| --- | --- |
| Are you competitive? | I am competitive. I hate finishing a round without knowing why I missed. |
| Why? | A lucky second guess would still leave me wondering where my reasoning went wrong. |
| For example? | If I mistook jingling keys for a bell, I would ask to hear that bit again. |
| Why do you stream? | I stream to meet people who see things in ways I would never think of. |
| For example? | I see an umbrella as shelter. Imagine someone calling it an instrument. |
| For example? | If the umbrella is an instrument, I would tap the handle along with the rain. |

The table excerpts two conversations. The JSON includes every intervening turn.
The command uses the studio's text selector and a window of simulated playback
acknowledgements. It runs offline with standard-library Python. [Full replay](../examples/character-interview.json)

## How the next answer is chosen

The character material specifies what Ray thinks, why she thinks it and what
example she can offer. Each of the two subjects has three reasons and three
examples per language. A short follow-up uses the latest recognized answer in
the eligible history. Previously used reasons and examples advance independently.
Changing the subject changes the anchor. An exhausted sequence can return to the
normal reply path. The same selector also handles Ray's self-assessment, an
unexpected interest, and how she chose her name. [Authored material and selector](../src/ravora/ray_self_disclosure.py) · [Bilingual follow-up tests](../tests/test_ray_self_disclosure.py)

In the studio, Twitch admission records the arrival time and commits the input
before waking playout. The bilingual studio enables this dialogue path when
`RAVORA_TWITCH_RECALL_MODE=session`, `RAVORA_MEMORY_ENABLED=true` and
`RAVORA_CHARACTER_ENABLED=true`. Up to four acknowledged
question-and-answer pairs are available for that viewer and program. The window
expires after ten idle minutes and has room for 128 keys. SQLite rechecks the
source, owner, playback state and revision before reuse. Deleted or expired
evidence drops out. For open questions the controller can also use recent topic
evidence to avoid returning to the same subject unprompted. An explicit question
about that subject can still be answered. [Admission](../src/ravora/twitch_intake.py) · [Session recall](../src/ravora/session_window.py) · [Dialogue policy](../src/ravora/viewer_dialogue.py)

## Measuring comment to speech

On November 28, 2026 (JST), an isolated studio replayed twelve earlier broadcast
questions in their original order and at their original intervals. A second
sequence asked seven questions about each of the two new subjects. Both ran with
Japanese GPU speech on a GTX 1070. Seven answers in the first sequence used
authored text and five used generated text. All fourteen in the second sequence
used authored text.

| Sequence | First sound within 1.8 s | Replies played in full | Median onset | Longest onset |
| --- | ---: | ---: | ---: | ---: |
| Twelve earlier questions | 12/12 | 12/12 | 0.690 s | 1.684 s |
| Two character interviews | 14/14 | 14/14 | 0.687 s | 0.795 s |

The clock started when the application received the chat event. It stopped at
the first audible sample in the frame written to program audio (48 kHz, 16-bit
PCM, absolute amplitude threshold 33). All 29 synthesized chunks matched the
recorded program waveform in order, including their tails, after resampling and
the studio's reverb. This measures a single pass over each sequence. Network
delivery to the viewer and playback on their device were outside the trial.

The public replay uses fictional questions and contains no broadcast recordings.
It reproduces text selection. The GPU measurements above come from the earlier
isolated studio run. [Public package validation](../publication/history/2026-11-28-character-interview-validation.json) · [Other speech measurements](twitch-local-tts.md)
