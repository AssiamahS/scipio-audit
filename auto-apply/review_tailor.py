"""Tailor one JD for the swipe-review page (review.html on GitHub Pages).

Called by .github/workflows/tailor-review.yml with a JD URL or pasted text:
fetches the JD (board API when known, readable text otherwise), runs the
same tailor the applier uses, and writes reviews/<id>/{meta.json, resume.md,
resume.html, resume.pdf} plus reviews/index.json. The page reads those
straight off Pages; approve/reject + reason land in reviews/feedback/<id>.json
via the GitHub contents API and are read back here (`--feedback` summary)
and in the next Claude session.

  uv run python review_tailor.py --id 20260909-1430 --url https://... [--jd-text-file f] [--role "..."]
  uv run python review_tailor.py --feedback
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
REPO = HERE.parent
REVIEWS = REPO / 'reviews'
sys.path.insert(0, str(HERE))


def slug(s: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', (s or '').lower()).strip('-')[:40] or 'job'


def tailor_one(rid: str, url: str, jd_text: str, role: str) -> dict:
    from fit_check import fetch_jd
    from tailor import build_tailored
    from ats_lint import extract_pdf_text, keyword_score

    jd = {'role': role, 'company': '', 'description': jd_text or '', 'posted': ''}
    if url and not jd_text:
        try:
            jd = fetch_jd(url)
        except Exception as e:
            jd['error'] = f'fetch failed: {e}'
    if role:
        jd['role'] = role
    if len(jd.get('description') or '') < 200:
        jd['error'] = jd.get('error') or 'JD too short — paste the text instead of the URL'

    out_dir = REVIEWS / rid
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / 'jd.txt').write_text(jd.get('description') or '')
    meta = {'id': rid, 'url': url, 'company': jd.get('company', ''), 'role': jd.get('role', ''),
            'created': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'jd_chars': len(jd.get('description') or ''), 'error': jd.get('error'),
            'status': 'pending'}
    if not meta['error']:
        pdf = out_dir / 'resume.pdf'
        try:
            manifest = build_tailored(jd['description'], jd.get('role', ''), str(pdf))
        except Exception as e:
            manifest = None
            meta['error'] = f'tailor failed: {e}'
        if manifest:
            meta['manifest'] = manifest
            # the applier writes md/html next to the pdf under the same stem
            for ext in ('md', 'html'):
                src = pdf.with_suffix('.' + ext)
                if src.exists() and src != out_dir / f'resume.{ext}':
                    (out_dir / f'resume.{ext}').write_text(src.read_text())
            try:
                rtext, _ = extract_pdf_text(str(pdf))
                kw = keyword_score(rtext, jd['description'], jd.get('role', ''))
                meta['keyword_coverage'] = kw.get('score')
                meta['keywords_missing'] = kw.get('missing', [])[:12]
                meta['keywords_present'] = kw.get('present', [])[:20]
            except Exception as e:
                meta['lint_error'] = str(e)
        elif not meta['error']:
            meta['error'] = 'tailoring not possible (rules floor not met — see resume_rules.py)'
    (out_dir / 'meta.json').write_text(json.dumps(meta, indent=1) + '\n')
    return meta


def rebuild_index() -> list:
    rows = []
    for d in sorted(REVIEWS.glob('*/meta.json'), reverse=True):
        try:
            m = json.loads(d.read_text())
        except json.JSONDecodeError:
            continue
        fb = REVIEWS / 'feedback' / f"{m['id']}.json"
        if fb.exists():
            try:
                m['feedback'] = json.loads(fb.read_text())
                m['status'] = m['feedback'].get('verdict', m.get('status'))
            except json.JSONDecodeError:
                pass
        rows.append({k: m.get(k) for k in ('id', 'url', 'company', 'role', 'created', 'status', 'error',
                                             'keyword_coverage', 'keywords_missing', 'feedback')})
    rows.sort(key=lambda r: r.get('created') or '', reverse=True)
    (REVIEWS / 'index.json').write_text(json.dumps(rows, indent=1) + '\n')
    return rows


def feedback_summary() -> int:
    rows = rebuild_index()
    fb = [r for r in rows if r.get('feedback')]
    print(f'{len(rows)} review(s), {len(fb)} with feedback')
    for r in fb:
        f = r['feedback']
        print(f"  [{f.get('verdict', '?'):>7}] {r.get('company')} — {r.get('role')} ({r['id']})")
        if f.get('reason'):
            print(f"           {f['reason'][:200]}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--id')
    ap.add_argument('--url', default='')
    ap.add_argument('--jd-text-file')
    ap.add_argument('--role', default='')
    ap.add_argument('--feedback', action='store_true', help='print every swipe verdict + reason')
    a = ap.parse_args()
    if a.feedback:
        return feedback_summary()
    if not a.id:
        ap.error('--id required')
    jd_text = Path(a.jd_text_file).read_text() if a.jd_text_file else ''
    meta = tailor_one(a.id, a.url, jd_text, a.role)
    rebuild_index()
    print(json.dumps({k: v for k, v in meta.items() if k != 'manifest'}, indent=1))
    return 0 if not meta.get('error') else 1


if __name__ == '__main__':
    raise SystemExit(main())
