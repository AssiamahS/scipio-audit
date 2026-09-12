#!/usr/bin/env python3
"""Sweep the Gmail inbox for ATS rejection emails and sync them into the
tracker (jobs.json) + a rejections.json feed for the dashboard.

Same inbox mechanics as the security-code fetcher: the main account POP-sweeps
mail into Trash within seconds, so INBOX + [Gmail]/Trash + [Gmail]/Spam are all
searched, and freshness is enforced on the Date header in Python — never with
IMAP SINCE (gmail evaluates it in Pacific time and silently drops early-UTC
"today" mail).

Usage:
  uv run python rejections.py [--days 14] [--dry-run]
Env:
  GMAIL_APP_PASSWORD  app password for the inbox (required)
  GMAIL_IMAP_USER     inbox to read (default: profile.json email)
"""

import email as email_mod
import imaplib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

HERE = Path(__file__).parent
JOBS = HERE.parent / 'jobs.json'
FEED = HERE / 'rejections.json'
CONFIRM_FEED = HERE / 'confirmations.json'
ACTION_FEED = HERE / 'needs_reply.json'
ISSUE_MARKS = HERE / 'interview_issues.json'


def _raise_interview_issue(actions: list) -> None:
    """An interview email that nobody reads is a lost interview (Blue Water,
    7/23: 'interview availability' sat in 105@ for 5 days, rejected 7/28).
    A labeled GitHub issue reaches the phone via the GitHub app — one per
    message-id, never repeated."""
    import os
    import httpx
    token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    if not token:
        print('  [!] no GH_TOKEN: interview issue not raised (needs_reply.json still written)')
        return
    done = set(json.loads(ISSUE_MARKS.read_text())) if ISSUE_MARKS.exists() else set()
    for a in actions:
        mid = a.get('message_id') or a.get('subject')
        if mid in done:
            continue
        title = f"INTERVIEW REQUEST: {a.get('company') or 'unmatched'} — {a.get('subject', '')[:70]}"
        body = (f"From: {a.get('from')}\nDate: {a.get('date')}\nMailbox: {a.get('mailbox')}\n"
                f"Job: #{a.get('job_id')}\n\nReply from 105@ TODAY. Interview requests expire.")
        try:
            r = httpx.post('https://api.github.com/repos/AssiamahS/scipio/issues',
                           headers={'Authorization': f'token {token}',
                                    'Accept': 'application/vnd.github+json'},
                           json={'title': title, 'body': body, 'labels': ['interview']},
                           timeout=20)
            if r.status_code in (200, 201):
                done.add(mid)
                print(f"  [!!] GitHub issue raised: {title[:70]}")
            else:
                print(f"  [!] issue create HTTP {r.status_code}: {r.text[:120]}")
        except Exception as e:
            print(f'  [!] issue create failed: {e}')
    ISSUE_MARKS.write_text(json.dumps(sorted(done)))
APPLY_LOG = HERE / 'apply_log.json'

# ATS "your application went through" emails (Greenhouse et al send these
# minutes after a successful submit). Used to upgrade submit_unconfirmed
# apply_log entries to submitted.
CONFIRM_PHRASES = [
    'thank you for applying', 'thanks for applying',
    'thank you for your application', 'application has been received',
    'we received your application', 'we have received your application',
    'your application was submitted', 'application has been submitted',
    'successfully submitted', 'confirm receipt of your application',
]

# Phrases that mean "no" in ATS-speak. Body must hit one of these AND look
# application-related, so newsletters/marketing don't trip it.
REJECT_PHRASES = [
    'not to move forward', 'not moving forward', 'will not be moving forward',
    'decided to move forward with other', 'move forward with other candidates',
    'pursue other candidates', 'pursuing other candidates',
    'other applicants whose qualifications', 'position has been filled',
    'no longer under consideration', 'not been selected',
    'unable to offer you', 'not selected for this position',
    'decided not to proceed', 'wish you success in your',
    'wish you the best in your job search', 'encourage you to apply for future',
]
# A human wants something back. These are the only emails in the whole inbox
# that are worth interrupting Sylvester for: the Blue Water Thinking interview
# request sat unanswered from 2026-07-23 until they rejected him on 07-28.
INTERVIEW_PHRASES = [
    'interview availability', 'your availability', 'schedule a call',
    'schedule an interview', 'set up a time', 'set up a call',
    'move forward with the interview', 'excited to move forward',
    'like to speak with you', 'like to chat', 'phone screen',
    'initial conversation', 'next steps in the process',
    'invitation to interview', 'book a time', 'calendly.com',
    'please let us know what times', 'when are you available',
]
APPLICATION_MARKERS = ['application', 'applying', 'applied', 'candidacy',
                       'your interest in', 'interviewing with']
