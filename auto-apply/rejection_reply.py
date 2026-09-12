#!/usr/bin/env python3
"""One polite reply to every rejection that came from a person.

Recruiters answer roughly one in ten of these, and the ones who do tell you
exactly what the gap was, which is worth more than the next ten applications.
Only human senders get a reply (no-reply relays are skipped by mailer), only
rejections under 14 days old, one reply per message-id, three per run.

  uv run python rejection_reply.py [--dry-run]
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).parent
STATE = HERE / 'rejection_replies_state.json'
FEED = HERE / 'rejections.json'

sys.path.insert(0, str(HERE))
import mailer  # noqa: E402

BODY = """Hi{greeting},

Thank you for letting me know, and for the time your team spent on my application for the {role} role.

If you have thirty seconds, I would value one line on what would have made me a stronger candidate for it. I take that kind of feedback seriously and it helps me aim better.

Either way, I appreciate the note, and I would be glad to hear from {company} if a closer fit opens up.

Best,
Sylvester Assiamah
<phone>
"""


def main():
    dry = '--dry-run' in sys.argv
    try:
        feed = json.loads(FEED.read_text())
    except Exception:
        print('no rejections feed')
        return
    try:
        state = json.loads(STATE.read_text())
    except Exception:
        state = {'replied': []}
    done = {r.get('message_id') for r in state['replied']}
    try:
        jobs = {str(j['id']): j for j in json.loads((HERE.parent / 'jobs.json').read_text())['jobs']}
    except Exception:
        jobs = {}
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)

    sent = 0
    for r in feed:
        if sent >= 3:
            break
        mid = r.get('message_id') or r.get('subject')
        if mid in done:
            continue
        try:
            dt = datetime.fromisoformat(r['date'])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if dt < cutoff:
            continue
        to = mailer.reply_target(r.get('from', ''), r.get('reply_to', ''))
        if not to:
            continue
        name, _ = mailer.parse_addr(r.get('from', ''))
        first = name.split()[0] if name and '@' not in name else ''
        job = jobs.get(str(r.get('job_id')))
        role = mailer.clean_voice((job or {}).get('role') or 'the')
        company = mailer.clean_voice(((job or {}).get('company') or r.get('company') or 'your team').replace('-', ' ')).title()
        subj = r.get('subject', '')
        subj = subj if subj.lower().startswith('re:') else f'Re: {subj}'
        body = BODY.format(greeting=f' {first}' if first else '', role=role, company=company)
        if mailer.send_mail(to, subj, body, in_reply_to=r.get('message_id'), dry_run=dry):
            state['replied'].append({'message_id': mid, 'to': to, 'ts': datetime.now(timezone.utc).isoformat(timespec='minutes'),
                                     'company': r.get('company')})
            sent += 1
    if not dry:
        STATE.write_text(json.dumps(state, indent=1))
    print(f'{sent} rejection repl{"y" if sent == 1 else "ies"} sent')


if __name__ == '__main__':
    main()
