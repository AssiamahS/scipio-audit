#!/usr/bin/env python3
"""
Scipio Auto-Apply Engine
Applies to jobs using browser automation + AI.

Usage:
  uv run python applier.py apply <url> [--dry-run] [--headed] [--hold] [--resume <pdf>]
  uv run python applier.py batch [--dry-run] [--headed] [--limit N]
  uv run python applier.py score <url>

Modes:
  apply   - Apply to a single job URL
  batch   - Apply to all wishlist jobs in jobs.json
  score   - Score a job URL against your profile (no apply)

Options:
  --dry-run     Fill forms but don't submit (screenshot instead)
  --headed      Show browser window (default: headless)
  --hold        Fill everything, then leave the browser open so you can
                review and click Submit yourself (implies no auto-submit;
                use with --headed)
  --resume PDF  Use this resume file for this application instead of the
                profile.json default
  --limit N     Max jobs to apply to in batch mode (default: 5)
"""

import asyncio
import json
import re
import sys
import os
import time
import base64
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from playwright.async_api import async_playwright

from jd_extract import extract_jd

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from ats_handlers import (
    fill_application, detect_ats, repair_required_fields,
    audit_required_fields, reset_qa_log, qa_note, QA_LOG,
)
from ai_engine import (
    answer_question, generate_cover_letter, score_job_match,
    get_quick_answer, classify_question, get_client
)
from ats_lint import extract_pdf_text, keyword_score
import captcha
from pacing import (
    within_business_hours, eastern_clock, start_jitter_seconds,
    between_apps_seconds, pick_fingerprint, board_key,
    todays_attempts_per_board, captcha_present, BOARD_DAILY_CAP,
    _norm_url, posting_age_days,
)

PROFILE_PATH = Path(__file__).parent / 'profile.json'
JOBS_PATH = Path(__file__).parent.parent / 'jobs.json'
SCREENSHOTS_DIR = Path(__file__).parent / 'screenshots'
LOG_PATH = Path(__file__).parent / 'apply_log.json'

# GitHub API config for syncing
GITHUB_OWNER = 'AssiamahS'
GITHUB_REPO = 'scipio'


def launch_kwargs(**extra) -> dict:
    """Browser launch options. PROXY_URL (e.g. a Tailscale/residential
    exit) routes all page traffic through that proxy — greenhouse and
    ashby CAPTCHA-score datacenter IPs, so the runner needs a way out.
    Format: http://host:port or http://user:pass@host:port."""
    kwargs = dict(extra)
    proxy_url = os.environ.get('PROXY_URL', '').strip()
    if proxy_url:
        from urllib.parse import urlsplit
        parts = urlsplit(proxy_url)
        proxy = {'server': f"{parts.scheme}://{parts.hostname}:{parts.port or 8080}"}
        if parts.username:
            proxy['username'] = parts.username
            proxy['password'] = parts.password or ''
        kwargs['proxy'] = proxy
        print(f"  [*] Proxying browser traffic via {proxy['server']}")
    return kwargs


PROFILE_DIR = Path.home() / '.slyci' / 'chrome-profile'
# 9/9 floors: refuse only what is plainly off-lane. The old 5/10 match floor
# and the 70% keyword-echo gate together retired most of the wishlist
# (9/7-9/9: 11 direct title matches -> weak_match, 0 attempts on 9/9).
MATCH_FLOOR = 3          # AI match score below this never ships
APPLY_ECHO_FLOOR = 45    # keyword echo % below this never ships (70 stays the lint target)


async def open_browser(p, headless: bool = True):
    """Real Google Chrome with a persistent profile when it is installed
    (apply.yml installs it on the box), stock Chromium otherwise.

    8/14 diagnosis: greenhouse/lever invisible reCAPTCHA scores the browser
    fingerprint and behaviour, not the IP. A fresh headless Chromium with a
    spoofed UA every run is the lowest-trust profile there is; a real Chrome
    build with cookies and storage that persist between wakes is what a
    person's browser looks like. No captcha is solved or bypassed here: a
    challenge that still appears parks the job as manual_review as before.
    Returns (context, closer) where closer() shuts everything down."""
    channel = os.environ.get('BROWSER_CHANNEL', 'chrome')
    kwargs = launch_kwargs(headless=headless)
    if channel and channel != 'chromium':
        try:
            PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            ua, viewport = pick_fingerprint()
            ctx = await p.chromium.launch_persistent_context(
                str(PROFILE_DIR), channel=channel, viewport=viewport,
                locale='en-US', timezone_id='America/New_York', **kwargs)
            print(f"  [*] Browser: {channel} with persistent profile {PROFILE_DIR}")

            async def _close():
                await ctx.close()
            return ctx, _close
        except Exception as e:
            print(f"  [~] {channel} channel unavailable ({str(e).splitlines()[0][:90]}); falling back to chromium")
    browser = await p.chromium.launch(**kwargs)
    ua, viewport = pick_fingerprint()
    ctx = await browser.new_context(viewport=viewport, user_agent=ua,
                                    locale='en-US', timezone_id='America/New_York')

    async def _close():
        await browser.close()
    return ctx, _close


def drop_proxy_if_degraded() -> None:
    """DataImpulse exits degrade to where a tiny curl succeeds but a real
    page load times out in the browser (net::ERR_TIMED_OUT on every goto,
    8/11 and 8/21). The pool also mixes in the odd broken exit (SSL-
    intercepting or dead) even on a healthy day — 8/23 measured 1 bad in 3.
    A single probe therefore can't tell "pool degraded" from "unlucky exit",
    and one unlucky probe used to send the whole run direct — which from a
    datacenter IP walls every greenhouse submit behind a CAPTCHA (8/21–8/23:
    0 submits, all manual_review). Probe a full ATS page 3x (each request is
    a fresh exit); keep the proxy on a 2/3 majority, run direct only when
    the pool really can't carry pages."""
    import httpx
    proxy_url = os.environ.get('PROXY_URL', '').strip()
    if not proxy_url:
        return
    probe = 'https://job-boards.greenhouse.io/precisionmedicinegroup'
    ok, reasons = 0, []
    for _ in range(3):
        try:
            try:
                r = httpx.get(probe, proxy=proxy_url, timeout=25, follow_redirects=True)
            except TypeError:  # older httpx spells it proxies=
                r = httpx.get(probe, proxies=proxy_url, timeout=25, follow_redirects=True)
            if r.status_code < 500:
                ok += 1
            else:
                reasons.append(f'status {r.status_code}')
        except Exception as e:
            reasons.append(type(e).__name__)
    if ok >= 2:
        if reasons:
            print(f"  [*] Proxy healthy {ok}/3 (flaky exits: {', '.join(reasons)}) — keeping PROXY_URL")
        return
    print(f"  [!] PROXY_URL failed real-page probes ({ok}/3 ok: {', '.join(reasons)}) — running direct this run")
    os.environ['PROXY_URL'] = ''


def load_profile() -> dict:
    profile = json.loads(PROFILE_PATH.read_text())
    # resume_path may be an absolute path from another machine (CI runs in
    # a fresh checkout) — fall back to the repo's resumes/ dir by basename
    rp = Path(profile.get('resume_path', ''))
    if profile.get('resume_path') and not rp.exists():
        candidate = PROFILE_PATH.parent.parent / 'resumes' / rp.name
        if candidate.exists():
            profile['resume_path'] = str(candidate)
            print(f"  [*] Resume resolved to repo copy: {candidate.name}")
        else:
            print(f"  [!] Resume not found: {rp}")
    return profile


RESUMES_DIR = Path(__file__).parent.parent / 'resumes'

# per-job resume picker: filename keyword -> resume basename. First match on
# company+role+JD wins; fall back to the profile.json default.
RESUME_RULES = [
    (('eclinical',), 'Resume - Sylvester Assiamah (eClinical Project Manager).pdf'),
    # 9/12 sector bases: finance-titled roles get the finance-first variant
    (('financial analyst', 'finance analyst', 'fp&a', 'pricing analyst', 'budget analyst',
      'procurement', 'cost analyst', 'billing analyst', 'revenue operations', 'accounting',
      'vendor management analyst', 'contract operations'),
     'Resume - Sylvester Assiamah (Finance).pdf'),
    (('connectionshealth', 'connections health'), 'Resume - Sylvester Assiamah (CHS Project Manager).pdf'),
    # infra/systems/identity roles get the infra-first variant
    (('infrastructure engineer', 'systems engineer', 'system engineer',
      'systems administrator', 'system administrator', 'it operations',
      'iam ', 'identity and access', 'identity engineer', 'sailpoint',
      'okta', 'site reliability', 'clinical systems', 'systems analyst',
      'desktop support', 'technical support engineer'),
     'Resume - Sylvester Assiamah (Infrastructure Engineer).pdf'),
]


