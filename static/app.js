'use strict';
/* Visualase GIF Extractor - front end (no external libraries). */

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const SERIES = ['#e6194b', '#3cb44b', '#4363d8', '#f58231', '#911eb4', '#0aa5c2', '#d0309c', '#9a6324', '#6b8e23', '#1f2a7a'];
const C = { fiber: '#ffdd00', center: '#00e5ff', tip: '#ff4040', entry: '#2fd36b', point: '#ff50ff' };

const state = {
  jobs: [],
  current: null,        // selected job id
  data: null,           // per-frame payload for the selected job
  fibers: {},           // slice idx -> {entry:[x,y], tip:[x,y], dirty, source}
  anat: {},             // slice idx -> HTMLImageElement
  tab: 'fiber',
  placing: null,        // slice idx while "Place by clicking" is waiting for clicks
  poll: null,
  viz: { frame: 0, playing: false, timer: null, images: new Map(), show: {}, hidden: new Set(), scale: 2 },
};

/* ------------------------------------------------------------------ helpers */
async function api(path, opts = {}) {
  const o = { ...opts };
  if (o.json !== undefined) {
    o.method = o.method || 'POST';
    o.headers = { 'Content-Type': 'application/json' };
    o.body = JSON.stringify(o.json);
    delete o.json;
  }
  const r = await fetch(path, o);
  const ct = r.headers.get('content-type') || '';
  const body = ct.includes('json') ? await r.json() : null;
  if (!r.ok) throw new Error((body && body.error) || `${r.status} ${r.statusText}`);
  return body;
}

let toastTimer = null;
function toast(msg, isErr = false) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast show' + (isErr ? ' err' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = 'toast'), isErr ? 5000 : 2600);
}

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmt1 = (v) => (v === null || v === undefined || Number.isNaN(v) ? '–' : Number(v).toFixed(1));
const job = () => state.jobs.find((j) => j.id === state.current) || null;

function pixelMm() {
  const v = parseFloat($('#pixelMm').value);
  return v > 0 ? v : 1.015625;
}
function store(key, val) { try { localStorage.setItem(key, val); } catch (e) { /* private mode */ } }
function load(key) { try { return localStorage.getItem(key); } catch (e) { return null; } }

function pointName(key, rows) {
  const [row, lab] = key.split(':').map(Number);
  if (!rows || rows < 2) return `Point ${lab}`;
  return `Point ${lab} (${row === 0 ? 'top' : 'bottom'})`;
}
function seriesColor(key) {
  const keys = state.data ? state.data.point_keys : [];
  const i = Math.max(0, keys.indexOf(key));
  return SERIES[i % SERIES.length];
}
function geom(sidx) {
  const f = state.fibers[sidx];
  if (!f || !f.entry || !f.tip) return null;
  const E = f.entry, T = f.tip;
  return { E, T, C: [(E[0] + T[0]) / 2, (E[1] + T[1]) / 2] };
}
const dist = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1]);

/* ------------------------------------------------------------------ files */
async function uploadFiles(list) {
  const files = [...list].filter((f) => /\.gif$/i.test(f.name));
  if (!files.length) { toast('Please choose .gif files', true); return; }
  const fd = new FormData();
  files.forEach((f) => fd.append('files', f));
  toast(`Uploading ${files.length} file${files.length > 1 ? 's' : ''}…`);
  try {
    const res = await api('/api/upload', { method: 'POST', body: fd });
    const bad = res.jobs.filter((j) => j.status === 'error');
    if (bad.length) toast(`${bad.length} file(s) skipped: ${bad[0].error}`, true);
    await refreshJobs();
    const first = res.jobs.find((j) => j.id);
    if (first && (!state.current || !job())) selectJob(first.id);
  } catch (e) { toast(e.message, true); }
}

async function refreshJobs() {
  const { jobs } = await api('/api/jobs');
  const prev = job();
  state.jobs = jobs;
  renderJobList();
  const cur = job();
  if (cur) {
    renderHeader(cur);
    if (prev && prev.status !== 'ready' && cur.status === 'ready') await loadJobData();
  }
  const busy = jobs.some((j) => j.status === 'queued' || j.status === 'analyzing');
  clearTimeout(state.poll);
  if (busy) state.poll = setTimeout(refreshJobs, 700);
  $('#dlAll').disabled = !jobs.some((j) => j.outputs && (j.outputs.xlsx || j.outputs.csv));
}

function statusChip(j) {
  if (j.status === 'ready') return '<span class="chip ok">Ready</span>';
  if (j.status === 'error') return '<span class="chip err">Error</span>';
  if (j.status === 'analyzing') return `<span class="chip info">Analyzing ${Math.round(j.progress * 100)}%</span>`;
  return '<span class="chip">Queued</span>';
}

function renderJobList() {
  const ul = $('#jobList');
  if (!state.jobs.length) { ul.innerHTML = '<li class="muted small">No files yet</li>'; return; }
  ul.innerHTML = state.jobs.map((j) => `
    <li class="job ${j.id === state.current ? 'active' : ''}" data-id="${j.id}" tabindex="0">
      <button class="x" data-del="${j.id}" title="Remove from list" aria-label="Remove">&times;</button>
      <div class="name">${esc(j.filename)}</div>
      <div class="meta">${statusChip(j)}${j.n_frames ? `<span>${j.n_frames} frames</span>` : ''}
        ${j.status === 'ready' ? `<span>${j.point_keys.length} points</span>` : ''}
        ${j.outputs && (j.outputs.xlsx || j.outputs.csv) ? '<span class="chip ok">exported</span>' : ''}</div>
      ${j.status === 'analyzing' || j.status === 'queued' ? `<div class="bar"><i style="width:${Math.round(j.progress * 100)}%"></i></div>` : ''}
    </li>`).join('');
}

async function selectJob(id) {
  stopPlay();
  state.current = id;
  state.data = null;
  state.fibers = {};
  state.anat = {};
  state.viz.images = new Map();
  state.viz.frame = 0;
  $('#results').hidden = true;
  $('#gLink').hidden = true;
  $('#gProg').hidden = true;
  renderJobList();
  const j = job();
  $('#welcome').hidden = !!j;
  $('#jobView').hidden = !j;
  if (!j) return;
  renderHeader(j);
  if (j.status === 'ready') await loadJobData();
}

