# Reproduce the portable slice

## Checkout-only replay

```bash
python3 -B scripts/demo.py replay
python3 -B scripts/demo.py early-reaction
python3 -B scripts/demo.py relationships
python3 -B scripts/demo.py media-timing
PYTHONPATH=src python3 -B -m unittest discover -s tests -v
python3 -B scripts/validate_public.py
```

No account, API key, model weights, audio tool, GPU, or host service is required.
SQLite must provide FTS5 and JSON functions. Python's standard-library SQLite
build normally supplies them; schema initialization reports an error if they
are unavailable.

The replay creates its own private temporary directory. Its database and waveform
fixtures are removed when it exits. `--report <new-file.json>` writes only the
sanitized synthetic result and refuses to overwrite an existing file.

## Installed package

```bash
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/ravora-case-study replay
.venv/bin/ravora-case-study early-reaction
.venv/bin/ravora-case-study relationships
.venv/bin/ravora-case-study media-timing
.venv/bin/python -m unittest discover -s tests -v
```

Schema files are packaged with the module, so replay does not depend on a source
checkout. The validation record also covers running the installed wheel from an
unrelated working directory.

The provisional-reaction replay uses the same controller, the packaged version-13
schema, low-stability synthetic transcript updates, and the two stage reservations.
Its waits advance a fixture; no latency result is claimed. The
packaged character presets are checked by the request-assembly tests.

Run `node --test tests/check_media.mjs` to check the browser modules with synthetic
phonemes, an interleaved mesh, and a stub encoder. The optional native Python
alignment test uses a locally synthesized utterance and requires the
`phone-alignment` extra, FFmpeg, and the local speech library.

## Optional browser console

```bash
.venv/bin/python -m pip install '.[console]'
.venv/bin/ravora-case-study serve --seconds 120
```

Use the printed URL in a local browser. The server uses the requested HTTP port
and the adjacent port for its authenticated WebSocket. It accepts text without
microphone permission. Holding the microphone button produces a canned transcript
and test-tone reply; it does not recognize the actual recording.

The demo has no cloud mode and ignores inherited provider keys. The reference
adapter modules retain explicit configuration gates for inspection and separate
integration work. Installing the optional SDK group alone does not activate an API.

The runtime expires after the requested period, or exits on Ctrl-C. Browser stop
and disconnect cancel the current turn. Session files are temporary. A process
crash or forced kill can leave a temporary directory; normal-exit cleanup should
not be presented as a guarantee after an uncatchable termination.

## Validation limits

The included workflow runs offline CI checks. Inspect the repository's Actions tab for remote
outcomes; workflow configuration alone is not a passed CI result. The dated local
validation record names the interpreter and checks executed at the archive stage.
Do not substitute the runtime's historical test count for the portable subset's
result.

## Route the microphone and playback

The mock console defaults to browser input and playback. Hold Space or the talk
button for manual turns. Automatic mode sends a turn after a pause. Hands-free
mode also waits for speech before starting a turn and listens again after playback
or an empty transcript. Stop or hide the tab to leave hands-free mode. The mock
recognizer still returns a fixture. Its turn-taking controls work with real PCM.

For a Linux capture host and a separate playback browser, give the console a
TOML file using `ravora-case-study serve --config voice.toml`. One configuration is shown below.

```toml
[developer_voice]
input_source = "dell"    # host ALSA capture; "windows" selects the browser
dell_device = "USB,0"    # replace with the card ID and PCM number from arecord -l
end_mode = "handsfree"   # manual, auto, or handsfree
end_silence_ms = 800
session_seconds = 7200
port = 8767
```

From the playback machine, forward both loopback ports over SSH and run the
console on the capture host. Replace the SSH alias and config path with your own.

```powershell
ssh -t -o ExitOnForwardFailure=yes -L 127.0.0.1:8767:127.0.0.1:8767 -L 127.0.0.1:8768:127.0.0.1:8768 voice-host "ravora-case-study serve --config voice.toml"
```

Open the printed launch URL in the playback browser. A reload reuses the tab's
session token. A second connected tab receives a conflict. The two-hour limit
is measured from launch. Browser-only use needs no ALSA device or SSH.

The fullscreen stage has a 1920 × 1080 drawing surface and a synthetic face.
To use registered artwork, install the `avatar` extra and add a `[pngtuber]`
section to the same TOML. Set `manifest` to a path under `assets/pngtuber/`
relative to the TOML directory. One configuration is shown below.

```toml
[pngtuber]
manifest = "assets/pngtuber/manifest.json"
open_threshold_db = -32
close_threshold_db = -42
hold_ms = 80
blink_interval_ms = 3800
blink_duration_ms = 150
```

The minimal manifest has an `images` object with `idle` and `talking` entries.
Each entry supplies a relative PNG `path` and its hex `sha256`. Optional blink
poses, a registered rig and expression-vowel patches use the same binding.
The console validates the asset root, file hashes, dimensions and geometry
before serving these files. [Avatar binding](../src/ravora/pngtuber.py)

With registered expression-vowel artwork, a manifest can also supply
`upper_lip_layers.happy`. List the layers in increasing `opening` order from
0 to 1. For each layer provide its RGBA PNG path and SHA-256, its pixel
`offset` on the idle portrait, and the `outer` and `inner` lip edges as 31
pairs of crop-local pixel coordinates each. Hardware WebGL2 blends these
layers during a spoken smile. The 2D fallback uses the talking and blinking
sprites and overlays any registered facial expressions. The built-in synthetic
face still works without artwork. [Reference smile and comparison](audio-timing.md#november-21-conversational-smile)
