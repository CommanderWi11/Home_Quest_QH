#!/usr/bin/env python3
"""Stage C: validate one track's winners and fold them into that track's board.

This is the gate between a non-deterministic model and the family's live dashboard.
`claude -p` is good, but it is not a database — so nothing it produces reaches
a track's listings.json until it has passed every check below. If validation
fails we exit non-zero and weekly-search.sh refuses to commit, leaving that
track's last board untouched. A stale board beats a corrupted one.

Run per track (`--track tafira` or `--track gc`, see tracks.py) — every file
path below is reassigned in main() from tracks.TRACKS[args.track], including
harvest.CANDIDATES_FILE / harvest.LISTINGS_FILE, since load_candidates() /
load_listings() (imported from harvest.py) resolve those paths from harvest's
own module globals, not this module's.
"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import archive
import board
import harvest
import notify_new_winners
import tracks
from harvest import (
    fetch_og_image, load_blocklist, load_candidates, load_listings,
    load_starred, make_id, same_house,
)

WINNERS_FILE = tracks.TRACKS[tracks.DEFAULT_TRACK]["winners_file"]
LISTINGS_FILE = tracks.TRACKS[tracks.DEFAULT_TRACK]["listings_file"]
HISTORY_FILE = Path(__file__).parent.parent / "docs" / "history.json"
MAX_WINNERS = 5


class Invalid(Exception):
    """The model's output cannot be trusted. Abort rather than publish it."""


def blocked_listings(blocked: set[str]) -> list:
    """Full dicts (title/specs/source) for every discarded id we have a record of,
    drawn from everywhere the family could have discarded it: the board, the
    harvester's own candidate pool, and manually-ingested history snapshots.

    Used with same_house() below for CROSS-SOURCE matching — a discard on one
    portal (e.g. idealista) must also catch Stage B relisting the identical
    house under a different portal (e.g. fotocasa), which has a different id
    (id = md5 of URL) and would never match on id alone.
    """
    known = load_candidates() + load_listings()
    if HISTORY_FILE.exists():
        for snapshot in json.loads(HISTORY_FILE.read_text()):
            known.extend(snapshot.get("entries", []))
    return [l for l in known if l.get("id") in blocked]


def validate(raw: object, blocked: set[str], min_bedrooms: int = 3) -> list:
    if not isinstance(raw, list):
        raise Invalid(f"expected a JSON list of winners, got {type(raw).__name__}")
    if len(raw) > MAX_WINNERS:
        raise Invalid(f"{len(raw)} winners — the board takes at most {MAX_WINNERS}")

    blocked_houses = blocked_listings(blocked)

    seen_ids: set[str] = set()
    winners = []  # survivors, original Stage B rank still attached — renumbered below
    dropped = 0

    for i, w in enumerate(raw):
        if not isinstance(w, dict):
            raise Invalid(f"winner #{i} is not an object")

        url = (w.get("url") or "").strip()
        source = (w.get("source") or "").strip()
        if not url.startswith("http"):
            raise Invalid(f"winner #{i} has no usable url: {url!r}")
        if not source:
            raise Invalid(f"winner #{i} has no source")

        # Reuse the harvested id when present so the family's stars and comments —
        # which are keyed on it in Supabase — survive. Otherwise derive it the same
        # way the harvester does, so a listing Claude found itself is addressable.
        wid = (w.get("id") or "").strip() or make_id(source, url)
        if wid in seen_ids:
            raise Invalid(f"duplicate winner id {wid}")

        # A discarded house reappearing is NOT a trust problem with Stage B's
        # output the way a malformed field is — research-prompt.md tells Stage B
        # to check this itself, but that's prompt-following, not a guarantee. A
        # relist under a brand-new id (a different portal, or the same listing
        # re-posted) would never match on id alone, which is why this also checks
        # same_house() against every known blocked listing, not just exact id
        # equality. Dropping it here and continuing means one bad entry costs a
        # rank slot, not the whole Stage B run and the day's board update.
        relisted_match = next((bv for bv in blocked_houses if same_house(w, bv)), None)
        if wid in blocked or relisted_match:
            reason = wid if wid in blocked else f"same house as blocked {relisted_match['id']}"
            print(f"  dropping {wid or '(no id)'} — discarded by the family "
                  f"({reason}), Stage B should not have re-included it", file=sys.stderr)
            dropped += 1
            continue

        for prev in winners:
            if same_house(w, prev):
                raise Invalid(
                    f"winner {wid} and winner {prev['id']} look like the same "
                    f"house from two different sources — the research pass "
                    f"ranked one house as two separate winners"
                )
        seen_ids.add(wid)

        rank = w.get("rank")
        if not isinstance(rank, int) or not 1 <= rank <= MAX_WINNERS:
            raise Invalid(f"winner {wid} has rank {rank!r}, expected 1..{MAX_WINNERS}")

        score = w.get("score")
        if not isinstance(score, (int, float)) or not 0 <= score <= 100:
            raise Invalid(f"winner {wid} has score {score!r}, expected 0..100")

        verdict = (w.get("verdict") or "").strip()
        if len(verdict) < 20:
            raise Invalid(f"winner {wid} has no real verdict ({verdict!r})")

        w = dict(w)
        w["id"] = wid
        w.setdefault("flags", [])
        w.setdefault("specs", {})

        # Budget, bedroom count, and a private garden are hard requirements
        # (project CLAUDE.md) — machine-checked invariants like rank/score, not
        # just prompt-trust. Stage B is told to enforce these itself, but that's
        # prompt-following, not a guarantee, so Stage C re-checks them here.
        price = w.get("price")
        if not isinstance(price, (int, float)) or price > 500000:
            raise Invalid(f"winner {wid} has price {price!r}, expected a number <=500000")

        bedrooms = w["specs"].get("bedrooms")
        if not isinstance(bedrooms, int) or bedrooms < min_bedrooms:
            raise Invalid(f"winner {wid} has {bedrooms!r} bedrooms, expected >={min_bedrooms}")

        if w["specs"].get("has_garden") is not True:
            raise Invalid(f"winner {wid} has no private garden — hard requirement is a garden")

        winners.append(w)

    # Original ranks must always be unique. If nothing was dropped above, they
    # must also be consecutive from 1 — that's a real Stage B numbering mistake.
    # A gap caused BY a drop (e.g. {1,2,4,5} after rank 3 gets dropped) is
    # expected and fine — sort by Stage B's original rank to preserve its
    # relative ordering, then renumber 1..n so the published board has no gaps.
    orig_ranks = [w["rank"] for w in winners]
    if len(set(orig_ranks)) != len(orig_ranks):
        raise Invalid(f"rank assigned twice: {sorted(orig_ranks)}")
    if not dropped and orig_ranks and sorted(orig_ranks) != list(range(1, len(winners) + 1)):
        raise Invalid(f"ranks are not consecutive from 1: {sorted(orig_ranks)}")
    winners.sort(key=lambda w: w["rank"])
    for idx, w in enumerate(winners, start=1):
        w["rank"] = idx

    return winners


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Stage C: validate one track's winners.json and update its board."
    )
    p.add_argument("--track", choices=sorted(tracks.TRACKS), default=tracks.DEFAULT_TRACK,
                    help="Which search track to validate/publish (default: %(default)s)")
    return p.parse_args()


