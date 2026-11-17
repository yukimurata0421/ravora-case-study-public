# Historical conversation, expression, and latency evaluation

These development observations were collected on 2026-11-17 JST, before this
portable update. They are curated from retained private evidence. This repository
does not contain the original recordings, artwork, databases, or raw API logs.
Updating the case study made no new inference calls. Machine-readable aggregates
are in [the evaluation summary](../publication/evaluation-20261117.json).

## Relationship comparison

The comparison used synthetic histories: no prior contact, a regular viewer with
forty comments and twenty played exchanges over twenty days, and the same history
after a forty-six-day gap. A fourth condition omitted relationship background.
Each trial began from a copied seed database, with no private developer dialogue
or factual memories. Five normal English prompts covered arrival, a cooking
mistake, praise, a tiring day, and recognition of the viewer. Conditions were
shuffled with a fixed seed and repeated twice in the final comparison.

The configured text model stayed the same. Character `cheeky`, reply style `brief`,
temperature 0.7, and maximum output tokens 400 were retained. There were three
evaluation stages: 33, 41, and 41 accepted requests, each including a separate HTTP
request-assembly check. The final comparison had ten replies per condition.

| Condition | Median first text | Median complete text | Median words | Maximum words |
| --- | ---: | ---: | ---: | ---: |
| New contact | 811 ms | 913 ms | 8.5 | 10 |
| Regular contact | 812 ms | 925 ms | 8.5 | 13 |
| Returning contact | 731 ms | 820 ms | 8 | 10 |
| Background omitted | 797 ms | 890 ms | 8.5 | 12 |

Connection startup preceded these intervals. Local relationship retrieval for the
thirty replies with background had median 2.43 ms and p95 3.95 ms. This small
sample does not establish a causal latency overhead or an upper bound.

Of those thirty replies, twenty-nine stayed within twelve words, twenty-nine were
one sentence, and twenty-seven were distinct. Five of six comfort replies still
contained "not going anywhere". Unique whole responses alone do not show that
stock phrasing has disappeared.

| Observed problem | Change and result |
| --- | --- |
| Receiving a return comment replaced the previous gap with zero. | Read the latest two contact days and preserve the gap on the first eligible post today; forty-six days remained available internally. |
| Replies repeated the character file's sample comfort line. | Removed sample dialogue and retained descriptive personality guidance. Similar comfort wording still recurred. |
| A reply recited a numeric visit count. | Convert statistics into qualitative background in application code. No numeric tally recitation was observed in the final sample. |
| New expression-vowel and motion assets were missing in the v13 loader. | Load the existing forty-five vowel/expression textures and four motion textures. This renderer fix remains in the development tree; the portable console does not render an avatar. |

Remaining quality problems include an unfair "for once" tease, treating missing
records as proof of a first visit, inventing "earlier game days", and assuming
success happened "so fast". Regular and returning responses were not reliably
different. Short, playful replies were observed, but natural relationships and
grounded warmth are not complete.

A separate four-turn text-input recording used the updated artwork and real
generation, reaction, and synthesis. Three matched recognition questions reached
browser audio at 1.492, 1.148, and 1.712 seconds for new, regular, and returning
conditions. A praise turn took 1.462 seconds. These exclude STT. The whole
relationship study and recording used 119 generation, four reaction, and four
synthesis requests. It did not use live viewer identities or comments.

## Audible questions and ordinary conversation

A later, separate six-turn recording played prerecorded synthetic English
questions while streaming the same audio to recognition. It captured both sides
and the real wait in one continuous 53.566-second take at normal speed. The
question audio was synthesized in advance; that preparation time is excluded
from conversation latency. This was one fresh developer conversation, not a
profile-switching relationship comparison.

Recognition used the Tokyo `long` profile. Interim text consumed the single
reaction slot; the final transcript alone reached text generation. The streaming
HD reply path and updated avatar were retained. Channels were prepared before
the dialogue. The first four timing columns below begin at the question's first
non-silent window; the final column begins at its last non-silent window.

| Topic | First STT | Reaction notification | Visible expression | Lead before question ends | End of question to reply audio |
| --- | ---: | ---: | --- | ---: | ---: |
| Happy to meet | 0.41 s | 1.20 s | Smile at 1.39 s | 1.91 s | 1.77 s |
| First award | 0.42 s | 1.91 s | Surprise at 2.10 s | 1.84 s | 1.85 s |
| Losing a game | 0.44 s | 1.45 s | Neutral retained | Not applicable | 1.73 s |
| A tiring day | 0.54 s | 1.42 s | Neutral retained | Not applicable | 2.14 s |
| Coffee and jazz | 0.36 s | 1.69 s | Neutral retained | Not applicable | 1.84 s |
| Recalling that preference | 0.44 s | 1.32 s | Neutral retained | Not applicable | 2.22 s |

All six reaction notifications arrived while the question was audible. Only two
changed the face: the current mapping selects sufficiently strong joy or surprise
and does not map concern directly to a sad expression. One early classification
cannot incorporate meaning that arrives later; there was no second call.

Replies were six to fifteen words. One exceeded the twelve-word target, but none
expanded into a long explanation. The final reply recalled coffee and jazz from
the recent played dialogue. All `memory_ids` results were empty, so this was not
a successful live long-term memory extraction test. Transcript errors were kept,
including the character name's spelling and "spent" becoming "spend".

Audio onset used an AudioWorklet with a 10 ms, -48 dBFS window. Expression onset
was the first rendered frame at least 5% weighted toward the target artwork, not
a human-perception threshold. Capture was capped at 15 fps and output at 30 fps.
Decoding the final MP4 and detecting both sides independently reproduced the
speech-end/reply gaps within 7.3 ms of the browser measurement. No audio timestamp
gap exceeded 50 ms. Extracted listening frames visibly showed the smile and
surprise. All six playback acknowledgements were accepted, with no PCM underflow.

Two observed silent speaking frames had no expression-mouth pop. This small
sample does not cover every expression or long internal pause. The user's v13
utterance-level mouth fix was preserved.

The run used thirty API requests: six to prepare question audio, then six each
for recognition, reaction, generation, and reply synthesis. A separate two-turn
offline capture passed and is not mixed into these measurements. Owned recording
processes stopped and temporary credentials were removed. There was no human
microphone, physical-speaker, OBS, receiver, or long-duration validation.

## Preserved failures and publication limits

Relationship recording preparation first stopped before API calls because its
preparation interpreter lacked an image library. A subsequent recording completed,
but the supervisor could not read a result file's permissions. The retained
recording, request ledger, and stop state were reconciled without replaying APIs;
result permissions were corrected for later runs. Failed evidence was retained.
The later six-turn recording completed through the standard supervisor.

The portable [relationship replay](relationships.md) tests deterministic state
and request boundaries. It cannot reproduce these live quality outcomes, measured
latency, or avatar rendering. Historical runtime tests, portable checkout tests,
installed-package tests, and remote CI are separate evidence gates.
