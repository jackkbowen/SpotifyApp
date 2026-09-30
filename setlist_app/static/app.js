"use strict";

// ---------------------------------------------------------------------------
// Camelot wheel + transition rules
// ---------------------------------------------------------------------------

const CAMELOT_KEYS = [];
for (let n = 1; n <= 12; n++) CAMELOT_KEYS.push(`${n}A`, `${n}B`);

const CAMELOT_NAMES = {
  "1A": "Abm", "2A": "Ebm", "3A": "Bbm", "4A": "Fm", "5A": "Cm", "6A": "Gm",
  "7A": "Dm", "8A": "Am", "9A": "Em", "10A": "Bm", "11A": "F#m", "12A": "C#m",
  "1B": "B", "2B": "F#", "3B": "Db", "4B": "Ab", "5B": "Eb", "6B": "Bb",
  "7B": "F", "8B": "C", "9B": "G", "10B": "D", "11B": "A", "12B": "E",
};

function parseCamelot(code) {
  const m = /^(\d{1,2})([AB])$/.exec(code || "");
  return m ? { n: Number(m[1]), l: m[2] } : null;
}

// Compatible = same key, ±1 on the wheel with the same letter, or the
// relative major/minor (same number, other letter).
function keyRelation(fromCode, toCode) {
  const a = parseCamelot(fromCode), b = parseCamelot(toCode);
  if (!a || !b) return null;
  if (a.n === b.n && a.l === b.l) return { compatible: true, label: "same key" };
  if (a.l === b.l) {
    const step = (b.n - a.n + 12) % 12;
    if (step === 1) return { compatible: true, label: "+1" };
    if (step === 11) return { compatible: true, label: "−1" };
  }
  if (a.n === b.n) return { compatible: true, label: "relative" };
  return { compatible: false, label: "clash" };
}

function compatibleKeys(code) {
  return new Set(CAMELOT_KEYS.filter((k) => keyRelation(code, k)?.compatible));
}

