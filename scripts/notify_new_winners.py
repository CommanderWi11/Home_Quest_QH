#!/usr/bin/env python3
"""Email the family when a house wins a track's Top 5 for the FIRST time ever.

Two halves:

  * Stage C (apply_winners.py) calls write_marker() with the winners whose id
    no earlier day's archive snapshot mentions (archive.append_snapshot()'s
    return value) -> .state/new-winners-<track>.json
  * weekly-search.sh runs this script once, after Stage E (cross-track dedup)
    and before Stage D (push). It reads both markers, keeps only the houses
    still on the final boards, and sends ONE email with each house's front
    photo (inline, fetched now; falls back to a linked image) and a direct
    link to the ad.

Idempotent per calendar day (.state/notified-<date>.json), stale markers from
another day are ignored, and nothing here may ever block the publish: every
failure path logs and exits 0.

Config (the repo is public, so neither lives in code):
  * mailbox (sender == recipient): GMAIL_MAILBOX=... in the gitignored .env
  * Gmail App Password: read at send time from the automation keychain —
    `cred get home-quest-qh GMAIL_APP_PASSWORD` — never logged.
Without either, the notifier says so and skips.
"""
import json
import subprocess
import sys
from datetime import date
from email.message import EmailMessage
from email.utils import make_msgid
from html import escape
from pathlib import Path
from typing import Callable, Optional

import requests

import tracks

STATE_DIR = Path(__file__).parent.parent / ".state"
ENV_FILE = Path(__file__).parent.parent / ".env"
CRED = Path.home() / ".config" / "ai-coworking" / "cred"
CRED_SERVICE, CRED_ACCOUNT = "home-quest-qh", "GMAIL_APP_PASSWORD"
SMTP_HOST, SMTP_PORT = "smtp.gmail.com", 465
DASHBOARD = "https://commanderwi11.github.io/Home_Quest_QH/"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}


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


def build_message(houses: list, photos: dict, today: str, mailbox: str) -> EmailMessage:
    """One email for all of today's first-time winners. `photos` maps id ->
    image bytes already fetched (inline, cid:); a missing entry falls back to
    an <img> pointing at the portal's own photo URL."""
    n = len(houses)
    msg = EmailMessage()
    msg["Subject"] = f"Home Quest: {n} casa{'s' if n != 1 else ''} nueva{'s' if n != 1 else ''} ({today})"
    msg["From"] = mailbox
    msg["To"] = mailbox

    lines = [f"Home Quest {today}: {n} casa(s) nueva(s) en el Top 5", ""]
    blocks = []
    inline = []  # (cid, bytes, subtype)
    for h in houses:
        url, title = h.get("url", ""), h.get("title", "")
        meta = " · ".join(x for x in (_price(h.get("price")), h.get("location", ""),
                                      h.get("track_label", "")) if x)
        lines += [title, meta, url, ""]

        data = photos.get(h.get("id"))
        if data:
            cid = make_msgid(domain="home-quest-qh")
            inline.append((cid, data, _image_subtype(data)))
            src = f"cid:{cid[1:-1]}"
        else:
            src = h.get("photo") or ""
        img = (f'<a href="{escape(url)}"><img src="{escape(src)}" alt="" '
               f'style="width:100%;max-width:480px;border-radius:8px;display:block"></a>'
               if src else "")
        blocks.append(
            f'<div style="margin:0 0 28px 0">{img}'
            f'<p style="margin:10px 0 2px 0;font-size:17px;font-weight:600">'
            f'<a href="{escape(url)}" style="color:#111;text-decoration:none">{escape(title)}</a></p>'
            f'<p style="margin:0 0 6px 0;color:#555">{escape(meta)}</p>'
            f'<p style="margin:0"><a href="{escape(url)}">Ver anuncio</a></p></div>')

    html = (f'<div style="font-family:-apple-system,Helvetica,Arial,sans-serif;max-width:520px">'
            f'<p style="font-size:14px;color:#555">{n} casa{"s" if n != 1 else ""} nueva'
            f'{"s" if n != 1 else ""} en el Top 5 de hoy. '
            f'<a href="{DASHBOARD}">Panel</a></p>{"".join(blocks)}</div>')

    msg.set_content("\n".join(lines))
    msg.add_alternative(html, subtype="html")
    for cid, data, subtype in inline:
        msg.get_payload()[1].add_related(data, maintype="image", subtype=subtype, cid=cid)
    return msg


def _image_subtype(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return "jpeg"


# ------------------------------------------------------------------ I/O

def fetch_photo(url: str) -> Optional[bytes]:
    if not (url or "").startswith("http"):
        return None
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.ok and r.headers.get("content-type", "").startswith("image/") and r.content:
            return r.content
    except requests.RequestException:
        pass
    return None


def load_mailbox(env_file: Optional[Path] = None) -> Optional[str]:
    """GMAIL_MAILBOX from the gitignored .env (KEY=value lines, # comments)."""
    env_file = env_file or ENV_FILE
    if not env_file.exists():
        return None
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line.startswith("GMAIL_MAILBOX="):
            return line.split("=", 1)[1].strip().strip('"\'') or None
    return None


def get_password() -> Optional[str]:
    if not CRED.exists():
        return None
    try:
        r = subprocess.run([str(CRED), "get", CRED_SERVICE, CRED_ACCOUNT],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    pw = r.stdout.strip()
    return pw if r.returncode == 0 and pw else None


def send(msg: EmailMessage, password: str) -> None:
    import smtplib
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as s:
        s.login(msg["From"], password)
        s.send_message(msg)


# ------------------------------------------------------------------ main

def run(state_dir: Path, track_cfg: dict, today: str, *,
        mailbox: Optional[str] = None,
        password_getter: Callable[[], Optional[str]] = get_password,
        sender: Callable[[EmailMessage, str], None] = send,
        photo_getter: Callable[[str], Optional[bytes]] = fetch_photo) -> int:
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

    mailbox = mailbox or load_mailbox()
    if not mailbox:
        print(f"[notify] GMAIL_MAILBOX missing from {ENV_FILE.name} — "
              f"{len(houses)} new house(s) NOT emailed.", file=sys.stderr)
        return 0
    password = password_getter()
    if not password:
        print(f"[notify] no credential ({CRED_SERVICE}/{CRED_ACCOUNT} in the automation "
              f"keychain) — {len(houses)} new house(s) NOT emailed.", file=sys.stderr)
        return 0

    photos = {h["id"]: p for h in houses if (p := photo_getter(h.get("photo", "")))}
    msg = build_message(houses, photos, today, mailbox)
    try:
        sender(msg, password)
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