def main() -> int:
    global WINNERS_FILE, LISTINGS_FILE

    args = _parse_args()
    track = tracks.TRACKS[args.track]
    WINNERS_FILE = track["winners_file"]
    LISTINGS_FILE = track["listings_file"]
    # load_candidates()/load_listings() (imported from harvest.py) resolve
    # their paths from harvest's OWN module globals, not this module's names —
    # a plain `from harvest import X` binds X at import time, it does not stay
    # live. Setting these module attributes is what actually makes
    # blocked_listings() / board.update_board()'s `load_listings()` call below
    # track-scoped.
    harvest.CANDIDATES_FILE = track["candidates_file"]
    harvest.LISTINGS_FILE = track["listings_file"]

    if not WINNERS_FILE.exists():
        print(f"ERROR: {WINNERS_FILE} was never written — the research step failed.",
              file=sys.stderr)
        return 1

    try:
        raw = json.loads(WINNERS_FILE.read_text())
    except json.JSONDecodeError as exc:
        print(f"ERROR: {WINNERS_FILE.name} is not valid JSON ({exc}).", file=sys.stderr)
        return 1

    blocked = load_blocklist()

    try:
        winners = validate(raw, blocked, min_bedrooms=track["min_bedrooms_gate"])
    except Invalid as exc:
        print(f"ERROR: refusing to publish — {exc}", file=sys.stderr)
        return 1

    if not winners:
        print("No winners this week; leaving the board as it is.")
        return 0

    # Every winner reaches the board with a real photo. A card whose source dropped
    # the image (lazy-load placeholder, price-on-request page) gets its og:image
    # pulled from the detail page — at most MAX_WINNERS fetches, best-effort.
    for w in winners:
        if not (w.get("photo") or "").startswith("http"):
            og = fetch_og_image(w.get("url", ""))
            if og:
                w["photo"] = og
                print(f"  backfilled photo for {w['id']} <- og:image")

    starred = load_starred()
    updated = board.update_board(
        load_listings(), winners, starred_ids=set(starred), blocked_ids=blocked,
    )
    LISTINGS_FILE.write_text(json.dumps(updated, ensure_ascii=False, indent=2))

    # "Previous searches" archive — today's real winners, kept forever (unlike
    # listings.json, which only ever holds today's Top 5 + Favorites). The
    # dashboard dedups this against Top 5/Favoritos/older dates client-side
    # (see docs/history-dedup.js); nothing here needs to be deduped or pruned.
    today = str(date.today())
    new_ids = archive.append_snapshot(track["archive_file"], today, winners)

    # First-time-ever winners -> marker for notify_new_winners.py, which runs
    # after Stage E (so a cross-track dedup drop never gets announced).
    notify_new_winners.write_marker(
        notify_new_winners.STATE_DIR, args.track, today,
        [w for w in winners if w["id"] in new_ids],
    )

    favorites = sum(1 for l in updated if not l.get("rank"))
    print(f"Board updated:")
    for w in sorted(winners, key=lambda x: x["rank"]):
        flags = f"  ⚠ {len(w['flags'])} flag(s)" if w["flags"] else ""
        print(f"  #{w['rank']}  {w['score']:>3}  {w['title'][:52]}{flags}")
    print(f"{len(updated)} listings on the board ({len(winners)} in today's Top 5, "
          f"{favorites} favorite(s)).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
