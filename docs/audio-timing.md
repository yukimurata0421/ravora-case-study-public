# Speech articulation and audio-clock capture

The voice path can show an initial expression while the speaker talks and then
confirm or revise it using the full transcript. The expression follows the
[two-stage contract](early-reactions.md). The mouth follows the synthesized audio
and keeps lip closures during a reply.

This page describes the current design. Dated recordings, their per-turn timing
tables and the superseded motion they show are kept in the
[recording history](recording-history.md).

## Current mouth and jaw

The November 22 revision moves the chin along the portrait's measured midline as
the mouth opens. The live mesh tapers this movement toward the cheeks and retains
the original texture coordinates for each vowel. Rounded vowels reduce the smile
overlay, and the optional English phone aligner uses lexical stress to vary the
opening. The side-by-side comparison plays the same speech and expressions on
both sides, with the earlier motion on the left. At 15–17 seconds, watch the chin
descend as the mouth opens.
[Same-audio 1080p comparison](media/ray-mouth-before-after-comparison.mp4)

Four independent landmarks along the nose, upper lip, lower lip and chin
determine the face midline. The avatar binding rejects inconsistent landmarks
before warping. It corrects the outer lip edges against rest thickness and
checks for folded geometry. The shader keeps the upper teeth anchored as the
lower lip covers them.

For a compatible portrait, the optional landmark measurement script accepts
the mouth region as its center and radii in pixels. It prints proposed landmark
coordinates and residuals for review before adding them to the manifest.
Install NumPy and Pillow for this script. [Measurement script](../scripts/measure_face_midline.py)

## Conversational smile

The upper lip used to stretch horizontally when the avatar smiled during speech.
The WebGL renderer now takes the two registered lip images on either side of
the current mouth opening, aligns them to the inner edge of the moving lip,
and blends them. The five reference openings are 0%, 25%, 50%, 75% and 100%.
The teeth stay anchored to the head as the lower lip moves. A brown band of gum
is still visible above the teeth near full opening. The
[README conversation](../README.md#see-it-in-30-seconds) shows this smile at
0:17–0:19; its frame-spacing result is in the
[recording history](recording-history.md#november-21-conversational-smile).

## Jaw smoothing

The live renderer, the development preview and the portable demo sample
the same jaw opening and vowel weights. The preview adapts a prepared phoneme
plan and audio levels with `createPreviewMouth(plan, levels, hop, sampleRate, frames)`.
Here `levels` are decibels, `hop` is seconds between levels, and `frames` is
the audio sample count. The returned function samples a pose at playback time
in seconds. [Preview adapter](../src/ravora/web/preview-mouth.mjs) ·
[Live articulation](../src/ravora/web/articulation.mjs).

A stressed vowel used to make the mouth jump open on a single frame. The jaw
now averages samples around the playhead and gives more weight to a narrower
window as the lips approach contact, so a short /m/, /b/ or /p/ can still close.
The wide jaw window now spans ±75 ms instead of ±100 ms. It keeps more of each
opening peak while still allowing the mouth to close for labial consonants.
Vowel weights are still averaged over ±100 ms.
[Measured effect of the first smoothing change](recording-history.md#concept-02-default-motion)

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
- In the upper-face detail band the renderer moves the neutral texture with the
  brow, preserving the definition of the forehead and hairline. In a renderer
  binding, `expressions.thinking.detailBand` holds two increasing source-image
  row coordinates, such as `[515, 550]`. Above the first row the renderer uses
  the warped neutral texture. By the second row it uses the expression drawing.
  Both rows must lie within the source image.

The reference code lives in the Python `ravora.phone_alignment` and
`ravora.speech` modules and the packaged browser mouth-plan, articulation, and
expression modules. A browser can pass a phoneme message and its arrival time
to [`createWordHandoff`](../src/ravora/web/articulation.mjs), then call
`sample(wavePose, audioContext.currentTime, playbackStart, sampleRate)` each
frame. The start time comes from the audio worklet's first consumed sample.
A result received after the final word has begun leaves the waveform in control.
The [media timing replay](../examples/media-timing-report.json)
uses a synthetic phone sequence. The aligner is optional and runs locally.
[Close-ups of four waveform-to-word transitions](images/balcony-tomato-mouth.png)

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
their original positions. The
[balcony recording](recording-history.md#reply-timing-in-the-balcony-conversation)
records how this change affected frame spacing.

`python3 scripts/demo.py media-timing` reproduces the timestamp mapping and a
short mouth plan. `node --test tests/check_media.mjs` exercises the browser
modules with a stub encoder and synthetic mesh vertices. These checks cover the
timing contract and pose calculation. The packaged console still demonstrates
test tones and reaction labels. The complete recording environment, original
character and source recordings are kept outside this snapshot.
