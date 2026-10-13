# Spotify Liked Songs → DJ Setlist Builder

A personal, local-only toolkit with three steps:

1. **Export** your Spotify Liked Songs: `export_liked_songs.py` (this section)
2. **Enrich** them, all with one script, `enrich_library.py`:
   - BPM, key, danceability and acousticness from GetSongBPM ([details](#phase-2-enrich-with-bpm--key))
   - mood/genre tags from Last.fm ([details](#phase-3-mood--genre-tags))
   - audio features (energy, valence, tempo, key…) from ReccoBeats, and genres from Deezer and MusicBrainz, merged into one genre profile per song ([details](#phase-4-audio-features-genres-and-the-taxonomy))
3. **Build setlists** in a local browser app: `python -m setlist_app` ([below](#phase-2-the-setlist-builder-app)). You can also generate drafts from a mood and get next-track suggestions ([phase 3](#phase-3-generate-from-a-mood--suggest-next-track)).

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
python enrich_library.py              # every step, everything that isn't cached yet
python enrich_library.py --only bpm   # just this step (or tags / features / genres)
```

Progress looks like `Enriched 400 / 1386 unique tracks... (this run: ...)`. The free API allows 3,000 requests/hour. The script keeps to 2,800/hour, including requests from earlier runs in the past hour. It pauses on its own if it reaches that limit. Each track takes 1–3 requests. A first run over ~1,400 tracks usually takes 20–40 minutes, but it may hit the hourly limit and pause. Ctrl+C is safe: progress is saved every 10 lookups and on exit.

| Flag | Effect |
| --- | --- |
| `--only bpm` / `tags` / `features` / `genres` | Run one step. By default all four run. A step whose key isn't in `.env` is skipped with a note, and `--only` without the key is an error. `features` and `genres` need no key. |
| `--limit N` | Look up at most N uncached tracks per step this run |
| `--retry-unmatched` | Try tracks that had no match last time again (the sources' catalogues grow) |
| `--no-fetch` | Don't call any API. Just rebuild `library_enriched.json` from the caches. Works without keys, so you can use the app before you have them. |
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
  - Tempo and key come from GetSongBPM when it has the song, otherwise from ReccoBeats. The pill's tooltip says when ReccoBeats was used.
  - **Key relationship** on the Camelot wheel. **Compatible (✓)** means same key, ±1 with the same letter (8A → 9A), or the relative major/minor (8A ↔ 8B). Anything else is marked "clash".
- **Energy arc:** one bar per track, showing ReccoBeats' energy score (0–100) or BPM. Grey stubs are tracks without data.
- The summary shows track count, total length, BPM range and how many transitions are key-compatible.

## Saving and exporting

- Name the set and click **Save** (or press Enter in the name field). Load saved sets from the dropdown. **New** starts a blank one, and **Delete** removes the loaded one after confirming.
- Unsaved work is kept in the browser if you close the tab or restart the app, and you're warned before discarding it.
- **Export CSV / Export M3U** saves the set first if needed, then downloads it:
  - **CSV:** position, track, artists, BPM, key, Camelot, danceability, acousticness, duration, ISRC, Spotify URL.
  - **M3U:** extended M3U with a BPM/key comment per track. The entries are Spotify URLs, not audio files, so Rekordbox/Serato will list the tracks but can't play them until you point them at local files.
- Exports use GetSongBPM's BPM/key only, like the library table's BPM and Key columns. The ReccoBeats fallback is used inside the set builder, suggestions and generation.
- If a track in a saved set is later removed from your Liked Songs, the set keeps it, with the BPM/key it had when you saved it. Its energy isn't kept, so it shows as a grey stub in the energy arc.

---

## Where files live

| Path | What | Written by |
| --- | --- | --- |
| `.env` | Spotify, GetSongBPM and Last.fm credentials | you |
| `.spotify_token_cache` | Cached Spotify login (read-only) | `export_liked_songs.py` |
| `.spotify_playlist_token_cache` | Cached Spotify login for creating playlists | the app |
| `output/liked_songs.json`, `.csv` | Raw Liked Songs export | `export_liked_songs.py` |
| `data/bpm_cache.json` | Every successful lookup, keyed by ISRC, plus recent request times for the hourly limit | `enrich_library.py` |
| `data/unmatched.json` | ISRCs with no match, with track name/artists for reference | `enrich_library.py` |
| `data/lastfm_tag_cache.json` | Raw Last.fm tags per artist+title and per artist, including "not found" results | `enrich_library.py` |
| `data/features_cache.json` | Raw ReccoBeats audio features per Spotify track ID, including "not found" results | `enrich_library.py` |
| `data/genre_cache.json` | Raw Deezer and MusicBrainz genres per ISRC, plus MusicBrainz's official genre list | `enrich_library.py` |
| `data/taxonomy_overrides.json` | Words you force to "style" or "vibe" (hand-edited, see phase 4) | you |
| `data/library_enriched.json` | Slim library + BPM/key + tags + audio features + genres + one merged profile per song, and the library-wide taxonomy; what the app reads | `enrich_library.py` |
| `data/setlists.json` | Your saved setlists, with any mood tags | the app |

All of these are git-ignored. Back up `data/setlists.json` if your sets matter to you. Deleting any `*_cache.json` file means every track is looked up again by that step.

Out of scope for now: variety rules (e.g. avoiding repeated artists), ML/embedding similarity, Rekordbox/Serato integration beyond CSV/M3U, and Spotify playback control.

---

# Phase 3: Mood & genre tags

Mood features need descriptive tags, which the BPM data can't provide. `enrich_library.py` has a second, independent step that fetches them from **Last.fm** (`tag_enrichment.py`). It uses `track.getTopTags`, falling back to `artist.getTopTags` when a track has fewer than 3 usable tags, which is common for new or niche releases. It's the same script as the BPM step, so you run one script with a flag, not a second script.

## Get a Last.fm API key

1. Log in to Last.fm (a free account is needed to get a key) and go to [last.fm/api/account/create](https://www.last.fm/api/account/create).
2. Fill in an application name and description, e.g. "Personal DJ setlist builder, non-commercial". Leave the callback URL empty. These read-only endpoints need no user login.
3. Copy the **API key** (you don't need the shared secret) into `.env`:

   ```ini
   LASTFM_API_KEY=<your key>
   ```

Last.fm's terms allow non-commercial use and require crediting Last.fm where the data is shown. The app footer does this.

## Run the tag step

```powershell
python enrich_library.py --only tags --limit 25   # optional quick test
python enrich_library.py --only tags              # everything not cached yet
```

It makes one request per unique artist+title (about 1,400 for this library), plus artist lookups where needed. It's paced at about 4 requests/second and backs off and retries if Last.fm returns its rate-limit error (29) or a temporary outage. A full run takes roughly 10–15 minutes and can be interrupted and resumed. The cache is about 1 MB, far under Last.fm's 100 MB limit. Re-runs only fetch newly liked songs.

## How tags are cleaned

Raw Last.fm tags are crowdsourced and noisy. They're cached as-is, and cleaned whenever `library_enriched.json` is rebuilt (`setlist_app/tags.py`), so the rules can be adjusted and applied with `--no-fetch` without refetching:

- Everything is lowercased. Spelling variants merge: `Future Bass` / `future-bass` / `futurebass` → `future bass`, and `dnb` / `Drum & Bass` / `drum n bass` → `drum and bass`.
- Non-descriptive tags are dropped: opinions ("awesome", "favorite", "10/10"), listening habits ("seen live", "my playlist"), bare years, and a tag that's just the artist's name, track title or album name.
- Each remaining tag keeps Last.fm's 0–100 weight. Artist-level fallback tags count at half weight, and tags under 5 are ignored as one-off noise.

Each track in `library_enriched.json` gets `tags: [{tag, weight, source}]` (strongest first, up to 12) and a `tag_status`: `tagged`, `no_tags` (nothing usable), or `pending` (not fetched yet). Tracks with no tags simply never appear in mood-filtered results.

There's **no fixed list of genres or moods** anywhere. The vocabulary is whatever tags exist in your library, with how many tracks carry each.

**Why not Spotify's artist genres?** The plan was to also use `GET /artists/{id}` genres. On 2026-09-30 Spotify returned **no `genres` field at all** for this app, with both a user token and an app token, even for artists like Skrillex. That's true even though the February 2026 changelog doesn't list genres as removed. So that source isn't used. Last.fm's artist tags cover the same need. If Spotify restores the field, it could be added as another source in `tag_enrichment.py`.

---

# Phase 4: Audio features, genres and the taxonomy

Two more `enrich_library.py` steps, neither of which needs an API key, plus a merge step that runs on every rebuild.

## Audio features (ReccoBeats)

```powershell
python enrich_library.py --only features
```

[ReccoBeats](https://reccobeats.com) is a free API with Spotify-style audio features: tempo, key, energy, valence (sad → happy), danceability, acousticness, instrumentalness, speechiness, liveness and loudness. It's looked up by Spotify track ID, 40 songs per request, so the whole library takes a few minutes. It has about 70% of this library (983 of 1,396 songs). Results are cached in `data/features_cache.json` and stored per song as `features`, with scores on a 0–100 scale.

ReccoBeats is unofficial and its terms aren't published, so check them before sharing anything that uses its data. The app's footer credits it.

## Genres (Deezer + MusicBrainz)

```powershell
python enrich_library.py --only genres
```

Both are looked up by ISRC, so there's no name matching:

- **Deezer** gives each album's genres: broad families such as "Electro", "Dance" or "Rap/Hip Hop". It covers about 98% of this library.
- **MusicBrainz** gives community-voted, specific genres for the recording, falling back to its album: "dubstep", "2-step", "electro house", "trap" and so on. About half the library has them.

MusicBrainz allows one request per second, so **the first run takes about an hour**. It can be stopped with Ctrl+C and resumed, and later runs only fetch new songs. Optionally set `MUSICBRAINZ_CONTACT` in `.env` (an email or URL) to identify yourself to MusicBrainz, as they ask; it's only sent to them. Results are stored per song as `genres`.

## The merged profile and taxonomy

Every rebuild merges each song's Last.fm tags, Deezer genres and MusicBrainz genres into one `profile` (`setlist_app/taxonomy.py`):

- **styles:** genre words, merged across sources. Spelling variants combine, and sources that agree raise the weight (two independent 50s make a 75).
- **vibes:** other descriptive words, such as places and eras ("west coast", "80s").
- **families:** broad groups such as electronic, hip hop, rock and pop. A style joins a family when at least half of its songs are already in it, so synonyms ("hip hop" / "rap") end up together and a style can belong to two families.
- **primary_style** and **primary_family:** the strongest of each.

`library_enriched.json` also gets a library-wide `taxonomy`: every style with its song count, parent styles (for example riddim → dubstep) and families, plus the vibe list. Nothing is hardcoded. The only outside vocabulary is MusicBrainz's official genre list, fetched once.

**Style or vibe?** A word is a style if it's an official MusicBrainz genre, if Deezer or MusicBrainz used it as a genre for one of your songs, or if it ends in a known style ("west coast rap"). Everything else is a vibe. This can't be worked out statistically, because place words cluster with genres (regional artists), so some real styles that aren't official genres get filed as vibes. Fix those in `data/taxonomy_overrides.json`:

```json
{
  "styles": ["riddim", "indie", "hardcore"],
  "vibes": []
}
```

Case and spelling don't matter, and a word in both lists counts as a vibe. Apply changes with `python enrich_library.py --no-fetch`, which rebuilds without calling any API. The summary reports how many overrides applied and any words not found in your library.

---

# Phase 3: Generate from a mood & suggest next track

Both features use one transition score (`setlist_app/static/scoring.js`), a weighted average of simple, visible parts. There's no machine learning, so every suggestion can be explained:

| Part | Weight | 0 = ideal … 1 = bad |
| --- | --- | --- |
| BPM | 0.30 | tempo change in %, compared at 1×, 2× and ½× for half-time; 8%+ scores as a hard jump |
| Key | 0.25 | Camelot: same key 0, ±1 0.1, relative 0.15, clash 1 |
| Genre | 0.20 | 1 − genre similarity of the two songs' profiles (see below) |
| Energy | 0.15 | how far the track's energy is from the arc's target (generation) or from the previous track (suggestions); 40+ points apart = 1 |
| Valence | 0.10 | the change in valence (sad → happy) from the previous track; 40+ points apart = 1 |
| Mood | 0.15 | 1 − how strongly the track carries the selected moods (only when a mood is active) |

Weights are averaged over the parts present, so without a mood they add up to 1.

- **BPM and key** come from GetSongBPM where it has the song, otherwise from ReccoBeats. Where both exist they agree on 96% of tempos (allowing half/double time) and 90% of keys, and together they cover about 74% of this library, against 19% from GetSongBPM alone.
- **Energy** is ReccoBeats' energy score. There's deliberately no fallback to GetSongBPM's danceability, because it's a different measure on a different scale.
- **Genre similarity** compares the two songs' styles and families, weighting each word by how rare it is in your library, so sharing "riddim" counts for more than sharing "dance". Related styles get partial credit through shared families, so dubstep and brostep are close. Vibes (places, eras) are ignored.
- **Missing data scores as uncertain (0.5–0.6), not as good or bad.** Songs without data can still appear, but those transitions are guesses.

The weights are a single `WEIGHTS` constant in `setlist_app/static/scoring.js`. Raising genre makes suggestions stick to a genre more closely, at the cost of more key clashes and tempo jumps.

## Generate a draft setlist

Click **Generate…** in the setlist panel:

1. **Mood / vibe tags:** search and pick one or more. Each shows how many tracks carry it. The words come from each song's merged profile (styles and vibes) plus its Last.fm tags, so Deezer and MusicBrainz genres count too. Spelling doesn't matter ("hyperpop" = "hyper pop"). Tracks don't need all the tags: matching more of them, more strongly, ranks higher. The preview shows how many tracks match and how many have BPM/key data.
2. **Length:** a number of tracks, or minutes.
3. **Energy arc:**
   - **Build:** low to high.
   - **Peak then cool:** rises to a peak about 2/3 of the way through, then eases to the middle.
   - **Plateau:** steady.
   - **Free:** no arc; transitions only.

   Low and high are taken from the matching tracks' own energy range.
4. **Generate draft:** picks an opening track that fits the mood and the arc's starting energy, then repeatedly adds the remaining track with the lowest transition cost until the length is reached. A recording is never used twice: other releases of the same song (same ISRC, such as an album and a deluxe version) are skipped.

The draft opens in the normal builder as an **unsaved** setlist, so you can reorder, remove, add, rename and save it like any other. The arc it aimed for is drawn as a dashed line over the energy bars. If fewer tracks match than you asked for, the draft stops there and **tells you**; it's never padded with tracks outside the mood.

**Use as mood filter only** sets the mood for suggestions without generating anything.

## Suggest next track

With at least one track in the set, click **Suggest next track** under the list. You get the 12 best-scoring tracks to follow the **last** track. Each one shows:
- the BPM change
- the key relationship (same key / ±1 / relative / clash)
- the energy change
- the valence change
- the genre similarity, with the styles the two songs share
- the matching mood tags
- a 0–100 fit score

Other releases of a recording already in the set are never suggested.

Click **+** to append one; the list then refreshes for the new last track.

If a mood is active (from a generated draft, or **Set mood…**), suggestions only come from tracks with at least one of those tags, weighted by how strongly they match. Remove the mood chips to search the whole library. A setlist's mood is saved with it.

---

# Export a setlist to Spotify

Click **Export to Spotify** in the setlist panel. It saves the set first if needed, then creates a **private** playlist in your Spotify account with the same name and track order.

- **First time:** a browser tab opens asking you to let the app **"create and edit your private playlists"** (`playlist-modify-private`). This is a separate login from the read-only one the export script uses, so neither disturbs the other. It's cached in `.spotify_playlist_token_cache`. The login is caught on the same `http://127.0.0.1:8888/callback` redirect URI you registered in phase 1, so the dashboard needs no changes. The page waits up to 3 minutes for you to finish.
- **Afterwards** the button becomes **Update on Spotify**. It updates the same playlist in place, replacing its tracks, name and description with the current set. The panel links to the playlist and shows "changed since" when the set has been edited after the last export. Use **export as new playlist** to make a separate copy, e.g. if you deleted the original in Spotify.
- **Description:** the playlist description lists the set's mood tags and credits getsongbpm.com and last.fm. GetSongBPM requires a backlink wherever its data is shared, and a playlist counts.
- **Large sets** are handled: tracks are sent in batches of 100, Spotify's per-request limit.
- **Tracks you've since unliked** are still exported. They're still Spotify tracks, just no longer in your library.

To revoke access, delete `.spotify_playlist_token_cache`, or remove the app under *Manage apps* in your Spotify account settings.

If Spotify answers **403**, the usual Development Mode causes apply: your account must be listed under **User Management** in the Developer Dashboard, and the app owner needs Premium.
