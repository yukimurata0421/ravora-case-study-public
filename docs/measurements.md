# Measurements and evidence boundaries

These are aggregate results from development studies on 2026-11-16 and
2026-11-17 JST.
Raw recordings, operational logs, host identity, and the conversation database
are excluded. They are not measurements newly run by the public replay.

## Direct local orchestration trial

| Delivery profile | Trials | Mean input-send completion to browser audio signal |
| --- | --- | --- |
| Legacy voice, sentence input | 3 | 2012.7 ms |
| HD voice, sentence input | 3 | 2427.4 ms |
| HD voice, bidirectional text input | 3 | 1740.7 ms |

Input was a 2.476-second fixture paced in real time. Recognition used a Singapore
endpoint, text generation used a global endpoint, and synthesis used a Tokyo
endpoint. Tokyo recognition requests were rejected in that experiment; the region
change was explicit. This was not an all-Tokyo deployment.

Channels were established before the turns. The interval starts after the last
input chunk is sent and therefore excludes the duration of the spoken input and
channel startup. It ends at receipt of a signal notification from the browser's
audio render thread. Detection used a 10 ms RMS window at -48 dBFS.

The HD streaming profile was faster in these small trials. Text generation was
not fixed across all trials, and voice tiers differ. These observations are not
a provider-wide benchmark or a statistical guarantee. The direct trial created
no VM or cloud relay service.

## Normal conversation console

A separate trial used a fake microphone through the normal console and real API
adapters. One audio-input turn reached a browser audio signal 2158.2 ms after the
flushed recording ended. One text-input turn took 1141.3 ms from submission.
Both completed playback with matching PCM frame counts and zero underflow.

There was one sample per input type, seven external inference requests in total,
and 157 synthesized characters. This supports end-to-end wiring with those
adapters; it does not establish human microphone accuracy or physical speaker output.

## Memory and public replay

The development memory change passed 190 tests and a mock browser run. Schema
migration preserved the prior 1042 rows across 20 existing tables. Its checks made
no external API requests. These historical checks belong to the development tree.

The public subset has its own test count and package validation recorded in
[publication/validation.json](../publication/validation.json). Its replay uses
mock text, synthetic audio, and simulated playback acknowledgements. Browser
validation of the public console is reported separately from the CLI replay.

## Remaining uncertainty

Live semantic answer quality under the tier policy, physical listening, avatar
synchronization with the fast profile, receiver AV, and long-term coexistence
remain outside these results. Earlier production observations that changed
between experimental runs are not rewritten as an all-clear result.

## Later short-reply and provisional-reaction trials

These runtime trials took place on 2026-11-17 JST. They are historical
observations, not new measurements made while updating this portable repository.
The curated data is in
[measurement-summary.json](../publication/measurement-summary.json).

Six short-reply examples produced one sentence each, 4--8 English words, with
reply clips lasting 1.60--2.84 seconds. A later four-turn voice recording reached
audible replies 2.04--2.61 seconds after the question's last non-silent window.
All four transcript notifications arrived after those short inputs ended.

An isolated recognition comparison then used the same 2.25-second input once per
profile. The earlier Singapore profile returned its first notification 3082.91 ms
after sending began, with no update before input completion. The selected Tokyo
`long` profile returned the first notification at 535.41 ms and supplied 13
notifications before completion. Regional metadata and actual requests were
checked again; the earlier rejected Tokyo experiment remains historical evidence,
not a claim that every later profile is unavailable there.

The later continuous recording used five prerecorded synthetic questions and one
reaction request for each input. The first four timing columns below use the
first non-silent question window as zero. The last column uses the final
non-silent question window as zero.

| Input | First transcript | Reaction notification | Reaction | First expression render at weight >=5% | End of question to reply audio |
| --- | ---: | ---: | --- | ---: | ---: |
| Longer positive input | 0.62 s | 1.62 s | Smile | 1.78 s | 1.93 s |
| Greeting | 0.50 s | 1.82 s | Smile | 2.01 s | 1.40 s |
| Game loss | 0.42 s | 1.30 s | Neutral | Not applicable | 1.38 s |
| Preference | 0.35 s | 1.54 s | Neutral | Not applicable | 1.84 s |
| Recall | 0.42 s | 1.04 s | Neutral | Not applicable | 1.34 s |

All five reaction notifications preceded question completion. The two smile
renders preceded completion by 7.62 s and 0.10 s respectively. A neutral result
means the existing neutral face was retained; it is not an additional visible
expression change. The 5% trace threshold is not a human-perception calibration.

The recording used a shared browser audio clock. Reply onset was detected by an
AudioWorklet using a -48 dBFS, 10 ms window. Source rendering was capped at 15 fps;
the recording container was 30 fps. All five replies completed playback with
matching frame counts and zero underflow. Comparing the saved mixed PCM with the
decoded recording gave an envelope correlation above 0.99999 and the best lag at
zero on a 10 ms search grid. This verifies recording consistency, not physical
speaker output or exact phoneme-to-mouth alignment.

The v13 utterance-level mouth change was included. Three internal quiet speaking
frames retained expression-mouth weight zero. Their expression was neutral;
smile plus a long internal pause was not observed in that live recording.

The before/after recordings had different input sets and generated reply text,
and synthesis paths differ from the earlier sentence trial. Their numbers must
not be combined into one controlled speedup percentage. The latest recall used
conversation history; it did not exercise live long-term memory extraction.

The runtime's 223 passing tests are separate from the portable test count in the
[current local validation](../publication/validation.json). Updating this port
made no new inference requests and includes no original recording or database.

## Relationship comparison and later six-turn voice trial

The [later evaluation record](live-evaluation-20261117.md) separates the final
forty-response relationship comparison from a six-turn audible-question recording.
It reports unresolved response-quality defects as well as the 1.73--2.22 s
speech-end-to-audio observations, two visible listening reactions, and an
independent decoded-audio check. The previous five-turn dataset above is preserved;
the datasets are not combined into a controlled speedup claim.
