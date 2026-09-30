Build a local desktop/web app that lets me browse my Spotify Liked Songs library (enriched with BPM/key/energy data) and build and save DJ setlists from it. This is phase 2 of a project — phase 1 (a separate script) already exported my full library to `liked_songs.json`.

## Context: the existing data

I have a `liked_songs.json` file with this shape (1,396 tracks currently, will grow over time as I like more songs):

```json
{
  "exported_at": "2026-09-30T04:33:40+00:00",
  "source": "Spotify Web API GET /me/tracks",
  "track_count": 1396,
  "tracks": [
    {
      "added_at": "2026-09-29T15:00:07Z",
      "track_id": "2eS2KdsCg86Ovan8HVGmJc",
      "track_uri": "spotify:track:2eS2KdsCg86Ovan8HVGmJc",
      "track_name": "DIRECT IT 2 THE ROOF",
      "artists": [{"id": "...", "name": "Juelz", "uri": "..."}],
      "album": {
        "id": "...", "uri": "...", "name": "DRAMATICA",
        "album_type": "album", "release_date": "2026-08-21",
        "release_date_precision": "day", "artists": [...]
      },
      "duration_ms": 195692,
      "explicit": true,
      "isrc": "QM24S2605145",
      "disc_number": 1,
      "track_number": 11,
      "is_local": false,
      "spotify_url": "https://open.spotify.com/track/2eS2KdsCg86Ovan8HVGmJc",
      "spotify_raw": { /* full nested Spotify API response, redundant with the above, ignore/drop this */ }
    }
  ]
}
```

Known facts about this data (confirmed by inspecting my actual export, don't assume otherwise):
- 100% of tracks have a valid ISRC — use ISRC as the primary key for external metadata lookups, it's far more reliable than fuzzy artist+title matching.
- No genre field exists anywhere (Spotify doesn't return genre at the track level). Don't build genre filtering unless we later derive it from another source — leave it out of v1.
- Some tracks share a duplicate ISRC (different releases of the same recording) — dedupe by ISRC when doing lookups (cache the result and reuse it for all tracks sharing that ISRC), but keep both as separate entries in the browsable library since they're technically different Spotify track_ids.
- `spotify_raw` roughly triples the file size for no benefit in this app — strip it out when ingesting.
- My library skews electronic/bass music (dubstep, house, future bass — artists like Skrillex, Knock2, Zeds Dead, ISOxo, Flux Pavilion) — this app should feel natural for that genre space, not assume techno/house-only DJ conventions.

## Part 1: Enrichment script (batch, run separately from the main app)

Build a standalone script that:
- Reads `liked_songs.json`, extracts the unique set of ISRCs.
- For each unique ISRC, looks up BPM, musical key (both standard notation and Camelot/open-key notation), danceability, and acousticness via the **GetSongBPM API** (https://getsongbpm.com/api — free, requires an API key you register for with an email, requires a mandatory backlink to getsongbpm.com wherever this app's output is shown or shared, capped at 3000 requests/hour which is far more than we need for a one-time batch of ~1,200 unique ISRCs).
- Read the API key from a `.env` file, not hardcoded.
- Cache every successful lookup result to a local file (e.g. `bpm_cache.json` keyed by ISRC) so re-runs don't repeat work, and so this script can be re-run later to enrich newly-liked songs without re-fetching everything.
- Handle tracks GetSongBPM has no match for — log them to a separate `unmatched.json` (or similar) rather than failing the whole run, and don't block the rest of the app on 100% coverage; the app should just show "no data" for those tracks.
- Respect the rate limit with basic throttling/backoff, and print progress (e.g. "Enriched 400 / 1219 unique tracks...").
- Since GetSongBPM's coverage may be incomplete for newer/niche electronic tracks, note in the code/README that AcousticBrainz's old public dataset (no longer updated since 2022, but the existing data is CC0/public domain, no backlink required) could be added later as a secondary fallback source for tracks GetSongBPM misses — don't build that integration now, just leave a clear extension point.
- Output a merged, enriched dataset — e.g. `library_enriched.json` — that's the slim track data (no `spotify_raw`) plus whatever BPM/key/danceability/acousticness data was found, with a clear field indicating when a track has no enrichment data yet.

## Part 2: The app itself

**Platform:** local-only, just for me — either a local web app (e.g. a small FastAPI/Flask backend + simple frontend, or a single-page app I open in a browser) or a local desktop app — your call on what's simplest to build well, but it must NOT require any hosting/deployment/accounts. I'll run it on my own machine.

**Feature 1 — Browse/filter/sort library:**
- A table or card view of all tracks from `library_enriched.json`: track name, artist(s), album, duration, BPM, key, energy/danceability, date liked.
- Sortable by any column.
- Filterable by BPM range (e.g. slider or min/max input), key, and text search on track/artist name.
- Clearly indicate tracks with missing BPM/key data (don't hide them, just mark them, e.g. "—" or a muted style).

**Feature 2 — Build and save named setlists:**
- Let me select tracks from the library view and add them to a working setlist, in a chosen order (drag-to-reorder, or up/down controls — whichever is simpler to implement well).
- While building a setlist, show useful at-a-glance info for the sequence: each track's BPM and key, and the BPM delta / key compatibility between consecutive tracks (using Camelot wheel adjacency rules for "compatible" — same key, adjacent number same letter, or same number opposite letter) so I can see whether a transition is smooth without leaving the screen.
- Also surface a rough energy arc for the set (e.g. a simple sparkline or bar showing energy/danceability across the ordered tracks) so I can eyeball whether it builds, peaks, and comes down the way I want.
- Let me save a setlist with a name, and reload/edit/delete previously saved setlists later. Local storage is fine (a JSON file or SQLite — your call, but keep it simple since this is single-user).
- Let me export a saved setlist as a plain CSV or M3U file (track name, artist, BPM, key at minimum) so I can reference it externally or hand it to other DJ software later.

## Explicitly out of scope for this phase

- No automatic "suggest the next track" recommendation engine — I'm ordering sets manually with the transition info as a guide, not asking the app to build the set for me. (This might come later.)
- No genre filtering/tagging.
- No multi-user support, accounts, hosting, or mobile app.
- No integration with Rekordbox/Serato/etc. beyond the plain CSV/M3U export.
- No live Spotify playback control.

## Code quality expectations

- Clear README covering: how to get a GetSongBPM API key, how to fill in `.env`, how to run the enrichment script, how to run the app, and where saved setlists/cache files live on disk.
- Reasonably organized code (the enrichment script and the app can be separate modules/files, doesn't need to be over-engineered — this is a personal tool, not a product).
- Handle the case where `library_enriched.json` doesn't exist yet (prompt me to run the enrichment script first) and where it's stale relative to a newer `liked_songs.json` (a warning is enough, don't need automatic re-sync logic).
