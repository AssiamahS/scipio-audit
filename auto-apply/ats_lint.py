#!/usr/bin/env python3
"""ATS lint — is this resume PDF actually parseable and does it echo the JD?

Checks a generated resume PDF the way an ATS reads it:
  1. text extracts cleanly (no image-only pages, no embedded images)
  2. standard section headers present (EXPERIENCE / EDUCATION / SKILLS)
  3. contact info survives extraction (email + phone)
  4. JD keyword coverage: which skills/tools/title terms from the job
     description literally appear in the resume text (echo, not synonyms —
     ATS matching is literal). Coverage under FAIL_UNDER fails the lint.

Usage:
  uv run python ats_lint.py <resume.pdf> --jd <jd.txt>
  uv run python ats_lint.py <resume.pdf> --job-id 46      # JD from jobs.json
  uv run python ats_lint.py <resume.pdf> --jd - < jd.txt

Exit code 0 = pass, 1 = fail. Also importable: keyword_score(resume_text, jd).
"""
import json
import re
import sys
from pathlib import Path

FAIL_UNDER = 70  # percent coverage

REQUIRED_HEADERS = ('experience', 'education', 'skills')

# terms an ATS/recruiter actually filters on. Lowercase; multiword allowed.
LEXICON = [
    # infra / ops
    'active directory', 'azure', 'aws', 'gcp', 'google cloud', 'vmware',
    'windows server', 'linux', 'powershell', 'bash', 'terraform', 'ansible',
    'kubernetes', 'docker', 'vdi', 'citrix', 'intune', 'sccm', 'jamf',
    'dns', 'dhcp', 'tcp/ip', 'vpn', 'firewall', 'load balancer',
    'monitoring', 'observability', 'incident management', 'change management',
    'itil', 'servicenow', 'jira', 'confluence', 'disaster recovery',
    'backup', 'patching', 'troubleshooting', 'infrastructure',
    'system administration', 'network', 'storage', 'virtualization',
    'automation', 'scripting', 'help desk', 'service desk', 'sla',
    # identity / security
    'sailpoint', 'okta', 'iam', 'identity and access management',
    'identity governance', 'sso', 'single sign-on', 'mfa', 'saml', 'oidc',
    'oauth', 'rbac', 'provisioning', 'deprovisioning', 'access review',
    'least privilege', 'security', 'compliance', 'audit',
    # healthcare
    'epic', 'cerner', 'ehr', 'emr', 'hipaa', 'hl7', 'fhir', 'clinical',
    'healthcare', 'hospital', 'patient', 'pharmacy', 'pharmaceutical',
    'medical device', 'telehealth', 'interoperability', 'phi',
    # data / dev
    'sql', 'mysql', 'postgresql', 'python', 'javascript', 'excel',
    'power bi', 'tableau', 'databricks', 'etl', 'reporting', 'analytics',
    'data analysis', 'dashboards', 'api', 'rest',
    # pm / process
    'project management', 'program management', 'agile', 'scrum', 'kanban',
    'waterfall', 'pmp', 'stakeholder', 'cross-functional', 'roadmap',
    'budget', 'vendor management', 'risk management', 'documentation',
    'training', 'implementation', 'deployment', 'migration', 'rollout',
    'operations', 'process improvement', 'communication',
]

STOP = set('''a an the and or of to in for with on at by from as is are was
were be been this that these those you your we our their it its will can
must may should would other more most such into within across per'''.split())


def extract_pdf_text(pdf_path: str) -> tuple[str, list]:
    """Return (text, problems). Problems = extraction red flags."""
    from pypdf import PdfReader
    problems = []
    reader = PdfReader(pdf_path)
    pages = []
    for i, page in enumerate(reader.pages, 1):
        t = page.extract_text() or ''
        if len(t.strip()) < 40:
            problems.append(f'page {i}: almost no extractable text (image-only?)')
        try:
            if page.images:
                problems.append(f'page {i}: {len(page.images)} embedded image(s) — ATS parsers choke on these')
        except Exception:
            pass
        pages.append(t)
    return '\n'.join(pages), problems


