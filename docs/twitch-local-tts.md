# Bilingual speech on the Twitch studio

The studio receives a comment, sends the text turn to Gemini, validates the returned JSON, and synthesizes the reply on the broadcast host. The same host mixes the PCM into the outgoing stream. A separate k3s observer reads Twitch's public player. The encoder and both GPU voices can run while the observer is offline. [Observer setup](#public-player-observer-on-k3s)

## Voice selection and first audio

| Turn | Language decision | Synthesis |
| --- | --- | --- |
| Viewer comment | Gemini returns the language of the viewer's own words and its confidence alongside the public reply. | Japanese uses AivisSpeech Engine 1.2.0, Kanon Normal. English uses Kokoro-82M v1.0. |
| Host reply | The same decision uses the conversation language as its fallback. | One passage, up to the worker's request limit. |
| Spontaneous speech | The scheduled show's language selects the voice. | One passage for a normal short turn. |

The threshold is inclusive at 0.80. For a lower score or an undetermined language, a viewer reply uses the existing text-language heuristic and its configured fallback. The controller records the reported language, score, threshold, decision and selected voice in the turn's `tts_route` metrics. Only validated speech reaches the synthesizer.

For an English viewer reply of at least 64 characters, the studio looks for a natural boundary near characters 20 through 35. With no suitable break it speaks the entire passage. With a break, the worker delivers the opening PCM to the mixer while preparing the tail. Tails over 165 characters can have intermediate segments. The final 24 kHz mono WAV contains the whole reply, and the mixer resumes at the consumed prefix. A failed tail cannot produce a successful complete WAV.

The English profile uses US English phonemes, 57.5% `af_heart`, 42.5% `jf_tebukuro`, and 3.5 dB gain with a soft peak ceiling. Set `RAVORA_TWITCH_EN_TTS=google` or `RAVORA_TWITCH_JA_TTS=google` to select the cloud voice for that language. Supply the corresponding Google credentials. A failed local voice request records a failure. The program does not automatically spend a second request on a cloud voice. See the [configuration example](../.env.example).

## GPU voice workers on Linux

Use a Linux x86-64 host with Docker, the NVIDIA Container Toolkit, a compatible GPU driver, and CUDA 12 / cuDNN 9 runtime libraries. The [launcher](../scripts/run_local_tts.sh) starts each worker as the studio user, with no container network, a read-only root filesystem and a private Unix socket. In the user-service examples the checkout is at `$HOME/ray-studio`. Install the studio from that checkout as described in the [studio guide](youtube-live.md).

Prepare these assets under `state/tts` in the checkout or an absolute directory selected with `RAY_TTS_ASSETS` in `~/ray-studio/tts.env`. Model assets are obtained separately under their respective terms.

