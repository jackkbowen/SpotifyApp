"use strict";

// Pure logic shared by the manual builder, "Generate setlist" and "Suggest
// next track": Camelot/BPM transition rules, mood tag matching, and one
// weighted transition-cost function. No DOM access, so it also runs under
// Node for testing (see the module.exports at the bottom).

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
// Mood tags
// ---------------------------------------------------------------------------

// Tag weights are Last.fm's 0-100 relevance (artist-level tags at half).
// A track "has" a tag for mood purposes from this weight up.
const MIN_TAG_WEIGHT = 10;

const trackTags = (t) => t.tags || [];

// The mood vocabulary is whatever tags exist in this library, with how many
// tracks carry each one. Nothing is hardcoded.
function tagVocabulary(tracks) {
  const counts = new Map();
  for (const t of tracks) {
    for (const { tag, weight } of trackTags(t)) {
      if (weight >= MIN_TAG_WEIGHT) counts.set(tag, (counts.get(tag) || 0) + 1);
    }
  }
  return [...counts].map(([tag, count]) => ({ tag, count }))
    .sort((a, b) => b.count - a.count || a.tag.localeCompare(b.tag));
}

// 0-1: the average of the track's weight for each selected tag (0 where it
// lacks one). Matching all selected tags strongly scores highest, but
// matching only some still counts: overlap is weighted, not required.
function moodRelevance(track, selected) {
  if (!selected.length) return { score: 1, matched: [] };
  const byTag = new Map(trackTags(track).map((t) => [t.tag, t.weight]));
  const matched = selected.filter((tag) => (byTag.get(tag) || 0) >= MIN_TAG_WEIGHT)
    .map((tag) => ({ tag, weight: byTag.get(tag) }));
  const score = selected.reduce((sum, tag) => sum + Math.min(byTag.get(tag) || 0, 100), 0) / (100 * selected.length);
  return { score, matched };
}

function moodPool(tracks, selected) {
  return tracks
    .map((track) => ({ track, ...moodRelevance(track, selected) }))
    .filter((c) => c.matched.length > 0);
}

// ---------------------------------------------------------------------------
// Transition cost
//
// Every component is 0 (ideal) .. 1 (bad); the cost is their weighted
// average, so scores are comparable and each part can be shown as a reason.
// Missing data gets a middling cost rather than 0 or 1: an unknown BPM isn't
// a good transition, but it isn't a known clash either.
// ---------------------------------------------------------------------------

const WEIGHTS = { bpm: 0.35, key: 0.3, energy: 0.2, mood: 0.15 };
const UNKNOWN_COST = { bpm: 0.6, key: 0.6, energy: 0.5 };
const BPM_PCT_AT_MAX_COST = 8;     // ≥8% tempo change counts as a hard jump
const ENERGY_AT_MAX_COST = 40;     // danceability points

const energyOf = (t) => t?.enrichment?.danceability ?? null;
const bpmOf = (t) => t?.enrichment?.bpm ?? null;
const camelotOf = (t) => t?.enrichment?.camelot ?? null;

const KEY_COSTS = { "same key": 0, "+1": 0.1, "−1": 0.1, relative: 0.15, clash: 1 };

// target: the energy the arc wants at this point (null = keep energy
// continuous with the previous track instead). moodScore: null when no mood
// is active.
function transition(prev, cand, { target = null, moodScore = null } = {}) {
  const bpm = bpmRelation(bpmOf(prev), bpmOf(cand));
  const key = keyRelation(camelotOf(prev), camelotOf(cand));
  const ePrev = energyOf(prev), eCand = energyOf(cand);

  const parts = {
    bpm: bpm ? Math.min(Math.abs(bpm.pct) / BPM_PCT_AT_MAX_COST, 1) : UNKNOWN_COST.bpm,
    key: key ? KEY_COSTS[key.label] : UNKNOWN_COST.key,
  };
  if (eCand == null) parts.energy = UNKNOWN_COST.energy;
  else if (target != null) parts.energy = Math.min(Math.abs(eCand - target) / ENERGY_AT_MAX_COST, 1);
  else if (ePrev != null) parts.energy = Math.min(Math.abs(eCand - ePrev) / ENERGY_AT_MAX_COST, 1);
  else parts.energy = UNKNOWN_COST.energy;
  if (moodScore != null) parts.mood = 1 - moodScore;

  let total = 0, weightSum = 0;
  for (const [name, value] of Object.entries(parts)) {
    total += WEIGHTS[name] * value;
    weightSum += WEIGHTS[name];
  }
  return {
    cost: total / weightSum,
    parts,
    bpm,
    key,
    energyDelta: ePrev != null && eCand != null ? eCand - ePrev : null,
    target,
  };
}

// ---------------------------------------------------------------------------
// Energy arcs
// ---------------------------------------------------------------------------

const ARC_SHAPES = {
  build: "Build",
  peak: "Peak then cool",
  plateau: "Plateau",
  free: "Free",
};

function percentile(sorted, p) {
  if (!sorted.length) return null;
  const i = Math.min(sorted.length - 1, Math.max(0, Math.round(p * (sorted.length - 1))));
  return sorted[i];
}

// Low/high energy for this pool, so arcs use the range the mood actually has
// (a "chill" pool and a "riddim" pool have very different danceability).
function energyRange(tracks) {
  const values = tracks.map(energyOf).filter((v) => v != null).sort((a, b) => a - b);
  if (values.length < 3) return null;
  return { lo: percentile(values, 0.2), hi: percentile(values, 0.8) };
}

