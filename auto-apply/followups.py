#!/usr/bin/env python3
"""Follow-ups that actually go out.

Every confirmed or clicked application gets two touches unless a rejection or
an interview arrives first:
  day 5   short check-in
  day 12  last note, asks for a yes/no so he can stop checking
The recipient is the first human address we know for that application, in
this order: job.recruiter_email (set by hand or by a future finder), a human
sender/reply-to on the confirmation email, a human sender on any earlier mail
from that company. Relay and no-reply addresses never get mail
(mailer.is_human_address). When no human address exists the note is saved to
Gmail Drafts once, with a LinkedIn people-search link in the body, so the
send is one tap after he finds the recruiter.

State: followups_state.json {"sent": [...], "drafted": [...]}; one touch per
(url, stage), ever. Cap 5 sends per run.

  uv run python followups.py [--dry-run]
"""
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus

HERE = Path(__file__).parent
ROOT = HERE.parent
STATE = HERE / 'followups_state.json'

sys.path.insert(0, str(HERE))
import mailer  # noqa: E402

DAY5 = """Hi{greeting},

I applied for the {role} position at {company} about a week ago and wanted to check in. I am still very interested.

Quick version of why I think it fits: I run infrastructure operations at Hackensack Meridian Health, NJ's largest health network. I am the first technical call for 200+ clinical and administrative staff, I brought 15+ medical devices online across the hospital, and I traced slow pre-op scan loading to a data transfer bottleneck and rebuilt the storage setup, which cut scan rendering times 40 percent. Before that I ran pharma marketing projects through MLR review at Publicis Health and FCB.

Happy to send anything else that would help.

Best,
Sylvester Assiamah
<phone> | <linkedin>
"""

DAY12 = """Hi{greeting},

One last note on the {role} application at {company}. If the role has moved on, no problem at all, and I would appreciate a quick heads up so I can stop checking.

If it is still open, I am ready to talk whenever works for you.

Thanks,
Sylvester Assiamah
<phone>
"""

STAGES = [(5, 'day5', DAY5, 'Following up on my {role} application'),
          (12, 'day12', DAY12, 'Re: {role} application at {company}')]
MAX_AGE_DAYS = 20   # older than this and the posting is stale; skip silently


def load(name, default):
    try:
        return json.loads((HERE / name).read_text())
    except Exception:
        return default


def norm(s):
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())


def pretty(company: str) -> str:
    return mailer.clean_voice((company or 'the team').replace('-', ' ').replace('_', ' ')).title()


def human_contact(company: str, job: dict | None, confirmations: list, rejections: list) -> str | None:
    if job and job.get('recruiter_email') and mailer.is_human_address(job['recruiter_email']):
        return job['recruiter_email']
    c = norm(company)
    for feed in (confirmations, rejections):
        for r in feed:
            if norm(r.get('company', '')) == c or (c and c in norm(r.get('from', ''))):
                t = mailer.reply_target(r.get('from', ''), r.get('reply_to', ''))
                if t:
                    return t
    return None


def due_applications(log: list, jobs_by_id: dict, rejections: list, invites: list, state: dict, now: datetime):
    rej = {norm(r.get('company', '')) for r in rejections}
    inv = {norm(r.get('company', '')) for r in invites if r.get('company')}
    done = {(s['url'], s['stage']) for s in state.get('sent', []) + state.get('drafted', [])}
    rejected_ids = {str(j['id']) for j in jobs_by_id.values() if j.get('status') in ('rejected', 'interview', 'withdrawn')}
    seen, todo = set(), []
    for e in log:
        status = (e.get('status') or '').lower()
        url = (e.get('url') or '').split('?')[0]
        if not url or url in seen:
            continue
        if not (status.startswith('submit') or status in ('applied',)):
            continue
        seen.add(url)
        if str(e.get('job_id')) in rejected_ids:
            continue
        c = norm(e.get('company', ''))
        if c and (any(c in r or r in c for r in rej if r) or any(c in r or r in c for r in inv if r)):
            continue
        try:
            sent_at = datetime.fromisoformat(e.get('timestamp', '')[:19])
        except ValueError:
            continue
        age = (now - sent_at).days
        if age > MAX_AGE_DAYS:
            continue
        for days, stage, tmpl, subj in STAGES:
            if age >= days and (url, stage) not in done:
                todo.append((url, stage, tmpl, subj, e))
                break   # one stage per run per application
    return todo


def main():
    dry = '--dry-run' in sys.argv
    log = load('apply_log.json', [])
    rejections = load('rejections.json', [])
    confirmations = load('confirmations.json', [])
    invites = load('needs_reply.json', [])
    state = load('followups_state.json', {})
    # migrate the old {"drafted": [url, ...]} shape
    if isinstance(state.get('drafted'), list) and state['drafted'] and isinstance(state['drafted'][0], str):
        state['drafted'] = [{'url': u, 'stage': 'day5', 'ts': ''} for u in state['drafted']]
    state.setdefault('sent', [])
    state.setdefault('drafted', [])
    db = load('../jobs.json', {'jobs': []})
    jobs_by_id = {str(j['id']): j for j in db['jobs']}

    todo = due_applications(log, jobs_by_id, rejections, invites, state, datetime.now())
    if not todo:
        print('no follow-ups due')
        return

    sent = 0
    for url, stage, tmpl, subj, e in todo:
        if sent >= 5:
            break
        job = jobs_by_id.get(str(e.get('job_id')))
        company = pretty(e.get('company'))
        role = mailer.clean_voice(e.get('role') or (job or {}).get('role') or 'the role')
        to = human_contact(e.get('company', ''), job, confirmations, rejections)
        greeting = ''
        body = tmpl.format(greeting=greeting, role=role, company=company)
        subject = subj.format(role=role, company=company)
        stamp = datetime.now().isoformat(timespec='minutes')
        if to:
            ok = mailer.send_mail(to, subject, body, dry_run=dry)
            if ok:
                state['sent'].append({'url': url, 'stage': stage, 'to': to, 'ts': stamp,
                                      'company': e.get('company'), 'job_id': e.get('job_id')})
                sent += 1
                print(f'  [+] {stage} follow-up -> {company} ({to})')
        else:
            if any(d['url'] == url for d in state['drafted']):
                continue   # one unaddressed draft per application, ever
            search = f"https://www.linkedin.com/search/results/people/?keywords={quote_plus(company + ' recruiter talent acquisition')}"
            ok = mailer.save_draft('', subject, body + f"\n\n[find the recruiter: {search} ]\n[{url}]\n", dry_run=dry)
            if ok:
                state['drafted'].append({'url': url, 'stage': stage, 'ts': stamp, 'company': e.get('company')})
                print(f'  [~] {stage} follow-up drafted for {company} (no human address known)')

    if not dry:
        STATE.write_text(json.dumps(state, indent=1))
    print(f'{sent} follow-up(s) sent, {len(state["drafted"])} lifetime unaddressed draft(s)')


if __name__ == '__main__':
    main()
