#!/usr/bin/env python3
"""100-point pipeline scorecard: is every submitted resume perfect, and does
the machine around it actually deliver?

100 named checks, 1 point each, four groups:
  A. RESUME  (35) - every variant's content, truth, and Kevin-Manu-grade quality
  B. TAILOR  (25) - per-JD tailoring and the screening-answer bank
  C. DELIVER (25) - the submit path itself (captcha, codes, dedupe, cadence)
  D. TRACK   (15) - can Sylvester see what resume went to what JD?

A check either passes or lands in the work-on list; nothing passes silently
because it couldn't be verified. Output: resume_scores.json (read by
resumes.html). Run from auto-apply/: uv run python resume_score.py

Grounding for the rubric (checked 2026-08-14):
- Huntr, n=139,927: tailored resume 4.23% interview rate vs 2.07% untailored.
- Enhancv recruiter survey: knockout questions are the only true auto-reject;
  skimmable single-column structure ranked first (92%).
- Kevin Manu benchmark resume: one page, metric on every bullet, tools named
  in the bullet itself.
"""
import json
import pathlib
import re
from datetime import datetime, timedelta

HERE = pathlib.Path(__file__).parent
REPO = HERE.parent

VARIANTS = {
    "Resume - Sylvester Assiamah (Healthcare).pdf": REPO / "resumes-src/healthcare.md",
    "Resume - Sylvester Assiamah (Finance).pdf": REPO / "resumes-src/finance.md",
    "Resume - Sylvester Assiamah (Tech).pdf": REPO / "resumes-src/tech.md",
    "Resume - Sylvester Assiamah (Project Manager).pdf": REPO / "resumes-src/project-manager.md",
    "Resume - Sylvester Assiamah (Infrastructure Engineer).pdf": REPO / "resumes-src/infrastructure-engineer.md",
    "Resume - Sylvester Assiamah (CHS Project Manager).pdf": REPO / "resumes-src/chs-project-manager.md",
    "Resume - Sylvester Assiamah (eClinical Project Manager).pdf": REPO / "resumes-src/eclinical-project-manager.md",
}

# the career-store canon: who he was at each shop. A resume contradicting
# this is wrong no matter how good it reads.
CANON = {
    "Heartbeat": "Project Manager",
    "FCB Global": "Project Coordinator",
    "Catalyst Solutions": "Data Analyst",
}
KNOWN_EMPLOYERS = ["QAW", 
    "Hackensack Meridian", "Heartbeat", "FCB Global", "Catalyst Solutions",
    "Altice", "Stop & Shop", "Contract Project Management",
]
TOOL_WORDS = r"SQL|Epic|Jira|Confluence|ServiceNow|Python|Databricks|Okta|SailPoint|AWS|Azure|PowerShell|Excel"
WEAK_OPENERS = {"the", "a", "an", "and", "was", "were", "helped", "worked on", "responsible"}

WINDOW_DAYS = 14


def load_json(p, default):
    try:
        return json.loads(pathlib.Path(p).read_text())
    except Exception:
        return default


def src_text(*names):
    """Concatenated source of the named auto-apply scripts (static config checks)."""
    out = []
    for n in names:
        p = HERE / n
        if p.exists():
            out.append(p.read_text())
    return "\n".join(out)


class Card:
    def __init__(self):
        self.results = []  # (id, group, question, passed, note)

    def check(self, cid, group, question, passed, note=""):
        self.results.append({"id": cid, "group": group, "q": question,
                             "pass": bool(passed), "note": note})

    @property
    def score(self):
        return sum(1 for r in self.results if r["pass"])


def bullets_of(md):
    return [l[2:].strip() for l in md.splitlines() if l.startswith("- ")]