// Target energy at progress p (0 = first track, 1 = last).
function arcTarget(shape, p, range) {
  if (!range || shape === "free") return null;
  const { lo, hi } = range;
  if (shape === "build") return lo + (hi - lo) * p;
  if (shape === "plateau") return (lo + hi) / 2;
  if (shape === "peak") {
    const peakAt = 0.65;
    return p <= peakAt
      ? lo + (hi - lo) * (p / peakAt)
      : hi - (hi - lo) * 0.5 * ((p - peakAt) / (1 - peakAt));
  }
  return null;
}

// ---------------------------------------------------------------------------
// Generate a draft setlist (greedy nearest neighbour)
// ---------------------------------------------------------------------------

const byId = (a, b) => (a.track.track_id < b.track.track_id ? -1 : 1);

function generateSetlist(tracks, { tags, shape = "free", count = null, minutes = null }) {
  if (!tags.length) throw new Error("Pick at least one mood/vibe tag.");
  const pool = moodPool(tracks, tags).sort(byId);
  const targetMs = minutes ? minutes * 60000 : null;
  const targetCount = count || null;
  const range = energyRange(pool.map((c) => c.track));

  // How far through the set we are, for the arc target.
  const progress = (n, elapsedMs) => {
    if (targetMs) return Math.min(elapsedMs / targetMs, 1);
    return targetCount > 1 ? n / (targetCount - 1) : 0;
  };

  const chosen = [];
  const steps = [];
  let elapsedMs = 0;
  const remaining = new Set(pool.map((_, i) => i));
  const done = () => (targetCount ? chosen.length >= targetCount : elapsedMs >= targetMs);

  // Opening track: best mood fit near the arc's starting energy, preferring
  // tracks with known BPM/key so the first transitions can be judged.
  if (pool.length) {
    const target = arcTarget(shape, 0, range);
    let best = null;
    for (const i of remaining) {
      const c = pool[i];
      const e = energyOf(c.track);
      const energyCost = target == null ? 0 : e == null ? UNKNOWN_COST.energy : Math.min(Math.abs(e - target) / ENERGY_AT_MAX_COST, 1);
      const dataCost = (bpmOf(c.track) ? 0 : 0.5) + (camelotOf(c.track) ? 0 : 0.5);
      const cost = WEIGHTS.mood * (1 - c.score) + WEIGHTS.energy * energyCost + 0.3 * dataCost;
      if (!best || cost < best.cost) best = { i, cost, target };
    }
    remaining.delete(best.i);
    chosen.push(pool[best.i]);
    steps.push({ cost: best.cost, target: best.target, opening: true });
    elapsedMs += pool[best.i].track.duration_ms || 0;
  }

  while (!done() && remaining.size) {
    const prev = chosen[chosen.length - 1].track;
    const target = arcTarget(shape, progress(chosen.length, elapsedMs), range);
    let best = null;
    for (const i of remaining) {
      const c = pool[i];
      const t = transition(prev, c.track, { target, moodScore: c.score });
      if (!best || t.cost < best.t.cost) best = { i, t };
    }
    remaining.delete(best.i);
    chosen.push(pool[best.i]);
    steps.push(best.t);
    elapsedMs += pool[best.i].track.duration_ms || 0;
  }

  const shortfall = !done();
  const withData = chosen.filter((c) => bpmOf(c.track) && camelotOf(c.track)).length;
  return {
    trackIds: chosen.map((c) => c.track.track_id),
    steps,
    poolSize: pool.length,
    poolWithData: pool.filter((c) => bpmOf(c.track) && camelotOf(c.track)).length,
    withData,
    elapsedMs,
    shortfall,
    range,
    shape,
  };
}

// ---------------------------------------------------------------------------
// Suggest the next track
// ---------------------------------------------------------------------------

function suggestNext(tracks, setIds, { tags = [], limit = 12 } = {}) {
  if (!setIds.length) return { suggestions: [], candidates: 0 };
  const inSet = new Set(setIds);
  const last = tracks.find((t) => t.track_id === setIds[setIds.length - 1]);
  let candidates = tracks.filter((t) => !inSet.has(t.track_id)).map((track) => ({ track, score: null, matched: [] }));
  if (tags.length) {
    // With a mood active, only tracks carrying at least one of its tags.
    candidates = candidates.map((c) => ({ ...c, ...moodRelevance(c.track, tags) })).filter((c) => c.matched.length);
  }
  const ranked = candidates
    .map((c) => ({ ...c, t: transition(last, c.track, { moodScore: tags.length ? c.score : null }) }))
    .sort((a, b) => a.t.cost - b.t.cost || (a.track.track_id < b.track.track_id ? -1 : 1));
  return { suggestions: ranked.slice(0, limit), candidates: candidates.length, last };
}

if (typeof module !== "undefined") {
  module.exports = {
    CAMELOT_KEYS, CAMELOT_NAMES, parseCamelot, keyRelation, compatibleKeys, bpmRelation,
    MIN_TAG_WEIGHT, tagVocabulary, moodRelevance, moodPool,
    WEIGHTS, transition, ARC_SHAPES, energyRange, arcTarget, generateSetlist, suggestNext,
  };
}
