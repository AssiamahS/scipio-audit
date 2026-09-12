#!/usr/bin/env python3
"""Discover each Workday tenant's public career-site ID by probing the cxs
jobs endpoint with common site-name patterns. A real site returns HTTP 200
with jobPostings; a wrong name returns the S21 site-not-found error.

Stamps 'site' into f500_coverage.json workday rows so the phone app can
search real openings directly (no auth needed).
"""
import json
import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).parent


def candidates(tenant: str) -> list[str]:
    t = tenant
    tc = tenant.capitalize()
    return [
        f"{tc}_External_Career_Site", f"{t}_External_Career_Site",
        "External_Career_Site", f"{tc}_Careers", f"{tc}Careers",
        f"{tc}_External", "External", "Careers", "External_Careers",
        f"{tc}_Career_Site", "careers", f"{t}careers", "Global_Careers",
        f"{tc}_Jobs", "Jobs", "External_Site", f"{tc}ExternalSite", "1",
    ]


def probe(host: str, tenant: str, site: str) -> bool:
    url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    body = json.dumps({"limit": 1, "offset": 0, "searchText": ""}).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json", "Accept": "application/json",
        "User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return b"jobPostings" in r.read(400)
    except Exception:
        return False


def find_site(board: str):
    host = board.split("//")[1].rstrip("/")
    tenant = host.split(".")[0]
    for site in candidates(tenant):
        if probe(host, tenant, site):
            return site
        time.sleep(0.1)
    return None


def main():
    path = HERE / "f500_coverage.json"
    d = json.loads(path.read_text())
    todo = [r for r in d["companies"] if r["status"] == "workday" and r.get("board")]
    print(f"{len(todo)} workday tenants")

    def work(r):
        r["site"] = find_site(r["board"])
        return r["site"]

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(work, todo))
    found = sum(1 for s in results if s)
    path.write_text(json.dumps(d, indent=0))
    print(f"done: {found}/{len(todo)} site IDs discovered")
    for r in todo[:15]:
        if r.get("site"):
            print(f"  {r['name']}: {r['site']}")


if __name__ == "__main__":
    main()
