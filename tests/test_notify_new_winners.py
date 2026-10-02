"""Tests for the "new house won" email (scripts/notify_new_winners.py).

Stage C drops a per-track marker of winners never archived before; after
Stage E the notifier reads the markers, keeps the houses still on the final
boards, and sends ONE email through the personal Gmail MCP server. Nothing
here spawns that server — the sender is injected; the MCP exchange itself is
tested against a fake stdio server (tests/fake_mcp_server.py).
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import notify_new_winners as nn

MAILBOX = "family@example.com"
ENV = {"GMAIL_MAILBOX": MAILBOX, "GMAIL_MCP_USER_ID": "user_test"}
SERVER = {"command": "true", "args": []}
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


def _run(tmp_path, tracks, sent, **kw):
    opts = dict(env=ENV, server_cfg=SERVER,
                sender=lambda p, uid, cfg: sent.append((p, uid, cfg)) or "ok")
    opts.update(kw)
    return nn.run(tmp_path, tracks, "2026-10-02", **opts)


# ---------------------------------------------------------------- markers

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


# ---------------------------------------------------------------- message

def test_message_links_the_ad_and_shows_the_photo():
    house = dict(HOUSE, track_label="Gran Canaria")
    msg = nn.build_message([house], "2026-10-02", MAILBOX)
    assert msg["to"] == [MAILBOX]
    assert "1 casa nueva" in msg["subject"]
    assert HOUSE["url"] in msg["html"] and HOUSE["url"] in msg["text"]
    assert HOUSE["title"] in msg["html"]
    assert "345.000 €" in msg["html"]
    assert "Gran Canaria" in msg["html"]
    assert f'src="{HOUSE["photo"]}"' in msg["html"]


def test_message_without_a_photo_has_no_img():
    house = dict(HOUSE, photo=None, track_label="Tafira")
    assert "<img" not in nn.build_message([house], "2026-10-02", MAILBOX)["html"]


def test_subject_pluralises():
    a = dict(HOUSE, track_label="Tafira")
    b = dict(HOUSE, id="x", track_label="Gran Canaria")
    assert "2 casas nuevas" in nn.build_message([a, b], "2026-10-02", MAILBOX)["subject"]


# ---------------------------------------------------------------- run()

def test_run_sends_once_and_clears_the_markers(tmp_path):
    tracks = _tracks(tmp_path)
    nn.write_marker(tmp_path, "gc", "2026-10-02", [HOUSE])
    sent = []
    assert _run(tmp_path, tracks, sent) == 0
    assert len(sent) == 1
    payload, uid, cfg = sent[0]
    assert uid == "user_test" and cfg is SERVER and payload["to"] == [MAILBOX]
    assert not nn.marker_path(tmp_path, "gc").exists()
    # same day again: nothing resent
    nn.write_marker(tmp_path, "gc", "2026-10-02", [HOUSE])
    _run(tmp_path, tracks, sent)
    assert len(sent) == 1


def test_run_without_env_config_skips_but_does_not_fail(tmp_path):
    nn.write_marker(tmp_path, "gc", "2026-10-02", [HOUSE])
    sent = []
    assert _run(tmp_path, _tracks(tmp_path), sent, env={}) == 0
    assert sent == []


def test_run_without_server_config_skips_but_does_not_fail(tmp_path, monkeypatch):
    monkeypatch.setattr(nn, "SETTINGS_FILES", (tmp_path / "no-settings.json",))
    nn.write_marker(tmp_path, "gc", "2026-10-02", [HOUSE])
    sent = []
    assert _run(tmp_path, _tracks(tmp_path), sent, server_cfg=None) == 0
    assert sent == []


def test_run_with_nothing_new_sends_nothing(tmp_path):
    sent = []
    assert _run(tmp_path, _tracks(tmp_path), sent) == 0 and sent == []


def test_run_survives_a_failing_sender(tmp_path):
    """A dead MCP server must never block Stage D's publish."""
    nn.write_marker(tmp_path, "gc", "2026-10-02", [HOUSE])

    def boom(p, uid, cfg):
        raise nn.McpError("server closed the pipe")

    assert _run(tmp_path, _tracks(tmp_path), [], sender=boom) == 0
    assert not (tmp_path / "notified-2026-10-02.json").exists()


# ---------------------------------------------------------------- config

def test_load_env_parses_the_env_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text('# comment\nGMAIL_MAILBOX="me@example.com"\nGMAIL_MCP_USER_ID=user_1\n')
    assert nn.load_env(env) == {"GMAIL_MAILBOX": "me@example.com", "GMAIL_MCP_USER_ID": "user_1"}
    assert nn.load_env(tmp_path / "missing") == {}


def test_mcp_server_config_reads_claude_settings(tmp_path):
    s = tmp_path / "settings.json"
    s.write_text(json.dumps({"mcpServers": {"gmail-personal": {"command": "node", "args": ["x.js"]}}}))
    assert nn.mcp_server_config((tmp_path / "missing.json", s)) == {"command": "node", "args": ["x.js"]}
    s.write_text(json.dumps({"mcpServers": {}}))
    assert nn.mcp_server_config((s,)) is None


def test_run_without_user_id_still_sends_single_user(tmp_path):
    nn.write_marker(tmp_path, "gc", "2026-10-02", [HOUSE])
    sent = []
    assert _run(tmp_path, _tracks(tmp_path), sent, env={"GMAIL_MAILBOX": MAILBOX}) == 0
    assert len(sent) == 1 and sent[0][1] is None


def test_send_omits_user_id_when_not_configured():
    cfg = {"command": sys.executable, "args": [FAKE, "ok", "single"]}
    assert "sent" in nn.send({"to": ["a@b"], "subject": "s"}, None, cfg, timeout=20)


# ---------------------------------------------------------------- MCP exchange

FAKE = str(Path(__file__).parent / "fake_mcp_server.py")


def test_send_talks_mcp_to_a_stdio_server():
    cfg = {"command": sys.executable, "args": [FAKE, "ok"]}
    out = nn.send({"to": ["a@b"], "subject": "s", "html": "<p>x</p>"}, "user_1", cfg, timeout=20)
    assert "sent" in out


def test_send_raises_on_a_tool_error():
    cfg = {"command": sys.executable, "args": [FAKE, "error"]}
    with pytest.raises(nn.McpError):
        nn.send({"to": ["a@b"], "subject": "s"}, "user_1", cfg, timeout=20)


def test_send_times_out_on_a_silent_server():
    cfg = {"command": sys.executable, "args": [FAKE, "hang"]}
    with pytest.raises(nn.McpError, match="within"):
        nn.send({"to": ["a@b"], "subject": "s"}, "user_1", cfg, timeout=2)