def choose_resume_by_ats(job_info: dict | None) -> tuple[str, int] | None:
    """Score every resume variant's literal keyword echo against this JD and
    return the best one. Beats a hand-written filename rule because it reads
    the actual PDF text an ATS would see."""
    jd = (job_info or {}).get('description') or ''
    if len(jd) < 400 or not RESUMES_DIR.exists():
        return None
    best = None
    for pdf in sorted(RESUMES_DIR.glob('*.pdf')):
        try:
            text, _ = extract_pdf_text(str(pdf))
            score = keyword_score(text, jd, (job_info or {}).get('role', ''))['score']
        except Exception:
            continue
        if best is None or score > best[1]:
            best = (str(pdf), score)
    return best


def tailor_resume_for(job_info: dict | None, job_id=None) -> dict | None:
    """Build a per-JD tailored resume from the approved bank (tailor.py picks
    variant wordings that echo the JD — selection, never writing). Runs as a
    subprocess because tailor uses sync playwright and this loop is async.
    Returns the manifest dict, or None to fall back to the variant picker."""
    import subprocess
    import sys as _sys
    import tempfile
    jd = (job_info or {}).get('description') or ''
    if len(jd) < 300:
        return None
    company = re.sub(r'[^A-Za-z0-9]+', '_', (job_info or {}).get('company') or 'job').strip('_')[:40]
    out = Path(__file__).parent / 'resumes_tailored' / f"{company}_{job_id or 'adhoc'}.pdf"
    try:
        with tempfile.NamedTemporaryFile('w', suffix='.txt', delete=False) as f:
            f.write(jd)
            jd_file = f.name
        r = subprocess.run(
            [_sys.executable, str(Path(__file__).parent / 'tailor.py'),
             '--jd-file', jd_file, '--role', (job_info or {}).get('role') or '',
             '--out', str(out)],
            capture_output=True, text=True, timeout=120)
        # parse from the first '{': any stray print ahead of the manifest used
        # to make json.loads throw and silently disable tailoring for weeks
        brace = r.stdout.find('{')
        if brace < 0:
            print(f"  [!] tailor produced no manifest; stdout: {r.stdout.strip()[:120]!r} "
                  f"stderr: {r.stderr.strip()[-120:]!r}")
            return None
        manifest = json.loads(r.stdout[brace:])
        return manifest if manifest.get('pdf') and Path(manifest['pdf']).exists() else None
    except Exception as e:
        print(f"  [!] tailor unavailable ({e})")
        return None


def prepare_resume(profile: dict, job_info: dict | None, job_id=None) -> tuple[dict, dict | None]:
    """Per-JD resume prep, FAIL CLOSED. Returns (job_profile, block).

    block is None when it's safe to proceed. When the JD is readable but no
    validated tailored artifact could be produced, block carries a
    blocked_* status and the application must NOT be attempted — sending a
    generic resume silently is how 29 untailored applications shipped."""
    job_profile = dict(profile)
    jd = (job_info or {}).get('description') or ''
    if len(jd) >= 300:
        # fit_check: a JD that requires a credential he does not hold is a
        # guaranteed reject and an application he would have to lie on
        # (Blue Water 7/22-7/30). Block it here, before any form is touched.
        try:
            from fit_check import assess
            rep = assess(job_info, profile)
            hard = [k for k in rep['knockouts'] if not k.startswith('level mismatch')]
            if hard:
                return job_profile, {'status': 'blocked_knockout',
                                     'reason': '; '.join(hard)[:300], 'fit': rep}
            if rep['knockouts']:
                print(f"  [~] fit warning: {rep['knockouts'][0][:90]} (queued by hand, proceeding)")
            job_profile['fit_report'] = rep
        except Exception as e:
            print(f"  [!] fit_check unavailable ({e}); continuing")
        t = tailor_resume_for(job_info, job_id)
        if not t:
            return job_profile, {
                'status': 'blocked_no_tailor',
                'reason': 'JD readable but tailoring produced no artifact — refusing to send a generic resume'}
        if not t.get('truth_ok', False):
            return job_profile, {
                'status': 'blocked_truth',
                'reason': 'tailored artifact failed truth/attribution validation',
                'truth_failures': t.get('truth_failures', [])}
        if not t.get('rules_ok', True):
            # RESUME_RULES.md: a resume that fails a rule is not sent, ever.
            # Fix the bank (usually: approve a pending bullet), then retry.
            return job_profile, {
                'status': 'blocked_rules',
                'reason': 'tailored artifact violates RESUME_RULES.md',
                'rules_failures': t.get('rules_failures', [])}
        job_profile['resume_path'] = t['pdf']
        job_profile['resume_variant'] = Path(t['pdf']).name
        job_profile['resume_tailor_choices'] = t.get('choices')
        job_profile['resume_tailored_flag'] = True
        print(f"  [*] Resume TAILORED to this JD: {Path(t['pdf']).name} "
              f"({len(t.get('choices') or [])} bullets selected, truth-validated)")
    else:
        # no readable JD to tailor against — best static variant, flagged
        # untailored so the scorecard keeps it visible
        picked = choose_resume(profile, job_info)
        if picked:
            job_profile['resume_path'] = picked
            print(f"  [*] No JD to tailor against — static variant: {Path(picked).name}")
        job_profile['resume_tailored_flag'] = False
    return job_profile, None


def choose_resume(profile: dict, job_info: dict | None) -> str | None:
    """Pick the best resume variant for this job. Returns a path or None to
    keep the profile default."""
    if not job_info:
        return None
    picked = choose_resume_by_ats(job_info)
    if picked:
        print(f"  [*] Resume by ATS score: {Path(picked[0]).name} ({picked[1]}%)")
        return picked[0]
    hay = ' '.join([
        job_info.get('company', ''), job_info.get('role', ''),
        (job_info.get('description') or '')[:500],
    ]).lower()
    for keywords, basename in RESUME_RULES:
        if any(k in hay for k in keywords):
            candidate = RESUMES_DIR / basename
            if candidate.exists():
                return str(candidate)
    return None


UPLOAD_NAME = 'Sylvester-Assiamah-Resume.pdf'


def stage_upload_copy(profile: dict) -> None:
    """ATS-friendly upload: recruiters and parsers see a clean canonical
    filename, while `resume_variant` keeps the real variant name for the
    log/report cards (their links depend on the variant filename)."""
    import shutil
    import tempfile
    src = Path(profile.get('resume_path', ''))
    if not src.exists():
        return
    profile['resume_variant'] = src.name
    staged = Path(tempfile.mkdtemp(prefix='scipio_resume_')) / UPLOAD_NAME
    try:
        shutil.copyfile(src, staged)
        profile['resume_path'] = str(staged)
    except Exception:
        pass  # upload the variant under its own name rather than fail


def load_jobs() -> dict:
    if JOBS_PATH.exists():
        return json.loads(JOBS_PATH.read_text())
    return {"jobs": [], "next_id": 1}


def save_jobs(db: dict):
    JOBS_PATH.write_text(json.dumps(db, indent=2))


def company_from_url(url: str) -> str | None:
    """Board slug from an ATS URL — greenhouse/lever/ashby put the org in the
    first path segment, workday in the subdomain."""
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(url)
        host, segs = parts.netloc.lower(), [s for s in parts.path.split('/') if s]
        if any(d in host for d in ('greenhouse.io', 'lever.co', 'ashbyhq.com')):
            seg = segs[0] if segs else None
            return None if seg in (None, 'embed', 'jobs') else seg
        if 'myworkdayjobs.com' in host:
            return host.split('.')[0]
    except Exception:
        pass
    return None


# execution classes: what actually happened, independent of optimism.
# UNCONFIRMED must never be presented as a successful application — four
# unconfirmed Humana clicks are four attempts, not one submission.
EXECUTION_CLASS = {
    'submitted': 'CONFIRMED',
    'submit_unconfirmed': 'UNCONFIRMED',
    'filled_not_submitted': 'FAILED',
    'manual_review': 'MANUAL',
    'needs_account': 'MANUAL',
    'needs_code': 'MANUAL',
    'held_for_review': 'MANUAL',
    'blocked_no_tailor': 'BLOCKED',
    'blocked_truth': 'BLOCKED',
    'blocked_rules': 'BLOCKED',
    'blocked_knockout': 'BLOCKED',
    'hand_apply': 'MANUAL',
    'weak_match': 'BLOCKED',
    'interrupted': 'MANUAL',
    'closed': 'EXPIRED',
    'no_form': 'FAILED',
    'error': 'FAILED',
    'dry_run': 'DRY',
}


