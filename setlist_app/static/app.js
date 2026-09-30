"use strict";

// Camelot/BPM rules, mood matching and transition scoring live in scoring.js.

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
  // longer in the library, so old setlists still render. `mood` is the active
  // mood/vibe tag filter for suggestions; `arc` is the target energy arc of a
  // generated draft (drawn over the energy bars).
  set: blankSet(),
  savedFingerprint: "",
  arcMetric: "danceability",
  vocab: [],
  suggestOpen: false,
  gen: { tags: [], shape: "build" },
};

function blankSet() {
  return { id: null, name: "", trackIds: [], snapshots: {}, mood: [], arc: null };
}

const fingerprintOf = (name, trackIds, mood) => JSON.stringify({ name: name.trim(), trackIds, mood: mood || [] });
const fingerprint = () => fingerprintOf(state.set.name, state.set.trackIds, state.set.mood);
const isDirty = () => fingerprint() !== state.savedFingerprint;
state.savedFingerprint = fingerprint();

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
  { key: "tags", label: "Tags", title: "Mood/genre tags from Last.fm, strongest first", value: (t) => t.tags?.[0]?.tag ?? null },
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
    COLUMNS.map((c) => `<col class="c-${c.key === "track_name" ? "track" : c.key}">`).join("");
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

function tagTitle(t) {
  if (t.tag_status === "pending") return "Tags not fetched yet — run enrich_library.py";
  if (!t.tags?.length) return "No usable tags on Last.fm";
  return t.tags.map((x) => `${x.tag} (${x.weight}${x.source === "artist" ? ", from artist" : ""})`).join(", ");
}

