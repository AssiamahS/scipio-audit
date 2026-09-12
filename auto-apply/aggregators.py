"""Aggregator supply + board discovery — the on-deck queue must never be empty.

9/9 measurement: 114 configured boards yield ~7,000 postings a run, 8 pass
title/location and are new, 1 reaches the wishlist. The boards are the
bottleneck, not the filters. Four keyless remote-job feeds (Himalayas ~100k
listings, Remotive, Jobicy, Remote OK) each carry an outbound apply link;
this module

  1. queries them with the scout's title keywords,
  2. resolves every hit to its real ATS apply URL (greenhouse / lever /
     ashby / workday / smartrecruiters / ...) by reading the aggregator page,
  3. returns them as scout candidates, and
  4. adds every greenhouse/lever/ashby board it reveals to scout_config.json
     after an API check — supply compounds on its own.

Only ATS URLs the applier already understands are returned; everything else
(company career pages, Indeed/LinkedIn redirects) is dropped.
"""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

import httpx

HERE = Path(__file__).parent
CONFIG_PATH = HERE / 'scout_config.json'
UA = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/128 Safari/537.36',
      'Accept': 'text/html,application/json;q=0.9,*/*;q=0.8'}

ATS_HOSTS = ('greenhouse.io', 'lever.co', 'ashbyhq.com', 'myworkdayjobs.com', 'smartrecruiters.com',
             'icims.com', 'jobvite.com', 'bamboohr.com', 'workable.com', 'rippling.com', 'breezy.hr')

_BOARD_RX = {
    'greenhouse': re.compile(r'(?:boards|job-boards)\.greenhouse\.io/([a-z0-9_-]+)/jobs/', re.I),
    'lever': re.compile(r'jobs\.lever\.co/([a-z0-9_-]+)/[0-9a-f-]{20,}', re.I),
    'ashby': re.compile(r'jobs\.ashbyhq\.com/([a-z0-9_.-]+)/[0-9a-f-]{20,}', re.I),
}
_VERIFY = {
    'greenhouse': 'https://boards-api.greenhouse.io/v1/boards/{t}/jobs',
    'lever': 'https://api.lever.co/v0/postings/{t}?mode=json',
    'ashby': 'https://api.ashbyhq.com/posting-api/job-board/{t}',
}


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            d = dict(attrs)
            if d.get('href'):
                self.hrefs.append(d['href'])


def _strip(html: str) -> str:
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', html or '')).strip()


def _get(client: httpx.Client, url: str, **kw):
    try:
        r = client.get(url, headers=UA, timeout=25, follow_redirects=True, **kw)
        return r if r.status_code == 200 else None
    except Exception:
        return None


# ---------------------------------------------------------------- feeds ---

def _himalayas(client, q: str, max_pages: int) -> list[dict]:
    out, cursor = [], None
    for _ in range(max_pages):
        params = {'limit': 100, 'search': q}
        if cursor:
            params['cursor'] = cursor
        r = _get(client, 'https://himalayas.app/jobs/api', params=params)
        if not r:
            break
        body = r.json()
        for j in body.get('jobs', []):
            locs = ', '.join(j.get('locationRestrictions') or []) or 'Remote'
            out.append({'company': j.get('companyName', ''), 'role': j.get('title', ''),
                        'page': j.get('applicationLink') or j.get('guid', ''),
                        'location': f'Remote ({locs})',
                        'description': _strip(j.get('description', ''))[:12000],
                        'posted': _epoch(j.get('pubDate')), 'source': 'himalayas'})
        cursor = body.get('nextCursor')
        if not cursor:
            break
    return out


def _remotive(client, q: str, max_pages: int) -> list[dict]:
    r = _get(client, 'https://remotive.com/api/remote-jobs', params={'search': q, 'limit': 100})
    if not r:
        return []
    return [{'company': j.get('company_name', ''), 'role': j.get('title', ''), 'page': j.get('url', ''),
             'location': f"Remote ({j.get('candidate_required_location') or 'Worldwide'})",
             'description': _strip(j.get('description', ''))[:12000],
             'posted': (j.get('publication_date') or '')[:10], 'source': 'remotive'}
            for j in r.json().get('jobs', [])]


def _jobicy(client, q: str, max_pages: int) -> list[dict]:
    r = _get(client, 'https://jobicy.com/api/v2/remote-jobs', params={'count': 100, 'tag': q})
    if not r:
        return []
    return [{'company': j.get('companyName', ''), 'role': j.get('jobTitle', ''), 'page': j.get('url', ''),
             'location': f"Remote ({j.get('jobGeo') or 'Anywhere'})",
             'description': _strip(j.get('jobDescription', ''))[:12000],
             'posted': (j.get('pubDate') or '')[:10], 'source': 'jobicy'}
            for j in r.json().get('jobs', [])]


_ROK_CACHE: list | None = None


