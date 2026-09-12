#!/usr/bin/env python3
"""Per-JD resume tailoring — selection, never writing.

Reads bank.json (approved:true text only, verbatim) and, for one job
description, picks the summary, the bullet set, and the WORDING VARIANT of
each bullet that best echoes the JD's own language. Renders an ATS-safe PDF
via render_resume + playwright. Every choice is logged to a manifest so the
phone app can show exactly what was sent and why.

  uv run python tailor.py --jd-file jd.txt --role "IT Project Manager" --out out.pdf
Library use: build_tailored(jd_text, role, out_pdf) -> manifest dict | None
"""
import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

BANK = HERE / "bank.json"
# one-page budget: the headline line under the name used to push the last
# certification onto page 2 on a full render
PDF_MARGIN = {"top": "0.35in", "bottom": "0.3in", "left": "0.5in", "right": "0.5in"}


def tokens(text: str) -> set:
    words = re.findall(r"[a-z][a-z+/#-]{3,}", text.lower())
    grams = set(words)
    for a, b in zip(words, words[1:]):
        grams.add(f"{a} {b}")
    return grams


def echo_score(candidate: str, jd_tokens: set) -> int:
    """How many of the JD's own words/bigrams this wording repeats."""
    return sum(2 if " " in t else 1 for t in tokens(candidate) if t in jd_tokens)


def pick_summary(bank: dict, role_title: str, jd_tokens: set) -> str:
    best, best_score = None, -1
    rt = role_title.lower()
    for s in bank.get("summaries", []):
        if not s.get("approved"):
            continue
        score = echo_score(s["text"], jd_tokens)
        score += 8 * sum(1 for t in s.get("titles", []) if t.lower() in rt or rt in t.lower())
        if score > best_score:
            best, best_score = s, score
    return best["text"] if best else ""


NUMBER_RX = re.compile(r"\d|\$|%")


_STOP = {"the", "a", "an", "and", "of", "for", "to", "in", "on", "at", "by", "with", "so", "from",
         "that", "into", "across", "as", "it", "its", "their", "his", "her", "all", "one", "per"}


def _content_tokens(text: str) -> set:
    return {t for t in re.findall(r"[a-z0-9$%+]+", text.lower()) if t not in _STOP and len(t) > 2}


def _drop_near_duplicates(scored: list, threshold: float = 0.25) -> list:
    """scored = [(relevance, bullet_id, variant_id, text, employer), ...] best
    first; a bullet whose content tokens overlap an already-kept bullet by
    Jaccard >= threshold is the same claim in other words — drop it."""
    kept, seen = [], []
    for item in scored:
        toks = _content_tokens(item[3])
        dup = any(len(toks & k) / max(1, len(toks | k)) >= threshold for k in seen)
        if dup:
            continue
        kept.append(item); seen.append(toks)
    return kept


