#!/usr/bin/env python3
"""Process the paste-a-URL apply queue, then the daily batch (once per UTC day).

queue.json (repo root) is appended to by the Cloudflare worker when a URL is
pasted on the dashboard. Each entry: {url, dry_run, ts, status}. We apply the
pending ones oldest-first, mark them done/failed in place, and keep the last
50 entries as history. The daily batch runs only inside the 12:00-15:59 UTC
window, in short deadline-bounded slices (one per cron wake, batch_state.json
tracks the day), so a queue-triggered run at any other hour stays cheap.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
QUEUE = ROOT / 'queue.json'
BATCH_STATE = HERE / 'batch_state.json'
KEEP_HISTORY = 50

# kill switch: while this file exists at the repo root, no application is
# attempted (queue or batch). Sweeps/stats/scorecard in apply.yml still run.
# Delete the file to resume. Added while the delivery layer produces zero
# confirmed submits — running blind just contaminates the funnel data.
PAUSE = ROOT / 'PAUSE_SUBMITS'


def run(cmd):
    print(f'+ {" ".join(cmd)}', flush=True)
    return subprocess.run(cmd, cwd=HERE).returncode


def process_queue():
    if PAUSE.exists():
        print(f'PAUSED: {PAUSE.name} present — skipping queue', flush=True)
        return
    if not QUEUE.exists():
        return
    try:
        entries = json.loads(QUEUE.read_text() or '[]')
    except json.JSONDecodeError:
        print('queue.json unparseable — leaving as-is for inspection', flush=True)
        return
    pending = [e for e in entries if e.get('status') == 'pending']
    print(f'queue: {len(pending)} pending / {len(entries)} total', flush=True)
    for entry in pending:
        cmd = ['uv', 'run', 'python', 'applier.py', 'apply', entry['url']]
        if entry.get('dry_run'):
            cmd.append('--dry-run')
        code = run(cmd)
        entry['status'] = 'done' if code == 0 else 'failed'
        entry['exit'] = code
        entry['ran_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
        # persist after every item so a crash mid-queue doesn't redo finished ones
        QUEUE.write_text(json.dumps(entries[-KEEP_HISTORY:], indent=2) + '\n')


# The cloudbox codespace idle-stops 30 min after a wake with nobody
# attached (GitHub counts client activity, not background CPU). One long
# daily batch cannot fit: on 8/24 both the 13:00 and 15:00 wakes died inside
# `applier.py batch --limit 5` with no status and no log commit. So the day
# is split into several short wakes (worker crons at 12/13/14/15 UTC), each
# doing a few applications inside a hard wall-clock budget, until the day's
# submit target or run cap is met.
# 9/5: the box now lives 240 min per boot and cloud_runner.sh re-triggers
# the pipeline every hour inside the window, so a slice can be long and
# there are up to 8 of them. Volume target moves from 6/day to 15/day
# (Huntr: searches that end in an offer median 16/WEEK; 15/day in-lane is
# the ceiling before the 2-per-company/30d cap starts skipping everything).
# Codespaces free tier is 120 core-hours/month on a 2-core box (~2h/day), so
# the day is done in as few boots as possible: big slices, self-stop when the
# target is met (cloud_runner.sh), hourly retry only while short.
BATCH_LIMIT = 12           # applications attempted per slice
BATCH_DEADLINE_MIN = 55    # applier stops starting new apps past this
MAX_RUNS_PER_DAY = 9       # slices per day: every hour of the 12-20 UTC window (9/9: volume)
DAILY_SUBMIT_TARGET = 40   # stop waking the browser once this many went out (9/9: was 15)
WINDOW_UTC = (12, 20)      # 8am-4pm ET


def daily_batch() -> dict:
    """Run one slice if the day calls for it. Returns the wake summary fields
    process_queue owns: reason for stopping, whether scout/applier ran."""
    info = {'scout_ran': False, 'applier_ran': False, 'run_index': None}
    if PAUSE.exists():
        print(f'PAUSED: {PAUSE.name} present — skipping daily batch', flush=True)
        return {**info, 'reason': 'paused'}
    now = datetime.now(timezone.utc)
    if not (WINDOW_UTC[0] <= now.hour < WINDOW_UTC[1]):
        print(f'batch: outside window (hour={now.hour} UTC), skipping', flush=True)
        return {**info, 'reason': f'outside window (hour={now.hour} UTC)'}
    state = {}
    if BATCH_STATE.exists():
        try:
            state = json.loads(BATCH_STATE.read_text())
        except json.JSONDecodeError:
            pass
    today = now.date().isoformat()
    runs = state.get('runs', 0) if state.get('runs_date') == today else 0
    if state.get('last_batch') == today:
        print('batch: day already complete, skipping', flush=True)
        return {**info, 'reason': 'day already complete'}
    if runs >= MAX_RUNS_PER_DAY:
        print(f'batch: {runs} runs today already — leaving the rest for tomorrow', flush=True)
        return {**info, 'reason': f'run cap ({MAX_RUNS_PER_DAY}/day) reached'}
    done_before = submits_today(now)
    if done_before >= DAILY_SUBMIT_TARGET:
        state['last_batch'] = today
        BATCH_STATE.write_text(json.dumps(state, indent=2) + '\n')
        print(f'batch: {done_before} submits today already — day complete', flush=True)
        return {**info, 'reason': f'submit target ({DAILY_SUBMIT_TARGET}) already met'}
    info['run_index'] = runs + 1
    # 9/9: no human queue — CAPTCHA/account parks come back by themselves
    run(['uv', 'run', 'python', 'requeue.py'])
    # scout on the first slice AND whenever the on-deck queue is thin: an
    # empty wishlist cost a whole day (9/9: 4 slices, 0 attempts)
    if runs == 0:
        # new boards first, then scout reads them (9/9: 114 boards -> 1
        # wishlist row/day; discovery is the supply lever)
        run(['uv', 'run', 'python', 'discover_boards.py', '--max-verify', '150'])
        # BBB NJ employer sweep, phase 2: a few more websites -> careers -> ATS each day
        run(['uv', 'run', 'python', 'bbb_nj_resolve.py', '--max', '60'])
    if runs == 0 or wishlist_size() < BATCH_LIMIT * 2:
        run(['uv', 'run', 'python', 'scout.py'])
        info['scout_ran'] = True
    code = run(['uv', 'run', 'python', 'applier.py', 'batch',
                '--limit', str(BATCH_LIMIT), '--deadline-min', str(BATCH_DEADLINE_MIN)])
    info['applier_ran'] = True
    info['applier_exit'] = code
    state['runs_date'] = today
    state['runs'] = runs + 1
    done_after = submits_today(now)
    if code != 0:
        reason = f'applier exited {code} — next wake retries'
    elif done_after >= DAILY_SUBMIT_TARGET:
        state['last_batch'] = today
        reason = f'day complete: {done_after} submits'
    else:
        reason = f'slice {runs + 1}/{MAX_RUNS_PER_DAY} done, {done_after} submits so far — next wake continues'
    print(f'batch: {reason}', flush=True)
    BATCH_STATE.write_text(json.dumps(state, indent=2) + '\n')
    return {**info, 'reason': reason}


def wishlist_size() -> int:
    try:
        db = json.loads((Path(__file__).parent.parent / 'jobs.json').read_text())
    except Exception:
        return 0
    return sum(1 for j in db.get('jobs', []) if j.get('status') in ('wishlist', 'resume_ready') and j.get('url'))


def submits_today(now) -> int:
    """Number of submit-class outcomes in today's apply_log."""
    log_path = Path(__file__).parent / 'apply_log.json'
    try:
        entries = json.loads(log_path.read_text())
    except Exception:
        return 0
    today = now.date().isoformat()
    return sum(1 for e in entries
               if e.get('timestamp', '').startswith(today)
               and e.get('status') in ('submitted', 'submit_unconfirmed', 'needs_code', 'held_for_review'))


