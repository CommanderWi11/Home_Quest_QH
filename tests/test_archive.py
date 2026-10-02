"""Tests for the "previous searches" per-track archive (scripts/archive.py).

Client-side dedup (which house shows under which date) is already covered by
tests/test_history_dedup.js, since app.js reuses history-dedup.js's
dedupeHistoryByLatest() verbatim for this. What's tested here is the
write-side contract: idempotent per calendar day, newest-first, non-mutating.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import archive


def test_append_snapshot_creates_a_new_file(tmp_path):
    f = tmp_path / "archive.json"
    archive.append_snapshot(f, "2026-08-28", [{"id": "a", "title": "Chalet en Tafira"}])
    assert json.loads(f.read_text()) == [
        {"date": "2026-08-28", "entries": [{"id": "a", "title": "Chalet en Tafira"}]}
    ]


def test_append_snapshot_is_idempotent_for_the_same_day():
    """A forced same-day re-run (delete the .state marker, re-kick the job)
    must replace today's snapshot, not duplicate it."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "archive.json"
        archive.append_snapshot(f, "2026-08-28", [{"id": "a"}])
        archive.append_snapshot(f, "2026-08-28", [{"id": "b"}])
        snapshots = json.loads(f.read_text())
        assert len(snapshots) == 1
        assert snapshots[0]["entries"] == [{"id": "b"}]


def test_append_snapshot_keeps_older_days_and_sorts_newest_first():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "archive.json"
        archive.append_snapshot(f, "2026-08-27", [{"id": "a"}])
        archive.append_snapshot(f, "2026-08-28", [{"id": "b"}])
        snapshots = json.loads(f.read_text())
        assert [s["date"] for s in snapshots] == ["2026-08-28", "2026-08-27"]


def test_load_snapshots_returns_empty_list_for_a_missing_file(tmp_path):
    assert archive.load_snapshots(tmp_path / "does-not-exist.json") == []


def test_load_snapshots_returns_empty_list_for_corrupt_json(tmp_path):
    f = tmp_path / "archive.json"
    f.write_text("not valid json{{{")
    assert archive.load_snapshots(f) == []


def test_append_snapshot_returns_ids_never_archived_before(tmp_path):
    """The "new house" email (notify_new_winners.py) keys off this return value:
    an id is new only if no EARLIER day's snapshot mentions it."""
    f = tmp_path / "archive.json"
    assert archive.append_snapshot(f, "2026-08-27", [{"id": "a"}, {"id": "b"}]) == {"a", "b"}
    assert archive.append_snapshot(f, "2026-08-28", [{"id": "b"}, {"id": "c"}]) == {"c"}


def test_append_snapshot_same_day_rerun_reports_the_same_new_ids(tmp_path):
    """A forced same-day re-run replaces today's snapshot, so today's own
    entries must not count as 'seen before' on the second pass."""
    f = tmp_path / "archive.json"
    archive.append_snapshot(f, "2026-08-27", [{"id": "a"}])
    assert archive.append_snapshot(f, "2026-08-28", [{"id": "a"}, {"id": "c"}]) == {"c"}
    assert archive.append_snapshot(f, "2026-08-28", [{"id": "a"}, {"id": "c"}]) == {"c"}
