#!/usr/bin/env python3
"""Surface every job waiting on a human — the parked queue.

manual_review (CAPTCHA wall), needs_code (email code, no GMAIL_APP_PASSWORD
in env), and submitted_unverified (clicked submit, no confirmation seen) all
retire a job from the wishlist forever, by design. Nothing retries them and
until now nothing reported them — 23 manual_review jobs sat invisible while
every daily run looked green. This writes parked.md (committed by the apply
run) and prints the list into CI logs so parked work is impossible to miss.

The payoff: a manual_review job arrives with its form already 100% filled
once by the applier — a human submit is ~30 seconds with the URL in hand.
"""

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

PARKED_STATUSES = {
    'manual_review': 'CAPTCHA wall — open the URL and submit by hand (form data is in profile.json / the tailored PDF)',
    'needs_code': 'greenhouse emailed a security code the run could not read — finish by hand or re-run with GMAIL_APP_PASSWORD',
    'submitted_unverified': 'submit was clicked but no confirmation appeared — check email/board before doing anything, it may have gone through',
}


def latest_log_by_url():
    try:
        entries = json.loads((HERE / 'apply_log.json').read_text())
    except Exception:
        return {}
    out = {}
    for e in entries:
        if e.get('url'):
            out[e['url']] = e  # later entries win
    return out


def main():
    db = json.loads((ROOT / 'jobs.json').read_text())
    jobs = db['jobs'] if isinstance(db, dict) else db
    parked = [j for j in jobs if j.get('status') in PARKED_STATUSES]
    logs = latest_log_by_url()

    counts = Counter(j['status'] for j in parked)
    lines = [
        '# Parked — needs a human',
        '',
        f'_{len(parked)} job(s) waiting; generated '
        f'{datetime.now(timezone.utc).isoformat(timespec="minutes")} by parked.py_',
        '',
    ]
    for status, hint in PARKED_STATUSES.items():
        group = [j for j in parked if j['status'] == status]
        if not group:
            continue
        lines += [f'## {status} ({len(group)}) — {hint}', '']
        # newest first: recent parks are the ones whose postings are still open
        group.sort(key=lambda j: j.get('updated_date') or '', reverse=True)
        for j in group:
            log = logs.get(j.get('url'), {})
            when = (log.get('timestamp') or j.get('updated_date') or '?')[:10]
            lines.append(f"- **{j['company']}** — {j.get('role', '?')} ({when})  \n"
                         f"  {j.get('url', 'no url')}")
        lines.append('')

    # the no-AI loop: questions nothing could answer stay blank on the form
    # and surface here — each becomes a one-time permanent bank rule
    # (DROPDOWN_ANSWERS / screening_answers), then never recurs
    try:
        log = json.loads((HERE / 'apply_log.json').read_text())
    except Exception:
        log = []
    cutoff = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    cutoff = cutoff[:8] + '01'  # this month
    unanswered = {}
    for e in log:
        if (e.get('timestamp') or '') < cutoff:
            continue
        d = e.get('details') or {}
        for u in d.get('unanswered_required') or []:
            lab = (u.get('label') or '').strip()
            if lab:
                unanswered.setdefault(lab[:120], e.get('company', ''))
    if unanswered:
        lines += ['## unanswered questions — add a bank rule for each (no-AI mode leaves them blank)', '']
        for lab, co in sorted(unanswered.items()):
            lines.append(f'- `{lab}` (seen at {co})')
        lines.append('')

    (HERE / 'parked.md').write_text('\n'.join(lines) + '\n')

    print(f"parked: {len(parked)} job(s) need a human "
          f"({', '.join(f'{k}:{v}' for k, v in sorted(counts.items()))})", flush=True)
    for j in parked[:10]:
        print(f"  {j['status']:<22} {j['company'][:28]:<28} {j.get('url', '')}", flush=True)
    if len(parked) > 10:
        print(f"  ... +{len(parked) - 10} more in auto-apply/parked.md", flush=True)


if __name__ == '__main__':
    main()
