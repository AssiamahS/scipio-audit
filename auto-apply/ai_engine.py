"""
AI engine for answering custom application questions and generating cover letters.
Uses Claude to intelligently respond to screening questions based on profile data.
"""
import anthropic
import httpx
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

client = None

# free tier via GitHub Models when no anthropic key is set.
# separate quota buckets per model, hop on 429. gpt-5 family wants
# max_completion_tokens, the rest accept it too on this endpoint.
GITHUB_MODELS_URL = "https://models.github.ai/inference/chat/completions"
GITHUB_MODELS = [
    "openai/gpt-5",
    "openai/gpt-5-mini",
    "openai/gpt-4.1-mini",
    "openai/gpt-4o-mini",
]


def _github_token() -> str | None:
    return os.environ.get('GITHUB_TOKEN') or os.environ.get('GH_TOKEN')


# github models is being retired (410 brownouts) — openrouter free tier is
# the working no-cost path. non-reasoning model: reasoning models burn the
# whole token budget thinking before they answer.
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODELS = [
    "google/gemma-4-26b-a4b-it:free",
    "google/gemma-4-31b-it:free",
    # verified 2026-08-21: both answer clean JSON when the gemmas 429
    "nvidia/nemotron-3-super-120b-a12b:free",
    # 9/9: nano-30b retired (404) — live :free list re-checked, deeper hops
    # so an 80-candidate scoring burst does not fall to keyword-only
    "nvidia/nemotron-3.5-lightning:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "nex-agi/nex-n2.5-mini:free",
    # 9/12: every hop above 429d for four straight slices and scoring fell
    # to 5/10 on everything — deeper hops from the live :free list
    "nex-agi/nex-n2.5-pro:free",
    "inclusionai/ling-3.0-flash-fin:free",
    "poolside/laguna-s-2.1:free",
    "dots-studio/dots-3-note-preview:free",
    "cohere/north-mini-code:free",
    # 9/11: inkling:free is 403 "only available on agentic harnesses" — never answers
]


_openrouter_key_cache = None


def _openrouter_key() -> str | None:
    """OPENROUTER_API_KEY env, then ~/.openrouter_key, then the macOS
    keychain item 'openrouter' — local runs shouldn't need an export."""
    global _openrouter_key_cache
    if _openrouter_key_cache is not None:
        return _openrouter_key_cache or None
    key = os.environ.get('OPENROUTER_API_KEY', '').strip()
    if not key:
        f = Path.home() / '.openrouter_key'
        if f.exists():
            key = f.read_text().strip()
    if not key and sys.platform == 'darwin':
        try:
            key = subprocess.run(
                ['security', 'find-generic-password', '-s', 'openrouter', '-w'],
                capture_output=True, text=True, timeout=5).stdout.strip()
        except Exception:
            key = ''
    _openrouter_key_cache = key
    return key or None


def _complete_openrouter(prompt: str, max_tokens: int) -> str:
    key = _openrouter_key()
    last_err = None
    # two passes: the free-tier providers 429 upstream in bursts, and a
    # single sweep at the wrong moment is how the brain played dead on 8/14
    for attempt in range(2):
        if attempt:
            time.sleep(5)
        for model in OPENROUTER_MODELS:
            try:
                r = httpx.post(
                    OPENROUTER_URL,
                    headers={"Authorization": f"Bearer {key}",
                             "Content-Type": "application/json"},
                    json={"model": model,
                          "messages": [{"role": "user", "content": prompt}],
                          "max_tokens": max_tokens},
                    timeout=60,
                )
                if r.status_code == 429:
                    last_err = f"{model}: rate limited"
                    continue
                r.raise_for_status()
                body = r.json()
                if body.get("error"):
                    last_err = f"{model}: {body['error'].get('message', body['error'])}"
                    continue
                return body["choices"][0]["message"]["content"].strip()
            except (httpx.HTTPError, KeyError, IndexError) as e:
                last_err = f"{model}: {e}"
                continue
    raise RuntimeError(f"All OpenRouter models failed, last error: {last_err}")


def _complete_github(prompt: str, max_tokens: int) -> str:
    token = _github_token()
    last_err = None
    for model in GITHUB_MODELS:
        try:
            r = httpx.post(
                GITHUB_MODELS_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    # gpt-5 family spends reasoning tokens from the same
                    # budget, so give the free path extra headroom
                    "max_completion_tokens": max(max_tokens * 4, 1200),
                },
                timeout=60,
            )
            if r.status_code == 429:
                last_err = f"{model}: rate limited"
                continue
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()
        except httpx.HTTPError as e:
            last_err = f"{model}: {e}"
            continue
    raise RuntimeError(f"All GitHub Models failed, last error: {last_err}")


