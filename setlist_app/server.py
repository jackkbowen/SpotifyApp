"""Flask server: JSON API for the library and setlists, plus the static UI."""

from __future__ import annotations

import re
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

from . import setlists, spotify_export
from .library import LibraryMissingError, load_enriched_library, staleness_warnings

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = Flask(__name__, static_folder=None)


def _tracks_by_id() -> dict[str, dict]:
    return {t["track_id"]: t for t in load_enriched_library()["tracks"] if t.get("track_id")}


@app.errorhandler(LibraryMissingError)
def _library_missing(error):
    return jsonify(error="library_missing", message=str(error)), 409


@app.errorhandler(setlists.SetlistNotFound)
def _setlist_missing(error):
    return jsonify(error="not_found", message="That setlist no longer exists."), 404


@app.errorhandler(setlists.InvalidSetlist)
def _bad_request(error):
    return jsonify(error="bad_request", message=str(error)), 400


@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(STATIC_DIR, filename)


@app.get("/api/library")
def library():
    data = load_enriched_library()
    return jsonify(
        generated_at=data.get("generated_at"),
        source_exported_at=data.get("source_exported_at"),
        status_counts=data.get("status_counts"),
        warnings=staleness_warnings(data),
        tracks=data["tracks"],
    )


@app.get("/api/setlists")
def list_setlists():
    return jsonify(setlists=setlists.list_setlists())


@app.get("/api/setlists/<setlist_id>")
def get_setlist(setlist_id):
    return jsonify(setlists.get_setlist(setlist_id))


def _save(setlist_id=None):
    body = request.get_json(silent=True) or {}
    track_ids = body.get("track_ids")
    if not isinstance(track_ids, list) or not all(isinstance(t, str) for t in track_ids):
        raise setlists.InvalidSetlist("track_ids must be a list of Spotify track ids.")
    mood_tags = body.get("mood_tags") or []
    if not isinstance(mood_tags, list) or not all(isinstance(t, str) for t in mood_tags):
        raise setlists.InvalidSetlist("mood_tags must be a list of tag names.")
    saved = setlists.save_setlist(body.get("name", ""), track_ids, _tracks_by_id(), setlist_id,
                                  mood_tags=mood_tags)
    return jsonify(saved)


@app.post("/api/setlists")
def create_setlist():
    return _save()


@app.put("/api/setlists/<setlist_id>")
def update_setlist(setlist_id):
    return _save(setlist_id)


@app.delete("/api/setlists/<setlist_id>")
def delete_setlist(setlist_id):
    setlists.delete_setlist(setlist_id)
    return "", 204


@app.errorhandler(spotify_export.SpotifyNotConnected)
def _spotify_not_connected(error):
    return jsonify(error="spotify_not_connected", message="Connect your Spotify account first."), 409


@app.errorhandler(spotify_export.SpotifyExportError)
def _spotify_failed(error):
    return jsonify(error="spotify_error", message=str(error)), 502


@app.get("/api/spotify/status")
def spotify_status():
    return jsonify(spotify_export.status())


@app.post("/api/spotify/connect")
def spotify_connect():
    spotify_export.start_login()
    return jsonify(spotify_export.status())


@app.post("/api/spotify/disconnect")
def spotify_disconnect():
    spotify_export.disconnect()
    return jsonify(spotify_export.status())


@app.post("/api/setlists/<setlist_id>/spotify")
def export_to_spotify(setlist_id):
    body = request.get_json(silent=True) or {}
    setlist = setlists.get_setlist(setlist_id)
    result = spotify_export.export_setlist(setlist, as_new=bool(body.get("as_new")))
    updated = setlists.record_spotify_export(setlist_id, result["playlist_id"], result["url"])
    return jsonify(result=result, setlist=updated)


@app.get("/api/setlists/<setlist_id>/export")
def export_setlist(setlist_id):
    fmt = request.args.get("format", "csv")
    setlist = setlists.get_setlist(setlist_id)
    tracks = _tracks_by_id()
    if fmt == "csv":
        body, mimetype = setlists.export_csv(setlist, tracks), "text/csv"
    elif fmt == "m3u":
        body, mimetype = setlists.export_m3u(setlist, tracks), "audio/x-mpegurl"
    else:
        raise setlists.InvalidSetlist("format must be csv or m3u")
    filename = re.sub(r"[^A-Za-z0-9_\- ]+", "", setlist["name"]).strip() or "setlist"
    return Response(
        body,
        mimetype=f"{mimetype}; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}.{fmt}"'},
    )
