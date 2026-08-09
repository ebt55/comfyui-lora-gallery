const state = {
  images: [],
  summary: null,
  selected: new Set(),
  loraSearch: '',
  imageSearch: '',
  matchMode: 'any',
  sortBy: 'newest',
  modalImage: null,
  scanPolling: false,
};

const $ = (id) => document.getElementById(id);
const els = {
  stats: $('stats'), loraSearch: $('loraSearch'), loraList: $('loraList'),
  imageSearch: $('imageSearch'), matchMode: $('matchMode'), sortBy: $('sortBy'),
  clearFilters: $('clearFilters'), rescan: $('rescan'), activeFilters: $('activeFilters'),
  gallery: $('gallery'), empty: $('empty'), visibleCount: $('visibleCount'), modal: $('modal'),
  modalClose: $('modalClose'), modalImage: $('modalImage'), modalTitle: $('modalTitle'),
  modalMeta: $('modalMeta'), modalLoras: $('modalLoras'), metadataJson: $('metadataJson'),
  copyLoras: $('copyLoras'), scanStatus: $('scanStatus'), scanLabel: $('scanLabel'),
  scanNumbers: $('scanNumbers'), scanBar: $('scanBar'), scanFile: $('scanFile'),
};

const esc = (s) => String(s ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const loraKey = (l) => String(l.file || l.name).replaceAll('\\', '/').toLowerCase();
const fmtBytes = (n) => n < 1024*1024 ? `${(n/1024).toFixed(0)} KB` : `${(n/1024/1024).toFixed(1)} MB`;
const strengthText = (l) => {
  const bits = [];
  if (l.weight != null) bits.push(`weight ${Number(l.weight).toFixed(2)}`);
  if (l.strength_model != null) bits.push(`model ${Number(l.strength_model).toFixed(2)}`);
  if (l.strength_clip != null) bits.push(`clip ${Number(l.strength_clip).toFixed(2)}`);
  return bits.join(' - ');
};

async function getJSON(url, options) {
  const res = await fetch(url, options);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

async function loadGalleryData() {
  const [summary, payload] = await Promise.all([getJSON('/api/summary'), getJSON('/api/images')]);
  state.summary = summary;
  state.images = payload.images;
  const valid = new Set(summary.loras.map(l => l.key));
  state.selected = new Set([...state.selected].filter(k => valid.has(k)));
  renderAll();
}

const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms));

function renderScanStatus(scan) {
  if (!scan?.running) {
    els.scanStatus.classList.add('hidden');
    return;
  }
  els.scanStatus.classList.remove('hidden');
  const track = els.scanBar.parentElement;
  if (scan.phase === 'discovering' || !scan.total) {
    els.scanLabel.textContent = 'Finding images...';
    els.scanNumbers.textContent = '';
    els.scanFile.textContent = 'Scanning the selected folder for supported image files.';
    track.classList.add('indeterminate');
    els.scanBar.style.width = '';
    return;
  }
  track.classList.remove('indeterminate');
  const processed = Number(scan.processed || 0);
  const total = Number(scan.total || 0);
  const pct = total ? Math.min(100, Math.round(processed / total * 100)) : 0;
  els.scanLabel.textContent = 'Scanning ComfyUI metadata...';
  els.scanNumbers.textContent = `${processed.toLocaleString()} / ${total.toLocaleString()} - ${pct}%`;
  els.scanBar.style.width = `${pct}%`;
  els.scanFile.textContent = scan.current || '';
}

async function waitForScan() {
  if (state.scanPolling) return;
  state.scanPolling = true;
  try {
    while (true) {
      const scan = await getJSON('/api/scan-status');
      renderScanStatus(scan);
      if (!scan.running) {
        if (scan.phase === 'error') throw new Error(scan.error || 'Scan failed');
        await loadGalleryData();
        renderScanStatus(scan);
        return;
      }
      await sleep(250);
    }
  } finally {
    state.scanPolling = false;
  }
}

async function init() {
  const scan = await getJSON('/api/scan-status');
  renderScanStatus(scan);
  if (scan.running) await waitForScan();
  else await loadGalleryData();
}

