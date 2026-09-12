#!/usr/bin/env python3
"""One mail path for the whole email loop: send from 105@, save Gmail drafts,
and push an alert to Sylvester's personal inbox (his phone already buzzes for
that account, no new app to install).

Rules baked in here so no caller can break them:
- Nothing goes to a no-reply / notifications / ATS relay address. Those
  bounce or land in a void; is_human_address() is the gate.
- Outgoing text carries no em dashes (his voice, recruiter stakes).
- Alerts dedupe per subject per UTC day (notify_state.json) so a wake that
  runs four times a day cannot send the same alarm four times.

Env: GMAIL_APP_PASSWORD (required to send), GMAIL_IMAP_USER (default: profile email).
Optional: NTFY_TOPIC — also POSTs the alert to https://ntfy.sh/<topic>.
"""
import imaplib
import json
import os
import re
import smtplib
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr
from pathlib import Path

HERE = Path(__file__).parent
PROFILE = HERE / 'profile.json'
STATE = HERE / 'notify_state.json'

# senders that never read replies. Greenhouse/Lever/Workday relays, and the
# generic no-reply shapes companies put on their own ATS mail.
# matched against the LOCAL part only (no '@'): prefixes for the relay shapes,
# exact names for shared role mailboxes nobody answers from
NOREPLY_PATTERNS = [
    r'^no[-_.]?reply', r'^do[-_.]?not[-_.]?reply', r'^notifications?$', r'^notify$',
    r'^mailer', r'^bounce', r'^auto', r'^postmaster', r'^system$', r'^alerts?$',
    r'^jobs$', r'^careers?$', r'^recruiting$', r'^recruitment$', r'^talent$', r'^hr$',
    r'^applications?$', r'^apply$', r'^support$', r'^info$', r'^hello$', r'^team$',
    r'^newsletter', r'^marketing$', r'^updates?$',
]
RELAY_DOMAINS = [
    'greenhouse-mail.io', 'greenhouse.io', 'lever.co', 'hire.lever.co',
    'myworkday.com', 'myworkdayjobs.com', 'ashbyhq.com', 'icims.com',
    'smartrecruiters.com', 'jobvite.com', 'bamboohr.com', 'successfactors.com',
    'silkroad.com', 'taleo.net', 'workablemail.com', 'breezy.hr', 'indeed.com',
    'linkedin.com', 'ziprecruiter.com', 'glassdoor.com',
]


def _profile() -> dict:
    try:
        return json.loads(PROFILE.read_text())
    except Exception:
        return {}


def creds() -> tuple[str, str]:
    """(sender address, app password). Password empty = cannot send."""
    prof = _profile()
    user = os.environ.get('GMAIL_IMAP_USER') or prof.get('email', '')
    return user, os.environ.get('GMAIL_APP_PASSWORD', '')


def parse_addr(header: str) -> tuple[str, str]:
    name, addr = parseaddr(header or '')
    return name.strip(), addr.strip().lower()


def is_human_address(addr: str) -> bool:
    """True when a reply to this address has a chance of being read."""
    addr = (addr or '').strip().lower()
    if '@' not in addr:
        return False
    local, domain = addr.rsplit('@', 1)
    if any(domain == d or domain.endswith('.' + d) for d in RELAY_DOMAINS):
        return False
    if any(re.search(p, local) for p in NOREPLY_PATTERNS):
        return False
    return True


def reply_target(msg_from: str, reply_to: str = '') -> str | None:
    """Best human address on an inbound mail, or None."""
    for header in (reply_to, msg_from):
        _, addr = parse_addr(header)
        if addr and is_human_address(addr):
            return addr
    return None


def clean_voice(text: str) -> str:
    """No em dashes leave this module. Ever."""
    return (text or '').replace('—', ',').replace('–', '-')


