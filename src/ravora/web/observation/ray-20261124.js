const refreshEvery = 30_000;
const staleAfter = 5 * 60;
let inflight = false;
const snapshot = () => document.getElementById('raySnapshot');

function updateAge() {
  const view = snapshot();
  if (!view) return;
  const now = Date.now() / 1000;
  const age = Math.max(0, now - Number(view.dataset.collected));
  const status = view.querySelector('#status');
  const title = view.querySelector('#statusTitle');
  const detail = view.querySelector('#statusDetail');
  if (age > staleAfter) {
    status.dataset.state = 'stale';
    title.textContent = 'Snapshot is out of date';
    detail.textContent = 'Last successful collection ' + Math.floor(age / 60) + ' minutes ago. Counts are from that observation.';
  } else if (status.dataset.state === 'live' && now > Number(status.dataset.until)) {
    status.dataset.state = 'unknown';
    title.textContent = title.textContent.replace(' is live', ' was last seen live');
    detail.textContent += ' Awaiting a fresh provider sample.';
  }
}

async function refresh() {
  if (inflight || !snapshot()) return;
  inflight = true;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 8000);
  try {
    const url = new URL(window.location.href);
    url.hash = '';
    url.searchParams.set('ray_snapshot', String(Date.now()));
    const response = await fetch(url, {cache:'no-store', signal:controller.signal});
    if (!response.ok) return;
    const documentText = await response.text();
    const next = new DOMParser().parseFromString(documentText, 'text/html').getElementById('raySnapshot');
    const old = snapshot();
    if (!next || !old || !Number.isFinite(Number(next.dataset.collected)) ||
        Number(next.dataset.collected) <= Number(old.dataset.collected)) return;
    if (next.dataset.version !== old.dataset.version) {
      window.location.reload();
      return;
    }
    const open = new Set([...old.querySelectorAll('details[open][data-key]')].map(d => d.dataset.key));
    for (const d of next.querySelectorAll('details[data-key]')) d.open = open.has(d.dataset.key);
    const active = document.activeElement;
    const focusKey = active?.closest('summary')?.parentElement?.dataset.key;
    const focusId = active?.id;
    old.replaceWith(document.importNode(next,true));
    if (focusKey) snapshot().querySelector('details[data-key="' + CSS.escape(focusKey) + '"] > summary')?.focus({preventScroll:true});
    else if (focusId) document.getElementById(focusId)?.focus({preventScroll:true});
  } catch {
    // Continue showing the dated snapshot until the next successful collection.
  } finally {
    clearTimeout(timeout);
    inflight = false;
    updateAge();
  }
}

document.addEventListener('click', event => {
  if (event.target.closest?.('#refresh') && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey) {
    event.preventDefault();
    refresh();
  }
});
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
updateAge();
refresh();
setInterval(() => { if (!document.hidden) refresh(); },refreshEvery);
setInterval(updateAge,5000);
