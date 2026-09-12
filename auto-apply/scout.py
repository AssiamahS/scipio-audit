#!/usr/bin/env python3
"""
Scipio Scout — keeps the wishlist fed with real, fresh postings.

Pulls jobs from Greenhouse/Lever public board APIs (real apply-form URLs +
full job descriptions), filters by title/location, scores against the
profile, and appends the best to jobs.json as wishlist entries.

Usage:
  uv run python scout.py [--dry-run] [--max N]

Config: scout_config.json (board tokens, keywords, thresholds).
"""
import json
import re
import sys
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from ai_engine import score_job_match
from ats_lint import keyword_score
from fit_check import assess


def fallback_score(profile: dict, job: dict) -> dict:
    """Deterministic score when no AI is reachable: how much of the JD's
    skill/tool vocabulary the profile actually covers (0-10)."""
    hay = ' '.join([
        profile.get('current_title', ''), profile.get('summary', ''),
        ' '.join(profile.get('skills', [])),
        json.dumps(profile.get('experience', '')),
    ])
    kw = keyword_score(hay, job.get('description', ''))
    score = max(1, min(10, round(kw['score'] / 10)))
    reason = (f"keyword coverage {kw['score']}% "
              f"({len(kw['present'])}/{len(kw['keywords'])} JD terms)")

    # His genuine edge is the overlap almost nobody else has: hospital
    # infrastructure ops + Epic/EHR + SailPoint/Okta IAM + regulated pharma.
    # Pure-infra depth roles he'd lose; healthcare identity and clinical
    # systems roles he should be near the top of the pile.
    niche = [k.strip() for k in (CONFIG or {}).get('niche_keywords', [])]
    text = f"{job.get('role', '')} {job.get('description', '')}".lower()
    hits = sorted({k for k in niche if k and k in text})
    if hits:
        bump = 2 if len(hits) >= 3 else 1
        score = min(10, score + bump)
        reason += f"; +{bump} healthcare/identity niche ({', '.join(hits[:4])})"

    return {'score': score, 'reason': reason,
            'matching_skills': kw['present'][:6]}

HERE = Path(__file__).parent
CONFIG_PATH = HERE / 'scout_config.json'
PROFILE_PATH = HERE / 'profile.json'
JOBS_PATH = HERE.parent / 'jobs.json'

# loaded once so fallback_score can weight his niche without re-reading disk
CONFIG = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def strip_html(html: str) -> str:
    p = _TextExtractor()
    p.feed(html or '')
    text = ' '.join(p.parts)
    return re.sub(r'\s+', ' ', text).strip()


def fetch_greenhouse(token: str) -> list[dict]:
    url = f'https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true'
    r = httpx.get(url, timeout=30, follow_redirects=True)
    if r.status_code != 200:
        print(f'  [!] greenhouse/{token}: HTTP {r.status_code}')
        return []
    out = []
    for j in r.json().get('jobs', []):
        out.append({
            'company': token,
            'role': j.get('title', ''),
            'url': j.get('absolute_url', ''),
            'location': (j.get('location') or {}).get('name', ''),
            'description': strip_html(j.get('content', '')),
            'posted': (j.get('updated_at') or '')[:10],
            'source': f'greenhouse/{token}',
        })
    return out


def fetch_lever(token: str) -> list[dict]:
    url = f'https://api.lever.co/v0/postings/{token}?mode=json'
    r = httpx.get(url, timeout=30, follow_redirects=True)
    if r.status_code != 200:
        print(f'  [!] lever/{token}: HTTP {r.status_code}')
        return []
    out = []
    for j in r.json():
        loc = (j.get('categories') or {}).get('location', '') or ''
        out.append({
            'company': token,
            'role': j.get('text', ''),
            'url': j.get('hostedUrl', ''),
            'location': loc,
            'description': (j.get('descriptionPlain') or '')[:12000],
            'posted': '',
            'source': f'lever/{token}',
        })
    return out


def fetch_ashby(token: str) -> list[dict]:
    url = f'https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true'
    r = httpx.get(url, timeout=30, follow_redirects=True)
    if r.status_code != 200:
        print(f'  [!] ashby/{token}: HTTP {r.status_code}')
        return []
    out = []
    for j in r.json().get('jobs', []):
        if not j.get('isListed', True):
            continue
        out.append({
            'company': token,
            'role': j.get('title', ''),
            'url': j.get('jobUrl', '') or j.get('applyUrl', ''),
            'location': j.get('location', '') or '',
            'description': strip_html(j.get('descriptionHtml', '')) or (j.get('descriptionPlain') or '')[:12000],
            'posted': (j.get('publishedAt') or '')[:10],
            'source': f'ashby/{token}',
        })
    return out


