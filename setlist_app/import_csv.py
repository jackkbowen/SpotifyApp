"""Import an Exportify CSV from the command line (same rules as the web page).

    python -m setlist_app.import_csv path/to/Liked_Songs.csv [more.csv ...]

Handy for seeding the library. Songs show up immediately; run the site
(python -m setlist_app) or `python enrich_library.py` to enrich them.
"""

import sys
from pathlib import Path

from . import exportify, store
from .worker import worker


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    changed = False
    for name in argv:
        path = Path(name)
        try:
            tracks, report = exportify.parse(path.read_bytes())
        except (OSError, exportify.ExportifyError) as error:
            print(f"{path.name}: {error}", file=sys.stderr)
            continue
        counts = store.import_tracks(tracks)
        store.record_import(report, counts)
        changed |= bool(counts["new"] or counts["updated"])
        print(f"{path.name}: {report['valid']} songs read ({counts['new']} new, {counts['updated']} updated, "
              f"{counts['unchanged']} already in the library); skipped {report['skipped_local']} local files "
              f"and {report['skipped_invalid']} unusable rows.")
        for note in report["notes"]:
            print(f"  note: {note}")
    if changed:
        worker.rebuild()
    print(f"Library now has {store.library_counts()['tracks']} songs.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