function tagsCell(t) {
  if (!t.tags?.length) return '<span class="none">—</span>';
  return esc(t.tags.slice(0, 3).map((x) => x.tag).join(", "));
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
      <td class="sub tags-cell" title="${esc(tagTitle(t))}">${tagsCell(t)}</td>
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

function bpmPill(a, b) {
  const ea = enr(a), eb = enr(b);
  const bpm = bpmRelation(ea.bpm, eb.bpm);
  return bpm
    ? `<span class="pill ${bpm.cls}" title="${fmtBpm(ea.bpm)} → ${fmtBpm(eb.bpm)} BPM${bpm.label ? ` (compared at ${bpm.label} tempo)` : ""}">${signed(bpm.delta)} BPM · ${signed(bpm.pct)}%${bpm.label ? ` · ${bpm.label}` : ""}</span>`
    : '<span class="pill unknown">BPM ?</span>';
}

function keyPill(a, b) {
  const ea = enr(a), eb = enr(b);
  const key = keyRelation(ea.camelot, eb.camelot);
  return key
    ? `<span class="pill ${key.compatible ? "good" : "bad"}" title="Camelot ${ea.camelot} → ${eb.camelot}">${esc(ea.camelot)} → ${esc(eb.camelot)} · ${key.label}${key.compatible ? " ✓" : ""}</span>`
    : '<span class="pill unknown">key ?</span>';
}

function energyPill(a, b) {
  const da = enr(a).danceability, db = enr(b).danceability;
  if (da == null || db == null) return '<span class="pill unknown">energy ?</span>';
  const d = db - da, abs = Math.abs(d);
  const cls = abs <= 10 ? "good" : abs <= 25 ? "ok" : "bad";
  return `<span class="pill ${cls}" title="Danceability ${da} → ${db}">energy ${signed(d, 0)}</span>`;
}

function transitionHtml(a, b) {
  return `<li class="transition" aria-label="Transition">${bpmPill(a, b)}${keyPill(a, b)}</li>`;
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
  renderMood();
  renderSuggestions();
}

function chip(tag, removable, count) {
  return `<span class="chip" data-tag="${esc(tag)}">${esc(tag)}${count != null ? ` <span class="chip-count">${count}</span>` : ""}${removable ? ` <button type="button" class="chip-x" aria-label="Remove ${esc(tag)}">×</button>` : ""}</span>`;
}

function renderMood() {
  const mood = state.set.mood;
  $("mood-chips").innerHTML = mood.length
    ? mood.map((t) => chip(t, true)).join("")
    : '<span class="none">any (whole library)</span>';
  $("mood-edit").textContent = mood.length ? "Change…" : "Set mood…";
}

// ---- Suggest next track ----

function renderSuggestions() {
  const ids = state.set.trackIds;
  $("suggest-wrap").hidden = ids.length === 0;
  $("suggest-toggle").setAttribute("aria-expanded", String(state.suggestOpen));
  $("suggest-toggle").textContent = state.suggestOpen ? "Hide suggestions" : "Suggest next track";
  const box = $("suggestions");
  box.hidden = !state.suggestOpen || !ids.length;
  if (box.hidden) return;

  const last = setTrack(ids[ids.length - 1]);
  const mood = state.set.mood;
  const { suggestions, candidates } = suggestNext(state.tracks, ids, { tags: mood, limit: 12 });
  const scope = mood.length ? `mood: ${mood.map(esc).join(", ")}` : "whole library";
  const noData = !enr(last).bpm && !enr(last).camelot;
  const head = `<div class="sugg-head">After <b>${esc(last.track_name)}</b> · ${scope} · ${candidates} candidates</div>` +
    (noData ? '<p class="hint">This track has no BPM/key data, so suggestions can only weigh energy and mood.</p>' : "");
  if (!suggestions.length) {
    box.innerHTML = head + `<p class="hint">No candidates${mood.length ? " with these mood tags left — change or clear the mood" : ""}.</p>`;
    return;
  }
  box.innerHTML = head + '<ol class="sugg-list">' + suggestions.map(({ track, t, matched }) => {
    const fit = Math.round((1 - t.cost) * 100);
    const moodPill = mood.length
      ? `<span class="pill ${matched.length === mood.length ? "good" : "ok"}" title="Matching mood tags (weight)">${matched.map((m) => `${esc(m.tag)} ${m.weight}`).join(", ")}</span>`
      : "";
    return `<li class="sugg" data-id="${esc(track.track_id)}">
      <div class="sugg-main">
        <div class="title" title="${esc(track.track_name)}">${esc(track.track_name)}</div>
        <div class="artist">${esc(artistNames(track))}${enr(track).bpm ? ` · ${fmtBpm(enr(track).bpm)} BPM` : ""}${enr(track).camelot ? ` · ${esc(enr(track).camelot)}` : ""}</div>
        <div class="why">${bpmPill(last, track)}${keyPill(last, track)}${energyPill(last, track)}${moodPill}</div>
      </div>
      <div class="sugg-side">
        <span class="fit" title="Transition fit: 100 = ideal. Weighted: BPM ${WEIGHTS.bpm}, key ${WEIGHTS.key}, energy ${WEIGHTS.energy}${mood.length ? `, mood ${WEIGHTS.mood}` : ""}; unknown data counts as uncertain.">${fit}</span>
        <button type="button" class="add" title="Add to end of set">+</button>
      </div>
    </li>`;
  }).join("") + "</ol>";
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
  // A generated draft's target arc, as a dashed line over the danceability bars.
  const arc = state.set.arc;
  if (arc && arc.range && state.arcMetric === "danceability" && arc.shape !== "free") {
    const points = values.map((_, i) => {
      const target = arcTarget(arc.shape, n > 1 ? i / (n - 1) : 0, arc.range);
      return `${(i * (barW + gap) + barW / 2).toFixed(1)},${(H - (target / 100) * (H - 2)).toFixed(1)}`;
    });
    bars.push(`<polyline points="${points.join(" ")}" fill="none" stroke="var(--text)" stroke-width="1.5" stroke-dasharray="4 3" opacity=".55" vector-effect="non-scaling-stroke"><title>Target: ${esc(ARC_SHAPES[arc.shape])}</title></polyline>`);
  }
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
    mood: setlist.mood_tags || [],
    arc: null,
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
  const body = JSON.stringify({ name, track_ids: state.set.trackIds, mood_tags: state.set.mood });
  const arc = state.set.arc;  // keep a generated draft's arc overlay after saving
  try {
    const saved = state.set.id
      ? await api(`/api/setlists/${state.set.id}`, { method: "PUT", body })
      : await api("/api/setlists", { method: "POST", body });
    loadIntoEditor(saved);
    state.set.arc = arc;
    renderArc(state.set.trackIds.map(setTrack));
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
    state.set = blankSet();
    state.savedFingerprint = fingerprint();
    hideNotice();
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
      hideNotice();
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
    state.set = blankSet();
    state.savedFingerprint = fingerprint();
    setChanged();
    await refreshSavedList();
  });

  $("export-csv").addEventListener("click", () => exportSet("csv"));
  $("export-m3u").addEventListener("click", () => exportSet("m3u"));

  document.querySelectorAll(".arc .seg button").forEach((btn) => btn.addEventListener("click", () => {
    state.arcMetric = btn.dataset.metric;
    document.querySelectorAll(".arc .seg button").forEach((b) => b.classList.toggle("active", b === btn));
    renderArc(state.set.trackIds.map(setTrack));
  }));

  // Mood chips in the setlist panel
  $("mood-chips").addEventListener("click", (e) => {
    const x = e.target.closest(".chip-x");
    if (!x) return;
    const tag = x.closest(".chip").dataset.tag;
    state.set.mood = state.set.mood.filter((t) => t !== tag);
    setChanged();
  });
  $("mood-edit").addEventListener("click", () => openGenerate());

  // Suggest next track
  $("suggest-toggle").addEventListener("click", () => {
    state.suggestOpen = !state.suggestOpen;
    renderSuggestions();
  });
  $("suggestions").addEventListener("click", (e) => {
    const item = e.target.closest(".sugg");
    if (item && e.target.closest("button.add")) addToSet([item.dataset.id]);
  });

  bindGenerateDialog();
  $("set-generate").addEventListener("click", () => openGenerate());
  $("gen-notice").addEventListener("click", (e) => { if (e.target.closest(".notice-x")) hideNotice(); });

  window.addEventListener("beforeunload", (e) => {
    if (isDirty()) { e.preventDefault(); e.returnValue = ""; }
  });
}

