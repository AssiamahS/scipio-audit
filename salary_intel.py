#!/usr/bin/env python3
"""Build f500_salaries.json: H-1B disclosure salary stats per F500 company.

One polite crawl of h1bdata.info (public DOL LCA data), aggregated to medians
so the phone app can show 'what they actually pay' + a verdict against the
$120k floor. US-citizen note: H-1B filings are just the only public per-company
salary disclosures — the wage data applies to the roles, not the visa.

  python3 salary_intel.py            # full run (~4 min, 0.4s/request)
"""
import json
import re
import statistics
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
FLOOR = 120_000
# roles that look like Sylvester's lane
LANE = re.compile(r"project manager|program manager|business analyst|systems? analyst|"
                  r"infrastructure|it manager|product manager|operations engineer|"
                  r"systems engineer|network|support engineer|service", re.I)


def fetch(company: str) -> list[tuple[str, int]]:
    q = urllib.parse.quote_plus(company.lower())
    url = f"https://h1bdata.info/index.php?em={q}&year=All+Years"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            html = r.read().decode("utf-8", "ignore")
    except Exception:
        return []
    rows = []
    # table rows: EMPLOYER, JOB TITLE, BASE SALARY, LOCATION, SUBMIT, START
    for tr in re.findall(r"<tr>(.*?)</tr>", html, re.S):
        tds = re.findall(r"<td[^>]*>\s*(?:<a[^>]*>)?([^<]*)", tr)
        if len(tds) >= 3:
            m = re.match(r"^[\d,]+$", tds[2].strip())
            if m:
                try:
                    rows.append((tds[1].strip(), int(tds[2].replace(",", ""))))
                except ValueError:
                    pass
    return rows


def stats_for(rows):
    if not rows:
        return None
    sals = sorted(s for _, s in rows if 30_000 < s < 900_000)
    if not sals:
        return None
    lane = sorted(s for t, s in rows if LANE.search(t) and 30_000 < s < 900_000)
    out = {
        "n": len(sals),
        "median": int(statistics.median(sals)),
        "p25": sals[len(sals) // 4],
        "p75": sals[(len(sals) * 3) // 4],
    }
    if lane:
        out["lane_n"] = len(lane)
        out["lane_median"] = int(statistics.median(lane))
    out["clears_floor"] = (out.get("lane_median") or out["median"]) >= FLOOR
    return out


def main():
    companies = json.loads((HERE / "F500.json").read_text())
    out = {}
    t0 = time.time()
    for i, c in enumerate(companies):
        rows = fetch(c["name"])
        s = stats_for(rows)
        if s:
            out[c["name"]] = s
        if i % 50 == 0:
            print(f"  {i}/500 ({len(out)} with data, {time.time()-t0:.0f}s)")
        time.sleep(0.4)
    (HERE / "f500_salaries.json").write_text(json.dumps(
        {"floor": FLOOR, "updated": time.strftime("%Y-%m-%d"), "companies": out}))
    print(f"done: {len(out)}/500 companies with salary data in {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
