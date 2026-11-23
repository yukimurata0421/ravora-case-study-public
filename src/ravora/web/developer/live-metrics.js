// Only the authenticated operator page loads this module.
const views = {
  broadcast: [
    ['番組フレームレート', 'fps', [['fps']], 1, 30],
    ['映像＋音声のビットレート', 'Mbps', [['encoded', '生成'], ['sent', '送出']], 2],
    ['送信待ちキュー', 'パケット', [['queued']], 0],
    ['エラー発生数', '件／計測区間', [['turn_errors', '会話'], ['mic_errors', '音声認識'], ['send_errors', '送出']], 0],
  ],
  usage: [
    ['CPU使用率', 'Rayプロセス · 1コア＝100%', [['cpu']], 1],
    ['使用メモリ', 'Rayプロセス · MiB', [['rss']], 1],
    ['会話API呼び出し', '件／直近60秒', [['gemini', 'Gemini'], ['jev', 'JEV'], ['tts', 'TTS']], 0],
    ['会話APIエラー', '件／直近60秒', [['api_failed', '失敗'], ['api_unknown', '結果不明']], 0],
    ['Gemini・JEV トークン', '報告済み · tokens／直近60秒', [['tokens']], 0],
    ['音声合成に送った文字数', '文字／直近60秒', [['characters']], 0],
  ],
  api: [
    ['会話・音声APIの合計', '件', [['calls']], 0],
    ['呼び出しの内訳', '件', [['gemini', 'Gemini'], ['jev', 'JEV'], ['tts', 'TTS'], ['stt', 'STT']], 0],
    ['Gemini・JEV トークン', '報告済み · tokens', [['tokens']], 0],
    ['音声合成に送った文字数', '文字', [['characters']], 0],
    ['音声認識に送った長さ', '秒', [['audio_seconds']], 1],
  ],
};
const colors = ['#f7bd71', '#8ccae8', '#efa3a2', '#a8d9b0'];
const format = (value, decimals) => value.toLocaleString('ja-JP', {maximumFractionDigits: decimals});
function element(type, className, text) {
  const node = document.createElement(type);
  node.className = className;
  node.textContent = text;
  return node;
}

function createChart([name, unit, series, decimals, floor = 1]) {
  const root = element('section', 'metric-card', '');
  const title = element('h3', '', name);
  title.append(element('span', 'metric-unit', unit));
  const readings = element('div', 'metric-readings', '');
  const values = series.map(([, label]) => {
    const item = element('span', 'metric-series', '');
    if (label) item.append(element('span', 'metric-name', label));
    const value = element('span', 'metric-value', '—');
    item.append(value);
    readings.append(item);
    return value;
  });
  const missing = series[0][0] === 'tokens' ? element('span', 'metric-name', '') : null;
  if (missing) readings.append(missing);
  const canvas = document.createElement('canvas');
  canvas.setAttribute('role', 'img');
  canvas.setAttribute('aria-label', name);
  root.append(title, readings, canvas);
  return {root, name, unit, series, values, decimals, floor, canvas, missing};
}

