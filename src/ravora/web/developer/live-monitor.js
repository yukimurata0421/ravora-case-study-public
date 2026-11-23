// The server's 30 Hz playout clock and WAVs both refer to the same 48 kHz signal.
const RATE = 48000, FPS = 30, BUFFER_SECONDS = 0.5;

export class ReplyMonitor {
  constructor(context, headers) {
    this.context = context;
    this.headers = headers;
    this.abort = new AbortController();
    this.cursor = null;
    this.generation = -1;
    this.sources = new Set();
    this.nextTime = 0;
    this.endFrame = null;
  }

  flush() {
    for (const source of this.sources) source.stop();
    this.sources.clear();
    this.nextTime = 0;
    this.endFrame = null;
  }

  invalidate(generation) {
    if (generation > this.generation) {
      this.flush();
      this.generation = generation;
    }
  }

  stop() {
    this.abort.abort();
    clearTimeout(this.timer);
    this.flush();
  }

  async poll() {
    let delay = 100;
    try {
      if (this.context.state !== 'running') {
        this.flush();
        this.cursor = null;
        return;
      }
      const headers = {...this.headers};
      if (this.cursor !== null) headers['X-Ray-After'] = String(this.cursor);
      const response = await fetch('/v1/monitor/next', {headers,
        signal: AbortSignal.any([this.abort.signal, AbortSignal.timeout(8000)])});
      if (!response.ok) return;
      const generation = Number(response.headers.get('X-Ray-Generation'));
      if (generation < this.generation) return;
      this.invalidate(generation);
      const join = response.headers.get('X-Ray-Join') === '1';
      if (join) this.flush();
      const frame = Number(response.headers.get('X-Ray-Frame'));
      const offset = Number(response.headers.get('X-Ray-Offset-Samples')) / RATE;
      const wav = await response.arrayBuffer();
      if (wav.byteLength) {
        const buffer = await this.context.decodeAudioData(wav);
        if (this.abort.signal.aborted || generation !== this.generation) return;
        if (offset < buffer.duration) this.play(buffer, frame, offset);
        delay = 0; // Fetch the next sequence immediately when catching up.
      }
      if (generation === this.generation) this.cursor = Number(response.headers.get('X-Ray-Sequence'));
    } catch {
      // Keep the last successfully scheduled sequence across fetch and decode failures.
      delay = 100;
    } finally {
      if (!this.abort.signal.aborted) this.timer = setTimeout(() => this.poll(), delay);
    }
  }

  play(buffer, frame, offset) {
    const now = this.context.currentTime;
    const gap = this.endFrame === null ? 0 : Math.max(0, frame - this.endFrame) / FPS;
    let start = this.nextTime + gap;
    if (this.endFrame === null || start <= now || !this.sources.size) start = now + BUFFER_SECONDS;
    const source = this.context.createBufferSource();
    source.buffer = buffer;
    source.connect(this.context.destination);
    source.onended = () => { this.sources.delete(source); source.disconnect(); };
    source.start(start, offset);
    this.sources.add(source);
    this.nextTime = start + buffer.duration - offset;
    this.endFrame = frame + Math.ceil(buffer.length * FPS / buffer.sampleRate);
  }
}