function renderHeader(j) {
  $('#jvName').textContent = j.filename;
  const facts = [];
  if (j.status === 'ready') {
    facts.push(`${j.n_frames} frames`, `${j.runs} run${j.runs === 1 ? '' : 's'}`, `${j.laser_on_frames} laser-on frames`,
      `${j.width}×${j.height}px · ${j.rows} panel row${j.rows > 1 ? 's' : ''}`, `${j.slices.length} slice${j.slices.length === 1 ? '' : 's'}`,
      j.point_keys.length ? `points: ${j.point_keys.map((k) => k.split(':')[1]).join(', ')}` : 'no numbered points');
  }
  $('#jvFacts').innerHTML = j.status === 'error'
    ? `<span class="chip err">Analysis failed: ${esc(j.error)}</span>`
    : facts.map((f) => `<span class="chip">${esc(f)}</span>`).join('');
  const prog = $('#jvProgress');
  prog.hidden = j.status === 'ready' || j.status === 'error';
  if (!prog.hidden) {
    $('i', prog).style.width = `${Math.round(j.progress * 100)}%`;
    $('span', prog).textContent = j.message || '';
  }
  $$('.tabs button').forEach((b) => (b.disabled = j.status !== 'ready'));
  $$('.tabpanel').forEach((p) => (p.style.visibility = j.status === 'ready' ? 'visible' : 'hidden'));
}

async function loadJobData() {
  const j = job();
  if (!j) return;
  const id = j.id;
  const data = await api(`/api/jobs/${id}/frames`);
  if (state.current !== id) return;
  state.data = data;
  j.slices.forEach((s) => {
    state.fibers[s.idx] = s.fiber ? { entry: s.fiber.entry, tip: s.fiber.tip, dirty: false, source: s.fiber_source }
      : { entry: null, tip: null, dirty: false, source: null };
  });
  renderFiberCards();
  renderOutputOptions();
  initViz();
  setTab(state.tab);
}

function setTab(t) {
  state.tab = t;
  $$('.tabs button').forEach((b) => b.classList.toggle('active', b.dataset.tab === t));
  $$('.tabpanel').forEach((p) => (p.hidden = p.dataset.panel !== t));
  if (t === 'viz') { drawFrame(); drawChart(); } else stopPlay();
  if (t === 'fiber') Object.keys(state.fibers).forEach((i) => drawFiberCanvas(+i));
}

/* ------------------------------------------------------------------ fiber review */
function sourceChip(f) {
  if (f.dirty) return '<span class="chip warn">Unsaved changes</span>';
  if (f.source === 'saved') return '<span class="chip ok">Remembered</span>';
  if (f.source === 'manual') return '<span class="chip ok">Saved</span>';
  if (f.source === 'auto') return '<span class="chip warn">Auto – please check</span>';
  return '<span class="chip err">Not set</span>';
}

function slicePoints(sidx, row) {
  const seen = new Map();
  for (const f of state.data.frames) {
    if (f.slice[String(row)] !== sidx) continue;
    for (const [k, v] of Object.entries(f.points)) {
      if (+k.split(':')[0] !== row || v[2] === null) continue;
      seen.set(`${k}@${v[2]},${v[3]}`, { key: k, x: v[2], y: v[3] });
    }
  }
  return [...seen.values()];
}

function renderFiberCards() {
  const j = job();
  const wrap = $('#sliceCards');
  wrap.innerHTML = j.slices.map((s) => `
    <div class="card" data-idx="${s.idx}">
      <div class="ch"><input type="text" value="${esc(s.label)}" aria-label="Slice name" maxlength="60">
        <span class="src">${sourceChip(state.fibers[s.idx])}</span></div>
      <canvas class="fcanvas" width="768" height="768"></canvas>
      <div class="coords"></div>
      <div class="btns">
        <button class="btn small" data-act="full" title="Open this slice full screen for precise clicking (or double-click the image)">&#x26F6; Full screen</button>
        <button class="btn small" data-act="place">Place by clicking</button>
        <button class="btn small" data-act="snap" title="Fit the line exactly onto the dark fiber">Snap to line</button>
        <button class="btn small" data-act="auto">Auto-detect</button>
        <button class="btn small primary" data-act="save">Save</button>
      </div>
      <div class="small muted">${j.rows > 1 ? (s.row === 0 ? 'Top' : 'Bottom') + ' panel · ' : ''}frames ${s.first_frame}–${s.last_frame} (${s.n_frames})</div>
    </div>`).join('');
  j.slices.forEach((s) => {
    const card = $(`.card[data-idx="${s.idx}"]`, wrap);
    const img = new Image();
    img.onload = () => drawFiberCanvas(s.idx);
    img.src = `/api/jobs/${j.id}/slice/${s.idx}/anatomy.png`;
    state.anat[s.idx] = img;
    bindFiberCanvas(card, s);
    $('input', card).addEventListener('input', () => { state.fibers[s.idx].dirty = true; updateCardMeta(s.idx); });
    $$('.btns button', card).forEach((b) => b.addEventListener('click', () => fiberAction(s.idx, b.dataset.act, b)));
    updateCardMeta(s.idx);
  });
}

function updateCardMeta(idx) {
  if (fs.idx === idx) { drawFs(); updateFsMeta(); }
  const card = $(`.card[data-idx="${idx}"]`);
  if (!card) return;
  const f = state.fibers[idx];
  $('.src', card).innerHTML = sourceChip(f);
  const g = geom(idx);
  $('.coords', card).innerHTML = g ? `
    <span><b>Skull entry</b> (${fmt1(g.E[0])}, ${fmt1(g.E[1])})</span><span><b>Tip</b> (${fmt1(g.T[0])}, ${fmt1(g.T[1])})</span>
    <span><b>Center</b> (${fmt1(g.C[0])}, ${fmt1(g.C[1])})</span><span><b>Length in skull</b> ${fmt1(dist(g.E, g.T) * pixelMm())} mm</span>`
    : '<span>No fiber yet – press <b>Place by clicking</b>.</span>';
}

function drawFiberCanvas(idx) {
  const card = $(`.card[data-idx="${idx}"]`);
  const img = state.anat[idx];
  if (!card || !img || !img.complete) return;
  const cv = $('canvas', card);
  cv.classList.toggle('placing', state.placing === idx);
  const ctx = cv.getContext('2d');
  const k = cv.width / 256;
  ctx.imageSmoothingEnabled = false;
  ctx.drawImage(img, 0, 0, cv.width, cv.height);
  const s = job().slices[idx];
  // numbered points of this slice, as a guide
  ctx.font = `${Math.round(5 * k)}px sans-serif`;
  for (const p of slicePoints(idx, s.row)) {
    ctx.strokeStyle = C.point; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.arc((p.x + 0.5) * k, (p.y + 0.5) * k, 3 * k, 0, 7); ctx.stroke();
    ctx.fillStyle = C.point; ctx.fillText(p.key.split(':')[1], (p.x + 4) * k, (p.y - 2) * k);
  }
  const g = geom(idx);
  const f = state.fibers[idx];
  if (f.entry && !f.tip) { handle(ctx, f.entry, k, C.entry, 'E'); return; }
  if (!g) return;
  const P = (q) => [(q[0] + 0.5) * k, (q[1] + 0.5) * k];
  ctx.strokeStyle = C.fiber; ctx.lineWidth = 2;
  ctx.beginPath(); ctx.moveTo(...P(g.E)); ctx.lineTo(...P(g.T)); ctx.stroke();
  ctx.strokeStyle = C.center; ctx.lineWidth = 3;
  ctx.beginPath(); ctx.arc(...P(g.C), 2.2 * k, 0, 7); ctx.stroke();
  ctx.fillStyle = C.center; ctx.font = `bold ${Math.round(4.5 * k)}px sans-serif`;
  ctx.fillText('center', P(g.C)[0] + 3 * k, P(g.C)[1] - 2 * k);
  handle(ctx, g.E, k, C.entry, 'E');
  handle(ctx, g.T, k, C.tip, 'T');
}

