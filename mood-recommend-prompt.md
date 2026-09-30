This is phase 3 of an existing project. Read the existing code in this repo first (the phase 1 export script and the phase 2 browse/setlist app built from `setlist-app-prompt.md`) and extend it — don't rebuild from scratch. Two new capabilities: (1) build setlists automatically from a mood/vibe, and (2) recommend the next track while manually building a setlist.

## Context: why this needs new data, and where it comes from

The app currently enriches tracks with BPM/key/danceability/acousticness from GetSongBPM (keyed by ISRC). That's numeric data — it can't tell you a track is "chill" or "dubstep" or "future bass." To support real mood/vibe selection, add a second enrichment source that pulls actual descriptive tags:

- **Last.fm's `track.getTopTags`** (falling back to `artist.getTopTags` when a track has few/no tags, which will be common for niche or very new electronic tracks) — free for non-commercial/personal use with an API key from Last.fm, no user auth needed for these read endpoints. Look tracks up by artist name + track name (Last.fm doesn't key by ISRC). Respect their rate limiting (back off and retry on their rate-limit error code) and keep total stored data well under their 100MB cap, which won't be an issue at this scale. Last.fm requires attribution — add a small "tag data from Last.fm" credit somewhere visible in the UI (footer or settings/about screen is fine).
- **Spotify's own artist genres** via `GET /artists/{id}` (this single-artist endpoint still returns a `genres` array and was not among the endpoints restricted in the 2024/Feb-2026 API changes — unlike the batch artist endpoint, call it one artist at a time). We already have every unique artist ID from the original library export, so this is a straightforward one-request-per-artist batch job.

Merge both sources into a single normalized `tags` list per track in `library_enriched.json` (or wherever the enriched data currently lives — check the existing schema before changing it). Normalization matters here: Last.fm tags are crowdsourced and noisy — lowercase everything, merge obvious duplicates/variants (e.g. "Dubstep" / "dubstep" / "DUBSTEP"), and filter out non-descriptive junk tags that don't describe genre/mood (things like "seen live," "favorite," "awesome," usernames, or a tag that's just the artist's own name repeated). Keep a tag-frequency count per track so more-confident/common tags can be weighted higher than one-off noise.

Cache both sources to disk (e.g. `lastfm_tag_cache.json` keyed by artist+track, `spotify_artist_genres_cache.json` keyed by artist_id) so this enrichment step, like the BPM one, can be re-run incrementally for newly-liked songs without refetching everything.

Do not hardcode a fixed list of moods/genres anywhere — the app should derive its available mood/vibe vocabulary dynamically from whatever tags actually exist across the enriched library, so it reflects this specific library rather than a generic assumption.

## Feature 1: Generate a draft setlist from a mood/vibe

Add a "Generate Setlist" flow, separate from (but able to hand off into) the existing manual setlist builder:

- Let me pick one or more mood/vibe tags from the vocabulary derived above (a searchable/autocomplete tag picker, since there could be a lot of tags — show tag frequency so I can see which ones are actually well-represented in my library vs. rare).
- Let me set a target setlist length (either a track count or a target total duration).
- Let me optionally pick an energy arc shape for how the set should progress: **build** (starts lower energy/danceability, ends higher), **peak-then-cool** (rises then eases off), **plateau** (stays roughly consistent), or **free** (no energy-arc constraint, just optimize transitions).
- Candidate pool = tracks whose tags intersect my selected mood tag(s) above some reasonable relevance threshold (don't require an exact/all-tags match if that pool would be too small — fall back to weighting by tag overlap rather than a hard filter, and tell me if the pool ended up small).
- Construct the ordered draft with a straightforward greedy nearest-neighbor approach: pick a sensible starting track for the chosen arc, then repeatedly select the next track from the remaining candidate pool that best minimizes a weighted "transition cost" — combining BPM difference, Camelot-wheel key compatibility (reuse the compatibility logic already built in phase 2), and how well the energy/danceability change fits the target arc at that point in the set — until the target length is reached or the pool runs out.
- The result should open directly in the existing setlist builder as an editable draft — I want to review, reorder, swap, or remove tracks before saving, not have this be a black box that locks in.
- If the candidate pool can't reach my target length, tell me clearly rather than silently padding it with poor-fit tracks or tracks outside the mood filter.

## Feature 2: Recommend next track (within manual setlist building)

In the existing setlist builder, add a "Suggest next track" action next to the last track in my current working setlist:

- Rank all tracks not already in the current setlist by the same weighted transition-cost function used above (BPM closeness, key compatibility, energy continuity relative to the last track in the list).
- If a mood/vibe filter is currently active for this session (e.g. I started from a generated draft, or manually set one), restrict/weight suggestions toward that tag pool too; otherwise rank across the whole library.
- Show a ranked list (top ~10-15 is plenty) with the reasoning visible per suggestion — e.g. BPM delta, the key relationship label (same key / compatible / clash), and energy delta — not just a bare ranked list with no explanation, since the point is to help me judge the transition, not just trust a score blindly.
- Clicking a suggestion adds it to the setlist at the end, same as manually adding one now.

## Explicitly out of scope / decisions already made

- No variety/repetition constraints — don't avoid repeated artists or deprioritize tracks used in past saved setlists. Pure compatibility + mood-fit scoring only, kept simple and predictable.
- Don't change or duplicate the existing BPM/key enrichment pipeline — this adds a second, independent tag-enrichment pipeline alongside it.
- Still fully local, no hosting, no new external accounts beyond the two new free API keys (Last.fm, and reusing the existing Spotify credentials — no new Spotify scopes needed since `GET /artists/{id}` doesn't require user auth beyond the existing token).
- Don't build any ML/embedding-based similarity — the weighted numeric scoring approach above is intentional so behavior stays explainable and debuggable.

## Code quality expectations

- Update the README to cover: getting a Last.fm API key, adding it to `.env`, and running the new tag-enrichment step (clarify whether it's a new script or an added mode of the existing enrichment script — your call, but don't make me run three separate scripts if two can reasonably be one with a flag).
- Handle missing/thin tag data gracefully per track (some tracks may end up with zero tags after cleaning) — those tracks just won't surface in mood-filtered results, that's fine, don't error out.
