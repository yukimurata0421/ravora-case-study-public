# Speech articulation and audio-clock capture

The voice path can show an initial expression while the speaker talks and then
confirm or revise it using the full transcript. The expression follows the
[two-stage contract](early-reactions.md). The mouth follows the synthesized audio
and keeps lip closures during a reply.

## Concept 02 default motion

The live renderer, the development preview and the portable demo now sample
the same jaw opening and vowel weights. The preview adapts a prepared phoneme
plan and audio levels with `createPreviewMouth(plan, levels, hop, sampleRate, frames)`.
Here `levels` are decibels, `hop` is seconds between levels, and `frames` is
the audio sample count. The returned function samples a pose at playback time
in seconds. [Preview adapter](../src/ravora/web/preview-mouth.mjs) ·
[Live articulation](../src/ravora/web/articulation.mjs).

A stressed vowel used to make the mouth jump open on a single frame. The jaw
now averages samples up to 100 ms on either side of the playhead. As the lips
approach contact it gives more weight to a narrower 50 ms window so a short
/m/, /b/ or /p/ can still close. On the same smiling line the largest change
in normalized opening between consecutive frames fell from 0.31 to 0.22.
The side-by-side clip uses the same audio and artwork on both sides, with the
earlier motion on the left. [Comparison MP4](media/concept02-mouth-comparison.mp4).

The three questions in the full recording were synthesized and played at normal
speed. Times are relative to the recording start on the browser audio clock.
Each gap runs from the last question signal to the first reply signal.

| Question | Question ends | Reply starts | Gap | Partial to final expression |
| --- | ---: | ---: | ---: | --- |
| Bread or soup? | 6.89 s | 8.78 s | 1.89 s | Neutral to thinking |
| A loaf in the freezer | 15.89 s | 17.39 s | 1.50 s | Neutral to smile |
| Lemon or yogurt? | 25.63 s | 27.27 s | 1.63 s | Thinking held |

The 1080p MP4 contains 924 frames over 30.83 s (29.97 fps). The source missed
one 30 fps tick. The encoder dropped zero queued frames. The decoded soundtrack
matches the captured question and reply audio across all three turns.
[Full recording](media/concept02-default-motion.mp4).

## The dinner conversation

Three English questions about dinner were synthesized in advance and streamed at
normal speed. Recognition, both reaction stages, reply generation and reply
synthesis used live APIs. The MP4 includes both voices and the wait between them.

| Turn | Speaker in the MP4 | RAVORA in the MP4 | Silent gap | Partial → final |
| --- | --- | --- | --- | --- |
| Bread or soup? | 0.10–6.90 s | 8.90–11.69 s | 2.00 s | Neutral → thinking |
| A loaf in the freezer | 12.57–16.54 s | 18.50–21.28 s | 1.96 s | Neutral → smile |
| Lemon or yogurt? | 22.31–27.35 s | 28.93–32.87 s | 1.58 s | Thinking → thinking |

The audible boundaries come from decoded AAC in 10 ms windows at −48 dBFS.
The on-screen speaking indicator followed the first audible reply by 33–37 ms.
The source captured 1,029 frames. The 30 fps export held one frame for a
missing tick, giving 1,030 frames over 34.33 seconds. The figures describe
these three synthetic-speaker turns. [MP4](media/ravora-thinking-conversation.mp4) ·
[Transcripts, scores and audio measurements](../publication/thinking-conversation-timings.json).

In the third turn the thinking pose first crossed a 0.05 render weight at
23.93 s, about 3.42 s before the speaker finished. The final decision retained
the pose. The renderer combines the closed mouth and five registered vowel
drawings on one mesh. In the upper-face detail band it moves the neutral
texture with the brow, preserving the definition of the forehead and hairline.
In a renderer binding, `expressions.thinking.detailBand` holds two increasing
source-image row coordinates, `[515, 550]` in this recording. Above the first
row the renderer uses the warped neutral texture. By the second row it uses the
expression drawing. Both rows must lie within the source image.

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
  from the upper lip down as the jaw opens. The registered thinking set supplies
  its own closed lip detail. The surrounding cheek keeps the expression's skin,
  and the mesh deforms the chin with the jaw.
- The playout worklet reports the sample index at which it consumes the first
  PCM. Playback starts as soon as the normal jitter buffer has enough samples.
  The initial mouth shape follows the amplitude and spectrum of the playing audio.
  The aligner returns the word starts and ends in the same PCM. A result
  arriving mid-word can take over at the next word start, using a 180 ms blend.
  If the reply ends first, the waveform drives the mouth to the end.
  The listening mouth starts blending into the first vowel up to 50 ms
  before its audible onset, once playback has begun. The measured PCM also
  determines when to return to the listening pose. A pause within a reply
  keeps the lips under speech control. A smiling speaker keeps the width and
  lifted corners of the smile even as the lips close.

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

The partner opens with the same line in all three recordings. After each reply, a
synthetic speaker uses the dialogue so far to generate its next turn. I decoded
the audio from each MP4 and measured from the end of the partner's
voice to the first audible sound from RAVORA. The uncut recordings include
recognition, reply generation, synthesis, and playback.

