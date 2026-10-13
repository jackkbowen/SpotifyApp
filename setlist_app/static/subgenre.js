"use strict";

// Subgenre page: stacked, colour-coded rows.
//   Genre (family)  ->  click to open its subgenres
//     Subgenre      ->  click to open its songs
//       Song  Artist                      BPM/Key  3:42
//
// Data comes from library_enriched.json: each song's merged `profile` and the
// library-wide `taxonomy` (see setlist_app/taxonomy.py). A song is in a
// subgenre (or genre) when its weight for it is at least PRESENCE, the same
// threshold the taxonomy uses for its own counts. bpmOf/camelotOf come from
// scoring.js.

const PRESENCE = 20;
// One hue per genre, in the order the taxonomy lists them (largest first).
const HUES = [0, 100, 222, 30, 275, 175, 330, 50, 200, 145];

const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const artistNames = (t) => (t.artists || []).map((a) => a.name).filter(Boolean).join(", ");
const fmtBpm = (b) => (Number.isInteger(b) ? String(b) : b.toFixed(1));
const fmtDuration = (ms) => {
  if (!ms) return "";
  const s = Math.round(ms / 1000);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
};

const state = {
  genres: [],           // [{name, label, hue, songCount, subgenres: [{name, songs: [{track, weight}]}]}]
  open: new Set(),      // open genres ("g:<name>") and subgenres ("s:<genre>/<subgenre>")
};

function build(tracks, taxonomy) {
  const styleSongs = new Map();
  const familySongs = new Map();
  const add = (map, key, entry) => { if (!map.has(key)) map.set(key, []); map.get(key).push(entry); };
  for (const t of tracks) {
    for (const s of t.profile?.styles || []) if (s.weight >= PRESENCE) add(styleSongs, s.style, { track: t, weight: s.weight });
    for (const f of t.profile?.families || []) if (f.weight >= PRESENCE) add(familySongs, f.family, { track: t, weight: f.weight });
  }
  // Strongest match first, then by name.
  const bySong = (a, b) => b.weight - a.weight || (a.track.track_name || "").localeCompare(b.track.track_name || "");
  const byCount = (a, b) => b.songs.length - a.songs.length || a.name.localeCompare(b.name);
  const styles = taxonomy.styles || {};
  const subgenresOf = (filter) => Object.keys(styles)
    .filter((name) => filter(styles[name]) && styleSongs.has(name))
    .map((name) => ({ name, songs: styleSongs.get(name).slice().sort(bySong) }))
    .sort(byCount);

  state.genres = (taxonomy.families || []).map(({ family }, i) => ({
    name: family,
    label: family,
    hue: HUES[i % HUES.length],
    songCount: (familySongs.get(family) || []).length,
    // The style named like the genre itself ("electronic") is the genre row, not a subgenre.
    subgenres: subgenresOf((s) => (s.families || []).includes(family)).filter((sg) => sg.name !== family),
    // Used when a genre has no subgenres: it then opens straight to these.
    songs: (familySongs.get(family) || []).slice().sort(bySong),
  }));
  // Subgenres that belong to no genre get a top-level line of their own,
  // which opens straight to their songs (grey: no genre colour).
  for (const sg of subgenresOf((s) => !(s.families || []).length)) {
    state.genres.push({ name: sg.name, label: sg.name, hue: null, standalone: true, songs: sg.songs });
  }
}

function songRow(entry) {
  const t = entry.track;
  const bpm = bpmOf(t), key = camelotOf(t);
  const bpmKey = bpm || key ? `${bpm ? fmtBpm(bpm) : "—"} / ${key || "—"}` : "—";
  const src = [bpmSource(t), keySource(t)].includes("ReccoBeats") ? " (tempo/key from ReccoBeats)" : "";
  return `<li class="row song-row">
    <a class="song-name" href="${esc(t.spotify_url)}" target="_blank" rel="noopener" title="Open in Spotify: ${esc(t.track_name)}">${esc(t.track_name)}</a>
    <span class="song-artist" title="${esc(artistNames(t))}">${esc(artistNames(t))}</span>
    <span class="song-meta"><span class="bpmkey" title="BPM / Camelot key${esc(src)}">${esc(bpmKey)}</span><span class="dur">${fmtDuration(t.duration_ms)}</span></span>
  </li>`;
}

