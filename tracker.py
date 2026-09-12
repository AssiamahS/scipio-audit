#!/usr/bin/env python3
"""
Job Application Tracker - lightweight CLI tool
Usage:
  python3 tracker.py add <company> <role> [--url URL] [--salary SALARY] [--notes NOTES]
  python3 tracker.py list [--status STATUS] [--sort date|company|status]
  python3 tracker.py update <id> <status>
  python3 tracker.py stats
  python3 tracker.py remove <id>
  python3 tracker.py export [--format csv|json]
  python3 tracker.py resume [load <path> | show]

Statuses: wishlist, applied, screening, interview, offer, rejected, withdrawn, accepted
"""

import json, csv, sys, os, shutil
from datetime import datetime
from pathlib import Path

DB = Path(__file__).parent / "jobs.json"
RESUME_DIR = Path(__file__).parent / "resumes"

STATUSES = ["wishlist", "applied", "screening", "interview", "offer", "rejected", "withdrawn", "accepted"]
STATUS_ICONS = {
    "wishlist": "[ ]", "applied": "[>]", "screening": "[~]", "interview": "[!]",
    "offer": "[*]", "rejected": "[x]", "withdrawn": "[-]", "accepted": "[+]"
}

def load_db():
    if DB.exists():
        return json.loads(DB.read_text())
    return {"jobs": [], "next_id": 1, "resume": None}

def save_db(db):
    DB.write_text(json.dumps(db, indent=2))

def add_job(args):
    db = load_db()
    company = args[0] if len(args) > 0 else input("Company: ")
    role = args[1] if len(args) > 1 else input("Role: ")

    url = extract_flag(args, "--url") or ""
    salary = extract_flag(args, "--salary") or ""
    notes = extract_flag(args, "--notes") or ""

    job = {
        "id": db["next_id"],
        "company": company,
        "role": role,
        "url": url,
        "salary": salary,
        "notes": notes,
        "status": "applied",
        "applied_date": datetime.now().strftime("%Y-%m-%d"),
        "updated_date": datetime.now().strftime("%Y-%m-%d"),
        "history": [{"status": "applied", "date": datetime.now().strftime("%Y-%m-%d %H:%M")}]
    }
    db["jobs"].append(job)
    db["next_id"] += 1
    save_db(db)
    print(f"Added #{job['id']}: {company} - {role}")

def list_jobs(args):
    db = load_db()
    jobs = db["jobs"]

    status_filter = extract_flag(args, "--status")
    if status_filter:
        jobs = [j for j in jobs if j["status"] == status_filter]

    sort_key = extract_flag(args, "--sort") or "date"
    if sort_key == "date":
        jobs.sort(key=lambda j: j["applied_date"], reverse=True)
    elif sort_key == "company":
        jobs.sort(key=lambda j: j["company"].lower())
    elif sort_key == "status":
        jobs.sort(key=lambda j: STATUSES.index(j["status"]))

    if not jobs:
        print("No applications found.")
        return

    # Header
    print(f"{'ID':>4}  {'St':4} {'Company':<20} {'Role':<25} {'Applied':<12} {'Salary':<12}")
    print("-" * 83)
    for j in jobs:
        icon = STATUS_ICONS.get(j["status"], "[ ]")
        sal = j.get("salary", "")[:12]
        print(f"#{j['id']:<3}  {icon} {j['company'][:20]:<20} {j['role'][:25]:<25} {j['applied_date']:<12} {sal:<12}")

    print(f"\nTotal: {len(jobs)} application(s)")

def update_status(args):
    if len(args) < 2:
        print("Usage: update <id> <status>")
        return

    job_id = int(args[0])
    new_status = args[1].lower()

    if new_status not in STATUSES:
        print(f"Invalid status. Choose from: {', '.join(STATUSES)}")
        return

    db = load_db()
    for j in db["jobs"]:
        if j["id"] == job_id:
            old = j["status"]
            j["status"] = new_status
            j["updated_date"] = datetime.now().strftime("%Y-%m-%d")
            j["history"].append({"status": new_status, "date": datetime.now().strftime("%Y-%m-%d %H:%M")})
            save_db(db)
            print(f"#{job_id}: {old} -> {new_status}")
            return
    print(f"Job #{job_id} not found.")

