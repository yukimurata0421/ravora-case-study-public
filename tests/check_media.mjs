import assert from 'node:assert/strict';
import test from 'node:test';
import {alignedMouth} from '../src/ravora/web/articulation.mjs';
import {createAudioClockCapture} from '../src/ravora/web/audio-clock-capture.mjs';
import {createExpressionMotion} from '../src/ravora/web/expressions.mjs';
import {sharedMouthTargets} from '../src/ravora/web/mouth-plan.mjs';

test('A finalized smile keeps its animation and concern replaces it smoothly', () => {
  const motion = createExpressionMotion(.4, new Set(['happy', 'concerned']), null, false);
  motion.sample(0, 'happy', .65);
  const listening = motion.sample(.5, 'happy', .65);
  assert.equal(motion.sample(.7, 'happy', .65).art_happy, listening.art_happy);
  motion.sample(1, 'concerned', .65);
  const middle = motion.sample(1.2, 'concerned', .65);
  assert.ok(middle.art_happy < listening.art_happy && middle.art_concerned > 0);
  const settled = motion.sample(1.5, 'concerned', .65);
  assert.equal(settled.art_happy, 0);
  assert.equal(settled.art_concerned, .65);
});

test('A phone controls shape, PCM controls opening, and closures own the lips', () => {
  const track = {
    frames: 12000, hop_frames: 240, reference_db: -23, db: Array(50).fill(-23),
    plan: [
      {shape: 'I', amount: .64, start: 0, end: .20},
      {shape: 'closed', amount: 0, start: .20, end: .30},
      {shape: 'U', amount: .55, start: .30, end: .50},
    ],
  };
  const good = alignedMouth(track, .10, 24000);
  const quiet = alignedMouth({...track, db: Array(50).fill(-40)}, .10, 24000);
  assert.equal(good.shape, 'I');
  assert.ok(quiet.open < good.open);
  assert.equal(alignedMouth(track, .25, 24000).open, 0);
  assert.equal(alignedMouth(track, .40, 24000).shape, 'U');
  assert.equal(alignedMouth(track, .51, 24000).open, 0);
});

test('Blended vowels use one weighted target geometry', () => {
  const layers = [
    {vertices: [0, 0, 10, 14, 0, 0, 12, 18, 0, 0, 0], weight: 1, expressionMix: .5},
    {vertices: [0, 0, 13, 8, 0, 0, 19, 12, 0, 0, 0], weight: 3, expressionMix: .5},
  ];
  assert.deepEqual(Array.from(sharedMouthTargets(layers)), [14.75, 11.5]);
  assert.equal(sharedMouthTargets(layers.slice(0, 1)), null);
});

test('The encoder accepts gaps on the sample clock without shifting later frames', async () => {
  const closed = [];
  globalThis.VideoFrame = class {
    constructor(_canvas, {timestamp, duration}) {
      this.timestamp = timestamp;
      this.duration = duration;
    }
    close() { closed.push(this.timestamp); }
  };
  globalThis.VideoEncoder = class {
    static async isConfigSupported(config) { return {supported: config.avc.format === 'annexb'}; }
    constructor({output}) { this.output = output; this.encodeQueueSize = 0; }
    configure() {}
    encode(frame) {
      this.output({timestamp: frame.timestamp, byteLength: 3, copyTo(data) { data.set([1, 2, 3]); }});
    }
    async flush() {}
    close() {}
  };
  const timestamps = [];
  const capture = await createAudioClockCapture({width: 1920, height: 1080},
    {fps: 30, bitrate: 6000000, maxBytes: 100}, error => { throw error; },
    (_bytes, tick) => timestamps.push(tick));
  capture.draw(0);
  await capture.resetAfterWarmup();
  capture.draw(0);
  capture.draw(1);
  capture.draw(3);
  const {blob, summary} = await capture.finish();
  assert.deepEqual(timestamps, [0, 1, 3]);
  assert.equal(summary.bytes, 9);
  assert.equal(blob.size, 0);
  assert.deepEqual(closed, [0, 0, 33333, 100000]);
});