function handle(ctx, q, k, color, letter) {
  const x = (q[0] + 0.5) * k, y = (q[1] + 0.5) * k;
  ctx.fillStyle = color; ctx.strokeStyle = '#000'; ctx.lineWidth = 2;
  ctx.beginPath(); ctx.arc(x, y, 3.2 * k, 0, 7); ctx.fill(); ctx.stroke();
  ctx.fillStyle = '#000'; ctx.font = `bold ${Math.round(4 * k)}px sans-serif`; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  ctx.fillText(letter, x, y + 0.5);
  ctx.textAlign = 'start'; ctx.textBaseline = 'alphabetic';
}

function bindFiberCanvas(card, s) {
  const cv = $('canvas', card);
  let drag = null;
  const toImg = (e) => {
    const r = cv.getBoundingClientRect();
    return [Math.min(255.9, Math.max(0, ((e.clientX - r.left) / r.width) * 256 - 0.5)),
            Math.min(255.9, Math.max(0, ((e.clientY - r.top) / r.height) * 256 - 0.5))];
  };
  cv.addEventListener('pointerdown', (e) => {
    const f = state.fibers[s.idx];
    const p = toImg(e);
    if (state.placing === s.idx) {
      placeClick(s.idx, p, true);
      return;
    }
    const g = geom(s.idx);
    if (!g) return;
    const tol = (14 / cv.getBoundingClientRect().width) * 256;
    const dE = dist(p, g.E), dT = dist(p, g.T);
    if (Math.min(dE, dT) <= tol) {
      drag = dE <= dT ? 'entry' : 'tip';
      cv.setPointerCapture(e.pointerId);
    }
  });
  cv.addEventListener('pointermove', (e) => {
    if (!drag) {
      const g = geom(s.idx);
      if (g && state.placing !== s.idx) {
        const p = toImg(e);
        const tol = (14 / cv.getBoundingClientRect().width) * 256;
        cv.style.cursor = Math.min(dist(p, g.E), dist(p, g.T)) <= tol ? 'grab' : 'crosshair';
      }
      return;
    }
    state.fibers[s.idx][drag] = toImg(e);
    state.fibers[s.idx].dirty = true;
    drawFiberCanvas(s.idx);
    updateCardMeta(s.idx);
  });
  const end = () => { drag = null; };
  cv.addEventListener('pointerup', end);
  cv.addEventListener('pointercancel', end);
  cv.addEventListener('dblclick', () => openFs(s.idx));
}

/* "Place by clicking": first click = skull entry, second click = tip. */
function placeClick(idx, p, autoSnap) {
  const f = state.fibers[idx];
  if (!f.entry || f.tip) {
    f.entry = p; f.tip = null;
    if (fs.idx !== idx) toast('Now click the fiber tip');
  } else {
    f.tip = p;
    state.placing = null;
    if (autoSnap) fiberAction(idx, 'snap');
  }
  f.dirty = true;
  drawFiberCanvas(idx); updateCardMeta(idx);
}

async function fiberAction(idx, act, btn) {
  const j = job();
  const f = state.fibers[idx];
  try {
    if (act === 'full') { openFs(idx); return; }
    if (act === 'place') {
      state.placing = idx;
      f.entry = null; f.tip = null;
      drawFiberCanvas(idx); updateCardMeta(idx);
      if (fs.idx !== idx) toast('Click the skull entry point, then the fiber tip');
      return;
    }
    if (btn) btn.disabled = true;
    if (act === 'snap') {
      if (!f.entry || !f.tip) { toast('Place the fiber first', true); return; }
      const r = await api(`/api/jobs/${j.id}/slice/${idx}/snap`, { json: { entry: f.entry, tip: f.tip } });
      Object.assign(f, r, { dirty: true });
    } else if (act === 'auto') {
      const r = await api(`/api/jobs/${j.id}/slice/${idx}/auto`, { method: 'POST' });
      Object.assign(f, r, { dirty: true, source: 'auto' });
      toast('Fiber detected automatically – please check it');
    } else if (act === 'save') {
      await saveFiber(idx);
      toast('Fiber saved – it will be remembered for this GIF');
    }
    drawFiberCanvas(idx); updateCardMeta(idx);
    if (state.tab === 'viz') drawFrame();
  } catch (e) { toast(e.message, true); }
  finally { if (btn) btn.disabled = false; }
}

async function saveFiber(idx) {
  const j = job();
  const f = state.fibers[idx];
  if (!f.entry || !f.tip) return;
  const card = $(`.card[data-idx="${idx}"]`);
  const label = card ? $('input', card).value : undefined;
  const r = await api(`/api/jobs/${j.id}/slice/${idx}/fiber`, { method: 'PUT', json: { entry: f.entry, tip: f.tip, label, remember: true } });
  Object.assign(f, { dirty: false, source: 'manual' });
  const s = j.slices[idx];
  s.label = r.label; s.fiber = r.fiber; s.fiber_source = 'manual';
  updateCardMeta(idx);
}

async function saveDirtyFibers() {
  for (const [idx, f] of Object.entries(state.fibers)) if (f.dirty && f.entry && f.tip) await saveFiber(+idx);
}

/* ------------------------------------------------------------------ full-screen fiber editor */
// View transform: image pixel (u, v) is drawn at screen (ox + u*sx, oy + v*sy) in CSS pixels.
const fs = { idx: null, sx: 1, sy: 1, ox: 0, oy: 0, fit: 1, w: 0, h: 0, drag: null, sel: null };

function openFs(idx) {
  fs.idx = idx; fs.sel = null; fs.drag = null;
  const el = $('#fs');
  el.hidden = false;
  document.body.style.overflow = 'hidden';
  if (el.requestFullscreen && !document.fullscreenElement) el.requestFullscreen().catch(() => { /* overlay still fills the window */ });
  fitFs();
  updateFsMeta();
  requestAnimationFrame(fitFs);       // again once the full-screen layout has settled
}

function closeFs() {
  if (fs.idx === null) return;
  const idx = fs.idx;
  fs.idx = null;
  $('#fs').hidden = true;
  document.body.style.overflow = '';
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  drawFiberCanvas(idx);
  updateCardMeta(idx);
}