def _complete(prompt: str, max_tokens: int) -> str:
    if os.environ.get('ANTHROPIC_API_KEY'):
        resp = get_client().messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}]
        )
        return resp.content[0].text.strip()
    if _openrouter_key():
        return _complete_openrouter(prompt, max_tokens)
    if _github_token():
        return _complete_github(prompt, max_tokens)
    raise ValueError("Set ANTHROPIC_API_KEY, OPENROUTER_API_KEY, or GITHUB_TOKEN")

VOICE_RULES = """VOICE RULES (hard requirements):
- Write like the candidate talks: plain verbs (ran, built, pushed, found, fixed), specific nouns, 1-2 real numbers where the profile has them.
- BANNED words, never use: spearheaded, orchestrated, leveraged, leverage, deliverables, results-driven, synergy, dynamic, passionate, seamlessly, utilize, robust, cutting-edge, esteemed, delve.
- Never invent employers, titles, dates, tools, metrics, certifications, or experience. Every fact must exist in the profile. If the profile doesn't support a claim, leave it out."""

def get_client():
    global client
    if client is None:
        api_key = os.environ.get('ANTHROPIC_API_KEY')
        if not api_key:
            raise ValueError("Set ANTHROPIC_API_KEY environment variable")
        client = anthropic.Anthropic(api_key=api_key)
    return client


def deterministic_answer(question: str, profile: dict, job_info: dict = None) -> str:
    """Bank-composed answers for the recurring open-ended questions. History
    (154 AI answers over 74 distinct questions, mined 8/23) shows they
    cluster into a handful of shapes — each one answers from approved
    screening_answers text, with only {company}/{role} substituted."""
    q = question.lower()
    sa = profile.get('screening_answers', {})
    company = (job_info or {}).get('company') or 'your team'
    role = (job_info or {}).get('role') or 'this role'
    if any(w in q for w in ('what excites', 'why do you want', 'why are you interested',
                            'interest in this role', 'why ' + company.lower())):
        t = sa.get('why_company_template', '')
        return t.format(company=company, role=role) if t else ''
    if 'why are you leaving' in q or 'reason for leaving' in q:
        return sa.get('why_leaving', '')
    if 'looking for in your next' in q or 'what are you looking for' in q:
        return sa.get('looking_for', '')
    if 'reference' in q:
        return sa.get('references', '')
    if any(w in q for w in ('describe your experience', 'briefly describe',
                            'tell us about your experience', 'describe your background',
                            'summarize your experience')):
        return sa.get('experience_blurb', '')
    return ''


def answer_question(question: str, profile: dict, job_info: dict = None) -> str:
    """Answer a custom application question. Deterministic-first: quick bank,
    then bank-composed answers. AI runs ONLY when USE_AI_ANSWERS=1 — the
    default is no AI at all (user, 8/23): an unknown question stays blank,
    lands in the unanswered digest, and gets a permanent bank rule once."""
    quick = get_quick_answer(question, profile)
    if quick:
        return quick
    det = deterministic_answer(question, profile, job_info)
    if det:
        return det
    if os.environ.get('USE_AI_ANSWERS', '') != '1':
        return ''

    job_context = ""
    if job_info:
        job_context = f"""
Job Details:
- Company: {job_info.get('company', 'Unknown')}
- Role: {job_info.get('role', 'Unknown')}
- Description: {job_info.get('description', 'N/A')[:500]}
"""

    prompt = f"""You are filling out a job application for a candidate. Answer this application question
concisely. Use first person. Keep it under 150 words unless the question clearly
requires more detail. Be specific with real details from the profile.

{VOICE_RULES}

Candidate Profile:
{json.dumps(profile, indent=2)}

{job_context}

Application Question: {question}

Answer (first person, professional, concise):"""

    return _strip_reasoning(_complete(prompt, 300), question)


_REASONING_TELLS = (
    'we need to', 'we must', 'must be first person', 'first person',
    'the question asks', 'the question is', 'this question',
    'the candidate', 'let me', "let's", 'i should answer', 'to answer this',
    'under 150 words', 'keep it concise', 'be concise',
    'as an ai', 'answer:',
)


def _norm(s: str) -> str:
    return re.sub(r'[^a-z0-9 ]', '', s.lower()).strip()


