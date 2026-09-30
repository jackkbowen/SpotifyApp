# Spotify Liked Songs → DJ Setlist Builder

A personal, local-only toolkit with three steps:

1. **Export** your Spotify Liked Songs: `export_liked_songs.py` (this section)
2. **Enrich** them with BPM, key, danceability and acousticness: `enrich_library.py` ([below](#phase-2-enrich-with-bpm--key))
3. **Build setlists** in a local browser app: `python -m setlist_app` ([below](#phase-2-the-setlist-builder-app))

Everything runs on your machine. There's no hosting and no accounts beyond the API keys.

---

# Phase 1: Export Liked Songs

Exports your full Spotify **Liked Songs** library to:

- `output/liked_songs.csv`: one row per track, for spreadsheets
- `output/liked_songs.json`: nested records (multiple artists, album info), plus the untouched Spotify response for each track under `spotify_raw`

It uses read-only access (`user-library-read`) and works with a normal **Development Mode** Spotify app. You don't need Spotify's approval.

## 1. Create a Spotify Developer app

1. Go to the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard) and log in.
2. Click **Create app** and fill in:
   - **App name / description:** anything, e.g. "Liked Songs Export"
   - **Redirect URI:** `http://127.0.0.1:8888/callback` (click **Add**). Spotify rejects `localhost`, so use the `127.0.0.1` form.
   - **Which API/SDKs are you planning to use:** tick **Web API**
3. Accept the terms and **Save**.
4. Open the app's **Settings**. Copy the **Client ID**, then click **View client secret** and copy the **Client secret**.
5. Under **User Management**, add the name and email of the Spotify account whose library you want to export. This is required even if it's your own account.

> Since February 2026, the owner of a Development Mode app must have an active Spotify **Premium** subscription.

## 2. Fill in `.env`

Copy the example file and edit it:

```powershell
Copy-Item .env.example .env      # PowerShell
# cp .env.example .env           # macOS / Linux / Git Bash
```

```ini
SPOTIFY_CLIENT_ID=<Client ID from the dashboard>
SPOTIFY_CLIENT_SECRET=<Client secret from the dashboard>
SPOTIFY_REDIRECT_URI=http://127.0.0.1:8888/callback
```

`SPOTIFY_REDIRECT_URI` must match the Redirect URI registered on the app **exactly**. Don't commit `.env`, since it contains your client secret. It's already in `.gitignore`.

## 3. Install dependencies

Requires Python 3.9+.

```powershell
python -m venv .venv
.venv\Scripts\activate           # Windows
# source .venv/bin/activate      # macOS / Linux
pip install -r requirements.txt
```

## 4. Run it

```powershell
python export_liked_songs.py
```

On the first run your browser opens a Spotify consent page. Log in and click **Agree**. The script catches the redirect on `127.0.0.1:8888` automatically. The login is cached in `.spotify_token_cache` and refreshed automatically, so later runs don't open the browser.

Example output:

```
Fetching Liked Songs from Spotify...
Spotify reports ~1214 liked songs.
Fetched 1214 of ~1214 tracks...

Export complete
  Tracks exported:  1214
  Oldest liked:     2015-03-02
  Newest liked:     2026-09-28
  CSV:              C:\Code\SpotifyApp\output\liked_songs.csv
  JSON:             C:\Code\SpotifyApp\output\liked_songs.json
```

Options:

| Flag | Effect |
| --- | --- |
| `--output-dir PATH` | Write the files somewhere other than `./output/` |
| `--reauth` | Delete the cached login and sign in again (e.g. to switch accounts, or after an auth error) |

Each run overwrites `liked_songs.csv` / `liked_songs.json`. If the library is empty, nothing is written, so an earlier export is left alone.

## What gets exported

Tracks are listed most-recently-liked first, like in the Spotify app.

**CSV columns:** `added_at`, `track_name`, `artists`, `album_name`, `album_artists`, `album_release_date`, `album_release_date_precision`, `album_type`, `duration_ms`, `duration` (m:ss), `explicit`, `isrc`, `disc_number`, `track_number`, `is_local`, `track_id`, `track_uri`, `spotify_url`, `artist_ids`, `album_id`. Multiple artists are joined with `; `.

Any other simple field Spotify returns is added as an extra column automatically: track fields keep their name, and album fields get an `album_` prefix. So the CSV follows Spotify's current response instead of a fixed schema. For example, `popularity` and `available_markets` were removed in the February 2026 API changes. If Spotify stops sending a field, its column disappears.

**JSON:** `{ exported_at, source, track_count, tracks: [...] }`. Each track has the same data as the CSV, but with `artists` and `album` kept as nested objects. It also keeps the full original Spotify item in `spotify_raw`.

Notes:

- `isrc` can be blank. Spotify's `external_ids` field is optional, and local files don't have one.
- Local files you've liked have `is_local = True` and no `track_id`.
- Tracks Spotify returns with no data (fully unavailable) are skipped and counted in the summary.

### Not included: BPM, key, energy, danceability

Spotify's audio-features endpoints only work for apps with extended quota, not Development Mode apps. This tool intentionally doesn't call them. A later phase can add this data from another source, joined on `isrc` or `track_id`. See the comment in `to_record()` in `export_liked_songs.py`.

## Troubleshooting

| Problem | Fix |
| --- | --- |
| `Missing SPOTIFY_CLIENT_ID...` | `.env` is missing or still has placeholder values (see step 2). |
| Browser shows **INVALID_CLIENT: Invalid redirect URI** | The URI in `.env` doesn't exactly match the one on your app in the dashboard. |
| `Spotify refused access (HTTP 403)` | Add your account under **User Management** (step 1.5). Also check that the app owner has Premium. |
| `saved Spotify login has expired or was revoked` / `access token is invalid` | Run `python export_liked_songs.py --reauth`. |
| `Rate limited by Spotify; waiting…` | Normal for large libraries. The script waits and retries by itself. If Spotify asks for a wait longer than 15 minutes, the script stops. Run it again later. |
| Port 8888 already in use | Change the port in both `.env` and the dashboard Redirect URI, e.g. `http://127.0.0.1:8889/callback`. |

---

# Phase 2: Enrich with BPM & key

`enrich_library.py` reads `output/liked_songs.json` and looks up each unique recording on the [GetSongBPM API](https://getsongbpm.com/api). It writes `data/library_enriched.json`, which is what the app reads. That file is the slim track data (no `spotify_raw`, about a third of the size) plus BPM, key (standard and Camelot), Open Key, danceability, acousticness and time signature where found.

## Get a GetSongBPM API key

1. Go to [getsongbpm.com/api](https://getsongbpm.com/api) and register with your email address. Confirm the activation email, and the key is sent to you.
2. **Backlink requirement:** the free API requires a visible link back to getsongbpm.com wherever its data is shown or shared, or they may suspend the key without notice. This project handles that for you:
   - the app footer links to GetSongBPM, and each BPM value links to its GetSongBPM page
   - CSV exports include a `bpm_key_source` column
   - M3U exports include a header comment

   Keep those in place if you modify or share anything. The registration form may ask where the link will appear. This app is local-only with no public site, so describe it truthfully; whether that's acceptable is GetSongBPM's call.
3. Add the key to `.env`:

   ```ini
   GETSONGBPM_API_KEY=<your key>
   ```

## Run the enrichment

```powershell
python enrich_library.py --limit 25   # optional: try 25 tracks first and check the matches in the app
python enrich_library.py              # everything that isn't cached yet
```

Progress looks like `Enriched 400 / 1386 unique tracks... (this run: ...)`. The free API allows 3,000 requests/hour. The script keeps to 2,800/hour, including requests from earlier runs in the past hour. It pauses on its own if it reaches that limit. Each track takes 1–3 requests. A first run over ~1,400 tracks usually takes 20–40 minutes, but it may hit the hourly limit and pause. Ctrl+C is safe: progress is saved every 10 lookups and on exit.

| Flag | Effect |
| --- | --- |
| `--limit N` | Look up at most N uncached tracks this run |
| `--retry-unmatched` | Try tracks that had no match last time again (GetSongBPM's catalogue grows) |
| `--no-fetch` | Don't call the API. Just rebuild `library_enriched.json` from the cache. Works without a key, so you can use the app before you have one. |
| `--input PATH` | Read a different Liked Songs export |

**After you like more songs:** run `export_liked_songs.py`, then `enrich_library.py`. Cached tracks are never fetched again, so only new ones cost requests.

### How matching works (and its limits)

GetSongBPM **has no ISRC lookup**. Its API only searches by song title and artist name. So ISRC is the cache key: one lookup per unique ISRC, shared by every Liked Songs entry with that ISRC. The match itself is by name, and the script is strict about it:

- `feat.`, `Radio Edit`, `Original Mix`, `Remastered` and `Extended Mix` are ignored, because they're the same recording.
- Remixes, VIPs, edits, flips and live versions must match exactly. `Song (Knock2 Remix)` never matches the original `Song` or a different remix, since they usually have a different BPM and key.
- The artist must match one of the Spotify artists. The second-billed artist is tried too, for collaborations.

Tracks without a confident match go to `data/unmatched.json` and show as "—" in the app. That's expected for new or niche releases. If a match looks wrong, hover its BPM in the app to see what it matched, or click it to open the GetSongBPM page.

### Adding another data source later

`build_sources()` in `enrich_library.py` is the extension point. Sources are tried in order for each track. A natural fallback is **AcousticBrainz**: it stopped updating in 2022, but its existing data (BPM, key, danceability) is CC0 public domain and needs no backlink. It's keyed by MusicBrainz recording ID, so it can be reached from ISRCs via the MusicBrainz API. See the comment above `build_sources()`. It isn't built yet.

---

# Phase 2: The setlist builder app

```powershell
python -m setlist_app               # opens http://127.0.0.1:8765 in your browser
python -m setlist_app --port 9000 --no-browser
```

The app is a small Flask server bound to `127.0.0.1`, so it's never reachable from other machines, with a plain HTML/JS page and no build step. Stop it with Ctrl+C.

If `data/library_enriched.json` doesn't exist yet, the page tells you to run the enrichment script. If `output/liked_songs.json` is newer than the export the enriched library was built from, or some tracks haven't been looked up yet, a warning banner says so.

## Browsing

- Click any column header to sort. Tracks without data always sort to the bottom.
- **Search** matches track and artist names.
- **BPM min–max:** tick **½× / 2×** to also match tracks listed at half or double tempo. With 140–150 and ½× / 2× ticked, a 72 BPM half-time listing also shows. BPM databases list bass music either way.
- **Key:** filter by Camelot key. Tick **+ compatible** to include the harmonically compatible keys.
- **All / Has BPM/key / Missing BPM/key:** tracks without data are never hidden by default. They show "—" with a hatched background, and hovering explains whether there was no match or the track hasn't been looked up yet.

## Building a set

- Click **+** on a row to append a track, or tick several and click **Add selected to set**. A track can only be in a set once. Rows already in the set show ✓.
- Reorder by dragging, or with ↑ ↓. ✕ removes a track.
- Between every pair of tracks you see:
  - **BPM change** in BPM and %. Green is ≤3%, amber ≤6%, red beyond. It's compared at 1×, 2× and ½×, and labelled `2×` / `½×` when a half/double-time reading is closer, e.g. 140 → 72.
  - **Key relationship** on the Camelot wheel. **Compatible (✓)** means same key, ±1 with the same letter (8A → 9A), or the relative major/minor (8A ↔ 8B). Anything else is marked "clash".
- **Energy arc:** one bar per track, showing danceability (GetSongBPM has no "energy" value, so danceability is the proxy) or BPM. Grey stubs are tracks without data.
- The summary shows track count, total length, BPM range and how many transitions are key-compatible.

## Saving and exporting

- Name the set and click **Save** (or press Enter in the name field). Load saved sets from the dropdown. **New** starts a blank one, and **Delete** removes the loaded one after confirming.
- Unsaved work is kept in the browser if you close the tab or restart the app, and you're warned before discarding it.
- **Export CSV / Export M3U** saves the set first if needed, then downloads it:
  - **CSV:** position, track, artists, BPM, key, Camelot, danceability, acousticness, duration, ISRC, Spotify URL.
  - **M3U:** extended M3U with a BPM/key comment per track. The entries are Spotify URLs, not audio files, so Rekordbox/Serato will list the tracks but can't play them until you point them at local files.
- If a track in a saved set is later removed from your Liked Songs, the set keeps it, with the BPM/key it had when you saved it.

---

## Where files live

| Path | What | Written by |
| --- | --- | --- |
| `.env` | Spotify + GetSongBPM credentials | you |
| `.spotify_token_cache` | Cached Spotify login | `export_liked_songs.py` |
| `output/liked_songs.json`, `.csv` | Raw Liked Songs export | `export_liked_songs.py` |
| `data/bpm_cache.json` | Every successful lookup, keyed by ISRC, plus recent request times for the hourly limit | `enrich_library.py` |
| `data/unmatched.json` | ISRCs with no match, with track name/artists for reference | `enrich_library.py` |
| `data/library_enriched.json` | Slim library + enrichment; what the app reads | `enrich_library.py` |
| `data/setlists.json` | Your saved setlists | the app |

All of these are git-ignored. Back up `data/setlists.json` if your sets matter to you. Deleting `data/bpm_cache.json` means every track is looked up again.

Out of scope for now: automatic next-track suggestions, genre tagging, Rekordbox/Serato integration beyond CSV/M3U, and Spotify playback control.
