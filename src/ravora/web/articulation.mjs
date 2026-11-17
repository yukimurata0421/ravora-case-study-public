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
