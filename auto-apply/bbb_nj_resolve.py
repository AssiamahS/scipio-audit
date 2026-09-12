#!/usr/bin/env python3
"""Phase 2 of the BBB NJ sweep: company -> website -> careers page -> ATS board.

bbb.org profile pages (where the website link lives) sit behind a Cloudflare
challenge, so the website comes from a Brave HTML search on the company name
+ city; the homepage's careers link is followed and both pages are scanned
for the ATS families the applier can submit to (greenhouse, lever, ashby,
workday, smartrecruiters — verified live, then appended to scout_config so
the scout watches them from the next run) or recorded as an unsupported ATS
(icims, jobvite, bamboohr, paylocity, adp, ukg, taleo, successfactors,
jazzhr, workable, rippling, breezy, applicantpro) so the coverage is honest.

Run: uv run python bbb_nj_resolve.py [--max N] [--sector S] [--redo]
State lives in nj_bbb_employers.json (website, careers_url, ats, board,
resolve_note, resolved_at per row); NJ_BBB_EMPLOYERS.md is rebuilt after.
"""
from __future__ import annotations

import base64
import html as html_mod
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import bbb_nj  # noqa: E402
from discover_boards import board_jobs  # noqa: E402

DATA = HERE / 'nj_bbb_employers.json'
CONFIG = HERE / 'scout_config.json'
UA = bbb_nj.UA
HEADERS = {'User-Agent': UA, 'Accept': 'text/html,application/xhtml+xml',
           'Accept-Language': 'en-US,en;q=0.9'}
DIRECTORY_HOSTS = ('bbb.org', 'linkedin.com', 'facebook.com', 'instagram.com', 'twitter.com', 'x.com',
                   'yelp.com', 'mapquest.com', 'yellowpages.com', 'zoominfo.com', 'indeed.com',
                   'glassdoor.com', 'healthgrades.com', 'dnb.com', 'manta.com', 'crunchbase.com',
                   'bloomberg.com', 'wikipedia.org', 'buzzfile.com', 'chamberofcommerce.com',
                   'opencorporates.com', 'bizapedia.com', 'nj.gov', 'njcourts', 'google.com',
                   'youtube.com', 'apple.com', 'angi.com', 'houzz.com', 'zocdoc.com', 'vitals.com',
                   'webmd.com', 'npiprofile', 'npidb', 'hipaaspace', 'doximity', 'sharecare',
                   'findatopdoc', 'us-business.info', 'cortera', 'superpages', 'citysearch',
                   'birdeye', 'trustpilot', 'sitejabber', 'nextdoor', 'alignable', 'thumbtack',
                   'homeadvisor', 'bark.com', 'porch.com', 'medicare.gov', 'cms.gov', 'sec.gov',
                   'fdic.gov', 'ncua.gov', 'finra.org', 'brokercheck', 'reddit.com', 'tiktok.com',
                   'americanbanker.com', 'bizjournals.com', 'prnewswire.com', 'businesswire.com', 'nj.com',
                   'patch.com', 'njbiz.com', 'roi-nj.com', 'globenewswire.com', 'yahoo.com', 'marketwatch.com',
                   'reuters.com', 'forbes.com', 'inc.com', 'pitchbook.com', 'owler.com', 'rocketreach',
                   'signalhire', 'apollo.io', 'kompass', 'thomasnet', 'bbbprograms', 'insurancejournal',
                   'annualreports.com', 'sec.report', 'lastmilesearch', 'craft.co', 'cbinsights', 'tracxn')
CAREER_WORDS = re.compile(r'career|jobs?\b|join[-_ ]?(our|the)?[-_ ]?team|employment|work[-_ ]?(with|for)[-_ ]?us|opportunit|hiring|we.re hiring|open positions', re.I)