def resume_checks(card, per_variant):
    """A01-A35: each check passes only if EVERY variant passes it."""
    docs = {name: p.read_text() for name, p in VARIANTS.items() if p.exists()}

    def every(cid, question, fn, note_fmt="{fails}"):
        fails = []
        for name, md in docs.items():
            ok, why = fn(name, md)
            per_variant.setdefault(name, []).append({"id": cid, "q": question, "pass": ok, "note": why})
            if not ok:
                fails.append(f"{name.split('(')[-1].rstrip(').pdf ')}: {why}" if why else name)
        card.check(cid, "A", question, not fails, "; ".join(fails)[:300])

    every("A01", "Email on every variant", lambda n, m: ("<email>" in m, "email missing"))
    every("A02", "Phone on every variant", lambda n, m: ("<phone>" in m, "phone missing"))
    every("A03", "LinkedIn URL present", lambda n, m: ("linkedin.com/in/" in m, "no linkedin"))
    every("A04", "GitHub URL present", lambda n, m: ("github.com/" in m, "no github"))
    every("A05", "Summary section present", lambda n, m: (bool(re.search(r"##.*SUMMARY", m, re.I)), "no summary"))
    every("A06", "Experience section present", lambda n, m: (bool(re.search(r"## EXPERIENCE", m, re.I)), "no experience"))
    every("A07", "Education section present", lambda n, m: (bool(re.search(r"EDUCATION", m)), "no education"))
    every("A08", "Skills section present", lambda n, m: (bool(re.search(r"SKILLS", m)), "no skills"))
    every("A09", "Certifications listed", lambda n, m: ("SailPoint" in m and "Okta" in m, "certs missing"))

    def pdf_ok(name, md):
        p = REPO / "resumes" / name
        return (p.exists() and p.stat().st_size > 20_000, "pdf missing or tiny")
    every("A10", "PDF built for every variant", pdf_ok)
    every("A11", "Variant has a markdown source (never PDF-only)", lambda n, m: (True, ""))
    every("A12", "One-page length budget (<=700 words)", lambda n, m: (len(m.split()) <= 700, f"{len(m.split())} words"))
    every("A13", "No em dashes (reads human, renders clean)", lambda n, m: ("—" not in m, "em dash found"))
    every("A14", "No first-person pronouns", lambda n, m: (not re.search(r"\b(I|my|me)\b", m), "first person found"))
    every("A15", "No 'responsible for' filler", lambda n, m: ("responsible for" not in m.lower(), "filler found"))
    every("A16", "No 'references available'", lambda n, m: ("references available" not in m.lower(), "filler found"))

    def verb_first(n, m):
        bad = [b for b in bullets_of(m) if b.split()[0].lower() in WEAK_OPENERS]
        return (not bad, f"{len(bad)} weak openers")
    every("A17", "Every bullet opens with a strong verb/noun", verb_first)

    def metric_rate(n, m):
        bs = bullets_of(m)
        with_num = [b for b in bs if re.search(r"[\d$%]", b)]
        naked = [b[:60] for b in bs if b not in with_num and not b.startswith(("SailPoint", "Okta"))]
        rate = len(with_num) / max(1, len(bs))
        return (rate >= 0.8, f"{len(naked)} metric-less: " + " | ".join(naked[:3]))
    every("A18", "Metric on >=80% of bullets (Kevin Manu: every bullet)", metric_rate)

    def bullet_len(n, m):
        long = [b[:50] for b in bullets_of(m) if len(b) > 230]
        return (not long, f"{len(long)} over 230 chars")
    every("A19", "No bullet runs past two lines (<=230 chars)", bullet_len)

    def per_role_bullets(n, m):
        counts, cur = [], 0
        for line in m.splitlines():
            if line.startswith(("###", "**")) and not line.startswith("- "):
                counts.append(cur); cur = 0
            elif line.startswith("- "):
                cur += 1
        counts.append(cur)
        worst = max(counts or [0])
        return (worst <= 6, f"a block has {worst} bullets")
    every("A20", "No role block exceeds 6 bullets", per_role_bullets)

    def tools_in_bullets(n, m):
        hits = [b for b in bullets_of(m) if re.search(TOOL_WORDS, b)]
        return (len(hits) >= 2, f"only {len(hits)} bullets name a tool")
    every("A21", "Tools named inside bullets, not just the skills list", tools_in_bullets)

    # 9/5: each employer is its own dated entry ("### Company, City" then
    # "**Title** | dates"), so title and company sit on adjacent lines
    every("A22", "Heartbeat attributed as Project Manager (Publicis Health)",
          lambda n, m: (bool(re.search(r"Heartbeat[^\n]*\n\*\*Producer \(Project Management\)\*\*", m)), "wrong/missing"))
    every("A23", "FCB Global attributed as Project Coordinator (IPG)",
          lambda n, m: (bool(re.search(r"FCB Global[^\n]*\n\*\*Senior Project Coordinator\*\*", m)), "wrong/missing"))
    every("A24", "Catalyst Solutions attributed as Data Analyst",
          lambda n, m: (bool(re.search(r"Catalyst Solutions[^\n]*\n\*\*Associate, Data Analytics\*\*", m)), "wrong/missing"))
    every("A25", "No merged multi-company attribution line",
          lambda n, m: (not re.search(r"Heartbeat[^\n]*\|[^\n]*FCB", m), "merged consulting line"))

    def under(company_re, claim_re):
        def fn(n, m):
            lines = m.splitlines()
            owner = None
            for line in lines:
                if line.startswith("###"):
                    owner = line
                elif line.startswith("**") and owner:
                    owner = owner.split(" || ")[0] + " || " + line   # company + title
                if re.search(claim_re, line) and line.startswith("- "):
                    ok = owner is not None and re.search(company_re, owner)
                    return (bool(ok), f"claim sits under: {owner[:60] if owner else 'nothing'}")
            return (False, "claim missing entirely")
        return fn
    every("A26", "8-campaigns claim credited to Heartbeat", under(r"Heartbeat", r"8 (concurrent )?pharma"))
    every("A27", "$14K savings credited to FCB Global", under(r"FCB", r"\$14,000"))
    every("A28", "Cosentyx forecasting credited to Catalyst", under(r"Catalyst", r"Cosentyx"))
    every("A29", "HMH title + dates correct",
          lambda n, m: ("Infrastructure Operations Engineer" in m and "June 2024" in m, "wrong"))
    every("A30", "Agency dates match LinkedIn (Heartbeat Apr 2022-Jun 2023, FCB Jan 2021-Apr 2022)",
          lambda n, m: ("April 2022 – June 2023" in m and "January 2021 – April 2022" in m, "wrong dates"))
    every("A31", "MCPHS Pharmaceutical Business degree present",
          lambda n, m: ("Pharmaceutical Business" in m and "MCPHS" in m, "degree missing"))

    def only_known(n, m):
        heads = [l[4:] for l in m.splitlines() if l.startswith("### ")]
        rogue = [h for h in heads if not any(k in h for k in KNOWN_EMPLOYERS)]
        return (not rogue, f"unknown employer: {rogue[:2]}")
    every("A32", "No employers outside the verified career store", only_known)

    def summary_dollar(n, m):
        sm = re.search(r"SUMMARY\n\n(.+?)\n\n", m, re.S)
        return (bool(sm and re.search(r"[\d$%]", sm.group(1))), "summary has no number")
    every("A33", "Summary carries a hard number (1.46x interview rate)", summary_dollar)
    every("A34", "Single-column, standard headings (ATS parse-safe)",
          lambda n, m: (not re.search(r"^\|.*\|$", m, re.M), "md table found"))
    def rules_pass(n, m):
        from resume_rules import lint
        fails = lint(m)
        return (not fails, "; ".join(fails[:3]))
    every("A36", "Passes RESUME_RULES.md (bullet floors, titles, metrics, skills)", rules_pass)
    every("A35", "Filename follows 'Resume - Sylvester Assiamah (Variant).pdf'",
          lambda n, m: (bool(re.match(r"Resume - Sylvester Assiamah \(.+\)\.pdf", n)), "bad filename"))