WD_HEADERS = {'Content-Type': 'application/json', 'Accept': 'application/json',
              'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/128 Safari/537.36'}


def workday_posted(info: dict, posting: dict) -> str:
    """Workday's detail call carries startDate (YYYY-MM-DD); the list call only
    says 'Posted 2 Days Ago'. Prefer the date, fall back to the phrase."""
    if info.get('startDate'):
        return str(info['startDate'])[:10]
    m = re.search(r'posted\s+(\d+)\+?\s+days?\s+ago', (posting.get('postedOn') or '').lower())
    if m:
        from datetime import timedelta
        return (datetime.now() - timedelta(days=int(m.group(1)))).strftime('%Y-%m-%d')
    if 'today' in (posting.get('postedOn') or '').lower() or 'yesterday' in (posting.get('postedOn') or '').lower():
        return datetime.now().strftime('%Y-%m-%d')
    return ''


def commutable(loc: str) -> bool:
    """Big-employer boards spell every US city as '<City>, <State>, United
    States of America', which sails through location_ok on 'united states'.
    He is in <city>, NJ: remote, work-at-home, or NJ/NYC only."""
    l = (loc or '').lower()
    if 'remote' in l or 'work at home' in l or 'work-at-home' in l or 'telecommute' in l or 'virtual' in l:
        return True
    return any(re.search(r'\b' + m + r'\b', l) for m in (
        'new jersey', 'nj', 'new york', 'ny', 'nyc', 'manhattan', 'brooklyn', 'jersey city',
        'newark', 'hoboken', 'princeton', 'edison', 'red bank', 'holmdel', 'freehold', 'neptune',
        'hackensack', 'nationwide', 'united states - remote', 'us - remote'))


def fetch_workday(spec: dict, cfg: dict | None = None, known_urls: set | None = None) -> list[dict]:
    """Workday CXS API: POST .../wday/cxs/<tenant>/<site>/jobs with a search
    term. Public, no auth (verified 9/5 on humana, iqvia, cvshealth, elevance,
    labcorp, mckesson, cardinalhealth, novartis, pfizer). One search per title
    keyword, then one detail GET per NEW posting that passes title/location
    (detail carries the JD + startDate). The applier never submits to Workday;
    these become hand-apply packs with a tailored resume."""
    cfg = cfg or CONFIG
    known_urls = known_urls or set()
    tenant, wd, site = spec['tenant'], spec.get('wd', 'wd1'), spec['site']
    base = f'https://{tenant}.{wd}.myworkdayjobs.com'
    api = f'{base}/wday/cxs/{tenant}/{site}'
    postings, seen = [], set()
    with httpx.Client(timeout=25, headers=WD_HEADERS, follow_redirects=True) as client:
        # CXS caps a page at 20 (limit 50 = HTTP 400) and ranks senior titles
        # first, so the mid-level reqs live on pages 2-3. Page by offset.
        pages = int(cfg.get('workday_pages', 3))
        for kw in cfg.get('workday_search_terms') or cfg.get('title_keywords', [])[:6]:
            for page in range(pages):
                try:
                    r = client.post(f'{api}/jobs', json={'appliedFacets': {}, 'limit': 20,
                                                         'offset': page * 20, 'searchText': kw})
                except Exception as e:
                    print(f'  [!] workday/{tenant} "{kw}": {e}')
                    break
                if r.status_code != 200:
                    print(f'  [!] workday/{tenant} "{kw}": HTTP {r.status_code}')
                    break
                batch = r.json().get('jobPostings', [])
                for p in batch:
                    path = p.get('externalPath')
                    if not path or path in seen:
                        continue
                    seen.add(path)
                    postings.append(p)
                if len(batch) < 20:
                    break
        out, details = [], 0
        cap = int(cfg.get('workday_detail_cap', 12))
        for p in postings:
            role = p.get('title', '')
            loc = p.get('locationsText', '') or ''
            url = f"{base}/{site}{p['externalPath']}"
            if url in known_urls or not title_ok(role, cfg) or not location_ok(loc, cfg) or not commutable(loc):
                continue
            if details >= cap:
                break
            details += 1
            try:
                d = client.get(f"{api}{p['externalPath']}")
                info = d.json().get('jobPostingInfo', {}) if d.status_code == 200 else {}
            except Exception:
                info = {}
            out.append({
                'company': tenant,
                'role': info.get('title') or role,
                'url': info.get('externalUrl') or url,
                'location': info.get('location') or loc,
                'description': strip_html(info.get('jobDescription', ''))[:12000],
                'posted': workday_posted(info, p),
                'source': f'workday/{tenant}',
            })
    return out