// ---------------------------------------------------------------------------
// Generate setlist dialog
// ---------------------------------------------------------------------------

function showNotice(html, warn = false) {
  const el = $("gen-notice");
  el.className = `notice${warn ? " warn" : ""}`;
  el.innerHTML = `<div>${html}</div><button type="button" class="notice-x" aria-label="Dismiss">×</button>`;
  el.hidden = false;
}

function hideNotice() {
  $("gen-notice").hidden = true;
}

function openGenerate() {
  state.gen.tags = [...state.set.mood];
  $("tag-search").value = "";
  renderGenDialog();
  $("gen-dialog").showModal();
  $("tag-search").focus();
}

function renderGenDialog() {
  const g = state.gen;
  $("tag-selected").innerHTML = g.tags.length
    ? g.tags.map((t) => chip(t, true)).join("")
    : '<span class="none">No tags selected</span>';

  const q = $("tag-search").value.trim().toLowerCase();
  const selected = new Set(g.tags);
  const options = state.vocab.filter((v) => !selected.has(v.tag) && (!q || v.tag.includes(q))).slice(0, 60);
  $("tag-options").innerHTML = state.vocab.length
    ? (options.length
      ? options.map((v) => `<button type="button" class="tag-opt" data-tag="${esc(v.tag)}" role="option">${esc(v.tag)} <span class="chip-count">${v.count}</span></button>`).join("")
      : '<span class="none">No matching tags</span>')
    : '<span class="none">No tags yet: add LASTFM_API_KEY to .env and run <code>python enrich_library.py --only tags</code>.</span>';

  if (g.tags.length) {
    const pool = moodPool(state.tracks, g.tags);
    const withData = pool.filter((c) => enr(c.track).bpm && enr(c.track).camelot).length;
    const all = pool.filter((c) => c.matched.length === g.tags.length).length;
    $("pool-preview").innerHTML = `<b>${pool.length}</b> tracks match${g.tags.length > 1 ? ` (${all} match all ${g.tags.length} tags)` : ""} · ${withData} of them have BPM/key data`;
  } else {
    $("pool-preview").textContent = "";
  }

  $("gen-shape").innerHTML = Object.entries(ARC_SHAPES).map(([k, label]) =>
    `<button type="button" data-shape="${k}" class="${g.shape === k ? "active" : ""}" role="radio" aria-checked="${g.shape === k}">${label}</button>`).join("");
  $("gen-go").disabled = $("gen-mood-only").disabled = !g.tags.length;
}

function autoName(tags, shape) {
  const date = new Date().toISOString().slice(0, 10);
  return `${tags.slice(0, 3).join(" + ")} (${ARC_SHAPES[shape].toLowerCase()}) ${date}`;
}