ATS_DOMAINS = ['greenhouse', 'lever.co', 'myworkday', 'ashbyhq', 'icims',
               'smartrecruiters', 'jobvite', 'bamboohr', 'successfactors']


def _norm(s: str) -> str:
    return re.sub(r'[^a-z0-9 ]', '', (s or '').lower()).strip()


def _company_tokens(company: str) -> list[str]:
    stop = {'inc', 'llc', 'the', 'of', 'and', 'health', 'care', 'group', 'co'}
    toks = [t for t in _norm(company).replace('/', ' ').split()
            if len(t) > 3 and t not in stop]
    return toks or _norm(company).split()


def _match_job(jobs: list, text: str):
    """Match email text to a tracked job. Companies are often stored as board
    slugs ('connectionshealthsolutions'), so besides token matching, compare
    space-squashed normalized forms."""
    squashed = _norm(text).replace(' ', '')
    for j in jobs:
        if j.get('status') in ('rejected', 'withdrawn', 'accepted', 'bad_url'):
            continue
        company = j.get('company', '')
        toks = _company_tokens(company)
        slug = _norm(company).replace(' ', '')
        if (toks and all(t in text for t in toks)) or \
           (len(slug) > 4 and slug in squashed):
            return j
    return None


def _real_interview(box: str, match, frm: str) -> bool:
    """An interview request is real when it lands outside Spam AND either
    matches a tracked job or comes from a known ATS sender."""
    if 'spam' in (box or '').lower():
        return False
    if match is not None:
        return True
    return any(d in (frm or '').lower() for d in ATS_DOMAINS)


def _mark_rejected(job: dict, dt_iso: str, subj: str):
    job['status'] = 'rejected'
    job['updated_date'] = dt_iso[:10]
    job.setdefault('history', []).append(
        {'status': 'rejected', 'date': dt_iso[:16].replace('T', ' '),
         'note': f'email: {subj[:80]}'})


def _body_text(msg) -> str:
    body = ''
    for part in msg.walk():
        if part.get_content_type() in ('text/plain', 'text/html'):
            try:
                body += part.get_payload(decode=True).decode(errors='ignore')
            except Exception:
                pass
    return re.sub(r'<[^>]+>', ' ', body)