def _norm(text: str) -> str:
    return re.sub(r'\s+', ' ', text.lower())


def jd_keywords(jd_text: str, role_title: str = '') -> list[str]:
    """Keywords the JD actually asks for: lexicon hits + the words of the
    job title itself (ATS filters heavily on title-variant echoes)."""
    jd = _norm(jd_text)
    found = [t for t in LEXICON if t in jd]
    for w in re.findall(r'[a-z][a-z+/#-]{3,}', _norm(role_title)):
        if w not in STOP and w not in found and not any(w in f for f in found):
            found.append(w)
    return found


def keyword_score(resume_text: str, jd_text: str, role_title: str = '') -> dict:
    """Literal-echo coverage of JD keywords in the resume text."""
    kws = jd_keywords(jd_text, role_title)
    if not kws:
        return {'score': 100, 'present': [], 'missing': [], 'keywords': []}
    resume = _norm(resume_text)
    present = [k for k in kws if k in resume]
    missing = [k for k in kws if k not in resume]
    return {
        'score': round(100 * len(present) / len(kws)),
        'present': present,
        'missing': missing,
        'keywords': kws,
    }


# typographic ligatures survive PDF extraction as single codepoints, so
# "first" comes out "ﬁrst" and no ATS keyword match will ever hit it
LIGATURES = 'ﬁﬂﬀﬃﬄﬅﬆ'


def structure_check(resume_text: str) -> list[str]:
    problems = []
    low = resume_text.lower()
    if '—' in resume_text:
        problems.append(
            "em dash on the resume: recruiters now read them as AI-written. "
            "use a comma, colon, or a new sentence")
    bad_lig = {c for c in LIGATURES if c in resume_text}
    if bad_lig:
        problems.append(
            f"pdf uses typographic ligatures ({''.join(sorted(bad_lig))}) — "
            "words containing them will not keyword-match; set "
            "font-variant-ligatures: none")
    for h in REQUIRED_HEADERS:
        if h not in low:
            problems.append(f'missing standard section header: {h.upper()}')
    if not re.search(r'[\w.+-]+@[\w-]+\.[\w.]+', resume_text):
        problems.append('email did not survive text extraction')
    if not re.search(r'\d{3}[-.\s]?\d{3}[-.\s]?\d{4}', resume_text):
        problems.append('phone number did not survive text extraction')
    return problems


def lint(pdf_path: str, jd_text: str, role_title: str = '') -> dict:
    text, extract_problems = extract_pdf_text(pdf_path)
    problems = extract_problems + structure_check(text)
    kw = keyword_score(text, jd_text, role_title)
    passed = not problems and kw['score'] >= FAIL_UNDER
    return {'pdf': pdf_path, 'problems': problems, 'passed': passed, **kw}


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(2)
    pdf = args[0]
    jd_text = ''
    role_title = ''
    if '--jd' in args:
        src = args[args.index('--jd') + 1]
        jd_text = sys.stdin.read() if src == '-' else Path(src).read_text()
    elif '--job-id' in args:
        jid = int(args[args.index('--job-id') + 1])
        db = json.loads((Path(__file__).parent.parent / 'jobs.json').read_text())
        job = next((j for j in db['jobs'] if j['id'] == jid), None)
        if not job or not job.get('description'):
            sys.exit(f'job {jid} not found or has no stored JD')
        jd_text = job['description']
        role_title = job['role']
        print(f"JD: #{jid} {job['company']} — {job['role']}")
    else:
        sys.exit('need --jd <file|-> or --job-id N')

    r = lint(pdf, jd_text, role_title)
    print(f"\nATS LINT: {Path(pdf).name}")
    print(f"  keyword coverage: {r['score']}% ({len(r['present'])}/{len(r['keywords'])})")
    if r['missing']:
        print(f"  missing from resume: {', '.join(r['missing'])}")
    for p in r['problems']:
        print(f"  !! {p}")
    print(f"  RESULT: {'PASS' if r['passed'] else 'FAIL'} (bar: {FAIL_UNDER}% + clean structure)")
    sys.exit(0 if r['passed'] else 1)


if __name__ == '__main__':
    main()