function draw(chart, points, end, windowSeconds, interval) {
  const {series, values, canvas, decimals} = chart;
  const width = canvas.clientWidth, height = canvas.clientHeight;
  if (!width || !height) return;
  const ratio = Math.min(devicePixelRatio || 1, 3);
  const w = Math.round(width * ratio), h = Math.round(height * ratio);
  if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);
  const latest = points.at(-1);
  if (chart.missing) chart.missing.textContent = latest?.missing_tokens ? '未報告 ' + latest.missing_tokens + '件' : '';
  const available = points.flatMap(p => series.map(([key]) => p[key]).filter(Number.isFinite));
  const top = Math.max(chart.floor, ...available);
  ctx.font = '11px system-ui';
  const left = Math.max(26, ctx.measureText(format(top, decimals)).width + 7);
  const right = width - 5, bottom = height - 17;
  const x = t => right - (end - t) / windowSeconds * (right - left);
  const y = v => bottom - (bottom - 8) * v / top;
  ctx.fillStyle = '#b9a99b';
  ctx.strokeStyle = '#514238';
  ctx.lineWidth = 1;
  for (const v of [0, top]) {
    ctx.beginPath(); ctx.moveTo(left, y(v)); ctx.lineTo(right, y(v)); ctx.stroke();
    ctx.fillText(format(v, decimals), 0, y(v) + 3);
  }
  const spanLabel = windowSeconds >= 60 && windowSeconds % 60 === 0
    ? windowSeconds / 60 + '分' : Math.round(windowSeconds) + '秒';
  ctx.fillText('−' + spanLabel, left, height - 2);
  ctx.textAlign = 'right'; ctx.fillText('現在', right, height - 2); ctx.textAlign = 'left';
  const description = [];
  series.forEach(([key, label], i) => {
    const v = latest?.[key];
    const display = Number.isFinite(v) ? format(v, decimals) : '—';
    values[i].textContent = display;
    description.push((label || chart.name) + ' ' + display);
    ctx.strokeStyle = ctx.fillStyle = colors[i]; ctx.lineWidth = 1.7;
    ctx.beginPath();
    let previous = null;
    for (const p of points) {
      if (!Number.isFinite(p[key])) { previous = null; continue; }
      if (previous === null || p.t - previous > interval * 2.5) ctx.moveTo(x(p.t), y(p[key]));
      else ctx.lineTo(x(p.t), y(p[key]));
      previous = p.t;
    }
    ctx.stroke();
    if (Number.isFinite(v)) {
      ctx.beginPath(); ctx.arc(x(latest.t), y(v), 2.5, 0, Math.PI * 2); ctx.fill();
    }
  });
  canvas.setAttribute('aria-label', description.join('、') + '　' + chart.unit);
  if (!available.length) {
    const local = latest && latest.queued === null && series[0][0] === 'queued';
    ctx.fillStyle = '#cbbfb4'; ctx.textAlign = 'center';
    ctx.fillText(local ? 'ローカル録画中' : 'データ待ち', (left + right) / 2, bottom / 2 + 5);
    ctx.textAlign = 'left';
  }
}

export function startMetrics(headers) {
  const panel = document.querySelector('#metric-charts');
  const period = document.querySelector('#metrics-window');
  const status = document.querySelector('#metrics-fresh');
  const buttons = [...document.querySelectorAll('.metrics-switch button')];
  let history = [], interval = 2, charts = [], newest = null, lastChange = 0, currentView;
  function render() {
    const end = history.at(-1)?.t || 0;
    const seconds = Number(period.value);
    const recent = history.filter(p => p.t >= end - seconds);
    const points = currentView === 'api' ? recent.map(p => ({...p.api_total, t: p.t})) : recent;
    const span = Math.min(seconds, Math.max(interval, end - (history[0]?.t ?? end)));
    for (const chart of charts) draw(chart, points, end, span, interval);
  }
  function show(view) {
    currentView = view;
    document.querySelector('#api-scope').hidden = view !== 'api';
    buttons.forEach(b => b.setAttribute('aria-pressed', String(b.dataset.view === view)));
    charts = views[view].map(createChart);
    panel.replaceChildren(...charts.map(c => c.root));
    render();
  }
  buttons.forEach(b => b.addEventListener('click', () => show(b.dataset.view)));
  period.addEventListener('change', render);
  new ResizeObserver(render).observe(panel);
  show('broadcast');
  async function refresh() {
    try {
      const response = await fetch('/v1/host/metrics', {headers, cache: 'no-store', signal: AbortSignal.timeout(5000)});
      if (response.status === 401 || response.status === 403) {
        status.textContent = '起動時のホスト操作URLを開いてください';
        return;
      }
      if (!response.ok) throw new Error();
      const data = await response.json();
      interval = data.interval;
      history = data.points;
      const stamp = history.at(-1)?.t;
      if (stamp !== newest) { newest = stamp; lastChange = performance.now(); }
      status.textContent = !history.length ? '計測待ち' : performance.now() - lastChange > 8000
        ? '計測の更新待ち' : 'ホスト専用 · ' + interval + '秒間隔';
      render();
    } catch { status.textContent = '計測データの取得待ち'; }
    setTimeout(refresh, interval * 1000);
  }
  refresh();
}
