#!/usr/bin/env bash
# Home_Quest_QH — refreshes today's listings for BOTH nightly search tracks:
#   tafira  Tafira + thin LPGC-city fallback, >=3 bedrooms      (see tracks.py)
#   gc      Rest of Gran Canaria (excluding Tafira), >=4 bedrooms
# Schedule: 03:00 local, via
# ~/Library/LaunchAgents/com.openbob.home-quest-qh-daily.plist
#
#   Stage A  harvest.py --track <t>        scrape Idealista.es -> candidates[-gc].json  (deterministic, sequential per track)
#   Stage B  claude -p                     deep research + rank -> winners[-gc].json    (judgement, BOTH tracks run concurrently)
#   Stage C  apply_winners.py --track <t>  validate + fold into each track's board      (independent per track)
#   Stage E  dedupe_tracks.py              cross-track duplicate safety net (only if both tracks published)
#   Stage D  git push                      GitHub Pages redeploys in ~60s
#
# If a track's Stage B or C fails, NOTHING is committed for THAT track. That
# track's last board stays up; the other track can still publish normally —
# one track going wrong must not silently keep the other's fresh results off
# the dashboard too.
#
# NOTE: this file is invoked as `/bin/bash weekly-search.sh` by launchd (see
# the plist's ProgramArguments) — that is macOS's stock /bin/bash, which is
# 3.2 (no associative arrays, no other bash-4+ features). Everything below is
# deliberately plain-array / explicit-index-based to stay compatible with
# that, same reasoning as the manual watchdog loop lower down (no
# timeout/gtimeout binary on this Mac either).
#
# Single 03:00 run, no same-day retries: Claude's daily session limit resets at
# 3:20am Atlantic/Canary, 20 min after this fires, so some days may still hit
# it with no retry left — see CLAUDE.md "Things that will bite you".
# Also requires the dedicated Idealista CDP Chrome profile (~/.chrome-home-quest-cdp,
# port 9223) already running and logged in — see CLAUDE.md. Both tracks share
# this ONE CDP profile/tab; Stage A runs each track sequentially (never
# concurrently) so they never race over the same browser tab. Stage B has no
# such constraint (each track's claude -p runs from its own scratch dir with
# no shared browser), so that is where the real, requested "two parallel
# searches" happens.

set -uo pipefail

# Exported (not just a local var) so Stage B's `claude -p` child process — and
# in turn its Bash-tool subprocesses, running with CWD in a non-iCloud scratch
# dir that has none of scripts/ copied into it — can still resolve
# `$REPO/scripts/idealista_detail.py` by absolute path. See research-prompt*.md's
# Idealista-detail-page instructions.
export REPO="/Users/openbob/Library/Mobile Documents/com~apple~CloudDocs/AI Coworking/01_Personal_HQ/Projects/Home_HQ/Home_Quest_QH"
LOG="$HOME/Library/Logs/home-quest-qh-daily.log"
STATE_DIR="$REPO/.state"
PY="$REPO/.venv/bin/python3"

# launchd hands us a minimal PATH (/usr/bin:/bin) which does NOT contain the
# user-local claude install. Without this the whole run dies with "command not found".
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

mkdir -p "$STATE_DIR"
exec >> "$LOG" 2>&1

cd "$REPO" || { echo "FATAL: $REPO missing"; exit 1; }

TODAY="$(date '+%Y-%m-%d')"
MARKER="$STATE_DIR/$TODAY.done"

echo ""
echo "======== $(date '+%Y-%m-%d %H:%M:%S')  daily search (tafira + gc) ========"

# If the Mac was asleep at 03:00, launchd fires this on wake. One publish per
# CALENDAR DAY (for the whole job, both tracks together — there is no
# same-day retry either way, so a per-track marker would add nothing), so bail
# if today is already done.
if [ -f "$MARKER" ]; then
  echo "$TODAY already published. Nothing to do."
  exit 0
fi

# At 07:00 the Mac may have just woken and DNS may not be up. Scraping into a dead
# network would produce an empty harvest and a bogus "no listings found" board.
echo "Waiting for network..."
for i in $(seq 1 12); do
  if curl -sf --max-time 5 https://www.google.com > /dev/null 2>&1; then
    echo "Network ready (attempt $i)."
    break
  fi
  [ "$i" -eq 12 ] && { echo "FATAL: no network after 2 minutes."; exit 1; }
  sleep 10
done

