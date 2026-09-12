#!/usr/bin/env python3
"""Fit check — would a recruiter shortlist this application, honestly?

Answers four questions no keyword score answers, BEFORE a submit happens:

  1. KNOCKOUTS  — does the JD *require* a credential he does not hold
                  (Master's/MBA, PMP/CAPM, clearance, RN, Epic/ITIL/CISSP
                  certification, 8+ years)? A knockout is an auto-reject and
                  an honest "No" on the screening question. Blue Water
                  Thinking 7/22-7/30: three submits on a Master's/PMP-required
                  role, all answered "Yes", rejected in 6 days.
  2. LEVEL      — senior/lead/principal/director titles he would lose on
                  experience (Clarify Capital 2026: 1 in 5 senior postings is
                  a ghost job; Huntr: level mismatch is the top silent reject).
  3. TITLE      — does the target title match one he has honestly held
                  (Jobscan 2025, 2.5M applications: 10.6x interview rate).
  4. FRESHNESS  — posting age. >30 days = ghost-job signal (RecruitmentPQ,
                  Clarify Capital); <7 days = apply-early window (Huntr: 52%
                  of recruiters stop at the first shortlist).

Plus the literal keyword echo from ats_lint (76.4% of recruiters filter by
skills pulled from the JD — Jobscan).

Verdict: apply | stretch | skip, with every reason spelled out.

CLI (the "test a resume for fit" tool):
  uv run python fit_check.py --job-id 47
  uv run python fit_check.py --jd-file jd.txt --role "Project Manager"
  uv run python fit_check.py --url https://job-boards.greenhouse.io/x/jobs/123
  ... add --resume path.pdf to score a specific PDF against the JD
Library: assess(job, profile) -> dict; credential_gaps(text, profile) -> list
"""
import argparse
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from ats_lint import keyword_score, extract_pdf_text  # noqa: E402

PROFILE_PATH = HERE / 'profile.json'
BANK_PATH = HERE / 'bank.json'
JOBS_PATH = HERE.parent / 'jobs.json'

# credentials he does NOT hold. Each: (label, regex over lowercased text).
# Kept in one place so the JD screen and the form-question screen agree.
MISSING_CREDENTIALS = [
    ("Master's degree", r"\b(master'?s?(?=\s+(?:degree|of|in|level|or|preferred|required|\(|s?\b))|mba|mph|msn|m\.s\.|m\.a\.|m\.h\.a\.|graduate degree)(?!\s*(?:data|schedul|plan|file|record|calendar|service|agreement|list))"),
    ('PMP/CAPM certification', r"\b(pmp|capm|pmi-acp|prince2|project management professional)\b"),
    ('Security clearance', r"\b(security clearance|secret clearance|ts/sci|public trust)\b"),
    ('RN / clinical license', r"\b(registered nurse|rn license|active rn|nursing license|licensed (?:clinical|practical|professional)|pharmd|physician license|md/do)\b"),
    ('Epic certification', r"\bepic[- ]certif"),
    ('ITIL certification', r"\bitil\b(?:\s*(?:v?[34]|foundation|certif))"),
    ('Scrum certification', r"\b(csm|psm|certified scrum ?master|safe agilist)\b"),
    ('Security certification', r"\b(cissp|cism|cisa|ccna|ccnp|security\+)\b"),
    ('Six Sigma', r"\b(six sigma|lean six sigma|black belt|green belt)\b"),
    ("Doctorate", r"\b(ph\.?d|doctorate|doctoral)\b"),
]
# a credential line counts as REQUIRED only in this context
REQUIRED_CTX = r"(required|must have|must possess|must be|must hold|must currently|hold an active|active (?:secret|top secret|ts)|minimum qualification|minimum requirement|is a requirement|need to have|needs to have|you have|you hold|you must|mandatory|essential)"
SOFT_CTX = r"(preferred|a plus|plus\b|nice to have|bonus|desired|ideally|or equivalent|equivalent experience|in lieu|strongly preferred|highly desirable)"
YEARS_RX = re.compile(r"(\d{1,2})\s*\+?\s*(?:-\s*\d{1,2}\s*)?(?:or more\s+)?(?:years?|yrs)\b")