# Written the instant before a submit click, cleared when the outcome is
# logged. Gitignored on purpose: slyci re-checks-out with `checkout -f`, which
# resets TRACKED files (a dead run's uncommitted apply_log entries vanish) but
# leaves untracked ones — so this survives on the box between wakes. If a wake
# dies between click and log, the next wake finds it and parks the job as
# submitted_unverified instead of submitting it again.
IN_FLIGHT_PATH = Path(__file__).parent / 'in_flight.json'
JOB_TIMEOUT_S = int(os.environ.get('JOB_TIMEOUT_S') or 480)


def mark_in_flight(url: str, job_info: dict | None) -> None:
    IN_FLIGHT_PATH.write_text(json.dumps({
        'url': url,
        'company': (job_info or {}).get('company', ''),
        'role': (job_info or {}).get('role', ''),
        'timestamp': datetime.now().isoformat(),
    }, indent=2))


def read_in_flight(url: str | None = None) -> dict | None:
    if not IN_FLIGHT_PATH.exists():
        return None
    try:
        d = json.loads(IN_FLIGHT_PATH.read_text())
    except Exception:
        return None
    if url and _norm_url(d.get('url', '')) != _norm_url(url):
        return None
    return d


def clear_in_flight(url: str) -> None:
    if read_in_flight(url):
        IN_FLIGHT_PATH.unlink(missing_ok=True)


def recover_in_flight(db: dict) -> None:
    """A previous wake died after clicking submit. Never re-submit: park it."""
    d = read_in_flight()
    if not d:
        return
    job = next((j for j in db['jobs'] if _norm_url(j.get('url', '')) == _norm_url(d.get('url', ''))), None)
    print(f"  [!] Interrupted submit from a previous wake: {d.get('company')} — "
          f"{d.get('role')} ({d.get('timestamp', '')[:16]}) — parking as submitted_unverified, never retrying")
    log_application(job['id'] if job else 0, d.get('company', ''), d.get('role', ''), d.get('url', ''),
                    'interrupted', {'note': 'wake died between submit click and log write',
                                    'clicked_at': d.get('timestamp')})
    if job and job.get('status') in ('wishlist', 'resume_ready'):
        update_job_status(job['id'], 'submitted_unverified')
    IN_FLIGHT_PATH.unlink(missing_ok=True)


async def apply_with_timeout(page, url: str, profile: dict, job_info: dict | None, dry_run: bool) -> dict:
    """One broken ATS must not eat the whole wake: hard per-application cap.
    If the cap fires after the submit click, the outcome is unknown — report
    submit_unconfirmed so the job parks instead of being retried."""
    try:
        return await asyncio.wait_for(apply_to_url(page, url, profile, job_info, dry_run),
                                      timeout=JOB_TIMEOUT_S)
    except asyncio.TimeoutError:
        clicked = read_in_flight(url) is not None
        print(f"  [!] Application exceeded {JOB_TIMEOUT_S}s — "
              f"{'submit was clicked, parking' if clicked else 'abandoned'}")
        return {"status": "submit_unconfirmed" if clicked else "error",
                "error": f"job timeout after {JOB_TIMEOUT_S}s", "ats": "unknown",
                "fields_filled": 0, "timed_out": True}


def log_application(job_id: int, company: str, role: str, url: str, status: str, details: dict):
    clear_in_flight(url)
    """Append to application log."""
    logs = []
    if LOG_PATH.exists():
        logs = json.loads(LOG_PATH.read_text())

    # every attempt carries its Q&A audit, even a blocked or errored one —
    # an entry without the field is indistinguishable from a lost audit
    details.setdefault("qa", [])

    logs.append({
        "timestamp": datetime.now().isoformat(),
        "job_id": job_id,
        "company": company,
        "role": role,
        "url": url,
        "status": status,
        "execution_class": EXECUTION_CLASS.get(status, 'UNKNOWN'),
        "details": details
    })
    LOG_PATH.write_text(json.dumps(logs, indent=2))


def update_job_status(job_id: int, new_status: str):
    """Update a job's status in jobs.json."""
    db = load_jobs()
    for job in db["jobs"]:
        if job["id"] == job_id:
            job["status"] = new_status
            job["updated_date"] = datetime.now().strftime("%Y-%m-%d")
            job.setdefault("history", []).append({
                "status": new_status,
                "date": datetime.now().strftime("%Y-%m-%d %H:%M")
            })
            break
    save_jobs(db)


def save_job_description(job_id: int, description: str):
    """Store the JD text captured during an apply, first capture wins."""
    db = load_jobs()
    for job in db["jobs"]:
        if job["id"] == job_id:
            if description and not job.get("description"):
                job["description"] = description[:12000]
                save_jobs(db)
            break


async def sync_to_github(db: dict):
    """Push updated jobs.json to GitHub."""
    token = os.environ.get('GITHUB_TOKEN')
    if not token:
        print("  [!] No GITHUB_TOKEN set, skipping sync")
        return

    import httpx

    headers = {
        'Authorization': f'token {token}',
        'Accept': 'application/vnd.github.v3+json',
    }

    api_url = f'https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/jobs.json'

    async with httpx.AsyncClient() as client:
        # Get current SHA
        r = await client.get(api_url, headers=headers)
        sha = r.json().get('sha') if r.status_code == 200 else None

        # Update file
        content = base64.b64encode(json.dumps(db, indent=2).encode()).decode()
        body = {
            "message": f"Auto-apply update - {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            "content": content,
        }
        if sha:
            body["sha"] = sha

        r = await client.put(api_url, headers=headers, json=body)
        if r.status_code in (200, 201):
            print("  [+] Synced to GitHub")
        else:
            print(f"  [!] GitHub sync failed: {r.status_code}")