def pick_bullets(bank: dict, jd_tokens: set, include_pending: bool = False, keep_all: bool = False):
    """Per role: order bullets by JD relevance, keep within min/max, and for
    each bullet choose the approved wording variant with the best JD echo.
    include_pending=True is the PREVIEW mode only (bank_approve.py decides
    what may ship); the applier never sets it."""
    chosen = {}
    manifest = []
    for role in bank.get("roles", []):
        rid = role["id"]
        pool = [b for b in bank.get("bullets", []) if b["role"] == rid
                and (b.get("approved") or include_pending)]
        scored = []
        for b in pool:
            wordings = [(b["id"], b["text"])] + [
                (v["id"], v["text"]) for v in b.get("variants", []) if v.get("approved")
            ]
            # a wording that drops the metric is a downgrade no matter how
            # well it echoes the JD (Kevin Manu rule: number on every bullet;
            # 8/26: echo-first picking left tailored resumes at 13/19 numbered
            # and blocked on RESUME_RULES §3)
            best_id, best_text = max(wordings, key=lambda w: (bool(NUMBER_RX.search(w[1])),
                                                             echo_score(w[1], jd_tokens)))
            relevance = echo_score(best_text, jd_tokens) + \
                2 * sum(1 for t in b.get("tags", []) if t.lower() in " ".join(jd_tokens))
            scored.append((relevance, b["id"], best_id, best_text, b.get("employer")))
        scored.sort(key=lambda x: -x[0])
        # 9/9: the bank carries several wordings of one claim as separate
        # bullets (Heartbeat MLR x2, FCB "4 accounts" x2, Catalyst Cosentyx
        # x2) and tailored resumes shipped both. Keep the first (most
        # relevant) of any near-duplicate pair.
        scored = _drop_near_duplicates(scored)
        lo, hi = role.get("min", 1), role.get("max", len(scored))
        keep = scored[:hi]
        # drop zero-relevance bullets past the minimum — but never a
        # numbered one: each metric bullet dropped costs the 70% floor
        # (8/26: tailored output sat at 13/19 and was blocked)
        while not keep_all and len(keep) > lo and keep[-1][0] == 0 \
                and not NUMBER_RX.search(keep[-1][3]):
            keep.pop()
        # relevance may cut a numbered bullet in favour of an unnumbered one
        # (Altice: "pipeline of 60+" vs the unnumbered territory wording) —
        # swap until >= 75% of this role's bullets carry a number, so the
        # 70% floor in RESUME_RULES §3 holds for every JD, not most
        dropped = [s for s in scored if s not in keep]
        def _num(k):
            return bool(NUMBER_RX.search(k[3]))
        while keep and sum(map(_num, keep)) / len(keep) < 0.75:
            swap_in = next((d for d in dropped if _num(d)), None)
            swap_out = next((k for k in reversed(keep) if not _num(k)), None)
            if not swap_in or not swap_out:
                break
            keep[keep.index(swap_out)] = swap_in
            dropped.remove(swap_in); dropped.append(swap_out)
        if role.get("employers"):
            # attribution: every engagement keeps its own floor of bullets
            # (RESUME_RULES §2: never a one-bullet employer) under its true
            # employer header — pooling is how 8 campaigns read as FCB's
            floor = role.get("per_employer_min", 1)
            for emp in role["employers"]:
                have = [k for k in keep if k[4] == emp]
                for extra in [s for s in scored if s[4] == emp and s not in keep]:
                    if len(have) >= floor:
                        break
                    keep.append(extra); have.append(extra)
        # RESUME_RULES §5: relevance orders the role, but the strongest
        # numbered bullet leads — a recruiter reads the first line only
        def lead(group):
            first_num = next((k for k in group if NUMBER_RX.search(k[3])), None)
            if first_num and group and group[0] is not first_num:
                group.remove(first_num); group.insert(0, first_num)
            return group
        if role.get("employers"):
            ordered = []
            for emp in role["employers"]:
                ordered += lead([k for k in keep if k[4] == emp])
            keep = ordered
        else:
            keep = lead(keep)
        chosen[rid] = [(k[3], k[4]) for k in keep]
        manifest += [{"role": rid, "bullet": k[1], "variant_used": k[2], "score": k[0]}
                     for k in keep]
    # RESUME_RULES §3: >= 70% of bullets carry a number. JD echo may have
    # picked an unnumbered wording; where an approved numbered wording of the
    # same claim exists, use it until the floor is met.
    by_id = {b["id"]: b for b in bank.get("bullets", [])}
    flat = [(rid, i) for rid, items in chosen.items() for i in range(len(items))]
    total = len(flat)
    numbered = sum(1 for rid, i in flat if NUMBER_RX.search(chosen[rid][i][0]))
    for m in manifest:
        if total and numbered / total >= 0.75:
            break
        b = by_id.get(m["bullet"])
        if not b:
            continue
        wordings = [(b["id"], b["text"])] + [(v["id"], v["text"]) for v in b.get("variants", []) if v.get("approved")]
        current = next((t for t in wordings if t[0] == m["variant_used"]), None)
        if current and not NUMBER_RX.search(current[1]):
            alt = next((t for t in wordings if NUMBER_RX.search(t[1])), None)
            if alt:
                items = chosen[m["role"]]
                for i, (text, emp) in enumerate(items):
                    if text == current[1]:
                        items[i] = (alt[1], emp); m["variant_used"] = alt[0]; numbered += 1
                        break
    return chosen, manifest


def to_markdown(bank: dict, summary: str, chosen: dict) -> str:
    ident = bank["identity"]
    lines = [f"# {ident['name']}", "", ident["contact"], "", "## SUMMARY", "", summary, "",
             "## EXPERIENCE", ""]
    for role in bank["roles"]:
        rid = role["id"]
        if not chosen.get(rid):
            continue
        lines.append(f"### {role['company']}, {role['location']}")
        lines.append(f"**{role['title']}** | {role['dates']}")
        employers = role.get("employers")
        if employers:
            # each claim stays under its true employer — pooling them is how
            # the 8-campaigns claim spent weeks reading as FCB's work
            for emp, header in employers.items():
                block = [text for text, e in chosen[rid] if e == emp]
                if block:
                    lines.append(f"**{header}**")
                    lines += [f"- {b}" for b in block]
                    lines.append("")
        else:
            lines += [f"- {text}" for text, _ in chosen[rid]]
            lines.append("")
    lines += ["## SKILLS", ""]
    for cat in bank.get("skills", []):
        lines.append(f"**{cat['category']}:** {', '.join(cat['items'])}")
    lines += ["", "## EDUCATION", ""]
    for e in bank.get("education", []):
        lines.append(f"**{e['school']}**, {e['location']}")
        lines.append(f"{e['credential']} | {e['date']}")
        lines.append("")
    lines += ["## CERTIFICATIONS", ""]
    lines += [f"- {c}" for c in bank.get("certifications", [])]
    return "\n".join(lines) + "\n"