function render() {
  $("stack").innerHTML = state.genres.map((g) => {
    const hueStyle = g.hue == null ? "--hue:0;--sat:0%" : `--hue:${g.hue}`;
    // No subgenres to show (a family-less subgenre, or a genre with none):
    // the line opens straight to its songs.
    if (g.standalone || !g.subgenres.length) {
      const key = `${g.standalone ? "x" : "g"}:${g.name}`;
      const open = state.open.has(key);
      return `<li class="genre-block${g.standalone ? " standalone" : ""}" style="${hueStyle}">
        <button type="button" class="row genre-row" data-key="${esc(key)}" aria-expanded="${open}"${g.standalone ? ' title="A subgenre that belongs to no genre"' : ""}>
          <span class="chev" aria-hidden="true">${open ? "▾" : "▸"}</span>
          <span class="row-name">${esc(g.label)}</span>
          <span class="row-count">${g.songs.length} song${g.songs.length === 1 ? "" : "s"}</span>
        </button>
        ${open ? `<ul class="songs">${g.songs.map(songRow).join("")}</ul>` : ""}
      </li>`;
    }
    const gKey = `g:${g.name}`;
    const gOpen = state.open.has(gKey);
    const subs = gOpen ? g.subgenres.map((sg) => {
      const sKey = `s:${g.name}/${sg.name}`;
      const sOpen = state.open.has(sKey);
      return `<li class="sub-block">
        <button type="button" class="row sub-row" data-key="${esc(sKey)}" aria-expanded="${sOpen}">
          <span class="chev" aria-hidden="true">${sOpen ? "▾" : "▸"}</span>
          <span class="row-name">${esc(sg.name)}</span>
          <span class="row-count">${sg.songs.length} song${sg.songs.length === 1 ? "" : "s"}</span>
        </button>
        ${sOpen ? `<ul class="songs">${sg.songs.map(songRow).join("")}</ul>` : ""}
      </li>`;
    }).join("") : "";
    return `<li class="genre-block" style="${hueStyle}">
      <button type="button" class="row genre-row" data-key="${esc(gKey)}" aria-expanded="${gOpen}">
        <span class="chev" aria-hidden="true">${gOpen ? "▾" : "▸"}</span>
        <span class="row-name">${esc(g.label)}</span>
        <span class="row-count">${g.subgenres.length} subgenre${g.subgenres.length === 1 ? "" : "s"} · ${g.songCount} songs</span>
      </button>
      ${gOpen ? `<ul class="subs">${subs}</ul>` : ""}
    </li>`;
  }).join("");
}

async function getJson(path) {
  const res = await fetch(path);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.message || `Request failed (HTTP ${res.status})`);
  return data;
}

async function init() {
  let lib, tax;
  try {
    [lib, tax] = await Promise.all([getJson("/api/library"), getJson("/api/taxonomy")]);
  } catch (err) {
    $("disc-missing").hidden = false;
    $("disc-missing-message").innerHTML = esc(err.message).replace(/`([^`]+)`/g, "<code>$1</code>");
    return;
  }
  if (!tax.taxonomy || !(tax.taxonomy.families || []).length) {
    $("disc-missing").hidden = false;
    $("disc-missing-message").textContent = "The library has no genre taxonomy yet. Run the genres step (and the tags step if you have a Last.fm key) to build it.";
    return;
  }
  const tracks = lib.tracks.filter((t) => t.track_id);
  build(tracks, tax.taxonomy);
  // Distinct subgenres: one appearing in two genres counts once; standalone lines count too.
  const distinct = new Set(state.genres.flatMap((g) => (g.standalone ? [g.name] : g.subgenres.map((s) => s.name)))).size;
  $("disc-stats").textContent = `${state.genres.filter((g) => !g.standalone).length} genres · ${distinct} subgenres · ${tracks.length} songs`;
  $("disc-main").hidden = false;

  $("stack").addEventListener("click", (e) => {
    const row = e.target.closest("button.row[data-key]");
    if (!row) return;
    const key = row.dataset.key;
    state.open.has(key) ? state.open.delete(key) : state.open.add(key);
    render();
    // Keep the row that was clicked in view after the content below it changes.
    document.querySelector(`button.row[data-key="${CSS.escape(key)}"]`)?.focus({ preventScroll: true });
  });
  render();
}

init();