def fetch_smartrecruiters(token: str, cfg: dict | None = None, known_urls: set | None = None) -> list[dict]:
    """SmartRecruiters public postings API (verified 9/5: PriviaHealth). List
    is filtered on title/location/country=us before the per-posting detail
    GET that carries the job ad text."""
    cfg = cfg or CONFIG
    known_urls = known_urls or set()
    out, details = [], 0
    with httpx.Client(timeout=25, headers={'Accept': 'application/json', 'User-Agent': WD_HEADERS['User-Agent']}) as client:
        r = client.get(f'https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=100')
        if r.status_code != 200:
            print(f'  [!] smartrecruiters/{token}: HTTP {r.status_code}')
            return []
        for p in r.json().get('content', []):
            loc = p.get('location') or {}
            if (loc.get('country') or '').lower() not in ('us', 'usa', 'united states'):
                continue
            loc_s = ', '.join(x for x in (loc.get('city'), loc.get('region')) if x)
            if loc.get('remote'):
                loc_s = f'Remote, {loc_s or "United States"}'
            role = p.get('name', '')
            url = f"https://jobs.smartrecruiters.com/{token}/{p.get('id')}"
            if url in known_urls or not title_ok(role, cfg) or not location_ok(loc_s, cfg) or not commutable(loc_s):
                continue
            if details >= int(cfg.get('smartrecruiters_detail_cap', 10)):
                break
            details += 1
            desc = ''
            try:
                d = client.get(p.get('ref') or f'https://api.smartrecruiters.com/v1/companies/{token}/postings/{p.get("id")}')
                if d.status_code == 200:
                    sections = (d.json().get('jobAd') or {}).get('sections') or {}
                    desc = ' '.join(strip_html((sections.get(k) or {}).get('text', ''))
                                    for k in ('companyDescription', 'jobDescription', 'qualifications', 'additionalInformation'))
            except Exception:
                pass
            out.append({
                'company': token.lower(),
                'role': role,
                'url': url,
                'location': loc_s,
                'description': desc[:12000],
                'posted': (p.get('releasedDate') or '')[:10],
                'source': f'smartrecruiters/{token}',
            })
    return out


_SAL_TOKEN = re.compile(r'\$\s?(\d{1,3}(?:,\d{3})+|\d{1,3}(?:\.\d{1,2})?\s?[kK]\b|\d{2,3}(?:\.\d{1,2})?)(?![\d,])')
_HOURLY = re.compile(r'^\s*(?:-|–|to)?\s*\$?\s?[\d.,]*\s?[kK]?\s*(?:/|per)\s*(?:hr|hour)|^\s*(?:/|per)\s*(?:hr|hour)|hourly', re.I)


def salary_min(text: str) -> int | None:
    """Lowest annual figure a JD states ($85,000, $85k, $45/hr x 2080)."""
    best = None
    text = text or ''
    for m in _SAL_TOKEN.finditer(text):
        raw = m.group(1).lower().replace(',', '').replace(' ', '')
        try:
            v = float(raw[:-1]) * 1000 if raw.endswith('k') else float(raw)
        except ValueError:
            continue
        tail = text[m.end():m.end() + 40]
        if v < 300 and _HOURLY.search(tail):
            v *= 2080
        if v < 20000 or v > 600000:
            continue
        best = int(v) if best is None else min(best, int(v))
    return best


def tier_of(loc: str, sal: int | None, cfg: dict) -> int:
    """1 = NJ/NYC/remote and pays >= tier1_min_salary (or unstated);
    2 = farther away or under the floor. Tier 2 still gets applied to, after
    every tier 1 (Sylvester, 9/9)."""
    l = (loc or '').lower()
    near = any(x in l for x in cfg.get('tier1_locations', cfg.get('locations', ['remote'])))
    floor = int(cfg.get('tier1_min_salary', 85000))
    return 1 if near and (sal is None or sal >= floor) else 2


def title_ok(role: str, cfg: dict) -> bool:
    r = role.lower()
    # 9/9: titles he has literally held ("Senior Project Coordinator") or
    # their peers beat the generic senior/level excludes
    if any(re.search(r'\b' + re.escape(x) + r'\b', r) for x in cfg.get('title_allow_overrides', [])):
        return True
    if any(x in r for x in cfg.get('exclude_keywords', [])):
        return False
    return any(x in r for x in cfg.get('title_keywords', []))


