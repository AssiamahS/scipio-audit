"""Pull a clean job description (+ company/role) out of any job posting page.

Tier ladder, same order the serious tools use (roasted-extension, karobar):
  1. schema.org/JobPosting JSON-LD in the HTML (iCIMS, LinkedIn, Lever,
     rendered Workday, most SEO-conscious ATSs embed it for Google Jobs)
  2. known per-ATS content containers
  3. whole-page text with the nav/cookie-wall junk lines filtered out

jd_from_jsonld/clean_page_text are pure functions so they can be tested on
curl'd HTML without a browser; extract_jd drives them from a Playwright page
(which also covers JS-rendered pages, since page.content() is post-render).

Tier 0 sits above all of that: the board's own public API. Application-form
pages (job-boards.greenhouse.io/.../jobs/<id>) carry no JD at all, so DOM
extraction returns "Attach / Dropbox / Google Drive" boilerplate — that
garbage then feeds tailoring and keyword scoring (zocdoc scored ats 0 this
way). The API always has the real posting text.
"""

import html as html_mod
import json
import re

# main-content containers, most specific first
JD_SELECTORS = [
    '.iCIMS_JobContent',                                  # iCIMS
    '[data-automation-id="jobPostingDescription"]',       # Workday
    '#content .job__description, #content',               # Greenhouse
    '.posting .section-wrapper, .posting',                # Lever
    '.job-description, .jobDescription, #job-description',
    'article',
    'main',
]

# lines that are page chrome, not JD — matched case-insensitively on whole line
_JUNK_LINE = re.compile(
    r'^(login|log in|sign in|sign up|register|welcome page|enter your information'
    r'|email|password|apply now|save job|share|print|back to search'
    r'|please enable cookies.*|cookie.*polic.*|accept.*cookies.*|privacy policy'
    r'|terms of use|©.*|all rights reserved.*|skip to (main )?content|menu|search jobs?'
    r'|home|careers?|faq|help|language|follow us.*)$',
    re.IGNORECASE,
)


def _strip_html(fragment: str) -> str:
    text = re.sub(r'<(script|style)[^>]*>.*?</\1>', ' ', fragment, flags=re.S | re.I)
    text = re.sub(r'<br\s*/?>|</p>|</li>|</div>|</h[1-6]>', '\n', text, flags=re.I)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = html_mod.unescape(text)
    text = re.sub(r'[ \t ]{2,}', ' ', text)
    text = re.sub(r'\n\s*\n+', '\n', text)
    return text.strip()


def _walk_for_jobposting(node):
    """Find a JobPosting object anywhere in a decoded JSON-LD document."""
    if isinstance(node, list):
        for item in node:
            hit = _walk_for_jobposting(item)
            if hit:
                return hit
        return None
    if not isinstance(node, dict):
        return None
    node_type = node.get('@type')
    types = node_type if isinstance(node_type, list) else [node_type]
    if any(t == 'JobPosting' for t in types if isinstance(t, str)):
        return node
    return _walk_for_jobposting(node.get('@graph'))


def jd_from_jsonld(page_html: str) -> dict | None:
    """Tier 1: schema.org JobPosting JSON-LD → {'description','role','company'}."""
    for m in re.finditer(
            r'<script[^>]*type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            page_html, re.S | re.I):
        raw = m.group(1).strip()
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError:
            # some sites leave literal newlines/tabs inside strings
            try:
                doc = json.loads(re.sub(r'[\x00-\x1f]', ' ', raw))
            except json.JSONDecodeError:
                continue
        posting = _walk_for_jobposting(doc)
        if not posting:
            continue
        description = _strip_html(posting.get('description') or '')
        if len(description) < 200:      # JSON-LD stub without the real JD
            continue
        org = posting.get('hiringOrganization')
        company = org.get('name') if isinstance(org, dict) else (
            org if isinstance(org, str) else None)
        return {
            'description': description,
            'role': posting.get('title') or None,
            'company': company,
            'jd_source': 'json-ld',
        }
    return None


def clean_page_text(text: str) -> str:
    """Tier 3: drop chrome/junk lines from whole-page text."""
    kept = []
    for line in text.splitlines():
        line = line.strip()
        if not line or _JUNK_LINE.match(line):
            continue
        kept.append(line)
    return '\n'.join(kept)


async def jd_from_api(url: str) -> dict | None:
    """Tier 0: the board's own public API — real posting text even when the
    URL lands on an application-form page that renders no JD."""
    import httpx
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(url or '')
        host = parts.netloc.lower()
        segs = [s for s in parts.path.split('/') if s]
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as c:
            if 'greenhouse.io' in host and 'jobs' in segs and segs[0] not in ('embed', 'jobs'):
                jid = next((s for s in segs[segs.index('jobs') + 1:] if s.isdigit()), None)
                if jid:
                    r = await c.get(f'https://boards-api.greenhouse.io/v1/boards/{segs[0]}/jobs/{jid}')
                    if r.status_code == 200:
                        d = r.json()
                        # content arrives entity-escaped (&lt;p&gt;), so
                        # unescape before stripping tags
                        desc = _strip_html(html_mod.unescape(d.get('content') or ''))
                        if len(desc) >= 200:
                            return {'description': desc, 'role': d.get('title'),
                                    'company': d.get('company_name') or segs[0],
                                    'jd_source': 'board-api'}
            elif 'jobs.lever.co' in host and len(segs) >= 2:
                r = await c.get(f'https://api.lever.co/v0/postings/{segs[0]}/{segs[1]}')
                if r.status_code == 200:
                    d = r.json()
                    pieces = [d.get('descriptionPlain') or _strip_html(d.get('description') or '')]
                    for sec in d.get('lists') or []:
                        pieces.append(sec.get('text') or '')
                        pieces.append(_strip_html(sec.get('content') or ''))
                    pieces.append(d.get('additionalPlain') or '')
                    desc = '\n'.join(p for p in pieces if p).strip()
                    if len(desc) >= 200:
                        return {'description': desc, 'role': d.get('text'),
                                'company': segs[0], 'jd_source': 'board-api'}
            elif 'jobs.ashbyhq.com' in host and len(segs) >= 2:
                r = await c.get(f'https://api.ashbyhq.com/posting-api/job-board/{segs[0]}')
                if r.status_code == 200:
                    target = segs[1].removesuffix('/application')
                    for j in r.json().get('jobs', []):
                        if j.get('id') == target or target in (j.get('jobUrl') or ''):
                            desc = _strip_html(j.get('descriptionHtml') or '') or (j.get('descriptionPlain') or '')
                            if len(desc) >= 200:
                                return {'description': desc, 'role': j.get('title'),
                                        'company': segs[0], 'jd_source': 'board-api'}
    except Exception:
        pass
    return None


async def extract_jd(page) -> dict:
    """Best JD the page offers. Always returns at least a description key."""
    hit = await jd_from_api(page.url)
    if hit:
        return hit

    try:
        hit = jd_from_jsonld(await page.content())
        if hit:
            return hit
    except Exception:
        pass

    for sel in JD_SELECTORS:
        try:
            text = await page.locator(sel).first.inner_text(timeout=1500)
        except Exception:
            continue
        text = clean_page_text(text)
        if len(text) > 300:
            return {'description': text, 'jd_source': sel}

    try:
        body = await page.locator('body').inner_text(timeout=5000)
        return {'description': clean_page_text(body), 'jd_source': 'body-filtered'}
    except Exception:
        return {'description': '', 'jd_source': 'none'}
