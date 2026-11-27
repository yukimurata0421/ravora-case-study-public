FROM python:3.11-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*
COPY kokoro-assets.json /tmp/kokoro-assets.json
# kokoro-onnx normally selects the CPU runtime. Install its pinned dependencies
# explicitly so that only the GPU build supplies the onnxruntime namespace.
RUN python -c 'import json; print("\n".join(k+"=="+v for k,v in json.load(open("/tmp/kokoro-assets.json"))["dependencies"].items()))' > /tmp/runtime.txt \
    && pip install --no-cache-dir --no-deps -r /tmp/runtime.txt \
    && pip install --no-cache-dir coloredlogs flatbuffers protobuf sympy humanfriendly mpmath \
    && python -c 'from kokoro_onnx import Kokoro; import onnxruntime as ort; assert "CUDAExecutionProvider" in ort.get_available_providers()'
