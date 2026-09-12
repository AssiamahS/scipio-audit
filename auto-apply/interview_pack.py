#!/usr/bin/env python3
"""The moment an interview email lands: reply draft + prep brief + phone alert.

Blue Water Thinking asked for availability on 7/23; nobody saw it; they
rejected him 7/28. That is the single most expensive failure this pipeline
has had, so the response is built the same minute the mail is detected:

  1. Gmail draft, threaded on the recruiter's message, offering three real
     windows over the next business days (ET). He opens Drafts, checks the
     times, hits send. Never auto-sent: only he knows his calendar.
  2. briefs/interview-<company>-<date>.md: role, JD summary, his six most
     relevant bullets from bank.json, likely questions, things to ask them.
  3. Alert to his personal inbox with the draft text inline.

Called from rejections.py; also runnable by hand:
  uv run python interview_pack.py --job 123 --from "Jane <jane@co.com>" --subject "..."
"""
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
BRIEFS = ROOT / 'briefs'
ET = ZoneInfo('America/New_York')

sys.path.insert(0, str(HERE))
import mailer  # noqa: E402

REPLY = """Hi{greeting},

Thank you for reaching out about the {role} role. I would be glad to talk.

Here are a few windows that work on my end, Eastern time:
- {w1}
- {w2}
- {w3}

If none of those fit, send me what works for you and I will make it happen.

Best,
Sylvester Assiamah
<phone>
"""

LIKELY_QUESTIONS = [
    'Walk me through a project you ran end to end. What went wrong and what did you change?',
    'How do you keep doctors and department leads informed without slowing the work down?',
    'Tell me about the storage rebuild behind the pre-op imaging fix. How did you find the bottleneck?',
    'How do you prioritize when 200 people all think their ticket is the urgent one?',
    'Describe your experience with MLR review or regulated content. What did compliance change about your process?',
    'What does your first 30 days in this role look like?',
    'Why this company, and why now?',
]
ASK_THEM = [
    'What does success look like at 90 days for this role?',
    'Who does this role work with day to day, and who does it report to?',
    'What is the biggest project on the team\'s plate this quarter?',
    'What are the next steps and the timeline for the decision?',
]


def windows(start: datetime | None = None) -> list[str]:
    """Three slots on the next three business days, ET, morning then afternoon."""
    now = start or datetime.now(ET)
    day = now
    out = []
    slots = [('10:00', '11:30'), ('14:00', '15:30'), ('11:00', '12:30')]
    while len(out) < 3:
        day = day + timedelta(days=1)
        if day.weekday() >= 5:
            continue
        a, b = slots[len(out)]
        out.append(f"{day.strftime('%A %b %d')}, {a} to {b} ET".replace(' 0', ' '))
    return out


def _tokens(text: str) -> set:
    return {t for t in re.findall(r'[a-z][a-z0-9+.#-]{2,}', (text or '').lower())}


def top_bullets(jd: str, n: int = 6) -> list[str]:
    try:
        bank = json.loads((HERE / 'bank.json').read_text())
    except Exception:
        return []
    jd_t = _tokens(jd)
    scored = []

    def walk(obj):
        if isinstance(obj, dict):
            if obj.get('text') and obj.get('approved', True):
                scored.append((len(_tokens(obj['text']) & jd_t), obj['text']))
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)
        elif isinstance(obj, str) and len(obj) > 40 and jd_t:
            scored.append((len(_tokens(obj) & jd_t), obj))
    walk(bank)
    seen, out = set(), []
    for _, t in sorted(scored, key=lambda x: -x[0]):
        if t not in seen:
            seen.add(t)
            out.append(mailer.clean_voice(t))
        if len(out) >= n:
            break
    return out


def jd_summary(jd: str, company: str, role: str) -> str:
    """AI summary when a brain is reachable, first 900 chars otherwise."""
    jd = (jd or '').strip()
    if not jd:
        return 'No JD captured on the job record. Pull the posting before the call.'
    try:
        from ai_engine import _complete
        text = _complete(
            f"Summarize this job description for an interview prep sheet in 5 short bullets: "
            f"what the role owns, the top 3 must-haves, and one thing the company clearly cares about. "
            f"Plain words, no marketing language, no em dashes.\n\nCompany: {company}\nRole: {role}\n\n{jd[:6000]}",
            350)
        text = text.strip()
        if len(text) > 40:
            return mailer.clean_voice(text)
    except Exception as e:
        print(f'  [~] ai summary unavailable ({e}); using excerpt')
    return jd[:900] + ('...' if len(jd) > 900 else '')


