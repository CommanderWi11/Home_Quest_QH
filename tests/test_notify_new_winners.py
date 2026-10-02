"""Tests for the "new house won" email (scripts/notify_new_winners.py).

Stage C drops a per-track marker of winners never archived before; after
Stage E the notifier reads the markers, keeps the houses still on the final
boards, and sends ONE email. Nothing here touches the network or SMTP —
sending is injected.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import notify_new_winners as nn

MAILBOX = "family@example.com"

HOUSE = {
    "id": "idealista-9d363c79",
    "url": "https://www.idealista.com/inmueble/111559115/",
    "title": "Chalet pareado en Calle Montebravo, 2",
    "price": 345000,
    "location": "Santa Brígida, Gran Canaria",
    "photo": "https://img4.idealista.com/blur/480_360_mq/x.jpg",
    "rank": 3,
}


def _tracks(tmp_path):
    board = tmp_path / "listings-gc.json"
    board.write_text(json.dumps([HOUSE]))
    return {"gc": {"label": "Gran Canaria", "listings_file": board}}


def test_write_then_collect_round_trips_a_marker(tmp_path):
    nn.write_marker(tmp_path, "gc", "2026-10-02", [HOUSE])
    got = nn.collect(tmp_path, _tracks(tmp_path), "2026-10-02")
    assert [h["id"] for h in got] == [HOUSE["id"]]
    assert got[0]["track_label"] == "Gran Canaria"


def test_collect_ignores_a_house_no_longer_on_the_board(tmp_path):
    """Stage E (cross-track dedup) can drop a winner after Stage C wrote the
    marker — the family must not be emailed about a house they can't see."""
    tracks = _tracks(tmp_path)
    tracks["gc"]["listings_file"].write_text("[]")
    nn.write_marker(tmp_path, "gc", "2026-10-02", [HOUSE])
    assert nn.collect(tmp_path, tracks, "2026-10-02") == []


def test_collect_ignores_a_stale_marker_from_another_day(tmp_path):
    nn.write_marker(tmp_path, "gc", "2026-10-01", [HOUSE])
    assert nn.collect(tmp_path, _tracks(tmp_path), "2026-10-02") == []


def test_collect_with_no_marker_is_empty(tmp_path):
    assert nn.collect(tmp_path, _tracks(tmp_path), "2026-10-02") == []


def test_message_links_the_ad_and_embeds_the_photo():
    house = dict(HOUSE, track_label="Gran Canaria")
    msg = nn.build_message([house], {HOUSE["id"]: b"\xff\xd8fakejpeg"}, "2026-10-02", MAILBOX)
    assert msg["To"] == MAILBOX
    assert msg["From"] == MAILBOX
    assert "1 casa nueva" in msg["Subject"]
    html = msg.get_body(preferencelist=("html",)).get_content()
    assert HOUSE["url"] in html
    assert HOUSE["title"] in html
    assert "345.000 €" in html
    assert "Gran Canaria" in html
    assert 'src="cid:' in html
    attached = [p for p in msg.walk() if p.get_content_type() == "image/jpeg"]
    assert len(attached) == 1


def test_message_falls_back_to_the_photo_url_when_fetch_failed():
    house = dict(HOUSE, track_label="Gran Canaria")
    msg = nn.build_message([house], {}, "2026-10-02", MAILBOX)
    html = msg.get_body(preferencelist=("html",)).get_content()
    assert f'src="{HOUSE["photo"]}"' in html
    assert "cid:" not in html


def test_subject_pluralises():
    a = dict(HOUSE, track_label="Tafira")
    b = dict(HOUSE, id="x", track_label="Gran Canaria")
    assert "2 casas nuevas" in nn.build_message([a, b], {}, "2026-10-02", MAILBOX)["Subject"]


def test_run_sends_once_and_clears_the_markers(tmp_path):
    tracks = _tracks(tmp_path)
    nn.write_marker(tmp_path, "gc", "2026-10-02", [HOUSE])
    sent = []
    rc = nn.run(tmp_path, tracks, "2026-10-02", mailbox=MAILBOX,
                password_getter=lambda: "app-pw", sender=lambda m, pw: sent.append((m, pw)),
                photo_getter=lambda url: None)
    assert rc == 0
    assert len(sent) == 1 and sent[0][1] == "app-pw"
    assert not nn.marker_path(tmp_path, "gc").exists()
    # same day again: nothing new, nothing resent
    nn.write_marker(tmp_path, "gc", "2026-10-02", [HOUSE])
    nn.run(tmp_path, tracks, "2026-10-02", mailbox=MAILBOX,
           password_getter=lambda: "app-pw", sender=lambda m, pw: sent.append((m, pw)),
           photo_getter=lambda url: None)
    assert len(sent) == 1


def test_run_without_credential_skips_but_does_not_fail(tmp_path):
    nn.write_marker(tmp_path, "gc", "2026-10-02", [HOUSE])
    sent = []
    rc = nn.run(tmp_path, _tracks(tmp_path), "2026-10-02", mailbox=MAILBOX,
                password_getter=lambda: None, sender=lambda m, pw: sent.append(m),
                photo_getter=lambda url: None)
    assert rc == 0 and sent == []


def test_run_with_nothing_new_sends_nothing(tmp_path):
    sent = []
    rc = nn.run(tmp_path, _tracks(tmp_path), "2026-10-02", mailbox=MAILBOX,
                password_getter=lambda: "pw", sender=lambda m, pw: sent.append(m),
                photo_getter=lambda url: None)
    assert rc == 0 and sent == []


def test_run_survives_a_failing_sender(tmp_path):
    """A dead SMTP must never block Stage D's publish."""
    nn.write_marker(tmp_path, "gc", "2026-10-02", [HOUSE])

    def boom(m, pw):
        raise OSError("smtp down")

    rc = nn.run(tmp_path, _tracks(tmp_path), "2026-10-02", mailbox=MAILBOX,
                password_getter=lambda: "pw", sender=boom, photo_getter=lambda url: None)
    assert rc == 0


def test_run_without_mailbox_skips_but_does_not_fail(tmp_path, monkeypatch):
    monkeypatch.setattr(nn, "ENV_FILE", tmp_path / "no-such-env")
    nn.write_marker(tmp_path, "gc", "2026-10-02", [HOUSE])
    sent = []
    rc = nn.run(tmp_path, _tracks(tmp_path), "2026-10-02", mailbox=None,
                password_getter=lambda: "pw", sender=lambda m, pw: sent.append(m),
                photo_getter=lambda url: None)
    assert rc == 0 and sent == []


def test_load_mailbox_reads_the_env_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# comment\nOTHER=1\nGMAIL_MAILBOX=\"me@example.com\"\n")
    assert nn.load_mailbox(env) == "me@example.com"
    assert nn.load_mailbox(tmp_path / "missing") is None
