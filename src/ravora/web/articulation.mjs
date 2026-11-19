import {sampleMouthPlan} from './mouth-plan.mjs';

const clamp = (x, low, high) => Math.min(high, Math.max(low, x));

// The mouth plan gives geometry; the envelope comes from the reply PCM itself.
// Both are sampled relative to the worklet's first consumed sample.
// Lips cannot open or close within one 33 ms frame. The reply PCM and plan are known ahead, so
// the opening and the vowel weights are averaged over +-50 ms (triangular) around the time. A
// 10 ms loudness dip or onset no longer snaps the mouth shut or wide open; a full m/b/p closure
// (>= ~80 ms) still closes.
const SMOOTH = [-.0375, -.025, -.0125, 0, .0125, .025, .0375];
export function alignedMouth(track, seconds, sampleRate) {
  if (seconds * sampleRate < 0 || seconds * sampleRate >= track.frames) return {open: 0, vowels: null, shape: 'closed', db: -100};
  let open = 0, weights = 0, db = -100, shape = 'closed', best = -1;
  const vowels = {};
  for (const offset of SMOOTH) {
    const w = 4 - Math.abs(offset) / .0125, raw = rawMouth(track, seconds + offset, sampleRate);
    weights += w;
    open += w * raw.open;
    if (offset === 0) db = raw.db;
    for (const [v, x] of Object.entries(raw.vowels || {})) if (v !== 'amount') vowels[v] = (vowels[v] || 0) + w * raw.open * x;
  }
  open /= weights;
  let total = 0;
  for (const v in vowels) total += vowels[v];
  if (!(open > 0) || !(total > 0)) return {open: 0, vowels: null, shape: 'closed', db};
  for (const v in vowels) { vowels[v] /= total; if (vowels[v] > best) { best = vowels[v]; shape = v; } }
  return {open, vowels: {...vowels, amount: open}, shape, db};
}

function rawMouth(track, seconds, sampleRate) {
  const position = seconds * sampleRate;
  if (position < 0 || position >= track.frames) return {open: 0, vowels: null, shape: 'closed', db: -100};
  const key = sampleMouthPlan(track.plan, seconds);
  const frame = clamp(position / track.hop_frames - .5, 0, track.db.length - 1);
  const i = Math.floor(frame);
  const db = track.db[i] + (track.db[Math.min(i + 1, track.db.length - 1)] - track.db[i]) * (frame - i);
  const emphasis = clamp(1 + (db - track.reference_db) / 44, .40, 1.21);
  const audibility = clamp((db + 57) / 19, 0, 1);
  const amount = clamp(key.amount * emphasis * audibility, 0, .88);
  if (!key.vowels) return {open: 0, vowels: null, shape: key.shape, db};
  return {open: amount, vowels: {...key.vowels, amount}, shape: key.shape, db};
}

