# Implementation history: causes, decisions, and outcomes

This record covers development through 2026-11-18 JST. It distinguishes runtime
observations from what can be reproduced in this portable repository. The
historical recordings, private database, and original artwork are not included.

## Development sequence

| Stage | Cause or requirement | Decision | Outcome and boundary |
| --- | --- | --- | --- |
| Isolated prototype | The development host also carried an existing live stream. | Use a separate process group, resource ceilings, independent audio/video, and an outside stop supervisor. | Finite runs completed with owned processes stopped. This did not qualify long-term coexistence. Host controls are not part of the portable package. |
| Character rendering | Both a supplied 3D model and a supplied image needed a preview path. | Keep a 3D path and build a separate 2D mesh-based character path. | The later v13 recordings use the 2D path; the image was not converted into a new 3D model. Original models and images remain excluded. |
| Intermediate image quality | Shrinking and alpha-blending mouth and eyelid patches produced double contours. | Align corresponding mesh points and map both endpoint textures to the same intermediate shape. Share the renderer between preview and export. | Double contours were reduced. Later eye-position defects also required correcting the expression artwork, not only interpolation code. |
| Speech and body motion | Amplitude-only lips and a static pose looked repetitive. | Add vowel shapes, opening intensity, expressions, gaze, delayed head following, breathing, and posture changes. | Scripted previews used available timing data. This earlier live path estimated shapes from audio features. |
| Provider boundaries | A preferred voice was expensive for the intended usage, and model choices could change. | Separate generation, reaction, and synthesis adapters; expose configuration gates. | A sentence-based voice path and a bidirectional HD path were both exercised. The fast path selected HD streaming; actual bills were not reconciled. |
| Comment ingress | The path around API calls had to work before live comments were enabled. | Add a read adapter, deduplication, source binding, cancellation, call ledgers, response validation, and playback acknowledgement. | Offline tests covered the path. Continuous live comment collection and receiver delivery were not established by those tests. |
| Memory | Fixed phrases were insufficient, and saved exchanges needed ownership rules. | Save source-backed replies, enqueue extraction only after playback acknowledgement, and retrieve approved context by tier. | Flexible extraction was checked with mocked structured responses. Later demo recall came from conversation history; it was not a live extraction-quality test. |
| Recognition latency | Local CPU recognition worked but delayed the reply. | Measure local recognition and a hosted alternative through the complete reply path. | In one earlier fixture study, local model reuse reached audio at about 7.72 s and the hosted path at about 3.28 s. Those intervals began at WAV submission, not the newer speech-end boundary. |
| Orchestration location | Moving the middle stages to a relay might reduce WAN crossings. | Compare local and warm relay execution, then test direct asynchronous RPC orchestration without a VM. | The relay mean advantage was about 0.20 s in three trials per route, insufficient for a general claim. Local orchestration remained the selected design. |
| Character and turn length | A playful voice still produced explanations too long for casual exchanges. | Share character and reply-length instructions across both request adapters. Preserve an explicit-detail exception. | Six runtime replies were one sentence, 4--8 words, with 1.60--2.84 s reply clips. This was an observation, not a hard word-count guarantee. |
| Reaction count | Repeated interim/final classifications could compete or arrive after cancellation. | Use one durable reservation for both paths; retain failed and unknown attempts. | The first version used one reaction request for each of five inputs. Unit tests also cover migration, contention, failure, late completion, and cancellation. |
| Real-time demonstration | Text-triggered clips hid the user's speaking time and the actual wait. | Send recorded questions in real time and capture questions, silence, replies, and render-thread audio onset in one take. | The recordings showed the complete interaction, rather than edited reply snippets. They still used synthetic questions, not a person's microphone. |
| v13 mouth defect | Per-frame silence was treated as the end of an utterance, allowing the expression mouth to pop into pauses. | Preserve the user's separation of voiced frames from an active reply. Fade the expression mouth out over 0.15 s at onset and back over 0.5 s after playback. | The revised runtime recorded three internal quiet frames with expression-mouth weight zero. Those frames had a neutral expression; smile plus a long internal pause was not observed. |
| Earlier provisional reaction | Interim results were enabled, but the original profile reported short inputs after they ended. The faster profile then returned persistently low stability. | Select the Tokyo `long` profile and accept a repeated-prefix condition, while preserving one reaction request per input. | All five runtime reaction notifications arrived during the question. Two selected a smile and both were rendered before speech ended; three preserved neutral. |