WAKE_LOG = HERE / 'wake_log.json'
KEEP_WAKES = 60
SUBMIT_CLASS = ('submitted', 'submit_unconfirmed', 'needs_code', 'held_for_review')


def write_wake_summary(started: datetime, info: dict) -> None:
    """One record per wake: what ran, what came of it, why it stopped. This is
    the first thing to read when a day looks quiet — before the run logs,
    which only live on the box."""
    ended = datetime.now(timezone.utc)
    # apply_log timestamps are naive local time (UTC on the box)
    since = started.replace(tzinfo=None).isoformat()
    try:
        entries = [e for e in json.loads((HERE / 'apply_log.json').read_text())
                   if e.get('timestamp', '') >= since]
    except Exception:
        entries = []
    try:
        jobs = json.loads((ROOT / 'jobs.json').read_text())['jobs']
    except Exception:
        jobs = []
    statuses = {}
    for e in entries:
        statuses[e.get('status')] = statuses.get(e.get('status'), 0) + 1
    rec = {
        'wake_id': f"{(os.environ.get('GITHUB_SHA') or 'local')[:7]}-{started.strftime('%Y%m%dT%H%M%SZ')}",
        'started': started.isoformat(timespec='seconds'),
        'ended': ended.isoformat(timespec='seconds'),
        'minutes': round((ended - started).total_seconds() / 60, 1),
        'run_index': info.get('run_index'),
        'scout_ran': info.get('scout_ran', False),
        'applier_ran': info.get('applier_ran', False),
        'attempted': len(entries),
        'submitted': sum(1 for e in entries if e.get('status') in SUBMIT_CLASS),
        'parked': sum(1 for e in entries if e.get('status') in ('manual_review', 'needs_account', 'interrupted')),
        'failed': sum(1 for e in entries if e.get('status') in ('error', 'no_form')),
        'outcomes': statuses,
        'remaining_wishlist': sum(1 for j in jobs if j.get('status') in ('wishlist', 'resume_ready')),
        'reason': info.get('reason', ''),
    }
    try:
        hist = json.loads(WAKE_LOG.read_text())
    except Exception:
        hist = []
    hist.append(rec)
    WAKE_LOG.write_text(json.dumps(hist[-KEEP_WAKES:], indent=2) + '\n')
    print(f"wake {rec['wake_id']}: {rec['minutes']}m, attempted {rec['attempted']}, "
          f"submitted {rec['submitted']}, parked {rec['parked']}, failed {rec['failed']}, "
          f"wishlist left {rec['remaining_wishlist']} — {rec['reason']}", flush=True)


if __name__ == '__main__':
    started = datetime.now(timezone.utc)
    process_queue()
    info = daily_batch()
    write_wake_summary(started, info)
    sys.exit(0)