function fsSize() {
  const cv = $('#fsCanvas');
  const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth, h = cv.clientHeight;
  fs.w = w; fs.h = h;
  if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) {
    cv.width = Math.round(w * dpr);
    cv.height = Math.round(h * dpr);
  }
  return { w, h, dpr };
}

function fitFs() {
  if (fs.idx === null) return;
  const { w, h } = fsSize();
  if ($('#fsStretch').checked) {
    fs.sx = w / 256; fs.sy = h / 256; fs.ox = 0; fs.oy = 0;
  } else {
    const k = Math.min(w, h) / 256;
    fs.sx = fs.sy = k; fs.ox = (w - 256 * k) / 2; fs.oy = (h - 256 * k) / 2;
  }
  fs.fit = Math.min(fs.sx, fs.sy);
  drawFs();
}

/* Window resized: keep the zoom level and the image point at the centre of the view. */
function resizeFs() {
  if (fs.idx === null) return;
  const z = Math.min(fs.sx, fs.sy) / fs.fit;
  const c = fsImg(fs.w / 2, fs.h / 2);
  fitFs();
  if (z > 1.001) {
    zoomFs(z);
    const [x, y] = fsP(c);
    fs.ox += fs.w / 2 - x;
    fs.oy += fs.h / 2 - y;
    drawFs();
  }
}

function zoomFs(factor, mx, my) {
  const { w, h } = fsSize();
  if (mx === undefined) { mx = w / 2; my = h / 2; }
  const cur = Math.min(fs.sx, fs.sy) / fs.fit;
  const target = Math.min(16, Math.max(1, cur * factor));
  if (target === 1) { fitFs(); return; }
  const f = target / cur;
  fs.ox = mx - (mx - fs.ox) * f;
  fs.oy = my - (my - fs.oy) * f;
  fs.sx *= f; fs.sy *= f;
  drawFs();
}

const fsP = (q) => [fs.ox + (q[0] + 0.5) * fs.sx, fs.oy + (q[1] + 0.5) * fs.sy];
const fsImg = (mx, my) => [Math.min(255.9, Math.max(0, (mx - fs.ox) / fs.sx - 0.5)),
                            Math.min(255.9, Math.max(0, (my - fs.oy) / fs.sy - 0.5))];
function fsMouse(e) {
  const r = $('#fsCanvas').getBoundingClientRect();
  return [e.clientX - r.left, e.clientY - r.top];
}

function drawFs() {
  if (fs.idx === null) return;
  const idx = fs.idx;
  const cv = $('#fsCanvas');
  const { w, h, dpr } = fsSize();
  const ctx = cv.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = '#000';
  ctx.fillRect(0, 0, w, h);
  const img = state.anat[idx];
  if (img && img.complete) {
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(img, fs.ox, fs.oy, 256 * fs.sx, 256 * fs.sy);
  }
  if (Math.min(fs.sx, fs.sy) >= 10) {          // pixel grid once single pixels are big enough to see
    const u0 = Math.max(0, Math.floor(-fs.ox / fs.sx)), u1 = Math.min(256, Math.ceil((w - fs.ox) / fs.sx));
    const v0 = Math.max(0, Math.floor(-fs.oy / fs.sy)), v1 = Math.min(256, Math.ceil((h - fs.oy) / fs.sy));
    ctx.strokeStyle = 'rgba(255,255,255,.09)'; ctx.lineWidth = 1;
    ctx.beginPath();
    for (let u = u0; u <= u1; u++) { const x = Math.round(fs.ox + u * fs.sx) + 0.5; ctx.moveTo(x, fs.oy + v0 * fs.sy); ctx.lineTo(x, fs.oy + v1 * fs.sy); }
    for (let v = v0; v <= v1; v++) { const y = Math.round(fs.oy + v * fs.sy) + 0.5; ctx.moveTo(fs.ox + u0 * fs.sx, y); ctx.lineTo(fs.ox + u1 * fs.sx, y); }
    ctx.stroke();
  }
  const s = job().slices[idx];
  if ($('#fsPoints').checked) {                 // point labels as small fixed-size guides
    ctx.font = '600 12px sans-serif';
    ctx.globalAlpha = 0.85;
    for (const p of slicePoints(idx, s.row)) {
      const [x, y] = fsP([p.x, p.y]);
      ctx.strokeStyle = C.point; ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.arc(x, y, 6, 0, 7); ctx.stroke();
      ctx.fillStyle = C.point; ctx.fillText(p.key.split(':')[1], x + 8, y - 5);
    }
    ctx.globalAlpha = 1;
  }
  const f = state.fibers[idx];
  const g = geom(idx);
  if (g) {
    ctx.strokeStyle = C.fiber; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(...fsP(g.E)); ctx.lineTo(...fsP(g.T)); ctx.stroke();
    const [cx, cy] = fsP(g.C);
    ctx.strokeStyle = C.center; ctx.lineWidth = 2.5;
    ctx.beginPath(); ctx.arc(cx, cy, 8, 0, 7); ctx.stroke();
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(cx - 14, cy); ctx.lineTo(cx + 14, cy); ctx.moveTo(cx, cy - 14); ctx.lineTo(cx, cy + 14); ctx.stroke();
    ctx.fillStyle = C.center; ctx.font = 'bold 13px sans-serif'; ctx.fillText('center', cx + 12, cy - 12);
  }
  for (const [key, color, letter] of [['entry', C.entry, 'E'], ['tip', C.tip, 'T']]) {
    const q = f[key];
    if (!q) continue;
    const [x, y] = fsP(q);
    ctx.globalAlpha = 0.28; ctx.fillStyle = color;
    ctx.beginPath(); ctx.arc(x, y, 11, 0, 7); ctx.fill();
    ctx.globalAlpha = 1; ctx.strokeStyle = color; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.arc(x, y, 11, 0, 7); ctx.stroke();
    ctx.fillStyle = '#fff';                     // exact point
    ctx.beginPath(); ctx.arc(x, y, 1.8, 0, 7); ctx.fill();
    if (fs.sel === key) { ctx.strokeStyle = '#fff'; ctx.lineWidth = 2; ctx.beginPath(); ctx.arc(x, y, 15, 0, 7); ctx.stroke(); }
    ctx.fillStyle = color; ctx.font = 'bold 13px sans-serif'; ctx.textAlign = 'center';
    ctx.fillText(letter, x, y - 19);
    ctx.textAlign = 'start';
  }
  const hint = $('#fsHint');
  hint.hidden = state.placing !== idx;
  if (!hint.hidden) hint.textContent = !f.entry || f.tip ? 'Click the skull entry point' : 'Now click the fiber tip';
  $('#fsZoom').textContent = `${Math.round((Math.min(fs.sx, fs.sy) / fs.fit) * 100)}%`;
}