// Bass music moves between half and double time (70 ↔ 140, 87 ↔ 174), and
// BPM databases list the same track either way, so compare at 1×, 2× and ½×
// and use whichever is closest.
function bpmRelation(fromBpm, toBpm) {
  if (!fromBpm || !toBpm) return null;
  let best = null;
  for (const [factor, label] of [[1, ""], [2, "2×"], [0.5, "½×"]]) {
    const effective = toBpm * factor;
    const pct = ((effective - fromBpm) / fromBpm) * 100;
    // Only prefer a half/double reading when it's clearly closer.
    if (!best || Math.abs(pct) + (factor === 1 ? 0 : 1) < Math.abs(best.pct)) {
      best = { delta: effective - fromBpm, pct, label };
    }
  }
  const abs = Math.abs(best.pct);
  best.cls = abs <= 3 ? "good" : abs <= 6 ? "ok" : "bad";
  return best;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const $ = (id) => document.getElementById(id);

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function fmtDuration(ms) {
  if (!ms) return "";
  const total = Math.round(ms / 1000);
  const h = Math.floor(total / 3600), m = Math.floor((total % 3600) / 60), s = total % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}

function fmtBpm(bpm) {
  return Number.isInteger(bpm) ? String(bpm) : bpm.toFixed(1);
}

function signed(x, digits = 1) {
  const v = x.toFixed(digits);
  return x > 0 ? `+${v}` : x < 0 ? v.replace("-", "−") : v;
}

const artistNames = (t) => (t.artists || []).map((a) => a.name).filter(Boolean).join(", ");
const enr = (t) => t.enrichment || {};

function missingReason(t) {
  return t.enrichment_status === "pending"
    ? "Not looked up yet — run enrich_library.py"
    : "No match found on GetSongBPM";
}

let toastTimer;
function toast(message, isError = false) {
  const el = $("toast");
  el.textContent = message;
  el.className = `toast show${isError ? " error" : ""}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.className = "toast"), isError ? 4500 : 2200);
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: options.body ? { "Content-Type": "application/json" } : {},
    ...options,
  });
  if (res.status === 204) return null;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.message || `Request failed (HTTP ${res.status})`);
    err.code = data.error;
    throw err;
  }
  return data;
}

function storage(action, key, value) {
  try {
    if (action === "get") return JSON.parse(localStorage.getItem(key));
    localStorage.setItem(key, JSON.stringify(value));
  } catch { /* storage unavailable: the draft just isn't remembered */ }
  return null;
}

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const state = {
  tracks: [],
  byId: new Map(),
  filters: { q: "", bpmMin: null, bpmMax: null, halftime: false, key: "", compatible: false, data: "all" },
  sort: { col: "added_at", dir: "desc" },
  selected: new Set(),
  savedSetlists: [],
  // Working setlist. `snapshots` holds saved info for tracks that are no
  // longer in the library, so old setlists still render.
  set: { id: null, name: "", trackIds: [], snapshots: {} },
  savedFingerprint: JSON.stringify({ name: "", trackIds: [] }),
  arcMetric: "danceability",
};

const fingerprint = () => JSON.stringify({ name: state.set.name.trim(), trackIds: state.set.trackIds });
const isDirty = () => fingerprint() !== state.savedFingerprint;

// ---------------------------------------------------------------------------
// Library table
// ---------------------------------------------------------------------------

const camelotSortValue = (code) => {
  const k = parseCamelot(code);
  return k ? k.n * 2 + (k.l === "B" ? 1 : 0) : null;
};

const COLUMNS = [
  { key: "track_name", label: "Track", value: (t) => t.track_name?.toLowerCase() },
  { key: "artists", label: "Artist", value: (t) => artistNames(t).toLowerCase() },
  { key: "album", label: "Album", value: (t) => t.album?.name?.toLowerCase() },
  { key: "duration_ms", label: "Time", num: true, value: (t) => t.duration_ms },
  { key: "bpm", label: "BPM", num: true, metric: true, value: (t) => enr(t).bpm ?? null },
  { key: "key", label: "Key", metric: true, value: (t) => camelotSortValue(enr(t).camelot) },
  { key: "danceability", label: "Dance", num: true, metric: true, title: "Danceability (0–100) from GetSongBPM", value: (t) => enr(t).danceability ?? null },
  { key: "acousticness", label: "Acoustic", num: true, metric: true, title: "Acousticness (0–100) from GetSongBPM", value: (t) => enr(t).acousticness ?? null },
  { key: "added_at", label: "Liked", value: (t) => t.added_at },
];

function bpmInRange(bpm, min, max, halftime) {
  const inRange = (b) => (min == null || b >= min) && (max == null || b <= max);
  return inRange(bpm) || (halftime && (inRange(bpm * 2) || inRange(bpm / 2)));
}

function filteredTracks() {
  const f = state.filters;
  const q = f.q.trim().toLowerCase();
  const keySet = f.key ? (f.compatible ? compatibleKeys(f.key) : new Set([f.key])) : null;
  const bpmActive = f.bpmMin != null || f.bpmMax != null;

  const rows = state.tracks.filter((t) => {
    const e = enr(t);
    if (f.data === "has" && !e.bpm && !e.camelot) return false;
    if (f.data === "missing" && (e.bpm || e.camelot)) return false;
    if (bpmActive && (!e.bpm || !bpmInRange(e.bpm, f.bpmMin, f.bpmMax, f.halftime))) return false;
    if (keySet && !keySet.has(e.camelot)) return false;
    if (q && !t._search.includes(q)) return false;
    return true;
  });

  const col = COLUMNS.find((c) => c.key === state.sort.col);
  const dir = state.sort.dir === "asc" ? 1 : -1;
  return rows.sort((a, b) => {
    const va = col.value(a), vb = col.value(b);
    // Missing values always sink to the bottom, whichever direction.
    if (va == null && vb == null) return 0;
    if (va == null) return 1;
    if (vb == null) return -1;
    return va < vb ? -dir : va > vb ? dir : 0;
  });
}

function renderHead() {
  const cells = ['<th class="col-check"><input type="checkbox" id="check-all" aria-label="Select all shown"></th>', '<th class="col-add"></th>'];
  for (const c of COLUMNS) {
    const arrow = state.sort.col === c.key ? `<span class="arrow">${state.sort.dir === "asc" ? "▲" : "▼"}</span>` : "";
    cells.push(`<th class="sortable${c.num ? " num" : ""}" data-sort="${c.key}"${c.title ? ` title="${esc(c.title)}"` : ""}>${c.label}${arrow}</th>`);
  }
  $("library-head").innerHTML = cells.join("");
  $("library-cols").innerHTML = '<col class="c-check"><col class="c-add">' +
    COLUMNS.map((c) => `<col class="c-${c.key === "track_name" ? "track" : c.key === "album" ? "album" : c.key}">`).join("");
}

function none(t) {
  return `<span class="none" title="${esc(missingReason(t))}">—</span>`;
}

function keyCell(e, t) {
  if (!e.camelot && !e.key) return none(t);
  return `<span class="keybadge">${esc(e.camelot || "?")}</span><span class="keyname">${esc(e.key || "")}</span>`;
}

function bpmCell(e, t) {
  if (!e.bpm) return none(t);
  const text = fmtBpm(e.bpm);
  if (!e.source_url) return text;
  const title = `GetSongBPM match: ${e.matched_title || "?"} — ${e.matched_artist || "?"}`;
  return `<a class="bpm-link" href="${esc(e.source_url)}" target="_blank" rel="noopener" title="${esc(title)}">${text}</a>`;
}

function pctCell(value, t) {
  return value == null ? none(t) : String(value);
}

let visibleIds = [];

function renderTable() {
  const rows = filteredTracks();
  visibleIds = rows.map((t) => t.track_id);
  const inSet = new Set(state.set.trackIds);

  $("library-body").innerHTML = rows.map((t) => {
    const e = enr(t);
    const hasData = e.bpm || e.camelot;
    const selected = state.selected.has(t.track_id);
    const added = inSet.has(t.track_id);
    return `<tr data-id="${esc(t.track_id)}" class="${selected ? "selected" : ""}${hasData ? "" : " nodata"}">
      <td class="col-check"><input type="checkbox" class="row-check" ${selected ? "checked" : ""} aria-label="Select"></td>
      <td class="col-add">${added
        ? '<button type="button" class="add in-set" title="Already in the set" disabled>✓</button>'
        : '<button type="button" class="add" title="Add to end of set">+</button>'}</td>
      <td class="track" title="${esc(t.track_name)}"><a href="${esc(t.spotify_url)}" target="_blank" rel="noopener">${esc(t.track_name)}</a></td>
      <td class="sub" title="${esc(artistNames(t))}">${esc(artistNames(t))}</td>
      <td class="sub" title="${esc(t.album?.name)}">${esc(t.album?.name)}</td>
      <td class="num">${fmtDuration(t.duration_ms)}</td>
      <td class="num metric">${bpmCell(e, t)}</td>
      <td class="metric">${keyCell(e, t)}</td>
      <td class="num metric">${pctCell(e.danceability, t)}</td>
      <td class="num metric">${pctCell(e.acousticness, t)}</td>
      <td class="sub">${esc((t.added_at || "").slice(0, 10))}</td>
    </tr>`;
  }).join("");

  $("result-count").textContent = `Showing ${rows.length} of ${state.tracks.length}`;
  const checkAll = $("check-all");
  const selectedVisible = visibleIds.filter((id) => state.selected.has(id)).length;
  checkAll.checked = rows.length > 0 && selectedVisible === rows.length;
  checkAll.indeterminate = selectedVisible > 0 && selectedVisible < rows.length;
  updateSelectionButtons();
}

function updateSelectionButtons() {
  const n = state.selected.size;
  $("add-selected").disabled = n === 0;
  $("add-selected").textContent = n ? `Add ${n} selected to set` : "Add selected to set";
  $("clear-selected").disabled = n === 0;
}

// ---------------------------------------------------------------------------
// Setlist builder
// ---------------------------------------------------------------------------

function setTrack(id) {
  const t = state.byId.get(id);
  if (t) return { ...t, missing: false };
  const snap = state.set.snapshots[id] || {};
  return {
    track_id: id,
    track_name: snap.track_name || id,
    artists: (snap.artists || []).map((name) => ({ name })),
    duration_ms: snap.duration_ms,
    enrichment: snap.enrichment || null,
    missing: true,
  };
}

function addToSet(ids) {
  const existing = new Set(state.set.trackIds);
  const fresh = ids.filter((id) => !existing.has(id));
  if (!fresh.length) {
    toast(ids.length === 1 ? "Already in the set" : "Those tracks are already in the set");
    return;
  }
  state.set.trackIds.push(...fresh);
  const skipped = ids.length - fresh.length;
  toast(`Added ${fresh.length} track${fresh.length === 1 ? "" : "s"}${skipped ? ` (${skipped} already in set)` : ""}`);
  setChanged();
}

function moveInSet(from, to) {
  const ids = state.set.trackIds;
  if (to < 0 || to >= ids.length || from === to) return;
  const [id] = ids.splice(from, 1);
  ids.splice(to, 0, id);
  setChanged();
}

function setChanged() {
  storage("set", "setlist-draft", state.set);
  renderSet();
  renderTable();
}

function transitionHtml(a, b) {
  const ea = enr(a), eb = enr(b);
  const bpm = bpmRelation(ea.bpm, eb.bpm);
  const key = keyRelation(ea.camelot, eb.camelot);
  const bpmPill = bpm
    ? `<span class="pill ${bpm.cls}" title="${fmtBpm(ea.bpm)} → ${fmtBpm(eb.bpm)} BPM${bpm.label ? ` (compared at ${bpm.label} tempo)` : ""}">${signed(bpm.delta)} BPM · ${signed(bpm.pct)}%${bpm.label ? ` · ${bpm.label}` : ""}</span>`
    : '<span class="pill unknown">BPM ?</span>';
  const keyPill = key
    ? `<span class="pill ${key.compatible ? "good" : "bad"}" title="Camelot ${ea.camelot} → ${eb.camelot}">${esc(ea.camelot)} → ${esc(eb.camelot)} · ${key.label}${key.compatible ? " ✓" : ""}</span>`
    : '<span class="pill unknown">key ?</span>';
  return `<li class="transition" aria-label="Transition">${bpmPill}${keyPill}</li>`;
}

function renderSet() {
  const ids = state.set.trackIds;
  const tracks = ids.map(setTrack);
  const parts = [];
  tracks.forEach((t, i) => {
    if (i > 0) parts.push(transitionHtml(tracks[i - 1], t));
    const e = enr(t);
    parts.push(`<li class="set-item${t.missing ? " missing-track" : ""}" draggable="true" data-index="${i}">
      <span class="pos">${i + 1}</span>
      <div style="min-width:0">
        <div class="title" title="${esc(t.track_name)}">${esc(t.track_name)}</div>
        <div class="artist">${esc(artistNames(t))}</div>
      </div>
      <div>
        <div class="meta">
          <span>${e.bpm ? `${fmtBpm(e.bpm)} BPM` : '<span class="none">— BPM</span>'}</span>
          ${e.camelot ? `<span class="keybadge" title="${esc(e.key || "")}">${esc(e.camelot)}</span>` : '<span class="keybadge none">—</span>'}
          <span class="none">${fmtDuration(t.duration_ms)}</span>
        </div>
        <div class="controls">
          <button type="button" data-act="up" title="Move up" ${i === 0 ? "disabled" : ""}>↑</button>
          <button type="button" data-act="down" title="Move down" ${i === ids.length - 1 ? "disabled" : ""}>↓</button>
          <button type="button" data-act="remove" title="Remove from set">✕</button>
        </div>
      </div>
    </li>`);
  });
  $("set-list").innerHTML = parts.join("");
  $("set-empty").hidden = ids.length > 0;

  $("set-name").value !== state.set.name && ($("set-name").value = state.set.name);
  renderSummary(tracks);
  renderArc(tracks);
  renderSetStatus();
}

function renderSummary(tracks) {
  if (!tracks.length) {
    $("set-summary").innerHTML = "";
    return;
  }
  const totalMs = tracks.reduce((sum, t) => sum + (t.duration_ms || 0), 0);
  const bpms = tracks.map((t) => enr(t).bpm).filter(Boolean);
  let compatible = 0, known = 0;
  for (let i = 1; i < tracks.length; i++) {
    const rel = keyRelation(enr(tracks[i - 1]).camelot, enr(tracks[i]).camelot);
    if (rel) { known++; if (rel.compatible) compatible++; }
  }
  const items = [
    `<span><b>${tracks.length}</b> tracks</span>`,
    `<span><b>${fmtDuration(totalMs)}</b> total</span>`,
  ];
  if (bpms.length) items.push(`<span>BPM <b>${fmtBpm(Math.min(...bpms))}–${fmtBpm(Math.max(...bpms))}</b></span>`);
  if (known) items.push(`<span><b>${compatible}/${known}</b> key-compatible transitions</span>`);
  const noData = tracks.length - tracks.filter((t) => enr(t).bpm || enr(t).camelot).length;
  if (noData) items.push(`<span>${noData} without BPM/key</span>`);
  $("set-summary").innerHTML = items.join("");
}

function renderArc(tracks) {
  const svg = $("arc-svg");
  const n = tracks.length;
  if (!n) {
    svg.innerHTML = "";
    svg.setAttribute("viewBox", "0 0 100 64");
    return;
  }
  const W = Math.max(n * 12, 100), H = 64, gap = 2;
  const barW = W / n - gap;
  let values, lo, hi, fmt;
  if (state.arcMetric === "bpm") {
    values = tracks.map((t) => enr(t).bpm ?? null);
    const known = values.filter((v) => v != null);
    lo = Math.min(...known, 999) - 5;
    hi = Math.max(...known, 0) + 5;
    fmt = (v) => `${fmtBpm(v)} BPM`;
  } else {
    values = tracks.map((t) => enr(t).danceability ?? null);
    lo = 0; hi = 100;
    fmt = (v) => `danceability ${v}`;
  }
  const bars = values.map((v, i) => {
    const x = i * (barW + gap);
    const label = `${i + 1}. ${esc(tracks[i].track_name)} — ${v == null ? "no data" : fmt(v)}`;
    if (v == null) {
      return `<rect x="${x}" y="${H - 4}" width="${barW}" height="4" fill="var(--bar-missing)"><title>${label}</title></rect>`;
    }
    const h = Math.max(3, ((v - lo) / Math.max(hi - lo, 1)) * (H - 2));
    return `<rect x="${x}" y="${H - h}" width="${barW}" height="${h}" rx="1.5" fill="var(--bar)"><title>${label}</title></rect>`;
  });
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.innerHTML = bars.join("");
}

function renderSetStatus() {
  const el = $("set-status");
  const dirty = isDirty();
  el.className = `set-status${dirty ? " dirty" : ""}`;
  el.textContent = !state.set.id
    ? (state.set.trackIds.length ? "Not saved yet" : "New setlist")
    : dirty ? "Unsaved changes" : "Saved";
  $("set-delete").disabled = !state.set.id;
  $("export-csv").disabled = $("export-m3u").disabled = !state.set.trackIds.length;
}

function renderSavedList() {
  const opts = ['<option value="">Saved setlists…</option>'];
  for (const s of state.savedSetlists) {
    opts.push(`<option value="${esc(s.id)}"${s.id === state.set.id ? " selected" : ""}>${esc(s.name)} (${s.track_count})</option>`);
  }
  $("saved-setlists").innerHTML = opts.join("");
}

// ---------------------------------------------------------------------------
// Persistence
// ---------------------------------------------------------------------------

async function refreshSavedList() {
  state.savedSetlists = (await api("/api/setlists")).setlists;
  renderSavedList();
}

function confirmDiscard() {
  return !isDirty() || confirm("Discard unsaved changes to the current setlist?");
}

function loadIntoEditor(setlist) {
  state.set = {
    id: setlist.id,
    name: setlist.name,
    trackIds: setlist.tracks.map((t) => t.track_id),
    snapshots: Object.fromEntries(setlist.tracks.map((t) => [t.track_id, t])),
  };
  state.savedFingerprint = fingerprint();
  setChanged();
  renderSavedList();
}

async function saveSet() {
  const name = state.set.name.trim();
  if (!name) {
    toast("Give the setlist a name first", true);
    $("set-name").focus();
    return false;
  }
  const body = JSON.stringify({ name, track_ids: state.set.trackIds });
  try {
    const saved = state.set.id
      ? await api(`/api/setlists/${state.set.id}`, { method: "PUT", body })
      : await api("/api/setlists", { method: "POST", body });
    loadIntoEditor(saved);
    await refreshSavedList();
    toast(`Saved “${saved.name}”`);
    return true;
  } catch (err) {
    if (err.code === "not_found") state.set.id = null;  // deleted elsewhere: next save creates it
    toast(err.message, true);
    return false;
  }
}

async function exportSet(format) {
  if ((isDirty() || !state.set.id) && !(await saveSet())) return;
  window.location.href = `/api/setlists/${state.set.id}/export?format=${format}`;
}

// ---------------------------------------------------------------------------
// Events
// ---------------------------------------------------------------------------

function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

function numberOrNull(value) {
  return value === "" || Number.isNaN(Number(value)) ? null : Number(value);
}

function bindEvents() {
  const f = state.filters;
  $("f-search").addEventListener("input", debounce((e) => { f.q = e.target.value; renderTable(); }, 120));
  $("f-bpm-min").addEventListener("input", debounce((e) => { f.bpmMin = numberOrNull(e.target.value); renderTable(); }, 200));
  $("f-bpm-max").addEventListener("input", debounce((e) => { f.bpmMax = numberOrNull(e.target.value); renderTable(); }, 200));
  $("f-halftime").addEventListener("change", (e) => { f.halftime = e.target.checked; renderTable(); });
  $("f-key").addEventListener("change", (e) => { f.key = e.target.value; renderTable(); });
  $("f-compatible").addEventListener("change", (e) => { f.compatible = e.target.checked; renderTable(); });
  $("f-data").addEventListener("change", (e) => { f.data = e.target.value; renderTable(); });
  $("f-reset").addEventListener("click", () => {
    Object.assign(f, { q: "", bpmMin: null, bpmMax: null, halftime: false, key: "", compatible: false, data: "all" });
    for (const id of ["f-search", "f-bpm-min", "f-bpm-max"]) $(id).value = "";
    $("f-halftime").checked = $("f-compatible").checked = false;
    $("f-key").value = "";
    $("f-data").value = "all";
    renderTable();
  });

  $("library-head").addEventListener("click", (e) => {
    const th = e.target.closest("th[data-sort]");
    if (!th) return;
    const col = th.dataset.sort;
    state.sort = state.sort.col === col
      ? { col, dir: state.sort.dir === "asc" ? "desc" : "asc" }
      : { col, dir: col === "added_at" ? "desc" : "asc" };
    renderHead();
    renderTable();
  });
  $("library-head").addEventListener("change", (e) => {
    if (e.target.id !== "check-all") return;
    for (const id of visibleIds) e.target.checked ? state.selected.add(id) : state.selected.delete(id);
    renderTable();
  });

  $("library-body").addEventListener("click", (e) => {
    const row = e.target.closest("tr[data-id]");
    if (!row) return;
    const id = row.dataset.id;
    if (e.target.matches("button.add:not(.in-set)")) {
      addToSet([id]);
    } else if (e.target.matches(".row-check")) {
      e.target.checked ? state.selected.add(id) : state.selected.delete(id);
      row.classList.toggle("selected", e.target.checked);
      updateSelectionButtons();
    }
  });

  $("add-selected").addEventListener("click", () => {
    // Add in the order currently shown in the table.
    const ordered = visibleIds.filter((id) => state.selected.has(id));
    const hidden = [...state.selected].filter((id) => !ordered.includes(id));
    addToSet([...ordered, ...hidden]);
    state.selected.clear();
    renderTable();
  });
  $("clear-selected").addEventListener("click", () => { state.selected.clear(); renderTable(); });

  // Setlist item buttons
  $("set-list").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-act]");
    const item = e.target.closest(".set-item");
    if (!btn || !item) return;
    const i = Number(item.dataset.index);
    if (btn.dataset.act === "up") moveInSet(i, i - 1);
    if (btn.dataset.act === "down") moveInSet(i, i + 1);
    if (btn.dataset.act === "remove") { state.set.trackIds.splice(i, 1); setChanged(); }
  });

  // Drag to reorder
  let dragFrom = null;
  const list = $("set-list");
  const clearMarks = () => list.querySelectorAll(".drop-before,.drop-after").forEach((el) => el.classList.remove("drop-before", "drop-after"));
  list.addEventListener("dragstart", (e) => {
    const item = e.target.closest(".set-item");
    if (!item) return;
    dragFrom = Number(item.dataset.index);
    item.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", String(dragFrom));
  });
  list.addEventListener("dragover", (e) => {
    const item = e.target.closest(".set-item");
    if (dragFrom == null || !item) return;
    e.preventDefault();
    clearMarks();
    const rect = item.getBoundingClientRect();
    item.classList.add(e.clientY < rect.top + rect.height / 2 ? "drop-before" : "drop-after");
  });
  list.addEventListener("drop", (e) => {
    const item = e.target.closest(".set-item");
    if (dragFrom == null || !item) return;
    e.preventDefault();
    let to = Number(item.dataset.index) + (item.classList.contains("drop-after") ? 1 : 0);
    if (to > dragFrom) to -= 1;
    clearMarks();
    moveInSet(dragFrom, to);
  });
  list.addEventListener("dragend", () => {
    dragFrom = null;
    clearMarks();
    list.querySelectorAll(".dragging").forEach((el) => el.classList.remove("dragging"));
  });

  $("set-name").addEventListener("input", (e) => {
    state.set.name = e.target.value;
    storage("set", "setlist-draft", state.set);
    renderSetStatus();
  });
  $("set-name").addEventListener("keydown", (e) => { if (e.key === "Enter") saveSet(); });
  $("set-save").addEventListener("click", saveSet);

  $("set-new").addEventListener("click", () => {
    if (!confirmDiscard()) return;
    state.set = { id: null, name: "", trackIds: [], snapshots: {} };
    state.savedFingerprint = fingerprint();
    setChanged();
    renderSavedList();
    $("set-name").focus();
  });

  $("saved-setlists").addEventListener("change", async (e) => {
    const id = e.target.value;
    if (!id || id === state.set.id) return;
    if (!confirmDiscard()) { renderSavedList(); return; }
    try {
      loadIntoEditor(await api(`/api/setlists/${id}`));
    } catch (err) {
      toast(err.message, true);
      await refreshSavedList();
    }
  });

  $("set-delete").addEventListener("click", async () => {
    if (!state.set.id || !confirm(`Delete “${state.set.name}”? This can't be undone.`)) return;
    try {
      await api(`/api/setlists/${state.set.id}`, { method: "DELETE" });
      toast(`Deleted “${state.set.name}”`);
    } catch (err) {
      if (err.code !== "not_found") { toast(err.message, true); return; }
    }
    state.set = { id: null, name: "", trackIds: [], snapshots: {} };
    state.savedFingerprint = fingerprint();
    setChanged();
    await refreshSavedList();
  });

  $("export-csv").addEventListener("click", () => exportSet("csv"));
  $("export-m3u").addEventListener("click", () => exportSet("m3u"));

  document.querySelectorAll(".seg button").forEach((btn) => btn.addEventListener("click", () => {
    state.arcMetric = btn.dataset.metric;
    document.querySelectorAll(".seg button").forEach((b) => b.classList.toggle("active", b === btn));
    renderArc(state.set.trackIds.map(setTrack));
  }));

  window.addEventListener("beforeunload", (e) => {
    if (isDirty()) { e.preventDefault(); e.returnValue = ""; }
  });
}