LEVEL_RX = re.compile(r"\b(senior|sr\.?|lead|principal|staff|director|head of|vp|vice president|chief|iii|iv)\b")

# titles he has honestly held (career store) + approved target titles
HELD_TITLES = [
    'infrastructure operations engineer', 'project manager', 'project coordinator',
    'data analyst', 'business development representative',
    'pharmaceutical business analyst', 'business analyst',
]

# profile facts the screen needs (kept explicit so a JD can never "grant" them)
TOTAL_YEARS = 7          # Apr 2018 -> present
PM_YEARS = 4.5           # Oct 2020-Jan 2023 agency PM + HMH project work since Jun 2024


def _split_units(text: str) -> list[str]:
    """Bullets/lines/sentences: the unit a 'required' qualifier applies to."""
    t = re.sub(r'\r', '', text or '')
    units = re.split(r'\n+|(?<=[.;])\s+|•|·|•', t)
    return [u.strip() for u in units if u and u.strip()]


def credential_gaps(text: str, profile: dict | None = None,
                    require_ctx: bool = True) -> list[dict]:
    """Credentials named in `text` that he does not hold.

    require_ctx=True (JD mode): only units that read as a requirement, and
    not softened by 'preferred/or equivalent'. require_ctx=False (form
    question mode): any mention counts — "Do you meet ALL of the following:
    Master's degree..." is a requirement by construction."""
    gaps = []
    low = (text or '').lower()
    units = _split_units(low) if require_ctx else [low]
    for label, rx in MISSING_CREDENTIALS:
        crx = re.compile(rx)
        for u in units:
            if not crx.search(u):
                continue
            if require_ctx:
                if not re.search(REQUIRED_CTX, u):
                    continue
                if re.search(SOFT_CTX, u):
                    continue
                # "Bachelor's or Master's degree required" — he has the Bachelor's
                if label == "Master's degree" and re.search(r"bachelor", u):
                    continue
            gaps.append({'credential': label, 'evidence': u[:160]})
            break
    return gaps


def years_required(text: str) -> int | None:
    """Largest 'N+ years' figure that appears in a required context."""
    best = None
    for u in _split_units((text or '').lower()):
        if not re.search(REQUIRED_CTX + r"|experience", u):
            continue
        if re.search(SOFT_CTX, u):
            continue
        for m in YEARS_RX.finditer(u):
            n = int(m.group(1))
            if 1 <= n <= 25 and (best is None or n > best):
                best = n
    return best


def level_mismatch(role: str) -> str | None:
    m = LEVEL_RX.search((role or '').lower())
    return m.group(1) if m else None


def title_match(role: str, bank: dict | None = None) -> bool:
    r = re.sub(r'[^a-z ]', ' ', (role or '').lower())
    r = re.sub(r'\s+', ' ', r).strip()
    targets = [t.lower() for t in (bank or {}).get('target_titles', [])] + HELD_TITLES
    for t in targets:
        if t in r:
            return True
        # "IT Project Manager" ~ "Project Manager, Healthcare IT"
        core = t.replace('it ', '').replace('technical ', '').strip()
        if len(core) > 6 and core in r:
            return True
    return False


def posting_age_days(posted: str) -> int | None:
    if not posted:
        return None
    try:
        d = datetime.strptime(posted[:10], '%Y-%m-%d').date()
    except ValueError:
        return None
    return (date.today() - d).days