def _build(to: str, subject: str, body: str, in_reply_to: str | None = None,
           references: str | None = None) -> EmailMessage:
    user, _ = creds()
    msg = EmailMessage()
    msg['From'] = formataddr(('Sylvester Assiamah', user))
    msg['To'] = to
    msg['Subject'] = clean_voice(subject)
    msg['Message-ID'] = make_msgid(domain='gmail.com')
    if in_reply_to:
        msg['In-Reply-To'] = in_reply_to
        msg['References'] = references or in_reply_to
    msg.set_content(clean_voice(body))
    return msg


def send_mail(to: str, subject: str, body: str, in_reply_to: str | None = None,
              references: str | None = None, dry_run: bool = False,
              allow_nonhuman: bool = False) -> bool:
    """Send from 105@. Refuses no-reply targets unless allow_nonhuman (alerts
    to his own inbox use that)."""
    if not allow_nonhuman and not is_human_address(to):
        print(f'  [~] not sending to a no-reply address: {to}')
        return False
    user, pw = creds()
    if dry_run:
        print(f'  [dry] would send to {to}: {subject}')
        return True
    if not (user and pw):
        print('  [!] GMAIL_APP_PASSWORD unset: cannot send')
        return False
    msg = _build(to, subject, body, in_reply_to, references)
    with smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=30) as s:
        s.login(user, pw)
        s.send_message(msg)
    print(f'  [+] sent to {to}: {subject[:60]}')
    return True


def save_draft(to: str, subject: str, body: str, in_reply_to: str | None = None,
               references: str | None = None, dry_run: bool = False) -> bool:
    """Drop a ready-to-send draft into Gmail Drafts (IMAP APPEND)."""
    user, pw = creds()
    if dry_run:
        print(f'  [dry] would draft to {to or "(no address)"}: {subject}')
        return True
    if not (user and pw):
        print('  [!] GMAIL_APP_PASSWORD unset: cannot save draft')
        return False
    msg = _build(to or '', subject, body, in_reply_to, references)
    box = imaplib.IMAP4_SSL('imap.gmail.com', timeout=60)
    try:
        box.login(user, pw)
        box.append('[Gmail]/Drafts', '\\Draft', imaplib.Time2Internaldate(time.time()),
                   msg.as_bytes())
    finally:
        try:
            box.logout()
        except Exception:
            pass
    print(f'  [+] draft saved: {subject[:60]}')
    return True


def _state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {'sent': {}}


def alert(subject: str, body: str, key: str | None = None, dry_run: bool = False) -> bool:
    """Push to his personal inbox (profile.alert_email) once per key per day.
    Optional ntfy mirror. Returns True if a new alert went out."""
    prof = _profile()
    to = prof.get('alert_email') or prof.get('email')
    if not to:
        return False
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    k = f"{today}:{key or subject}"
    st = _state()
    if k in st.get('sent', {}):
        print(f'  [~] alert already sent today: {subject[:60]}')
        return False
    ok = send_mail(to, f'[scipio] {subject}', body, dry_run=dry_run, allow_nonhuman=True)
    topic = os.environ.get('NTFY_TOPIC')
    if topic and not dry_run:
        try:
            import httpx
            httpx.post(f'https://ntfy.sh/{topic}', content=clean_voice(body)[:3000].encode(),
                       headers={'Title': clean_voice(subject)[:120], 'Priority': 'high'},
                       timeout=15)
        except Exception as e:
            print(f'  [~] ntfy failed: {e}')
    if ok and not dry_run:
        st.setdefault('sent', {})[k] = datetime.now(timezone.utc).isoformat(timespec='minutes')
        # keep the file small: only today + yesterday
        st['sent'] = {kk: v for kk, v in st['sent'].items() if kk[:10] >= today[:8] + '01' or kk[:7] == today[:7]}
        STATE.write_text(json.dumps(st, indent=1))
    return ok


if __name__ == '__main__':
    import sys
    if '--test-alert' in sys.argv:
        alert('test alert', 'If you can read this on your phone, the alert channel works.',
              key=f'test-{int(time.time())}', dry_run='--dry-run' in sys.argv)
    else:
        for a in sys.argv[1:]:
            print(a, '->', 'human' if is_human_address(a) else 'no-reply')