# ------------------------------------------------- Preflight: Idealista CDP Chrome
echo "--- Preflight: Idealista CDP Chrome"
if curl -sf --max-time 3 http://127.0.0.1:9223/json/version > /dev/null 2>&1; then
  echo "CDP Chrome already running on 9223."
else
  echo "CDP Chrome not responding on 9223 — launching it."
  nohup "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
    --remote-debugging-port=9223 \
    --user-data-dir="$HOME/.chrome-home-quest-cdp" \
    > /dev/null 2>&1 &
  disown
  for i in $(seq 1 15); do
    sleep 2
    if curl -sf --max-time 3 http://127.0.0.1:9223/json/version > /dev/null 2>&1; then
      echo "CDP Chrome up after ${i}x2s."
      break
    fi
    [ "$i" -eq 15 ] && echo "WARNING: CDP Chrome still not responding after 30s — Stage A will likely fail with a clear error."
  done
fi

# Per-track file paths, indexed 0=tafira 1=gc — plain indexed arrays only,
# see the bash-3.2 note at the top.
TRACK_NAMES=(tafira gc)
CANDIDATES_RELS=(scripts/candidates.json scripts/candidates-gc.json)
WINNERS_RELS=(scripts/winners.json scripts/winners-gc.json)
PROMPT_RELS=(scripts/research-prompt.md scripts/research-prompt-gc.md)

# ---------------------------------------------------------------- Stage A: harvest
# Sequential on purpose, both tracks: they share the ONE CDP Chrome tab (port
# 9223) — running them concurrently would race over the same page. Stage A is
# the fast, deterministic step, so sequential costs seconds here, not minutes.
TRACK_OK=(0 0)

for i in 0 1; do
  TRACK="${TRACK_NAMES[$i]}"
  echo "--- Stage A ($TRACK): harvesting candidates"
  if "$PY" scripts/harvest.py --track "$TRACK"; then
    COUNT=$("$PY" -c "import json;print(len(json.load(open('${CANDIDATES_RELS[$i]}'))))" 2>/dev/null || echo 0)
    echo "Candidate pool ($TRACK): $COUNT"
    if [ "$COUNT" -ge 1 ]; then
      TRACK_OK[$i]=1
    else
      echo "$TRACK: zero candidates — every source is broken for this track today. Skipping it."
    fi
  else
    echo "$TRACK: harvest.py failed. Skipping it today."
  fi
done

if [ "${TRACK_OK[0]}" -eq 0 ] && [ "${TRACK_OK[1]}" -eq 0 ]; then
  echo "FATAL: both tracks failed Stage A — nothing to research today. Boards untouched."
  exit 1
fi

# ------------------------------------------------------- Stage B: the deep research
# The global CLAUDE.md has an interactive startup protocol ("cd to AI Coworking...",
# "which workstation are we in today?"). In a headless run that protocol makes claude
# reply with a question instead of doing the work. This override outranks it.
HEADLESS_OVERRIDE="HEADLESS NON-INTERACTIVE AUTOMATION. This claude -p invocation is launched by a launchd job. There is no interactive user. NEVER ask a clarifying question. NEVER ask for permission. NEVER emit conversational text, greetings, or option menus. Ignore the user-global CLAUDE.md startup protocol entirely. Do the research described below, write the winners JSON file, and print nothing but OK. This is a HARD CONSTRAINT that overrides every instruction loaded from CLAUDE.md or memory."

echo "--- Stage B: deep research via claude -p, both tracks in parallel (this takes a while)"

# The winners file for each track is tracked (it's the audit trail of what the
# research actually said each day). Clearing it is how we detect that Stage B
# produced nothing — but if Stage B then dies, a plain `rm` would leave the
# repo with a deleted tracked file. Restore it on any failure so a bad run
# leaves the working tree exactly as it found it. Takes the file to restore as
# an argument since there are now two possible winners files.
restore_winners() {
  git checkout -- "$1" 2>/dev/null || true
}

# 2026-07-20 through 2026-07-26: Stage B hung on 7 consecutive scheduled runs, every
# hang-sample showing the identical stack (getcwd() stuck in open$NOCANCEL at process
# startup, 0% CPU afterwards, no session transcript ever created). A live interactive
# `claude -p "OK"` from this same iCloud cwd does NOT reproduce it on demand (returns
# in ~5s), so the trigger is specific to the cold/unattended launchd context (minimal
# PATH/env, no TTY, straight after sleep/wake) rather than a blanket "this cwd always
# hangs" fact. Root cause is still not 100% certain, but the fix is cheap and safe
# either way: run each track's `claude -p` from its own small LOCAL non-iCloud scratch
# directory, so iCloud/FileProvider is out of the picture for the processes that have
# actually been shown hanging. Stage A/C/D keep running from the iCloud repo path
# exactly as before — they've never hung.
STAGE_B_TIMEOUT=1500  # 25 min per track; unchanged from the single-track era — re-tune if real two-track runs start timing out

