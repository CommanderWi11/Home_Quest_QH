"""Per-track "previous searches" archive — one dated snapshot of that day's
real winners per track, kept forever (unlike docs/listings.json, which only
ever holds TODAY's Top 5 + Favorites and drops everything else).

Same shape and same dashboard-side dedup as the pre-existing manual-shortlist
history (docs/history.json / history-dedup.js): the dashboard renders each
listing under only the newest date that mentions it (dedupeHistoryByLatest in
docs/history-dedup.js) and never under a date it's already shown for
elsewhere (today's Top 5, Favoritos). The archive file itself is never
deduped or pruned — every day's real result stays on record; only what the
dashboard chooses to render is deduped, client-side.
"""
import json
from pathlib import Path


def load_snapshots(archive_file: Path) -> list:
    if not archive_file.exists():
        return []
    try:
        return json.loads(archive_file.read_text())
    except json.JSONDecodeError:
        return []


def append_snapshot(archive_file: Path, date: str, entries: list) -> set[str]:
    """Record `entries` (today's validated winners) as `date`'s snapshot.

    Idempotent per calendar day: replaces any existing snapshot for the same
    date instead of duplicating it, so a forced same-day re-run (delete the
    .state marker, re-kick the job) doesn't pile up two entries for one day.
    Newest-first, matching history.json's existing convention.

    Returns the ids in `entries` that no OTHER day's snapshot mentions — i.e.
    houses winning for the first time ever. notify_new_winners.py emails the
    family about exactly these. Today's own (replaced) snapshot is excluded
    from "seen before" so a same-day re-run reports the same set.
    """
    snapshots = [s for s in load_snapshots(archive_file) if s.get("date") != date]
    seen = {e.get("id") for s in snapshots for e in s.get("entries", [])}
    snapshots.insert(0, {"date": date, "entries": entries})
    archive_file.write_text(json.dumps(snapshots, ensure_ascii=False, indent=2))
    return {e["id"] for e in entries if e.get("id") and e["id"] not in seen}