| Location under the asset directory | Contents |
| --- | --- |
| `cuda/` | Compatible Linux CUDA libraries including cuBLAS and cuDNN, with their loader names and dependencies. The NVIDIA container runtime supplies the host driver. |
| `kanon/engine/` | Extracted AivisSpeech Engine 1.2.0 Linux x64 distribution, including `run`, `engine_internal/` and `resources/`. [Engine releases](https://github.com/Aivis-Project/AivisSpeech-Engine/releases) |
| `kanon/profile/` | Dedicated, writable engine home with Kanon 1.0.0 installed under `.local/share/AivisSpeech-Engine/Models` and the engine's BERT cache populated under `BertModelCaches`. Provision the engine and model before launching the offline worker. The worker checks speaker identity and Normal style ID `1325133120`. [Kanon model](https://hub.aivis-project.com/aivm-models/a670e6b8-0852-45b2-8704-1bc9862f2fe6) |
| `kokoro/` | `model.onnx` (FP32), `tokenizer.json`, `af_heart.bin` and `jf_tebukuro.bin`, stored together. Check each file against the digests in the [asset manifest](../examples/tts/kokoro-assets.json). [Pinned ONNX snapshot](https://huggingface.co/onnx-community/Kokoro-82M-v1.0-ONNX/tree/1939ad2a8e416c0acfeecc08a694d14ef25f2231) |

From the checkout, prepare the two local images and install the example services for the studio user.

```bash
docker pull python:3.14.4-slim
docker build -t ray-kokoro-runtime:local-v1 examples/tts
install -d -m 700 ~/.config/systemd/user
install -m 644 examples/systemd/ray-{kanon,kokoro}-tts.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ray-kanon-tts.service ray-kokoro-tts.service
curl --fail --unix-socket "$XDG_RUNTIME_DIR/ray-kokoro/kokoro.sock" http://localhost/health
curl --fail --unix-socket "$XDG_RUNTIME_DIR/ray-aivis/kanon.sock" http://localhost/version
```

Kokoro performs a synthesis and checks the ONNX Runtime profile for CUDA compute kernels before opening its socket. The CUDA allocator has a 4.25 GiB ceiling. Four English replies previously failed at the 3 GiB setting when a convolution requested about 1.08 GiB and the allocator had 109 MiB available. After restarting only the English worker with the higher ceiling, four short synthetic replies returned complete audio. The restart also cleared the allocator, so this check does not establish how it behaves after a long broadcast. In a finite GTX 1070 segmentation trial, peak observed process VRAM was 2,390 MiB. In a separate Kanon test, the maximum observed across the reported runs was 1,412 MiB. These peaks came from different observation windows. The launcher sets host RAM limits of 4 GiB for Kokoro and 5 GiB for Kanon.

Before opening the publisher, the studio requests short readiness phrases from the selected local voices and checks for non-silent audio. It discards the audio locally. A voice that is unavailable during that check is logged as deferred so the broadcast can still open. Run the [studio configuration check](twitch-live.md#select-the-h264-encoder-and-start) to inspect provider choices separately from worker readiness.

## Observed response times

On November 27, 2026 (JST), four individual Twitch viewer replies completed playback on the studio program. Times below are measured from studio receipt to its first non-silent Ray audio in the outgoing program. The middle columns split the interval at the point where the studio prepared the TTS call.

| Voice | Reply characters | Receipt to TTS preparation | Preparation to first signal | Receipt to first signal |
| --- | ---: | ---: | ---: | ---: |
| Kanon | 50 | 1.202 s | 1.103 s | 2.305 s |
| Kokoro | 85 | 1.356 s | 0.528 s | 1.884 s |
| Kokoro | 83 | 1.958 s | 0.753 s | 2.710 s |
| Kanon | 29 | 1.136 s | 0.839 s | 1.976 s |

Mean time from receipt was 2.219 s. Starting at the Twitch message timestamp gives a mean of 2.345 s. The measurements end at the studio mix. The viewing device's playback time was not captured.

The historical comparison uses November 25 through 27. Both groups used the same Gemini text model and speech policy, individual replies, a single accepted TTS attempt, program audio and playback acknowledgement. Prefetched replies were excluded. These were different comments, with two local observations per language.

| Language and provider | Replies | Mean receipt to first program signal | Mean TTS preparation to first program signal |
| --- | ---: | ---: | ---: |
| English, Google Chirp 3 HD | 29 | 2.154 s | 0.891 s |
| English, Kokoro | 2 | 2.297 s | 0.640 s |
| Japanese, Google Chirp 3 HD | 4 | 2.232 s | 0.792 s |
| Japanese, Kanon | 2 | 2.140 s | 0.971 s |

In a separate isolated test, a 99-character English reply reached the mixer in a median 803 ms with whole-passage synthesis and 325 ms with a 19-character lead. The complete WAV took 738 and 760 ms respectively (three runs per case). A 188-character reply with a 35-character lead reached the mixer in 401 ms, compared with 1,113 ms for whole-passage synthesis, also three runs each. These timed runs started at synthesis and used an earlier voice configuration.

An earlier isolated Japanese comparison timed synthetic comment receipt through the program signal and WAV completion. Gemini generated two-sentence replies in all conditions. Each row gives the median. The GPU and API trials used different generated replies and ran at different times.

| Japanese TTS and delivery | Runs | First program signal | Complete WAV |
| --- | ---: | ---: | ---: |
| Aivis on GTX 1070, start synthesis on the first sentence | 6 | 1.438 s | 1.851 s |
| Google Chirp, start synthesis on the first sentence | 18 | 1.549 s | 2.376 s |
| Google Chirp, send the validated reply together | 18 | 1.646 s | 2.296 s |
| Google Chirp, split after validating the reply | 18 | 1.731 s | 2.583 s |

The early-synthesis experiment held the audio until the complete JSON passed validation. It measured a candidate design. The current Japanese studio worker receives a validated utterance.

## CPU synthesis on the broadcast host

With a two-core budget on a Xeon E5-2695 v4, the GTX 1070 returned a complete WAV sooner for all five passages. Each cell gives the warm median for one fixed passage. The GPU reference for each English passage is a single observation.

| Voice | Characters | CPU runs | CPU complete WAV | GPU runs | GTX 1070 complete WAV |
| --- | ---: | ---: | ---: | ---: | ---: |
| Kokoro | 28 | 3 | 1.682 s | 1 | 0.375 s |
| Kokoro | 54 | 3 | 2.632 s | 1 | 0.424 s |
| Kokoro | 45 | 3 | 2.361 s | 1 | 0.346 s |
| Kokoro | 32 | 3 | 1.832 s | 1 | 0.327 s |
| Kanon | 50 | 3 | 8.286 s | 5 | 0.636 s |

The CPU trials ran on November 27, 2026 (JST) inside separate containers with a two-CPU quota, two logical CPUs on different physical cores and no GPU device. Kokoro ran the production English blend with the ONNX Runtime CPU provider. Kanon ran AivisSpeech Engine 1.2.0 in CPU mode. Each worker synthesized a warm-up phrase before timing began. Each timed request produced non-silent audio.

Timing starts before the first local HTTP request and ends when the client receives the entire 24 kHz mono WAV. Kanon includes both the text query and synthesis. CPU clients and the Kokoro GPU client used Unix sockets. The earlier Kanon GPU client used container loopback. CPU and GPU runs occurred at different times. Comment receipt, text generation, mixing and viewer playback are outside this interval. [Synthetic passages and per-run timings](../examples/tts/cpu-timings.json)

## Public player observer on k3s

The [manifest](../deploy/k3s/ray-monitor/monitor.yaml) defines a namespace, a retained local-path storage class, a 1 GiB claim, and one non-root observer replica with the Recreate strategy. The observer uses HTTPS egress to Twitch, a read-only root filesystem, temporary storage and a writable SQLite history volume. It has no service account token, stream key, GPU or publisher control. Set `TWITCH_CHANNEL` in the manifest to the public login to observe.

From the checkout on a k3s node equipped with Docker and the local-path provisioner, build and import the [observer image](../deploy/k3s/ray-monitor/Dockerfile). For a multi-node cluster, import the same image on every eligible node or constrain placement to the prepared node. Use a new image tag in both the build and manifest for each revision.

```bash
docker build -t ray-ravora-observer:local-v1 deploy/k3s/ray-monitor
docker save ray-ravora-observer:local-v1 | sudo k3s ctr images import -
sudo k3s kubectl apply -f deploy/k3s/ray-monitor/monitor.yaml
sudo k3s kubectl -n ray-monitoring rollout status deploy/twitch-observer
sudo k3s kubectl -n ray-monitoring logs deploy/twitch-observer --tail=10
```

Readiness and liveness probes check the observer heartbeat; Twitch availability is recorded separately in SQLite. Every 30 seconds the observer checks the public stream identity. When live it normally decodes the last complete HLS segment every 60 seconds, checking timestamps for both audio and video and comparing the segment digest to the previous sample. Missing observations remain unknown. The history stores status, counts, timestamps and segment digests. It can show that media is advancing, but cannot identify the onset of a particular reply on a viewer's device. To pause observation while retaining history, scale the deployment to zero. Scale back to one to resume.