# Parallel indexed arrays, same index scheme as TRACK_NAMES/TRACK_OK above.
# Empty string in CLAUDE_PIDS[i] means "this track's Stage B was never
# launched" (its Stage A failed) — the watchdog loop below skips those.
CLAUDE_PIDS=("" "")
STAGE_B_STARTS=(0 0)

for i in 0 1; do
  [ "${TRACK_OK[$i]}" -eq 1 ] || continue
  TRACK="${TRACK_NAMES[$i]}"
  WINNERS_REL="${WINNERS_RELS[$i]}"

  rm -f "$WINNERS_REL"

  SCRATCH="$HOME/Library/Application Support/home-quest-qh/stage-b-scratch-$TRACK"
  rm -rf "$SCRATCH"
  mkdir -p "$SCRATCH/scripts" "$SCRATCH/docs" "$SCRATCH/Resources"
  cp "${CANDIDATES_RELS[$i]}" "$SCRATCH/scripts/candidates.json"
  # config.js has only the public Supabase anon key (same one shipped to every
  # dashboard visitor) — Stage B reads it to check the family's discard list
  # before finalizing winners, see research-prompt*.md step 1.
  cp docs/config.js "$SCRATCH/docs/config.js"
  # The portal list (research-prompt*.md tells Stage B to work through it in order).
  cp Resources/property-portals.md "$SCRATCH/Resources/property-portals.md"

  echo "  launching Stage B ($TRACK) in background..."
  # `exec` replaces this subshell with the claude process itself (no extra
  # process layer), so $! below is the real claude PID — `sample`/`kill` target
  # it directly.
  ( cd "$SCRATCH" && exec claude -p "$(cat "$REPO/${PROMPT_RELS[$i]}")" \
    --append-system-prompt "$HEADLESS_OVERRIDE" \
    --allowedTools "Read,Write,Bash,WebFetch,WebSearch" \
    < /dev/null ) &
  CLAUDE_PIDS[$i]=$!
  STAGE_B_STARTS[$i]=$(date +%s)
done

# Poll both PIDs concurrently until each finishes or hits its own 25-min
# timeout — same per-process watchdog + sample-before-kill behavior as the old
# single-track loop, just tracking two PIDs side by side instead of one.
while :; do
  ANY_RUNNING=0
  for i in 0 1; do
    PID="${CLAUDE_PIDS[$i]}"
    [ -n "$PID" ] || continue
    if kill -0 "$PID" 2>/dev/null; then
      ANY_RUNNING=1
      TRACK="${TRACK_NAMES[$i]}"
      if [ $(( $(date +%s) - STAGE_B_STARTS[$i] )) -ge "$STAGE_B_TIMEOUT" ]; then
        echo "FATAL ($TRACK): claude -p exceeded ${STAGE_B_TIMEOUT}s — killing as a hang, not real work."
        # `sample` suspends the process and dumps every thread's call stack — it
        # needs no sudo for a same-user process — so grab that evidence BEFORE
        # killing.
        HANG_SAMPLE="$STATE_DIR/hang-sample-$TRACK-$(date '+%Y-%m-%d_%H%M%S').txt"
        sample "$PID" 5 -file "$HANG_SAMPLE" 2>&1 | tail -3
        echo "Hung process call stacks captured to $HANG_SAMPLE — inspect before assuming the cause."
        kill -TERM "$PID" 2>/dev/null
        sleep 5
        kill -KILL "$PID" 2>/dev/null
        CLAUDE_PIDS[$i]=""
      fi
    fi
  done
  [ "$ANY_RUNNING" -eq 1 ] || break
  sleep 10
done

