import {sampleMouthPlan} from './mouth-plan.mjs';

const clamp = (x, low, high) => Math.min(high, Math.max(low, x));

// The mouth plan gives geometry; the envelope comes from the reply PCM itself.
// Both are sampled relative to the worklet's first consumed sample.
export function alignedMouth(track, seconds, sampleRate) {
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

// Both timestamps are AudioContext seconds. A track received in the middle of
// a word can only take over at a subsequent word start. Missing word timing
// or a stalled animation frame leaves the waveform in control until then.
export function createWordHandoff(track, receivedAt) {
  let boundary = null, startedAt = null;
  return {
    get boundary() { return boundary; },
    get startedAt() { return startedAt; },
    needsWave(now) { return startedAt === null || now < startedAt + .09; },
    sample(wave, now, playbackStart, sampleRate) {
      const elapsed = now - playbackStart;
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
