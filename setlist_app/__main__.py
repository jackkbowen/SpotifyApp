"""Run the site: python -m setlist_app [--port 8765] [--no-browser] [--no-enrich]"""

import argparse
import json
import logging
import threading
import webbrowser

from flask import cli as flask_cli

from . import store
from .paths import ENRICHED_LIBRARY_PATH, LIBRARY_SOURCE_PATH, MIGRATED_LINKS_PATH
from .server import app
from .worker import worker


def _library_is_current() -> bool:
    """True if library_enriched.json was built from the database's current contents."""
    counts = store.library_counts()
    if not counts["tracks"]:
        return True
    if not ENRICHED_LIBRARY_PATH.exists() or not LIBRARY_SOURCE_PATH.exists():
        return False
    with ENRICHED_LIBRARY_PATH.open(encoding="utf-8") as f:
        built_from = json.load(f).get("source_exported_at")
    return built_from == counts["changed_at"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Shared-library DJ setlist site.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true", help="Don't open a browser tab.")
    parser.add_argument("--no-enrich", action="store_true",
                        help="Don't call the enrichment APIs; imported songs use their CSV data only.")
    args = parser.parse_args()

    store.connect().close()  # create the database if needed
    links = store.migrate_setlists_json()
    if links:
        print(f"Moved {len(links)} saved setlist(s) into the database. Their edit links are in {MIGRATED_LINKS_PATH}.")

    counts = store.library_counts()
    print(f"Shared library: {counts['tracks']} songs from {counts['imports']} import(s).")
    if not _library_is_current():
        print("Rebuilding the library from the database...")
        worker.rebuild()
    worker.start(enabled=not args.no_enrich)
    if counts["tracks"] and not args.no_enrich:
        worker.notify()  # catch up on anything not enriched yet

    url = f"http://127.0.0.1:{args.port}/"
    print(f"Running at {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    # Skip Flask's "development server" banner and the per-request log lines.
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    flask_cli.show_server_banner = lambda *args, **kwargs: None
    # 127.0.0.1 only for now. Going public needs a production server (e.g.
    # waitress) behind a proxy; see the README.
    app.run(host="127.0.0.1", port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