def assess(job: dict, profile: dict | None = None, bank: dict | None = None,
           resume_text: str | None = None) -> dict:
    """Full fit report for one job dict {role, description, posted, company}."""
    profile = profile or json.loads(PROFILE_PATH.read_text())
    bank = bank or (json.loads(BANK_PATH.read_text()) if BANK_PATH.exists() else {})
    role = job.get('role') or job.get('title') or ''
    jd = job.get('description') or ''
    reasons, knockouts, stretch = [], [], []

    for g in credential_gaps(jd, profile):
        knockouts.append(f"requires {g['credential']} (not held): \"{g['evidence'][:90]}\"")

    yrs = years_required(jd)
    if yrs is not None:
        if yrs >= 8:
            knockouts.append(f'requires {yrs}+ years (he has {TOTAL_YEARS})')
        elif yrs >= 5 and re.search(r'project|program', role.lower()):
            stretch.append(f'asks {yrs}+ years of PM experience (he has ~{PM_YEARS})')

    lvl = level_mismatch(role)
    if lvl:
        knockouts.append(f'level mismatch: "{lvl}" title (ghost-job zone; he is mid-level)')

    tmatch = title_match(role, bank)
    if not tmatch:
        stretch.append(f'title "{role}" matches no title he has held (Jobscan: 10.6x when it does)')

    age = posting_age_days(job.get('posted', ''))
    if age is not None:
        if age > 30:
            knockouts.append(f'posted {age} days ago (>30 = ghost-job signal)')
        elif age > 21:
            stretch.append(f'posted {age} days ago (late; first-100-applicants window closed)')

    kw = keyword_score(resume_text or _profile_haystack(profile), jd, role) if jd else \
        {'score': 0, 'present': [], 'missing': [], 'keywords': []}
    if jd and kw['score'] < 50:
        stretch.append(f"keyword echo {kw['score']}% (missing: {', '.join(kw['missing'][:6])})")

    verdict = 'skip' if knockouts else ('stretch' if stretch else 'apply')
    score = (0.4 * kw['score'] + (25 if tmatch else 0)
             + (15 if age is not None and age <= 7 else 5 if age is None else 0)
             + (20 if not stretch else 8))
    if knockouts:
        score = min(score, 25)
    return {
        'verdict': verdict, 'score': round(score),
        'knockouts': knockouts, 'stretch': stretch, 'reasons': reasons,
        'title_match': tmatch, 'posting_age_days': age, 'years_required': yrs,
        'keyword_score': kw['score'], 'keywords_missing': kw['missing'][:10],
        'keywords_present': kw['present'][:10],
    }


def _profile_haystack(profile: dict) -> str:
    return ' '.join([
        profile.get('current_title', ''), profile.get('summary', ''),
        ' '.join(profile.get('skills', [])), json.dumps(profile.get('experience', '')),
        ' '.join(profile.get('certifications', [])),
    ])


def fetch_jd(url: str) -> dict:
    """JD text straight from the board API when the URL is one we know."""
    import httpx
    m = re.search(r'greenhouse\.io/(?:embed/job_app\?for=)?([^/?#]+)/jobs/(\d+)', url) or \
        re.search(r'greenhouse\.io/embed/job_app\?for=([^&]+)&token=(\d+)', url)
    if m:
        r = httpx.get(f'https://boards-api.greenhouse.io/v1/boards/{m.group(1)}/jobs/{m.group(2)}',
                      timeout=20)
        if r.status_code == 200:
            j = r.json()
            from jd_extract import _strip_html
            return {'role': j.get('title', ''), 'company': m.group(1),
                    'description': _strip_html(j.get('content', '')),
                    'posted': (j.get('updated_at') or '')[:10]}
    m = re.search(r'lever\.co/([^/]+)/([0-9a-f-]{36})', url)
    if m:
        r = httpx.get(f'https://api.lever.co/v0/postings/{m.group(1)}/{m.group(2)}', timeout=20)
        if r.status_code == 200:
            j = r.json()
            return {'role': j.get('text', ''), 'company': m.group(1),
                    'description': j.get('descriptionPlain', ''), 'posted': ''}
    m = re.search(r'ashbyhq\.com/([^/]+)/([0-9a-f-]{36})', url)
    if m:
        r = httpx.get(f'https://api.ashbyhq.com/posting-api/job-board/{m.group(1)}', timeout=20)
        if r.status_code == 200:
            for j in r.json().get('jobs', []):
                if m.group(2) in (j.get('jobUrl', '') + j.get('applyUrl', '') + j.get('id', '')):
                    from jd_extract import _strip_html
                    return {'role': j.get('title', ''), 'company': m.group(1),
                            'description': _strip_html(j.get('descriptionHtml', '')) or j.get('descriptionPlain', ''),
                            'posted': (j.get('publishedAt') or '')[:10]}
    # anything else: readable text via Jina
    r = httpx.get(f'https://r.jina.ai/{url}', timeout=40, headers={'Accept': 'text/plain'})
    text = r.text if r.status_code == 200 else ''
    title = re.search(r'^Title:\s*(.+)$', text, re.M)
    return {'role': title.group(1).strip() if title else '', 'company': '',
            'description': text, 'posted': ''}


