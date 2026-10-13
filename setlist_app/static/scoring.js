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

// Weights are 0-100 relevance. A track "has" a word for mood purposes from
// this weight up.
const MIN_TAG_WEIGHT = 10;

// Spelling-insensitive identity, so "hyper pop", "Hyperpop" and "hyper-pop"
// are one mood (and moods saved with older setlists still match).
const tagKey = (s) => String(s || "").toLowerCase().replace(/&/g, " and ").replace(/[^a-z0-9]+/g, "");

// A song's mood words: its merged genre profile (styles + vibes, built from
// Last.fm, Deezer and MusicBrainz by setlist_app/taxonomy.py) plus its raw
// Last.fm tags, keeping the strongest weight per word. {key: {tag, weight}}.
const moodWordCache = new WeakMap();
function moodWords(t) {
  if (t && typeof t === "object" && moodWordCache.has(t)) return moodWordCache.get(t);
  const words = new Map();
  const add = (tag, weight) => {
    const key = tagKey(tag);
    if (!key) return;
    const current = words.get(key);
    if (!current) words.set(key, { tag, weight });
    else if (weight > current.weight) current.weight = weight;  // keep the profile's spelling
  };
  for (const s of t?.profile?.styles || []) add(s.style, s.weight);
  for (const v of t?.profile?.vibes || []) add(v.vibe, v.weight);
  for (const x of t?.tags || []) add(x.tag, x.weight);
  if (t && typeof t === "object") moodWordCache.set(t, words);
  return words;
}
const trackTags = (t) => [...moodWords(t).values()];

// The mood vocabulary is whatever words exist in this library, with how many
// tracks carry each one. Nothing is hardcoded.
function tagVocabulary(tracks) {
  const counts = new Map();  // key -> {count, spellings}
  for (const t of tracks) {
    for (const [key, { tag, weight }] of moodWords(t)) {
      if (weight < MIN_TAG_WEIGHT) continue;
      const entry = counts.get(key) || { count: 0, spellings: new Map() };
      entry.count++;
      entry.spellings.set(tag, (entry.spellings.get(tag) || 0) + 1);
      counts.set(key, entry);
    }
  }
  return [...counts.values()]
    .map(({ count, spellings }) => ({ tag: [...spellings].sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : 1))[0][0], count }))
    .sort((a, b) => b.count - a.count || a.tag.localeCompare(b.tag));
}

// 0-1: the average of the track's weight for each selected word (0 where it
// lacks one). Matching all selected words strongly scores highest, but
// matching only some still counts: overlap is weighted, not required.
function moodRelevance(track, selected) {
  if (!selected.length) return { score: 1, matched: [] };
  const words = moodWords(track);
  const weightOf = (tag) => words.get(tagKey(tag))?.weight || 0;
  const matched = selected.filter((tag) => weightOf(tag) >= MIN_TAG_WEIGHT)
    .map((tag) => ({ tag, weight: weightOf(tag) }));
  const score = selected.reduce((sum, tag) => sum + Math.min(weightOf(tag), 100), 0) / (100 * selected.length);
  return { score, matched };
}

function moodPool(tracks, selected) {
  return tracks
    .map((track) => ({ track, ...moodRelevance(track, selected) }))
    .filter((c) => c.matched.length > 0);
}

// ---------------------------------------------------------------------------
// Genre similarity
//
// Each song's `profile` (merged Last.fm + Deezer + MusicBrainz, see
// setlist_app/taxonomy.py) has weighted styles and weighted families. Two songs
// are compared by the cosine of their style vectors and of their family
// vectors, blended: the style part rewards exact matches ("riddim" + "riddim"),
// the family part gives partial credit to related styles ("riddim" vs
// "brostep" share the dance/electro families). Each word is IDF-weighted over
// the library, so a word on half of all songs ("dance") counts far less than a
// rare one. `vibes` (places, eras) are deliberately ignored.
// ---------------------------------------------------------------------------

const GENRE_MIN_WEIGHT = 10;   // ignore very weak styles/families
const STYLE_SHARE = 0.6;       // style cosine vs family cosine in the blend

let genreIdf = { style: new Map(), family: new Map() };
let genreVectors = new WeakMap();

// Call once after the library loads (and again if it changes).
function buildGenreModel(tracks) {
  const df = { style: new Map(), family: new Map() };
  let n = 0;
  const count = (map, name) => map.set(name, (map.get(name) || 0) + 1);
  for (const t of tracks) {
    const styles = (t.profile?.styles || []).filter((s) => s.weight >= GENRE_MIN_WEIGHT);
    const families = (t.profile?.families || []).filter((f) => f.weight >= GENRE_MIN_WEIGHT);
    if (!styles.length && !families.length) continue;
    n++;
    styles.forEach((s) => count(df.style, s.style));
    families.forEach((f) => count(df.family, f.family));
  }
  const idf = (map) => new Map([...map].map(([name, c]) => [name, Math.log((n + 1) / (c + 1)) + 1]));
  genreIdf = { style: idf(df.style), family: idf(df.family) };
  genreVectors = new WeakMap();
}

function weightedVector(items, nameKey, idf) {
  const v = new Map();
  let sumSquares = 0;
  for (const item of items || []) {
    if (item.weight < GENRE_MIN_WEIGHT) continue;
    const x = (item.weight / 100) * (idf.get(item[nameKey]) ?? 1);
    v.set(item[nameKey], x);
    sumSquares += x * x;
  }
  return sumSquares ? { v, norm: Math.sqrt(sumSquares) } : null;
}

