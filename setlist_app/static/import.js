"use strict";

// Import page: upload an Exportify CSV into the shared library, then show
// what happened and how enrichment is progressing.

const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const n = (x) => Number(x || 0).toLocaleString();
let chosen = null;
let pollTimer = null;

function choose(file) {
  chosen = file || null;
  $("file-name").textContent = chosen ? `${chosen.name} · ${(chosen.size / 1024 / 1024).toFixed(1)} MB` : "Up to 15 MB";
  $("drop").classList.toggle("has-file", Boolean(chosen));
  $("upload").disabled = !chosen;
}

function renderResult(data) {
  const r = data.report, c = data.counts;
  const skipped = r.skipped_local + r.skipped_invalid + r.duplicates_in_file;
  $("result").hidden = false;
  $("result").className = "result ok";
  $("result").innerHTML = `
    <p><b>${n(r.valid)}</b> song${r.valid === 1 ? "" : "s"} read from ${n(r.rows)} rows:
      <b>${n(c.new)}</b> new to the library, ${n(c.updated)} updated with extra details, ${n(c.unchanged)} already here.</p>
    ${skipped ? `<p class="muted">Skipped ${n(r.skipped_local)} local files, ${n(r.skipped_invalid)} unusable rows and ${n(r.duplicates_in_file)} repeats.</p>` : ""}
    ${r.notes.map((note) => `<p class="muted">Note: ${esc(note)}</p>`).join("")}
    <p>${c.new ? `${data.enrichment_queued ? "Extra data for the new songs is being added in the background. " : ""}` : ""}<a href="/">Open the setlist builder →</a></p>`;
}

function renderError(message) {
  $("result").hidden = false;
  $("result").className = "result err";
  $("result").textContent = message;
}

async function upload(e) {
  e.preventDefault();
  if (!chosen) return;
  $("upload").disabled = true;
  $("upload").textContent = "Importing…";
  try {
    const form = new FormData();
    form.append("file", chosen);
    const res = await fetch("/api/import", { method: "POST", body: form });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.message || `Import failed (HTTP ${res.status})`);
    renderResult(data);
    choose(null);
    $("file").value = "";
    refreshStatus();
  } catch (err) {
    renderError(err.message);
  } finally {
    $("upload").textContent = "Import songs";
    $("upload").disabled = !chosen;
  }
}

async function refreshStatus() {
  clearTimeout(pollTimer);
  let s;
  try {
    s = await (await fetch("/api/status")).json();
  } catch {
    return;
  }
  const lib = s.library || {}, pend = s.pending || {}, en = s.enrichment || {};
  $("lib-stats").textContent = `${n(lib.tracks)} songs in the library · ${n(lib.imports)} imports`;
  const waiting = Math.max(pend.bpm || 0, pend.tags || 0, pend.genres || 0, pend.features || 0);
  const busy = en.phase && en.phase !== "idle";
  $("status").innerHTML = [
    `<span><b>${n(lib.tracks)}</b> songs</span>`,
    `<span><b>${n(lib.imports)}</b> imports</span>`,
    `<span>enrichment: <b>${en.enabled === false ? "off" : esc(en.phase || "idle")}</b></span>`,
    waiting ? `<span><b>${n(waiting)}</b> songs still waiting for extra data</span>` : `<span>all songs enriched</span>`,
    en.last_error ? `<span class="err-text">last problem: ${esc(en.last_error)}</span>` : "",
  ].join("");
  refreshQueue();
  if (busy || waiting) pollTimer = setTimeout(refreshStatus, 10000);
}

async function refreshQueue() {
  let q;
  try {
    q = await (await fetch("/api/queue")).json();
  } catch {
    return;
  }
  const running = q.enrichment?.phase && q.enrichment.phase !== "idle";
  $("queue-count").textContent = q.total ? `· ${n(q.total)} song${q.total === 1 ? "" : "s"}${running ? ` · ${q.enrichment.phase}` : ""}` : "";
  $("queue-empty").hidden = q.total > 0;
  $("queue").innerHTML = q.tracks.map((t) => `<li><div>
      <span class="pl-title"><a href="https://open.spotify.com/track/${encodeURIComponent(t.track_id)}" target="_blank" rel="noopener">${esc(t.track_name)}</a></span>
      <span class="pl-artist">${esc(t.artists.join(", "))}</span>
      <span class="pl-meta">${t.waiting_for.map((s) => `<span class="q-step">${esc(s)}</span>`).join("")}</span>
      <span class="pl-dur" title="Added to the library">${t.added_at ? esc(new Date(t.added_at).toLocaleDateString()) : ""}</span>
    </div></li>`).join("");
  $("queue-more").hidden = q.total <= q.tracks.length;
  $("queue-more").textContent = `…and ${n(q.total - q.tracks.length)} more.`;
}

$("file").addEventListener("change", (e) => choose(e.target.files[0]));
const drop = $("drop");
["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (e) => choose(e.dataTransfer.files[0]));
$("upload-form").addEventListener("submit", upload);
refreshStatus();
