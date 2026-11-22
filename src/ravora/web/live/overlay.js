// The broadcast compositor supplies these same pixels to HEVC. Keep at most
// one preview request outstanding and preserve the last decoded frame on jitter.
const screen = document.querySelector('#screen');
const context = screen.getContext('2d', {alpha:false});
const status = document.querySelector('#screen-status');
let revision = '', lastSuccess = performance.now();
async function refresh() {
  const started = performance.now();
  try {
    const response = await fetch('/overlay/frame.jpg', {
      headers:{'X-Overlay-Revision':revision}, cache:'no-store', signal:AbortSignal.timeout(3200)
    });
    if (!response.ok) throw new Error('frame');
    if (response.status === 200) {
      const bitmap = await createImageBitmap(await response.blob());
      try { context.drawImage(bitmap,0,0); } finally { bitmap.close(); }
      revision = response.headers.get('X-Overlay-Revision') ?? '';
      status.hidden = true;
    }
    lastSuccess = performance.now();
  } catch {
    if (performance.now()-lastSuccess > 3000) {
      context.fillStyle = '#15110f'; context.fillRect(0,0,screen.width,screen.height);
      status.textContent = '配信画面に再接続中…'; status.hidden = false; revision = '';
    }
  }
  setTimeout(refresh, document.hidden ? 750 : Math.max(0, 1000/30 - (performance.now()-started)));
}
refresh();
