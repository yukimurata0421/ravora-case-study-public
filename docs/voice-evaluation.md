# Automatic voice evaluation

The console starts recording a local evaluation when it opens. It saves the
accepted microphone PCM, transcript, reply, endpoint reason and browser timing.
The review panel shows progress, latency p50/p95 and manual review controls.

The public console remains offline: recognition is canned and replies are tones.
Its measurements test capture and timing plumbing, not speech-recognition quality
or remote-provider response time. Real microphone accuracy needs an independently
configured recognizer, reference transcripts and human endpoint review.

## Start and review

```bash
python3 -m pip install '.[console]'
ravora-case-study serve --evaluation-dir ./.runtime/voice-run
```

Choose a new directory. The command refuses to reuse an existing evaluation
directory. Without that option, each launch creates a new directory under
`.runtime/`. The printed `Evaluation retained:` path points to `review.html`.
Use `--no-evaluation` to keep the earlier discard-input-audio behavior.

1. Speak for 30 to 50 acknowledged turns.
2. Open **Conversation evaluation**, then **Review input audio and transcripts**.
3. Refresh records, listen to each input and enter a reference transcript.
4. Mark transcription and endpoint errors as correct, erroneous or unreviewed.
5. Save the review. Error rates use reviewed inputs only; missing references
   and unreviewed labels are not silently counted as correct.

After shutdown, open `review.html` locally or update the review through the
installed command:

```bash
ravora-voice-evaluation annotate ./.runtime/voice-run --id INPUT_ID \
  --reference 'Reviewed transcript' --stt-error yes --false-endpoint no
ravora-voice-evaluation report ./.runtime/voice-run
```

The checkout wrapper is `python3 scripts/evaluate_voice.py` with the same
arguments. Command-line annotation refuses an active run; use the browser panel
while the server is running.

## Retained data and collection limits

| File | Content |
| --- | --- |
| `turns.json` | Input and session IDs, JST timestamps, source, end mode, language, models, status, transcript, reply, review labels and timings |
| `INPUT_ID.wav` | The accepted 16 kHz mono PCM16 input, with WAV and PCM SHA-256 values |
| `summary.json` | Counts, missing samples, failures, percentiles, reviewed error rates, CER/WER and implementation hashes |
| `review.html` | An escaped local report with audio playback links |

The default target is 30 acknowledged turns. Collection stops at 50 acknowledged
turns, 100 inputs or 128 MiB of input audio; conversation may continue. These
limits do not reset or refund any existing provider budget.
Failed, empty, cancelled and interrupted inputs retain acquired audio. Waiting
for a hands-free onset does not write background audio.
Storage errors are visible and do not cancel a conversation or retry a request.

An optional [configuration file](../examples/voice-evaluation.toml) sets
`developer_voice.evaluation`. Pass it with `serve --config FILE`.
The recording directory is group-private and evaluation files are created with
mode 0640. Audio downloads require the console token and are never exposed through
an unauthenticated static file route. There is no automatic age-based deletion.
These independent evaluation copies are not part of memory sharing or expiry.
Keep them private and remove them when no longer needed.

## Latency and error definitions

| Metric | Definition |
| --- | --- |
| `input_end_to_audio_ms` | Manual recording flush/send, or receipt of automatic capture-end notification, to the first non-silent AudioWorklet notification, using the browser's monotonic clock |
| `stt_final_to_audio_ms` | Last final-segment notification to the same audio notification; missing when the recognizer does not emit a final segment |
| `last_voice_to_audio_ms` | Estimated last voiced input PCM at the server to the browser audio notification, with browser/server clock conversion |
| `sync_uncertainty_ms` | Half the minimum round-trip time of five clock probes near the input |

The estimated speech end uses 20 ms PCM RMS windows at -42 dBFS. Server PCM
receipt minus trailing samples anchors that estimate. Capture, input chunking
and browser-to-server upload delay remain sources of error beyond clock-probe
uncertainty. Unsynchronized clocks produce a missing estimate. This is not a
physical acoustic end-of-speech measurement.

Reply onset is the notification of the first 10 ms AudioWorklet window at
-48 dBFS. It is not the sound reaching a listener's ear.
Backend first-text, first-PCM and total-generation timings are separate fields.
Latency p50/p95 use nearest rank over acknowledged inputs only. Missing values
are counted and excluded rather than replaced with zero. Source, end mode,
language, model and mock/fixture provenance remain separate groups.
Small-sample p95 may simply be that group's maximum.

Transcription references use NFKC normalization, case folding and punctuation
removal. CER compares non-whitespace characters; WER compares whitespace-delimited
words. CER is the more useful measure for unsegmented text. A reviewed reference
with no transcript counts the missing text as deletions.
Human endpoint labels remain necessary because captured input alone cannot
establish the speaker's intended continuation after a premature endpoint.

The test suite covers byte-identical PCM, missing samples, failed/cancelled inputs,
collection limits, retained reports, review commands, token checks and graceful
shutdown. Synthetic validation does not establish 30 to 50 turns of real
microphone quality. No private runtime data or recordings accompany this port.
