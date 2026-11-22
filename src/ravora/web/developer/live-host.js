const secret = location.hash.slice(1);
history.replaceState(null, '', location.pathname);
const headers = {Authorization: 'Bearer ' + secret};
const micButton = document.querySelector('#mic');
const talkMode = document.querySelector('#talk-mode');
const record = document.querySelector('#record'), monitor = document.querySelector('#monitor');
const notice = document.querySelector('#notice'), caption = document.querySelector('#caption');
let mic, submitting = false, limit, dell = false;
let playback, lastAudio, monitorChosen = false, monitorReset, nextSound = 0;
const sources = new Set();
let sessionChange = Promise.resolve();
const languageTabs = document.querySelector('#language-tabs');
const tabs = Array.from(languageTabs.querySelectorAll('[role=tab]'));
const conversation = document.querySelector('#conversation');
let hostLanguage, languageRequest = 0, switching = false;

function showLanguage(state) {
  hostLanguage = state.host_language;
  if (!state.conversation_language) return;
  for (const tab of tabs) {
    const selected = tab.dataset.language === state.conversation_language;
    tab.setAttribute('aria-selected', String(selected));
    tab.tabIndex = selected ? 0 : -1;
    if (selected) conversation.setAttribute('aria-labelledby', tab.id);
  }
}

async function selectLanguage(tab) {
  if (switching || tab.dataset.language === hostLanguage) return;
  switching = true;
  languageRequest++;
  const error = document.querySelector('#language-error');
  error.textContent = '';
  try {
    const response = await fetch('/v1/host/language', {
      method: 'POST', headers: {...headers, 'Content-Type': 'application/json'},
      body: JSON.stringify({language: tab.dataset.language}), signal: AbortSignal.timeout(4000)
    });
    if (!response.ok) throw new Error();
    showLanguage(await response.json());
  } catch {
    error.textContent = '会話言語を切り替えられませんでした。もう一度選んでください。';
  } finally {
    languageRequest++;
    switching = false;
  }
}
for (const tab of tabs) {
  tab.onclick = () => selectLanguage(tab);
  tab.onkeydown = event => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    if (switching) return;
    const next = event.key === 'Home' ? tabs[0] : event.key === 'End' ? tabs.at(-1) : tabs.find(other => other !== tab);
    next.focus();
    selectLanguage(next);
  };
}

function controls() {
  talkMode.disabled = Boolean(mic);
  record.hidden = talkMode.value === 'auto';
  micButton.disabled = !limit;
  const label = dell ? 'Dellマイク' : 'マイク';
  micButton.textContent = mic?.session ? label + 'をミュート' : mic ? '接続を中止' : label + 'をオン';
  micButton.setAttribute('aria-pressed', String(Boolean(mic?.session)));
  record.disabled = !mic?.session || Boolean(mic?.question?.ending) || submitting;
  record.textContent = mic?.question ? '発言を確定' : 'Rayに話しかける';
  record.setAttribute('aria-pressed', String(Boolean(mic?.question)));
}
talkMode.onchange = controls;
controls();

function recognitionMessage(code) {
  const messages = {
    AUDIO_SILENCE: 'マイクの入力音量が小さいようです。',
    HOST_NO_VOICE: '発話区間を検出できませんでした。',
    HOST_SPEECH_UNCLEAR: '音声を言葉として読み取れませんでした。',
    HOST_LANGUAGE_UNCERTAIN: '発言の言語を判別できませんでした。',
    HOST_MIC_BUSY: '先の発言を処理中です。'
  };
  return messages[code] || code;
}

function closeSession(session) {
  return fetch('/v1/host/mic/mute', {
    method: 'POST', keepalive: true, body: '{}',
    headers: {...headers, 'Content-Type': 'application/json', 'X-Mic-Session': session},
    signal: AbortSignal.timeout(1000)
  }).catch(() => {});
}

function changeSession(operation) {
  const result = sessionChange.then(operation);
  sessionChange = result.catch(() => {});
  return result;
}

