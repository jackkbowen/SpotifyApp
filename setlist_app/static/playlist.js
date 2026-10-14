"use strict";

// Read-only share page for a playlist: /p/<id>

const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtDuration = (ms) => {
  if (!ms) return "";
  const s = Math.round(ms / 1000), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}` : `${m}:${String(sec).padStart(2, "0")}`;
};
const fmtBpm = (b) => (Number.isInteger(b) ? String(b) : Number(b).toFixed(1));

let toastTimer;
function toast(message) {
  const el = $("toast");
  el.textContent = message;
  el.className = "toast show";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.className = "toast"), 3000);
}

async function copyText(text, message) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = Object.assign(document.createElement("textarea"), { value: text });
    document.body.append(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
  toast(message);
}

async function init() {
  const id = decodeURIComponent(location.pathname.split("/").pop() || "");
  const res = await fetch(`/api/playlists/${encodeURIComponent(id)}?details=1`);
  if (!res.ok) {
    $("pl-missing").hidden = false;
    return;
  }
  const p = await res.json();
  document.title = `${p.name} · Playlist`;
  $("pl").hidden = false;
  $("pl-name").textContent = p.name;
  const rows = p.details || [];
  const total = rows.reduce((sum, r) => sum + (r.duration_ms || 0), 0);
  $("pl-facts").innerHTML = [
    `<span><b>${rows.length}</b> tracks</span>`,
    `<span><b>${fmtDuration(total)}</b> total</span>`,
    p.mood_tags?.length ? `<span>mood: ${p.mood_tags.map(esc).join(", ")}</span>` : "",
    `<span>updated ${esc(new Date(p.updated_at).toLocaleDateString())}</span>`,
  ].join("");
  const base = `/api/playlists/${encodeURIComponent(id)}/export`;
  $("dl-csv").href = `${base}?format=csv`;
  $("dl-m3u").href = `${base}?format=m3u`;
  $("open-copy").href = `/?copy=${encodeURIComponent(id)}`;
  $("copy-links").addEventListener("click", () =>
    copyText(rows.map((r) => r.spotify_url).join("\n"), `Copied ${rows.length} track links. Paste them into a Spotify playlist.`));
  $("pl-tracks").innerHTML = rows.map((r) => `<li><div>
      <span class="pl-title"><a href="${esc(r.spotify_url)}" target="_blank" rel="noopener">${esc(r.track_name)}</a></span>
      <span class="pl-artist">${esc((r.artists || []).join(", "))}</span>
      <span class="pl-meta" title="BPM / Camelot key">${r.bpm ? fmtBpm(r.bpm) : "—"} / ${esc(r.camelot || "—")}</span>
      <span class="pl-dur">${fmtDuration(r.duration_ms)}</span>
    </div></li>`).join("");
}

init();
