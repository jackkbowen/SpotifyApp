"""Flask server: the shared library, CSV imports, playlists and the static UI.

Public-facing rules:
  * Anyone can import an Exportify CSV (size- and rate-limited).
  * Anyone can view a playlist by its ID; changing or deleting one needs the
    edit key returned when it was created (X-Edit-Key header).
  * Nothing here can touch the owner's Spotify account.
"""

from __future__ import annotations

import re
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

from . import exportify, setlists, store
from .library import LibraryMissingError, load_enriched_library, staleness_warnings
from .worker import worker

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_UPLOAD_MB = 15

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024


# --------------------------------------------------------------------------- #
# Rate limiting (per client address, in memory)
# --------------------------------------------------------------------------- #

class RateLimit:
    def __init__(self, limit: int, window_seconds: int):
        self.limit, self.window = limit, window_seconds
        self.hits: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.time()
        with self.lock:
            q = self.hits[key]
            while q and q[0] <= now - self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True


IMPORT_LIMIT = RateLimit(10, 60 * 60)      # 10 uploads an hour per address
WRITE_LIMIT = RateLimit(120, 10 * 60)      # playlist saves/deletes


class TooManyRequests(Exception):
    pass


def _client() -> str:
    # Behind a proxy (e.g. Cloudflare), trust its header only if you configure that proxy.
    return request.remote_addr or "unknown"


def _limit(limiter: RateLimit) -> None:
    if not limiter.allow(_client()):
        raise TooManyRequests()


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #

@app.errorhandler(LibraryMissingError)
def _library_missing(error):
    return jsonify(error="library_missing", message=str(error)), 409


@app.errorhandler(store.PlaylistNotFound)
def _playlist_missing(error):
    return jsonify(error="not_found", message="That playlist doesn't exist (it may have been deleted)."), 404


@app.errorhandler(store.Forbidden)
def _forbidden(error):
    return jsonify(error="forbidden", message="You need this playlist's edit link to change it."), 403


@app.errorhandler(store.InvalidPlaylist)
@app.errorhandler(setlists.InvalidSetlist)
@app.errorhandler(exportify.ExportifyError)
def _bad_request(error):
    return jsonify(error="bad_request", message=str(error)), 400


@app.errorhandler(TooManyRequests)
def _too_many(error):
    return jsonify(error="rate_limited", message="Too many requests from your connection. Please wait a while and try again."), 429


@app.errorhandler(413)
def _too_big(error):
    return jsonify(error="too_large", message=f"That file is too big (limit {MAX_UPLOAD_MB} MB)."), 413


# --------------------------------------------------------------------------- #
# Pages and static files
# --------------------------------------------------------------------------- #

@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/subgenre")
def subgenre():
    return send_from_directory(STATIC_DIR, "subgenre.html")


@app.get("/import")
def import_page():
    return send_from_directory(STATIC_DIR, "import.html")


@app.get("/p/<playlist_id>")
def playlist_page(playlist_id):
    return send_from_directory(STATIC_DIR, "playlist.html")