# supported families -> (regex over html+urls, how to build the board record)
ATS_PATTERNS = {
    'greenhouse': re.compile(r'(?:boards|job-boards|boards-api)\.greenhouse\.io/(?:v1/boards/)?(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)'),
    'lever': re.compile(r'jobs\.lever\.co/([A-Za-z0-9_-]+)'),
    'ashby': re.compile(r'jobs\.ashbyhq\.com/([A-Za-z0-9_-]+)'),
    'workday': re.compile(r'https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)'),
    'smartrecruiters': re.compile(r'(?:careers|jobs)\.smartrecruiters\.com/([A-Za-z0-9_-]+)'),
}
UNSUPPORTED = {
    'icims': re.compile(r'([a-z0-9-]+)\.icims\.com'), 'jobvite': re.compile(r'jobs\.jobvite\.com/([a-z0-9-]+)'),
    'bamboohr': re.compile(r'([a-z0-9-]+)\.bamboohr\.com/(?:jobs|careers)'), 'paylocity': re.compile(r'recruiting\.paylocity\.com'),
    'adp': re.compile(r'workforcenow\.adp\.com|recruiting\.adp\.com'), 'paycom': re.compile(r'paycomonline\.net'),
    'ukg': re.compile(r'ultipro\.com|ukg\.com/careers|recruiting\.ultipro'), 'taleo': re.compile(r'taleo\.net'),
    'successfactors': re.compile(r'successfactors\.com|jobs\.sap\.com'), 'jazzhr': re.compile(r'applytojob\.com'),
    'workable': re.compile(r'apply\.workable\.com/([a-z0-9-]+)'), 'rippling': re.compile(r'ats\.rippling\.com/([a-z0-9-]+)'),
    'breezy': re.compile(r'([a-z0-9-]+)\.breezy\.hr'), 'applicantpro': re.compile(r'([a-z0-9-]+)\.applicantpro\.com'),
    'phenom': re.compile(r'phenompeople|phenom\.com'), 'oracle_hcm': re.compile(r'oraclecloud\.com/hcmUI'),
    'dayforce': re.compile(r'dayforcehcm\.com'), 'indeed': re.compile(r'indeed\.com/cmp/[a-z0-9-]+/jobs'),
    'ziprecruiter': re.compile(r'ziprecruiter\.com/c/'), 'hireology': re.compile(r'hireology\.com'),
    'clearcompany': re.compile(r'clearcompany\.com'), 'jobscore': re.compile(r'jobscore\.com'),
    'recruitee': re.compile(r'\.recruitee\.com'), 'teamtailor': re.compile(r'\.teamtailor\.com'),
    'pinpoint': re.compile(r'pinpointhq\.com'), 'gem': re.compile(r'jobs\.gem\.com'),
}


GENERIC_WORDS = {'inc', 'llc', 'corp', 'corporation', 'company', 'co', 'the', 'of', 'and', 'group', 'llp',
                 'pc', 'pa', 'ltd', 'services', 'service', 'solutions', 'associates', 'partners', 'center',
                 'centers', 'health', 'medical', 'financial', 'insurance', 'bank', 'software', 'technologies',
                 'technology', 'systems', 'consulting', 'management', 'new', 'jersey', 'nj', 'america',
                 'american', 'national', 'international', 'global', 'united', 'north', 'south', 'east', 'west'}


def name_words(name: str) -> list[str]:
    return [w for w in re.findall(r'[a-z0-9]+', name.lower()) if len(w) >= 3 and w not in GENERIC_WORDS]


def looks_like_theirs(name: str, html: str, host: str) -> bool:
    """the page (or its host) carries a distinctive word of the company name."""
    words = name_words(name)
    if not words:
        return False
    head = re.sub(r'<[^>]+>', ' ', html[:20000]).lower()
    if re.search(r'domain (is )?for sale|buy this domain|parked|godaddy|hugedomains|sedo', head):
        return False
    return any(w in host for w in words) or any(w in head for w in words)


def domain_guesses(name: str) -> list[str]:
    base = re.sub(r'[^a-z0-9 ]', ' ', name.lower())
    words = [w for w in base.split() if w not in {'inc', 'llc', 'corp', 'corporation', 'co', 'the', 'ltd', 'llp', 'pc', 'pa'}]
    if not words:
        return []
    joined = ''.join(words)
    out = [joined, '-'.join(words)]
    if len(words) > 2:
        out.append(''.join(words[:2]))
    if len(words) > 1:
        out.append(''.join(w[0] for w in words))
    guesses = []
    for stem in out:
        if 3 <= len(stem) <= 40:
            for tld in ('.com', '.net', '.org'):
                guesses.append(stem + tld)
    return guesses[:9]


