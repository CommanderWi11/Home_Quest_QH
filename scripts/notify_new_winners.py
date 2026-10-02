#!/usr/bin/env python3
"""Email the family when a house wins a track's Top 5 for the FIRST time ever.

Two halves:

  * Stage C (apply_winners.py) calls write_marker() with the winners whose id
    no earlier day's archive snapshot mentions (archive.append_snapshot()'s
    return value) -> .state/new-winners-<track>.json
  * weekly-search.sh runs this script once, after Stage E (cross-track dedup)
    and before Stage D (push). It reads both markers, keeps only the houses
    still on the final boards, and sends ONE email with each house's front
    photo and a direct link to the ad.

Idempotent per calendar day (.state/notified-<date>.json), stale markers from
another day are ignored, and nothing here may ever block the publish: every
failure path logs and exits 0.

Sending goes through the already-authorized personal Gmail MCP server
(`gmail-personal` in ~/.claude/settings.json, the family's OAuth): this script
spawns that exact node process and speaks MCP JSON-RPC to it over stdio —
no `claude -p`, no LLM call, no extra credential. Config, both local-only
because the repo is public, in the gitignored .env:
  GMAIL_MAILBOX=...       sender == recipient
  GMAIL_MCP_USER_ID=...   the server's multi-user session id for that mailbox
Without either, the notifier says so and skips.
"""
import json
import subprocess
import sys
import threading
from datetime import date
from html import escape
from pathlib import Path
from typing import Callable, Optional

import tracks

STATE_DIR = Path(__file__).parent.parent / ".state"
ENV_FILE = Path(__file__).parent.parent / ".env"
SETTINGS_FILE = Path.home() / ".claude" / "settings.json"
MCP_SERVER = "gmail-personal"
MCP_TIMEOUT = 90  # seconds for the whole initialize -> send exchange
DASHBOARD = "https://commanderwi11.github.io/Home_Quest_QH/"


# ------------------------------------------------------------------ markers

def marker_path(state_dir: Path, track: str) -> Path:
    return state_dir / f"new-winners-{track}.json"


def write_marker(state_dir: Path, track: str, today: str, houses: list) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    marker_path(state_dir, track).write_text(
        json.dumps({"date": today, "houses": houses}, ensure_ascii=False, indent=2))


def collect(state_dir: Path, track_cfg: dict, today: str) -> list:
    """Every first-time winner recorded today that is still on its final board."""
    out = []
    for track, cfg in track_cfg.items():
        marker = marker_path(state_dir, track)
        if not marker.exists():
            continue
        try:
            data = json.loads(marker.read_text())
        except json.JSONDecodeError:
            continue
        if data.get("date") != today:
            continue
        try:
            on_board = {l.get("id") for l in json.loads(cfg["listings_file"].read_text())}
        except (OSError, json.JSONDecodeError):
            on_board = set()
        for h in data.get("houses", []):
            if h.get("id") in on_board:
                out.append(dict(h, track_label=cfg["label"]))
    return out


def clear_markers(state_dir: Path, track_cfg: dict) -> None:
    for track in track_cfg:
        marker_path(state_dir, track).unlink(missing_ok=True)


# ------------------------------------------------------------------ message

def _price(p) -> str:
    try:
        return f"{int(p):,} €".replace(",", ".")
    except (TypeError, ValueError):
        return "precio no indicado"


def build_message(houses: list, today: str, mailbox: str) -> dict:
    """One email for all of today's first-time winners, as the dict the Gmail
    MCP `gmail_send_email` tool takes. The photo is the portal's own image URL
    (Gmail fetches it through its proxy at view time)."""
    n = len(houses)
    plural = "s" if n != 1 else ""
    lines = [f"Home Quest {today}: {n} casa{plural} nueva{plural} en el Top 5", ""]
    blocks = []
    for h in houses:
        url, title = h.get("url", ""), h.get("title", "")
        meta = " · ".join(x for x in (_price(h.get("price")), h.get("location", ""),
                                      h.get("track_label", "")) if x)
        lines += [title, meta, url, ""]
        photo = h.get("photo") or ""
        img = (f'<a href="{escape(url)}"><img src="{escape(photo)}" alt="" '
               f'style="width:100%;max-width:480px;border-radius:8px;display:block"></a>'
               if photo.startswith("http") else "")
        blocks.append(
            f'<div style="margin:0 0 28px 0">{img}'
            f'<p style="margin:10px 0 2px 0;font-size:17px;font-weight:600">'
            f'<a href="{escape(url)}" style="color:#111;text-decoration:none">{escape(title)}</a></p>'
            f'<p style="margin:0 0 6px 0;color:#555">{escape(meta)}</p>'
            f'<p style="margin:0"><a href="{escape(url)}">Ver anuncio</a></p></div>')

    html = (f'<div style="font-family:-apple-system,Helvetica,Arial,sans-serif;max-width:520px">'
            f'<p style="font-size:14px;color:#555">{n} casa{plural} nueva{plural} en el Top 5 '
            f'de hoy. <a href="{DASHBOARD}">Panel</a></p>{"".join(blocks)}</div>')
    return {
        "to": [mailbox],
        "subject": f"Home Quest: {n} casa{plural} nueva{plural} ({today})",
        "text": "\n".join(lines),
        "html": html,
    }


