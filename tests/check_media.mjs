import assert from 'node:assert/strict';
import test from 'node:test';
import {alignedMouth, createWordHandoff} from '../src/ravora/web/articulation.mjs';
import {createPreviewMouth} from '../src/ravora/web/preview-mouth.mjs';
import {createAudioClockCapture, encodeObservedDraws} from '../src/ravora/web/audio-clock-capture.mjs';
import {createExpressionMotion, createMouthHandoff} from '../src/ravora/web/expressions.mjs';
import {createIdleGlance} from '../src/ravora/web/presence.mjs';
import {writeMouthLandmarks} from '../src/ravora/web/pngtuber-surface.mjs';
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

test('A sustained thinking pose stays through a reply and releases on the next cue', () => {
  const motion = createExpressionMotion(.4, new Set(['thinking']));
  motion.sample(0, 'thinking', .65, null, true);
  const settled = motion.sample(1, 'thinking', .65, null, true);
  assert.equal(settled.art_thinking, .65);
  assert.equal(motion.sample(6, 'thinking', .65, null, true).art_thinking, settled.art_thinking);
  assert.equal(motion.sample(6, 'neutral').art_thinking, settled.art_thinking);
  assert.equal(motion.sample(9, 'neutral').art_thinking, 0);
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
  // The lip renderer starts revealing the inner mouth above .006 opening.
  assert.ok(alignedMouth(track, .25, 24000).open < .006);
  const held = {...track, plan: [{shape: 'closed', amount: 0, start: 0, end: .5}]};
  assert.equal(alignedMouth(held, .25, 24000).open, 0);
  assert.equal(alignedMouth(track, .40, 24000).shape, 'U');
  assert.equal(alignedMouth(track, .51, 24000).open, 0);
});

test('Prepared and live speech share an opening curve through a bilabial closure', () => {
  const rate = 48000;
  const plan = [
    {shape: 'A', amount: .72, start: 0, end: .20},
    {shape: 'closed', amount: 0, start: .20, end: .30},
    {shape: 'E', amount: .58, start: .30, end: .5},
  ];
  const levels = Array.from({length: 50}, (_, i) => i % 2 ? -20 : -24);
  const track = {plan, db: levels, reference_db: -22, frames: rate / 2, hop_frames: rate * .01};
  const sample = createPreviewMouth(plan, levels, .01, rate, track.frames);
  for (let frame = 0; frame <= 15; frame++) {
    const t = frame / 30;
    const {open, shape, vowels} = alignedMouth(track, t, rate);
    assert.deepEqual(sample(t), {amount: open, shape, vowels});
  }
  assert.ok(sample(.14).amount > .55);
  assert.ok(sample(.25).amount < .006);
  assert.equal(sample(.40).shape, 'E');
  assert.equal(sample(.5).amount, 0);
  assert.equal(createPreviewMouth([], levels, .01, rate, track.frames), null);
});

