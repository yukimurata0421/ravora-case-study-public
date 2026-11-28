# Broadcast observations

A studio run records chat receipts, completed replies, audio sample positions, audience
observations and API attempts in its local SQLite database. The report groups studio
sessions by the provider's broadcast ID. A separate exporter selects counts and timings
for the public site. [Live example](https://yukimurata0421.dev/ray/)

The [Twitch voice measurements and public player observer](twitch-local-tts.md) describe the two measurement endpoints.

## Try the pipeline offline

From the checkout with a virtual environment active, run the following commands on Linux.

```bash
python -m pip install '.[observations]'
preview=$(mktemp -d)
ravora-ray-site --output "$preview" --source-command python examples/broadcast_observations.py
python -m http.server 8000 --bind 127.0.0.1 --directory "$preview"
```

Open `http://127.0.0.1:8000/ray/`. The fixture creates a temporary database for one
fictional January broadcast with four comments from one speaker. Three replies started
within five seconds. The simulated speech budget held the fourth reply, so the page
shows 3/4. The speaker wrote again after a reply. Only one comment was already waiting when the
previous utterance ended. Its 250 ms gap therefore appears with a sample count of 1/20.
The fixture never contacts a platform or speech provider. The generated HTML, CSS,
JavaScript and aggregate JSON remain in the preview directory.

## Read a studio database

```bash
ravora-studio-report --db state/conversation.sqlite3 report
ravora-studio-report --db state/conversation.sqlite3 timeline SESSION_ID
ravora-ray-export --db state/conversation.sqlite3
```

The first two commands are local diagnostics. The timeline contains receipt and turn IDs.
Only the last command emits the schema accepted by the public site. Report and export
open a read-only SQLite transaction. The studio applies migrations when it opens the
database for normal operation. Rehearsals are recorded locally with platform `local`.
Sessions that predate broadcast identity tracking contribute separate historical counts.

| Observation | How to interpret it |
| --- | --- |
| Audience | Twitch live samples supply current, mean and sampled peak concurrency. The per-broadcast unique audience comes from an imported platform summary. An offline or failed sample differs from zero viewers. |
| Participation | A platform account enters the first-commenter count once at its first observed broadcast. A completed reply to that account and a later comment in the same broadcast advance the next two counts. |
| Ray gap | Last audible sample in the preceding Ray utterance to first audible sample in the next reply, on one program clock. The target comment must have arrived before the preceding audio ended. |
| Comment to program | Studio receipt to the first audible Ray frame accepted by the program writer. Preparing the next reply and putting it on air have separate timestamps. |
| Comment to player | Platform event time to independently matched player audio. Each reply needs an actual match to enter the sample. |
| API usage | Counts include dispatched retries and discarded prefetched calls. A known cost subtotal accompanies the count of unpriced calls. Estimated prices describe the rate assumptions in the report. |

Each broadcast shows the count of eligible comments with an observed audio start,
with a start within three and five seconds, and with a budget stop. All four counts
use every eligible comment received during observed chat coverage as the denominator.
A broadcast with zero eligible comments has no rate. Missing coverage is marked
unobserved. The stream card identifies the source of an end observation. For Twitch,
the last successful Helix sample and the consecutive and total 401 counts accompany
the chat coverage. An EventSub authentication error leaves the scheduled Helix worker running.

The report gives p50, nearest-rank sample p95, measurement count and missing count.
A sample p95 based on 1–19 intervals is provisional. The comparison p95 starts at 20.
A slot or all-broadcast gap pools eligible intervals across broadcasts. The page displays
gap by slot and broadcast. The aggregate JSON also provides the overall gap.
A time slot enters the first-commenter comparison after a completed scheduled broadcast
has at least 114 minutes of observed chat and 95% chat coverage. Live counts are available
earlier. Open the broadcast details for chat coverage, audio timings and provider usage.

### Import independent evidence

```bash
ravora-studio-report --db state/conversation.sqlite3 import summary.json
ravora-studio-report --db state/conversation.sqlite3 match-player SESSION_ID TURN_ID player.wav \
  --capture-start 1800000000.125 --reference first-chunk.wav > matched.json
ravora-studio-report --db state/conversation.sqlite3 import matched.json
```

A summary record has `kind` set to `summary`, the platform and stream ID, Unix
`started_at` and `ended_at` timestamps, and a nonnegative `unique_viewers` count.
Optional `average_viewers` and `peak_viewers` are provider-reported values.
Import accepts one record or a JSON array in a transaction. A `test_comment` record
uses the local session and receipt to exclude that message from the public counts.
Keep these input files private. Player matching uses the live studio dependencies,
a synchronized player capture and the corresponding first TTS chunk. Weak or ambiguous
matches leave the reply unmeasured. [Player capture procedure](twitch-live-operations-20261124.md#what-the-viewer-received)

## Publish observations from a separate host

The exporter writes `ravora.ray_public_observation.v9`. The site accepts versions v4 through v9 and checks each against its own schema and slot calendar.
It rejects unexpected fields before rendering. A successful collection writes
`ray-observation.json`, `ray/index.html` and the two referenced assets. The initial HTML
contains the measurements. An open tab checks for a newer snapshot every 30 seconds
and also checks on returning to the tab. The last provider confirmation expires after
150 seconds. The page marks a snapshot older than five minutes as stale.

Use a private studio directory for the database and a separate output directory for
the static host. The collector can read stdin or run an explicitly supplied exporter.
For example, configure an SSH alias whose command runs the installed exporter in the
studio working directory. From the site host run:

```bash
ravora-ray-site --output public --source-command ssh -T -o BatchMode=yes studio-source ravora-ray-export
```

The example [collector service](../examples/systemd/ray-observation-site.service) and
[30-second timer](../examples/systemd/ray-observation-site.timer) use an operator-provided
source alias in `RAY_OBSERVATION_SOURCE` from `~/ray-site/source.env`. Install the
observations extra in `~/ray-site/.venv` on the publishing host. Deploy the resulting
four static files through your own publisher. A failed fetch or validation retains the
last successful snapshot and collection timestamp. The example SSH command requires
noninteractive access to the exporter. The static host needs no chat, model or platform
credentials.

## Selecting speakers when chat is busy

Migration 024 adds the local `speaker_rotation_v1` policy. Each waiting speaker
gets one position. An update from that speaker keeps their waiting time and
lottery ticket. Expired entries leave before admission. At capacity, tickets
from distinct speakers compete within the first-reply, continuation and open
opportunities. The ticket stays stable for that speaker during a freshness
window. Quiet sessions dispatch the first eligible comment immediately.

Each four opportunities give two to speakers who have received no individual
reply in this broadcast, one to a continuation and one to any speaker. An empty
opportunity goes to the oldest eligible speaker, with a ticket breaking ties.
A continuation can use its opportunity early when the same viewer answers a
played question within the comment TTL. An explicit chat reply must name the
original message. For a spoken question the fallback uses the addressed viewer,
publication time and a subsequent answer. A continuation cannot open another
continuation. The playout deadline uses the larger of remaining program audio
and the recent preparation p90, with a one-second initial estimate.

Equivalent public questions about the current music can share an open
opportunity. One group reply occupies one audio turn and credits zero
individuals. The local report includes eligible and individually reached
speaker counts, their ratio, group replies, selection lanes, rejection reasons
and estimated cost of discarded prefetches. The report uses a null ratio when
there were no eligible speakers. Older sessions have no policy marker.

## Scheduled Twitch shows and prepared replies

The slot plan uses JST. Each slot is a separate two-hour broadcast.

| Language | Daily starts (JST) |
| --- | --- |
| English | 01:00, 03:00, 05:00, 07:00, 09:00, 11:00, 13:00 |
| Japanese | 15:00, 17:00, 19:00, 21:00, 23:00 |

`ravora-twitch-schedule plan` prints the next seven calendar days and next start.
The calendar contains 84 two-hour slots per week. Actual broadcast coverage is
measured separately. The five-minute market observer tracks
tagged Just Chatting streams in the slot language and samples overlap with vedal987.
The report records incomplete scans and uncovered time. The market observer uses the
operator's own Twitch application.

For opt-in live operation install the live studio extra, prepare the working directory
and authorize chat as described in the [Twitch studio guide](twitch-live.md).
Set `RAVORA_TWITCH_CHANNEL` to the authorized broadcaster login in the private studio
environment. Configure the studio's live send switches, operating budget and stream key
before running a slot. The scheduler uses `studio.toml` in that directory when present.

```bash
ravora-twitch-schedule authorize
ravora-twitch-schedule authorize-pin
ravora-twitch-schedule check
```

The first grant permits channel metadata updates. The optional second grant permits
posting and pinning the bilingual welcome to the confirmed broadcast. The studio checks
ownership and leaves an existing pin in place. A message send with an uncertain outcome
is recorded, and a pin with an uncertain outcome is reconciled by reading the provider
state. Each slot claims a durable database row before starting. Adjacent slots check
the preceding stream and publisher lock.

The [user timer examples](../examples/systemd/) use `~/ray-studio` and its installed
virtual environment. Use one slot timer instance for each two-digit start hour in the table.
The market timer samples at five-minute intervals. Installing the Python package leaves scheduling disabled.

Scheduled shows prepare one waiting comment while the preceding reply plays. A manual
studio launch enables this with `--prefetch`. The playout order stays serial. Deletion,
expiry or interruption can discard a prepared reply before it reaches the program.
With the scheduled `short-turn-v1` policy, Ray's own openings use one short sentence.
The studio checks the synthesized WAV and reverb tail against a three-second ceiling
before playout. An incoming comment can begin preparation while that line plays.
A comment that arrives during preparation can take the floor while an already dispatched
opening request finishes. Its provider call stays in the ledger. The operator's enabled
microphone takes priority on detected speech.

A fresh positive viewer sample allows an opening. Ray greets the room once when the first sample shows a single listener. With one silent listener, successive
waits are 120, 240, 480 and then 600 seconds at the default settings. A human turn resets
the interval. Zero or unavailable viewer counts keep the show on video and music until a
comment or operator speech arrives. Older sessions have an unrecorded speech policy.

Playback acknowledgement gates conversational memory. For operator-supplied music,
`bgm_tracks` is an ordered list of paths relative to the selected TOML file and
`bgm_volume_db` sets the shared mix level. [Mixer configuration](../examples/studio-nvenc.toml)