def _strip_reasoning(text: str, question: str = '') -> str:
    """Free-tier models sometimes echo their reasoning before the answer
    ('We need to answer the question: ...') — on 8/23 that meta-text was
    typed verbatim into a Lockwood textarea. If a tell appears, keep only
    what follows an 'Answer:' marker, else drop leading tell-lines; if
    nothing clean survives, return '' so the caller's non-AI fallbacks run
    instead of shipping the monologue."""
    if not text:
        return text
    # question-echo: the free model sometimes replies with (a prefix of) the
    # question itself ('"Please list any relevant certifications applicabl',
    # 8/23) — that must never be typed into a form
    if question:
        qn, tn = _norm(question), _norm(text)
        if tn and (qn.startswith(tn[:60]) or tn[:60] in qn):
            return ''
    lowered = text.lower()
    if not any(t in lowered[:200] for t in _REASONING_TELLS):
        return text
    marker = lowered.rfind('answer:')
    if marker != -1 and marker + 7 < len(text):
        candidate = text[marker + 7:].strip()
        if candidate and not any(t in candidate.lower()[:80] for t in _REASONING_TELLS):
            return candidate
    kept = []
    for line in text.splitlines():
        l = line.strip().lower()
        if l and any(l.startswith(t) for t in _REASONING_TELLS):
            continue
        kept.append(line)
    cleaned = '\n'.join(kept).strip()
    return cleaned if cleaned and not any(
        t in cleaned.lower()[:80] for t in _REASONING_TELLS) else ''


def generate_cover_letter(profile: dict, job_info: dict) -> str:
    """Cover letter: the approved template with {company}/{role} substituted —
    never AI-written unless USE_AI_ANSWERS=1 (user, 8/23: no AI)."""
    t = profile.get('screening_answers', {}).get('cover_letter_template', '')
    if t:
        return t.format(company=job_info.get('company', 'your company'),
                        role=job_info.get('role', 'this role'))
    if os.environ.get('USE_AI_ANSWERS', '') != '1':
        return ''

    prompt = f"""Write a concise cover letter (200-250 words) for this candidate applying to this job.
Be specific - reference actual experience from the profile that matches the job. No fluff.
Do NOT include addresses or date headers. Start with "Dear Hiring Manager," and end with the candidate's name.

{VOICE_RULES}

Candidate Profile:
{json.dumps(profile, indent=2)}

Job Details:
- Company: {job_info.get('company', 'Unknown')}
- Role: {job_info.get('role', 'Unknown')}
- Description: {job_info.get('description', 'N/A')[:1000]}

Cover Letter:"""

    return _strip_reasoning(_complete(prompt, 500))


def score_job_match(profile: dict, job_info: dict) -> dict:
    """Score how well a job matches the candidate profile."""
    roles = ' | '.join(
        f"{e.get('title', '')} @ {e.get('company', '')}"
        for e in profile.get('experience', [])[:4])
    prompt = f"""Rate this job match for the candidate on a scale of 1-10. Return ONLY valid JSON.
Score fit for what the candidate can credibly do and get called back for —
mid-level infrastructure/IT-ops/IAM/analyst and project/program roles are his
lane; score senior/staff versions of deep-specialist roles lower.

Candidate Profile:
- Title: {profile.get('current_title')}
- Career: {roles}
- Skills: {', '.join(profile.get('skills', [])[:25])}
- Experience: {profile.get('years_experience')} years
- Summary: {profile.get('summary', '')[:400]}

Job:
- Company: {job_info.get('company', 'Unknown')}
- Role: {job_info.get('role', 'Unknown')}
- Description: {job_info.get('description', 'N/A')[:500]}

Return JSON: {{"score": <1-10>, "reason": "<1 sentence>", "matching_skills": ["skill1", "skill2"]}}"""

    try:
        text = _complete(prompt, 350)
        # Extract JSON — free models truncate mid-object or wrap in fences,
        # so salvage the score/reason by regex when json.loads can't
        if '{' in text:
            try:
                return json.loads(text[text.index('{'):text.rindex('}') + 1])
            except (ValueError, json.JSONDecodeError):
                m = re.search(r'"score"\s*:\s*(\d+)', text)
                if m:
                    reason = re.search(r'"reason"\s*:\s*"([^"]*)', text)
                    return {"score": int(m.group(1)),
                            "reason": reason.group(1) if reason else "salvaged from truncated reply",
                            "matching_skills": []}
                raise ValueError(f"unparseable reply: {text[:80]!r}")
    except Exception as e:
        # a silent 5/10 fallback hid a dead brain for a week — name the cause
        print(f"  [!] match scoring failed ({e}) — using fallback 5/10")

    return {"score": 5, "reason": "Could not score", "matching_skills": []}