@app.get("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(STATIC_DIR, filename)


# --------------------------------------------------------------------------- #
# Library
# --------------------------------------------------------------------------- #

def _library() -> dict:
    if store.library_counts()["tracks"] == 0:
        raise LibraryMissingError("No songs in the library yet.")
    return load_enriched_library()


def _tracks_by_id() -> dict[str, dict]:
    return {t["track_id"]: t for t in _library()["tracks"] if t.get("track_id")}


@app.get("/api/library")
def library():
    data = _library()
    return jsonify(
        generated_at=data.get("generated_at"),
        source_exported_at=data.get("source_exported_at"),
        status_counts=data.get("status_counts"),
        warnings=staleness_warnings(data),
        tracks=data["tracks"],
    )


@app.get("/api/taxonomy")
def taxonomy():
    # Families, subgenres (with parent links) and vibes, built by enrich_library.py.
    data = _library()
    return jsonify(generated_at=data.get("generated_at"), taxonomy=data.get("taxonomy"))


@app.get("/api/status")
def status():
    counts = store.library_counts()
    pending = None
    try:
        data = load_enriched_library() if counts["tracks"] else None
    except LibraryMissingError:
        data = None
    if data:
        pending = {
            "bpm": (data.get("status_counts") or {}).get("pending", 0),
            "tags": (data.get("tag_status_counts") or {}).get("pending", 0),
            "features": (data.get("feature_status_counts") or {}).get("pending", 0),
            "genres": (data.get("genre_status_counts") or {}).get("pending", 0),
        }
    return jsonify(library=counts, enrichment=worker.state, pending=pending)


QUEUE_STEPS = (  # in the order the pipeline runs them
    ("enrichment_status", "BPM/key"),
    ("tag_status", "tags"),
    ("feature_status", "audio features"),
    ("genre_status", "genres"),
)
QUEUE_LIMIT = 500


@app.get("/api/queue")
def queue():
    """Songs still waiting for at least one enrichment step, with what each is waiting for."""
    try:
        tracks = load_enriched_library()["tracks"] if store.library_counts()["tracks"] else []
    except LibraryMissingError:
        tracks = []
    waiting = []
    for t in tracks:
        steps = [label for field, label in QUEUE_STEPS if t.get(field) == "pending"]
        if steps:
            waiting.append({
                "track_id": t.get("track_id"),
                "track_name": t.get("track_name"),
                "artists": [a.get("name") for a in t.get("artists") or []],
                "added_at": t.get("added_at"),
                "waiting_for": steps,
            })
    return jsonify(total=len(waiting), tracks=waiting[:QUEUE_LIMIT], enrichment=worker.state)


@app.post("/api/import")
def import_csv():
    _limit(IMPORT_LIMIT)
    upload = request.files.get("file")
    if not upload or not upload.filename:
        raise exportify.ExportifyError("Choose a CSV file to upload.")
    if not upload.filename.lower().endswith(".csv"):
        raise exportify.ExportifyError("Please upload the .csv file Exportify gives you.")
    tracks, report = exportify.parse(upload.read())
    counts = store.import_tracks(tracks)
    store.record_import(report, counts)
    if counts["new"] or counts["updated"]:
        worker.rebuild()   # new songs show up right away with their CSV data
        worker.notify()    # ...and get enriched in the background
    return jsonify(report=report, counts=counts, library=store.library_counts(),
                   enrichment_queued=bool(worker.enabled and (counts["new"] or counts["updated"])))


# --------------------------------------------------------------------------- #
# Playlists
# --------------------------------------------------------------------------- #

def _body() -> tuple[str, list[str], list[str]]:
    body = request.get_json(silent=True) or {}
    track_ids = body.get("track_ids")
    if not isinstance(track_ids, list) or not all(isinstance(t, str) for t in track_ids):
        raise store.InvalidPlaylist("track_ids must be a list of Spotify track ids.")
    mood_tags = body.get("mood_tags") or []
    if not isinstance(mood_tags, list) or not all(isinstance(t, str) for t in mood_tags):
        raise store.InvalidPlaylist("mood_tags must be a list of tag names.")
    if len(track_ids) > store.MAX_PLAYLIST_TRACKS:
        raise store.InvalidPlaylist(f"A playlist can have at most {store.MAX_PLAYLIST_TRACKS} tracks.")
    return body.get("name", ""), track_ids, mood_tags


@app.get("/api/playlists")
def playlist_summaries():
    ids = [i for i in (request.args.get("ids") or "").split(",") if i]
    return jsonify(playlists=store.get_summaries(ids))


def _details(playlist: dict) -> list[dict]:
    """Display rows for the share page, using the library's current data
    (tempo/key: GetSongBPM, else the song's audio features)."""
    try:
        by_id = _tracks_by_id()
    except LibraryMissingError:
        by_id = {}
    rows = []
    for entry in playlist["tracks"]:
        t = by_id.get(entry["track_id"]) or {}
        enrichment, features = t.get("enrichment") or entry.get("enrichment") or {}, t.get("features") or {}
        rows.append({
            "track_id": entry["track_id"],
            "track_name": t.get("track_name") or entry.get("track_name"),
            "artists": [a.get("name") for a in t.get("artists") or []] or entry.get("artists") or [],
            "duration_ms": t.get("duration_ms") or entry.get("duration_ms"),
            "bpm": enrichment.get("bpm") or features.get("tempo"),
            "camelot": enrichment.get("camelot") or features.get("camelot"),
            "spotify_url": f"https://open.spotify.com/track/{entry['track_id']}",
        })
    return rows


@app.get("/api/playlists/<playlist_id>")
def get_playlist(playlist_id):
    playlist = store.get_playlist(playlist_id)
    if request.args.get("details"):
        playlist["details"] = _details(playlist)
    return jsonify(playlist)


@app.post("/api/playlists")
def create_playlist():
    _limit(WRITE_LIMIT)
    name, track_ids, mood_tags = _body()
    entries = setlists._snapshot(track_ids, _tracks_by_id(), [])
    playlist, key = store.create_playlist(name, entries, mood_tags)
    return jsonify(playlist=playlist, edit_key=key), 201


@app.put("/api/playlists/<playlist_id>")
def update_playlist(playlist_id):
    _limit(WRITE_LIMIT)
    name, track_ids, mood_tags = _body()
    key = request.headers.get("X-Edit-Key")
    previous = store.get_playlist(playlist_id)["tracks"]
    entries = setlists._snapshot(track_ids, _tracks_by_id(), previous)
    return jsonify(store.update_playlist(playlist_id, key, name, entries, mood_tags))


@app.delete("/api/playlists/<playlist_id>")
def delete_playlist(playlist_id):
    _limit(WRITE_LIMIT)
    store.delete_playlist(playlist_id, request.headers.get("X-Edit-Key"))
    return "", 204


@app.get("/api/playlists/<playlist_id>/export")
def export_playlist(playlist_id):
    fmt = request.args.get("format", "csv")
    playlist = store.get_playlist(playlist_id)
    tracks = _tracks_by_id()
    if fmt == "csv":
        body, mimetype = setlists.export_csv(playlist, tracks), "text/csv"
    elif fmt == "m3u":
        body, mimetype = setlists.export_m3u(playlist, tracks), "audio/x-mpegurl"
    else:
        raise store.InvalidPlaylist("format must be csv or m3u")
    filename = re.sub(r"[^A-Za-z0-9_\- ]+", "", playlist["name"]).strip() or "playlist"
    return Response(
        body,
        mimetype=f"{mimetype}; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}.{fmt}"'},
    )