function mute(input, message) {
  if (mic !== input) return;
  mic = null;
  clearTimeout(input.question?.timer);
  input.question = input.pending = null;
  clearInterval(input.watchdog);
  input.upload?.abort();
  for (const track of input.stream?.getTracks() ?? []) {
    track.enabled = false;
    track.stop();
  }
  if (input.node) {
    input.node.port.onmessage = null;
    input.node.disconnect();
  }
  if (input.context && input.context.state !== 'closed') input.context.close().catch(() => {});
  if (input.session) changeSession(() => closeSession(input.session));
  controls();
  notice.textContent = message;
}

async function sendPackets(input) {
  if (input.upload) return;
  const upload = new AbortController();
  input.upload = upload;
  try {
    while (input.pending && mic === input) {
      const {samples, sequence, endTime} = input.pending;
      input.pending = null;
      if (input.context.currentTime - endTime > .25) continue;
      const response = await fetch('/v1/host/mic/pcm', {
        method: 'POST', body: samples, signal: AbortSignal.any([upload.signal, AbortSignal.timeout(900)]),
        headers: {...headers, 'Content-Type': 'application/octet-stream',
                  'X-Mic-Session': input.session, 'X-Mic-Sequence': String(sequence)}
      });
      if (!response.ok) throw new Error('mic upload');
    }
  } catch {
    if (mic === input) mute(input, '接続が途切れたためミュートしました。マイクをオンにすると再接続します。');
  } finally {
    input.upload = null;
  }
}

async function claimSession(input) {
  await changeSession(async () => {
    if (mic !== input) return;
    const response = await fetch(input.automatic ? '/v1/host/mic/open-auto' : '/v1/host/mic/open', {
      method: 'POST', body: '{}', headers: {...headers, 'Content-Type': 'application/json'},
      signal: AbortSignal.timeout(dell ? 12000 : 1500)
    });
    const result = await response.json();
    if (!response.ok) throw new Error(response.status === 409 ? '別の画面でマイクがオンです。' : result.error);
    if (mic !== input) { await closeSession(result.session); return; }
    input.session = result.session;
  });
}

async function dellRequest(input, action, extra = {}, timeout = 1500) {
  const response = await fetch('/v1/host/mic/' + action, {
    method: 'POST', body: '{}', signal: AbortSignal.timeout(timeout),
    headers: {...headers, 'Content-Type': 'application/json', 'X-Mic-Session': input.session, ...extra}
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error);
}

async function renewDell(input) {
  if (mic !== input) return;
  try {
    await dellRequest(input, 'heartbeat');
    if (mic === input) input.watchdog = setTimeout(() => renewDell(input), 550);
  } catch {
    mute(input, 'Dellマイクの接続が切れたためミュートしました。');
  }
}

async function openMic() {
  const input = {sequence: 0, automatic: talkMode.value === 'auto'};
  if (input.automatic && !monitorChosen && !playback) monitor.click();
  mic = input;
  controls();
  notice.textContent = 'マイクを接続中…';
  try {
    if (dell) {
      await claimSession(input);
      if (mic !== input) return;
      controls();
      notice.textContent = 'Dellマイク入力中';
      renewDell(input);
      return;
    }
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {channelCount: 1, echoCancellation: true}, video: false
    });
    if (mic !== input) { stream.getTracks().forEach(track => track.stop()); return; }
    input.stream = stream;
    const track = stream.getAudioTracks()[0];
    track.onended = () => mute(input, 'マイクが切断されたためミュートしました。');
    const context = input.context = new AudioContext({sampleRate: 48000});
    await context.audioWorklet.addModule('/mic-stream.js');
    await context.resume();
    if (mic !== input) return;
    const source = context.createMediaStreamSource(stream);
    const node = input.node = new AudioWorkletNode(context, 'ravora-mic-stream', {
      processorOptions: {chunk_ms: 100}
    });
    const silent = context.createGain();
    silent.gain.value = 0;
    await claimSession(input);
    if (mic !== input) return;
    input.lastPacket = performance.now();
    input.watchdog = setInterval(() => {
      if (performance.now() - input.lastPacket > 1500) mute(input, 'マイク入力が途切れたためミュートしました。');
    }, 500);
    node.onprocessorerror = () => mute(input, 'マイク入力エラーのためミュートしました。');
    node.port.onmessage = ({data}) => {
      if (mic !== input || data.kind !== 'pcm') return;
      const sequence = input.sequence++;
      if (context.currentTime - data.endTime > .25) return;
      input.lastPacket = performance.now();
      const question = input.question;
      if (question) {
        const start = data.endTime - data.samples.length / context.sampleRate;
        const index = time => Math.max(0, Math.min(data.samples.length, Math.round((time - start) * context.sampleRate)));
        const first = index(question.start);
        const last = index(question.end);
        if (last > first) question.chunks.push(data.samples.subarray(first, last));
        if (data.endTime >= question.end) finishQuestion(input);
      }
      input.pending = {sequence, samples: data.samples, endTime: data.endTime};
      sendPackets(input);
    };
    context.onstatechange = () => {
      if (mic === input && context.state !== 'running') mute(input, '音声入力が停止したためミュートしました。');
    };
    source.connect(node).connect(silent).connect(context.destination);
    controls();
    notice.textContent = 'マイク配信中';
  } catch (error) {
    mute(input, 'マイクはミュートです。' + error.message);
  }
}