def _remoteok(client, q: str, max_pages: int) -> list[dict]:
    global _ROK_CACHE
    if _ROK_CACHE is None:
        r = _get(client, 'https://remoteok.com/api')
        _ROK_CACHE = [j for j in (r.json() if r else []) if isinstance(j, dict) and j.get('position')]
    ql = q.lower()
    return [{'company': j.get('company', ''), 'role': j.get('position', ''), 'page': j.get('url', ''),
             'location': f"Remote ({j.get('location') or 'Worldwide'})",
             'description': _strip(j.get('description', ''))[:12000],
             'posted': (j.get('date') or '')[:10], 'source': 'remoteok'}
            for j in _ROK_CACHE if ql in (j.get('position') or '').lower()]


FEEDS = {'himalayas': _himalayas, 'remotive': _remotive, 'jobicy': _jobicy, 'remoteok': _remoteok}


def _epoch(v) -> str:
    try:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(int(v), tz=timezone.utc).strftime('%Y-%m-%d')
    except (TypeError, ValueError):
        return ''


# ------------------------------------------------------------- resolver ---

def resolve_apply_url(client, page_url: str) -> str | None:
    """Follow an aggregator listing to the ATS apply page it links to."""
    if not page_url:
        return None
    if any(h in page_url for h in ATS_HOSTS):
        return page_url
    r = _get(client, page_url)
    if not r:
        return None
    p = _Links()
    try:
        p.feed(r.text)
    except Exception:
        return None
    for href in p.hrefs:
        if any(h in href for h in ATS_HOSTS):
            return href.split('#')[0]
    # jobicy/remotive route "apply" through their own redirect endpoints
    for href in p.hrefs:
        if re.search(r'/apply|/out/|redirect|/go/', href) and href.startswith('http'):
            rr = _get(client, href)
            if rr and any(h in str(rr.url) for h in ATS_HOSTS):
                return str(rr.url).split('#')[0]
    return None


# ------------------------------------------------------------ discovery ---

def discover_boards(urls: list[str], cfg: dict, client=None) -> dict[str, list[str]]:
    """Add every new greenhouse/lever/ashby board token seen in `urls` to
    cfg (in place) after a live API check. Returns what was added."""
    client = client or httpx.Client()
    added: dict[str, list[str]] = {}
    for url in urls:
        for fam, rx in _BOARD_RX.items():
            m = rx.search(url or '')
            if not m:
                continue
            token = m.group(1)
            have = {t.lower() for t in cfg.get(fam, [])}
            if token.lower() in have or token.lower() in {t.lower() for t in added.get(fam, [])}:
                continue
            r = _get(client, _VERIFY[fam].format(t=token))
            if not r:
                continue
            cfg.setdefault(fam, []).append(token)
            added.setdefault(fam, []).append(token)
    return added


# ---------------------------------------------------------------- entry ---

def fetch_aggregators(cfg: dict, known_urls: set, title_ok, location_ok) -> list[dict]:
    agg = cfg.get('aggregators') or {}
    if not agg.get('enabled'):
        return []
    queries = agg.get('queries') or cfg.get('title_keywords', [])[:8]
    max_pages = int(agg.get('max_pages', 1))
    seen_pages, hits = set(), []
    with httpx.Client() as client:
        for name in agg.get('sources') or list(FEEDS):
            fn = FEEDS.get(name)
            if not fn:
                continue
            got = 0
            for q in queries:
                try:
                    rows = fn(client, q, max_pages)
                except Exception as e:
                    print(f'  [!] {name}/{q}: {e}')
                    continue
                for c in rows:
                    if c['page'] in seen_pages or not title_ok(c['role'], cfg) or not location_ok(c['location'], cfg):
                        continue
                    seen_pages.add(c['page'])
                    hits.append(c)
                    got += 1
            print(f'  [*] {name}: {got} title/location matches')
        out = []
        for c in hits:
            url = resolve_apply_url(client, c['page'])
            if not url or url in known_urls:
                continue
            c = {**c, 'url': url, 'source': f"{c['source']}->{_family(url)}"}
            c.pop('page', None)
            out.append(c)
        print(f'  [*] aggregators: {len(hits)} matches, {len(out)} resolved to a new ATS apply URL')
        if cfg.get('auto_discover_boards'):
            added = discover_boards([c['url'] for c in out], cfg, client)
            if added:
                CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + '\n')
                print('  [+] boards discovered: ' + ', '.join(f"{k} +{len(v)} ({', '.join(v[:5])})" for k, v in added.items()))
    return out


def _family(url: str) -> str:
    for h in ATS_HOSTS:
        if h in url:
            return h.split('.')[0]
    return 'other'


if __name__ == '__main__':
    import sys
    sys.path.insert(0, str(HERE))
    from scout import title_ok, location_ok
    cfg = json.loads(CONFIG_PATH.read_text())
    rows = fetch_aggregators(cfg, set(), title_ok, location_ok)
    for c in rows[:40]:
        print(f"  {c['source']:<24} {c['company'][:24]:<24} {c['role'][:50]:<50} {c['url'][:70]}")