## Why the last change required two fixes

In the same 2.25-second input probe, the earlier recognition profile returned its
first result about 3.08 seconds after sending began. The selected Tokyo profile
returned an interim result about 0.54 seconds after sending began and produced
13 updates before the input ended. There was one probe per profile, so this is
a small observation, not a model-wide benchmark.

The faster profile repeatedly supplied stability `0.01`. Lowering the threshold
enough to accept that value would not establish reliable text. Instead, the
controller tracks agreement of the first three words across notifications. Once
that agreement lasts 120 ms and the current hypothesis has at least four words,
the latest whole hypothesis can consume the single provisional-reaction slot.
The [reaction contract](early-reactions.md) describes corrections and failure cases.

In the longer runtime input, the first transcript arrived 0.62 s after audible
speech began, the reaction notification at 1.62 s, and the first render with at
least 5% expression-art weight at 1.78 s. That render preceded the end of the
question by 7.62 s. The short greeting had only a 0.10 s visual lead.

The 5% weight is a rendering trace threshold, not a calibrated threshold for
human perception. A short input can end before any visible reaction is possible.

## What the portable update validates

The first `early-reaction` command used the actual controller and a shared SQLite
reservation with a controlled, synthetic transcriber. It verified ordering and
ownership of that slot. It did not reproduce hosted recognition latency or the
avatar. Character tests compare the actual HTTP and RPC request assembly without
making outbound calls.

Porting exposed a dependency on an older persistence condition: the earlier
snapshot saved reactions only while a turn was running. A pending provisional
reaction may finish after generation but before playback acknowledgement. The
update includes that runtime persistence change and preserves the completed state.
The initial failing portable test is retained in the local validation history.

The [measurement record](measurements.md) keeps earlier and later clocks and
sample sets separate. It does not calculate one improvement percentage across
different input, generation, and synthesis conditions. See the
[current validation record](../publication/validation.json) for portable checks,
and the [previous record](../publication/history/2026-11-16-validation.json) for
the original archive stage.

## Contact state and the later voice recording

| Cause or requirement | Decision | Result and boundary |
| --- | --- | --- |
| Past interaction should influence a reply without replacing factual memory. | Add source-bound contact events, daily aggregates, and capped familiarity; credit replies only after playback acknowledgement. | Duplicate receipts and acknowledgements are idempotent. Developer profiles are excluded; state and conversation quality are tested separately. |
| A burst of comments should not create unlimited familiarity. | Preserve raw counts while capping three comment units and two played-exchange units per day. | Equal post counts spread over many days contribute more than a one-day burst. Frequency and short messages alone are not treated as spam or distrust. |
| Return gaps vanished after ingesting the current comment, and numeric metadata appeared in speech. | Preserve the prior eligible day and map counters into qualitative background before inference. | Return context survives the first post; no numeric tally recitation was observed in the final finite sample. |
| Comfort replies reused sample dialogue; the updated artwork was not fully loaded. | Remove example dialogue, refine character guidance, and load the renderer's existing expression-vowel and motion textures. | Short replies remained, but comfort repetition, unfair teasing, and invented shared details still occurred. Avatar assets and renderer remain outside the portable slice. |
| A text-input video hid speaking time and normal conversation. | Prepare synthetic questions, play and stream the same PCM, and capture all waits with reaction timing overlays. | Six replies began 1.73--2.22 s after question audio ended; smile and surprise appeared during questions. Recent-dialogue recall was observed, not long-term extraction success. |

The [contact contract](relationships.md) explains state, privacy, and retention.
The [live evaluation](live-evaluation-20261117.md) preserves failed and partial
outcomes and distinguishes historical recordings from portable reproduction.
The case-study update adds `relationships` to the offline command-line entrypoint;
it does not create fabricated model replies for those profiles.

## Revising the face and synchronizing the recording

The final transcript can change the meaning of the provisional prefix. The
current controller assigns each phase its own durable claim and accepts results
in increasing phase order. Migration preserves the original single-slot limit
and consumed claims on existing inputs.

In the recording, simultaneous vowel layers initially revealed different lip
outlines. A shared target mesh and PCM-aligned mouth cues addressed the geometry
and timing separately. The new capture timestamps came from the recorded audio
clock, and a finite recording exposed a tradeoff between alignment coverage and
reply waiting time. The [media timing record](audio-timing.md) reports both, along
with the remaining frame gaps and the failed captures that led to the changes.
