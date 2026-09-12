#!/usr/bin/env python3
"""Rebuild the static resume variants FROM bank.json (single source of truth).

resumes-src/*.md, resumes/*.pdf and the tailored artifacts all come from the
same approved bank now — the hand-edited md files drifted (four wordings of
the same bullet, one-bullet employers, a made-up umbrella company).

  uv run python build_static.py                      # rebuild the 4 shipping variants
  uv run python build_static.py --include-pending --out-dir /tmp/preview   # PREVIEW with unapproved bullets
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))
from tailor import pick_bullets, to_markdown, tokens, PDF_MARGIN  # noqa: E402
from resume_rules import lint  # noqa: E402

# key -> (summary id, pdf name, sector hint). The hint is a fake mini-JD:
# pick_bullets orders each role's bullets by echo against it, so the three
# sector bases (9/12, Sylvester: "base resumes for healthcare, finance and
# tech, then tailor to the JD") lead with the bullets that sector reads first.
# Empty hint = pure bank order.
HEALTHCARE_HINT = ('healthcare hospital clinical patient care physicians nurses clinicians '
                   'medical devices Epic EHR EMR HIPAA pharma MLR review FDA pharmacy compliance '
                   'stakeholder project management timelines implementation')
FINANCE_HINT = ('financial analysis budget forecasting pricing model margins revenue savings '
                'vendor costs procurement reporting SQL Excel data analysis compliance production '
                'orders contract revenue pipeline accounts reimbursement')
TECH_HINT = ('infrastructure systems engineer storage VMware Windows Server ServiceNow technical '
             'support troubleshooting automation Python Playwright CI/CD GitHub Actions Swift iOS '
             'Cloudflare Linux identity access SailPoint Okta network')
VARIANTS = {
    'healthcare': ('sum-pm', 'Resume - Sylvester Assiamah (Healthcare).pdf', HEALTHCARE_HINT),
    'finance': ('sum-finance', 'Resume - Sylvester Assiamah (Finance).pdf', FINANCE_HINT),
    'tech': ('sum-infra', 'Resume - Sylvester Assiamah (Tech).pdf', TECH_HINT),
    'project-manager': ('sum-pm', 'Resume - Sylvester Assiamah (Project Manager).pdf', ''),
    'infrastructure-engineer': ('sum-infra', 'Resume - Sylvester Assiamah (Infrastructure Engineer).pdf', ''),
    'chs-project-manager': ('sum-pm', 'Resume - Sylvester Assiamah (CHS Project Manager).pdf', ''),
    'eclinical-project-manager': ('sum-pm', 'Resume - Sylvester Assiamah (eClinical Project Manager).pdf', ''),
}


def _pages(pdf_path: Path) -> int:
    try:
        from pypdf import PdfReader
        return len(PdfReader(str(pdf_path)).pages)
    except Exception:
        return 1


def render_pdf(md_path: Path, pdf_path: Path) -> None:
    """Render; if it spills past one page, re-render compact (same font,
    tighter leading), like tailor.py does. 9/5: six dated roles instead of
    four pushed every static variant to page 2 at normal leading."""
    html_path = md_path.with_suffix('.html')
    from playwright.sync_api import sync_playwright
    for compact in (False, True):
        cmd = [sys.executable, str(HERE / 'render_resume.py'), str(md_path), str(html_path)]
        if compact:
            cmd.append('--compact')
        subprocess.run(cmd, check=True, capture_output=True)
        with sync_playwright() as p:
            b = p.chromium.launch()
            page = b.new_page()
            page.goto(f'file://{html_path.resolve()}')
            page.pdf(path=str(pdf_path), format='Letter', margin=PDF_MARGIN)
            b.close()
        if _pages(pdf_path) == 1:
            return


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--include-pending', action='store_true', help='PREVIEW ONLY')
    ap.add_argument('--out-dir', help='write md/html/pdf here instead of resumes-src/ + resumes/')
    ap.add_argument('--only', help='one variant key')
    a = ap.parse_args()
    bank = json.loads((HERE / 'bank.json').read_text())
    out_dir = Path(a.out_dir) if a.out_dir else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
    bad = 0
    for key, (sum_id, pdf_name, hint) in VARIANTS.items():
        if a.only and key != a.only:
            continue
        summary = next(s['text'] for s in bank['summaries'] if s['id'] == sum_id)
        # empty hint: pure bank order, strongest numbered bullet first per role
        chosen, _ = pick_bullets(bank, tokens(hint), include_pending=a.include_pending, keep_all=True)
        md = to_markdown(bank, summary, chosen)
        md_path = (out_dir or REPO / 'resumes-src') / f'{key}.md'
        pdf_path = (out_dir or REPO / 'resumes') / pdf_name
        md_path.write_text(md)
        render_pdf(md_path, pdf_path)
        fails = lint(md, str(pdf_path))
        print(f'{key}: {"PASS" if not fails else "FAIL"} -> {pdf_path.name} ({len(md.split())} words)')
        for x in fails:
            print('   -', x)
        bad += bool(fails)
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