def location_ok(loc: str, cfg: dict) -> bool:
    # excludes + allow-list ALWAYS apply — the old remote_only:false path
    # returned True for everything, which would have let "Remote, Slovakia"
    # straight through. Widening to NJ/NYC hybrid/onsite (user, 8/23) is
    # done by adding metro tokens to `locations`, never by skipping checks.
    l = loc.lower()
    # "Remote, Slovakia" must not pass a US-only filter on the word "remote"
    if any(x in l for x in cfg.get('exclude_locations', [])):
        return False
    # the exclude list is a blacklist and will always be missing a country
    # ("Remote, Belgium" got queued 8/24): a remote posting that names a
    # place after the word must name a US one
    m = re.search(r'(?:remote|hybrid)\s*[,\-–(]\s*(.+)', l)
    if m:
        rest = m.group(1)
        markers = list(cfg.get('us_markers', US_MARKERS)) + [
            x for x in cfg.get('locations', []) if x not in ('remote', 'hybrid')]
        if not any(re.search(r'\b' + re.escape(x.strip(', ')) + r'\b', rest) for x in markers):
            return False
    # 9/11: a bare "Hybrid" / "Onsite" names no place at all — PayPay's Tokyo
    # engineering reqs were listed as "(Hybrid)" and passed on the word alone
    # (3 submits each to Japanese SRE/infra roles). Bare "Remote" stays US by
    # convention; a work mode with no city, state or country does not.
    if not re.sub(r'\b(hybrid|on-?site|in-?office|in-?person)\b|[^a-z]', '', l):
        return False
    return any(x in l for x in cfg.get('locations', ['remote']))


US_MARKERS = ['united states', 'usa', 'u.s', 'us', 'america', 'new jersey', 'nj',
              'new york', 'ny', 'nyc', 'east coast', 'eastern', 'est', 'et']


