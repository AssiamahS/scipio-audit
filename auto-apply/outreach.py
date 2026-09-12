#!/usr/bin/env python3
"""Draft the human follow-up for each application. Drafts only, never sends.

A cold application is a lottery ticket. One short note to a real person at the
company is the single highest-return thing Sylvester can do by hand, so this
writes the note for him and leaves the sending to him.

    uv run python outreach.py            # draft for everything applied/queued
    uv run python outreach.py --status applied

Writes outreach.json: {job_id, company, role, note, followup, linkedin_search}.
No contact name is ever invented. When we don't know who to write to, the draft
is addressed to the team and carries a LinkedIn people-search URL so finding
the recruiter is one tap.
"""
import argparse
import json
import sys
from pathlib import Path
from urllib.parse import quote_plus

HERE = Path(__file__).parent
JOBS = HERE.parent / 'jobs.json'
OUT = HERE / 'outreach.json'

# Sylvester's voice: plain, specific, no em dashes, no agency words. These are
# templates over his real facts, not generated claims.
NOTE = """Hi{greeting},

I applied for the {role} role at {company} this week and wanted to put a name to the application.

I run infrastructure operations at Hackensack Meridian Health, which is NJ's largest health network. Day to day that means I am the first technical call for over 200 clinical and administrative staff, I brought 15+ medical devices online across the hospital, and I traced slow pre op scan loading to a data transfer bottleneck and rebuilt the storage setup, which cut scan rendering times by 40 percent. I am SailPoint IdentityNow and Okta certified.

If it would help, I am happy to walk through how any of that maps to what your team is working on.

Thanks for your time,
Sylvester Assiamah
<phone> | <linkedin>"""

FOLLOWUP = """Hi{greeting},

Following up on the {role} role at {company}. I know these inboxes fill up fast.

Still very interested, and still happy to answer anything about the healthcare infrastructure and identity side of my background. If the role has moved on, no problem at all, and I would appreciate knowing so I can stop checking.

Thanks,
Sylvester Assiamah"""


def linkedin_search(company: str, role: str) -> str:
    """People search at the company for the humans who own this hire."""
    terms = f'{company} recruiter talent acquisition'
    return f'https://www.linkedin.com/search/results/people/?keywords={quote_plus(terms)}'


def _clean(text: str) -> str:
    """Job titles carry em dashes; nothing Sylvester sends may."""
    return (text or '').replace('—', ',').replace('–', '-').strip()


def draft(job: dict) -> dict:
    company = _clean(job.get('company') or '').replace('-', ' ').title()
    role = _clean(job.get('role')) or 'the role'
    contact = job.get('contact_name')
    greeting = f' {contact.split()[0]}' if contact else ' there'
    fields = {'greeting': greeting, 'role': role, 'company': company}
    return {
        'job_id': job.get('id'),
        'company': company,
        'role': role,
        'status': job.get('status'),
        'contact_name': contact,
        'note': NOTE.format(**fields),
        'followup': FOLLOWUP.format(**fields),
        'followup_after_days': 7,
        'linkedin_search': linkedin_search(company, role),
        'sent': False,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--status', default=None,
                    help='only draft for jobs in this status')
    ap.add_argument('--print', action='store_true', help='show one draft')
    a = ap.parse_args()

    db = json.loads(JOBS.read_text())
    wanted = ({a.status} if a.status
              else {'wishlist', 'resume_ready', 'applied', 'submitted_unverified'})
    jobs = [j for j in db['jobs'] if j.get('status') in wanted]

    prev = json.loads(OUT.read_text()) if OUT.exists() else []
    kept = {d['job_id']: d for d in prev if d.get('sent')}  # never clobber sent
    drafts = [kept.get(j['id']) or draft(j) for j in jobs]
    OUT.write_text(json.dumps(drafts, indent=2))

    print(f'drafted {len(drafts)} outreach note(s) -> {OUT.name}')
    print('nothing is sent automatically; review and send from the app')
    if a.print and drafts:
        print('\n' + '=' * 58)
        print(drafts[0]['note'])
        print('=' * 58)
        print('follow-up in 7 days:\n')
        print(drafts[0]['followup'])
        print('\nfind the recruiter:', drafts[0]['linkedin_search'])


if __name__ == '__main__':
    main()
