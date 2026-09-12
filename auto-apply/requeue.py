"""Put parked jobs back on deck — nothing waits on a human.

captcha_retry  : a CAPTCHA wall on the last attempt (captcha.py failed)
needs_account  : a Workday tenant refused account creation that wake
manual_review  : legacy parks from before 9/9

Each comes back to `wishlist` once it has sat >= RETRY_AFTER_HOURS, up to
MAX_TRIES parks in total; after that it becomes blocked_captcha /
blocked_account and is never touched again. Runs at the top of every slice
(process_queue.py) before scout.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

JOBS_PATH = Path(__file__).parent.parent / 'jobs.json'
RETRY_AFTER_HOURS = 20
MAX_TRIES = 3
PARKED = {'captcha_retry': 'blocked_captcha', 'manual_review': 'blocked_captcha',
          'needs_account': 'blocked_account'}


def _last_change(job: dict) -> datetime | None:
    hist = job.get('history') or []
    for h in reversed(hist):
        if h.get('status') == job.get('status'):
            try:
                return datetime.strptime(h['date'], '%Y-%m-%d %H:%M')
            except (KeyError, ValueError):
                break
    try:
        return datetime.strptime(job.get('updated_date', ''), '%Y-%m-%d')
    except ValueError:
        return None


def requeue(db: dict, now: datetime | None = None) -> list[str]:
    now = now or datetime.now()
    notes = []
    for job in db.get('jobs', []):
        st = job.get('status')
        if st not in PARKED or not job.get('url'):
            continue
        parks = sum(1 for h in job.get('history') or [] if h.get('status') in PARKED)
        when = _last_change(job)
        if when and now - when < timedelta(hours=RETRY_AFTER_HOURS):
            continue
        new = PARKED[st] if parks >= MAX_TRIES else 'wishlist'
        job['prev_status'] = st
        job['status'] = new
        job['updated_date'] = now.strftime('%Y-%m-%d')
        job.setdefault('history', []).append({'status': new, 'date': now.strftime('%Y-%m-%d %H:%M'),
                                              'note': f'requeue after {st} x{parks}'})
        notes.append(f"#{job.get('id')} {job.get('company')} {st} -> {new} (parks={parks})")
    return notes


def main() -> int:
    db = json.loads(JOBS_PATH.read_text())
    notes = requeue(db)
    if notes:
        JOBS_PATH.write_text(json.dumps(db, indent=2))
    print(f'requeue: {len(notes)} job(s) back on deck' if notes else 'requeue: nothing waiting')
    for n in notes:
        print('  ' + n)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