micButton.onclick = () => mic ? mute(mic, 'マイクはミュートです。') : openMic();
window.addEventListener('pagehide', () => { if (mic) mute(mic, 'マイクはミュートです。'); });

async function questionWave(chunks) {
  const length = chunks.reduce((n, chunk) => n + chunk.length, 0);
  const render = new OfflineAudioContext(1, Math.ceil(length / 3), 16000);
  const source = render.createBufferSource();
  source.buffer = render.createBuffer(1, length, 48000);
  const channel = source.buffer.getChannelData(0);
  let i = 0;
  for (const chunk of chunks) for (const sample of chunk) channel[i++] = sample / 32768;
  source.connect(render.destination);
  source.start();
  const samples = (await render.startRendering()).getChannelData(0);
  const bytes = new ArrayBuffer(44 + samples.length * 2), view = new DataView(bytes);
  const label = (offset, text) => [...text].forEach((ch, n) => view.setUint8(offset + n, ch.charCodeAt(0)));
  label(0, 'RIFF'); view.setUint32(4, bytes.byteLength - 8, true); label(8, 'WAVE'); label(12, 'fmt ');
  view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, 16000, true); view.setUint32(28, 32000, true); view.setUint16(32, 2, true);
  view.setUint16(34, 16, true); label(36, 'data'); view.setUint32(40, samples.length * 2, true);
  samples.forEach((value, n) => view.setInt16(44 + n * 2, Math.max(-32768, Math.min(32767, Math.round(value * 32768))), true));
  return bytes;
}

async function finishQuestion(input) {
  const {chunks, language} = input.question;
  input.question = null;
  if (chunks.reduce((n, chunk) => n + chunk.length, 0) < 9600) {
    controls(); notice.textContent = '発言が短すぎます。'; return;
  }
  submitting = true;
  controls();
  notice.textContent = '発言を音声認識中';
  try {
    const response = await fetch('/v1/host/question', {
      method: 'POST', body: await questionWave(chunks),
      headers: {...headers, 'Content-Type': 'audio/wav', 'X-Input-ID': crypto.randomUUID().replaceAll('-', ''),
                'X-Host-Language': language}
    });
    const result = await response.json();
    if (mic === input) notice.textContent = response.ok ? 'Rayへの発言を受け付けました。マイク配信中' : recognitionMessage(result.error);
  } catch {
    if (mic === input) notice.textContent = '発言の送信結果を確認できませんでした。';
  } finally {
    submitting = false;
    controls();
  }
}

async function dellQuestion(input) {
  const starting = !input.question;
  submitting = true;
  clearTimeout(input.question?.timer);
  controls();
  try {
    if (starting) {
      await dellRequest(input, 'question/start');
      if (mic !== input) return;
      input.question = {timer: setTimeout(() => dellQuestion(input), limit * 1000)};
      notice.textContent = 'Dellマイクで話し、話し終えたら「発言を確定」を押してください。';
    } else {
      input.question = null;
      notice.textContent = '発言を音声認識中';
      await dellRequest(input, 'question/finish', {'X-Input-ID': crypto.randomUUID().replaceAll('-', '')}, 90000);
      if (mic === input) notice.textContent = 'Rayへの発言を受け付けました。';
    }
  } catch (error) {
    if (mic === input) notice.textContent = '発言を受け付けられませんでした。' + recognitionMessage(error.message);
  } finally {
    submitting = false;
    controls();
  }
}

