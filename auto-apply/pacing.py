"""Human-pace controls for the applier.

Everything here exists so scipio behaves like a person working through a
short list of applications, not a scraper: submits only during Eastern
business hours, minutes (not seconds) between applications, a per-board
daily cap, and small randomized pauses while filling. None of this hides
from anti-bot systems — when a board blocks automation (CAPTCHA), the job
is parked for manual review, never retried around the block.
"""
import random
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

# Eastern time without a tz database dependency: US DST = second Sunday of
# March to first Sunday of November.
def _eastern_now() -> datetime:
    utc = datetime.now(timezone.utc)
    year = utc.year
    def nth_sunday(month, n):
        d = datetime(year, month, 1, 7, tzinfo=timezone.utc)  # 2am ET ~ 7Z
        firsts = [d + timedelta(days=i) for i in range(31) if (d + timedelta(days=i)).month == month]
        sundays = [x for x in firsts if x.weekday() == 6]
        return sundays[n - 1]
    dst = nth_sunday(3, 2) <= utc < nth_sunday(11, 1)
    return utc + timedelta(hours=-4 if dst else -5)


# 8am floor, not 9: the 13:00 UTC cron lands at 9am ET in summer but 8am ET
# once EST returns — a 9am floor would silently skip every winter run.
BUSINESS_START = 8    # 8:00 ET
BUSINESS_END = 18     # 18:00 ET
BOARD_DAILY_CAP = 3   # max submit attempts per board per day


def within_business_hours() -> bool:
    now = _eastern_now()
    return BUSINESS_START <= now.hour < BUSINESS_END


def eastern_clock() -> str:
    return _eastern_now().strftime('%H:%M ET')


def start_jitter_seconds(max_minutes: int = 10) -> int:
    """A person doesn't sit down at 9:00:00 sharp every day."""
    return random.randint(30, max_minutes * 60)


def between_apps_seconds() -> int:
    """Minutes between applications, jittered (2.5–7 min)."""
    return random.randint(150, 420)


def human_pause() -> float:
    """Short think-pause between form sections (0.4–1.8s)."""
    return random.uniform(0.4, 1.8)


# real, current desktop Chrome fingerprints — variety, not disguise
UA_POOL = [
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36", {"width": 1280, "height": 900}),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36", {"width": 1440, "height": 900}),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36", {"width": 1366, "height": 768}),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", {"width": 1536, "height": 864}),
]


def pick_fingerprint() -> tuple[str, dict]:
    return random.choice(UA_POOL)


def _norm_url(url: str) -> str:
    """Same posting seen as an embed, a company vanity domain, or a gh_jid
    query is still the same posting. Key on the numeric job id when there is
    one, so hioscar.com/careers/8052062 and the greenhouse embed for token
    8052062 collapse to one entry."""
    if not url:
        return ''
    u = url.strip().lower().rstrip('/')
    ids = re.findall(r'(\d{6,})', u)
    if ids:
        return f"job:{max(ids, key=len)}"
    return u.split('?')[0]


def posting_age_days(job: dict) -> int | None:
    """Days since the posting went live, from whatever date the board gave us.
    None when the board did not publish one."""
    raw = (job.get('posted_date') or job.get('published_at')
           or job.get('updated_at') or '')
    if not raw:
        return None
    text = str(raw).replace('Z', '+00:00')
    for parse in (datetime.fromisoformat,
                  lambda s: datetime.strptime(s[:10], '%Y-%m-%d')):
        try:
            dt = parse(text)
        except (ValueError, TypeError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0, (datetime.now(timezone.utc) - dt).days)
    return None


def board_key(url: str) -> str:
    """Board identity for daily caps: greenhouse/lever slug, else hostname."""
    try:
        p = urlparse(url or '')
        host = (p.netloc or '').lower()
        parts = [x for x in (p.path or '').split('/') if x]
        if 'greenhouse.io' in host or 'lever.co' in host:
            return f"{host.split('.')[-2]}:{parts[0]}" if parts else host
        return host
    except Exception:
        return 'unknown'


def todays_attempts_per_board(logs: list) -> dict:
    """Count today's real submit attempts per board from apply_log entries."""
    today = _eastern_now().strftime('%Y-%m-%d')
    counts: dict[str, int] = {}
    for entry in logs:
        if not str(entry.get('timestamp', '')).startswith(today):
            continue
        if entry.get('status') in ('dry_run', 'no_form', 'bad_url', 'closed', 'needs_account'):
            continue
        key = board_key(entry.get('url', ''))
        counts[key] = counts.get(key, 0) + 1
    return counts


CAPTCHA_SELECTORS = (
    'iframe[src*="recaptcha"], iframe[src*="hcaptcha"], '
    'div.g-recaptcha, div.h-captcha, iframe[src*="turnstile"], '
    '[class*="cf-turnstile"], '
    # lever's new form throws an Arkose "place the puzzle piece" modal
    'iframe[src*="arkoselabs"], iframe[src*="funcaptcha"]'
)


async def captcha_present(page) -> bool:
    """A visible CAPTCHA CHALLENGE means this board wants a human — we stop,
    we do not work around it. But the invisible score-based reCAPTCHA badge
    greenhouse renders on ~every job-board form is NOT a challenge: counting
    it parked fully-filled applications as manual_review for weeks (8/21-8/23
    screenshots show no puzzle, just the corner badge + required-field
    errors) and short-circuited the repair-and-resubmit sweep that would
    have fixed those fields."""
    try:
        loc = page.locator(CAPTCHA_SELECTORS)
        n = await loc.count()
        for i in range(min(n, 5)):
            el = loc.nth(i)
            if not await el.is_visible():
                continue
            src = (await el.get_attribute('src')) or ''
            if 'size=invisible' in src:
                continue  # invisible-mode widget: no puzzle, not a gate
            if await el.evaluate("el => !!el.closest('.grecaptcha-badge')"):
                continue  # the fixed corner badge container, same thing
            if await el.evaluate(
                    "el => (el.closest('.g-recaptcha,.h-captcha')?.dataset?.size || '') === 'invisible'"):
                continue
            return True
        # ashby's anti-bot doesn't render a captcha — it refuses the submit
        # with a spam banner; treat it the same way (human takes over)
        banner = page.get_by_text('flagged as possible spam').first
        if await banner.is_visible(timeout=1000):
            return True
    except Exception:
        pass
    return False
