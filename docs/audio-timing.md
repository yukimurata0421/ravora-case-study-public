# Speech articulation and audio-clock capture

The voice path can show an initial expression while the speaker talks and then
confirm or revise it using the full transcript. The expression follows the
[two-stage contract](early-reactions.md). The mouth follows the synthesized audio
and keeps lip closures during a reply.

## Mouth and expression

- The optional local aligner estimates phoneme times from the synthesized PCM
  and the response text. It can also interpolate phone timings between matched
  word boundaries. If the word sequence cannot be matched, the live recorder
  uses waveform-based shapes.
- The mouth plan maps English phones to five vowel shapes, consonant
  constrictions, and lip closures. It samples the intensity from the same PCM.
  Neighboring shapes share a weighted target mesh so that they can blend
  without two separate lip contours.
- The playout worklet reports the sample index at which it consumes the first
  PCM. The speaking mouth keeps control through pauses. Listening-mouth art
  returns after playback.

The reference code lives in the Python `ravora.phone_alignment` and
`ravora.speech` modules and the packaged browser mouth-plan, articulation, and
expression modules. The [media timing replay](../examples/media-timing-report.json)
uses a synthetic phone sequence. The aligner is optional and runs locally.

## One clock for both tracks

The PCM tap marks the first recorded audio sample. The browser assigns a video
tick from that audio clock before submitting the frame to the encoder. The
timestamp filter uses the actual ticks, so a skipped frame leaves a gap in its
original position. A fixed-rate export can hold the preceding frame in a gap.
There is no need to shift later audio or remap video wall time.

`python3 scripts/demo.py media-timing` reproduces the timestamp mapping and a
short mouth plan. `node --test tests/check_media.mjs` exercises the browser
modules with a stub encoder and synthetic mesh vertices. These checks cover the
timing contract and pose calculation. The packaged console still demonstrates
test tones and reaction labels. The complete recording environment, original
character and source recordings are kept outside this snapshot.

## Historical recording, 2026-11-18 JST

The finite live run used five synthetic English questions and actual API replies.
It used one provisional and one final classification per input. Among the observed
transitions were a smile that persisted, a smile that changed to surprise, and a
smile that changed to concern.

| Check | Observation |
| --- | --- |
| Original capture | 29.718 fps. The 95th-percentile gap was 33.3 ms and the largest gap was 100 ms. The earlier capture had a largest gap of 167.8 ms. |
| Export | 1920 × 1080, fixed 30 fps. Nineteen missing source ticks used a held frame. |
| Encoded audio and picture | The audible reply and its on-screen speaking indicator were at most 53.3 ms apart. No playback underflow was observed. |
| Articulation | Alignment was ready at reply onset in four of five replies. One used the waveform fallback. Ten sampled lip closures were at or below 0.12 opening. |
| Waiting time | 2.64–3.28 s from question audio ending to reply audio starting. The recording profile allowed a bounded wait for the mouth plan. |

One earlier attempt played the replies but timed out during media retrieval.
Another preserved the media and exposed late phone alignment and uneven frame
spacing. The selected take used bounded binary uploads during recording. The
comparison concerns these finite samples. Codec timing and the speaking
indicator do not measure the perceptual correctness of individual mouth shapes,
human microphone latency, or long-running capture performance.

[Aggregated measurements](../publication/audio-timing-evaluation-20261118.json) ·
[Reproduction](reproduce.md)
