"""Run the setlist builder: python -m setlist_app [--port 8765] [--no-browser]"""

import argparse
import logging
import threading
import webbrowser

from flask import cli as flask_cli

from .library import LibraryMissingError, load_enriched_library, staleness_warnings
from .paths import SETLISTS_PATH
from .server import app


def main() -> None:
    parser = argparse.ArgumentParser(description="Local DJ setlist builder.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true", help="Don't open a browser tab.")
    args = parser.parse_args()

    try:
        library = load_enriched_library()
    except LibraryMissingError as error:
        print(f"Warning: {error}\nThe app will start, but shows nothing until then.")
    else:
        print(f"Loaded {library['track_count']} tracks.")
        for warning in staleness_warnings(library):
            print(f"Warning: {warning}")
    print(f"Setlists are saved to {SETLISTS_PATH}")

    url = f"http://127.0.0.1:{args.port}/"
    print(f"Setlist builder running at {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    # Personal local tool: skip Flask's "development server" banner and the
    # per-request log lines.
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    flask_cli.show_server_banner = lambda *args, **kwargs: None
    # 127.0.0.1 only: the app is never reachable from other machines.
    app.run(host="127.0.0.1", port=args.port, debug=False)


if __name__ == "__main__":
    main()