def search_first_site(name: str, query: str, client: httpx.Client) -> str | None:
    """Bing HTML first (no throttling seen), Brave second (429s in bursts; backoff)."""
    cands: list[str] = []
    try:
        r = client.get('https://www.bing.com/search', params={'q': query, 'setlang': 'en'}, timeout=25)
        if r.status_code == 200:
            for block in re.findall(r'class="b_algo"(.*?)</li>', r.text, re.S):
                m = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"', block, re.S)
                if m:
                    cands.append(bing_unwrap(html_mod.unescape(m.group(1))))
    except httpx.HTTPError:
        pass
    if not cands:
        for attempt in range(3):
            try:
                r = client.get('https://search.brave.com/search', params={'q': query, 'source': 'web'}, timeout=25)
            except httpx.HTTPError:
                break
            if r.status_code == 200:
                cands = re.findall(r'href="(https?://[^"]+)"', r.text)
                break
            time.sleep(6 * (attempt + 1))
    words = name_words(name)
    sites: list[str] = []
    for href in cands:
        host = urlparse(href).netloc.lower()
        if not host or any(d in host for d in DIRECTORY_HOSTS) or 'brave.com' in host or 'bing.com' in host or 'microsoft.com' in host:
            continue
        site = f'{urlparse(href).scheme}://{host}/'
        if any(w in host for w in words):
            return site
        if site not in sites:
            sites.append(site)
    # no name word in any host: the page itself has to say the company's name
    for site in sites[:3]:
        final, html = get(client, site, timeout=10)
        if html and looks_like_theirs(name, html, urlparse(final).netloc.lower()):
            return f'{urlparse(final).scheme}://{urlparse(final).netloc}/'
    return None


def bing_unwrap(href: str) -> str:
    """bing.com/ck/a?...&u=a1<base64url of the real url>&... -> the real url."""
    if 'bing.com/ck/' not in href:
        return href
    m = re.search(r'[?&]u=a1([A-Za-z0-9_-]+)', href)
    if not m:
        return href
    b = m.group(1)
    try:
        return base64.urlsafe_b64decode(b + '=' * (-len(b) % 4)).decode('utf-8', 'replace')
    except Exception:
        return href


def find_website(name: str, city: str, client: httpx.Client) -> str | None:
    for d in domain_guesses(name):
        for url in (f'https://www.{d}/', f'https://{d}/'):
            final, html = get(client, url, timeout=10)
            if html and looks_like_theirs(name, html, urlparse(final).netloc.lower()):
                return f'{urlparse(final).scheme}://{urlparse(final).netloc}/'
            if html:
                break
    site = search_first_site(name, f'{name} {city} NJ', client)
    time.sleep(1.0)
    return site


def get(client: httpx.Client, url: str, timeout: int = 20) -> tuple[str, str]:
    """(final_url, html) or ('', '') — one attempt, short timeout, never raises."""
    try:
        r = client.get(url, timeout=timeout)
        if r.status_code >= 400:
            return '', ''
        return str(r.url), r.text[:600000]
    except Exception:
        return '', ''


def careers_links(base: str, html: str) -> list[str]:
    out = []
    for m in re.finditer(r'<a\b[^>]*href="([^"#]+)"[^>]*>(.*?)</a>', html, re.I | re.S):
        href, text = m.group(1), re.sub(r'<[^>]+>', ' ', m.group(2))
        if CAREER_WORDS.search(href) or CAREER_WORDS.search(text):
            out.append(urljoin(base, href))
    seen, uniq = set(), []
    for u in out:
        if u not in seen and not u.lower().startswith(('mailto:', 'tel:', 'javascript:')):
            seen.add(u)
            uniq.append(u)
    return uniq[:4]


def detect_ats(texts: list[str]) -> tuple[str, str, dict] | None:
    blob = '\n'.join(texts)
    for fam, rx in ATS_PATTERNS.items():
        m = rx.search(blob)
        if not m:
            continue
        if fam == 'workday':
            tenant, wd, site = m.groups()
            return fam, f'{tenant}/{site}', {'tenant': tenant, 'wd': wd, 'site': site}
        tok = m.group(1)
        if tok.lower() in ('embed', 'v1', 'boards', 'js', 'css', 'static'):
            continue
        return fam, tok, {}
    for fam, rx in UNSUPPORTED.items():
        if rx.search(blob):
            return f'unsupported:{fam}', '', {}
    return None


def workday_live(client: httpx.Client, spec: dict) -> bool:
    url = f"https://{spec['tenant']}.{spec['wd']}.myworkdayjobs.com/wday/cxs/{spec['tenant']}/{spec['site']}/jobs"
    try:
        r = client.post(url, json={'limit': 1, 'offset': 0, 'searchText': ''},
                        headers={'Accept': 'application/json', 'Content-Type': 'application/json'}, timeout=20)
        return r.status_code == 200 and 'jobPostings' in r.text
    except Exception:
        return False


def smartrecruiters_live(client: httpx.Client, company: str) -> bool:
    try:
        r = client.get(f'https://api.smartrecruiters.com/v1/companies/{company}/postings', timeout=20)
        return r.status_code == 200
    except Exception:
        return False


