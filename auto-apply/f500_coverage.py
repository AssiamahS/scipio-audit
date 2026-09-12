#!/usr/bin/env python3
"""
Fortune 500 coverage board: which companies can scipio auto-apply to,
which are blocked and why, and who's been applied to already.

Probes public ATS APIs by slug guess (greenhouse/lever/ashby/smartrecruiters)
and Workday tenant hosts, merges apply_log.json, writes ../f500_coverage.json
for the pages site + the phone app.

Status values:
  applied      — at least one application logged for this company
  attemptable  — a handled ATS (greenhouse/lever) board was found
  workday      — Workday tenant found: needs the account-creation flow
  other_ats    — board found on an unhandled ATS (ashby/smartrecruiters)
  unknown      — no public board found by slug guessing; needs manual careers URL
"""
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.request import Request, urlopen

HERE = Path(__file__).parent
HANDLED = {"greenhouse", "lever"}  # what ats_handlers.py fills reliably today
TIMEOUT = 6


def slugify(name: str) -> list[str]:
    base = re.sub(r"[^a-z0-9 ]", "", name.lower())
    words = base.split()
    slugs = ["".join(words)]
    if len(words) > 1:
        slugs.append(words[0])
    return list(dict.fromkeys(s for s in slugs if len(s) > 2))[:2]


def get(url: str, post_json: str | None = None) -> tuple[int, str]:
    headers = {"User-Agent": "Mozilla/5.0 (coverage-probe)"}
    data = None
    if post_json is not None:
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "application/json"
        data = post_json.encode()
    req = Request(url, headers=headers, data=data)
    try:
        with urlopen(req, timeout=TIMEOUT) as r:
            return r.status, r.read(2000).decode("utf-8", "ignore")
    except Exception as e:
        code = getattr(e, "code", 0)
        body = ""
        try:
            body = e.read(2000).decode("utf-8", "ignore")  # HTTPError carries a body
        except Exception:
            pass
        return (code if isinstance(code, int) else 0), body


def name_matches(board_name: str, company: str) -> bool:
    """Strict identity check: slug squatters (a vet clinic on /archer, a
    Beatles cover band on /disney) must not count as the F500 company."""
    a = norm(board_name)
    b = norm(company)
    if not a:
        return False
    return a == b or (a in b and len(a) >= 8) or (b in a and len(b) >= 8)


# Boards whose NAME matches an F500 company but whose POSTINGS prove otherwise
# (checked 2026-08-06: national=Canadian PR firm, universal=London design studio,
# alliance=empty board). Name similarity alone cannot catch these.
SLUG_DENYLIST = {"national", "universal", "alliance"}


def detect_ats(name: str) -> tuple[str, str]:
    """Return (ats, board_url) for the first verified hit, else ('', '')."""
    for slug in slugify(name):
        if slug in SLUG_DENYLIST:
            continue
        code, body = get(f"https://boards-api.greenhouse.io/v1/boards/{slug}")
        if code == 200:
            m = re.search(r'"name"\s*:\s*"([^"]+)"', body)
            if m and name_matches(m.group(1), name):
                return "greenhouse", f"https://boards.greenhouse.io/{slug}"
        code, body = get(f"https://jobs.lever.co/{slug}")
        if code == 200:
            m = re.search(r"<title>([^<]+)</title>", body)
            title = (m.group(1) if m else "").replace(" jobs", "").replace(" Careers", "")
            if name_matches(title, name):
                return "lever", f"https://jobs.lever.co/{slug}"
        code, body = get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
        if code == 200 and '"jobs"' in body:
            return "ashby", f"https://jobs.ashbyhq.com/{slug}"
        code, body = get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=1")
        # 200 with totalFound:0 comes back for ANY slug — only a non-empty
        # posting list proves the company is actually on smartrecruiters
        m = re.search(r'"totalFound"\s*:\s*(\d+)', body) if code == 200 else None
        if m and int(m.group(1)) > 0:
            return "smartrecruiters", f"https://careers.smartrecruiters.com/{slug}"
        for wd in ("wd1", "wd5", "wd3"):
            # Real tenants answer the cxs API with a site-not-found JSON
            # (errorCode S21 naming Job_Posting_Site_ID); dead tenants 422 bare.
            code, body = get(
                f"https://{slug}.{wd}.myworkdayjobs.com/wday/cxs/{slug}/probe/jobs",
                post_json='{"limit":1,"offset":0}')
            # S21/Job_Posting_Site = real tenant, wrong site name.
            # Dead slugs return errorCode HTTP_422 — must NOT match.
            if "Job_Posting_Site" in body:
                return "workday", f"https://{slug}.{wd}.myworkdayjobs.com/"
    return "", ""


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def main():
    companies = json.load(open(HERE.parent / "F500.json"))
    # duplicate names in the source list crash the iOS app's dict build —
    # dedupe defensively even if F500.json regresses
    seen: set[str] = set()
    companies = [c for c in companies
                 if not (c["name"] in seen or seen.add(c["name"]))]
    log = json.load(open(HERE / "apply_log.json"))

    # applied counts by normalized company name (substring match both ways)
    applied: dict[str, int] = {}
    for entry in log:
        c = norm(entry.get("company", ""))
        if c and c != "unknown":
            applied[c] = applied.get(c, 0) + 1

    def applied_count(name: str) -> int:
        n = norm(name)
        return sum(v for k, v in applied.items() if k in n or n in k)

    def probe(c):
        ats, board = detect_ats(c["name"])
        count = applied_count(c["name"])
        if count > 0:
            status = "applied"
        elif ats in HANDLED:
            status = "attemptable"
        elif ats == "workday":
            status = "workday"
        elif ats:
            status = "other_ats"
        else:
            status = "unknown"
        return {
            "rank": c["rank"], "name": c["name"], "sector": c["sector"],
            "ats": ats or None, "board": board or None,
            "applied": count, "status": status,
        }

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=40) as pool:
        rows = list(pool.map(probe, companies))
    rows.sort(key=lambda r: r["rank"])

    summary = {}
    for r in rows:
        summary[r["status"]] = summary.get(r["status"], 0) + 1
    out = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "summary": summary,
        "companies": rows,
    }
    json.dump(out, open(HERE.parent / "f500_coverage.json", "w"), indent=0)
    print(f"done in {time.time()-t0:.0f}s — {summary}")


if __name__ == "__main__":
    sys.exit(main())
