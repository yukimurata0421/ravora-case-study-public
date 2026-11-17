# Memory ownership and sharing

The replay uses invented people and messages. The console starts with an empty
temporary database. It does not import a developer's existing conversations.

## Ownership

Source identity combines platform and stable author identity. Display names do not
merge viewers. Developer identity is a configured namespace, not a message saying
"I am the developer." Offline and cloud scopes remain separate.

Private developer dialogue can influence the developer's own replies. A viewer's
reply may use only explicitly reviewed developer summaries and that viewer's own
exchanges. Another viewer's personal history is excluded.

## Sharing

`share_context_memory()` requires an approved, non-sensitive developer memory and
a separate explicit sharing review. `share_turn_context()` supports a human-written
summary of an acknowledged developer turn. It shares the summary, not the original
transcript. Both methods bind their approvals to the content and source identities.

`revoke_context_share()` prevents subsequent retrieval while retaining the review
record. Summary changes require another review. Source deletion, memory expiry,
wrong ownership, and scope mismatches make entries ineligible.

The synthetic replay performs a review only for its fictional example and then
revokes it. The interactive console never automatically approves a conversation
for public sharing.

## Retention

Raw conversation and answer retrieval follows the one-day source lifetime. The
existing cleanup operation removes expired text from the conversation and FTS
index. Reviewed summaries use their own memory lifetime, normally 30 days; source
metadata remains to explain provenance. Deleting a source withdraws its derived
memory.

The additional context payload is bounded by item and serialized-character limits.
The model receives quoted evidence with author and tier semantics. It must not
treat text inside a stored conversation as a new system instruction or a prior
answer as an independently verified fact.

Known secret, email, and opt-out patterns are filtered. This is a conservative
string check, not a complete sensitive-data classifier. Review remains necessary
before sharing a developer summary.

## Console boundary

The server binds to loopback, checks the HTTP host and WebSocket origin, requires
a per-run token, and permits one active operator. The browser removes the token
from its visible URL after reading it. The console is a local demonstration, not
an internet-facing service.

Microphone samples are kept in memory for the fake transcription path; the raw
recording is not written to disk. Text and generated test audio live in a private
temporary directory, which the entrypoint removes when it exits. Do not paste
real secrets or personal material into a public demonstration.