def resolve_row(row: dict, client: httpx.Client, cfg: dict) -> str:
    """Fill website/careers_url/ats/board on the row; return a one-line note."""
    name, city = row['name'], row.get('city', '')
    if not row.get('website'):
        site = find_website(name, city, client)
        if not site:
            row['resolve_note'] = 'no_website'
            return 'no website found'
        row['website'] = site
    final, home = get(client, row['website'])
    if not home:
        row['resolve_note'] = 'fetch_failed'
        return f"homepage fetch failed {row['website']}"
    texts, urls = [home, final], [final]
    for link in careers_links(final, home):
        cfinal, chtml = get(client, link)
        urls.append(link)
        if chtml:
            texts += [chtml, cfinal]
            urls.append(cfinal)
            row['careers_url'] = cfinal or link
            break
    hit = detect_ats(texts + urls)
    if not hit:
        row['resolve_note'] = 'careers_page' if row.get('careers_url') else 'no_careers'
        return row['resolve_note']
    fam, board, extra = hit
    row['ats'] = fam
    if fam.startswith('unsupported:'):
        row['resolve_note'] = fam
        return fam
    # live check + append to the scout config (NJ + his sector: watched from now on)
    if fam == 'workday':
        ok = workday_live(client, extra)
        if ok and not any(w.get('tenant') == extra['tenant'] and w.get('site') == extra['site'] for w in cfg.get('workday', [])):
            cfg.setdefault('workday', []).append(extra)
    elif fam == 'smartrecruiters':
        ok = smartrecruiters_live(client, board)
        if ok and board not in cfg.get('smartrecruiters', []):
            cfg.setdefault('smartrecruiters', []).append(board)
    else:
        jobs = board_jobs(client, fam, board)
        ok = jobs is not None
        if ok and board.lower() not in {x.lower() for x in cfg.get(fam, [])}:
            cfg.setdefault(fam, []).append(board)
    row['board'] = board
    row['resolve_note'] = 'board' if ok else 'board_dead'
    return f"{fam}/{board} {'live' if ok else 'DEAD'}"


def main() -> int:
    max_n = 150
    sector = None
    redo = '--redo' in sys.argv
    if '--max' in sys.argv:
        max_n = int(sys.argv[sys.argv.index('--max') + 1])
    if '--sector' in sys.argv:
        sector = sys.argv[sys.argv.index('--sector') + 1]
    rows = json.loads(DATA.read_text())
    cfg = json.loads(CONFIG.read_text())
    todo = [r for r in rows if r.get('accredited') and r.get('sector') in ('healthcare', 'finance', 'tech')
            and (redo or not r.get('resolved_at')) and (not sector or r['sector'] == sector)]
    print(f"\n{'=' * 60}\n  SCIPIO BBB NJ RESOLVE — {len(todo)} to do, {max_n} this run\n{'=' * 60}", flush=True)
    added = {k: len(cfg.get(k, [])) for k in ('greenhouse', 'lever', 'ashby', 'workday', 'smartrecruiters')}
    with httpx.Client(headers=HEADERS, follow_redirects=True) as client:
        for i, row in enumerate(todo[:max_n], 1):
            note = resolve_row(row, client, cfg)
            row['resolved_at'] = time.strftime('%Y-%m-%d')
            print(f"  [{i}/{min(max_n, len(todo))}] {row['name']} ({row['sector']}): {note}", flush=True)
            if i % 10 == 0:
                DATA.write_text(json.dumps(rows, indent=1, ensure_ascii=False) + '\n')
                CONFIG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + '\n')
    DATA.write_text(json.dumps(rows, indent=1, ensure_ascii=False) + '\n')
    new = {k: len(cfg.get(k, [])) - v for k, v in added.items()}
    if any(new.values()):
        cfg['nj_bbb_boards'] = f"{time.strftime('%Y-%m-%d')}: +{sum(new.values())} boards from BBB NJ sweep (bbb_nj_resolve.py)"
    CONFIG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + '\n')
    bbb_nj.write_md(rows)
    done = [r for r in rows if r.get('resolved_at')]
    notes = {}
    for r in done:
        notes[r.get('resolve_note')] = notes.get(r.get('resolve_note'), 0) + 1
    print(f"  [*] resolved {len(done)}/{len([r for r in rows if r.get('accredited') and r.get('sector')])}: " +
          ', '.join(f'{k} {v}' for k, v in sorted(notes.items(), key=lambda kv: -kv[1])))
    print('  [*] boards added this run: ' + ', '.join(f'{k} +{v}' for k, v in new.items() if v))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