def main():
    dry_run = '--dry-run' in sys.argv
    cfg = json.loads(CONFIG_PATH.read_text())
    max_new = cfg.get('max_new_per_run', 5)
    if '--max' in sys.argv:
        max_new = int(sys.argv[sys.argv.index('--max') + 1])
    profile = json.loads(PROFILE_PATH.read_text())
    db = json.loads(JOBS_PATH.read_text())
    known_urls = {j.get('url', '') for j in db['jobs']}

    print(f"\n{'=' * 60}\n  SCIPIO SCOUT {'(DRY RUN)' if dry_run else ''}\n{'=' * 60}")

    candidates = []
    for source, fetch in (('greenhouse', fetch_greenhouse), ('lever', fetch_lever), ('ashby', fetch_ashby)):
        for token in cfg.get(source, []):
            # one board resetting the connection must not kill the whole run
            try:
                candidates += fetch(token)
            except Exception as e:
                print(f'  [!] {source}:{token} fetch failed, skipping: {e}')
    # Workday + SmartRecruiters: where the big healthcare employers post
    # (Humana IT PM 2 was the best-fit req of July and lived only there).
    # Pre-filtered inside the fetcher so detail calls stay cheap.
    for spec in cfg.get('workday', []):
        try:
            got = fetch_workday(spec, cfg, known_urls)
            print(f"  [*] workday/{spec['tenant']}: {len(got)} new matching posting(s)")
            candidates += got
        except Exception as e:
            print(f"  [!] workday:{spec.get('tenant')} fetch failed, skipping: {e}")
    for token in cfg.get('smartrecruiters', []):
        try:
            got = fetch_smartrecruiters(token, cfg, known_urls)
            print(f'  [*] smartrecruiters/{token}: {len(got)} new matching posting(s)')
            candidates += got
        except Exception as e:
            print(f'  [!] smartrecruiters:{token} fetch failed, skipping: {e}')
    # 9/9: keyless aggregator feeds resolved to ATS apply URLs, plus any new
    # board they reveal (aggregators.py) — the boards were the bottleneck
    try:
        from aggregators import fetch_aggregators
        candidates += fetch_aggregators(cfg, known_urls, title_ok, location_ok)
    except Exception as e:
        print(f'  [!] aggregators failed, skipping: {e}')
    print(f'  [*] {len(candidates)} postings fetched')

    fresh = [c for c in candidates
             if c['url'] and c['url'] not in known_urls
             and title_ok(c['role'], cfg) and location_ok(c['location'], cfg)]
    print(f'  [*] {len(fresh)} match title/location filters and are new')

    # fit screen (fit_check.py): a JD that REQUIRES a credential he lacks, an
    # 8+ year bar, a senior title, or a posting older than the ghost-job line
    # never reaches the wishlist — those were 35 of the 121 jobs ever logged,
    # and every one of them was a guaranteed silent reject. Freshest first:
    # 52% of recruiters review chronologically and stop at a shortlist.
    max_age = cfg.get('max_posting_age_days', 21)
    screened, skipped = [], []
    for c in fresh:
        rep = assess(c, profile)
        age = rep.get('posting_age_days')
        if age is not None and age > max_age:
            rep['knockouts'].append(f'posted {age}d ago > {max_age}d cap')
            rep['verdict'] = 'skip'
        if rep['verdict'] == 'skip':
            skipped.append((c, rep))
            continue
        c['fit'] = rep
        screened.append(c)
    for c, rep in skipped[:12]:
        print(f"  [-] {c['company']} — {c['role'][:50]}: {rep['knockouts'][0][:90]}")
    if len(skipped) > 12:
        print(f'  [-] ... and {len(skipped) - 12} more screened out')
    print(f'  [*] {len(screened)} pass the fit screen ({len(skipped)} knockouts)')
    # one combined key (NOTES 7/27: a second .sort() silently overrides the
    # first): clean fits first, newest posting first within each group
    screened.sort(key=lambda c: (c['fit']['verdict'] != 'apply', -(int((c.get('posted') or '0000-00-00').replace('-', '') or 0))))
    fresh = screened

    # don't waste scoring slots: drop roles we already track and collapse
    # per-city clones of the same posting before scoring, not after
    known_roles = {(j['company'].lower(), j['role'].lower()) for j in db['jobs']}
    unique, seen_keys = [], set()
    for c in fresh:
        key = (c['company'].lower(), c['role'].lower())
        if key in known_roles or key in seen_keys:
            continue
        seen_keys.add(key)
        unique.append(c)
    print(f'  [*] {len(unique)} are new roles (not yet tracked)')
    fresh = unique

    # score with the free model, best first; cap AI calls
    scored = []
    for c in fresh[:cfg.get('max_scored_per_run', 10)]:
        try:
            s = score_job_match(profile, c)
            # ai_engine's own failure path returns 5/"Could not score" —
            # replace that stub with the deterministic keyword score
            if s.get('reason') == 'Could not score':
                s = fallback_score(profile, c)
            else:
                # free models rate his own lane (mid-level IT ops/analyst)
                # erratically low; when the literal skill-overlap evidence is
                # stronger than the AI's guess, trust the evidence
                d = fallback_score(profile, c)
                if d['score'] > s.get('score', 0):
                    d['reason'] = f"{d['reason']} (ai said {s.get('score')}: {s.get('reason', '')[:80]})"
                    s = d
        except Exception as e:
            print(f'  [!] ai scoring failed for {c["role"]} — keyword fallback: {e}')
            s = fallback_score(profile, c)
        c['score'] = s.get('score', 0)
        c['score_reason'] = s.get('reason', '')
        print(f'  [{c["score"]}/10] {c["company"]} — {c["role"]} ({c["location"]})')
        scored.append(c)

    min_score = cfg.get('min_score', 6)
    # same role posted per-city: keep only the best-scoring copy
    best = {}
    for c in sorted([c for c in scored if c['score'] >= min_score],
                    key=lambda c: -c['score']):
        key = (c['company'].lower(), c['role'].lower())
        if key not in best and key not in known_roles:
            best[key] = c
    picks = list(best.values())[:max_new]

    if not picks:
        print('  [*] Nothing cleared the score bar; jobs.json unchanged')
        return

    now = datetime.now().strftime('%Y-%m-%d')
    now_full = datetime.now().strftime('%Y-%m-%d %H:%M')
    for c in picks:
        job = {
            'id': db['next_id'],
            'company': c['company'],
            'role': c['role'],
            'url': c['url'],
            'location': c.get('location', ''),
            'salary': salary_min(c.get('description', '')) or '',
            'tier': tier_of(c.get('location', ''), salary_min(c.get('description', '')), cfg),
            'notes': f"scout {c['source']} {c['score']}/10: {c['score_reason']}",
            'description': c["description"][:12000],
            # freshness: applications sent within a day or two of a posting
            # going live get answered far more often, so the applier drains
            # newest-first off this field
            'posted_date': c.get('posted', ''),
            'status': 'wishlist',
            'applied_date': '',
            'updated_date': now,
            'history': [{'status': 'wishlist', 'date': now_full}],
        }
        db['next_id'] += 1
        db['jobs'].append(job)
        print(f"  [+] queued #{job['id']}: {c['company']} — {c['role']}")

    if dry_run:
        print('  [~] DRY RUN — not writing jobs.json')
        return
    JOBS_PATH.write_text(json.dumps(db, indent=2))
    print(f'  [+] jobs.json updated ({len(picks)} new wishlist entries)')


if __name__ == '__main__':
    main()
