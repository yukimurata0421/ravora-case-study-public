# Contact history as conversational background

The `contact-v1` policy keeps observed viewer contact separate from factual
memory. It uses SQLite schema 9 and adds no model requests. The state describes
familiarity and continuity; comment volume is not evidence of trust or affection.

## State and event boundaries

| Table | Responsibility |
| --- | --- |
| `relationship_events` | One source-bound receipt, eligibility reason, owner, scope, and acknowledged reply; no copied comment body. |
| `relationship_days` | Daily posted/eligible comments and played/eligible exchanges, using the first receipt's date in Asia/Tokyo. |
| `viewer_relationships` | Cumulative counts, active days, and capped familiarity units. |

The existing `viewer_key` identifies a viewer within a platform. Display names do
not merge identities. `offline` and `cloud` are separate. Developer conversations
do not create viewer relationship profiles; approved developer summaries still
follow the existing [memory sharing policy](privacy.md).

Receipt insertion and relationship accounting share a transaction. Re-fetching a
comment cannot add another contact. A new message with identical content remains
a new posted fact, while eligibility is assessed separately. There is no historical
backfill: earlier comments do not have a known eligibility assessment.

Playback credit is added in the existing first successful playback-acknowledgement
transaction. Generation, prepared audio, partial playback, and cancelled replies
do not establish a conversation. The bridge checks completion and claimed audio;
the store rechecks source, viewer, scope, deletion, and expiry. One source event can
receive at most one exchange credit, even after duplicate acknowledgements or a
different turn processing the same comment.

## Frequency, duplicates, and caps

Short messages, emoji, and rapid posting alone are eligible. Queue capacity and
viewer fairness affect reply selection, not whether a person is considered spam.
Historical or future-dated comments count as posted facts without relationship
credit. Within 120 seconds, normalized repeated content from the same viewer,
scope, and stream is excluded if no newly played reply intervened. This is a soft
duplicate-content exclusion, not a negative trust score. A repeated "yes" after a
new reply can contribute again.

Local promotion detection requires both a clear opening solicitation and an HTTP(S)
link. It is deliberately narrow. Context-dependent spam and unrelated posts are
not fully recognized by this rule. Trusted application code can supply
`RelationshipEvidence(kind, source)` at ingestion or call
`assess_relationship_event()` afterward. Corrections update aggregates atomically.
Neither path accepts arbitrary scores or dispatches an API request automatically.

```text
day_units = min(eligible_comments, 3) + min(eligible_exchanges, 2)
familiarity = 1 - exp(-sum(day_units) / 60)
```

One hundred comments in one day contribute three comment units; the same number
spread over twenty days contribute sixty. Played exchanges can add two units per
day. Counts remain exact above the cap. These are testable initial constants,
not parameters calibrated on a live audience. Absence does not erase contact or
lower a trust score. Recent activity and return gaps are separate context.

## What generation receives

Only the current viewer and scope are queried. A snapshot uses three indexed
queries, including at most ninety recent daily rows. The latest two eligible days
preserve a return gap even after today's first comment has been received.

```json
{
  "contact_history": "established_contact",
  "prior_contact": "across_prior_days",
  "recent_pattern": "reconnecting_after_a_gap",
  "prior_spoken_contact": "previously_played"
}
```

The model receives qualitative categories, not numeric scores, counts, gaps,
identifiers, or another person's statistics. Initial familiarity cutoffs are 0.1
and 0.5; a first eligible post after at least fourteen days can indicate a return.
Four active days in the last thirty indicate ongoing contact. These categories
provide background without requiring a greeting, gratitude phrase, intimate tone,
or stock reply. Missing history does not prove a first-ever visit.

Both request adapters retain the character, reply-length, memory-priority, and
untrusted-context instructions. Background is refreshed or cleared before a new
request. `relationship_lookup_ms` measures the local lookup. There is no extra API
request, though additional prompt tokens can add cost and generation time.

## Retention and controls

The existing raw-content expiry removes text and duplicate fingerprints while
keeping contact aggregates. Source removal or a ban retracts eligible
contributions; receipt and posted-fact accounting prevent re-fetch resurrection.
An explicit input-level memory opt-out cannot be promoted by a later normal label.
A permanent opt-out UI and deletion of an entire viewer's aggregate profile are
not added by this change.

`RAVORA_RELATIONSHIP_ENABLED=false` stops new collection, playback credit, and
generation context; it does not delete prior aggregates. Disabling the memory
master switch also disables these pipeline hooks. The portable demos ignore
dotenv files and remain offline.

## Reproduce and evaluate separately

```bash
python3 scripts/demo.py relationships
```

The [sample result](../examples/relationship-report.json) shows new, regular, and
returning backgrounds, daily caps, duplicate receipts/acknowledgements, unplayed
exclusion, and developer/scope separation. Playback is simulated and no free-form
reply is generated. Focused tests additionally cover concurrency, transaction
rollback, deletion, expiry, semantic corrections, migration, and both adapters.
See [live evaluation](live-evaluation-20261117.md) for the separate, imperfect
historical conversation-quality results.