// ---------------------------------------------------------------------------
// Startup
// ---------------------------------------------------------------------------

function renderLibraryMeta(lib) {
  const c = lib.status_counts || {};
  const withData = state.tracks.filter((t) => enr(t).bpm || enr(t).camelot).length;
  $("library-stats").textContent =
    `${state.tracks.length} tracks · ${withData} with BPM/key · ` +
    `${(c.unmatched || 0)} not found on GetSongBPM` + (c.pending ? ` · ${c.pending} not looked up yet` : "");

  const warnings = lib.warnings || [];
  $("warnings").hidden = !warnings.length;
  $("warnings").innerHTML = warnings.map((w) => `<p>⚠ ${esc(w).replace(/`([^`]+)`/g, "<code>$1</code>")}</p>`).join("");

  $("f-key").innerHTML = '<option value="">Any key</option>' +
    CAMELOT_KEYS.map((k) => `<option value="${k}">${k} · ${CAMELOT_NAMES[k]}</option>`).join("");
}

async function init() {
  let lib;
  try {
    lib = await api("/api/library");
  } catch (err) {
    $("missing-library").hidden = false;
    $("missing-message").innerHTML = esc(err.message).replace(/`([^`]+)`/g, "<code>$1</code>");
    return;
  }
  state.tracks = lib.tracks.filter((t) => t.track_id);
  for (const t of state.tracks) {
    t._search = `${t.track_name || ""} ${artistNames(t)}`.toLowerCase();
    state.byId.set(t.track_id, t);
  }
  renderLibraryMeta(lib);
  $("main").hidden = false;

  bindEvents();
  renderHead();

  // Restore an unsaved working setlist from a previous visit, if any.
  const draft = storage("get", "setlist-draft");
  await refreshSavedList().catch((err) => toast(err.message, true));
  if (draft && Array.isArray(draft.trackIds)) {
    const saved = draft.id && state.savedSetlists.some((s) => s.id === draft.id)
      ? await api(`/api/setlists/${draft.id}`).catch(() => null)
      : null;
    state.set = { id: saved ? draft.id : null, name: draft.name || "", trackIds: draft.trackIds, snapshots: draft.snapshots || {} };
    state.savedFingerprint = saved
      ? JSON.stringify({ name: saved.name.trim(), trackIds: saved.tracks.map((t) => t.track_id) })
      : JSON.stringify({ name: "", trackIds: [] });
    renderSavedList();
  }
  renderSet();
  renderTable();
}

init();
