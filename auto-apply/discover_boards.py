"""Board discovery — the supply side grows by itself.

9/9 measurement: the 114 hand-picked boards produced ~7,000 postings a run,
of which 8 were new title/location matches and 1 reached the wishlist. The
applier can submit to any Greenhouse / Lever / Ashby board, so the fix is
more boards, found without a human: public GitHub code is full of job
aggregators and scrapers that embed board tokens next to the three public
APIs. This script

  1. code-searches GitHub for the API hosts (gh CLI, GH_TOKEN — present on
     the box and the Mac),
  2. pulls the matching files and regex-extracts tokens,
  3. verifies each token live and keeps only boards that CURRENTLY post at
     least one title/location match for him (his filters, not an industry
     guess),
  4. appends them to scout_config.json and remembers every token it has
     judged in discovered_boards.json so a daily run only pays for new ones.

Run: uv run python discover_boards.py [--max-verify N] [--dry-run]
"""
from __future__ import annotations

import collections
import concurrent.futures as cf
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx

HERE = Path(__file__).parent
CONFIG_PATH = HERE / 'scout_config.json'
STATE_PATH = HERE / 'discovered_boards.json'

QUERIES = {
    'greenhouse': 'boards-api.greenhouse.io/v1/boards',
    'lever': 'api.lever.co/v0/postings',
    'ashby': 'api.ashbyhq.com/posting-api/job-board',
}
LANGS = ['', ' language:json', ' language:python', ' language:javascript', ' language:typescript',
         ' language:yaml', ' language:markdown', ' language:go', ' language:ruby', ' language:html']
RX = {
    'greenhouse': re.compile(r'(?:boards-api\.greenhouse\.io/v1/boards|(?:boards|job-boards)\.greenhouse\.io)/([A-Za-z0-9_-]{2,60})'),
    'lever': re.compile(r'(?:api\.lever\.co/v0/postings|jobs\.lever\.co)/([A-Za-z0-9_-]{2,60})'),
    'ashby': re.compile(r'(?:api\.ashbyhq\.com/posting-api/job-board|jobs\.ashbyhq\.com)/([A-Za-z0-9_.-]{2,60})'),
}
PLACEHOLDER = re.compile(r'^(v\d|boards|postings|job-board|jobs|embed|api|test|example|company|slug|token|board|your|my|foo|bar|xxx+)$', re.I)
HEADERS = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/128 Safari/537.36'}


