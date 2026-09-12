#!/usr/bin/env python3
"""BBB New Jersey employer sweep — every BBB-accredited business in NJ in his
three sectors (healthcare, finance, tech), as a list the scout can grow into.

bbb.org's profile pages sit behind a Cloudflare challenge, but its search
pages are plain HTML with an analytics JSON blob (15 results a page, total
count, business_id, name, phone, zip, rating, accredited flag) plus the
profile paths (city + category slug) for the same results. That is enough
for the list; the website/careers resolution is phase 2 (bbb_nj_resolve.py).

Run: uv run python bbb_nj.py [--max-pages N] [--sector healthcare|finance|tech]
Writes: nj_bbb_employers.json (every NJ hit, deduped by business_id) and
        ../NJ_BBB_EMPLOYERS.md (by sector, for reading on the phone).
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import re
import sys
import time
from pathlib import Path

import httpx

HERE = Path(__file__).parent
OUT_JSON = HERE / 'nj_bbb_employers.json'
OUT_MD = HERE.parent / 'NJ_BBB_EMPLOYERS.md'
UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36')
SEARCH = 'https://www.bbb.org/search'
PER_PAGE = 15

# search text per sector: BBB search is relevance-ranked free text, not a
# category filter, so several phrasings per sector are needed and the
# category slug from the profile path decides the sector afterwards
QUERIES = {
    'healthcare': ['hospital', 'health system', 'medical center', 'healthcare', 'health care',
                   'pharmaceutical', 'biotechnology', 'medical device', 'health insurance',
                   'home health care', 'clinical research', 'medical laboratory', 'medical billing',
                   'health information', 'urgent care', 'physical therapy', 'behavioral health',
                   'medical practice', 'nursing home', 'pharmacy'],
    'finance': ['bank', 'credit union', 'financial services', 'financial planning', 'investment',
                'wealth management', 'insurance company', 'insurance agency', 'mortgage lender',
                'accounting', 'payroll services', 'fintech', 'asset management', 'tax services'],
    'tech': ['software', 'information technology', 'it services', 'technology consulting',
             'computer services', 'cybersecurity', 'managed services', 'data services',
             'telecommunications', 'internet services', 'web development', 'computer consultants',
             'software developers', 'it consulting', 'cloud services'],
}
# category slug -> sector (profile path); anything not listed is decided by name/query
SLUG_SECTOR = {
    'healthcare': ('hospital', 'medical', 'health', 'clinic', 'pharma', 'biotech', 'laborator',
                   'physician', 'doctor', 'dental', 'urgent-care', 'physical-therap', 'nursing',
                   'home-care', 'hospice', 'rehab', 'surgery', 'imaging', 'radiolog', 'diagnos',
                   'pharmacy', 'medic', 'clinical', 'behavioral', 'mental-health', 'wellness-center'),
    'finance': ('bank', 'credit-union', 'financ', 'invest', 'insurance', 'mortgage', 'lend', 'loan',
                'account', 'payroll', 'tax', 'wealth', 'broker', 'securit', 'payment', 'credit-'),
    'tech': ('software', 'computer', 'information-technology', 'it-', 'technolog', 'internet',
             'web-', 'data', 'cyber', 'telecom', 'network', 'cloud', 'saas', 'app-develop',
             'managed-service', 'electronics', 'engineering'),
}
NOT_FOR_HIM = ('animal', 'veterinar', 'pet-', 'jewel', 'pawn', 'auto', 'car-', 'roof', 'plumb',
               'hvac', 'landscap', 'restaurant', 'salon', 'spa', 'tattoo', 'church', 'daycare',
               'child-care', 'cemeter', 'funeral', 'massage', 'chiropract', 'acupunct', 'optic',
               'hearing-aid', 'medical-spa', 'weight-loss', 'tanning', 'cannabis', 'cbd', 'vape',
               'fitness', 'gym', 'yoga', 'martial', 'dance', 'music', 'photograph', 'wedding',
               'florist', 'bakery', 'pizza', 'deli', 'catering', 'limo', 'taxi', 'moving',
               'storage', 'cleaning', 'janitorial', 'pest', 'painting', 'flooring', 'window',
               'kitchen', 'bath', 'furniture', 'mattress', 'appliance', 'electrician', 'contractor',
               'construction', 'remodel', 'real-estate', 'apartment', 'property-manag', 'home-builder',
               'lawyer', 'attorney', 'law-firm', 'notary', 'bail', 'debt-relief', 'debt-collect',
               'collections-agenc', 'check-cashing', 'atm', 'coin', 'gold-buyer', 'towing', 'driving',
               'tutoring', 'school', 'college', 'university', 'preschool', 'camp', 'travel', 'hotel',
               'medical-transport', 'ambulance', 'medical-equipment', 'medical-supplies', 'scrubs',
               'medical-alarm', 'hearing', 'eyeglass', 'dentist', 'dental-', 'orthodont', 'podiatr',
               'dermatolog', 'plastic-surg', 'cosmetic', 'laser', 'hair', 'nail', 'barber')

PATH_RE = re.compile(r'us/nj/([a-z0-9-]+)/profile/([a-z0-9-]+)/([a-z0-9-]+)-(\d{4})-(\d+)')
INFO_RE = re.compile(r'"search_info":(\{.*?"used_type_aheads":"[^"]*"\})', re.S)


def fetch(client: httpx.Client, text: str, page: int) -> tuple[dict | None, dict[str, dict]]:
    """search_info JSON + {business_id: {city, category}} from the profile paths."""
    for attempt in range(3):
        try:
            r = client.get(SEARCH, params={'find_country': 'USA', 'find_loc': 'NJ',
                                           'find_text': text, 'page': page}, timeout=30)
            if r.status_code == 200:
                break
            time.sleep(2 + attempt * 3)
        except httpx.HTTPError:
            time.sleep(2 + attempt * 3)
    else:
        return None, {}
    m = INFO_RE.search(r.text)
    info = json.loads(m.group(1)) if m else None
    paths: dict[str, dict] = {}
    for city, cat, _slug, _bbb, bid in PATH_RE.findall(r.text):
        paths.setdefault(bid, {'city': city.replace('-', ' ').title(), 'category': cat})
    return info, paths


def sector_of(category: str, name: str, query_sector: str) -> str | None:
    cat = (category or '').lower()
    if any(x in cat for x in NOT_FOR_HIM):
        return None
    for sec, keys in SLUG_SECTOR.items():
        if any(k in cat for k in keys):
            return sec
    # unknown slug: trust the query's sector only when the name backs it up
    n = name.lower()
    hints = {'healthcare': ('health', 'medical', 'pharma', 'bio', 'clinic', 'care', 'therap', 'lab'),
             'finance': ('bank', 'financial', 'capital', 'insurance', 'credit', 'invest', 'wealth',
                         'mortgage', 'account', 'payroll', 'tax'),
             'tech': ('tech', 'software', 'systems', 'data', 'digital', 'cyber', 'network', 'solutions',
                      'computer', 'it ', 'cloud', 'labs', 'analytics')}
    if any(h in n for h in hints[query_sector]):
        return query_sector
    return None


def is_nj(row: dict) -> bool:
    return row.get('bbb_id') == '0221' or (row.get('zip_code') or '')[:2] in ('07', '08')


def sweep(sectors: list[str], max_pages: int) -> dict[str, dict]:
    found: dict[str, dict] = {}
    jobs = [(sec, q) for sec in sectors for q in QUERIES[sec]]
    print(f'  [*] {len(jobs)} queries x up to {max_pages} pages', flush=True)

    def run_query(job: tuple[str, str]) -> list[dict]:
        sec, q = job
        rows = []
        with httpx.Client(headers={'User-Agent': UA, 'Accept': 'text/html',
                                   'Accept-Language': 'en-US,en;q=0.9'},
                          follow_redirects=True) as client:
            info, paths = fetch(client, q, 1)
            if not info:
                print(f'  [!] "{q}": no results block', flush=True)
                return rows
            total = int(info.get('total_results') or 0)
            pages = min(max_pages, max(1, -(-total // PER_PAGE)))
            page = 1
            while True:
                for r in info.get('results', []):
                    if not is_nj(r):
                        continue
                    p = paths.get(r['business_id'], {})
                    rows.append({**r, 'query': q, 'query_sector': sec, **p})
                page += 1
                if page > pages:
                    break
                time.sleep(0.8)
                info, paths = fetch(client, q, page)
                if not info or not info.get('results'):
                    break
        nj = len(rows)
        print(f'  [*] "{q}" ({sec}): {total} results, {pages} page(s) read, {nj} NJ rows', flush=True)
        return rows

    with cf.ThreadPoolExecutor(3) as ex:
        for rows in ex.map(run_query, jobs):
            for r in rows:
                bid = r['business_id']
                cur = found.get(bid)
                if cur is None:
                    found[bid] = {
                        'business_id': bid, 'name': r['business_name'], 'city': r.get('city', ''),
                        'zip': r.get('zip_code', ''), 'phone': r.get('business_phone', ''),
                        'rating': r.get('business_rating', ''),
                        'accredited': r.get('accredited_status') == 'AB',
                        'category': r.get('category', ''), 'queries': [r['query']],
                        'query_sectors': [r['query_sector']],
                        'profile': (f"https://www.bbb.org/us/nj/{r['city'].lower().replace(' ', '-')}/profile/"
                                    f"{r['category']}/x-{r['bbb_id']}-{bid}" if r.get('city') else ''),
                    }
                else:
                    if r['query'] not in cur['queries']:
                        cur['queries'].append(r['query'])
                    if r['query_sector'] not in cur['query_sectors']:
                        cur['query_sectors'].append(r['query_sector'])
                    if not cur['category'] and r.get('category'):
                        cur['category'] = r['category']
                        cur['city'] = r.get('city', '')
    for row in found.values():
        # the sector the slug says, else the sector of the query that found it
        sec = None
        for qs in row['query_sectors']:
            sec = sector_of(row['category'], row['name'], qs)
            if sec:
                break
        row['sector'] = sec
    return found


def write_md(rows: list[dict]) -> None:
    by = {'healthcare': [], 'finance': [], 'tech': []}
    for r in rows:
        if r.get('sector') in by and r.get('accredited'):
            by[r['sector']].append(r)
    lines = ['# BBB-accredited employers in New Jersey — healthcare, finance, tech', '',
             f"_Generated {time.strftime('%Y-%m-%d %H:%M')} by auto-apply/bbb_nj.py from bbb.org search "
             f"(NJ = BBB Serving New Jersey or a 07/08 zip). {sum(len(v) for v in by.values())} accredited "
             f"businesses in the three sectors; {len(rows)} NJ hits total._", '',
             '| # | Company | City | Category | Rating | Careers |', '|--:|---|---|---|---|---|']
    for sec, items in by.items():
        lines += ['', f'## {sec.title()} ({len(items)})', '',
                  '| # | Company | City | Category | Rating | Careers |', '|--:|---|---|---|---|---|']
        for i, r in enumerate(sorted(items, key=lambda x: x['name'].lower()), 1):
            careers = r.get('ats') and f"{r['ats']}: {r.get('board') or r.get('careers_url', '')}" or \
                (r.get('website') or '')
            lines.append(f"| {i} | {r['name']} | {r.get('city', '')} | {r.get('category', '').replace('-', ' ')} "
                         f"| {r.get('rating', '')} | {careers} |")
    del lines[4:6]
    OUT_MD.write_text('\n'.join(lines) + '\n')


def main() -> int:
    max_pages = 40
    sectors = list(QUERIES)
    if '--max-pages' in sys.argv:
        max_pages = int(sys.argv[sys.argv.index('--max-pages') + 1])
    if '--sector' in sys.argv:
        sectors = [sys.argv[sys.argv.index('--sector') + 1]]
    print(f"\n{'=' * 60}\n  SCIPIO BBB NJ SWEEP\n{'=' * 60}")
    prev = {}
    if OUT_JSON.exists():
        prev = {r['business_id']: r for r in json.loads(OUT_JSON.read_text())}
    found = sweep(sectors, max_pages)
    # keep phase-2 fields (website/careers/ats) from an earlier run
    for bid, row in found.items():
        for k in ('website', 'careers_url', 'ats', 'board', 'resolved_at', 'resolve_note'):
            if bid in prev and prev[bid].get(k):
                row[k] = prev[bid][k]
    for bid, row in prev.items():
        found.setdefault(bid, row)
    rows = sorted(found.values(), key=lambda r: (r.get('sector') or 'zzz', r['name'].lower()))
    OUT_JSON.write_text(json.dumps(rows, indent=1, ensure_ascii=False) + '\n')
    write_md(rows)
    acc = [r for r in rows if r.get('accredited')]
    by = {}
    for r in acc:
        by[r.get('sector')] = by.get(r.get('sector'), 0) + 1
    print(f'  [*] {len(rows)} NJ businesses ({len(acc)} accredited): ' +
          ', '.join(f'{k or "other/skipped"} {v}' for k, v in sorted(by.items(), key=lambda kv: str(kv[0]))))
    print(f'  [*] wrote {OUT_JSON.name} + {OUT_MD}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