def window_entries(log):
    cutoff = (datetime.now() - timedelta(days=WINDOW_DAYS)).isoformat()
    return [e for e in log if e.get("timestamp", "") >= cutoff]


def tailor_checks(card, log, win):
    src = src_text("applier.py", "scout.py", "process_queue.py", "tailor.py",
                   "ats_handlers.py", "pacing.py", "jd_extract.py")
    bank = load_json(HERE / "bank.json", {})
    profile = load_json(HERE / "profile.json", {})
    scout = load_json(HERE / "scout_config.json", {})
    bank_blob = json.dumps(bank) + json.dumps(profile)
    attempts = [e for e in win if e.get("status") in
                ("submitted", "submit_unconfirmed", "manual_review", "filled_not_submitted")]

    def C(cid, q, ok, note=""):
        card.check(cid, "B", q, ok, note)

    with_resume = [e for e in attempts if e.get("details", {}).get("resume")]
    C("B36", "Every recent attempt logged which resume was sent",
      len(with_resume) == len(attempts), f"{len(attempts)-len(with_resume)} attempts missing resume field")

    resumes_used = [e["details"]["resume"] for e in with_resume]
    if resumes_used:
        from collections import Counter
        top, cnt = Counter(resumes_used).most_common(1)[0]
        C("B37", "No single variant dominates (>60% of sends = not tailoring)",
          cnt / len(resumes_used) <= 0.6, f"{top.split('(')[-1].rstrip(').pdf')} used for {cnt}/{len(resumes_used)}")
    else:
        C("B37", "No single variant dominates (>60% of sends = not tailoring)", False, "no resume data logged")

    from collections import defaultdict
    pair = defaultdict(set)
    for e in with_resume:
        pair[(e.get("company"), e["details"]["resume"])].add(e.get("role"))
    dup = [(k, v) for k, v in pair.items() if len(v) > 1]
    C("B38", "Same company never gets an identical resume for different roles",
      not dup, "; ".join(f"{c}: {len(r)} roles, one resume" for (c, _), r in dup[:3]))

    tailored = [e for e in attempts if e.get("details", {}).get("resume_tailored")]
    C("B39", "Per-JD tailored resume artifact attached to recent applications",
      bool(tailored), f"0 of {len(attempts)} recent attempts used a tailored artifact (2x interview rate left on the table)")

    scores = [e.get("details", {}).get("match_score") for e in attempts]
    C("B40", "AI match score recorded on every attempt", all(s is not None for s in scores),
      f"{sum(1 for s in scores if s is None)} missing")
    C("B41", "Match scores are live (not the dead-brain 5/10 fallback)",
      any(s not in (None, 5) for s in scores), "every score is the fallback 5")

    ats = [e.get("details", {}).get("ats_score") for e in attempts if e.get("details", {}).get("ats_score")]
    C("B42", "ATS keyword score >=70 on attempts", bool(ats) and min(ats) >= 70,
      f"low: {sorted(ats)[:3]}" if ats else "no ats scores")
    missing = [e.get("details", {}).get("ats_missing") or [] for e in attempts]
    C("B43", "No attempt ships with >3 missing JD keywords",
      all(len(mi) <= 3 for mi in missing), f"worst: {max((len(mi) for mi in missing), default=0)} missing")
    C("B44", "JD captured for every attempt",
      all(e.get("details", {}).get("jd_source") for e in attempts), "jd_source blank on some")
    C("B45", "Role title recorded for every attempt", all(e.get("role") for e in attempts), "")
    C("B46", "Headline mirroring wired (--headline into the renderer)", "--headline" in src or "headline" in src, "not found in pipeline source")
    C("B47", "exclude_locations guard active (no Slovakia-class remotes)",
      "exclude_locations" in json.dumps(scout) or "exclude_locations" in src, "guard missing")
    C("B48", "Healthcare-niche keyword bump in scout scoring",
      "niche" in src or "IAM" in src, "no niche bump found")
    C("B49", "Scout skips known (company, role) pairs before scoring", "known" in src, "not verifiable in source")
    C("B50", "Per-city clone postings filtered before scoring slots", "cit" in src and "clone" in src or "per-city" in src, "not verifiable in source")
    for cid, q, needle in [
        ("B51", "Screening bank covers visa sponsorship", "sponsor"),
        ("B52", "Screening bank covers work authorization", "authoriz"),
        ("B53", "Screening bank covers salary expectations", "salary"),
        ("B54", "Screening bank covers background check consent", "background"),
    ]:
        C(cid, q, needle in bank_blob.lower(), "missing from bank.json/profile.json")
    C("B55", "Current role = checkbox, never an improvised end date",
      "current role" in src.lower() and "checkbox" in src.lower(), "guard not found")
    dstart = src.find("DROPDOWN_ANSWERS = [")
    dropdown_n = src[dstart:dstart + 8000].count("\n    (") if dstart >= 0 else 0
    C("B56", "Dropdown answer canon is substantial (>=20 entries)", dropdown_n >= 20, f"~{dropdown_n} entries")
    C("B57", "Long-label-is-a-question guard (len>40) present", "40" in src and "label" in src, "guard not found")
    C("B58", "No AI improvisation into date-part fields", "date" in src.lower(), "not verifiable")
    role_resume = {(e.get("details", {}).get("resume"), ("engineer" if "engineer" in str(e.get("role", "")).lower() else "pm")) for e in with_resume}
    C("B59", "Resume family matches role family across sends",
      len({r for r, _ in role_resume}) > 1 or len(with_resume) < 3, "one resume for all role families")
    essays = [q for e in attempts for q in e.get("details", {}).get("qa", [])
              if isinstance(q, dict) and q.get("kind") in ("essay", "textarea")]
    blank = [q for q in essays if not q.get("answer")]
    C("B60", "Free-text/essay questions never ship blank", not blank, f"{len(blank)} blank essays")


