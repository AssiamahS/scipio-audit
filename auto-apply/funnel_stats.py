#!/usr/bin/env python3
"""Publishable funnel stats: which resume variant actually gets responses,
and which applications have gone silent for 21+ days (ghosts).

Joins apply_log.json (which resume PDF each run sent) with confirmations,
rejections, and needs_reply (interview invites). Writes funnel_stats.json
for the pages site + phone app.

  uv run python funnel_stats.py
"""
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).parent


def load(name, default):
    p = HERE / name
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def main():
    log = load("apply_log.json", [])
    rejections = load("rejections.json", [])
    invites = load("needs_reply.json", [])

    rej_companies = {norm(r.get("company", "")) for r in rejections if r.get("company")}
    inv_companies = {norm(r.get("company", "")) for r in invites if r.get("company")}

    # one row per distinct posting URL, newest attempt wins
    by_url = {}
    for e in log:
        url = (e.get("url") or e.get("details", {}).get("url") or "").split("?")[0]
        if url:
            by_url[url] = e

    # per-send resume recording began 2026-07-27 (dd36012). Every send before
    # that used the profile's single pinned variant — profile.json's
    # resume_path at the time (c9051ad) — so those sends are attributable,
    # not unknown.
    pre_variant_resume = "Resume - Sylvester Assiamah (Project Manager).pdf"

    variants = {}
    # measure OUR OWN tailored-vs-untailored interview rate instead of
    # trusting anyone's benchmark — the benchmark (2x) is the hypothesis,
    # this table is the experiment
    tailoring = {"tailored": {"sent": 0, "rejected": 0, "interviews": 0},
                 "untailored": {"sent": 0, "rejected": 0, "interviews": 0}}
    ghosts = []
    now = datetime.now()
    for url, e in by_url.items():
        det = e.get("details", {})
        status = (det.get("status") or e.get("status") or "").lower()
        if "submit" not in status:
            continue
        resume = det.get("resume") or (
            pre_variant_resume if e.get("timestamp", "") < "2026-07-27" else "unknown")
        company = e.get("company") or "Unknown"
        v = variants.setdefault(resume, {"sent": 0, "rejected": 0, "interviews": 0})
        v["sent"] += 1
        t = tailoring["tailored" if det.get("tailored") else "untailored"]
        t["sent"] += 1
        c = norm(company)
        got_rej = c and any(c in r or r in c for r in rej_companies if r)
        got_inv = c and any(c in r or r in c for r in inv_companies if r)
        if got_rej:
            v["rejected"] += 1
            t["rejected"] += 1
        if got_inv:
            v["interviews"] += 1
            t["interviews"] += 1
        # ghost: submitted 21+ days ago, no rejection, no invite
        try:
            sent_at = datetime.fromisoformat(e.get("timestamp", "")[:19])
        except ValueError:
            continue
        if not got_rej and not got_inv and now - sent_at > timedelta(days=21):
            ghosts.append({
                "company": company, "role": e.get("role") or "",
                "url": url, "days_silent": (now - sent_at).days,
            })

    for v in variants.values():
        answered = v["rejected"] + v["interviews"]
        v["response_rate"] = round(answered / v["sent"], 3) if v["sent"] else 0

    ghosts.sort(key=lambda g: -g["days_silent"])
    for t in tailoring.values():
        t["interview_rate"] = round(t["interviews"] / t["sent"], 4) if t["sent"] else None
    out = {
        "updated": now.strftime("%Y-%m-%d %H:%M"),
        "tailoring": tailoring,
        "variants": [{"resume": k, **v} for k, v in
                     sorted(variants.items(), key=lambda kv: -kv[1]["sent"])],
        "ghosts": ghosts[:40],
        "open_invites": [{"company": i.get("company"), "subject": i.get("subject", "")[:100],
                          "date": i.get("date", "")} for i in invites][:20],
    }
    (HERE / "funnel_stats.json").write_text(json.dumps(out, indent=1))
    print(f"funnel_stats.json: {len(out['variants'])} variants, "
          f"{len(ghosts)} ghosts, {len(out['open_invites'])} open invites")


if __name__ == "__main__":
    main()