function runGenerate() {
  const g = state.gen;
  const length = Math.max(1, Math.round(Number($("gen-length").value) || 0));
  const byMinutes = $("gen-length-unit").value === "minutes";
  if (!confirmDiscard()) return;
  const result = generateSetlist(state.tracks, {
    tags: g.tags, shape: g.shape, count: byMinutes ? null : length, minutes: byMinutes ? length : null,
  });
  $("gen-dialog").close();
  if (!result.trackIds.length) {
    showNotice(`No tracks carry ${g.tags.map((t) => `“${esc(t)}”`).join(" or ")}. Try other tags.`, true);
    return;
  }
  state.set = {
    ...blankSet(),
    name: autoName(g.tags, g.shape),
    trackIds: result.trackIds,
    mood: [...g.tags],
    arc: { shape: g.shape, range: result.range },
  };
  state.savedFingerprint = fingerprintOf("", [], []);
  state.suggestOpen = false;
  setChanged();
  renderSavedList();

  const got = `${result.trackIds.length} tracks · ${fmtDuration(result.elapsedMs)}`;
  const lines = [];
  if (result.shortfall) {
    const wanted = byMinutes ? `${length} minutes` : `${length} tracks`;
    lines.push(`<b>Only ${result.poolSize} track${result.poolSize === 1 ? "" : "s"} match this mood</b>, so the draft is ${got} instead of the ${wanted} you asked for. Nothing outside the mood was added. Add more tags to widen it.`);
  } else {
    lines.push(`<b>Draft generated:</b> ${got}, chosen from ${result.poolSize} matching tracks. Review, reorder or swap, then save.`);
    if (result.poolSize < 2 * result.trackIds.length) lines.push("The mood pool is small, so later transitions had little to choose from.");
  }
  const noData = result.trackIds.length - result.withData;
  if (noData) lines.push(`${noData} track${noData === 1 ? " has" : "s have"} no BPM/key data, so ${noData === 1 ? "its" : "their"} transitions are unscored guesses.`);
  if (!result.range && g.shape !== "free") lines.push("Too few tracks here have energy data to follow the arc; ordering used BPM/key only.");
  showNotice(lines.join("<br>"), result.shortfall);
}

function bindGenerateDialog() {
  $("tag-search").addEventListener("input", renderGenDialog);
  $("tag-search").addEventListener("keydown", (e) => {
    // Enter picks the first matching tag instead of submitting the form.
    if (e.key !== "Enter") return;
    e.preventDefault();
    const first = $("tag-options").querySelector(".tag-opt");
    if (first) first.click();
  });
  $("tag-options").addEventListener("click", (e) => {
    const opt = e.target.closest(".tag-opt");
    if (!opt) return;
    state.gen.tags.push(opt.dataset.tag);
    $("tag-search").value = "";
    renderGenDialog();
    $("tag-search").focus();
  });
  $("tag-selected").addEventListener("click", (e) => {
    const x = e.target.closest(".chip-x");
    if (!x) return;
    state.gen.tags = state.gen.tags.filter((t) => t !== x.closest(".chip").dataset.tag);
    renderGenDialog();
  });
  $("gen-shape").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-shape]");
    if (!btn) return;
    state.gen.shape = btn.dataset.shape;
    renderGenDialog();
  });
  $("gen-length-unit").addEventListener("change", (e) => {
    $("gen-length").value = e.target.value === "minutes" ? 60 : 20;
  });
  $("gen-cancel").addEventListener("click", () => $("gen-dialog").close());
  $("gen-mood-only").addEventListener("click", () => {
    state.set.mood = [...state.gen.tags];
    $("gen-dialog").close();
    state.suggestOpen = true;
    setChanged();
  });
  $("gen-form").addEventListener("submit", (e) => {
    e.preventDefault();
    runGenerate();
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
    `${(c.unmatched || 0)} not found on GetSongBPM` + (c.pending ? ` · ${c.pending} not looked up yet` : "") +
    ` · ${state.tracks.filter((t) => t.tags?.length).length} with mood tags`;

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
    t._search = `${t.track_name || ""} ${artistNames(t)} ${(t.tags || []).map((x) => x.tag).join(" ")}`.toLowerCase();
    state.byId.set(t.track_id, t);
  }
  state.vocab = tagVocabulary(state.tracks);
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
    state.set = {
      ...blankSet(),
      id: saved ? draft.id : null,
      name: draft.name || "",
      trackIds: draft.trackIds,
      snapshots: draft.snapshots || {},
      mood: Array.isArray(draft.mood) ? draft.mood : [],
      arc: draft.arc || null,
    };
    state.savedFingerprint = saved
      ? fingerprintOf(saved.name, saved.tracks.map((t) => t.track_id), saved.mood_tags)
      : fingerprintOf("", [], []);
    renderSavedList();
  }
  renderSet();
  renderTable();
}

init();
