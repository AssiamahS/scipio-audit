#!/usr/bin/env python3
"""Workday handler fixtures. No network. Exit 1 blocks the wake."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import workday  # noqa: E402

PROFILE = json.loads((Path(__file__).parent / 'profile.json').read_text())
CVS_PAY = ['Select One', 'Under $20,000', '$20,000 - $40,000', '$40,000 - $60,000', '$60,000 - $80,000',
           '$80,000 - $100,000', '$100,000 - $120,000', '$220,000+', 'Decline to Answer']


def main() -> int:
    bad = 0
    # salary ranges: the desired figure lands in its own bracket, never "Under"
    for want, expect in (('60000', '$60,000 - $80,000'), ('75000', '$60,000 - $80,000'),
                         ('45000', '$40,000 - $60,000'), ('300000', '$220,000+')):
        got = CVS_PAY[workday.pick_salary_option(CVS_PAY, want)]
        if got != expect:
            print(f'FAIL salary {want} -> {got}, want {expect}'); bad += 1
    # screening canon: questions that mention "extensions" are not skip labels
    cases = [
        ('Do you currently need employer-provided visa sponsorship or immigration support to work for this company? (For example: H-1B - including transfer, amendment, extension, or lottery)', 'choose', ['no']),
        ('Will you need employer-provided visa sponsorship in the future?', 'choose', ['no']),
        ('Do you currently work at CVS Health, CVS Pharmacy, Aetna, MinuteClinic, or any subsidiaries?', 'choose', ['no']),
        ('Have you ever been employed by, worked as a contingent worker, contractor, intern for CVS Health?', 'radio', ['no']),
        ('I authorize CVS Health to communicate with me via text (standard message rates may apply)', 'choose', None),
        ('Are you at least 18 years old?', 'choose', None),
        ('What is your base pay expectation for this position?', 'salary', None),
        ('How Did You Hear About Us?', 'prompt', None),
        ('First Name', 'text', None),
        ('Address Line 2', 'skip', None),
        ('Phone Extension', 'skip', None),
        ('Please select your gender', 'choose', None),
    ]
    for label, kind, want in cases:
        spec = workday.wd_value(label, PROFILE)
        if not spec or spec[0] != kind:
            print(f'FAIL wd_value({label[:50]!r}) -> {spec}, want kind {kind}'); bad += 1
        elif want and spec[1][:1] != want:
            print(f'FAIL wd_value({label[:50]!r}) -> {spec[1]}, want {want}'); bad += 1
    if workday.wd_value('Are you at least 18 years old?', PROFILE)[1][0] != 'yes':
        print('FAIL 18+ must be yes'); bad += 1
    pw = workday.derive_password('cvshealth')
    if not (len(pw) >= 8 and any(c.isupper() for c in pw) and any(c.islower() for c in pw)
            and any(c.isdigit() for c in pw) and '!' in pw):
        print('FAIL derived password does not meet Workday rules'); bad += 1
    if workday.derive_password('cvshealth') != pw or workday.derive_password('humana') == pw:
        print('FAIL derived password must be stable per tenant and differ across tenants'); bad += 1
    if workday.tenant_of('https://cvshealth.wd1.myworkdayjobs.com/en-US/CVS_Health_Careers/job/x/y_R1') != 'cvshealth':
        print('FAIL tenant_of'); bad += 1
    print('workday fixtures:', 'FAIL' if bad else 'ok', f'({bad} problem(s))')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