// Mouth from the reply PCM as it arrives, before any phone timing exists. The browser receives the
// streamed reply ahead of the playhead, so loudness and a coarse vowel colour are measured on 10 ms
// hops of the received samples and averaged around the playhead like the planned mouth. This
// replaces the per-frame analyser of the played signal, which flapped open and shut at speech start.
const HOP = 240;
// Openings scaled to the planned mouth (measured on recorded replies: the PCM mouth averaged 0.45
// against the plan's 0.35). The coarse band measure guesses rounded vowels poorly (22-37% agreement
// with the phone plan) and a wrong pucker is the most visible error, so it only picks unrounded
// shapes; rounding comes with the phone plan.
const SHAPE_AMOUNT = {A: .48, E: .39, I: .33};
function bandpass(sampleRate, low, high) {
  const f0 = Math.sqrt(low * high), q = f0 / (high - low), w = 2 * Math.PI * f0 / sampleRate;
  const alpha = Math.sin(w) / (2 * q), a0 = 1 + alpha;
  const b0 = alpha / a0, b2 = -alpha / a0, a1 = -2 * Math.cos(w) / a0, a2 = (1 - alpha) / a0;
  let x1 = 0, x2 = 0, y1 = 0, y2 = 0;
  return x => { const y = b0 * x + b2 * x2 - a1 * y1 - a2 * y2; x2 = x1; x1 = x; y2 = y1; y1 = y; return y * y; };
}
export function createPcmMouth(sampleRate = 24000) {
  const filters = [[200, 550], [550, 1000], [1500, 3500]].map(([a, b]) => bandpass(sampleRate, a, b));
  const hops = [], pending = [];
  let acc = [0, 0, 0, 0], count = 0, frames = 0, done = 0, ended = false, voicedSum = 0, voicedCount = 0;
  // Received samples are measured lazily, a little ahead of the playhead, so a burst of packets
  // never blocks a frame (measuring a whole 5 s reply at once took ~30 ms).
  function ensure(seconds) {
    const until = Math.min(frames, Math.ceil(seconds * sampleRate));
    while (done < until && pending.length) {
      const chunk = pending[0], start = chunk.offset;
      const stop = Math.min(chunk.samples.length, until - start);
      for (let i = done - start; i < stop; i++) {
        const x = chunk.samples[i];
        acc[0] += x * x; for (let k = 0; k < 3; k++) acc[k + 1] += filters[k](x);
        if (++count === HOP) close();
      }
      done = start + stop;
      if (stop === chunk.samples.length) pending.shift();
    }
    if (ended && done === frames && count) close();
  }
  function close() {
    const db = 10 * Math.log10(acc[0] / count + 1e-12);
    const f1ratio = (acc[2] + 1e-12) / (acc[1] + 1e-12), front = Math.log10((acc[3] + 1e-12) / (acc[1] + acc[2] + 1e-12));
    const shape = db < -54 ? null : f1ratio > 1.3 ? 'A' : front > .1 ? 'I' : front > -.45 ? 'E' : f1ratio < .45 ? 'E' : 'A';
    if (db > -48) { voicedSum += db; voicedCount++; }
    hops.push({db, shape});
    acc = [0, 0, 0, 0]; count = 0;
  }
  function raw(seconds) {
    const i = Math.floor(seconds * sampleRate / HOP);
    const h = hops[i];
    if (!h || !h.shape) return {open: 0, shape: null, db: h ? h.db : -100};
    const reference = voicedCount ? voicedSum / voicedCount : -30;
    const emphasis = clamp(1 + (h.db - reference) / 44, .40, 1.21), audibility = clamp((h.db + 57) / 19, 0, 1);
    return {open: clamp(SHAPE_AMOUNT[h.shape] * emphasis * audibility, 0, .88), shape: h.shape, db: h.db};
  }
  return {
    push(samples) {
      pending.push({offset: frames, samples: Float32Array.from(samples)});
      frames += samples.length;
    },
    finish() { ended = true; },
    get seconds() { return frames / sampleRate; },
    // Same +-50 ms triangular average as alignedMouth, over what has arrived.
    sample(seconds) {
      ensure(seconds + .1);
      let open = 0, weights = 0;
      const vowels = {};
      for (const offset of SMOOTH) {
        const w = 4 - Math.abs(offset) / .0125, r = raw(seconds + offset);
        weights += w; open += w * r.open;
        if (r.shape) vowels[r.shape] = (vowels[r.shape] || 0) + w * r.open;
      }
      open /= weights;
      let total = 0, shape = 'closed', best = 0;
      for (const v in vowels) total += vowels[v];
      if (!(open > 0) || !(total > 0)) return {open: 0, vowels: null, shape: 'closed', db: raw(seconds).db, width: 1};
      for (const v in vowels) { vowels[v] /= total; if (vowels[v] > best) { best = vowels[v]; shape = v; } }
      return {open, vowels: {...vowels, amount: open}, shape, db: raw(seconds).db, width: 1};
    },
    // First sound, first opening peak and (once the reply has fully arrived) last sound.
    span(playhead = 0) {
      // Look ahead at most 0.1 s of new audio per frame (~1 ms of work) so the reply start, when
      // packets, captions and playback all begin, gets no extra main-thread stall.
      ensure(Math.max(playhead + .1, Math.min(playhead + .7, done / sampleRate + .1)));
      const first = hops.findIndex(h => h.db > -48);
      if (first < 0) return null;
      const onset = first * HOP / sampleRate;
      let vowel = onset + .15, best = 0;
      for (let t = onset; t < onset + .6 && t < done / sampleRate - .05; t += .01) {
        const o = this.sample(t).open;
        if (o > best) { best = o; vowel = t; } else if (best > .2 && o < best - .05) break;
      }
      let last = hops.length - 1;
      // The tail uses the same -54 dB voicing threshold as the speaking state, so a quiet word end
      // still counts as speech.
      while (last > first && !(hops[last].db > -54)) last--;
      return {onset, vowel: Math.max(onset + .1, vowel), end: ended && done === frames ? (last + 1) * HOP / sampleRate : Infinity};
    },
  };
}