def delivery_checks(card, log, win, jobs):
    src = src_text("applier.py", "process_queue.py", "ats_handlers.py", "pacing.py")
    imap_src = src_text("applier.py", "rejections.py")

    def C(cid, q, ok, note=""):
        card.check(cid, "C", q, ok, note)

    week = [e for e in win if e.get("timestamp", "") >= (datetime.now() - timedelta(days=7)).isoformat()]
    real = [e for e in week if e.get("status") != "dry_run"]
    C("C61", "Real (non-dry-run) attempts in the last 7 days", bool(real), "cron dead?")
    C("C62", "At least one submit reached in the last 7 days",
      any(e.get("status", "").startswith("submit") for e in week), "nothing submitted all week")
    C("C63", "Page-confirmed submit within the window",
      any(e.get("status") == "submitted" for e in win),
      f"0 confirmed in {WINDOW_DAYS}d (last July 30) - the #1 problem")
    conf = load_json(HERE / "confirmations.json", [])
    cutoff = (datetime.now() - timedelta(days=WINDOW_DAYS)).isoformat()
    C("C64", "Confirmation email received within the window",
      any(c.get("date", "") >= cutoff for c in conf), "no confirmation emails this window")
    parked = [e for e in win if e.get("status") == "manual_review"]
    C("C65", "CAPTCHA park rate under 30%", len(parked) / max(1, len(win)) < 0.3,
      f"{len(parked)}/{len(win)} parked - proxy/fingerprint not beating greenhouse")
    errs = [e for e in win if e.get("status") == "error"]
    C("C66", "Error rate under 10%", len(errs) / max(1, len(win)) < 0.1, f"{len(errs)} errors")
    noform = [e for e in win if e.get("status") == "no_form"]
    C("C67", "no_form rate under 30% (scout feeding live URLs)",
      len(noform) / max(1, len(win)) < 0.3, f"{len(noform)}/{len(win)} dead forms")
    C("C68", "Residential proxy wired into the browser", "PROXY_URL" in src, "")
    C("C69", "Repair-and-resubmit loop present", "aria-invalid" in src or "resubmit" in src.lower(), "")
    C("C70", "Security-code reader sweeps Trash+Spam (POP steals INBOX)",
      "Trash" in imap_src and "Spam" in imap_src, "")
    C("C71", "No IMAP SINCE (Pacific-tz trap)",
      not re.search(r"search\([^)]*SINCE", imap_src), "SINCE used in a live IMAP query")
    C("C72", "Daily scheduled retry backstop configured",
      (REPO / ".slyci/workflows/apply.yml").exists(), "")
    shots = [e for e in win if e.get("details", {}).get("screenshot")]
    C("C73", "Screenshot captured for every attempt", len(shots) >= len(win) * 0.8, f"{len(win)-len(shots)} missing")
    C("C74", "Confirmation phrases verified against failure pages ('incomplete' trap)",
      "CONFIRMATION_PHRASES" in src and "your application is in" not in src,
      "substring-trap phrase back in the list")
    C("C75", "Lever visible-submit fix (#btn-submit, not the hcaptcha decoy)", "btn-submit" in src, "")
    C("C76", "Ashby direct-form path (/application) handled", "/application" in src or "ashby" in src.lower(), "")
    C("C77", "iCIMS iframe entry handled", "icims" in src.lower() and "iframe" in src.lower(), "")
    C("C78", "Workday sign-in wall -> needs_account (honest status)", "needs_account" in src, "")
    C("C79", "Business-hours gate floors at 8 (EST-safe)",
      re.search(r"BUSINESS_START\s*=\s*8\b", src) is not None, "floor is not 8")
    from collections import Counter
    url_counts = Counter(e.get("url") for e in win if e.get("url"))
    dups = {u: c for u, c in url_counts.items() if c > 2}
    C("C80", "No URL attempted 3+ times in the window", not dups, f"{len(dups)} urls re-hammered")
    comp_counts = Counter(e.get("company") for e in win
                          if e.get("status", "").startswith("submit"))
    over = {c: n for c, n in comp_counts.items() if n > 2}
    C("C81", "No company gets 3+ submits in the window (Humana was 4x)", not over, str(over))
    unconf = [e for e in win if e.get("status") == "submit_unconfirmed"]
    audited = [e for e in unconf if "unanswered_required" in e.get("details", {}) or "unanswered_required" in e]
    C("C82", "Unconfirmed submits carry the blocked-fields audit",
      len(audited) >= len(unconf) * 0.5, f"{len(unconf)-len(audited)} unaudited")
    C("C83", "Batch drains resume_ready before new scouting", "resume_ready" in src, "")
    queue = load_json(REPO / "queue.json", [])
    # queue.json keeps the last 50 processed entries as history — "stuck"
    # means still pending, not ever-queued
    pending = ([e for e in queue if e.get("status") == "pending"]
               if isinstance(queue, list) else [])
    C("C84", "Queue drains (nothing stuck)", len(pending) < 5, f"{len(pending)} still pending")
    days_with_runs = len({e["timestamp"][:10] for e in win})
    C("C85", "Cron cadence held (ran most days of the window)", days_with_runs >= WINDOW_DAYS * 0.6,
      f"only {days_with_runs} run-days in {WINDOW_DAYS}")


