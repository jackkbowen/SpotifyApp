"""Export a saved setlist to a playlist in your Spotify account.

Uses its own login and token cache (.spotify_playlist_token_cache) with the
`playlist-modify-private` scope, separate from export_liked_songs.py's
read-only `user-library-read` login, so neither script disturbs the other's
cached token. The playlist is created private.

Endpoints (current as of the Feb 2026 Web API changes): POST /me/playlists,
PUT/POST /playlists/{id}/items, PUT /playlists/{id}.
"""

from __future__ import annotations

import os
import threading
import webbrowser

import spotipy
from dotenv import load_dotenv
from spotipy.cache_handler import CacheFileHandler
from spotipy.exceptions import SpotifyException, SpotifyOauthError
from spotipy.oauth2 import SpotifyOAuth

from .paths import ENV_PATH, PROJECT_DIR

SCOPE = "playlist-modify-private"
TOKEN_CACHE_PATH = PROJECT_DIR / ".spotify_playlist_token_cache"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8888/callback"
MAX_ITEMS_PER_REQUEST = 100  # Spotify's limit for adding/replacing playlist items
DESCRIPTION_LIMIT = 300

_login_thread: threading.Thread | None = None
_login_error: str | None = None
_display_name: str | None = None


class SpotifyNotConnected(Exception):
    pass


class SpotifyExportError(Exception):
    pass


def _setting(name: str) -> str:
    value = os.getenv(name, "").strip()
    return "" if value.startswith("your-") or set(value) <= {"X"} else value


def _auth_manager() -> SpotifyOAuth:
    load_dotenv(ENV_PATH)
    client_id, secret = _setting("SPOTIFY_CLIENT_ID"), _setting("SPOTIFY_CLIENT_SECRET")
    if not client_id or not secret:
        raise SpotifyExportError(
            "SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET aren't set in .env (see README, phase 1)."
        )
    return SpotifyOAuth(
        client_id=client_id,
        client_secret=secret,
        redirect_uri=_setting("SPOTIFY_REDIRECT_URI") or DEFAULT_REDIRECT_URI,
        scope=SCOPE,
        cache_handler=CacheFileHandler(cache_path=str(TOKEN_CACHE_PATH)),
        open_browser=True,
    )


def _cached_token() -> str | None:
    """A valid access token from the cache (refreshed if needed), or None.
    Never starts an interactive login."""
    auth = _auth_manager()
    try:
        token = auth.validate_token(auth.cache_handler.get_cached_token())
    except SpotifyOauthError:
        # Refresh token revoked/expired: forget it so the next connect starts clean.
        TOKEN_CACHE_PATH.unlink(missing_ok=True)
        return None
    return token["access_token"] if token else None


def status() -> dict:
    global _display_name
    connecting = bool(_login_thread and _login_thread.is_alive())
    try:
        token = _cached_token()
    except SpotifyExportError as error:
        return {"configured": False, "connected": False, "connecting": False, "message": str(error)}
    if token and _display_name is None:
        try:
            me = spotipy.Spotify(auth=token, requests_timeout=15).current_user()
            _display_name = me.get("display_name") or me.get("id")
        except SpotifyException:
            _display_name = None
    return {
        "configured": True,
        "connected": bool(token),
        "connecting": connecting and not token,
        "user": _display_name if token else None,
        "error": _login_error,
    }


def start_login() -> None:
    """Open Spotify's consent page; spotipy catches the redirect on the
    registered 127.0.0.1:8888 callback in a background thread."""
    global _login_thread, _login_error
    auth = _auth_manager()
    if _login_thread and _login_thread.is_alive():
        # Already waiting for the callback: just show the login page again.
        webbrowser.open(auth.get_authorize_url())
        return
    _login_error = None

    def run():
        global _login_error
        try:
            auth.get_access_token(as_dict=False)
        except Exception as error:  # surfaced to the page via status()
            _login_error = f"Spotify login failed: {error}"

    _login_thread = threading.Thread(target=run, name="spotify-login", daemon=True)
    _login_thread.start()


def disconnect() -> None:
    global _display_name
    TOKEN_CACHE_PATH.unlink(missing_ok=True)
    _display_name = None


def _description(setlist: dict) -> str:
    parts = ["DJ set from my Setlist Builder."]
    if setlist.get("mood_tags"):
        parts.append(f"Mood: {', '.join(setlist['mood_tags'])}.")
    # GetSongBPM's free API requires a backlink wherever its data is shared.
    parts.append("BPM/key data: getsongbpm.com. Tags: last.fm.")
    text = " ".join(parts)
    return text if len(text) <= DESCRIPTION_LIMIT else text[: DESCRIPTION_LIMIT - 1] + "…"


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def export_setlist(setlist: dict, as_new: bool) -> dict:
    """Create (or update in place) a private Spotify playlist for this setlist.

    Returns {"playlist_id", "url", "created", "track_count"}.
    """
    token = _cached_token()
    if not token:
        raise SpotifyNotConnected()
    sp = spotipy.Spotify(auth=token, requests_timeout=20)
    uris = [f"spotify:track:{t['track_id']}" for t in setlist["tracks"] if t.get("track_id")]
    if not uris:
        raise SpotifyExportError("This setlist has no tracks to export.")
    existing = setlist.get("spotify_playlist_id")

    try:
        if existing and not as_new:
            try:
                sp.playlist_change_details(existing, name=setlist["name"], description=_description(setlist))
            except SpotifyException as error:
                if error.http_status in (403, 404):
                    raise SpotifyExportError(
                        "Couldn't update the existing Spotify playlist (it may have been deleted, "
                        "or belongs to another account). Use “Export as new playlist” instead."
                    ) from error
                raise
            playlist_id, url, created = existing, setlist.get("spotify_playlist_url"), False
            # Replacing sets the first 100; the rest are appended.
            first, *rest = list(_chunks(uris, MAX_ITEMS_PER_REQUEST))
            sp.playlist_replace_items(playlist_id, first)
        else:
            playlist = sp.current_user_playlist_create(
                setlist["name"], public=False, description=_description(setlist))
            playlist_id = playlist["id"]
            url = (playlist.get("external_urls") or {}).get("spotify") or f"https://open.spotify.com/playlist/{playlist_id}"
            created, rest = True, list(_chunks(uris, MAX_ITEMS_PER_REQUEST))
        for chunk in rest:
            sp.playlist_add_items(playlist_id, chunk)
    except SpotifyException as error:
        if error.http_status == 401:
            disconnect()
            raise SpotifyNotConnected() from error
        if error.http_status == 403:
            raise SpotifyExportError(
                "Spotify refused (HTTP 403). In Development Mode, your account must be listed under "
                "User Management in the Developer Dashboard, and the app owner needs Premium. "
                f"Details: {error.msg}"
            ) from error
        if error.http_status == 429:
            raise SpotifyExportError("Spotify is rate limiting this app; try again in a few minutes.") from error
        raise SpotifyExportError(f"Spotify error (HTTP {error.http_status}): {error.msg}") from error

    return {"playlist_id": playlist_id, "url": url, "created": created, "track_count": len(uris)}