// Where the reply actually speaks (seconds from playback start): first and last non-silent phone.
export function speechSpan(track, sampleRate = 24000) {
  const phones = (track?.phones || []).filter(p => !/^(sil|sp|spn|\+.*)$/i.test(p.phone));
  if (!phones.length) return null;
  const onset = phones[0].start;
  // The listening mouth hands over at the first opening peak of the articulated mouth (the first
  // vowel fully open), found on the same smoothed curve that drives the lips.
  let vowel = onset + .15, best = 0;
  for (let t = onset; t < onset + .6; t += .01) {
    const open = alignedMouth(track, t, sampleRate).open;
    if (open > best) { best = open; vowel = t; } else if (best > .2 && open < best - .05) break;
  }
  return {onset, vowel: Math.max(onset + .1, vowel), end: phones[phones.length - 1].end};
}

// Speaking weight of the face. The listening mouth dissolves straight into the first vowel: it
// starts 0.05 s before the first sound and is gone when the first vowel is open, so the mouth never
// shuts at playback start and opens again. From 0.1 s after the last sound it returns over 0.4 s.
export function speechEnvelope(span, elapsed) {
  if (!span || !Number.isFinite(elapsed)) return 0;
  const ease = x => { const t = clamp(x, 0, 1); return t*t*(3-2*t); };
  const start = Math.max(0, span.onset - .05);
  return ease((elapsed - start) / (span.vowel - start)) * (1 - ease((elapsed - (span.end + .1)) / .4));
}

// Both timestamps are AudioContext seconds. A track received in the middle of
// a word can only take over at a subsequent word start. Missing word timing
// or a stalled animation frame leaves the waveform in control until then.
export function createWordHandoff(track, receivedAt) {
  let boundary = null, startedAt = null, direct = false;
  const span = speechSpan(track, track.sample_rate || 24000);
  return {
    span,
    get boundary() { return boundary; },
    get startedAt() { return startedAt; },
    needsWave(now) { return !direct && (startedAt === null || now < startedAt + .09); },
    sample(wave, now, playbackStart, sampleRate) {
      const elapsed = now - playbackStart;
      // If alignment arrives before playback, the plan
      // drives the mouth from the first sample, never the per-frame loudness classifier.
      if (receivedAt <= playbackStart) {
        if (startedAt === null) { startedAt = now; direct = true; boundary = track.words?.[0] ?? null; }
        return {...alignedMouth(track, elapsed, sampleRate), width: 1, wordBlend: 1};
      }
      if (startedAt === null) {
        // Accept the first rendered frame of a word, allowing one 15 fps frame.
        const earliest = Math.max(0, receivedAt - playbackStart, elapsed - 1/15);
        boundary = track.words?.find(w => w.start >= earliest && w.end > elapsed) ?? null;
        if (!boundary || elapsed < boundary.start) return wave;
        startedAt = now;
      }
      const target = alignedMouth(track, elapsed, sampleRate);
      if (now >= startedAt + .09) return {...target, width:1, wordBlend:1};
      const t = clamp((now - startedAt) / .09, 0, 1);
      const blend = t*t*(3-2*t);
      // Weight each shape by its actual opening so a closed phoneme fades to
      // a single closed mouth, regardless of the vowel that came before it.
      const from = (1-blend)*wave.open, to = blend*target.open;
      const open = from + to, vowels = {amount:open};
      if (open > 0) {
        for (const vowel of ['A','I','U','E','O']) {
          const weight = (from*(wave.vowels?.[vowel] || 0) + to*(target.vowels?.[vowel] || 0))/open;
          if (weight > 0) vowels[vowel] = weight;
        }
      }
      return {open, vowels, width:wave.width+(1-wave.width)*blend,
        db:wave.db+(target.db-wave.db)*blend, shape:blend<.5?wave.shape:target.shape, wordBlend:blend};
    }
  };
}