function renderStats() {
  const s = state.summary;
  els.stats.innerHTML = `
    <div class="stat"><strong>${s.image_count}</strong><span>images</span></div>
    <div class="stat"><strong>${s.lora_count}</strong><span>unique LoRAs</span></div>
    <div class="stat" style="grid-column:1/-1"><span title="${esc(s.root)}">${esc(s.root)}</span></div>
    ${s.errors?.length ? `<div class="error-note" style="grid-column:1/-1">${s.errors.length} file(s) could not be indexed.</div>` : ''}
  `;
}

function renderLoraList() {
  const needle = state.loraSearch.trim().toLowerCase();
  const items = (state.summary?.loras || []).filter(l => !needle || l.name.toLowerCase().includes(needle) || l.file.toLowerCase().includes(needle));
  els.loraList.innerHTML = items.map(l => {
    const checked = state.selected.has(l.key);
    return `<label class="lora-option" title="${esc(l.file)}">
      <input type="checkbox" data-lora="${esc(l.key)}" ${checked ? 'checked' : ''} />
      <span class="name">${esc(l.name)}</span>
      <span class="count-pill">${l.count}</span>
    </label>`;
  }).join('');
  els.loraList.querySelectorAll('input[data-lora]').forEach(input => input.addEventListener('change', () => {
    if (input.checked) state.selected.add(input.dataset.lora); else state.selected.delete(input.dataset.lora);
    renderGallery();
    renderActiveFilters();
  }));
}

function matches(img) {
  const keys = new Set(img.loras.map(loraKey));
  if (state.selected.size) {
    const checks = [...state.selected].map(k => keys.has(k));
    if (state.matchMode === 'all' ? !checks.every(Boolean) : !checks.some(Boolean)) return false;
  }
  const q = state.imageSearch.trim().toLowerCase();
  if (q) {
    const hay = [img.filename, img.relpath, ...img.loras.flatMap(l => [l.name, l.file])].join('\n').toLowerCase();
    if (!hay.includes(q)) return false;
  }
  return true;
}

function sortedVisible() {
  const rows = state.images.filter(matches);
  if (state.sortBy === 'oldest') rows.sort((a,b) => a.mtime - b.mtime);
  else if (state.sortBy === 'name') rows.sort((a,b) => a.filename.localeCompare(b.filename));
  else if (state.sortBy === 'loras') rows.sort((a,b) => b.loras.length - a.loras.length || b.mtime - a.mtime);
  else rows.sort((a,b) => b.mtime - a.mtime);
  return rows;
}

function renderGallery() {
  const rows = sortedVisible();
  els.visibleCount.textContent = `${rows.length} of ${state.images.length}`;
  els.empty.classList.toggle('hidden', rows.length !== 0);
  els.gallery.innerHTML = rows.map(img => {
    const badges = img.loras.length ? img.loras.map(l => {
      const active = state.selected.has(loraKey(l));
      const s = strengthText(l);
      return `<span class="badge ${active ? 'active' : ''}" title="${esc(l.file)}${s ? ` - ${esc(s)}` : ''}">${esc(l.name)}${s ? ` - ${esc(s)}` : ''}</span>`;
    }).join('') : `<span class="no-lora">No LoRA detected</span>`;
    return `<article class="card">
      <button class="thumb-button" data-open="${img.id}" title="Open ${esc(img.filename)}">
        <img src="/api/thumb?id=${encodeURIComponent(img.id)}&px=520" loading="lazy" alt="${esc(img.filename)}" />
      </button>
      <div class="card-body">
        <div class="filename" title="${esc(img.relpath)}">${esc(img.filename)}</div>
        <div class="badges">${badges}</div>
        <div class="card-foot"><span>${img.width}x${img.height}</span><span>${img.loras.length} LoRA${img.loras.length === 1 ? '' : 's'}</span></div>
      </div>
    </article>`;
  }).join('');
  els.gallery.querySelectorAll('[data-open]').forEach(el => el.addEventListener('click', () => openModal(el.dataset.open)));
}