def sweep(days: int, dry_run: bool) -> int:
    app_pw = os.environ.get('GMAIL_APP_PASSWORD')
    if not app_pw:
        print('[!] GMAIL_APP_PASSWORD unset — skipping rejection sweep')
        return 0
    profile = json.loads((HERE / 'profile.json').read_text())
    user = os.environ.get('GMAIL_IMAP_USER') or profile['email']
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    db = json.loads(JOBS.read_text())
    jobs = db['jobs']
    feed = json.loads(FEED.read_text()) if FEED.exists() else []
    seen = {(r.get('message_id') or r.get('subject'), r.get('from')) for r in feed}

    # Backfill: re-match earlier feed entries that didn't match a job at the
    # time (matcher improvements make these self-healing).
    backfilled = 0
    for r in feed:
        if r.get('job_id') is None:
            m = _match_job(jobs, (r.get('subject', '') + ' ' + r.get('from', '')).lower())
            if m:
                r['job_id'], r['company'] = m['id'], m['company']
                if not dry_run:
                    _mark_rejected(m, r.get('date', ''), r.get('subject', ''))
                backfilled += 1
    if backfilled:
        print(f'[~] backfilled {backfilled} previously unmatched rejection(s)')

    confirm_feed = json.loads(CONFIRM_FEED.read_text()) if CONFIRM_FEED.exists() else []
    confirm_seen = {(r.get('message_id') or r.get('subject'), r.get('from')) for r in confirm_feed}

    hits = []
    confirms = []
    actions = []
    # socket timeout: on 9/5 a fetch sat in tcp_recvmsg against Gmail for 10+
    # minutes with no timeout set, and every step after the sweep (follow-ups,
    # digest) never ran. A stalled mailbox is skipped, never waited on.
    M = imaplib.IMAP4_SSL('imap.gmail.com', timeout=60)
    M.login(user, app_pw)
    for box in ('INBOX', '"[Gmail]/Trash"', '"[Gmail]/Spam"'):
        try:
            M.select(box, readonly=True)
            typ, data = M.search(None, 'ALL')
        except Exception as e:
            print(f'  [~] {box}: select/search failed ({e}); skipping mailbox', flush=True)
            continue
        stalls = 0
        for mid in data[0].split()[-400:]:
            try:
                typ, msg_data = M.fetch(mid, '(BODY.PEEK[])')
                msg = email_mod.message_from_bytes(msg_data[0][1])
                dt = parsedate_to_datetime(msg['Date'])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            except (TimeoutError, OSError, imaplib.IMAP4.abort) as e:
                stalls += 1
                print(f'  [~] {box}: fetch stalled ({type(e).__name__}); giving up on this mailbox', flush=True)
                try:
                    M = imaplib.IMAP4_SSL('imap.gmail.com', timeout=60)
                    M.login(user, app_pw)
                except Exception:
                    pass
                break
            except Exception:
                continue
            if dt < cutoff:
                continue
            subj = str(email_mod.header.make_header(
                email_mod.header.decode_header(msg.get('Subject', ''))))
            frm = str(msg.get('From', ''))
            key = (str(msg.get('Message-ID') or subj), frm)
            if key in seen:
                continue
            hay = (subj + ' ' + _body_text(msg)).lower()
            applicationish = (any(m in hay for m in APPLICATION_MARKERS)
                              or any(d in frm.lower() for d in ATS_DOMAINS))
            if not applicationish:
                continue
            # Interview requests outrank everything: they expire.
            if (any(p in hay for p in INTERVIEW_PHRASES)
                    and not any(p in hay for p in REJECT_PHRASES)):
                match = _match_job(jobs, hay + ' ' + frm.lower())
                if not _real_interview(box, match, frm):
                    # a mobile-notary spam blast in [Gmail]/Spam opened issue #2
                    # as an "interview" on 8/30. Spam folder + no tracked
                    # company + no ATS sender = marketing, not a recruiter.
                    print(f"  [~] ignored interview-looking mail from {frm[:50]} ({box.strip(chr(34))}, no tracked company)")
                    seen.add(key)
                    continue
                actions.append({'date': dt.isoformat(), 'from': frm,
                                'reply_to': str(msg.get('Reply-To') or ''),
                                'subject': subj,
                                'message_id': str(msg.get('Message-ID') or ''),
                                'job_id': match['id'] if match else None,
                                'company': match['company'] if match else None,
                                'mailbox': box.strip('"')})
                seen.add(key)
                if match and not dry_run and match.get('status') != 'interview':
                    match['status'] = 'interview'
                    match['updated_date'] = dt.strftime('%Y-%m-%d')
                    match.setdefault('history', []).append(
                        {'status': 'interview', 'date': dt.strftime('%Y-%m-%d %H:%M')})
            # Rejections next: a rejection often opens with "thank you for
            # applying" before the bad news.
            elif any(p in hay for p in REJECT_PHRASES):
                match = _match_job(jobs, hay + ' ' + frm.lower())
                hits.append({'date': dt.isoformat(), 'from': frm, 'subject': subj,
                             'reply_to': str(msg.get('Reply-To') or ''),
                             'message_id': str(msg.get('Message-ID') or ''),
                             'job_id': match['id'] if match else None,
                             'company': match['company'] if match else None,
                             'mailbox': box.strip('"')})
                seen.add(key)
                if match and not dry_run:
                    _mark_rejected(match, dt.isoformat(), subj)
            elif any(p in hay for p in CONFIRM_PHRASES):
                if key in confirm_seen:
                    continue
                match = _match_job(jobs, hay + ' ' + frm.lower())
                if match and not dry_run and match.get('status') in (
                        'hand_apply', 'manual_review', 'captcha_retry', 'needs_account', 'submitted_unverified', 'needs_code', 'wishlist'):
                    # the board says it arrived: a hand submit from HAND_APPLY.md
                    # or an unverified click is now a real application
                    match['status'] = 'applied'
                    match['applied_date'] = match.get('applied_date') or dt.strftime('%Y-%m-%d')
                    match['updated_date'] = dt.strftime('%Y-%m-%d')
                    match.setdefault('history', []).append(
                        {'status': 'applied', 'date': dt.strftime('%Y-%m-%d %H:%M'),
                         'note': f'confirmation email: {subj[:60]}'})
                confirms.append({'date': dt.isoformat(), 'from': frm,
                                 'reply_to': str(msg.get('Reply-To') or ''),
                                 'subject': subj,
                                 'message_id': str(msg.get('Message-ID') or ''),
                                 'job_id': match['id'] if match else None,
                                 'company': match['company'] if match else None,
                                 'mailbox': box.strip('"')})
                confirm_seen.add(key)
    try:
        M.logout()
    except Exception:
        pass

    for h in hits:
        tag = f"job #{h['job_id']} {h['company']}" if h['job_id'] else 'UNMATCHED'
        print(f"[x] {h['date'][:10]}  {tag}  «{h['subject'][:70]}»")
    for h in confirms:
        print(f"[+] {h['date'][:10]}  {h['company'] or 'UNMATCHED'}  «{h['subject'][:70]}»")
    for h in actions:
        print(f"[!!] NEEDS A REPLY  {h['date'][:10]}  "
              f"{h['company'] or 'UNMATCHED'}  «{h['subject'][:70]}»")
    if actions:
        print(f"\n  {len(actions)} email(s) are waiting on Sylvester. "
              f"Interview requests expire; answer these first.")
        if not dry_run:
            _raise_interview_issue(actions)
        # reply draft + prep brief + phone alert, the same minute it is seen
        try:
            sys.path.insert(0, str(HERE))
            from interview_pack import handle as _pack
            by_id = {str(j.get('id')): j for j in jobs}
            for a in actions:
                _pack(a, by_id.get(str(a.get('job_id'))), dry_run=dry_run)
        except Exception as e:
            print(f'  [!] interview pack failed: {e}')

    upgraded = _upgrade_unconfirmed(confirm_feed + confirms, dry_run)

    if (hits or backfilled) and not dry_run:
        feed.extend(hits)
        FEED.write_text(json.dumps(feed, indent=2))
        JOBS.write_text(json.dumps(db, indent=2))
    if actions and not dry_run:
        prev = json.loads(ACTION_FEED.read_text()) if ACTION_FEED.exists() else []
        known = {a.get('message_id') for a in prev}
        prev.extend(a for a in actions if a['message_id'] not in known)
        ACTION_FEED.write_text(json.dumps(prev, indent=2))
        JOBS.write_text(json.dumps(db, indent=2))
    if confirms and not dry_run:
        confirm_feed.extend(confirms)
        CONFIRM_FEED.write_text(json.dumps(confirm_feed, indent=2))
        JOBS.write_text(json.dumps(db, indent=2))
    print(f"[=] {len(hits)} new rejection(s), {len(confirms)} new confirmation(s), "
          f"{upgraded} apply-log entr(ies) upgraded to submitted")
    return len(hits)