function updateFsMeta() {
  if (fs.idx === null) return;
  const idx = fs.idx;
  const card = $(`.card[data-idx="${idx}"]`);
  $('#fsTitle').textContent = card ? $('input', card).value : job().slices[idx].label;
  $('#fsSrc').innerHTML = sourceChip(state.fibers[idx]);
  const g = geom(idx);
  const xy = (q) => `${q[0].toFixed(1)}, ${q[1].toFixed(1)}`;
  $('#fsCoords').innerHTML = g
    ? `Entry (${xy(g.E)}) &middot; Tip (${xy(g.T)}) &middot; <b>Center (${xy(g.C)})</b> &middot; length ${fmt1(dist(g.E, g.T) * pixelMm())} mm`
    : 'No fiber yet &ndash; press <b>Place by clicking</b>';
}

function bindFs() {
  const cv = $('#fsCanvas');
  cv.addEventListener('pointerdown', (e) => {
    if (fs.idx === null) return;
    const idx = fs.idx;
    const f = state.fibers[idx];
    const [mx, my] = fsMouse(e);
    if (state.placing === idx) { placeClick(idx, fsImg(mx, my), false); return; }
    let hit = null, best = 16;
    for (const key of ['entry', 'tip']) {
      if (!f[key]) continue;
      const [x, y] = fsP(f[key]);
      const d = Math.hypot(x - mx, y - my);
      if (d <= best) { best = d; hit = key; }
    }
    cv.setPointerCapture(e.pointerId);
    if (hit) {
      const [x, y] = fsP(f[hit]);
      fs.sel = hit;
      fs.drag = { type: 'handle', key: hit, dx: x - mx, dy: y - my };   // keep the grab offset: no jump
    } else {
      fs.sel = null;
      fs.drag = { type: 'pan', mx, my, ox: fs.ox, oy: fs.oy };
      cv.style.cursor = 'grabbing';
    }
    drawFs();
  });
  cv.addEventListener('pointermove', (e) => {
    if (fs.idx === null) return;
    const [mx, my] = fsMouse(e);
    const p = fsImg(mx, my);
    $('#fsCursor').textContent = `cursor x ${p[0].toFixed(1)}, y ${p[1].toFixed(1)}`;
    const d = fs.drag;
    if (!d) {
      const f = state.fibers[fs.idx];
      const near = ['entry', 'tip'].some((k) => f[k] && Math.hypot(fsP(f[k])[0] - mx, fsP(f[k])[1] - my) <= 16);
      cv.style.cursor = state.placing === fs.idx ? 'crosshair' : near ? 'grab' : 'crosshair';
      return;
    }
    if (d.type === 'pan') {
      fs.ox = d.ox + (mx - d.mx);
      fs.oy = d.oy + (my - d.my);
      drawFs();
    } else {
      const f = state.fibers[fs.idx];
      f[d.key] = fsImg(mx + d.dx, my + d.dy);
      f.dirty = true;
      drawFiberCanvas(fs.idx); updateCardMeta(fs.idx);
    }
  });
  const end = () => { fs.drag = null; cv.style.cursor = 'crosshair'; };
  cv.addEventListener('pointerup', end);
  cv.addEventListener('pointercancel', end);
  cv.addEventListener('wheel', (e) => {
    if (fs.idx === null) return;
    e.preventDefault();
    const [mx, my] = fsMouse(e);
    zoomFs(Math.exp(-e.deltaY * 0.0015), mx, my);
  }, { passive: false });
  $$('[data-fs]').forEach((b) => b.addEventListener('click', () => {
    const act = b.dataset.fs;
    if (fs.idx === null) return;
    if (act === 'close') closeFs();
    else if (act === 'zoomin') zoomFs(1.5);
    else if (act === 'zoomout') zoomFs(1 / 1.5);
    else if (act === 'fit') fitFs();
    else fiberAction(fs.idx, act, b);
  }));
  $('#fsStretch').addEventListener('change', () => { store('vx.fsStretch', $('#fsStretch').checked ? '1' : ''); fitFs(); });
  $('#fsPoints').addEventListener('change', drawFs);
  $('#fsStretch').checked = load('vx.fsStretch') === '1';
  document.addEventListener('fullscreenchange', () => {
    if (!document.fullscreenElement && fs.idx !== null) closeFs();
  });
  window.addEventListener('resize', resizeFs);
  document.addEventListener('keydown', (e) => {
    if (fs.idx === null) return;
    const f = state.fibers[fs.idx];
    if (e.key === 'Escape') { e.preventDefault(); if (state.placing === fs.idx) { state.placing = null; drawFs(); } else closeFs(); return; }
    if (e.key === '+' || e.key === '=') { e.preventDefault(); zoomFs(1.5); return; }
    if (e.key === '-' || e.key === '_') { e.preventDefault(); zoomFs(1 / 1.5); return; }
    if (e.key === '0') { e.preventDefault(); fitFs(); return; }
    const step = e.shiftKey ? 2 : 0.5;
    const mv = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[e.key];
    if (mv && fs.sel && f[fs.sel]) {
      e.preventDefault();
      const q = f[fs.sel];
      f[fs.sel] = [Math.min(255.9, Math.max(0, q[0] + mv[0])), Math.min(255.9, Math.max(0, q[1] + mv[1]))];
      f.dirty = true;
      drawFiberCanvas(fs.idx); updateCardMeta(fs.idx);
    }
  });
}

/* ------------------------------------------------------------------ output */
function renderOutputOptions() {
  const j = job();
  $('#pointChecks').innerHTML = state.data.point_keys.length
    ? state.data.point_keys.map((k) => `<label><input type="checkbox" name="pt" value="${k}" checked>
        <span class="dot" style="background:${seriesColor(k)}"></span>${esc(pointName(k, j.rows))}</label>`).join('')
    : '<span class="muted small">This GIF shows no numbered points – only time and laser data will be exported.</span>';
  $('#fStart').max = $('#fEnd').max = j.n_frames - 1;
  $('#fStart').value = 0;
  $('#fEnd').value = j.n_frames - 1;
  const o = j.outputs || {};
  $('#results').hidden = true;
  $('#runMsg').textContent = o.xlsx || o.csv ? 'Previously exported – run again to refresh.' : '';
}

async function runExport() {
  const j = job();
  const fields = $$('input[name=field]:checked').map((i) => i.value);
  const points = $$('input[name=pt]:checked').map((i) => i.value);
  const frames = $('input[name=frames]:checked').value;
  const fmt = $('input[name=fmt]:checked').value;
  if (!fields.length) { toast('Select at least one kind of data', true); return; }
  const btn = $('#runBtn');
  btn.disabled = true;
  $('#runMsg').innerHTML = '<span class="spin"></span> Working…';
  try {
    await saveDirtyFibers();
    const res = await api(`/api/jobs/${j.id}/export`, {
      json: { fields, points, frames, format: fmt, start: +$('#fStart').value, end: +$('#fEnd').value, pixel_mm: pixelMm() },
    });
    j.outputs = j.outputs || {};
    j.outputs[fmt] = { name: res.file, url: res.url };
    renderResults(res);
    $('#runMsg').textContent = `${res.n_frames} frames exported${fmt === 'xlsx' && !res.recalculated ? ' (Excel will compute the distance formulas when the file is opened)' : ''}.`;
    renderJobList();
    $('#dlAll').disabled = false;
    $('#readyText').textContent = `${res.file} – ${res.n_frames} frames, ${res.rows.length} point position${res.rows.length === 1 ? '' : 's'}. Download it now?`;
    $('#readyDl').href = res.url;
    $('#readyDlg').showModal();
  } catch (e) {
    $('#runMsg').textContent = '';
    toast(e.message, true);
  } finally { btn.disabled = false; }
}