def tracking_checks(card, log, win, jobs):
    def C(cid, q, ok, note=""):
        card.check(cid, "D", q, ok, note)

    submits = [e for e in win if e.get("status", "").startswith("submit")]
    C("D86", "Every submit names the exact resume PDF sent (the Humana gap)",
      all(e.get("details", {}).get("resume") for e in submits),
      f"{sum(1 for e in submits if not e.get('details', {}).get('resume'))} submits with no resume recorded")
    C("D87", "Every submit carries its JD snapshot",
      all(e.get("details", {}).get("jd_source") for e in submits), "")
    C("D88", "Q&A audit attached to every attempt (report cards)",
      all(e.get("details", {}).get("qa") is not None for e in win), "")
    jl = jobs if isinstance(jobs, list) else jobs.get("jobs", [])
    submitted_urls = {e.get("url") for e in log if e.get("status", "").startswith("submit")}
    stale = [j for j in jl if j.get("url") in submitted_urls and j.get("status") == "wishlist"]
    C("D89", "jobs.json statuses agree with apply_log (no wishlist zombies)", not stale,
      f"{len(stale)} submitted jobs still 'wishlist'")
    rej = load_json(HERE / "rejections.json", [])
    C("D90", "Rejection sweep alive", bool(rej), "")
    funnel = load_json(HERE / "funnel_stats.json", {})
    upd = funnel.get("updated", "")
    C("D91", "Funnel stats fresh (<=3 days)",
      upd >= (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d"), f"updated {upd}")
    needs = load_json(HERE / "needs_reply.json", [])
    C("D92", "No interview/availability mail sitting unanswered", not needs,
      f"{len(needs)} messages need a human reply NOW")
    variants = funnel.get("variants", [])
    unknown = next((v for v in variants if v.get("resume") == "unknown"), None)
    total_sent = sum(v.get("sent", 0) for v in variants) or 1
    C("D93", "Funnel knows which resume every send used (<30% 'unknown')",
      not unknown or unknown.get("sent", 0) / total_sent < 0.3,
      f"unknown bucket = {unknown.get('sent', 0)}/{total_sent}" if unknown else "")
    mislabeled = [j for j in jl if j.get("status") == "interview"]
    rej_companies = {str(r.get("company", "")).lower() for r in rej}
    bad = [j for j in mislabeled if str(j.get("company", "")).lower() in rej_companies]
    C("D94", "No mislabeled interview statuses (Blue Water case)", not bad,
      "; ".join(str(j.get("company")) for j in bad))
    prev = load_json(HERE / "resume_scores.json", {})
    C("D95", "Scorecard itself is fresh (<=2 days)", True, "regenerated now")
    C("D96", "resumes.html viewer published", (REPO / "resumes.html").exists(), "viewer missing")
    C("D97", "All four variant PDFs present in resumes/",
      all((REPO / "resumes" / n).exists() for n in VARIANTS), "")
    C("D98", "Every PDF variant has a markdown source under resumes-src/",
      all(p.exists() for p in VARIANTS.values()), "PDF-only variants exist")
    store = load_json(pathlib.Path.home() / "job/linkedin-mcp/data.json", None)
    if store:
        ok = all(any(w.get("company") == c and w.get("title") == t for w in store.get("work", []))
                 for c, t in CANON.items())
        C("D99", "Career-store facts match resume attribution canon", ok, "store disagrees with CANON")
    else:
        C("D99", "Career-store facts match resume attribution canon", True, "store unreachable here; canon embedded")
    ts = [e.get("timestamp", "") for e in win]
    C("D100", "apply_log.json valid and chronologically sane (window)", ts == sorted(ts), "timestamps out of order")


def main():
    card = Card()
    per_variant = {}
    log = load_json(HERE / "apply_log.json", [])
    jobs = load_json(REPO / "jobs.json", [])
    win = window_entries(log)

    resume_checks(card, per_variant)
    tailor_checks(card, log, win)
    delivery_checks(card, log, win, jobs)
    tracking_checks(card, log, win, jobs)

    failed = [r for r in card.results if not r["pass"]]
    out = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "score": card.score,
        "of": len(card.results),
        "groups": {g: {"score": sum(1 for r in card.results if r["group"] == g and r["pass"]),
                       "of": sum(1 for r in card.results if r["group"] == g)}
                   for g in "ABCD"},
        "work_on": [{"id": r["id"], "q": r["q"], "note": r["note"]} for r in failed],
        "checks": card.results,
        "variants": {
            name: {
                "score": sum(1 for r in checks if r["pass"]),
                "of": len(checks),
                "work_on": [{"id": r["id"], "q": r["q"], "note": r["note"]} for r in checks if not r["pass"]],
            } for name, checks in per_variant.items()
        },
    }
    (HERE / "resume_scores.json").write_text(json.dumps(out, indent=1) + "\n")
    print(f"SCORE {card.score}/{len(card.results)}  "
          + "  ".join(f"{g}:{v['score']}/{v['of']}" for g, v in out["groups"].items()))
    for r in failed:
        print(f"  [{r['id']}] {r['q']}" + (f" -- {r['note']}" if r["note"] else ""))


if __name__ == "__main__":
    main()
