#!/usr/bin/env bash
# Start one network-isolated GPU voice worker for the studio user.
set -euo pipefail
umask 077
root=$(cd -- "$(dirname -- "$0")/.." && pwd -P)
assets=${RAY_TTS_ASSETS:-$root/state/tts}
runtime=${XDG_RUNTIME_DIR:?Start as the studio user}
cuda=$assets/cuda
[[ -r "$cuda/libcudnn.so.9" && -r "$cuda/libcublasLt.so.12" ]] || { echo 'Missing CUDA 12 / cuDNN 9 runtime libraries' >&2; exit 1; }
uid=$(id -u)
gid=$(id -g)
case "${1:-}" in
  kanon)
    socket_dir=$runtime/ray-aivis
    mem=5g
    pids=192
    image=${RAY_KANON_IMAGE:-python:3.14.4-slim}
    server=aivis_socket.py
    [[ -x "$assets/kanon/engine/run" && -d "$assets/kanon/profile/.local/share/AivisSpeech-Engine/Models" ]] || { echo 'Missing Kanon engine or model directory' >&2; exit 1; }
    options=(-v "$assets/kanon/engine:/engine:ro" -v "$assets/kanon/profile:/labhome:rw"
             --tmpfs "/labhome/.local/share/AivisSpeech-Engine/Logs:rw,nosuid,nodev,noexec,size=1m,uid=$uid,gid=$gid,mode=0700"
             -w /engine -e HOME=/labhome -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1)
    ;;
  kokoro)
    socket_dir=$runtime/ray-kokoro
    mem=4g
    pids=96
    image=${RAY_KOKORO_IMAGE:-ray-kokoro-runtime:local-v1}
    server=kokoro_socket.py
    for file in model.onnx tokenizer.json af_heart.bin jf_tebukuro.bin; do
      [[ -r "$assets/kokoro/$file" ]] || { echo "Missing Kokoro asset: $file" >&2; exit 1; }
    done
    options=(-v "$assets/kokoro:/model:ro" -e HOME=/tmp
             --tmpfs /phonemes:rw,exec,nosuid,nodev,size=16m -e TMPDIR=/phonemes)
    ;;
  *) echo 'Usage: run_local_tts.sh kanon|kokoro' >&2; exit 2 ;;
esac
install -d -m 0700 "$socket_dir"
exec docker run --rm --init --pull never --name "ray-$1-tts" --gpus "device=${RAY_TTS_GPU:-0}" \
  --network none --read-only --cap-drop ALL --security-opt no-new-privileges --user "$uid:$gid" \
  --cpus 2 --memory "$mem" --memory-swap "$mem" --pids-limit "$pids" --shm-size 32m \
  --tmpfs /tmp:rw,nosuid,nodev,size=256m --log-driver none \
  -e NVIDIA_DRIVER_CAPABILITIES=compute,utility -e CUDA_VISIBLE_DEVICES=0 \
  -e OMP_NUM_THREADS=2 -e MKL_NUM_THREADS=2 -e OPENBLAS_NUM_THREADS=1 \
  -e LD_LIBRARY_PATH=/cuda:/usr/local/nvidia/lib64:/usr/local/nvidia/lib \
  -v "$cuda:/cuda:ro" -v "$socket_dir:/socket:rw" -v "$root/scripts/$server:/app/serve.py:ro" \
  "${options[@]}" "$image" python3 -B /app/serve.py
