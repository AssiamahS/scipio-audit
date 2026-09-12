#!/usr/bin/env python3
"""Flip bank.json bullets to approved. Usage: uv run python bank_approve.py <id> [<id>...] | --list-pending"""
import json, sys
from pathlib import Path
P = Path(__file__).parent / 'bank.json'
b = json.loads(P.read_text())
if '--list-pending' in sys.argv or len(sys.argv) == 1:
    for x in b['bullets'] + b['summaries']:
        if not x.get('approved'):
            print(f"{x['id']:16} [{x.get('role', 'summary')}/{x.get('employer', '')}] {x['text']}")
    sys.exit(0)
want = set(sys.argv[1:])
hit = 0
for x in b['bullets'] + b['summaries']:
    if x['id'] in want:
        x['approved'] = True; hit += 1; print('approved', x['id'])
P.write_text(json.dumps(b, indent=2, ensure_ascii=False) + '\n')
print(f'{hit}/{len(want)} flipped')
