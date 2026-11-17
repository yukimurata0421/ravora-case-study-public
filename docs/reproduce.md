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

The provisional-reaction replay uses the same controller, the packaged version-10
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
