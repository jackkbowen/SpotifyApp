# DJ Setlist Builder

Build DJ setlists and playlists from a shared song library. Anyone can add songs by uploading an [Exportify](https://exportify.net) export of their Spotify playlists, and anyone can build playlists from everything in the library. The app fills in BPM, key, energy, mood and genre data, and then helps you order tracks so they mix well together.

---

## How to use it

### 1. Install and start the app

Requires Python 3.9+.

```powershell
python -m venv .venv
.venv\Scripts\activate           # Windows
# source .venv/bin/activate      # macOS / Linux
pip install -r requirements.txt
python -m setlist_app            # opens http://127.0.0.1:8765
```

Optional API keys go in a `.env` file in the project folder. Without them, those data sources are skipped and everything else still works:

```ini
GETSONGBPM_API_KEY=<key from getsongbpm.com/api>
LASTFM_API_KEY=<key from last.fm/api/account/create>
MUSICBRAINZ_CONTACT=<optional: an email or URL, sent only to MusicBrainz>
```

| Option | Effect |
| --- | --- |
| `--port 9000` | Use a different port |
| `--no-browser` | Don't open a browser tab on start |
| `--no-enrich` | Never call the data APIs. Imported songs still show up, using only their CSV data. |

### 2. Add songs

1. Go to [exportify.net](https://exportify.net), log in with Spotify, and click **Export** next to **Liked Songs** or any playlist. You get a `.csv` file.
2. Open the **Import** page and drop the file on it.

The songs can be used straight away. Extra data is added in the background, and the **Enrichment queue** on the Import page shows which songs are still waiting. You can also import from the command line:

```powershell
python -m setlist_app.import_csv Liked_Songs.csv
```

### 3. Build a setlist

On the **Setlist builder** page:

- **Find tracks:** search, filter by BPM range or Camelot key, and sort any column.
- **Add tracks:** click **+**, or tick several and click **Add selected to set**.
- **Arrange:** drag to reorder, or use ↑ ↓. ✕ removes a track.
- **Generate…** builds a draft from mood tags, a length and an energy shape.
- **Suggest next track** ranks the best songs to play after the last one.

### 4. Save and share

- Click **Save**. You get two links:
  - **Share link** (`/p/<id>`): a read-only page anyone can open.
  - **Edit link**: lets any browser edit or delete the playlist. Keep it private; it can't be recovered if lost.
- **Copy track links**, then paste them into a playlist in the Spotify desktop app (Ctrl/Cmd+V) to recreate the playlist in Spotify.
- **Export CSV / M3U** downloads the setlist with its BPM and key for DJ software.

---

## Features

### Setlist builder
- **Library table:** title, artist, album, tags, length, BPM, key, danceability, acousticness and date added. It scrolls smoothly through thousands of songs.
- **Filters:**
  - search by track or artist
  - BPM range, with **½× / 2×** to include half- and double-time listings (a 72 BPM listing matches 140–150)
  - Camelot key, with **+ compatible** for harmonically compatible keys
  - "has data" / "missing data"
- **Transition labels** between every pair of tracks in the set:
  - **BPM change:** green ≤3%, amber ≤6%, red beyond
  - **Key relationship:** same key, ±1, relative, or clash
- **Energy arc:** a bar per track showing how the set's energy moves.
- **Summary:** track count, total length, BPM range and how many transitions are key-compatible.
- **Mood:** set mood tags that steer suggestions. They're saved with the setlist.
- **Drafts:** unsaved work is kept in the browser, and you're warned before losing it.

### Generate from a mood
- Pick one or more mood, genre or vibe tags from the library's own vocabulary.
- Choose a length (tracks or minutes) and an energy arc: **Build**, **Peak then cool**, **Plateau** or **Free**.
- The draft opens unsaved, so you can edit it. The arc it aimed for is drawn over the energy bars.

### Suggest next track
- The 12 best tracks to follow the last one.
- Each suggestion shows its BPM change, key relationship, energy and valence change, shared genres, matching moods and a 0–100 fit score.

### Subgenre page
- Stacked, colour-coded rows: open a **genre** to see its **subgenres**, and open a subgenre to see its **songs** with BPM, key and length.
- Subgenres without a parent genre get their own grey rows. Genres with no subgenres open straight to their songs.

### Import page
- Drag-and-drop Exportify CSV upload, with a report of new, updated and skipped songs.
- Library status, plus an **enrichment queue** showing each song still waiting and what data it's missing.
- **Privacy:** only song information is kept. The uploader's Spotify username and the dates songs were saved are never stored.

### Sharing
- No accounts: playlists use private edit links and public share links.
- A read-only share page with copy-links, CSV/M3U downloads and **Open a copy in the builder**.
- **My playlists** lists everything you've saved from this browser.

---

## The algorithm

### Where each value comes from

| Value | First choice | Fallback |
| --- | --- | --- |
| BPM and key | GetSongBPM | the song's audio features (Spotify's from the Exportify CSV, else ReccoBeats) |
| Energy, valence | audio features (Spotify's from the CSV, else ReccoBeats) | none |
| Genres | Spotify artist genres (from the CSV), Deezer, MusicBrainz | n/a |
| Mood tags | Last.fm track tags | Last.fm artist tags, at half weight |

GetSongBPM is matched by title and artist, strictly. Remixes, VIPs, edits and live versions must match exactly, because they usually have a different BPM and key. Tags such as "feat.", "Radio Edit" and "Original Mix" are ignored.

### Genre profile

Each song's genres and tags are merged into one profile:

- **Styles** are genre words. Spelling variants combine ("future-bass" = "Future Bass"), and sources that agree raise the weight: two independent 50s make 75.
- **Vibes** are other descriptive words, such as places and eras ("west coast", "80s").
- **Families** are broad groups such as electronic or hip hop. A style joins a family when at least half its songs are already in it.

Nothing is hardcoded. The vocabulary is whatever the library contains. Styles that get filed as vibes by mistake can be corrected in `data/taxonomy_overrides.json`.

### Transition score

Every candidate transition gets a cost from 0 (ideal) to 1 (bad). The cost is the weighted average of these parts:

| Part | Weight | How it's scored (0 = ideal, 1 = bad) |
| --- | --- | --- |
| BPM | 0.30 | % tempo change, compared at 1×, 2× and ½×. A change of 8% or more costs 1. |
| Key | 0.25 | Camelot: same key 0, ±1 0.1, relative major/minor 0.15, clash 1 |
| Genre | 0.20 | 1 − genre similarity |
| Energy | 0.15 | distance from the arc's target energy (when generating) or from the previous track (when suggesting). 40+ points costs 1. |
| Valence | 0.10 | change in valence (sad → happy) from the previous track. 40+ points costs 1. |
| Mood | 0.15 | 1 − how strongly the track carries the selected moods. Only counts when a mood is active. |

- **Weights:** the cost is divided by the sum of the weights in use, so it stays between 0 and 1 with or without a mood.
- **Missing data:** it counts as *uncertain*, not good or bad. Unknown BPM or key costs 0.6, and unknown energy, valence or genre costs 0.5.
- **Fit score:** shown on suggestions as `(1 − cost) × 100`.
- **Tuning:** the weights are the `WEIGHTS` constant in `setlist_app/static/scoring.js`. Raising genre keeps suggestions closer to one genre, at the cost of more tempo jumps and key clashes.

**Genre similarity:**
1. Each song becomes a vector of its styles and a vector of its families.
2. Each word is weighted by how rare it is in the library (IDF), so sharing "riddim" counts for more than sharing "dance".
3. The similarity is 60% style cosine plus 40% family cosine. The family part gives related styles partial credit, for example dubstep and brostep.
4. Vibes are ignored.

**Mood relevance:** the average of the track's weight for each selected tag, from 0 to 1. A track must carry at least one selected tag (weight 10 or more) to be included. Matching more tags, more strongly, ranks higher.

### Generating a setlist

1. **Pool:** every track that carries at least one selected mood tag.
2. **Energy range:** the pool's own energy from the 20th to the 80th percentile, so a "chill" set and a "riddim" set each use their own range.
3. **Arc target** at each point in the set:
   - **Build:** rises from low to high.
   - **Peak then cool:** rises to high at 65% of the set, then eases back to the middle of the range.
   - **Plateau:** stays at the middle of the range.
   - **Free:** no target.
4. **Opening track:** the best mood fit closest to the arc's starting energy, preferring songs with known BPM and key.
5. **Each next track:** the remaining track with the lowest transition cost from the previous one (greedy nearest neighbour). This repeats until the length is reached.
6. **No repeats:** a recording is never used twice. Other releases with the same ISRC are skipped.
7. **No padding:** if the pool runs out, the draft stops short and says so. It's never filled with tracks outside the mood.

### Suggesting the next track

Every track not already in the set (and not another release of one that is) is scored as a transition from the set's last track, and the 12 lowest-cost tracks are shown. With a mood active, only tracks carrying at least one mood tag are considered.

---

## Credits

- **[Exportify](https://exportify.net):** Spotify playlist exports, including Spotify's audio features and artist genres.
- **[GetSongBPM](https://getsongbpm.com):** BPM, key, danceability and acousticness. Their free API requires a visible backlink wherever the data is shown; the app's footers, BPM links and exports include it.
- **[Last.fm](https://www.last.fm):** mood and genre tags (non-commercial use, credited in the app).
- **[ReccoBeats](https://reccobeats.com):** audio features for songs whose export has none.
- **[Deezer](https://www.deezer.com):** album genres.
- **[MusicBrainz](https://musicbrainz.org):** community-voted genres and the official genre list.
- **[Spotify](https://www.spotify.com):** track data and links. This project isn't affiliated with or endorsed by Spotify or any of these services.
