# Conversation controls and memory ownership

## Selecting a voice

The current integration sends US English to the Tokyo Chirp 3 HD endpoint with
`RAVORA_VOICE_TTS_VOICE=en-US-Chirp3-HD-Zephyr` and
`GOOGLE_TTS_SPEAKING_RATE=0.96`. The streaming request has no pitch field.
On the capture host, Rubber Band 4 LiveShifter raises the received 24 kHz mono
PCM by `GOOGLE_TTS_PITCH=2` semitones with formant preservation. The browser,
mouth animation and saved utterance consume the same samples. At clean EOF the
processor drains its delay line to the original frame count. Cancellation
discards the tail. Zero semitones bypasses the native library.

Five Chirp voices read the same synthetic script; the sixth sample is a previous
WaveNet H synthesis of that script. The selected voice starts at 32.9 seconds.
The local pitch comparison was measured separately using Aoede: 43 voiced
windows had a median change of 1.97 semitones and both versions lasted 2.22
seconds. LiveShifter reported a 1,235-frame initial delay in that configuration.
[Voice audition](media/voice-audition.m4a) · [Streaming adapter](../src/ravora/edge_grpc.py)

## Listening, speaking and muting

| When | What the viewer sees |
| --- | --- |
| Listening or waiting for playback | Ray's provisional listening reaction, updated when the final judgment arrives. |
| The audio worklet starts playing this reply | A specific persona expression takes priority and its public motion starts. A neutral or absent expression lets the latest listening reaction show through. |
| A final listening judgment arrives during speech | It updates the fallback face. An active, specific persona expression holds until playback ends. |
| Playback ends or the input is cancelled | Ray returns to neutral. Messages for an earlier turn cannot set her face. |