async def apply_to_url(page, url: str, profile: dict, job_info: dict = None, dry_run: bool = False) -> dict:
    """Navigate to a job URL and apply."""
    import hashlib
    import uuid
    rp = profile.get('resume_path', '')
    jd_text = (job_info or {}).get('description') or ''
    result = {
        "url": url,
        "status": "unknown",
        "ats": "unknown",
        "fields_filled": [],
        "fields_missed": [],
        "screenshot": None,
        "resume": profile.get('resume_variant') or Path(profile.get('resume_path', '')).name or None,
        "resume_tailored": profile.get('resume_tailor_choices'),
        # immutable artifact record: six months from now, "what exactly did
        # we send?" must be answerable by hash, not by variant nickname
        "application_id": uuid.uuid4().hex[:12],
        "resume_sha256": (hashlib.sha256(Path(rp).read_bytes()).hexdigest()
                          if rp and Path(rp).exists() else None),
        "jd_sha256": (hashlib.sha256(jd_text.encode()).hexdigest() if jd_text else None),
        "tailored": bool(profile.get('resume_tailored_flag')),
    }

    reset_qa_log()
    try:
        print(f"  [*] Navigating to {url}")
        await page.goto(url, wait_until='domcontentloaded', timeout=60000)
        await asyncio.sleep(2)  # Let JS render

        # Detect ATS
        ats = await detect_ats(page)
        result["ats"] = ats
        print(f"  [*] Detected ATS: {ats}")

        # Capture the job description before the form takes over the page
        try:
            jd_hit = await extract_jd(page)
            result["description"] = jd_hit.get("description", "")[:12000]
            result["jd_source"] = jd_hit.get("jd_source")
            result["jd_company"] = jd_hit.get("company")
            result["jd_role"] = jd_hit.get("role")
        except Exception:
            pass

        # Score this resume against the JD so the report card can flag weak
        # matches. Non-fatal: a scoring failure never blocks the apply.
        try:
            jd = (job_info or {}).get('description') or result.get('description')
            if jd:
                match = score_job_match(profile, {
                    **(job_info or {}), 'description': jd,
                })
                result["match_score"] = match.get('score')
                # floor: the model rated garnerhealth 1/10 and natera 3/10 and
                # both still shipped (HR audit). Below 5 we do not apply.
                try:
                    # 9/9: the 5/10 floor was retiring direct title matches
                    # (Marathon Technical PM, IQVIA Sr TPM, Humana PM all sat
                    # at 4-5 from a free model). Only garbage (<3) is refused.
                    if not dry_run and int(match.get('score') or 0) < MATCH_FLOOR \
                            and match.get('reason') != 'Could not score':
                        result["status"] = "weak_match"
                        print(f"  [~] Match {match.get('score')}/10 < {MATCH_FLOOR} — not applying ({str(match.get('reason'))[:80]})")
                        return result
                except (TypeError, ValueError):
                    pass
                result["match_reason"] = match.get('reason')
                result["matching_skills"] = match.get('matching_skills', [])
                print(f"  [*] Match score: {result['match_score']}/10 — {result['match_reason']}")
        except Exception as e:
            print(f"  [!] Scoring failed (continuing): {e}")

        # Deterministic ATS keyword coverage: does the resume we're about to
        # upload literally echo the JD's skills/tools? Logged per application
        # so the report card and phone app can flag weak echoes.
        try:
            jd = (job_info or {}).get('description') or result.get('description')
            rp = profile.get('resume_path', '')
            if jd and rp and Path(rp).exists():
                resume_text, _ = extract_pdf_text(rp)
                kw = keyword_score(resume_text, jd, (job_info or {}).get('role', ''))
                result["ats_score"] = kw['score']
                result["ats_missing"] = kw['missing'][:12]
                print(f"  [*] ATS keyword coverage: {kw['score']}% "
                      f"(missing: {', '.join(kw['missing'][:6]) or 'none'})")
        except Exception as e:
            print(f"  [!] ATS lint failed (continuing): {e}")

        # ashby's /application route lands straight on the form — no
        # Apply-button hunt, no iframe
        if ats == 'ashby' and '/application' not in page.url:
            app_url = page.url.split('?')[0].rstrip('/') + '/application'
            print(f"  [*] Ashby: jumping to application form: {app_url[:80]}")
            await page.goto(app_url, wait_until='domcontentloaded', timeout=60000)
            try:
                await page.wait_for_selector('#_systemfield_name', timeout=15000)
            except Exception:
                body = ''
                try:
                    body = await page.locator('body').inner_text(timeout=3000)
                except Exception:
                    pass
                if 'job not found' in body.lower():
                    result["status"] = "closed"
                    print("  [!] Posting is closed (Job not found) — skipping")
                    return result
                print("  [!] Ashby form fields never appeared (posting may be closed)")

        # icims hides the whole form in an iframe — enter it or every
        # handler sweep sees an empty page
        if 'icims.com' in page.url.lower() or await page.locator('iframe[src*="icims.com"]').count() > 0:
            try:
                iframe = page.locator('#icims_content_iframe, iframe[src*="icims.com"]').first
                if await iframe.count() > 0:
                    src = await iframe.get_attribute('src')
                    if src:
                        print(f"  [*] iCIMS: entering form iframe: {src[:80]}")
                        await page.goto(src, wait_until='domcontentloaded', timeout=60000)
                        await asyncio.sleep(2)
            except Exception:
                pass

        # Workday: its own multi-step flow (account, verification, 6 pages,
        # submit) lives in workday.py — four unconfirmed Humana clicks in July
        # were this generic path pretending the sign-in wall was a form
        if ats == 'workday' or 'myworkdayjobs.com' in page.url.lower():
            from workday import apply as workday_apply
            print("  [*] Workday flow")

            def _wd_answer(label):
                return get_quick_answer(label, profile) or answer_question(label, profile, job_info)
            if not dry_run:
                mark_in_flight(url, job_info)
            wd = await workday_apply(page, url, profile, job_info, dry_run, ai_answer=_wd_answer)
            result.update({k: v for k, v in wd.items() if k not in ('url',)})
            result["fields_filled"] = wd.get('filled', [])
            result["fields_missed"] = wd.get('missed', [])
            result["ats"] = 'workday'
            SCREENSHOTS_DIR.mkdir(exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            company = (job_info or {}).get("company", "unknown").replace(" ", "_")[:20]
            ss_path = SCREENSHOTS_DIR / f"{company}_{ts}.png"
            try:
                await page.screenshot(path=str(ss_path), full_page=True)
                result["screenshot"] = str(ss_path)
            except Exception:
                pass
            if not dry_run and wd.get('status') != 'submitted':
                pass  # in_flight cleared by the caller's normal path below
            if wd.get('status') in ('submitted', 'dry_run', 'closed'):
                clear_in_flight(url)
            return result

        # Look for "Apply" button on job listing pages
        apply_btn = page.locator(
            'a:has-text("Apply"), button:has-text("Apply"), '
            'a:has-text("Apply Now"), button:has-text("Apply Now"), '
            'a:has-text("Apply for this job"), button:has-text("Submit Application"), '
            '[data-automation-id="jobPostingApplyButton"]'
        ).first

        try:
            if ats != 'ashby' and await apply_btn.is_visible(timeout=3000):
                print("  [*] Clicking Apply button...")
                await apply_btn.click()
                await asyncio.sleep(3)
        except Exception:
            print("  [*] No Apply button found, assuming we're on the form")

        # Company career pages often embed the real form in an iframe
        # (greenhouse embed) — jump straight into the embed page so the
        # fill handlers can actually see the fields
        already_on_ats = any(d in page.url.lower() for d in ('greenhouse.io', 'lever.co', 'myworkdayjobs.com', 'ashbyhq.com'))
        if not already_on_ats:
            try:
                iframe = page.locator('#grnhse_iframe, iframe[src*="boards.greenhouse.io"], iframe[src*="jobs.lever.co"]').first
                if await iframe.count() > 0:
                    src = await iframe.get_attribute('src')
                    if src:
                        print(f"  [*] Form is in an iframe — entering embed: {src[:80]}")
                        await page.goto(src, wait_until='domcontentloaded', timeout=60000)
                        await asyncio.sleep(2)
                        result["ats"] = await detect_ats(page)
            except Exception:
                pass

        # Fill the application
        print("  [*] Filling application form...")
        fill_result = await fill_application(page, profile)
        result["fields_filled"] = fill_result.get("filled", [])
        result["fields_missed"] = fill_result.get("missed", [])
        result["ats"] = fill_result.get("ats", ats)

        # Handle any custom text questions with AI
        await handle_custom_questions(page, profile, job_info)

        print(f"  [+] Filled {len(result['fields_filled'])} fields")
        if result["fields_missed"]:
            print(f"  [!] Missed: {', '.join(result['fields_missed'])}")

        # Screenshot
        SCREENSHOTS_DIR.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        company = (job_info or {}).get("company", "unknown").replace(" ", "_")[:20]
        ss_path = SCREENSHOTS_DIR / f"{company}_{ts}.png"
        await page.screenshot(path=str(ss_path), full_page=True)
        result["screenshot"] = str(ss_path)
        print(f"  [*] Screenshot: {ss_path}")

        if dry_run:
            result["status"] = "dry_run"
            print("  [~] DRY RUN - not submitting")
        elif not result["fields_filled"]:
            # nothing was filled — clicking submit here would be a lie
            result["status"] = "no_form"
            print("  [!] Nothing filled - no real application form on this page")
            # workday hides the form behind account creation — name that
            # instead of pretending there was no form
            if result.get("ats") == "workday":
                try:
                    body = (await page.locator('body').inner_text(timeout=3000)).lower()
                    if any(k in body for k in ('sign in', 'create account', 'sign up')):
                        result["status"] = "needs_account"
                        print("  [!] Workday sign-in wall — needs an account (apply by hand or add credentials)")
                except Exception:
                    pass
        else:
            # Find and click submit
            if not dry_run:
                mark_in_flight(url, job_info)
            # lever/ashby render the widget before submit — solve it first so
            # the click carries a token (9/9: no human step, ever)
            if not dry_run and await captcha.widget(page):
                await captcha.solve(page)
            submitted = await click_submit(page)
            if submitted:
                # trust the page, not the click: poll for a confirmation
                confirmed = await wait_confirmed(page)

                # A challenge after the click: solve it and submit again.
                # Still walled -> the job retries on a later wake
                # (requeue.py), never a hand-apply list.
                captcha_blocked = not confirmed and await captcha_present(page)
                if captcha_blocked and not dry_run and await captcha.solve(page):
                    await click_submit(page)
                    confirmed = await wait_confirmed(page)
                    captcha_blocked = not confirmed and await captcha_present(page)
                if captcha_blocked:
                    print("  [!] CAPTCHA still on page — will retry on a later wake")

                # validation errors block greenhouse submits silently —
                # repair every flagged required field and resubmit once
                if not confirmed and not captcha_blocked:
                    def _answer_for(label):
                        return get_quick_answer(label, profile) or \
                            answer_question(label, profile, job_info)
                    repaired = await repair_required_fields(page, profile, _answer_for)
                    if repaired:
                        print(f"  [*] Repaired {repaired} required fields flagged by validation - resubmitting")
                        result["fields_filled"].append(f"repaired:{repaired}")
                        await click_submit(page)
                        confirmed = await wait_confirmed(page)

                # greenhouse anti-bot: emails a security code, expects it
                # entered + resubmit
                if not confirmed and not captcha_blocked:
                    import re as _re
                    code_input = page.get_by_label(_re.compile('security code', _re.I)).or_(
                        page.locator('input[id*="security" i], input[name*="security" i], '
                                     'input[aria-label*="security" i]')).first
                    code_visible = False
                    try:
                        code_visible = await code_input.is_visible(timeout=3000)
                    except Exception:
                        pass
                    if code_visible:
                        result["code_requested"] = True
                        code = await get_security_code(profile)
                        if code:
                            # segmented one-char-per-box inputs: click the
                            # first box and type — auto-advance spreads it
                            await code_input.click()
                            await page.keyboard.type(code, delay=80)
                            await asyncio.sleep(1)
                            await click_submit(page)
                            confirmed = await wait_confirmed(page)

                await page.screenshot(path=str(SCREENSHOTS_DIR / f"{company}_{ts}_submitted.png"), full_page=True)
                if confirmed:
                    result["status"] = "submitted"
                    print("  [+] APPLICATION SUBMITTED (confirmation seen on page)")
                elif captcha_blocked:
                    result["status"] = "manual_review"
                    print("  [!] CAPTCHA gate — retrying on a later wake (set CAPTCHA_API_KEY to solve challenges)")
                else:
                    result["status"] = "submit_unconfirmed"
                    print("  [!] Clicked submit but no confirmation appeared - NOT counting as submitted (check screenshot)")
                    # name the exact questions still blocking the submit
                    result["unanswered_required"] = await audit_required_fields(page)
            elif await captcha_present(page):
                # the CAPTCHA modal can block the submit click itself — solve
                # it and click again before giving this wake up
                if not dry_run and await captcha.solve(page) and await click_submit(page):
                    if await wait_confirmed(page):
                        result["status"] = "submitted"
                        print("  [+] APPLICATION SUBMITTED after solving the CAPTCHA modal")
                    else:
                        result["status"] = "submit_unconfirmed"
                        print("  [!] Submitted after CAPTCHA but no confirmation seen")
                else:
                    result["status"] = "manual_review"
                    print("  [!] CAPTCHA modal blocking submit — retrying on a later wake")
            else:
                result["status"] = "filled_not_submitted"
                print("  [!] Form filled but could not find submit button")

        # QA audit: everything this form asked, how it was answered, and
        # what's still required — goes to CI logs AND apply_log.json
        result["qa"] = list(QA_LOG)
        print(f"  [QA] {len(result['qa'])} question(s) answered:")
        for q in result["qa"]:
            print(f"    OK {q['kind']}: {q['label'][:70]} = {q['answer'][:50]} ({q['source']})")
        for q in result.get("unanswered_required", []):
            print(f"    ** STILL REQUIRED {q['kind']}: {q['label'][:80]}")

    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)
        result.setdefault("qa", list(QA_LOG))
        print(f"  [!] Error: {e}")

        # Error screenshot
        try:
            SCREENSHOTS_DIR.mkdir(exist_ok=True)
            await page.screenshot(path=str(SCREENSHOTS_DIR / f"error_{datetime.now().strftime('%H%M%S')}.png"))
        except Exception:
            pass

    return result