function genreVector(track) {
  if (!track || typeof track !== "object") return null;
  if (genreVectors.has(track)) return genreVectors.get(track);
  const p = track.profile;
  const vec = p && {
    styles: weightedVector(p.styles, "style", genreIdf.style),
    families: weightedVector(p.families, "family", genreIdf.family),
  };
  const result = vec && (vec.styles || vec.families) ? vec : null;
  genreVectors.set(track, result);
  return result;
}

function cosine(a, b) {
  if (!a || !b) return null;
  const [small, large] = a.v.size <= b.v.size ? [a, b] : [b, a];
  let dot = 0;
  for (const [name, x] of small.v) {
    const y = large.v.get(name);
    if (y) dot += x * y;
  }
  return dot / (a.norm * b.norm);
}

function sharedNames(a, b) {
  if (!a || !b) return [];
  const shared = [];
  for (const [name, x] of a.v) {
    const y = b.v.get(name);
    if (y) shared.push([name, Math.min(x, y)]);
  }
  return shared.sort((p, q) => q[1] - p[1] || (p[0] < q[0] ? -1 : 1)).map(([name]) => name);
}

// null when either song has no genre profile yet.
function genreSimilarity(a, b) {
  const va = genreVector(a), vb = genreVector(b);
  if (!va || !vb) return null;
  const style = cosine(va.styles, vb.styles), family = cosine(va.families, vb.families);
  const similarity = style != null && family != null ? STYLE_SHARE * style + (1 - STYLE_SHARE) * family : style ?? family;
  if (similarity == null) return null;
  return {
    similarity, style, family,
    shared: sharedNames(va.styles, vb.styles).slice(0, 3),
    sharedFamilies: sharedNames(va.families, vb.families).slice(0, 2),
  };
}

// ---------------------------------------------------------------------------
// Transition cost
//
// Every component is 0 (ideal) .. 1 (bad); the cost is their weighted
// average, so scores are comparable and each part can be shown as a reason.
// Missing data gets a middling cost rather than 0 or 1: an unknown BPM isn't
// a good transition, but it isn't a known clash either.
// ---------------------------------------------------------------------------

const WEIGHTS = { bpm: 0.30, key: 0.25, energy: 0.15, genre: 0.20, valence: 0.10, mood: 0.15 };
const UNKNOWN_COST = { bpm: 0.6, key: 0.6, energy: 0.5, genre: 0.5, valence: 0.5 };
const BPM_PCT_AT_MAX_COST = 8;     // ≥8% tempo change counts as a hard jump
const ENERGY_AT_MAX_COST = 40;     // energy points (0-100)
const VALENCE_AT_MAX_COST = 40;    // valence points (0-100, sad -> happy)

// ReccoBeats energy (0-100). Deliberately no fallback to GetSongBPM's danceability:
// it's a different measure on a different scale, so mixing them would distort comparisons.
const energyOf = (t) => t?.features?.energy ?? null;
// Tempo and key: GetSongBPM where it has the song, else ReccoBeats. They agree
// on 96% of tempos (allowing half/double time) and 90% of keys where both
// exist, and the fallback lifts tempo+key coverage from ~19% to ~74%.
const bpmOf = (t) => t?.enrichment?.bpm || t?.features?.tempo || null;
const camelotOf = (t) => t?.enrichment?.camelot || t?.features?.camelot || null;
const keyNameOf = (t) => (t?.enrichment?.camelot ? t.enrichment.key : t?.features?.key) || null;
const bpmSource = (t) => (t?.enrichment?.bpm ? "GetSongBPM" : t?.features?.tempo ? "ReccoBeats" : null);
const keySource = (t) => (t?.enrichment?.camelot ? "GetSongBPM" : t?.features?.camelot ? "ReccoBeats" : null);
const valenceOf = (t) => t?.features?.valence ?? null;   // ReccoBeats

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
  const genre = genreSimilarity(prev, cand);
  parts.genre = genre ? 1 - genre.similarity : UNKNOWN_COST.genre;
  const vPrev = valenceOf(prev), vCand = valenceOf(cand);
  parts.valence = vPrev != null && vCand != null
    ? Math.min(Math.abs(vCand - vPrev) / VALENCE_AT_MAX_COST, 1)
    : UNKNOWN_COST.valence;
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
    valenceDelta: vPrev != null && vCand != null ? vCand - vPrev : null,
    genre,
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
// (a "chill" pool and a "riddim" pool have very different energy).
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
  // Different releases of the same recording share an ISRC. An identical copy
  // looks like a perfect transition, so without this the greedy step would
  // happily play the same song twice in a row.
  const take = (i, step) => {
    const c = pool[i];
    chosen.push(c);
    steps.push(step);
    elapsedMs += c.track.duration_ms || 0;
    remaining.delete(i);
    if (c.track.isrc) for (const j of [...remaining]) if (pool[j].track.isrc === c.track.isrc) remaining.delete(j);
  };

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
    take(best.i, { cost: best.cost, target: best.target, opening: true });
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
    take(best.i, best.t);
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
  // Skip other releases of recordings already in the set (same ISRC).
  const setIsrcs = new Set(tracks.filter((t) => inSet.has(t.track_id) && t.isrc).map((t) => t.isrc));
  let candidates = tracks
    .filter((t) => !inSet.has(t.track_id) && !(t.isrc && setIsrcs.has(t.isrc)))
    .map((track) => ({ track, score: null, matched: [] }));
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
    buildGenreModel, genreSimilarity, valenceOf, energyOf, bpmOf, camelotOf, bpmSource, keySource, tagKey, trackTags,
  };
}
