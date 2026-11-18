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
  On a registered rig, neighboring vowels and the current expression displace
  the same set of lip landmarks. One image supplies the outer lip contour.
  Inside the lip seam, the renderer reveals the expression's teeth and tongue
  from the upper lip down as the jaw opens. A closed mouth samples the neutral
  portrait. A narrow feather blends the surrounding skin.
- The playout worklet reports the sample index at which it consumes the first
  PCM. Playback starts as soon as the normal jitter buffer has enough samples.
  The initial mouth shape follows the amplitude and spectrum of the playing audio.
  The aligner returns the word starts and ends in the same PCM. A result
  arriving mid-word can take over at the next word start, using a 90 ms blend.
  If the reply ends first, the waveform drives the mouth to the end.
  While audio is buffered the listening pose holds. When playback starts,
  its resting geometry eases into articulation over 230 ms. A pause between
  words keeps the lips under speech control. At playback end, returning to the
  listening pose takes 420 ms.

The reference code lives in the Python `ravora.phone_alignment` and
`ravora.speech` modules and the packaged browser mouth-plan, articulation, and
expression modules. A browser can pass a phoneme message and its arrival time
to [`createWordHandoff`](../src/ravora/web/articulation.mjs), then call
`sample(wavePose, audioContext.currentTime, playbackStart, sampleRate)` each
frame. The start time comes from the audio worklet's first consumed sample.
A result received after the final word has begun leaves the waveform in control.
The [media timing replay](../examples/media-timing-report.json)
uses a synthetic phone sequence. The aligner is optional and runs locally.

## Reply timing in the balcony conversation

The partner opens with the same line in both recordings. After each reply, a
synthetic speaker uses the dialogue so far to generate its next turn. I decoded
the audio from each published MP4 and measured from the end of the partner's
voice to the first audible sound from RAVORA. The uncut recordings include
recognition, reply generation, synthesis, and playback.

| Recording | Turn 1 | Turn 2 | Turn 3 | Turn 4 | Mean |
| --- | ---: | ---: | ---: | ---: | ---: |
| Previous demo | 5.02 s | 4.17 s | 4.85 s | 3.89 s | 4.48 s |
| Audio-first demo | 2.44 s | 2.16 s | 1.77 s | 2.43 s | 2.20 s |

The browser also recorded when it received the first PCM packet and when the
audio worklet consumed the first sample. Those events were an average of 1.61 s
apart in the previous recording. They fell on the same reported audio-clock
tick in all four new replies. The dialogue and provider response times varied
between takes, so the full 2.28 s difference cannot be attributed to this change.

In the new demo the mouth initially followed the waveform on every turn. The
phoneme track arrived 0.84–1.09 s into playback. The transition began on
*just*, *feels*, *after*, and *right*, 8–30 ms after each estimated word start.
The mouth opening matched the waveform on the first transition frame in all
four turns. A frame-by-frame review of the encoded mouth region showed a
single lip contour through each transition. The worklet reported zero
underflows. The longest frame interval was 100 ms in the 1080p recording.
These observations cover four English replies with a synthetic partner.

## A glance between turns

The renderer starts a short sideways glance after an uninterrupted idle interval.
An incoming turn releases the glance from its current position within 180 ms.
The eyelid animation suppresses it as the eyes close. To build the movement,
I marked the pupils and the inside of each eyelid on a neutral crop and a
generated left-looking pose. I aligned the pupils before blending the two images
into nine small frames. The mask leaves the surrounding face in place. The
[frame builder](../scripts/glance_frames.py) takes two crops and the marked eye
regions and runs offline with Pillow.

The [surface](../src/ravora/web/pngtuber-surface.mjs) consumes the registered
poses, the [presence module](../src/ravora/web/presence.mjs) schedules the glance,
and the [expression module](../src/ravora/web/expressions.mjs) provides the
playback handoff. With an owned rig, keep both samplers alive across frames:

```js
const restPose = createMouthHandoff();
const idleGlance = createIdleGlance(seed);

function draw(time, playing, idle, lip, emotion, movement) {
  const face = composeExpression(lip, emotion, playing);
  face.expression.restPose = restPose(time, playing);
  movement.idleGlance = idleGlance(time, idle);
  return surface.render(face.open, face.blink, face.width,
    face.expression, movement, face.vowels);
}
```

Here `time` is the playback clock in seconds. `playing` spans the whole utterance
including quiet phones. `idle` means the system is ready for another turn.

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