def build_tailored(jd_text: str, role_title: str, out_pdf: str, include_pending: bool = False):
    """Returns manifest dict, or None if tailoring isn't possible."""
    if not BANK.exists() or len(jd_text or "") < 300:
        return None
    bank = json.loads(BANK.read_text())
    jd = tokens(jd_text)
    summary = pick_summary(bank, role_title or "", jd)
    chosen, manifest = pick_bullets(bank, jd, include_pending=include_pending)
    if not summary or not chosen:
        return None

    md = to_markdown(bank, summary, chosen)
    truth_failures = validate_truth(md)
    out = Path(out_pdf)
    out.parent.mkdir(parents=True, exist_ok=True)
    md_path = out.with_suffix(".md")
    html_path = out.with_suffix(".html")
    md_path.write_text(md)

    # mirror the applied-for title under the name ONLY when it matches an
    # approved target title — never claim a title he hasn't honestly done
    headline = None
    rt = (role_title or "").lower()
    for t in bank.get("target_titles", []):
        if t.lower() in rt or rt in t.lower():
            headline = role_title.strip()
            break

    _render_via_cli(md_path, html_path, headline)  # soeasy's renderer, synced by build.sh
    _pdf(html_path, out)
    # one page is a hard rule: JD-echo wordings run a few words longer than
    # the static base, so re-render with tighter leading before failing
    if _pages(out) > 1:
        _render_via_cli(md_path, html_path, headline, compact=True)
        _pdf(html_path, out)
    if _pages(out) > 1 and headline:
        _render_via_cli(md_path, html_path, None, compact=True)
        _pdf(html_path, out)
    from resume_rules import lint as rules_lint
    rules_failures = rules_lint(md, str(out))
    return {"pdf": str(out), "md": str(md_path), "summary_used": summary[:60],
            "choices": manifest, "truth_ok": not truth_failures,
            "truth_failures": truth_failures,
            "rules_ok": not rules_failures, "rules_failures": rules_failures}


def _pdf(html_path, out):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page()
        page.goto(f"file://{html_path.resolve()}")
        page.pdf(path=str(out), format="Letter", margin=PDF_MARGIN)
        b.close()


def _pages(pdf) -> int:
    try:
        from pypdf import PdfReader
        return len(PdfReader(str(pdf)).pages)
    except Exception:
        return 1


def _render_via_cli(md_path, html_path, headline=None, compact=False):
    import subprocess
    # capture_output: the renderer prints "rendered <path>" — if that leaks
    # into OUR stdout it lands ahead of the JSON manifest and the applier's
    # json.loads() throws, silently killing tailoring on every single run
    # (this exact bug shipped untailored resumes from 7/27 to 8/14)
    cmd = [sys.executable, str(HERE / "render_resume.py"),
           str(md_path), str(html_path)]
    if headline:
        cmd += ["--headline", headline]
    if compact:
        cmd.append("--compact")
    subprocess.run(cmd, check=True, capture_output=True)


# the career-store canon: every tailored artifact is validated against this
# before it may be submitted. A resume contradicting it is wrong no matter
# how well it echoes the JD.
TRUTH_CANON = [
    # each employer is its own dated entry now: "### Company, Location" then
    # "**Title** | dates" on the next line (Sylvester, 9/5: never merge jobs)
    ("heartbeat-is-pm", r"Heartbeat[^\n]*\n\*\*Producer \(Project Management\)\*\*"),
    ("fcb-is-coordinator", r"FCB Global[^\n]*\n\*\*Senior Project Coordinator\*\*"),
    ("catalyst-is-analyst", r"Catalyst Solutions[^\n]*\n\*\*Associate, Data Analytics\*\*"),
]
TRUTH_FORBIDDEN = [
    ("no-merged-consulting-line", r"Heartbeat[^\n]*\|[^\n]*FCB"),
    ("no-umbrella-contract-block", r"Contract Project Management"),
]
KNOWN_EMPLOYERS = ("Hackensack Meridian", "QAW", "Heartbeat", "FCB Global",
                   "Catalyst Solutions", "Altice", "Stop & Shop",
                   "Contract Project Management")


def validate_truth(md: str) -> list:
    """Return the list of truth failures (empty = artifact is submittable)."""
    fails = []
    for name, pat in TRUTH_CANON:
        if not re.search(pat, md):
            fails.append(name)
    for name, pat in TRUTH_FORBIDDEN:
        if re.search(pat, md):
            fails.append(name)
    for line in md.splitlines():
        if line.startswith("### ") and not any(k in line for k in KNOWN_EMPLOYERS):
            fails.append(f"unknown-employer:{line[4:40]}")
    return fails


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jd-file", required=True)
    ap.add_argument("--role", default="")
    ap.add_argument("--out", default="resumes_tailored/test.pdf")
    ap.add_argument("--include-pending", action="store_true",
                    help="PREVIEW ONLY: render bullets awaiting approval too")
    a = ap.parse_args()
    m = build_tailored(Path(a.jd_file).read_text(), a.role, a.out, include_pending=a.include_pending)
    print(json.dumps(m, indent=1) if m else "tailoring not possible")


if __name__ == "__main__":
    main()