function renderResults(res) {
  $('#results').hidden = false;
  $('#dlLink').href = res.url;
  $('#dlLink').textContent = `Download ${res.file}`;
  const rows = res.rows;
  $('#resTable').innerHTML = rows.length ? `
    <thead><tr><th>Slice</th><th>Point</th><th>Position (x, y)</th><th>Frames</th><th>Time</th>
      <th>To center (mm)</th><th>To tip (mm)</th><th>Result</th></tr></thead>
    <tbody>${rows.map((r) => `<tr>
      <td>${esc(r.slice)}</td><td><span class="dot" style="background:${seriesColor(r.key)}"></span>${r.point}</td>
      <td>(${r.x}, ${r.y})</td><td>${r.first_frame}–${r.last_frame} <span class="muted">(${r.n_frames})</span></td>
      <td>${esc(r.first_time || '–')} – ${esc(r.last_time || '–')}</td>
      <td class="num">${fmt1(r.dist_center_mm)}</td><td class="num">${fmt1(r.dist_tip_mm)}</td>
      <td class="result">${esc(r.result)}</td></tr>`).join('')}</tbody>`
    : '<tbody><tr><td class="muted">No numbered points in the exported frames.</td></tr></tbody>';
}

/* ------------------------------------------------------------------ visualize */
function initViz() {
  const d = state.data;
  const n = d.frames.length;
  $('#vSlider').max = n - 1;
  state.viz.frame = Math.min(state.viz.frame, n - 1);
  state.viz.scale = d.width <= 512 ? 2 : 1;
  const cv = $('#vCanvas');
  cv.width = d.width * state.viz.scale;
  cv.height = d.height * state.viz.scale;
  $$('.toggles input[data-show]').forEach((i) => (state.viz.show[i.dataset.show] = i.checked));
  state.viz.hidden = new Set();
  renderLegend();
  drawFrame();
}

function frameImage(i) {
  const cache = state.viz.images;
  if (cache.has(i)) return cache.get(i);
  const p = new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = reject;
    img.src = `/api/jobs/${state.current}/frame/${i}.png`;
  });
  cache.set(i, p);
  return p;
}

async function drawFrame() {
  if (!state.data) return;
  const i = state.viz.frame;
  const id = state.current;
  let img;
  try { img = await frameImage(i); } catch (e) { return; }
  if (state.current !== id || state.viz.frame !== i) return;
  for (let k = 1; k <= 4; k++) if (i + k < state.data.frames.length) frameImage(i + k);  // prefetch
  const cv = $('#vCanvas');
  const ctx = cv.getContext('2d');
  const s = state.viz.scale;
  ctx.imageSmoothingEnabled = false;
  ctx.drawImage(img, 0, 0, cv.width, cv.height);
  const f = state.data.frames[i];
  if (!$('#vOriginal').checked) drawOverlay(ctx, f, s);
  $('#vSlider').value = i;
  $('#vLabel').textContent = `Frame ${i} / ${state.data.frames.length - 1} · ${f.time || '--:--'} · run ${f.run}`;
  renderReadout(f);
  drawChart();
}

function drawOverlay(ctx, f, s) {
  const d = state.data;
  const show = state.viz.show;
  const cols = d.anat_col === 1 ? [0, 1] : [0];
  const mm = pixelMm();
  for (let row = 0; row < d.rows; row++) {
    const sidx = f.slice[String(row)];
    const g = sidx === undefined ? null : geom(sidx);
    for (const col of cols) {
      const ox = col * 256, oy = row * 256;
      const P = (q) => [(q[0] + ox + 0.5) * s, (q[1] + oy + 0.5) * s];
      if (g && show.fiber) {
        ctx.strokeStyle = C.fiber; ctx.lineWidth = Math.max(1, s);
        ctx.beginPath(); ctx.moveTo(...P(g.E)); ctx.lineTo(...P(g.T)); ctx.stroke();
      }
      if (col === 0 && show.points) {
        const labels = [];
        for (const [key, v] of Object.entries(f.points)) {
          if (+key.split(':')[0] !== row || v[2] === null || state.viz.hidden.has(key)) continue;
          const q = [v[2], v[3]];
          const [x, y] = P(q);
          if (g && show.lines) {
            ctx.strokeStyle = 'rgba(255,255,255,.85)'; ctx.lineWidth = 1; ctx.setLineDash([4, 3]);
            ctx.beginPath(); ctx.moveTo(...P(g.C)); ctx.lineTo(x, y); ctx.stroke(); ctx.setLineDash([]);
          }
          ctx.strokeStyle = seriesColor(key); ctx.lineWidth = 2;
          ctx.beginPath(); ctx.arc(x, y, 5 * s, 0, 7); ctx.stroke();
          if (show.labels) labels.push({ key, x, y, txt: g ? `${key.split(':')[1]}: ${(dist(q, g.C) * mm).toFixed(1)} mm` : key.split(':')[1] });
        }
        drawLabels(ctx, labels, s);
      }
      if (g) {
        for (const [q, color, on] of [[g.T, C.tip, show.tip], [g.C, C.center, show.center]]) {
          if (!on) continue;
          ctx.strokeStyle = color; ctx.lineWidth = Math.max(2, s);
          ctx.beginPath(); ctx.arc(...P(q), 3.2 * s, 0, 7); ctx.stroke();
        }
      }
    }
  }
}

/* Place point labels beside their points, nudging them up/down so they never overlap. */
function drawLabels(ctx, labels, s) {
  ctx.font = `bold ${6 * s}px sans-serif`;
  const h = 7.5 * s;
  const placed = [];
  labels.sort((a, b) => a.y - b.y);
  for (const L of labels) {
    const w = ctx.measureText(L.txt).width + 4;
    const x0 = L.x + 6 * s;
    let y0 = L.y - h / 2;
    for (const k of [0, 1, -1, 2, -2, 3, -3, 4, -4, 5]) {
      const cand = L.y - h / 2 + k * (h + 1);
      if (!placed.some((p) => x0 < p.x + p.w && p.x < x0 + w && cand < p.y + h && p.y < cand + h)) { y0 = cand; break; }
    }
    placed.push({ x: x0, y: y0, w });
    if (Math.abs(y0 - (L.y - h / 2)) > 1) {
      ctx.strokeStyle = seriesColor(L.key); ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(L.x + 4 * s, L.y); ctx.lineTo(x0, y0 + h / 2); ctx.stroke();
    }
    ctx.fillStyle = 'rgba(0,0,0,.78)'; ctx.fillRect(x0 - 2, y0, w, h);
    ctx.fillStyle = seriesColor(L.key); ctx.fillRect(x0 - 2, y0, 2, h);
    ctx.fillStyle = '#fff'; ctx.fillText(L.txt, x0 + 1, y0 + h - 1.6 * s);
  }
}

