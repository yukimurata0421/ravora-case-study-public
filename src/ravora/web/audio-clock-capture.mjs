export async function createAudioClockCapture(canvas, profile, onError, write) {
  const config = {
    codec: 'avc1.420028',
    width: canvas.width,
    height: canvas.height,
    bitrate: profile.bitrate,
    framerate: profile.fps,
    latencyMode: 'realtime',
    hardwareAcceleration: 'prefer-software',
    avc: {format: 'annexb'},
  };
  const support = await VideoEncoder.isConfigSupported(config);
  if (!support.supported) throw Error('AVC_1080P30_UNAVAILABLE');
  const packets = [];
  const ticks = [];
  let bytes = 0, queueHighWater = 0, queueDrops = 0;
  let lastKey = -profile.fps * 2;
  let armed = false;
  const encoder = new VideoEncoder({
    output(chunk) {
      bytes += chunk.byteLength;
      if (bytes > profile.maxBytes) {
        onError(Error('RECORDING_LIMIT'));
        return;
      }
      const data = new Uint8Array(chunk.byteLength);
      chunk.copyTo(data);
      const tick = Math.round(chunk.timestamp * profile.fps / 1e6);
      if (armed && write) write(data, tick);
      else packets.push(data);
      ticks.push(tick);
    },
    error: onError,
  });
  encoder.configure(config);

  return {
    draw(tick) {
      queueHighWater = Math.max(queueHighWater, encoder.encodeQueueSize);
      if (encoder.encodeQueueSize >= 6) {
        queueDrops++;
        return;
      }
      const timestamp = Math.round(tick * 1e6 / profile.fps);
      const frame = new VideoFrame(canvas, {
        timestamp,
        duration: Math.round((tick + 1) * 1e6 / profile.fps) - timestamp,
      });
      try {
        const keyFrame = tick - lastKey >= profile.fps * 2;
        encoder.encode(frame, {keyFrame});
        if (keyFrame) lastKey = tick;
      } finally {
        frame.close();
      }
    },
    async resetAfterWarmup() {
      await encoder.flush();
      packets.length = 0;
      ticks.length = 0;
      bytes = 0;
      lastKey = -profile.fps * 2;
      queueHighWater = 0;
      queueDrops = 0;
      armed = true;
    },
    async finish() {
      await encoder.flush();
      encoder.close();
      return {
        blob: new Blob(packets, {type: 'video/h264'}),
        summary: {ticks, bytes, queue_high_water: queueHighWater, queue_drops: queueDrops, codec: config.codec},
      };
    },
  };
}