# ------------------------------------------------------------------ config

def load_env(env_file: Optional[Path] = None) -> dict:
    """KEY=value pairs from the gitignored .env (# comments, optional quotes)."""
    env_file = env_file or ENV_FILE
    out = {}
    if not env_file.exists():
        return out
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"\'')
    return out


def mcp_server_config(settings_file: Optional[Path] = None) -> Optional[dict]:
    """The `gmail-personal` launch spec (command/args/env) from Claude's settings."""
    settings_file = settings_file or SETTINGS_FILE
    try:
        return json.loads(settings_file.read_text())["mcpServers"][MCP_SERVER]
    except (OSError, KeyError, json.JSONDecodeError):
        return None


# ------------------------------------------------------------------ send

class McpError(Exception):
    pass


def _rpc_exchange(proc: subprocess.Popen, payload: dict, user_id: str) -> str:
    def call(obj):
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def reply(id_):
        while True:
            line = proc.stdout.readline()
            if not line:
                raise McpError("server closed the pipe")
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue  # non-protocol chatter on stdout
            if obj.get("id") == id_:
                if "error" in obj:
                    raise McpError(obj["error"].get("message", str(obj["error"])))
                return obj["result"]

    call({"jsonrpc": "2.0", "id": 1, "method": "initialize",
          "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                     "clientInfo": {"name": "home-quest-qh", "version": "1"}}})
    reply(1)
    call({"jsonrpc": "2.0", "method": "notifications/initialized"})
    call({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
          "params": {"name": "gmail_send_email",
                     "arguments": dict(payload, userId=user_id)}})
    result = reply(2)
    text = " ".join(c.get("text", "") for c in result.get("content", [])
                    if c.get("type") == "text")
    if result.get("isError") or text.lstrip().startswith("❌"):
        raise McpError(text[:300] or "tool reported an error")
    return text


def send(payload: dict, user_id: str, server_cfg: dict, timeout: int = MCP_TIMEOUT) -> str:
    """Spawn the Gmail MCP server and call gmail_send_email once. Raises on
    any failure; the whole exchange is bounded by `timeout`."""
    import os
    proc = subprocess.Popen(
        [server_cfg["command"], *server_cfg.get("args", [])],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env={**os.environ, **server_cfg.get("env", {})}, text=True,
    )
    box: dict = {}

    def worker():
        try:
            box["ok"] = _rpc_exchange(proc, payload, user_id)
        except Exception as exc:  # noqa: BLE001 — surfaced below
            box["err"] = exc

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout)
    try:
        proc.stdin.close()
    except OSError:
        pass
    proc.kill()
    if t.is_alive():
        raise McpError(f"no answer from {MCP_SERVER} within {timeout}s")
    if "err" in box:
        raise box["err"]
    return box["ok"]


# ------------------------------------------------------------------ main

def run(state_dir: Path, track_cfg: dict, today: str, *,
        env: Optional[dict] = None,
        server_cfg: Optional[dict] = None,
        sender: Callable[[dict, str, dict], str] = send) -> int:
    notified = state_dir / f"notified-{today}.json"
    if notified.exists():
        print("[notify] already emailed today; skipping.")
        clear_markers(state_dir, track_cfg)
        return 0

    houses = collect(state_dir, track_cfg, today)
    if not houses:
        print("[notify] no first-time winners today.")
        clear_markers(state_dir, track_cfg)
        return 0

    env = load_env() if env is None else env
    mailbox, user_id = env.get("GMAIL_MAILBOX"), env.get("GMAIL_MCP_USER_ID")
    if not (mailbox and user_id):
        print(f"[notify] GMAIL_MAILBOX / GMAIL_MCP_USER_ID missing from {ENV_FILE.name} — "
              f"{len(houses)} new house(s) NOT emailed.", file=sys.stderr)
        return 0
    server_cfg = server_cfg or mcp_server_config()
    if not server_cfg:
        print(f"[notify] no `{MCP_SERVER}` server in {SETTINGS_FILE} — "
              f"{len(houses)} new house(s) NOT emailed.", file=sys.stderr)
        return 0

    payload = build_message(houses, today, mailbox)
    try:
        sender(payload, user_id, server_cfg)
    except Exception as exc:  # noqa: BLE001 — never block the publish
        print(f"[notify] email failed ({type(exc).__name__}: {exc}); will not retry.",
              file=sys.stderr)
        return 0

    notified.write_text(json.dumps([h["id"] for h in houses]))
    clear_markers(state_dir, track_cfg)
    print(f"[notify] emailed {len(houses)} new house(s) to {mailbox}: "
          + ", ".join(h["id"] for h in houses))
    return 0


if __name__ == "__main__":
    sys.exit(run(STATE_DIR, tracks.TRACKS, str(date.today())))