def classify_question(question: str) -> str:
    """Classify what type of question this is to determine how to answer."""
    q = question.lower()

    if any(w in q for w in ['authorized', 'authorization', 'legally', 'eligible to work']):
        return 'work_auth'
    if any(w in q for w in ['sponsorship', 'visa', 'h1b', 'h-1b']):
        return 'sponsorship'
    if any(w in q for w in ['salary', 'compensation', 'pay', 'expected salary']):
        return 'salary'
    if any(w in q for w in ['years of experience', 'years experience', 'how many years']):
        return 'experience'
    if any(w in q for w in ['relocate', 'relocation', 'willing to move']):
        return 'relocation'
    if any(w in q for w in ['remote', 'on-site', 'hybrid', 'work arrangement']):
        return 'remote'
    if any(w in q for w in ['start date', 'when can you start', 'availability']):
        return 'start_date'
    if any(w in q for w in ['cover letter', 'why this role', 'why are you interested', 'tell us about']):
        return 'cover_letter'
    if any(w in q for w in ['gender', 'race', 'ethnicity', 'veteran', 'disability', 'demographic']):
        return 'eeo'

    return 'custom'


def get_quick_answer(question: str, profile: dict) -> str | None:
    """Try to answer common questions without AI call — first from the
    profile's screening_answers bank, then by question type."""
    q = question.lower()
    bank = profile.get('screening_answers', {})
    bank_patterns = [
        (('notice period',), 'notice_period'),
        (('reference',), 'references'),
        (('language', 'english proficiency'), 'languages'),
        (('security clearance', 'active clearance'), 'security_clearance'),
        (('travel',), 'travel'),
        (('previously applied', 'applied to', 'applied before'), 'previously_applied'),
        (('referred', 'referral'), 'referred_by'),
        (('hear about', 'how did you'), 'how_heard'),
        (('salary range', 'compensation expectation'), 'salary_range'),
    ]
    # word-boundary match: plain `in` made every "preferred ..." question match
    # "referred" and answer "No" — including "Preferred name" and "Preferred
    # work arrangement".
    for patterns, key in bank_patterns:
        if not bank.get(key):
            continue
        if any(re.search(rf'\b{re.escape(p)}', q) for p in patterns):
            return bank[key]

    # "list tools/software/systems you have experience with" is canon on PM
    # forms and the AI fallback kept leaking meta-text into it (8/23) — the
    # profile's own skills list answers it deterministically
    if any(w in q for w in ('tools', 'software', 'systems', 'technologies',
                            'platforms')) and \
       any(w in q for w in ('experience', 'utiliz', 'proficien', 'familiar',
                            'used', 'list')):
        skills = profile.get('skills') or []
        if skills:
            return ', '.join(str(s) for s in skills[:12])

    # "list relevant certifications" got a question-echo from the free model
    # (8/23 LTS dry run) — the profile knows the real answer
    if 'certification' in q or 'certificate' in q:
        certs = profile.get('certifications') or []
        if certs:
            return '; '.join(str(c) for c in certs)

    qtype = classify_question(question)

    quick_answers = {
        'work_auth': 'Yes',
        'sponsorship': 'No',
        # a range, not a bare number: a single figure gets auto-screened out
        # whenever it sits above the posted band, and it kills the negotiation
        # before a human ever reads the resume. numeric-only fields still get
        # the plain number (see below).
        'salary': bank.get('salary_range') or profile.get('desired_salary', '120000'),
        'experience': profile.get('years_experience', '7'),
        'relocation': 'No' if profile.get('remote_only') else 'Yes',
        'remote': 'Remote',
        'start_date': bank.get('available_start', 'Available to start within 2 weeks of offer acceptance.'),
        'eeo': 'Decline to self-identify',
    }

    answer = quick_answers.get(qtype)

    # an hourly-rate field must never get the annual number (120000/hr, 8/23)
    if qtype == 'salary' and any(w in q for w in ('hourly', 'per hour', '/hr')):
        try:
            annual = float(str(profile.get('desired_salary', '120000')).replace(',', ''))
            return str(round(annual / 2080))
        except (TypeError, ValueError):
            return None

    # numeric-only salary inputs reject "$110,000 - $130,000" outright, so a
    # field that clearly wants digits gets the midpoint instead of the range
    if qtype == 'salary' and answer and any(
            w in q for w in ('number', 'numeric', 'digits only', 'usd amount')):
        return str(profile.get('desired_salary', '120000'))
    return answer