def show_stats(args):
    db = load_db()
    jobs = db["jobs"]
    if not jobs:
        print("No applications yet.")
        return

    counts = {}
    for j in jobs:
        counts[j["status"]] = counts.get(j["status"], 0) + 1

    print("=== Application Stats ===")
    print(f"Total: {len(jobs)}")
    print()
    for s in STATUSES:
        c = counts.get(s, 0)
        if c > 0:
            bar = "#" * min(c, 40)
            print(f"  {STATUS_ICONS[s]} {s:<12} {c:>3}  {bar}")

    # Response rate
    responded = sum(counts.get(s, 0) for s in ["screening", "interview", "offer", "accepted"])
    total_decided = responded + counts.get("rejected", 0)
    if total_decided > 0:
        rate = (responded / total_decided) * 100
        print(f"\n  Response rate: {rate:.0f}% ({responded}/{total_decided})")

    # This week
    from datetime import timedelta
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    this_week = [j for j in jobs if j["applied_date"] >= week_ago]
    print(f"  This week: {len(this_week)} applications")

def remove_job(args):
    if not args:
        print("Usage: remove <id>")
        return
    job_id = int(args[0])
    db = load_db()
    before = len(db["jobs"])
    db["jobs"] = [j for j in db["jobs"] if j["id"] != job_id]
    if len(db["jobs"]) < before:
        save_db(db)
        print(f"Removed #{job_id}")
    else:
        print(f"Job #{job_id} not found.")

def export_jobs(args):
    fmt = extract_flag(args, "--format") or "csv"
    db = load_db()

    if fmt == "json":
        out = Path(__file__).parent / "export.json"
        out.write_text(json.dumps(db["jobs"], indent=2))
        print(f"Exported {len(db['jobs'])} jobs to {out}")
    else:
        out = Path(__file__).parent / "export.csv"
        if not db["jobs"]:
            print("No jobs to export.")
            return
        fields = ["id", "company", "role", "status", "applied_date", "updated_date", "url", "salary", "notes"]
        with open(out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(db["jobs"])
        print(f"Exported {len(db['jobs'])} jobs to {out}")

def handle_resume(args):
    RESUME_DIR.mkdir(exist_ok=True)
    db = load_db()

    if not args or args[0] == "show":
        if db.get("resume"):
            print(f"Current resume: {db['resume']}")
            p = RESUME_DIR / Path(db["resume"]).name
            if p.exists():
                print(f"Stored at: {p}")
        else:
            print("No resume loaded. Use: tracker.py resume load <path>")
        return

    if args[0] == "load":
        if len(args) < 2:
            print("Usage: resume load <path-to-resume>")
            return
        src = Path(args[1]).expanduser()
        if not src.exists():
            print(f"File not found: {src}")
            return
        dst = RESUME_DIR / src.name
        shutil.copy2(src, dst)
        db["resume"] = str(dst)
        save_db(db)
        print(f"Resume loaded: {dst}")

def extract_flag(args, flag):
    """Extract --flag value from args list."""
    for i, a in enumerate(args):
        if a == flag and i + 1 < len(args):
            return args[i + 1]
    return None

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1]
    args = sys.argv[2:]

    commands = {
        "add": add_job,
        "list": list_jobs,
        "ls": list_jobs,
        "update": update_status,
        "stats": show_stats,
        "remove": remove_job,
        "rm": remove_job,
        "export": export_jobs,
        "resume": handle_resume,
    }

    fn = commands.get(cmd)
    if fn:
        fn(args)
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)

if __name__ == "__main__":
    main()