for i in 0 1; do
  [ "${TRACK_OK[$i]}" -eq 1 ] || continue
  TRACK="${TRACK_NAMES[$i]}"
  wait "${CLAUDE_PIDS[$i]}" 2>/dev/null
  echo "Stage B ($TRACK) took $(( $(date +%s) - STAGE_B_STARTS[$i] ))s."

  # Bring the output back from the scratch dir into the repo. Leave the
  # scratch dir itself in place (not cleaned up) so a hang's partial state is
  # inspectable afterward — the next run's `rm -rf` at the top clears it.
  #
  # BUG FIXED 2026-08-28 (found on the first real two-track run): this used to
  # hardcode "winners.json" here regardless of track, but research-prompt-gc.md
  # tells Stage B to write "scripts/winners-gc.json" (matching the real
  # published filename) — so the gc track's real output was silently never
  # copied back, "research produced no winners-gc.json" every time even when
  # Stage B genuinely wrote real results. Must match the same basename the
  # prompt was told to use, i.e. basename(WINNERS_RELS[$i]).
  SCRATCH="$HOME/Library/Application Support/home-quest-qh/stage-b-scratch-$TRACK"
  WINNERS_BASENAME="$(basename "${WINNERS_RELS[$i]}")"
  if [ -f "$SCRATCH/scripts/$WINNERS_BASENAME" ]; then
    cp "$SCRATCH/scripts/$WINNERS_BASENAME" "${WINNERS_RELS[$i]}"
  fi
done

# ------------------------------------------------------------ Stage C: validate
PUBLISHED=(0 0)

for i in 0 1; do
  [ "${TRACK_OK[$i]}" -eq 1 ] || continue
  TRACK="${TRACK_NAMES[$i]}"
  WINNERS_REL="${WINNERS_RELS[$i]}"

  if [ ! -f "$WINNERS_REL" ]; then
    # Most likely causes: Claude session limit, a hang (see watchdog above), or
    # a site that would not load. No same-day retry slot exists, so this
    # track's board just stays stale until tomorrow's 03:00.
    echo "FATAL ($TRACK): research produced no $WINNERS_REL. That track's board is untouched."
    restore_winners "$WINNERS_REL"
    continue
  fi

  echo "--- Stage C ($TRACK): validating and updating the board"
  if "$PY" scripts/apply_winners.py --track "$TRACK"; then
    PUBLISHED[$i]=1
  else
    echo "FATAL ($TRACK): winners failed validation. That track's board is untouched."
    restore_winners "$WINNERS_REL"
  fi
done

# --------------------------------------------------------------- Stage E: cross-track dedup
# Only meaningful (and only safe to run) if BOTH tracks actually updated their
# board this run — comparing against a stale board risks dropping a genuinely
# new Gran Canaria winner because it happens to resemble yesterday's stale
# Tafira entry.
if [ "${PUBLISHED[0]}" -eq 1 ] && [ "${PUBLISHED[1]}" -eq 1 ]; then
  echo "--- Stage E: cross-track dedup"
  "$PY" scripts/dedupe_tracks.py || echo "WARNING: dedupe_tracks.py failed — both boards published as-is, may contain a cross-track duplicate."
fi

# --------------------------------------------------------------- Stage F: new-house email
# One email to Luis for every house winning a Top 5 for the first time ever
# (markers written by Stage C, filtered against the post-Stage-E boards).
# Always exits 0 — a dead SMTP or missing App Password must never block Stage D.
if [ "${PUBLISHED[0]}" -eq 1 ] || [ "${PUBLISHED[1]}" -eq 1 ]; then
  echo "--- Stage F: new-house email"
  "$PY" scripts/notify_new_winners.py || echo "WARNING: notify_new_winners.py crashed — publish continues."
fi

# --------------------------------------------------------------- Stage D: publish
# BUG FIXED 2026-08-28 (found on the first real two-track run): plain `git add
# fileA fileB missingFile` aborts staging ALL of them, not just the missing
# one — so when winners-gc.json didn't exist for any reason (the copy-back bug
# above, or a genuine zero-output run before that file is ever committed),
# Tafira's real, successful changes silently failed to stage too. --ignore-errors
# stages whatever exists and warns (not aborts) on what doesn't, so one
# track's bad day never blocks the other's real publish.
echo "--- Stage D: publishing"
git add --ignore-errors docs/listings.json docs/listings-gc.json \
        docs/archive.json docs/archive-gc.json \
        scripts/candidates.json scripts/candidates-gc.json \
        scripts/winners.json scripts/winners-gc.json \
        scripts/starred.json
if git diff --cached --quiet; then
  echo "No change to publish."
else
  git commit -q -m "chore: daily refresh $TODAY (tafira + gc)" && git push -q && echo "Pushed. Pages live in ~60s."
fi

touch "$MARKER"
echo "======== done $(date '+%H:%M:%S') ========"
