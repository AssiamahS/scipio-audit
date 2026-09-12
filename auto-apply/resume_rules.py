#!/usr/bin/env python3
"""RESUME_RULES.md, enforced. lint(md, pdf) -> list of failures (empty = sendable).

Runs on every static variant (pipeline test step, scorecard) and on every
tailored artifact (tailor.py manifest -> applier blocks with blocked_rules).
Pure text checks; the only I/O is optional PDF page/size/image inspection.

CLI: uv run python resume_rules.py [<file.md> ...]   (default: all static variants)
Exit 1 on any failure.
"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).parent
REPO = HERE.parent

BANNED = ['responsible for', 'duties included', 'helped with', 'worked on', 'spearheaded',
          'leveraged', 'orchestrated', 'synergy', 'results-driven', 'passionate', 'seamlessly', 'dynamic']
WEAK_OPENERS = {'the', 'a', 'an', 'and', 'was', 'were', 'helped', 'responsible', 'i', 'my'}
DATE_RX = re.compile(r'^(January|February|March|April|May|June|July|August|September|October|November|December) \d{4} [–-] '
                     r'((January|February|March|April|May|June|July|August|September|October|November|December) \d{4}|Present)$')
HEADERS = ['SUMMARY', 'EXPERIENCE', 'SKILLS', 'EDUCATION', 'CERTIFICATIONS']
FAKE_EMPLOYERS = ['Healthcare IT & Technology Consulting', 'Contract Project Management, Healthcare']
MAX_WORDS, MAX_BULLET_CHARS, MIN_TOTAL, MAX_TOTAL, METRIC_RATE = 720, 230, 15, 26, 0.7  # words are a proxy; the PDF page count is the hard rule
# bullets that are honestly just a scope statement can go without a number,
# but the first bullet of a role must carry one
NUMBER_RX = re.compile(r'\d|\$|%')


def parse(md: str) -> dict:
    """Roles as ordered list of {title, dates, header, employers:[{name, bullets}]}."""
    roles, cur, emp = [], None, None
    section = None
    for raw in md.splitlines():
        line = raw.rstrip()
        if line.startswith('## '):
            section = line[3:].strip().upper()
            cur = emp = None
            continue
        if section != 'EXPERIENCE':
            continue
        if line.startswith('### '):
            cur = {'header': line[4:].strip(), 'title': '', 'dates': '', 'employers': []}
            roles.append(cur); emp = None
        elif line.startswith('**') and cur is not None:
            m = re.match(r'\*\*(.+?)\*\*\s*\|\s*(.+)$', line)
            if m and not cur['title']:
                cur['title'], cur['dates'] = m.group(1).strip(), m.group(2).strip()
            else:
                emp = {'name': re.sub(r'\*', '', line).strip(), 'bullets': []}
                cur['employers'].append(emp)
        elif line.startswith('- ') and cur is not None:
            if emp is None:
                emp = {'name': cur['header'], 'bullets': [], 'implicit': True}
                cur['employers'].append(emp)
            emp['bullets'].append(line[2:].strip())
    return {'roles': roles}


_MONTHS = {m: i for i, m in enumerate(['January', 'February', 'March', 'April', 'May', 'June', 'July',
                                        'August', 'September', 'October', 'November', 'December'], 1)}


def _months(dates: str) -> int:
    """Length of a 'Month YYYY – Month YYYY|Present' span in months; 99 when unparseable."""
    try:
        a, z = [x.strip() for x in re.split(r'\s[–-]\s', dates)]
        ma, ya = a.split(); start = int(ya) * 12 + _MONTHS[ma]
        if z.lower() == 'present':
            from datetime import date
            end = date.today().year * 12 + date.today().month
        else:
            mz, yz = z.split(); end = int(yz) * 12 + _MONTHS[mz]
        return max(1, end - start)
    except Exception:
        return 99


def lint(md: str, pdf: str | None = None) -> list:
    f = []
    words = len(md.split())
    if words > MAX_WORDS:
        f.append(f'length: {words} words > {MAX_WORDS} (one page)')
    for h in HEADERS:
        if not re.search(rf'^## {h}\b', md, re.M):
            f.append(f'structure: missing ## {h}')
    order = [h for h in HEADERS if re.search(rf'^## {h}\b', md, re.M)]
    if order != [h for h in HEADERS if h in order]:
        f.append('structure: sections out of order')
    lines = md.splitlines()
    if not lines or not lines[0].startswith('# '):
        f.append('structure: first line must be the name')
    contact = next((l for l in lines[1:5] if '@' in l), '')
    for need in ('@', 'linkedin.com/in/', re.compile(r'\d{3}-\d{3}-\d{4}')):
        ok = need.search(contact) if hasattr(need, 'search') else need in contact
        if not ok:
            f.append('contact: email | phone | linkedin must sit directly under the name')
    if re.search(r'\b(I|my|me)\b', md):
        f.append('voice: first-person pronoun')
    low = md.lower()
    for w in BANNED:
        if w in low:
            f.append(f'voice: banned phrase "{w}"')
    for fe in FAKE_EMPLOYERS:
        if fe in md:
            f.append(f'titles: umbrella employer "{fe}" reads as a made-up company')

    roles = parse(md)['roles']
    if not roles:
        f.append('experience: no roles parsed')
    all_bullets = []
    counts = []
    for i, r in enumerate(roles):
        if not r['title']:
            f.append(f'titles: role "{r["header"][:40]}" has no **Title** | dates line')
        if r['dates'] and not DATE_RX.match(r['dates']):
            f.append(f'dates: "{r["dates"]}" is not "Month YYYY – Month YYYY|Present"')
        if '/' in r['title'] and r['employers'] and not r['employers'][0].get('implicit'):
            f.append(f'titles: slash-title "{r["title"]}" over sub-engagements — use one umbrella title')
        role_bullets = [b for e in r['employers'] for b in e['bullets']]
        # weight is judged per employer block: a grouped contract period is
        # three engagements, not one nine-bullet job
        counts.append(max(len(e['bullets']) for e in r['employers']) if r['employers'] else 0)
        all_bullets += role_bullets
        current = i == 0
        months = _months(r['dates'])
        short = months <= 4   # Altice (3 mo), Catalyst (4 mo): 2-3 bullets
        # 9/9: a role under 18 months (Heartbeat 14, FCB 15) carries 2-4 —
        # the bank holds 2-3 distinct claims each and tailor.py now drops
        # re-worded duplicates rather than ship the same claim twice
        mid = 4 < months <= 18
        old_role = bool(re.search(r'\b(201[0-8])\b', r['dates'].split('–')[0] if '–' in r['dates'] else r['dates']))
        side = (not current) and r['dates'].strip().lower().endswith('present')   # concurrent side venture (QAW)
        lo, hi = (4, 6) if current else ((2, 3) if (short or old_role or side) else ((2, 4) if mid else (3, 5)))
        for e in r['employers']:
            n = len(e['bullets'])
            e_lo = 3 if len(r['employers']) > 1 else lo
            e_hi = 5 if len(r['employers']) > 1 else hi
            if n < e_lo:
                f.append(f'bullets: {e["name"][:45]} has {n} bullet(s), rule is {e_lo}–{e_hi}')
            if n > e_hi:
                f.append(f'bullets: {e["name"][:45]} has {n} bullets, rule is {e_lo}–{e_hi}')
            if e['bullets'] and any(NUMBER_RX.search(b) for b in e['bullets']) \
                    and not NUMBER_RX.search(e['bullets'][0]):
                f.append(f'order: first bullet under {e["name"][:40]} carries no number')
    if counts and max(counts) != counts[0]:
        f.append(f'weight: current role has {counts[0]} bullets but an older role has {max(counts)}')
    if all_bullets:
        if not MIN_TOTAL <= len(all_bullets) <= MAX_TOTAL:
            f.append(f'bullets: {len(all_bullets)} total, rule is {MIN_TOTAL}–{MAX_TOTAL}')
        with_num = sum(1 for b in all_bullets if NUMBER_RX.search(b))
        if with_num / len(all_bullets) < METRIC_RATE:
            f.append(f'metrics: {with_num}/{len(all_bullets)} bullets carry a number (< {int(METRIC_RATE*100)}%)')
        for b in all_bullets:
            if len(b) > MAX_BULLET_CHARS:
                f.append(f'length: bullet > {MAX_BULLET_CHARS} chars: "{b[:50]}…"')
            if b.split()[0].lower() in WEAK_OPENERS:
                f.append(f'voice: weak opener: "{b[:50]}…"')
            if b.endswith('.') != all_bullets[0].endswith('.'):
                f.append('consistency: mixed bullet punctuation')
                break

    # skills: no duplicates across categories
    sk = re.search(r'^## SKILLS\n(.*?)(?=^## |\Z)', md, re.M | re.S)
    if sk:
        seen, dup = set(), set()
        for line in sk.group(1).splitlines():
            m = re.match(r'\*\*(.+?):\*\*\s*(.+)', line)
            if not m:
                continue
            for item in m.group(2).split(','):
                k = item.strip().lower()
                if k in seen:
                    dup.add(k)
                seen.add(k)
        if dup:
            f.append(f'skills: listed twice: {", ".join(sorted(dup))}')

    if pdf and Path(pdf).exists():
        try:
            from pypdf import PdfReader
            r = PdfReader(pdf)
            if len(r.pages) != 1:
                f.append(f'pdf: {len(r.pages)} pages, rule is 1')
            if Path(pdf).stat().st_size > 200_000:
                f.append('pdf: over 200KB')
            try:
                if any(p.images for p in r.pages):
                    f.append('pdf: embedded image')
            except Exception:
                pass
            if len((r.pages[0].extract_text() or '').strip()) < 500:
                f.append('pdf: text does not extract (image-only?)')
        except Exception as e:
            f.append(f'pdf: unreadable ({e})')
    return f


def main() -> int:
    targets = [Path(a) for a in sys.argv[1:]] or sorted((REPO / 'resumes-src').glob('*.md'))
    bad = 0
    for md in targets:
        pdf = None
        if md.parent.name == 'resumes-src':
            from resume_score import VARIANTS
            pdf = next((str(REPO / 'resumes' / n) for n, p in VARIANTS.items() if p.name == md.name), None)
        else:
            cand = md.with_suffix('.pdf')
            pdf = str(cand) if cand.exists() else None
        fails = lint(md.read_text(), pdf)
        print(f'{md.name}: {"PASS" if not fails else "FAIL"}')
        for x in fails:
            print('   -', x)
        bad += bool(fails)
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