function renderActiveFilters() {
  if (!state.selected.size) {
    els.activeFilters.innerHTML = '';
    return;
  }
  const byKey = new Map((state.summary?.loras || []).map(l => [l.key, l]));
  els.activeFilters.innerHTML = [...state.selected].map(k => `<button class="active-chip" data-remove="${esc(k)}">${esc(byKey.get(k)?.name || k)} x</button>`).join('');
  els.activeFilters.querySelectorAll('[data-remove]').forEach(btn => btn.addEventListener('click', () => {
    state.selected.delete(btn.dataset.remove);
    renderLoraList(); renderGallery(); renderActiveFilters();
  }));
}

function renderAll() { renderStats(); renderLoraList(); renderActiveFilters(); renderGallery(); }

async function openModal(id) {
  els.modalTitle.textContent = 'Loading...';
  els.modalImage.removeAttribute('src');
  els.modalLoras.innerHTML = '';
  els.metadataJson.textContent = '';
  if (!els.modal.open) els.modal.showModal();
  try {
    const img = await getJSON(`/api/detail?id=${encodeURIComponent(id)}`);
    state.modalImage = img;
    els.modalImage.src = `/api/media?id=${encodeURIComponent(id)}`;
    els.modalTitle.textContent = img.filename;
    els.modalMeta.textContent = `${img.relpath} - ${img.width}x${img.height} - ${fmtBytes(img.bytes)}`;
    els.modalLoras.innerHTML = img.loras.length ? img.loras.map(l => `<div class="lora-detail">
      <strong>${esc(l.name)}</strong>
      <span>${esc(l.file)}</span>
      ${strengthText(l) ? `<span>${esc(strengthText(l))}</span>` : ''}
    </div>`).join('') : `<span class="no-lora">No LoRA could be detected in the embedded metadata.</span>`;
    els.metadataJson.textContent = JSON.stringify(img.metadata, null, 2);
  } catch (err) {
    els.modalTitle.textContent = 'Could not load image details';
    els.modalMeta.textContent = String(err);
  }
}

els.loraSearch.addEventListener('input', e => { state.loraSearch = e.target.value; renderLoraList(); });
els.imageSearch.addEventListener('input', e => { state.imageSearch = e.target.value; renderGallery(); });
els.matchMode.addEventListener('change', e => { state.matchMode = e.target.value; renderGallery(); });
els.sortBy.addEventListener('change', e => { state.sortBy = e.target.value; renderGallery(); });
els.clearFilters.addEventListener('click', () => {
  state.selected.clear(); state.imageSearch = ''; state.loraSearch = '';
  els.imageSearch.value = ''; els.loraSearch.value = '';
  renderLoraList(); renderActiveFilters(); renderGallery();
});
els.rescan.addEventListener('click', async () => {
  els.rescan.disabled = true; els.rescan.textContent = 'Scanning…';
  try {
    const response = await getJSON('/api/rescan', {method:'POST'});
    renderScanStatus(response.scan);
    await waitForScan();
  } catch (err) {
    els.gallery.insertAdjacentHTML('afterbegin', `<div class="error-note">Rescan failed: ${esc(err)}</div>`);
  } finally {
    els.rescan.disabled = false; els.rescan.textContent = 'Rescan folder';
  }
});

els.modalClose.addEventListener('click', () => els.modal.close());
els.modal.addEventListener('click', e => { if (e.target === els.modal) els.modal.close(); });
els.copyLoras.addEventListener('click', async () => {
  const img = state.modalImage;
  if (!img) return;
  const text = img.loras.map(l => `${l.file}${strengthText(l) ? ` -- ${strengthText(l)}` : ''}`).join('\n');
  await navigator.clipboard.writeText(text || 'No LoRA detected');
  const old = els.copyLoras.textContent; els.copyLoras.textContent = 'Copied'; setTimeout(() => els.copyLoras.textContent = old, 900);
});

init().catch(err => {
  els.gallery.innerHTML = `<div class="error-note">Failed to load gallery: ${esc(err)}</div>`;
});
