#!/usr/bin/env python3
"""Hand-apply packs: everything a human needs to submit in two minutes.

Some boards will never take a headless submit: Workday (account wall, four
unconfirmed Humana clicks in July), SmartRecruiters (own captcha), and any
Greenhouse/Lever/Ashby page that showed a CAPTCHA (manual_review). Those jobs
used to sit in parked.md as a bare URL. Now each one gets:
  - a resume tailored to its JD (same fail-closed path the applier uses)
  - the fit verdict and knockouts
  - the screening answers he gives everywhere
written into HAND_APPLY.md, newest first. The daily digest links to it.

  uv run python handapply.py            # rebuild HAND_APPLY.md
"""
import json
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
JOBS = ROOT / 'jobs.json'
LOG = HERE / 'apply_log.json'
OUT = HERE / 'HAND_APPLY.md'

HAND_STATUSES = ('hand_apply', 'manual_review', 'needs_account')


def stage(job: dict, profile: dict, prepare_resume, log_application, update_job_status) -> str:
    """Tailor + park one job as hand_apply. Returns the new status."""
    job_info = {'company': job.get('company', ''), 'role': job.get('role', ''),
                'description': job.get('description') or job.get('notes', '')}
    print(f"\n--- [hand-apply] {job.get('company')} - {job.get('role')} ---")
    job_profile, block = prepare_resume(profile, job_info, job.get('id'))
    if block:
        print(f"  [X] {block['status']}: {block.get('reason', '')[:120]}")
        log_application(job['id'], job['company'], job['role'], job['url'], block['status'], block)
        if block['status'] == 'blocked_knockout':
            update_job_status(job['id'], 'blocked_knockout')
        return block['status']
    details = {
        'url': job['url'], 'status': 'hand_apply', 'ats': 'workday/smartrecruiters',
        'resume': job_profile.get('resume_path'),
        'resume_tailored': bool(job_profile.get('resume_tailored_flag')),
        'fit': (job_profile.get('fit_report') or {}).get('verdict'),
        'note': 'board cannot be submitted headless; tailored resume staged for a human submit',
    }
    log_application(job['id'], job['company'], job['role'], job['url'], 'hand_apply', details)
    update_job_status(job['id'], 'hand_apply')
    print(f"  [+] Tracker: #{job['id']} -> hand_apply "
          f"({'tailored' if details['resume_tailored'] else 'static'} resume staged)")
    return 'hand_apply'


def _latest_log_by_id() -> dict:
    try:
        entries = json.loads(LOG.read_text())
    except Exception:
        return {}
    out = {}
    for e in entries:
        if e.get('job_id') is not None:
            out[str(e['job_id'])] = e
    return out


def _resume_for(job: dict, log: dict) -> str:
    d = (log.get('details') or {})
    for k in ('resume', 'resume_path'):
        if d.get(k):
            return str(d[k]).replace(str(HERE) + '/', 'auto-apply/').replace('/home/codespace/.slyci/work/AssiamahS__scipio/', '')
    cand = HERE / 'resumes_tailored' / f"{job.get('company', '')}_{job.get('id')}.pdf"
    if cand.exists():
        return f'auto-apply/resumes_tailored/{cand.name}'
    return 'auto-apply/resumes/Resume - Sylvester Assiamah (Project Manager).pdf (static, no tailored copy staged)'


def build(profile: dict | None = None) -> int:
    db = json.loads(JOBS.read_text())
    jobs = [j for j in db['jobs'] if j.get('status') in HAND_STATUSES]
    logs = _latest_log_by_id()
    profile = profile or json.loads((HERE / 'profile.json').read_text())
    sa = profile.get('screening_answers', {})

    jobs.sort(key=lambda j: j.get('updated_date') or '', reverse=True)
    lines = [
        '# Hand-apply pack',
        '',
        f'_{len(jobs)} job(s) ready for a human submit; generated '
        f'{datetime.now(timezone.utc).isoformat(timespec="minutes")} by handapply.py_',
        '',
        'Each entry already passed the fit screen and has a resume staged. Open the URL,',
        'upload the PDF named below, paste the answers, submit. Then mark the job `applied`',
        'in scipioOS or the dashboard so the tracker matches the inbox sweep (a confirmation',
        'email flips it automatically too).',
        '',
        '## Standard answers',
        '',
        f"- Work authorization: {profile.get('work_authorization', '')}",
        f"- Sponsorship: {'No' if not profile.get('requires_sponsorship') else 'Yes'}",
        f"- Location: {profile.get('city', '')}, {profile.get('state', '')} {profile.get('zip', '')}",
        f"- Phone / email: {profile.get('phone', '')} / {profile.get('email', '')}",
        f"- LinkedIn: {profile.get('linkedin', '')}",
    ]
    for k in ('notice_period', 'available_start', 'salary_range', 'travel', 'security_clearance',
              'previously_applied', 'references'):
        if sa.get(k):
            lines.append(f"- {k.replace('_', ' ').capitalize()}: {sa[k]}")
    lines += ['', '## Jobs', '']
    for j in jobs:
        log = logs.get(str(j.get('id')), {})
        d = log.get('details') or {}
        fit = d.get('fit') or ((j.get('fit') or {}).get('verdict') if isinstance(j.get('fit'), dict) else None)
        when = (log.get('timestamp') or j.get('updated_date') or '?')[:10]
        why = {'hand_apply': 'Workday/SmartRecruiters, no headless submit',
               'manual_review': 'CAPTCHA at submit, form otherwise fillable',
               'needs_account': 'board wants an account first'}.get(j['status'], j['status'])
        lines += [
            f"### {j.get('company', '?')} : {j.get('role', '?')}",
            '',
            f"- URL: {j.get('url', '')}",
            f"- Status: `{j['status']}` since {when} ({why})",
            f"- Resume: `{_resume_for(j, log)}`",
        ]
        if fit:
            lines.append(f"- Fit: {fit}")
        if j.get('posted_date'):
            lines.append(f"- Posted: {j['posted_date']}")
        if j.get('salary'):
            lines.append(f"- Salary: {j['salary']}")
        lines.append('')
    OUT.write_text('\n'.join(lines) + '\n')
    print(f'hand-apply pack: {len(jobs)} job(s) -> {OUT.name}')
    return len(jobs)


if __name__ == '__main__':
    build()