test('A stressed vowel still opens clearly without jumping between adjacent 30 fps frames', () => {
  const plan = [
    {shape: 'A', amount: .12, start: 0, end: .20},
    {shape: 'A', amount: .84, start: .20, end: .29},
    {shape: 'E', amount: .32, start: .29, end: .50},
  ];
  const sample = createPreviewMouth(plan, Array(50).fill(-23), .01, 24000, 12000);
  const openings = Array.from({length: 12}, (_, i) => sample((i + 1) / 30).amount);
  const changes = openings.slice(1).map((opening, i) => Math.abs(opening - openings[i]));
  // The 75 ms window preserves the short stressed peak while spreading its rise over three frames.
  assert.ok(Math.max(...openings) > .74);
  assert.ok(Math.max(...changes) < .24);
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

test('Deferred capture paints only observed draws and drains a slow encoder', async t => {
  const canvas = {width: 1920, height: 1080};
  const profile = {fps: 30, bitrate: 6000000, maxBytes: 100};
  const draws = [0, 1, 3, 4, 5, 6, 8, 9].map(tick => ({tick, caption: String(tick)}));
  const painted = [];
  class Frame {
    constructor(_canvas, {timestamp}) { this.timestamp = timestamp; }
    close() {}
  }
  class QueuedEncoder {
    static async isConfigSupported() { return {supported: true}; }
    constructor({output}) { this.output = output; this.queue = []; }
    get encodeQueueSize() { return this.queue.length; }
    configure() {}
    encode(frame) { this.queue.push(frame.timestamp); }
    async flush() {
      for (const timestamp of this.queue.splice(0)) {
        this.output({timestamp, byteLength: 1, copyTo(data) { data[0] = 1; }});
      }
    }
    close() {}
  }
  const previousFrame = globalThis.VideoFrame, previousEncoder = globalThis.VideoEncoder;
  t.after(() => { globalThis.VideoFrame = previousFrame; globalThis.VideoEncoder = previousEncoder; });
  globalThis.VideoFrame = Frame;
  globalThis.VideoEncoder = QueuedEncoder;
  const {blob, summary} = await encodeObservedDraws(canvas, profile, draws, draw => painted.push(draw.caption));
  assert.deepEqual(painted, draws.map(draw => draw.caption));
  assert.deepEqual(summary.ticks, draws.map(draw => draw.tick));
  assert.equal(summary.queue_drops, 0);
  assert.equal(blob.size, draws.length);
  await assert.rejects(encodeObservedDraws(canvas, profile, [draws[0], draws[0]], () => {}), /INVALID_VIDEO_FRAME_INDEX/);
});

test('Buffering preserves the listening pose; playback owns one continuous handoff', () => {
  const rest = createMouthHandoff();
  assert.equal(rest(0, false), 1);
  assert.equal(rest(7.4, false), 1);
  assert.equal(rest(8.1, true), 1);
  assert.ok(Math.abs(rest(8.215, true) - .5) < 1e-6);
  assert.equal(rest(8.5, true), 0);
  assert.equal(rest(9.0, true), 0); // A quiet phone inside the same utterance.
  assert.equal(rest(9.2, false), 0);
  const partial = rest(9.35, false);
  assert.ok(partial > 0 && partial < 1);
  assert.equal(rest(9.35, true), partial);
  assert.equal(rest(9.8, true), 0);
});

test('A silent interval can produce a glance; input releases it from the current position', () => {
  const glance = createIdleGlance(13);
  assert.equal(glance(0, true), 0);
  assert.equal(glance(.75, true), 0);
  let time = .75, away = 0;
  while (time < 2 && away < .5) { time += .01; away = glance(time, true); }
  assert.ok(away >= .5);
  assert.equal(glance(time, false), away);
  const returning = glance(time+.08, false);
  assert.ok(returning > 0 && returning < away);
  assert.equal(glance(time+.08, true), returning);
  assert.equal(glance(time+.2, true), 0);
  assert.equal(glance(time+8, false), 0);
  assert.equal(glance(time+9, true), 0);
});

test('Rest, expression and two vowels displace a single registered lip contour', () => {
  const mesh = (x, y, tx, ty, ex, ey) => ({N:11,
    array:Float32Array.of(x,y,tx,ty,0,0,ex,ey,0,0,0)});
  const donor = mesh(40,60,50,70,50,70);
  const rest = mesh(39,60,45,62,45,62);
  const vowel = mesh(41,57,43,61,45,63);
  writeMouthLandmarks(donor, [{mesh:rest,weight:.5,express:0},
    {mesh:donor,weight:.25,express:0}, {mesh:vowel,weight:.25,express:.5}]);
  assert.deepEqual(Array.from(donor.array.slice(9)), [46.25,64.75]);
  writeMouthLandmarks(donor, []);
  assert.deepEqual(Array.from(donor.array.slice(9)), [40,60]);
});

test('A late phoneme track joins the next word on the playback clock', () => {
  const track = {frames:24000, hop_frames:240, db:Array(100).fill(-20), reference_db:-20,
    words:[{word:'we',start:.10,end:.39}, {word:'met',start:.4,end:.8}],
    plan:[{start:0,end:1,amount:.6,shape:'I'}]};
  const wave = {open:.2, width:.8, db:-20, shape:'A', vowels:{A:1,amount:.2}};
  const start = 1, handoff = createWordHandoff(track, 1.25);
  assert.equal(handoff.sample(wave, 1.36, start, 24000), wave);
  assert.equal(handoff.startedAt, null);
  const first = handoff.sample(wave, 1.41, start, 24000);
  assert.equal(first.wordBlend, 0);
  assert.equal(first.open, wave.open);
  assert.equal(handoff.boundary.word, 'met');
  const middle = handoff.sample(wave, 1.50, start, 24000);
  assert.ok(Math.abs(middle.wordBlend-.5)<1e-6);
  assert.ok(middle.vowels.A>0 && middle.vowels.I>0);
  assert.equal(handoff.sample(wave, 1.60, start, 24000).wordBlend, 1);
  for (const [words, arrival, draw] of [[track.words,1.46,1.55],
      [track.words,1.25,1.71],[undefined,1.1,1.41]]) {
    const late = createWordHandoff({...track,words}, arrival);
    assert.equal(late.sample(wave, draw, start, 24000), wave);
  }
});