def _upgrade_unconfirmed(confirmations: list, dry_run: bool) -> int:
    """Greenhouse emails a 'thank you for applying' within minutes of a real
    submit — use those to flip submit_unconfirmed apply_log entries to
    submitted. Each email confirms at most one entry: the closest submit in
    the 48h before the email."""
    if not confirmations or not APPLY_LOG.exists():
        return 0
    log = json.loads(APPLY_LOG.read_text())
    used = {e.get('confirmed_by') for e in log if e.get('confirmed_by')}
    upgraded = 0
    for c in sorted(confirmations, key=lambda x: x.get('date', '')):
        cid = c.get('message_id') or c.get('subject', '')
        if not cid or cid in used:
            continue
        ctext = ((c.get('company') or '') + ' ' + c.get('subject', '')
                 + ' ' + c.get('from', ''))
        csquash = _norm(ctext).replace(' ', '')
        try:
            cdt = datetime.fromisoformat(c['date'])
        except Exception:
            continue
        best = None
        for e in log:
            if e.get('status') != 'submit_unconfirmed' or e.get('confirmed_by'):
                continue
            token = (e.get('url', '').split('/') + [''])[3] if 'greenhouse' in e.get('url', '') \
                else _norm(e.get('company', '')).replace(' ', '')
            eslug = _norm(e.get('company', '')).replace(' ', '')
            if not ((len(token) > 4 and token in csquash)
                    or (len(eslug) > 4 and eslug in csquash)):
                continue
            try:
                edt = datetime.fromisoformat(e['timestamp'])
                if edt.tzinfo is None:
                    edt = edt.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            delta = (cdt - edt).total_seconds()
            if -300 <= delta <= 48 * 3600 and (best is None or delta < best[0]):
                best = (delta, e)
        if best:
            _, e = best
            e['status'] = 'submitted'
            e['confirmed_via'] = 'email'
            e['confirmed_by'] = cid
            used.add(cid)
            upgraded += 1
            print(f"[^] upgraded {e.get('company') or e.get('url', '')[:50]} "
                  f"({e['timestamp'][:16]}) via «{c.get('subject', '')[:50]}»")
    if upgraded and not dry_run:
        APPLY_LOG.write_text(json.dumps(log, indent=2))
    return upgraded


if __name__ == '__main__':
    days = 14
    if '--days' in sys.argv:
        days = int(sys.argv[sys.argv.index('--days') + 1])
    sweep(days, '--dry-run' in sys.argv)