record.onclick = () => {
  if (!mic?.session || submitting) return;
  if (dell) { dellQuestion(mic); return; }
  if (mic.question) {
    mic.question.end = Math.min(mic.question.end, mic.context.currentTime);
    mic.question.ending = true;
    controls();
  }
  else {
    const start = mic.context.currentTime;
    mic.question = {start, end: start + limit, chunks: [], language: hostLanguage};
    notice.textContent = 'Rayに話しかけています。話し終えたら発言を確定してください。';
    controls();
  }
};

monitor.onclick = async () => {
  monitorChosen = true;
  if (playback) { const old = playback; playback = null; await old.close(); sources.clear(); nextSound = 0; monitor.textContent = '返答モニターを有効化'; }
  else { playback = new AudioContext(); await playback.resume(); monitor.textContent = '返答モニターを消音'; }
  lastAudio = null;
  monitor.setAttribute('aria-pressed',String(Boolean(playback)));
};
async function update() {
  try {
    const revision = languageRequest;
    const response = await fetch('/v1/status', {headers});
    if (!response.ok) { notice.textContent = '起動時に表示された操作URLを開いてください'; return; }
    const state = await response.json();
    if (revision === languageRequest && !switching) showLanguage(state);
    if (!limit) {
      if (state.reply_mode === 'cloud' && state.conversation_language) {
        languageTabs.hidden = false;
        conversation.setAttribute('role', 'tabpanel');
      }
      document.querySelector('#reply-mode').textContent = state.reply_mode === 'offline'
        ? '音声テスト用の英語定型文' : 'Rayの会話モード';
      limit = state.max_mic_seconds; dell = state.mic_source === 'dell'; controls(); monitor.disabled = false; }
    if (mic?.automatic && mic.session && state.handsfree) {
      const {state: phase, result} = state.handsfree;
      const messages = {speaking: '聞いています…', transcribing: '発言を認識中…', playback: 'Rayが話しています。マイク音声は録画中です。'};
      const last = result && result !== 'received' ? recognitionMessage(result) + ' ' : '';
      const message = messages[phase] || last + '声を待っています。';
      if (notice.textContent !== message) notice.textContent = message;
    }
    if (state.conversation_paused || (state.last_error && !state.stage.speaker && !state.stage.thinking))
      notice.textContent = '返答を生成できませんでした。' + (state.conversation_paused || state.last_error);
    caption.textContent = [state.stage.host_caption && 'HOST ' + state.stage.host_caption, state.stage.speaker, state.stage.caption].filter(Boolean).join('　');
    const name = state.monitor_audio, output = playback;
    if (monitorReset !== state.monitor_reset) {
      for (const source of sources) source.stop();
      sources.clear(); nextSound = 0; monitorReset = state.monitor_reset;
    }
    if (output && name !== lastAudio) {
      lastAudio = name;
      if (name) {
        const received = performance.now();
        const audio = await fetch('/v1/monitor/' + name, {headers});
        if (audio.ok) {
          const data = await output.decodeAudioData(await audio.arrayBuffer());
          const lag = state.monitor_offset_seconds + (performance.now() - received) / 1000;
          // Normal polling observes an utterance just after onset. Play its first
          // syllable and let the preceding clip finish. Seek only on a late join.
          const offset = lag < .6 ? 0 : lag;
          if (playback === output && offset < data.duration) {
            const playing = output.createBufferSource();
            sources.add(playing); playing.onended = () => sources.delete(playing);
            playing.buffer = data;
            playing.connect(output.destination);
            const start = Math.max(output.currentTime, nextSound);
            playing.start(start, offset);
            nextSound = start + data.duration - offset;
          }
        }
      }
    }
  } catch { notice.textContent = '配信バックエンドとの接続を確認してください'; }
  setTimeout(update, 150);
}
update();
