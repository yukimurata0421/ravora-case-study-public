# Why Ray prioritizes Twitch for live conversation

Accepted on 2026-11-24 JST for Ray's interactive live shows.

Ray will use Twitch as the primary venue for live conversation. A viewer who
comments on what Ray just said should hear the reply while the topic is still
current. On YouTube, the time Ray could start answering depended in part on
when the viewer posted relative to the studio's next chat connection. The
plan for YouTube is to use short clips to help new viewers discover Ray.

## What happened on YouTube

Two comments reached the studio in the same batch. One was six seconds old
and Ray answered it. The other was already eight minutes old. Across the six
recorded comments, three arrived past the studio's 45-second cutoff for
selecting a comment to answer.

In a later diagnostic window, eight gRPC `streamList` connections each
delivered one batch and closed normally after an average of 10.651 seconds.
The studio usually waited five seconds to reconnect. Its local limit of 40
starts per hour added waits of 47–55 seconds on five occasions. The reason
the connections closed so soon is unknown. None returned a provider quota
error. [YouTube chat receipts and connection timings](../youtube-live-operations-20261124.md)

## What a larger chat budget buys

The planning assumption was one two-hour show per day. The revised studio
allows 240 starts per hour. At five units per `streamList` call, 480 starts
would use 2,400 of the 4,800 units budgeted locally per day, before other API
calls. Even at that rate, starts are at least 15 seconds apart. If a
connection again lasts 10.651 seconds, the spacing alone leaves about 4.35
seconds before the next start (`15 − 10.651`). Retry waits can extend it.
[Chat budget calculation and implementation tests](../youtube-live-operations-20261124.md#why-the-chat-budget-changed)

A viewer posting just after the connection closes can wait several seconds
before Ray even sees the comment. A viewer posting on an open connection
can skip that wait.
This recurring source of variation was enough to prioritize Twitch over
further YouTube tuning. The 4–5 seconds describe an estimated gap in chat
intake. How often viewers encountered it under the revised budget is unknown.

## What Twitch showed and when to revisit

An open Twitch EventSub subscription lets the studio receive chat as Twitch
sends it, without scheduling the next chat request. For one test comment,
the studio received the event in 0.209 seconds, and Ray's reply became
audible in the watch player about 4.44 seconds after the event. Both
intervals start at the Twitch event timestamp.
[Watch-player measurement and timing breakdown](../twitch-live-operations-20261124.md#what-the-viewer-received)

The long-run p95 from chat event to audible reply remains unmeasured on both
platforms. Revisit the choice if two-hour YouTube sessions under the revised
budget show timely replies through reconnects. A comparison would use the
same event-to-player interval on both platforms and count expired comments
separately, since Ray never answered them.
