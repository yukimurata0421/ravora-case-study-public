// The AudioContext sets the sample rate for its microphone transport.
class RavoraMicStream extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const chunkMs = options.processorOptions.chunk_ms ?? 100;
    this.chunk = new Int16Array(Math.round(sampleRate * chunkMs / 1000));
    this.used = 0;
    this.stopped = false;
    this.port.onmessage = ({data}) => {
      if (data.kind === 'stop' && !this.stopped) {
        this.stopped = true;
        this.flush(currentTime);
        this.port.postMessage({kind: 'done'});
      }
    };
  }

  flush(endTime) {
    if (!this.used) return;
    const samples = this.chunk.slice(0, this.used);
    this.port.postMessage({kind: 'pcm', samples, endTime}, [samples.buffer]);
    this.used = 0;
  }

  process(inputs) {
    if (this.stopped) return false;
    const channel = inputs[0]?.[0];
    if (!channel) return true;
    for (let i = 0; i < channel.length; i++) {
      const sample = Math.min(1, Math.max(-1, channel[i]));
      this.chunk[this.used++] = Math.round(sample * (sample < 0 ? 32768 : 32767));
      if (this.used === this.chunk.length) this.flush((currentFrame + i + 1) / sampleRate);
    }
    return true;
  }
}
registerProcessor('ravora-mic-stream', RavoraMicStream);