function renderReadout(f) {
  const laser = f.laser === 'On'
    ? `<span class="laser-on">Laser ON · ${f.power ?? '–'}% · ${f.watts ?? '–'} W</span>`
    : f.laser === 'Off' ? 'Laser off' : '<span class="muted">Laser status not readable</span>';
  $('#vStatus').innerHTML = `<b>${f.time || '--:--'}</b> · run ${f.run} · ${laser}${f.notes.length ? `<div class="small muted">${esc(f.notes.join('; '))}</div>` : ''}`;
  const rows = state.data.rows;
  const keys = Object.keys(f.points);
  $('#vTable').innerHTML = keys.length ? `<tr><th>Point</th><th>Temp</th><th>Limit</th><th>Center</th><th>Tip</th></tr>` +
    keys.map((k) => {
      const v = f.points[k];
      const g = geom(f.slice[k.split(':')[0]]);
      const q = v[2] === null ? null : [v[2], v[3]];
      const dc = g && q ? dist(q, g.C) * pixelMm() : null;
      const dt = g && q ? dist(q, g.T) * pixelMm() : null;
      return `<tr><td><span class="dot" style="background:${seriesColor(k)}"></span>${esc(pointName(k, rows).replace('Point ', ''))}</td>
        <td class="num${v[0] !== null && v[0] >= v[1] ? ' over' : ''}" ${v[0] !== null && v[0] >= v[1] ? 'title="At or over its limit"' : ''}>${v[0] === null ? '--.-' : v[0].toFixed(1)}°</td><td class="num">&lt;${v[1]}</td>
        <td class="num">${fmt1(dc)}</td><td class="num">${fmt1(dt)}</td></tr>`;
    }).join('') + '<tr><td colspan="5" class="small muted">distances in mm</td></tr>'
    : '<tr><td class="muted small">No numbered points on this frame.</td></tr>';
}

function play() {
  if (!state.data) return;
  state.viz.playing = true;
  $('#vPlay').innerHTML = '&#10074;&#10074;';
  const step = () => {
    if (!state.viz.playing) return;
    const n = state.data.frames.length;
    state.viz.frame = (state.viz.frame + 1) % n;
    drawFrame();
    const dur = (state.data.durations[state.viz.frame] || 600) / parseFloat($('#vSpeed').value);
    state.viz.timer = setTimeout(step, dur);
  };
  state.viz.timer = setTimeout(step, 50);
}
function stopPlay() {
  state.viz.playing = false;
  clearTimeout(state.viz.timer);
  $('#vPlay').innerHTML = '&#9654;';
}
function seek(i) {
  if (!state.data) return;
  state.viz.frame = Math.max(0, Math.min(state.data.frames.length - 1, i));
  drawFrame();
}

/* chart */
function renderLegend() {
  const d = state.data;
  $('#vLegend').innerHTML = d.point_keys.map((k) => `<label><input type="checkbox" data-series="${k}" ${state.viz.hidden.has(k) ? '' : 'checked'}>
    <span class="dot" style="background:${seriesColor(k)}"></span>${esc(pointName(k, d.rows))}</label>`).join('')
    || '<span class="muted">No numbered points in this GIF.</span>';
}

function chartGeom(cv) {
  const pad = { l: 44, r: 12, t: 10, b: 26 };
  return { pad, w: cv.clientWidth - pad.l - pad.r, h: cv.clientHeight - pad.t - pad.b };
}

function drawChart() {
  const cv = $('#vChart');
  if (!state.data || cv.offsetParent === null) return;
  const dpr = window.devicePixelRatio || 1;
  cv.width = cv.clientWidth * dpr;
  cv.height = cv.clientHeight * dpr;
  const ctx = cv.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cv.clientWidth, cv.clientHeight);
  const { pad, w, h } = chartGeom(cv);
  const fr = state.data.frames;
  const n = fr.length;
  const keys = state.data.point_keys.filter((k) => !state.viz.hidden.has(k));
  let lo = Infinity, hi = -Infinity;
  for (const f of fr) for (const k of keys) { const v = f.points[k]; if (v && v[0] !== null) { lo = Math.min(lo, v[0]); hi = Math.max(hi, v[0]); } }
  if (!Number.isFinite(lo)) { lo = 30; hi = 60; }
  lo = Math.floor((lo - 2) / 5) * 5; hi = Math.ceil((hi + 2) / 5) * 5;
  const X = (i) => pad.l + (n > 1 ? (i / (n - 1)) * w : w / 2);
  const Y = (t) => pad.t + h - ((t - lo) / (hi - lo)) * h;
  // laser-on shading and run boundaries
  ctx.fillStyle = 'rgba(255, 64, 64, .10)';
  let start = null;
  for (let i = 0; i <= n; i++) {
    const on = i < n && fr[i].laser === 'On';
    if (on && start === null) start = i;
    if (!on && start !== null) { ctx.fillRect(X(start) - 0.5, pad.t, Math.max(1, X(i - 1) - X(start) + 1), h); start = null; }
  }
  ctx.strokeStyle = '#9aa7b4'; ctx.setLineDash([4, 4]); ctx.lineWidth = 1;
  for (let i = 1; i < n; i++) if (fr[i].run !== fr[i - 1].run) { ctx.beginPath(); ctx.moveTo(X(i), pad.t); ctx.lineTo(X(i), pad.t + h); ctx.stroke(); }
  ctx.setLineDash([]);
  // axes
  ctx.strokeStyle = '#dde3ea'; ctx.fillStyle = '#5c6b7a'; ctx.font = '11px sans-serif'; ctx.textAlign = 'right';
  const stepT = (hi - lo) > 40 ? 10 : 5;
  for (let t = lo; t <= hi; t += stepT) {
    ctx.beginPath(); ctx.moveTo(pad.l, Y(t)); ctx.lineTo(pad.l + w, Y(t)); ctx.stroke();
    ctx.fillText(`${t}°`, pad.l - 6, Y(t) + 4);
  }
  ctx.textAlign = 'center';
  const stepF = Math.max(1, Math.round(n / 8 / 10) * 10 || 1);
  for (let i = 0; i < n; i += stepF) ctx.fillText(String(i), X(i), pad.t + h + 16);
  ctx.textAlign = 'start';
  // series
  for (const k of keys) {
    ctx.strokeStyle = seriesColor(k); ctx.lineWidth = 1.8;
    ctx.beginPath();
    let pen = false;
    fr.forEach((f, i) => {
      const v = f.points[k];
      if (!v || v[0] === null) { pen = false; return; }
      if (!pen) { ctx.moveTo(X(i), Y(v[0])); pen = true; } else ctx.lineTo(X(i), Y(v[0]));
    });
    ctx.stroke();
  }
  // cursor
  const i = state.viz.frame;
  ctx.strokeStyle = '#17202b'; ctx.lineWidth = 1.5;
  ctx.beginPath(); ctx.moveTo(X(i), pad.t); ctx.lineTo(X(i), pad.t + h); ctx.stroke();
}