async def handle_custom_questions(page, profile: dict, job_info: dict = None):
    """Find and answer custom text questions on the form."""
    # Find textarea and text inputs that might be custom questions
    textareas = await page.locator('textarea:visible').all()

    for ta in textareas:
        try:
            current = await ta.input_value()
            if current:  # Already answered
                continue

            # Find the question text
            ta_id = await ta.get_attribute('id') or ''
            label_text = ''
            if ta_id:
                label = page.locator(f'label[for="{ta_id}"]')
                if await label.count() > 0:
                    label_text = await label.first.inner_text()

            if not label_text:
                # Try previous sibling or parent label
                aria = await ta.get_attribute('aria-label') or ''
                placeholder = await ta.get_attribute('placeholder') or ''
                label_text = aria or placeholder

            if not label_text:
                continue

            # Try quick answer first, fall back to AI
            quick = get_quick_answer(label_text, profile)
            if quick:
                await ta.fill(quick)
                print(f"  [*] Quick answered: {label_text[:40]}...")
                qa_note('textarea', label_text, quick, 'quick')
            else:
                qtype = classify_question(label_text)
                if qtype == 'cover_letter':
                    # optional cover letters stay blank (user, 8/23): the
                    # free-tier model's output is uneven and a blank optional
                    # field beats a mediocre letter. Required ones still get
                    # generated (sanitized by _strip_reasoning).
                    required = ('*' in label_text or await ta.evaluate(
                        "el => el.required || el.getAttribute('aria-required') === 'true'"))
                    if not required:
                        print(f"  [~] Skipped optional cover letter: {label_text[:40]}")
                        qa_note('textarea', label_text, '(left blank — optional)', 'policy')
                        continue
                    answer = generate_cover_letter(profile, job_info or {})
                else:
                    answer = answer_question(label_text, profile, job_info)
                if not answer:
                    continue  # sanitizer rejected the AI output; repair sweep retries
                await ta.fill(answer)
                print(f"  [*] AI answered: {label_text[:40]}...")
                qa_note('textarea', label_text, answer, 'ai')

        except Exception:
            continue


def _fetch_code_imap(email_addr: str, app_password: str) -> str | None:
    """Pull the newest greenhouse security code via Gmail IMAP.

    The main account sweeps this inbox via POP with delete-copy, so a code
    lands in INBOX for seconds before moving to [Gmail]/Trash (occasionally
    Spam). Search all three, keep only mail from the last 15 minutes, and
    return the code from the newest message — the current submit's request
    is always the most recent."""
    import imaplib
    import email as email_mod
    import re
    from email.utils import parsedate_to_datetime
    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=15)
    best_dt, best_code = None, None
    try:
        M = imaplib.IMAP4_SSL('imap.gmail.com')
        M.login(email_addr, app_password)
        for box in ('INBOX', '"[Gmail]/Trash"', '"[Gmail]/Spam"'):
            try:
                M.select(box, readonly=True)
                # no SINCE here: gmail evaluates it in Pacific time, so
                # early-UTC queries silently exclude everything from "today".
                # freshness is enforced below on the Date header instead.
                typ, data = M.search(
                    None, '(FROM "greenhouse-mail.io" SUBJECT "Security code")')
            except Exception:
                continue
            for mid in data[0].split()[-5:]:
                typ, msg_data = M.fetch(mid, '(BODY.PEEK[])')
                msg = email_mod.message_from_bytes(msg_data[0][1])
                try:
                    dt = parsedate_to_datetime(msg['Date'])
                except Exception:
                    continue
                if dt < cutoff:
                    continue
                body = ''
                for part in msg.walk():
                    if part.get_content_type() in ('text/plain', 'text/html'):
                        body += part.get_payload(decode=True).decode(errors='ignore')
                m = re.search(r'application:\s*(?:<[^>]+>\s*)*([A-Za-z0-9]{6,12})', body)
                if m and (best_dt is None or dt > best_dt):
                    best_dt, best_code = dt, m.group(1)
    except Exception as e:
        print(f"  [!] IMAP: {e}")
    return best_code


async def get_security_code(profile: dict, timeout: int = 300) -> str | None:
    """Greenhouse emails a security code after submit. Sources, in order:
    Gmail IMAP (GMAIL_APP_PASSWORD env — CI autonomy) or a code file
    (auto-apply/.security_code — an orchestrator/human drops the code in)."""
    import time
    app_pw = os.environ.get('GMAIL_APP_PASSWORD')
    code_file = Path(__file__).parent / '.security_code'
    code_file.unlink(missing_ok=True)
    print(f"  [*] Security code required — polling {'imap + ' if app_pw else ''}code file for up to {timeout}s...")
    start = time.time()
    while time.time() - start < timeout:
        if app_pw:
            # GMAIL_IMAP_USER overrides which inbox we read: the profile
            # address forwards into the main account and the copy is swept,
            # so codes only ever surface in the main inbox
            imap_user = os.environ.get('GMAIL_IMAP_USER') or profile['email']
            code = _fetch_code_imap(imap_user, app_pw)
            if code:
                print("  [+] Code from email")
                return code
        if code_file.exists():
            code = code_file.read_text().strip()
            if code:
                code_file.unlink()
                print("  [+] Code from file")
                return code
        await asyncio.sleep(10)
    print("  [!] No security code arrived in time")
    return None


CONFIRMATION_PHRASES = [
    'thank you for applying', 'thanks for applying',
    'application has been submitted', 'application was submitted',
    'application submitted', 'application received',
    'we have received your application', "we've received your application",
    'your application has been received',
    'successfully submitted', 'submission was successful',
    'thank you for your application',
    'successfully applied', 'you have applied',
]