The persona uses the existing generation call. The streaming adapter requests a
single public object containing speech, expression and motion. The controller
validates the object before publishing the action and sending its speech field
to synthesis. Malformed replies fail before audio starts. The offline mock
supplies the same fields, so the handoff works with the built-in synthetic face.
[Output validation](../src/ravora/public_output.py) · [Expression selection](early-reactions.md#expression-selection)

**Mute microphone** is available on the stage, including in fullscreen. Muting
ends an open recording or an armed hands-free input. A sealed turn can finish
speaking and the text box remains usable. The browser stops its microphone
tracks, and a configured host capture releases its device. While muted, the
server ignores audio starts and microphone frames. Unmuting in hands-free mode
rearms capture when the console is idle. The tab remembers mute across reloads.
Audio received before mute follows the normal [evaluation retention policy](voice-evaluation.md).

## Topic changes and recall

Ray can offer a brief impression of a co-host's news or take the first step in
a challenge. The live studio and streaming console use the same local dialogue
router. It considers the speaker's current words and acknowledged turns from
the same session. A direct question or request to continue keeps the conversation
on its subject. A backchannel can give Ray room to introduce a related idea.

Each immediately preceding exchange on the same subject adds 0.35 to the
saturation score, up to 1.0. At 0.70 the router skips knowledge retrieval.
For example, after two exchanges about jazz, another observation about rhythm
can lead to footsteps on different surfaces. A question about jazz chords
still gets a direct answer using the current conversation. The router can
also skip retrieval during a listening turn or when Ray volunteers an interest.
The turn metric `knowledge_rag` records `allowed`, `skipped_saturation`, or
`skipped_dialogue_policy`. Routing adds no model request.

Schema 15 adds optimism and a willingness to try challenges to Ray's profile.
Schema 16 keeps up to two new, curated topic suggestions per acknowledged turn.
Each suggestion belongs to one speaker and scope and is linked to its source.
A turn can reserve one suggestion. It becomes used after acknowledged playback
when the spoken reply contains a recognized English or Japanese expression for
that subject. Cancellation or an unrecognized paraphrase leaves it available.
Source removal, source expiry cleanup and character-memory reset remove the
associated suggestions. Momentary feelings and unselected suggestions stay out of
the speech prompt. [Router](../src/ravora/dialogue_router.py) · [Playback and routing tests](../tests/test_dialogue_router.py)

## Ray's interests and viewer memories

Ray starts with three interests in the live studio and the offline console.
She likes recognizing constellations by shape, solving puzzles by spotting a
decisive clue, and understanding how speech becomes subtitles. Each starts at 0.60. Starting again
preserves existing strengths and deletions in the same database. Repeated,
acknowledged conversations can strengthen an interest. When someone asks about
Ray's hobbies, the speech prompt can draw on configured character interests
even when the router skips viewer recall. These interests supply character
background and create no records about the speaker.

In the cloud studio, set `RAVORA_API_ENABLED=true` and
`RAVORA_MEMORY_API_ENABLED=true` in the private `.env` to enable remote
memory extraction. After an acknowledged reply, the worker can send the
speaker's message and reply context to the configured text provider. It checks
opt-outs before sending and accepts only summaries backed by an exact quote
from the speaker. Summaries and topic tags can be Japanese or English.
Japanese topic tags support lookup inside questions written without spaces.
The next reply can use committed memories while extraction continues in the
background. Previously skipped turns stay skipped.
[Extraction tests](../tests/test_gemini_memory.py) · [Character tests](../tests/test_ray_character.py)

## From a pause to the next turn

The input controller uses provisional transcripts and speech-activity events.
It checks the input audio timeline before accepting an end-of-speech event.
Hands-free mode waits for a new onset, retains the short lead-in, releases
capture during the reply and rearms after browser playback. Empty or oversized
transcripts can rearm; provider and budget errors remain visible. A connection
loss invalidates its turn and releases the microphone. Acknowledging the exact
rendered frame count commits the played reply. [Input controller](../src/ravora/fast_voice.py)

The reply stream can start up to two additional requests at 1.3 and 2.6 seconds
if no provider response has arrived. The controller selects one responding stream
and cancels the others. Speech synthesis starts when validated reply text is available. These thresholds govern dispatch; the provider can still take longer.

## Recall across different wording

The sample configuration leaves episode retrieval off. A separate integration can
set `RAVORA_SEMANTIC_MEMORY_MODE` to `candidates` or `jev`.
Schema versions 11 and 12 add episodes, cited source spans, transitions and
owner-scoped retrieval indexes. Candidate selection combines FTS, recent open
events and prior ranking within a bounded set. A final reaction can also judge a
candidate's relation, topic continuity and explicit state change. Relatedness
alone leaves an event unconfirmed. A topic switch can leave the earlier event
open. An ambiguous reference can use a tentative conversational check; a missing
or late judgment cannot confirm an event. The worker verifies source spans,
owner, expiry and playback state before committing asynchronous extraction.
[Episode retrieval](../src/ravora/semantic_memory.py) · [Synthetic cases](../tests/test_semantic_memory.py)

Schema version 13 indexes developer-owned sources by event ID and owner key.
The migration backfills eligible existing events. The input channel and
configured identity establish the speaker. A name typed in a public message
cannot establish that identity. The existing explicit approval, withdrawal,
deletion and expiry rules continue to govern shared summaries.
[Ownership tests](../tests/test_conversation_context.py)

## Budgeting a cloud integration

| Service | Example monthly admission limit |
| --- | --- |
| Mono speech recognition | 43,200 seconds |
| Speech synthesis | 1,000,000 characters |
| Reply generation | USD 7 in estimated reservations |

The example budgets the three services together and accounts for the reaction
provider separately. Speech-to-Text V2 standard recognition is charged from
the first minute. Chirp 3 HD lists one million free characters per month. At
the published rates used in the November 20 estimate, 720 minutes cost
USD 11.52. If all three limits are used, the estimate is USD 18.52 with the
full synthesis allowance available, or USD 48.52 if other usage has exhausted
it. At the planning conversion of JPY 198 per dollar, these are about JPY
3,667 and JPY 9,607. These are application estimates. The application cannot
observe other projects' usage, taxes, exchange rates or the billing account's
remaining allowance. [STT pricing](https://cloud.google.com/speech-to-text/pricing) · [TTS pricing](https://cloud.google.com/text-to-speech/pricing)

Before the first reply request, SQLite reserves an upper estimate for the
assembled input, maximum output and all possible additional attempts in one
transaction. Failed, cancelled and ambiguous attempts retain the reservation.
The public console uses an isolated temporary database and simulated providers.
[Reservation tests](../tests/test_database.py)
