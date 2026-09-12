#!/usr/bin/env python3
"""Email-loop fixtures. No network. Exit 1 blocks the wake, by design: a
follow-up to a no-reply relay or an "interview" raised on a spam blast is a
real-world embarrassment, not a unit-test nit."""
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))
import mailer  # noqa: E402
import rejections  # noqa: E402
import interview_pack  # noqa: E402
import scout  # noqa: E402

HUMAN = ['abrown@eclinicalsol.com', 'Sarah K <sarah.k@bw-thinking.com>', 'jsmith@humana.com']
NOT_HUMAN = [
    'no-reply@us.greenhouse-mail.io', 'noreply@connectionshs.com', 'no-reply@bw-thinking.com',
    'notifications@lever.co', 'donotreply@myworkday.com', 'careers@acme.com', 'talent@acme.com',
    'applications@certifiedmobilenotaryservice.com', 'hr@acme.com', 'not-an-address',
]

# (mailbox, matched job or None, from header, expect real)
INTERVIEW_CASES = [
    ('"[Gmail]/Spam"', None, 'Sarah Boykins <applications@certifiedmobilenotaryservice.com>', False),
    ('"[Gmail]/Spam"', {'id': 1}, 'recruiter@bw-thinking.com', False),
    ('INBOX', None, 'random@marketing-blast.com', False),
    ('INBOX', None, 'no-reply@us.greenhouse-mail.io', True),
    ('"[Gmail]/Trash"', {'id': 7}, 'jane@company.com', True),
]


def main() -> int:
    bad = 0
    for a in HUMAN:
        _, addr = mailer.parse_addr(a)
        if not mailer.is_human_address(addr):
            print(f'FAIL human address rejected: {a}'); bad += 1
    for a in NOT_HUMAN:
        _, addr = mailer.parse_addr(a)
        if mailer.is_human_address(addr):
            print(f'FAIL no-reply address accepted: {a}'); bad += 1
    if mailer.send_mail('no-reply@us.greenhouse-mail.io', 'x', 'y', dry_run=True):
        print('FAIL send_mail let a relay address through'); bad += 1
    if mailer.clean_voice('a — b – c') != 'a , b - c':
        print('FAIL clean_voice left a dash'); bad += 1

    for box, match, frm, want in INTERVIEW_CASES:
        got = rejections._real_interview(box, match, frm)
        if got != want:
            print(f'FAIL interview guard: {box} match={bool(match)} {frm} -> {got}, want {want}'); bad += 1

    w = interview_pack.windows(datetime(2026, 9, 4, 16, 0, tzinfo=ZoneInfo('America/New_York')))  # a Friday
    if len(w) != 3 or not w[0].startswith('Monday') or not w[2].startswith('Wednesday'):
        print(f'FAIL interview windows skipped a weekend wrong: {w}'); bad += 1
    if any('—' in x for x in (interview_pack.REPLY,)):
        print('FAIL reply template has an em dash'); bad += 1

    if scout.workday_posted({'startDate': '2026-09-03'}, {}) != '2026-09-03':
        print('FAIL workday startDate not used'); bad += 1
    if not scout.workday_posted({}, {'postedOn': 'Posted 2 Days Ago'}):
        print('FAIL workday relative date not parsed'); bad += 1
    for loc, want in (('Durham, North Carolina, United States of America', False),
                      ('Work At Home-New Jersey', True), ('Remote Nationwide', True),
                      ('Louisville, KY, United States of America', False), ('New York, NY', True)):
        if scout.commutable(loc) != want:
            print(f'FAIL commutable({loc!r}) != {want}'); bad += 1
    for loc in ('Remote Arizona', 'Remote, USA', 'Louisville, KY'):
        ok = scout.location_ok(loc, scout.CONFIG)
        if loc.startswith('Remote') and not ok:
            print(f'FAIL workday remote location rejected: {loc}'); bad += 1

    print('email-loop fixtures:', 'FAIL' if bad else 'ok', f'({bad} problem(s))')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