/* ------------------------------------------------------------------ annotated gif */
async function makeGif() {
  const j = job();
  const btn = $('#gBtn');
  btn.disabled = true;
  $('#gLink').hidden = true;
  const prog = $('#gProg');
  prog.hidden = false;
  $('i', prog).style.width = '0%';
  try {
    await saveDirtyFibers();
    const { task } = await api(`/api/jobs/${j.id}/annotated`, {
      json: { scale: +$('#gScale').value, step: +$('#gStep').value, show: state.viz.show, pixel_mm: pixelMm() },
    });
    for (;;) {
      await new Promise((r) => setTimeout(r, 500));
      const t = await api(`/api/tasks/${task}`);
      $('i', prog).style.width = `${Math.round(t.progress * 100)}%`;
      if (t.status === 'done') {
        const a = $('#gLink');
        a.href = t.url; a.hidden = false; a.textContent = `Download ${t.name}`;
        toast('Annotated GIF ready');
        break;
      }
      if (t.status === 'error') throw new Error(t.error);
    }
  } catch (e) { toast(e.message, true); }
  finally { btn.disabled = false; prog.hidden = true; }
}

async function downloadAll() {
  try {
    const r = await fetch('/api/download_all', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}) });
    if (!r.ok) throw new Error((await r.json()).error);
    const blob = await r.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'visualase_results.zip';
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  } catch (e) { toast(e.message, true); }
}

/* ------------------------------------------------------------------ wiring */
function wire() {
  bindFs();
  const drop = $('#drop');
  const input = $('#fileInput');
  drop.addEventListener('click', () => input.click());
  drop.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); input.click(); } });
  input.addEventListener('change', () => { uploadFiles(input.files); input.value = ''; });
  ['dragenter', 'dragover'].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add('over'); }));
  ['dragleave', 'drop'].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove('over'); }));
  drop.addEventListener('drop', (e) => uploadFiles(e.dataTransfer.files));
  document.addEventListener('dragover', (e) => e.preventDefault());
  document.addEventListener('drop', (e) => { if (!drop.contains(e.target)) { e.preventDefault(); if (e.dataTransfer.files.length) uploadFiles(e.dataTransfer.files); } });

  $('#jobList').addEventListener('click', async (e) => {
    const del = e.target.closest('[data-del]');
    if (del) {
      e.stopPropagation();
      await api(`/api/jobs/${del.dataset.del}`, { method: 'DELETE' });
      if (state.current === del.dataset.del) { state.current = null; selectJob(null); }
      refreshJobs();
      return;
    }
    const li = e.target.closest('.job');
    if (li && li.dataset.id !== state.current) selectJob(li.dataset.id);
  });
  $('#jobList').addEventListener('keydown', (e) => { const li = e.target.closest('.job'); if (li && e.key === 'Enter') selectJob(li.dataset.id); });
  $('#dlAll').addEventListener('click', downloadAll);

  $$('.tabs button').forEach((b) => b.addEventListener('click', () => setTab(b.dataset.tab)));

  const px = load('vx.pixelMm');
  if (px) $('#pixelMm').value = px;
  $('#pixelMm').addEventListener('change', () => {
    store('vx.pixelMm', $('#pixelMm').value);
    Object.keys(state.fibers).forEach((i) => updateCardMeta(+i));
    drawFrame();
  });

  $$('[data-pts]').forEach((a) => a.addEventListener('click', (e) => {
    e.preventDefault();
    $$('input[name=pt]').forEach((i) => (i.checked = a.dataset.pts === 'all'));
  }));
  $$('#fStart, #fEnd').forEach((i) => i.addEventListener('focus', () => ($('input[name=frames][value=range]').checked = true)));
  $('#runBtn').addEventListener('click', runExport);
  $('#readyLater').addEventListener('click', () => $('#readyDlg').close());
  $('#readyDl').addEventListener('click', () => setTimeout(() => $('#readyDlg').close(), 100));

  $('#vPlay').addEventListener('click', () => (state.viz.playing ? stopPlay() : play()));
  $('#vPrev').addEventListener('click', () => { stopPlay(); seek(state.viz.frame - 1); });
  $('#vNext').addEventListener('click', () => { stopPlay(); seek(state.viz.frame + 1); });
  $('#vSlider').addEventListener('input', (e) => { stopPlay(); seek(+e.target.value); });
  $$('.toggles input[data-show]').forEach((i) => i.addEventListener('change', () => { state.viz.show[i.dataset.show] = i.checked; drawFrame(); }));
  $('#vOriginal').addEventListener('change', drawFrame);
  $('#vLegend').addEventListener('change', (e) => {
    const k = e.target.dataset.series;
    if (!k) return;
    if (e.target.checked) state.viz.hidden.delete(k); else state.viz.hidden.add(k);
    drawFrame();
  });
  $('#vChart').addEventListener('click', (e) => {
    if (!state.data) return;
    const cv = e.currentTarget;
    const { pad, w } = chartGeom(cv);
    const r = cv.getBoundingClientRect();
    const t = (e.clientX - r.left - pad.l) / w;
    stopPlay();
    seek(Math.round(t * (state.data.frames.length - 1)));
  });
  $('#gBtn').addEventListener('click', makeGif);
  document.addEventListener('keydown', (e) => {
    if (state.tab !== 'viz' || !state.data || /INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName) && document.activeElement.type !== 'range') return;
    if (e.key === ' ') { e.preventDefault(); state.viz.playing ? stopPlay() : play(); }
    if (e.key === 'ArrowRight') { e.preventDefault(); stopPlay(); seek(state.viz.frame + 1); }
    if (e.key === 'ArrowLeft') { e.preventDefault(); stopPlay(); seek(state.viz.frame - 1); }
  });
  let rt = null;
  window.addEventListener('resize', () => { clearTimeout(rt); rt = setTimeout(drawChart, 120); });
}

(async function main() {
  wire();
  try {
    const info = await api('/api/info');
    if (!load('vx.pixelMm')) $('#pixelMm').value = info.default_pixel_mm;
    await refreshJobs();
    if (state.jobs.length) selectJob(state.jobs[state.jobs.length - 1].id);
  } catch (e) { toast('Cannot reach the local server – is app.py running?', true); }
})();
