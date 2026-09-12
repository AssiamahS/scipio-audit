#!/usr/bin/env python3
"""Where applications actually go, counted from the log rather than guessed.

    uv run python funnel.py            # the funnel
    uv run python funnel.py --dupes    # every repeat submit, worst first

Counts DISTINCT postings, not log rows. Sixty-seven submit attempts looked
like sixty-seven applications until this was written; they were twenty-six
jobs, five of them applied to between six and eleven times.
"""
import argparse
import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from pacing import _norm_url  # noqa: E402

LOG_PATH = Path(__file__).parent / 'apply_log.json'
JOBS_PATH = Path(__file__).parent.parent / 'jobs.json'

SUBMIT_STATES = ('submitted', 'submit_unconfirmed')


def load():
    logs = json.loads(LOG_PATH.read_text()) if LOG_PATH.exists() else []
    db = json.loads(JOBS_PATH.read_text()) if JOBS_PATH.exists() else {'jobs': []}
    return logs, db


def funnel(logs, db):
    attempts = [l for l in logs if l.get('status') in SUBMIT_STATES]
    by_url = collections.defaultdict(list)
    for l in attempts:
        by_url[_norm_url(l.get('url', ''))].append(l)

    confirmed_page = {u for u, ls in by_url.items()
                      if any(l['status'] == 'submitted' for l in ls)}
    confirmed_mail = {u for u, ls in by_url.items()
                      if any(l.get('confirmed_via') == 'email' for l in ls)}
    jobs = db.get('jobs', [])
    statuses = collections.Counter(j.get('status') for j in jobs)
    rejected = statuses.get('rejected', 0)

    return {
        'tracked': len(jobs),
        'queued': statuses.get('wishlist', 0) + statuses.get('resume_ready', 0),
        'attempts': len(attempts),
        'distinct': len(by_url),
        'duplicates': len(attempts) - len(by_url),
        'confirmed_page': len(confirmed_page),
        'confirmed_email': len(confirmed_mail),
        'unverified': len(by_url) - len(confirmed_page),
        'rejected': rejected,
        'unverified_tracker': statuses.get('submitted_unverified', 0),
        'bad_url': statuses.get('bad_url', 0),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dupes', action='store_true', help='list repeat submits')
    ap.add_argument('--json', action='store_true')
    a = ap.parse_args()
    logs, db = load()
    f = funnel(logs, db)

    if a.json:
        print(json.dumps(f, indent=2))
        return

    print(f"\n{'='*58}\n  SCIPIO FUNNEL\n{'='*58}")
    print(f"  jobs tracked           {f['tracked']:4}")
    print(f"  queued to apply        {f['queued']:4}")
    print(f"  submit attempts        {f['attempts']:4}")
    print(f"  DISTINCT jobs applied  {f['distinct']:4}"
          f"   <- the number that counts")
    if f['duplicates']:
        pct = f['duplicates'] / f['attempts'] * 100
        print(f"  wasted repeat submits  {f['duplicates']:4}   ({pct:.0f}% of attempts)")
    print(f"  confirmed on page      {f['confirmed_page']:4}")
    print(f"  confirmed by email     {f['confirmed_email']:4}")
    print(f"  never confirmed        {f['unverified']:4}")
    print(f"  rejections received    {f['rejected']:4}")
    if f['distinct']:
        print(f"\n  response rate          {f['rejected']/f['distinct']*100:5.1f}%"
              f"  ({f['rejected']}/{f['distinct']} distinct)")

    if a.dupes:
        by_url = collections.defaultdict(list)
        for l in logs:
            if l.get('status') in SUBMIT_STATES:
                by_url[_norm_url(l.get('url', ''))].append(l)
        print(f"\n{'='*58}\n  REPEAT SUBMITS\n{'='*58}")
        for u, ls in sorted(by_url.items(), key=lambda kv: -len(kv[1])):
            if len(ls) < 2:
                continue
            print(f"  {len(ls)}x  {ls[0]['company'][:26]:26} {ls[0].get('role', '')[:34]}")
    print()


if __name__ == '__main__':
    main()
