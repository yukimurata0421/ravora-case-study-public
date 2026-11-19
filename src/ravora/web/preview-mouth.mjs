import {alignedMouth} from './articulation.mjs';

// Adapt a prepared English utterance (or portable demo soundtrack) to the
// same phoneme and loudness sampler used during a V13 reply.
export function createPreviewMouth(plan, levels, hop, sampleRate, frames) {
  if (!plan?.length || !levels.length) return null;
  const voiced = levels.filter(db => db > -48).sort((a, b) => a - b);
  const middle = Math.floor(voiced.length / 2);
  const reference = voiced.length ? (voiced[middle] + voiced[Math.floor((voiced.length - 1) / 2)]) / 2 : -48;
  const track = {plan, frames, db: levels, hop_frames: hop * sampleRate, reference_db: reference};
  return seconds => {
    const {open, vowels, shape} = alignedMouth(track, seconds, sampleRate);
    return {amount: open, vowels, shape};
  };
}