def format_report(job: dict, rep: dict) -> str:
    lines = [f"{job.get('company', '')} — {job.get('role', '')}",
             f"  VERDICT: {rep['verdict'].upper()}  (fit score {rep['score']}/100)"]
    for k in rep['knockouts']:
        lines.append(f"  [X] knockout: {k}")
    for s in rep['stretch']:
        lines.append(f"  [~] stretch:  {s}")
    lines.append(f"  title match: {'yes' if rep['title_match'] else 'NO'} | posted: "
                 f"{rep['posting_age_days'] if rep['posting_age_days'] is not None else '?'}d ago | "
                 f"years asked: {rep['years_required'] or '-'} | keyword echo {rep['keyword_score']}%")
    if rep['keywords_missing']:
        lines.append(f"  missing keywords: {', '.join(rep['keywords_missing'])}")
    return '\n'.join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--job-id', type=int)
    ap.add_argument('--jd-file')
    ap.add_argument('--url')
    ap.add_argument('--role', default='')
    ap.add_argument('--resume', help='score this PDF against the JD instead of profile.json')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--all-logged', action='store_true',
                    help='re-grade every job in jobs.json that has JD text')
    a = ap.parse_args()
    profile = json.loads(PROFILE_PATH.read_text())
    resume_text = None
    if a.resume:
        resume_text, _ = extract_pdf_text(a.resume)

    jobs = []
    if a.all_logged:
        db = json.loads(JOBS_PATH.read_text())
        jobs = [j for j in db['jobs'] if len(j.get('description') or '') > 300]
    elif a.job_id:
        db = json.loads(JOBS_PATH.read_text())
        jobs = [j for j in db['jobs'] if j.get('id') == a.job_id]
    elif a.jd_file:
        text = sys.stdin.read() if a.jd_file == '-' else Path(a.jd_file).read_text()
        jobs = [{'role': a.role, 'description': text, 'company': ''}]
    elif a.url:
        j = fetch_jd(a.url)
        if a.role:
            j['role'] = a.role
        jobs = [j]
    else:
        ap.print_help()
        return 2

    out = []
    for j in jobs:
        rep = assess(j, profile, resume_text=resume_text)
        out.append({'id': j.get('id'), 'company': j.get('company'), 'role': j.get('role'),
                    'status': j.get('status'), **rep})
        if not a.json:
            print(format_report(j, rep))
            print()
    if a.json:
        print(json.dumps(out, indent=1))
    if a.all_logged:
        from collections import Counter
        c = Counter(o['verdict'] for o in out)
        print(f"{len(out)} jobs graded: {dict(c)}")
    return 0 if all(o['verdict'] != 'skip' for o in out) else 1


if __name__ == '__main__':
    sys.exit(main())
