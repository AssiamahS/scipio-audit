#!/usr/bin/env python3
"""One email a day to his phone with the whole funnel and what needs a human.

Sent once per UTC day, after the last slice (4 runs done, day closed, or the
15:00 UTC wake). Everything else the pipeline writes is a file in a repo
nobody opens on a Saturday; this is the piece that reaches him.

  uv run python digest.py [--force] [--dry-run]
"""
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
STATE = HERE / 'digest_state.json'

sys.path.insert(0, str(HERE))
import mailer  # noqa: E402


def load(path, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def should_send(now: datetime, batch: dict, force: bool) -> bool:
    if force:
        return True
    today = now.strftime('%Y-%m-%d')
    st = load(STATE, {})
    if st.get('last_sent') == today:
        return False
    if batch.get('last_batch') == today:
        return True
    if batch.get('runs_date') == today and int(batch.get('runs') or 0) >= 4:
        return True
    return now.hour >= 15


def main():
    force = '--force' in sys.argv
    dry = '--dry-run' in sys.argv
    now = datetime.now(timezone.utc)
    today = now.strftime('%Y-%m-%d')
    batch = load(HERE / 'batch_state.json', {})
    if not should_send(now, batch, force):
        print('digest: not due yet')
        return

    log = load(HERE / 'apply_log.json', [])
    jobs = load(ROOT / 'jobs.json', {'jobs': []})['jobs']
    confirms = load(HERE / 'confirmations.json', [])
    rejects = load(HERE / 'rejections.json', [])
    needs = load(HERE / 'needs_reply.json', [])
    fstate = load(HERE / 'followups_state.json', {})
    rstate = load(HERE / 'rejection_replies_state.json', {})
    wakes = load(HERE / 'wake_log.json', [])

    today_log = [e for e in log if (e.get('timestamp') or '').startswith(today)]
    by_status = Counter(e.get('status') for e in today_log)
    submits = [e for e in today_log if str(e.get('status', '')).startswith('submit')]
    conf_today = [c for c in confirms if (c.get('date') or '').startswith(today)]
    rej_today = [r for r in rejects if (r.get('date') or '').startswith(today)]
    wakes_today = [w for w in wakes if (w.get('started') or '').startswith(today)]
    open_interviews = [n for n in needs if n.get('company') and n.get('mailbox') != '[Gmail]/Spam']
    statuses = Counter(j.get('status') for j in jobs)
    hand = [j for j in jobs if j.get('status') in ('hand_apply', 'manual_review')]
    hand.sort(key=lambda j: j.get('updated_date') or '', reverse=True)
    fsent = [s for s in fstate.get('sent', []) if (s.get('ts') or '').startswith(today)]
    rsent = [s for s in rstate.get('replied', []) if (s.get('ts') or '').startswith(today)]
    week_cut = (now.replace(hour=0, minute=0) - __import__('datetime').timedelta(days=7)).isoformat()
    week_submits = len({e.get('url') for e in log if e.get('timestamp', '') >= week_cut
                        and str(e.get('status', '')).startswith('submit')})

    L = []
    L.append(f"Scipio, {now.strftime('%a %b %d')}")
    L.append('')
    L.append(f"Today: {len(submits)} submit(s) over {len(wakes_today)} wake(s). "
             f"Last 7 days: {week_submits} (target 16).")
    if by_status:
        L.append('Outcomes: ' + ', '.join(f'{k} {v}' for k, v in by_status.most_common()))
    for e in submits:
        L.append(f"  - {e.get('company')} : {e.get('role', '')[:60]} [{e.get('status')}]")
    L.append('')
    if open_interviews:
        L.append(f"INTERVIEW REQUESTS WAITING ON YOU: {len(open_interviews)}")
        for n in open_interviews[-3:]:
            L.append(f"  - {n.get('company')} : {n.get('subject', '')[:70]} (reply is in Gmail Drafts)")
        L.append('')
    L.append(f"Inbox: {len(conf_today)} confirmation(s), {len(rej_today)} rejection(s) today. "
             f"Lifetime: {len(confirms)} confirmed, {len(rejects)} rejected, {statuses.get('interview', 0)} interview(s).")
    for c in conf_today:
        L.append(f"  + confirmed: {c.get('company') or c.get('subject', '')[:50]}")
    for r in rej_today:
        L.append(f"  - rejected: {r.get('company') or r.get('subject', '')[:50]}")
    L.append('')
    L.append(f"Follow-ups sent today: {len(fsent)}. Rejection feedback asks sent today: {len(rsent)}.")
    for s in fsent:
        L.append(f"  - {s.get('stage')} -> {s.get('company')} ({s.get('to')})")
    L.append('')
    if hand:
        L.append(f"HAND-APPLY PACK: {len(hand)} job(s) with a resume staged. Two minutes each.")
        for j in hand[:6]:
            L.append(f"  - {j.get('company')} : {j.get('role', '')[:55]}")
            L.append(f"    {j.get('url', '')}")
        if len(hand) > 6:
            L.append(f"  ... {len(hand) - 6} more in auto-apply/HAND_APPLY.md")
        L.append('')
    L.append(f"Pipeline: wishlist {statuses.get('wishlist', 0)}, hand_apply {statuses.get('hand_apply', 0)}, "
             f"manual_review {statuses.get('manual_review', 0)}, submitted_unverified {statuses.get('submitted_unverified', 0)}, "
             f"applied {statuses.get('applied', 0)}, blocked_knockout {statuses.get('blocked_knockout', 0)}, "
             f"weak_match {statuses.get('weak_match', 0)}.")
    L.append('')
    L.append('Still only you can do (each one lifts the resume grade):')
    L.append('  - Real numbers for three soft bullets: Heartbeat campaign budget or team size, HMH tickets per week, Catalyst report count.')
    L.append('  - Rutgers bootcamp start month (covers the Feb 2023 to Jun 2024 gap on paper).')
    L.append('  - CAPM (or the Google PM certificate): PMP/CAPM is the most-missed JD keyword in the log.')
    L.append('  - Send the three referral notes in briefs/REFERRALS.md (Kevin Manu, HMH manager, Bayshore preceptor).')
    L.append('  - Give the resume its dates: months for Heartbeat, FCB, Catalyst and one line each for the 2020 and 2023-24 gaps.')
    L.append('')
    L.append('Reports: https://assiamahs.github.io/scipio/reports.html')
    body = '\n'.join(L)

    subj = f"{now.strftime('%b %d')}: {len(submits)} submit(s), {len(open_interviews)} interview(s) waiting, {len(hand)} hand-apply"
    ok = mailer.alert(subj, body, key='daily-digest', dry_run=dry)
    if ok and not dry:
        STATE.write_text(json.dumps({'last_sent': today}, indent=1))
    print(body if dry else f'digest: {"sent" if ok else "not sent"}')


if __name__ == '__main__':
    main()
