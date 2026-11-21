# Provisional and final reaction stages

The recognition adapter publishes hypotheses while audio is still arriving. A
provisional worker can react to one hypothesis. Generation receives only the
final transcript after input ends. Interim text is not inserted into conversation
sources, retrieval context, or memory-extraction jobs.

## Eligibility

Very short normalized hypotheses are ignored. A final segment or a supplied
stability at least `0.8` can be used immediately. If stability is unspecified, an
unchanged hypothesis waits 120 ms.

A second route handles updates with persistently low stability. The latest
hypothesis must contain at least four words, and its first three words must agree
across at least two notifications for 120 ms. The current whole hypothesis is
submitted; the worker does not manufacture a sentence or claim high confidence.
Appending words does not reset the age of an unchanged prefix. A correction
resets agreement at and after the changed word. Case, whitespace, and punctuation
updates do not count as different words.

Word agreement establishes a stable prefix. A later negation can change its
meaning, which is why the final transcript has its own stage.

## Durable limit and lifecycle

Schema version 10 freezes the reaction limit when an input is created.
`reaction_stage_claims` permits one partial reservation and one final reservation.
The previous `input_reaction_claims` table keeps earlier consumption and the first
claim for compatibility. Migrated inputs retain a shared single slot. New inputs
default to two stages, and `RAVORA_EARLY_REACTION_MAX_REQUESTS=1` retains the shared
slot behavior. A stage remains consumed on timeout, failure, or cancellation.

The final request can start while the provisional request is pending. A stored
sequence ranks the final result above the provisional result. A same-expression
decision confirms the current face without restarting its motion. The final
decision may keep a supported provisional expression under the rule below.
Otherwise a changed decision updates the expression, including a return to
neutral. The browser checks input and turn identity and rejects duplicates and
lower sequences.
Deleting or expiring the source clears the cached result while preserving the
stage reservations.

Generation and synthesis do not await reaction completion. The input ID, turn
generation, source state, and playback state reject stale callbacks. An in-flight
reaction whose submitted words no longer match the latest prefix is discarded
without refunding the slot. A valid late reaction may update a generated but
unplayed turn; it cannot undo the turn's completed state.

## Expression selection

Both stages judge how Ray would react to what she has heard. A quietly delivered
compliment can make her smile. A report of an injury can bring out concern even
when the speaker sounds calm. The questions describe Ray's curiosity, independence
and warmth. They treat quotations and requests to change a score as conversation
data. Live adapter results identify the subject with `reaction_subject: ray`.

The console's dialogue profile scores joy, surprise, concern and deliberation
in one request per stage. A meaningful trade-off may make Ray look thoughtful.
If the highest score is below 0.60, she keeps a neutral face. When the final
joy and surprise scores both reach 0.60 and are at most 0.10 apart, the same
turn can retain its provisional smile or surprise. The stored result records
`jev_recommendation` and `reaction_selection=kept_supported_expression` when
that differs from the model's first choice. The basic, three-question adapter
retains its smaller display vocabulary.

The listening expression stays visible until playback begins. The persona can
then choose an expression for Ray's own reply. [Playback priority and microphone controls](conversation-controls.md#listening-speaking-and-muting)

The historical speaker-emotion evaluation used 12 selected English utterances from human
conversations. On the same final transcripts, the earlier questions matched the
preassigned acceptable expressions on 11 of 12, and the speaker-grounded questions on 12
of 12. One recording showed surprise about 2.04 seconds before speech ended and
a smile after the final transcript. The rule retained a provisional expression
twice in a separate synthetic-speech replay and zero times in the natural-speech
sample. The sample used fixed utterance endpoints. Conversation-wide accuracy,
perceived naturalness, and live turn-taking remain unevaluated.

## Offline replay

```bash
python3 scripts/demo.py early-reaction
```

The fixture supplies two low-stability hypotheses, with enough agreement time
between them. The replay confirms:

- the reaction arrives while input is still open and before generation starts;
- generation receives the complete final transcript once;
- interim text has no conversation source or memory job;
- the provisional and final stages each have one reservation, and the final
  result returns the simulated smile to neutral;
- all actual API-attempt counters remain zero;
- acknowledged playback uses the normal frame-count check.

The delay advances a fixture's state machine; it is not a recognition or reaction
latency measurement. Acknowledgement is simulated, and the command draws no avatar.

## Character and reply-length instructions

The reference request adapters share `conversation_prompt`. `RAVORA_PERSONA`
selects `cheeky`, `calm`, or `custom`; custom text comes from a UTF-8 file capped at
16 KiB. `RAVORA_REPLY_STYLE=brief` asks for one compact sentence, usually 5--12
English words, with shorter acknowledgements allowed. `normal` asks for two or
three short sentences. Explicit requests for detail can expand either policy.

These settings do not override source ownership, sharing, or input-trust rules.
They do not apply a generated-text cut-off or call another summarizer. The
portable entrypoints use explicit offline settings and canned replies, so their
output cannot establish character quality or compliance with the word target.