async def submit_confirmed(page) -> bool:
    """True only if the page actually acknowledges the submission."""
    try:
        url = page.url.lower()
        # greenhouse redirects to .../confirmation, lever to .../thanks
        if 'confirmation' in url or url.rstrip('/').endswith('/thanks'):
            return True
        body = (await page.locator('body').inner_text(timeout=5000)).lower()
        return any(p in body for p in CONFIRMATION_PHRASES)
    except Exception:
        return False


async def wait_confirmed(page, seconds: int = 18) -> bool:
    """Poll for the confirmation instead of one 3s check — lever's /thanks
    redirect routinely lands after the single check did, which is how real
    submissions kept getting logged submit_unconfirmed."""
    for _ in range(max(1, seconds // 2)):
        if await submit_confirmed(page):
            return True
        await asyncio.sleep(2)
    return False


async def click_submit(page) -> bool:
    """Find and click the submit button."""
    # cookie banners overlay the form and swallow the click
    try:
        dismiss = page.locator(
            'button:has-text("dismiss"), button:has-text("Accept"), '
            'button:has-text("Got it")').first
        if await dismiss.is_visible(timeout=1500):
            await dismiss.click()
            await asyncio.sleep(0.5)
    except Exception:
        pass

    submit_selectors = [
        '#btn-submit',  # lever: type="button", plus an invisible hcaptcha decoy with type="submit"
        'button[type="submit"]:visible',
        'input[type="submit"]:visible',
        'button:has-text("Submit")',
        'button:has-text("Submit Application")',
        'button:has-text("Apply")',
        'button:has-text("Send Application")',
        'button:has-text("Complete")',
        '[data-automation-id="submitButton"]',
    ]

    for selector in submit_selectors:
        try:
            btn = page.locator(selector).first
            if await btn.is_visible(timeout=2000):
                # lever's cookie-consent banner renders visible type=submit
                # "accept"/"deny" buttons (cc-btn classes) — clicking one
                # would count as a submit and skip the real button
                cls = (await btn.get_attribute('class')) or ''
                txt = ((await btn.inner_text()) or '').strip().lower()
                if 'cc-' in cls or txt in ('accept', 'deny', 'got it', 'dismiss',
                                           'accept all', 'reject all'):
                    continue
                await btn.click()
                return True
        except Exception:
            continue

    return False


async def run_apply(url: str, dry_run: bool = False, headed: bool = False, job_info: dict = None, job_id: int = None,
                    resume_path: str = None, hold: bool = False):
    """Apply to a single job."""
    drop_proxy_if_degraded()
    profile = load_profile()
    if resume_path:
        rp = Path(resume_path).expanduser().resolve()
        if not rp.exists():
            print(f"  [!] Resume not found: {rp}")
            return {"status": "error", "error": f"resume not found: {rp}"}
        profile['resume_path'] = str(rp)
        print(f"  [*] Using resume: {rp.name}")
    async with async_playwright() as p:
        browser = await p.chromium.launch(**launch_kwargs(headless=not headed))
        ua, viewport = pick_fingerprint()
        context = await browser.new_context(viewport=viewport, user_agent=ua)
        page = await context.new_page()

        if not resume_path:
            # queue applies arrive as bare URLs with no JD — fetch it BEFORE
            # the tailor decision, or every queue apply ships untailored
            if len((job_info or {}).get('description') or '') < 300:
                try:
                    await page.goto(url, wait_until='domcontentloaded', timeout=60000)
                    await asyncio.sleep(2)
                    jd_hit = await extract_jd(page)
                    if jd_hit and jd_hit.get('description'):
                        job_info = {**(job_info or {}),
                                    **{k: v for k, v in jd_hit.items() if v}}
                        print(f"  [*] JD prefetched for tailoring ({len(jd_hit['description'])} chars)")
                except Exception as e:
                    print(f"  [!] JD prefetch failed ({e}); tailor gate will decide")
            profile, block = prepare_resume(profile, job_info, job_id)
            if block:
                print(f"  [X] {block['status']}: {block['reason']}")
                company = (job_info or {}).get('company') or company_from_url(url) or 'Unknown'
                role = (job_info or {}).get('role') or 'Unknown'
                log_application(job_id or 0, company, role, url, block['status'], block)
                await browser.close()
                return block
        stage_upload_copy(profile)

        result = await apply_to_url(page, url, profile, job_info, dry_run or hold)

        if hold:
            result["status"] = "held_for_review"
            print("  [*] HOLD — browser stays open. Review the form, fill anything blank,")
            print("      and click Submit yourself. Close the window when you're done.")
            try:
                await page.wait_for_event('close', timeout=0)
            except Exception:
                pass

        try:
            await browser.close()
        except Exception:
            pass

    # Log it — queue applies arrive with a bare URL, so fall back to what the
    # JD extraction saw, then to the ATS board slug in the URL
    company = ((job_info or {}).get("company") or result.get("jd_company")
               or company_from_url(url) or "Unknown")
    role = (job_info or {}).get("role") or result.get("jd_role") or "Unknown"
    log_application(job_id or 0, company, role, url, result["status"], result)

    # Update tracker if submitted
    if job_id and result["status"] == "submitted":
        update_job_status(job_id, "applied")
        print(f"\n  [+] Tracker updated: #{job_id} -> applied")

        # Sync to GitHub
        db = load_jobs()
        await sync_to_github(db)

    return result


def ats_family(url: str) -> str:
    u = (url or '').lower()
    for fam in ('greenhouse', 'lever', 'ashby', 'myworkdayjobs', 'icims', 'smartrecruiters'):
        if fam in u:
            return fam
    return 'other'


async def posting_alive(url: str) -> bool | None:
    """Cheap board-API liveness probe before spending a browser attempt.
    True = live, False = the board says it's gone, None = can't tell.
    Fails open: an API hiccup must never skip a live posting."""
    import httpx
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(url)
        host = parts.netloc.lower()
        segs = [s for s in parts.path.split('/') if s]
        api = None
        if 'greenhouse.io' in host and 'jobs' in segs and segs[0] not in ('embed', 'jobs'):
            jid = segs[segs.index('jobs') + 1]
            if jid.isdigit():
                api = f'https://boards-api.greenhouse.io/v1/boards/{segs[0]}/jobs/{jid}'
        elif 'jobs.lever.co' in host and len(segs) >= 2:
            api = f'https://api.lever.co/v0/postings/{segs[0]}/{segs[1]}'
        elif 'myworkdayjobs.com' in host and 'job' in segs:
            tenant = host.split('.')[0]
            site = segs[1] if segs[0].lower().startswith(('en-', 'fr-', 'es-')) and len(segs) > 1 else segs[0]
            path = '/' + '/'.join(segs[segs.index('job'):])
            api = f'https://{host}/wday/cxs/{tenant}/{site}{path}'
        elif 'jobs.ashbyhq.com' in host and len(segs) >= 2:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(f'https://api.ashbyhq.com/posting-api/job-board/{segs[0]}')
            return segs[1] in r.text if r.status_code == 200 else None
        if api:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(api)
            if r.status_code == 200:
                return True
            if r.status_code == 404:
                return False
    except Exception:
        pass
    return None


async def run_batch(dry_run: bool = False, headed: bool = False, limit: int = 5,
                    deadline_min: float = 0):
    """Apply to all wishlist jobs.

    deadline_min > 0 = wall-clock budget for the whole batch. The cloudbox
    codespace idle-stops 30 min after boot with nobody attached, and a
    5-job batch with 2.5-7 min human pacing between apps runs 40+ min —
    on 8/24 both wakes died mid-batch with no status and no log commit.
    Stop starting new applications once the budget is spent; the next
    wake picks up the rest."""
    batch_t0 = time.time()
    deadline_min = deadline_min or float(os.environ.get('BATCH_DEADLINE_MIN') or 0)

    def budget_left() -> float:
        return deadline_min * 60 - (time.time() - batch_t0)

    # real submits happen when a person would be applying: ET business hours.
    # dry runs and FORCE_OUTSIDE_HOURS=1 (manual dispatch) are exempt.
    if not dry_run and not os.environ.get('FORCE_OUTSIDE_HOURS'):
        if not within_business_hours():
            print(f"  [~] {eastern_clock()} is outside ET business hours — "
                  "skipping real submits this run (FORCE_OUTSIDE_HOURS=1 to override)")
            return
        if os.environ.get('GITHUB_ACTIONS'):
            # don't start at the exact same second every day
            j = start_jitter_seconds()
            print(f"  [*] Start jitter: waiting {j // 60}m{j % 60:02d}s...")
            await asyncio.sleep(j)

    drop_proxy_if_degraded()
    profile = load_profile()
    db = load_jobs()
    if not dry_run:
        recover_in_flight(db)
        db = load_jobs()

    # per-board daily cap from today's log — a person doesn't file five
    # applications with the same company in one day
    logs = json.loads(LOG_PATH.read_text()) if LOG_PATH.exists() else []
    board_counts = todays_attempts_per_board(logs)

    # resume_ready = a tailored resume is staged but nobody ever clicked —
    # those go out first; nothing should sit waiting on a human
    wishlist = [j for j in db["jobs"] if j["status"] in ("wishlist", "resume_ready") and j.get("url")]

    # hard duplicate guard, independent of tracker status: if we have ever
    # clicked submit on this URL, never touch it again. a status update that
    # silently failed is what let one req collect 11 applications.
    tried = {_norm_url(l.get('url', '')) for l in logs
             if l.get('status') in ('submitted', 'submit_unconfirmed')}
    dupes = [j for j in wishlist if _norm_url(j.get('url', '')) in tried]
    for j in dupes:
        update_job_status(j["id"], "applied")
        print(f"  [!] #{j['id']} {j['company']} already submitted once — "
              f"marking applied, not resubmitting")
    wishlist = [j for j in wishlist if _norm_url(j.get('url', '')) not in tried]

    # window guards: a URL gets at most 2 attempts and a company at most 2
    # submits per 14 days — re-hammering is how one komodo req collected 11
    # applications and Humana got 4
    cutoff = (datetime.now() - timedelta(days=14)).isoformat()
    recent = [l for l in logs if l.get('timestamp', '') >= cutoff]
    # the 2-attempt guard exists to stop duplicate SUBMISSIONS (11 apps to
    # one komodo req). A CAPTCHA park, proxy error, or formless page never
    # sent anything, so those must not retire a job for 14 days — on 8/21
    # five proxy errors burned most of the wishlist and the 8/23 batch had
    # 1 job left to try. Submit-class outcomes keep the hard 2-cap; every
    # other outcome gets a looser 4-cap so a broken page still can't churn
    # forever.
    SUBMIT_CLASS = ('submitted', 'submit_unconfirmed', 'needs_code', 'held_for_review')
    url_attempts = Counter(_norm_url(l.get('url', '')) for l in recent
                           if l.get('url') and l.get('status') in SUBMIT_CLASS)
    url_misses = Counter(_norm_url(l.get('url', '')) for l in recent
                         if l.get('url') and l.get('status') not in SUBMIT_CLASS)
    # 9/12: a host the proxy cannot even reach (stripe.com ERR_TUNNEL_CONNECTION_FAILED,
    # careers.roblox.com ERR_HTTP2_PROTOCOL_ERROR) burned 10 of 12 slots every
    # slice for two days under the 4-cap. Two navigation failures = the page
    # is not coming up; hand the slot to a job that can load.
    url_nav_errors = Counter(_norm_url(l.get('url', '')) for l in recent
                             if l.get('url') and l.get('status') == 'error'
                             and ('Page.goto' in str((l.get('details') or {}).get('error', ''))
                                  or 'net::ERR' in str((l.get('details') or {}).get('error', ''))))
    # company cap runs on a 30-day window, not 14: Huntr Q1-2026 — one
    # application at a company interviews at 6.07%, 2-3 at 4.64%, 8+ at
    # 1.91%. Oscar got 15, Connections 14, Komodo 11 in July.
    cutoff_co = (datetime.now() - timedelta(days=30)).isoformat()
    company_submits = Counter((l.get('company') or '').lower() for l in logs
                              if l.get('timestamp', '') >= cutoff_co
                              and str(l.get('status', '')).startswith('submit'))
    kept = []
    for j in wishlist:
        n_url = url_attempts.get(_norm_url(j.get('url', '')), 0)
        n_miss = url_misses.get(_norm_url(j.get('url', '')), 0)
        n_co = company_submits.get((j.get('company') or '').lower(), 0)
        if n_url >= 2:
            print(f"  [~] #{j['id']} {j['company']} skipped — URL already submitted-to {n_url}x in 14d")
        elif n_miss >= 4:
            print(f"  [~] #{j['id']} {j['company']} skipped — URL failed {n_miss}x in 14d without a submit")
        elif url_nav_errors.get(_norm_url(j.get('url', '')), 0) >= 2:
            print(f"  [~] #{j['id']} {j['company']} skipped — page never loaded "
                  f"{url_nav_errors[_norm_url(j.get('url', ''))]}x in 14d (proxy/host unreachable)")
        elif n_co >= 2:
            print(f"  [~] #{j['id']} {j['company']} skipped — company already has {n_co} submits in 14d")
        else:
            kept.append(j)
    wishlist = kept

    # a board family whose recent attempts mostly end in CAPTCHA parks goes to
    # the back of the line — lever/ashby can actually confirm while greenhouse
    # walls us. Nothing is skipped, just ordered by the odds of a real submit.
    fam_recent = Counter(ats_family(l.get('url')) for l in recent if l.get('url'))
    fam_parked = Counter(ats_family(l.get('url')) for l in recent
                         if l.get('url') and l.get('status') == 'manual_review')
    walled = {f for f, n in fam_recent.items() if n >= 3 and fam_parked.get(f, 0) / n >= 0.5}
    if walled:
        print(f"  [*] CAPTCHA-walled board families deprioritized: {', '.join(sorted(walled))}")

    # freshest postings first: applications sent within a day or two of a
    # posting going live get answered far more often than week-old ones
    wishlist.sort(key=lambda j: (int(j.get("tier") or 1),   # 9/9: NJ/NYC/remote $85k+ first, farther/lower after
                                 ats_family(j.get("url")) in walled,
                                 j["status"] != "resume_ready",
                                 not j.get("description"),
                                 posting_age_days(j) if posting_age_days(j) is not None else 99,
                                 -j.get("id", 0)))
    # Workday and SmartRecruiters postings never submit headless (account
    # wall, own captcha): four unconfirmed Humana clicks in July. Instead of a
    # browser slot they get a tailored resume + an entry in HAND_APPLY.md so
    # the human submit is a two-minute job with everything in hand.
    HAND_FAMILIES = ('smartrecruiters',)   # workday is automated in workday.py since 9/7
    hand = [j for j in wishlist if ats_family(j.get('url')) in HAND_FAMILIES]
    wishlist = [j for j in wishlist if ats_family(j.get('url')) not in HAND_FAMILIES]
    if hand and not dry_run:
        try:
            from handapply import stage
            for j in hand[:2]:   # tailoring is the expensive part; two per wake
                stage(j, profile, prepare_resume, log_application, update_job_status)
        except Exception as e:
            print(f"  [!] hand-apply staging failed ({e}) — jobs stay in wishlist")

    if not wishlist:
        print("No wishlist jobs with URLs to apply to.")
        return

    to_apply = wishlist[:limit]
    print(f"\n{'='*60}")
    print(f"  SCIPIO AUTO-APPLY {'(DRY RUN)' if dry_run else ''}")
    print(f"  Applying to {len(to_apply)} of {len(wishlist)} wishlist jobs")
    print(f"{'='*60}\n")

    results = []

    async with async_playwright() as p:
        context, close_browser = await open_browser(p, headless=not headed)

        skipped_capped = 0
        deferred = 0
        for i, job in enumerate(to_apply, 1):
            # ~4 min is a normal tailored fill+submit+confirm; don't start one
            # the box will be killed in the middle of
            if deadline_min and budget_left() < 240:
                deferred = len(to_apply) - i + 1
                print(f"  [~] Batch deadline ({deadline_min:.0f}m) reached — "
                      f"{deferred} job(s) left for the next wake")
                break
            print(f"\n--- [{i}/{len(to_apply)}] {job['company']} - {job['role']} ---")

            bkey = board_key(job.get("url", ""))
            if not dry_run and board_counts.get(bkey, 0) >= BOARD_DAILY_CAP:
                print(f"  [~] Board '{bkey}' hit today's cap ({BOARD_DAILY_CAP}) — leaving for tomorrow")
                skipped_capped += 1
                continue
            board_counts[bkey] = board_counts.get(bkey, 0) + 1

            # dead-posting probe: the board's own API knows a req is closed —
            # don't burn a browser attempt (and a no_form log entry) finding out
            if not dry_run and await posting_alive(job["url"]) is False:
                update_job_status(job["id"], "closed")
                print("  [~] Posting gone (board API 404) — closed without an attempt")
                continue

            page = await context.new_page()

            job_info = {
                "company": job["company"],
                "role": job["role"],
                "description": job.get("description") or job.get("notes", ""),
            }

            # per-job resume: tailored to this JD or fail closed. (The batch
            # path used to skip tailoring entirely — every cron send was a
            # static variant.)
            job_profile, block = prepare_resume(profile, job_info, job["id"])
            if block and not dry_run:
                print(f"  [X] {block['status']}: {block['reason']}")
                log_application(job["id"], job["company"], job["role"], job["url"],
                                block['status'], block)
                if block['status'] == 'blocked_knockout':
                    # a knockout is a property of the JD, not of this run: the
                    # same LTS clearance req took 5 batch slots in 2 days
                    # (9/4-9/5) because it stayed 'wishlist'. Retire it.
                    update_job_status(job["id"], "blocked_knockout")
                    print(f"  [~] Tracker: #{job['id']} -> blocked_knockout (retired, never retried)")
                await page.close()
                continue

            # fit gate: even the best tailored artifact can't honestly echo an
            # off-lane JD (a ServiceNow admin req scored 19%). Below the lint
            # floor we don't apply at all — quarantine so it never retries.
            jd = job_info.get("description") or ""
            if not dry_run and len(jd) >= 300:
                try:
                    rtext, _ = extract_pdf_text(job_profile.get('resume_path', ''))
                    kw = keyword_score(rtext, jd, job.get("role", ""))
                    if kw['score'] < APPLY_ECHO_FLOOR:
                        update_job_status(job["id"], "weak_match")
                        print(f"  [~] Fit gate: keyword echo {kw['score']}% < {APPLY_ECHO_FLOOR}% "
                              f"(missing: {', '.join(kw['missing'][:6])}) — "
                              f"#{job['id']} -> weak_match, not applying")
                        await page.close()
                        continue
                except Exception as e:
                    print(f"  [!] Fit gate unreadable ({e}) — proceeding")
            stage_upload_copy(job_profile)

            result = await apply_with_timeout(page, job["url"], job_profile, job_info, dry_run)
            if result["status"] == "error" and not result.get("timed_out"):
                # self-heal: one retry on a fresh page before giving up
                print("  [!] Errored — retrying once on a fresh page...")
                await page.close()
                page = await context.new_page()
                result = await apply_with_timeout(page, job["url"], job_profile, job_info, dry_run)
            results.append({"job": job, "result": result})

            # Keep the JD we saw on the job record
            if result.get("description"):
                save_job_description(job["id"], result["description"])

            # Log (without the JD blob — that lives on the job record)
            log_details = {k: v for k, v in result.items() if k != "description"}
            log_application(job["id"], job["company"], job["role"], job["url"], result["status"], log_details)

            # Update status if submitted
            if result["status"] == "submitted" and not dry_run:
                update_job_status(job["id"], "applied")
                print(f"  [+] Tracker: #{job['id']} -> applied")
            elif not dry_run and not result["fields_filled"] and result["status"] != "error":
                # self-heal: page had no fillable application form
                # (listing/search page) — quarantine so it's never retried
                update_job_status(job["id"], "bad_url")
                print(f"  [!] Tracker: #{job['id']} -> bad_url (no form on page)")
            elif (not dry_run and result["status"] == "submit_unconfirmed"
                  and result.get("code_requested") and not os.environ.get('GMAIL_APP_PASSWORD')):
                # greenhouse wants an emailed code and this run can't read
                # email — park it instead of burning codes every day. Must be
                # checked BEFORE the generic unconfirmed branch or it never fires.
                update_job_status(job["id"], "needs_code")
                print(f"  [!] Tracker: #{job['id']} -> needs_code (set GMAIL_APP_PASSWORD to automate)")
            elif not dry_run and result["status"] == "submit_unconfirmed":
                # we clicked submit and could not read a confirmation. it may
                # well have gone through, so NEVER auto-retry: retrying is what
                # sent 11 applications to one komodo req. park for a human look.
                update_job_status(job["id"], "submitted_unverified")
                print(f"  [!] Tracker: #{job['id']} -> submitted_unverified "
                      f"(clicked submit, no confirmation seen; will not retry)")
            elif not dry_run and result["status"] == "weak_match":
                update_job_status(job["id"], "weak_match")
                print(f"  [~] Tracker: #{job['id']} -> weak_match (match score floor)")
            elif not dry_run and result["status"] == "manual_review":
                # CAPTCHA wall this wake — requeue.py puts it back on the
                # wishlist after a day, three tries, then blocked_captcha
                update_job_status(job["id"], "captcha_retry")
                print(f"  [!] Tracker: #{job['id']} -> captcha_retry (automatic retry, no human)")

            await page.close()

            # Human-pace delay between applications: minutes, jittered.
            # Dry runs keep a short delay so local testing stays fast.
            if i < len(to_apply):
                delay = 5 if dry_run else between_apps_seconds()
                if deadline_min and budget_left() - delay < 240:
                    print(f"  [~] Batch deadline ({deadline_min:.0f}m) — no time for "
                          f"another application after the pacing wait, stopping here")
                    deferred = len(to_apply) - i
                    break
                print(f"  [*] Waiting {delay // 60}m{delay % 60:02d}s before next application...")
                await asyncio.sleep(delay)

        await close_browser()

    # Sync to GitHub (skip in Actions — the workflow's commit step handles
    # it; an API push mid-run makes the later git push conflict)
    if not (os.environ.get('GITHUB_ACTIONS') or os.environ.get('CI')):
        db = load_jobs()
        await sync_to_github(db)

    # Summary
    print(f"\n{'='*60}")
    print("  BATCH RESULTS")
    print(f"{'='*60}")
    submitted = sum(1 for r in results if r["result"]["status"] == "submitted")
    filled = sum(1 for r in results if r["result"]["status"] in ("filled_not_submitted", "dry_run"))
    errors = sum(1 for r in results if r["result"]["status"] == "error")
    print(f"  Submitted: {submitted}")
    print(f"  Filled (not submitted): {filled}")
    print(f"  Errors: {errors}")
    if skipped_capped:
        print(f"  Skipped (board daily cap): {skipped_capped}")
    if deferred:
        print(f"  Deferred (batch deadline): {deferred}")
    print(f"  Total: {len(results)}")

    for r in results:
        status_icon = {"submitted": "+", "dry_run": "~", "filled_not_submitted": "?", "error": "!"}.get(r["result"]["status"], "?")
        print(f"  [{status_icon}] {r['job']['company']} - {r['result']['status']} ({r['result']['ats']})")


async def run_score(url: str):
    """Score a job URL against profile."""
    profile = load_profile()

    async with async_playwright() as p:
        browser = await p.chromium.launch(**launch_kwargs(headless=True))
        page = await browser.new_page()
        await page.goto(url, wait_until='domcontentloaded', timeout=60000)
        await asyncio.sleep(2)

        # Get job description text (JSON-LD first — carries company/role too)
        jd_hit = await extract_jd(page)

        await browser.close()

    job_info = {
        "company": jd_hit.get("company") or "Unknown",
        "role": jd_hit.get("role") or "Unknown",
        "description": jd_hit.get("description", "")[:12000],
    }

    result = score_job_match(profile, job_info)
    print(f"\nJob Match Score: {result.get('score', '?')}/10")
    print(f"Reason: {result.get('reason', 'N/A')}")
    print(f"Matching Skills: {', '.join(result.get('matching_skills', []))}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1]
    args = sys.argv[2:]

    dry_run = '--dry-run' in args
    headed = '--headed' in args
    hold = '--hold' in args
    limit = 5
    resume = None
    deadline_min = 0.0

    consumed = set()
    for i, a in enumerate(args):
        if a == '--limit' and i + 1 < len(args):
            limit = int(args[i + 1])
            consumed.add(i + 1)
        if a == '--resume' and i + 1 < len(args):
            resume = args[i + 1]
            consumed.add(i + 1)
        if a == '--deadline-min' and i + 1 < len(args):
            deadline_min = float(args[i + 1])
            consumed.add(i + 1)

    # Filter out flags and flag values
    urls = [a for i, a in enumerate(args) if not a.startswith('--') and i not in consumed]

    if cmd == 'apply':
        if not urls:
            print("Usage: applier.py apply <url> [--dry-run] [--headed] [--hold] [--resume <pdf>]")
            return
        asyncio.run(run_apply(urls[0], dry_run=dry_run, headed=headed, resume_path=resume, hold=hold))

    elif cmd == 'batch':
        asyncio.run(run_batch(dry_run=dry_run, headed=headed, limit=limit,
                              deadline_min=deadline_min))

    elif cmd == 'score':
        if not urls:
            print("Usage: applier.py score <url>")
            return
        asyncio.run(run_score(urls[0]))

    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)


if __name__ == '__main__':
    main()
