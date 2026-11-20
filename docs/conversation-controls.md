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
and cancels the others. Speech synthesis starts with the first usable reply
text. These thresholds govern dispatch; the provider can still take longer.

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
