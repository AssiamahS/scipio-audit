#!/usr/bin/env python3
"""Scout filter fixtures — runs in the pipeline before any application.

A wrong location or title filter costs real submits (8/24: "Remote, Belgium"
queued as a 7/10 and a Biostatistics role passed on "research associate").
No network, no AI: pure config + function checks. Exit 1 blocks the wake.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import scout  # noqa: E402

CFG = json.loads((Path(__file__).parent / 'scout_config.json').read_text())

LOCATION_OK = [
    'Remote', 'Remote, USA', 'Remote (US)', 'US Remote', 'Remote-US', 'Remote - US East',
    'Remote, United States', 'Remote, New Jersey', 'Remote, NJ', 'New York, NY',
    'Hoboken, NJ (Hybrid)', 'Jersey City, NJ', 'Manhattan, New York', 'Princeton, NJ',
    'Hybrid, Newark, NJ', 'New York, NY (Hybrid)', 'Hybrid - New York',
]
LOCATION_REJECT = [
    'Remote, Belgium', 'Remote - Bulgaria', 'Remote, Slovakia', 'Remote, Russia',
    'Remote, Austria', 'Remote, Vietnam', 'Remote, EMEA', 'Remote, Europe', 'Remote, Canada',
    'Remote (India)', 'London, United Kingdom', 'Toronto, Ontario', 'Remote, Worldwide',
    # 9/11: PayPay Tokyo reqs were listed as just "(Hybrid)" and got 3 submits each
    'Hybrid', '(Hybrid)', 'Onsite', 'On-site', 'Hybrid - Tokyo', 'Hybrid, Tokyo, Japan',
]
TITLE_OK = [
    'IT Project Manager', 'Technical Project Manager', 'Infrastructure Engineer',
    'Systems Administrator', 'IAM Analyst', 'Clinical Research Coordinator',
    'Implementation Specialist', 'Epic Analyst', 'Help Desk Technician',
]
TITLE_REJECT = [
    'Senior Project Manager', 'Sr. Technical Project Manager', 'Lead Implementation Manager',
    'IT Project Manager III', 'VP, Program Management', 'Head of IT',
    'Biostatistics Research Associate', 'Senior Software Engineer', 'Director of IT',
    'Staff Engineer, Platform', 'Nurse Practitioner', 'Recovery Support Specialist',
    'Principal Data Engineer', 'Statistician II', 'Machine Learning Engineer',
    'Senior Manager, Clinical Trials', 'Senior Clinical Research Scientist',
]


# fit_check knockouts: (JD snippet, role, expect_skip)
FIT_CASES = [
    ("Requirements: Master's degree in Public Health, Health Administration, or Business required. PMP certification required.", 'Project Manager - Informatics', True),
    ("Bachelor's degree required; Master's degree preferred. 3+ years of project coordination.", 'Project Coordinator', False),
    ("Must hold an active Secret security clearance.", 'IT Project Manager', True),
    ("8+ years of enterprise program management experience required.", 'Program Manager', True),
    ("3-5 years of healthcare IT experience. Epic experience a plus. Bachelor's or equivalent.", 'Healthcare IT Analyst', False),
    ("Experience with MS Office, MS Excel and master data management required.", 'IT Analyst', False),
    ("PMP or CAPM preferred but not required. Bachelor's degree required.", 'Implementation Project Manager', False),
]
# form-question honesty: (question label, expected answer)
QUESTION_CASES = [
    ("Do you meet ALL of the following job requirements? Master's degree in Public Health or Business and 5 years of experience", 'no'),
    ("Do you meet ALL of the following job requirements? Bachelor's degree or higher and certification in project management (PMP)", 'no'),
    ("Do you meet the minimum qualifications: Bachelor's degree and 3+ years of project management?", 'yes'),
]


def main() -> int:
    failures = []
    import fit_check
    from ats_handlers import _desired_options
    for jd, role, expect_skip in FIT_CASES:
        rep = fit_check.assess({'role': role, 'description': jd}, {'skills': [], 'experience': []})
        if (rep['verdict'] == 'skip') != expect_skip:
            failures.append(f'fit_check {role!r} expected skip={expect_skip}, got {rep["verdict"]}: {rep["knockouts"]}')
    for q, want in QUESTION_CASES:
        got = _desired_options(q)
        if not got or got[0] != want:
            failures.append(f'question {q[:60]!r} expected {want}, got {got}')
    for loc in LOCATION_OK:
        if not scout.location_ok(loc, CFG):
            failures.append(f'location should PASS: {loc!r}')
    for loc in LOCATION_REJECT:
        if scout.location_ok(loc, CFG):
            failures.append(f'location should REJECT: {loc!r}')
    for t in TITLE_OK:
        if not scout.title_ok(t, CFG):
            failures.append(f'title should PASS: {t!r}')
    for t in TITLE_REJECT:
        if scout.title_ok(t, CFG):
            failures.append(f'title should REJECT: {t!r}')
    total = len(LOCATION_OK) + len(LOCATION_REJECT) + len(TITLE_OK) + len(TITLE_REJECT) + len(FIT_CASES) + len(QUESTION_CASES)
    for f in failures:
        print(f'  FAIL {f}')
    print(f'scout filters: {total - len(failures)}/{total} fixtures pass')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