def _gh(args: list[str], timeout: int = 60) -> str:
    try:
        return subprocess.run(['gh', *args], capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ''


def search_files() -> list[tuple[str, str]]:
    files = []
    for q in QUERIES.values():
        for lang in LANGS:
            out = _gh(['search', 'code', q + lang, '--limit', '100', '--json', 'repository,path'])
            try:
                for h in json.loads(out or '[]'):
                    files.append((h['repository']['nameWithOwner'], h['path']))
            except (json.JSONDecodeError, KeyError):
                continue
            time.sleep(1.5)  # code search: 10 req/min authenticated
    return list(dict.fromkeys(files))


def extract_tokens(files: list[tuple[str, str]]) -> dict[str, set]:
    toks: dict[str, set] = collections.defaultdict(set)

    def pull(f):
        repo, path = f
        raw = _gh(['api', '-H', 'Accept: application/vnd.github.raw', f'repos/{repo}/contents/{path}'])
        found = set()
        for fam, rx in RX.items():
            for t in rx.findall(raw):
                if not PLACEHOLDER.match(t) and not t.startswith(('{', '$', '<', '%')):
                    found.add((fam, t))
        return found

    with cf.ThreadPoolExecutor(6) as ex:
        for found in ex.map(pull, files):
            for fam, t in found:
                toks[fam].add(t)
    return toks


def board_jobs(client: httpx.Client, fam: str, token: str) -> list[dict] | None:
    """Live postings for a board, or None when the board does not exist."""
    try:
        if fam == 'greenhouse':
            r = client.get(f'https://boards-api.greenhouse.io/v1/boards/{token}/jobs', timeout=20)
            if r.status_code != 200:
                return None
            return [{'role': j.get('title', ''), 'location': (j.get('location') or {}).get('name', '')}
                    for j in r.json().get('jobs', [])]
        if fam == 'lever':
            r = client.get(f'https://api.lever.co/v0/postings/{token}?mode=json', timeout=20)
            if r.status_code != 200:
                return None
            return [{'role': j.get('text', ''), 'location': (j.get('categories') or {}).get('location', '') or ''}
                    for j in r.json()]
        if fam == 'ashby':
            r = client.get(f'https://api.ashbyhq.com/posting-api/job-board/{token}', timeout=20)
            if r.status_code != 200:
                return None
            return [{'role': j.get('title', ''), 'location': j.get('location', '') or ''}
                    for j in r.json().get('jobs', []) if j.get('isListed', True)]
    except Exception:
        return None
    return None


def verify(tokens: dict[str, set], cfg: dict, state: dict, max_verify: int) -> dict[str, list[str]]:
    sys.path.insert(0, str(HERE))
    from scout import title_ok, location_ok
    judged = state.setdefault('judged', {})
    added: dict[str, list[str]] = collections.defaultdict(list)
    todo = [(fam, t) for fam, ts in tokens.items() for t in sorted(ts)
            if t.lower() not in {x.lower() for x in cfg.get(fam, [])} and f'{fam}:{t.lower()}' not in judged]
    todo = todo[:max_verify]
    print(f'  [*] verifying {len(todo)} candidate boards ({len(judged)} already judged)')
    with httpx.Client(headers=HEADERS, follow_redirects=True) as client:
        for i, (fam, t) in enumerate(todo, 1):
            jobs = board_jobs(client, fam, t)
            key = f'{fam}:{t.lower()}'
            if jobs is None:
                judged[key] = {'ok': False, 'why': 'no such board', 'when': time.strftime('%Y-%m-%d')}
                continue
            n = sum(1 for j in jobs if title_ok(j['role'], cfg) and location_ok(j['location'], cfg))
            judged[key] = {'ok': n > 0, 'matches': n, 'total': len(jobs), 'when': time.strftime('%Y-%m-%d')}
            if n > 0:
                cfg.setdefault(fam, []).append(t)
                added[fam].append(t)
                print(f'  [+] {fam}/{t}: {n} matching of {len(jobs)} postings')
            if i % 50 == 0:
                print(f'  [*] {i}/{len(todo)} verified, {sum(len(v) for v in added.values())} added so far')
    return added


def main() -> int:
    dry = '--dry-run' in sys.argv
    max_verify = 400
    if '--max-verify' in sys.argv:
        max_verify = int(sys.argv[sys.argv.index('--max-verify') + 1])
    cfg = json.loads(CONFIG_PATH.read_text())
    state = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}
    print(f"\n{'=' * 60}\n  SCIPIO BOARD DISCOVERY {'(DRY RUN)' if dry else ''}\n{'=' * 60}")
    files = search_files()
    print(f'  [*] {len(files)} public files reference the board APIs')
    tokens = extract_tokens(files)
    print('  [*] tokens seen: ' + ', '.join(f'{k} {len(v)}' for k, v in tokens.items()))
    added = verify(tokens, cfg, state, max_verify)
    total = sum(len(v) for v in added.values())
    print(f'  [*] {total} new board(s) with a live match: ' +
          ', '.join(f'{k} +{len(v)}' for k, v in added.items()) if total else '  [*] no new boards today')
    if not dry:
        cfg['boards_discovered'] = f"{time.strftime('%Y-%m-%d')}: +{total} via discover_boards.py (GitHub code search, live-verified)"
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + '\n')
        STATE_PATH.write_text(json.dumps(state, indent=1) + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