def build_brief(job: dict, action: dict) -> Path:
    company = (job or {}).get('company') or action.get('company') or 'unknown'
    role = (job or {}).get('role') or 'the role'
    date = action.get('date', datetime.now(ET).isoformat())[:10]
    slug = re.sub(r'[^a-z0-9]+', '-', company.lower()).strip('-')
    path = BRIEFS / f'interview-{slug}-{date}.md'
    jd = (job or {}).get('description') or ''
    bullets = top_bullets(jd) if jd else top_bullets(role)
    lines = [
        f'# Interview prep: {company} : {role}',
        '',
        f"- Request received: {action.get('date', '')[:16]} from {action.get('from', '')}",
        f"- Subject: {action.get('subject', '')}",
        f"- Posting: {(job or {}).get('url', '')}",
        f"- Job id: #{(job or {}).get('id')}",
        '',
        '## Reply draft (in Gmail Drafts, check the times, then send)',
        '',
        '```',
        action.get('reply_text', '').rstrip(),
        '```',
        '',
        '## What the role is',
        '',
        jd_summary(jd, company, role),
        '',
        '## Your strongest matching points',
        '',
    ] + [f'- {b}' for b in bullets] + [
        '',
        '## Questions to expect',
        '',
    ] + [f'- {q}' for q in LIKELY_QUESTIONS] + [
        '',
        '## Ask them',
        '',
    ] + [f'- {q}' for q in ASK_THEM] + [
        '',
        '## Before the call',
        '',
        '- Reread the posting and the resume version that went out (see apply_log for this job id).',
        '- Have two stories ready with a number in each: the 40% scan rendering fix and the 15+ device rollout.',
        '- Quiet room, headphones, water, notes open. Join two minutes early.',
        '',
    ]
    BRIEFS.mkdir(exist_ok=True)
    path.write_text('\n'.join(lines))
    return path


def handle(action: dict, job: dict | None, dry_run: bool = False) -> dict:
    """Build draft + brief + alert for one interview request."""
    company = (job or {}).get('company') or action.get('company') or 'the team'
    role = (job or {}).get('role') or 'the role'
    name, _ = mailer.parse_addr(action.get('from', ''))
    first = name.split()[0] if name and not any(ch.isdigit() for ch in name) and '@' not in name else ''
    w = windows()
    reply = REPLY.format(greeting=f' {first}' if first else '', role=role, w1=w[0], w2=w[1], w3=w[2])
    action = dict(action, reply_text=reply)

    to = mailer.reply_target(action.get('from', ''), action.get('reply_to', ''))
    subj = action.get('subject', '')
    subj = subj if subj.lower().startswith('re:') else f'Re: {subj}'
    drafted = mailer.save_draft(to or '', subj, reply, in_reply_to=action.get('message_id'),
                                dry_run=dry_run)
    brief = build_brief(job, action)
    body = (f"{company} wants to talk about {role}.\n\n"
            f"From: {action.get('from', '')}\nSubject: {action.get('subject', '')}\n\n"
            f"A reply is sitting in Gmail Drafts on the 105 account"
            f"{'' if to else ' (no human address found, add the recipient)'}. "
            f"Check the three windows and send it today. Interview requests expire.\n\n"
            f"Prep brief: {brief.relative_to(ROOT)}\n\n---\n{reply}")
    alerted = mailer.alert(f"INTERVIEW: {company} : {role}", body,
                           key=f"interview:{action.get('message_id') or subj}", dry_run=dry_run)
    print(f"  [!!] interview pack: draft={'yes' if drafted else 'no'} brief={brief.name} alert={'yes' if alerted else 'dup/no'}")
    return {'draft': drafted, 'brief': str(brief), 'alert': alerted, 'to': to}


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--job', type=int)
    ap.add_argument('--from', dest='frm', default='')
    ap.add_argument('--subject', default='Interview availability')
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    db = json.loads((ROOT / 'jobs.json').read_text())
    job = next((j for j in db['jobs'] if str(j.get('id')) == str(a.job)), None)
    handle({'from': a.frm, 'subject': a.subject, 'date': datetime.now(ET).isoformat(),
            'message_id': '', 'company': (job or {}).get('company')}, job, dry_run=a.dry_run)