| Recording | Turn 1 | Turn 2 | Turn 3 | Turn 4 | Mean |
| --- | ---: | ---: | ---: | ---: | ---: |
| Original demo | 5.02 s | 4.17 s | 4.85 s | 3.89 s | 4.48 s |
| First audio-first demo | 2.44 s | 2.16 s | 1.77 s | 2.43 s | 2.20 s |
| Current demo | 2.30 s | 1.96 s | 2.20 s | 2.06 s | **2.13 s** |

In the current demo the word-aligned mouth shapes take over on *weeks* at
8.93 s, *totally* at 21.96 s, *eating* at 34.12 s and *sounds* at 45.99 s.
On each transition the lips start with the opening already visible.
The audio worklet reported zero underflows across all four replies.
[Close-ups of all four transitions](images/balcony-tomato-mouth.png).

A development take had 100 ms and 133 ms frame intervals during the first
reply. Encoding after the conversation reduced the longest interval to 67 ms
over the full 49.3-second take. From 100 ms before each audible reply through
its first second, the longest intervals were 67, 33, 33 and 33 ms respectively.
The two 67 ms intervals left in the full video each span one missing 30 fps tick.

### Earlier audio-first measurements

The browser also recorded when it received the first PCM packet and when the
audio worklet consumed the first sample. Those events were an average of 1.61 s
apart in the original recording. They fell on the same reported audio-clock
tick in all four replies of the first audio-first demo. The dialogue and provider response times varied
between takes, so the full 2.28 s difference cannot be attributed to this change.

In the first audio-first demo the mouth initially followed the waveform on every turn. The
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
The current renderer uses the rig's measured gaze field to move the iris and
eyelids together, reaching up to 80% of its sideways pose. The earlier
[frame builder](../scripts/glance_frames.py) is available for preparing
rig artwork. [Eye close-ups between turns](images/balcony-tomato-eyes.png).

The [surface](../src/ravora/web/pngtuber-surface.mjs) consumes the registered
poses, the [presence module](../src/ravora/web/presence.mjs) schedules the glance,
and the [articulation module](../src/ravora/web/articulation.mjs) computes the
speech envelope from the received PCM. The idle timer runs when the session
is ready for another turn and releases when a new input begins.

## One clock for both tracks

The PCM tap marks the first recorded sample. During the conversation the
browser renders each observed pose and caption and records their state and
audio-clock tick. After playback, it paints those states again in order and
waits for the software encoder when its queue fills. The [capture module](../src/ravora/web/audio-clock-capture.mjs)
checks that every encoded tick matches an observed draw. Skipped ticks retain
their original positions.

In the current MP4, all 1,477 frame timestamps matched the live draws.
Seven 480 x 270 comparisons, four during speech and three at rest, also
matched pixel for pixel between the live canvas and the canvas before
compression. These pixel comparisons cover the seven sampled frames.

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

## November 20 lip and jaw revision

Four independent landmarks along the nose, upper lip, lower lip and chin
determine the face midline. The avatar binding rejects inconsistent landmarks
before warping. It corrects the outer lip edges against rest thickness and
checks for folded geometry. The shader keeps the upper teeth anchored as the
lower lip covers them. The 75 ms smoothing window preserves more of a short
stressed vowel while allowing the mouth to close for labial consonants.

The [34.83-second 1080p dinner recording](media/concept02-lip-motion.mp4)
contains a synthetic input speaker and three Zephyr replies. The development
browser measured 2.62, 1.77 and 2.61 seconds from input end to reply audio in
those turns. The [three-way smile comparison](media/concept02-lip-comparison.mp4)
reuses a line to show the change in mouth shape. The published surface and
articulation logic match the recording's source hashes after adapting the
module import for the portable package. These observations cover the recorded
turns; Windows speaker latency and long-term conversation quality remain unmeasured.

For a compatible portrait, the optional landmark measurement script accepts
the mouth region as its center and radii in pixels. It prints proposed landmark
coordinates and residuals for review before adding them to the manifest.
Install NumPy and Pillow for this script. [Measurement script](../scripts/measure_face_midline.py)

## November 21 conversational smile

The upper lip used to stretch horizontally when the avatar smiled during speech.
The WebGL renderer now takes the two registered lip images on either side of
the current mouth opening, aligns them to the inner edge of the moving lip,
and blends them. The five reference openings are 0%, 25%, 50%, 75% and 100%.
The teeth stay anchored to the head as the lower lip moves. A brown band of gum
is still visible above the teeth near full opening.
[Same-audio close-up](media/ray-upper-lip-comparison.mp4)

The 29.568-second reference recording includes three scripted questions and
RAY’s spoken replies. The smile is visible in the second reply around 17–19 s.
At the first reply onset, the browser recorded an 83.3 ms gap between draws
and the recording failed its frame-spacing check. The largest onset gaps for
the second and third replies were 33.4 and 33.5 ms. The exported 30 fps file
includes the held frames. [1080p reference recording](media/ray-upper-lip-conversation.mp4)
