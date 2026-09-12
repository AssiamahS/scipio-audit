#!/usr/bin/env python3
"""Workday (myworkdayjobs.com) end to end, no human: account, sign-in,
password reset, the multi-step form, submit, confirmation.

Every Workday tenant hides the form behind an account. The account email is
the profile email; the password is derived per tenant from GMAIL_APP_PASSWORD
(HMAC), so nothing is stored anywhere and the same box can always log back
in. Verification links and reset links arrive in the 105 inbox and are read
over IMAP the way Greenhouse security codes already are.

Form pages are identified by the progress bar (progressBarActiveStep) and
filled by data-automation-id, which Workday keeps stable across tenants:
My Information, My Experience (resume upload + one work block + education),
Application Questions (dropdown/radio/text via the same answer canon as the
other boards), Voluntary Disclosures (decline), Self Identify (decline),
Review (Submit). `beecatcher` is a honeypot text field: never touched.

  WD_DEBUG=1 dumps automation ids + a screenshot per step to /tmp.
"""
import asyncio
import base64
import email as email_mod
import hashlib
import hmac
import imaplib
import os
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

from ats_handlers import _desired_options, _profile_value_for, qa_note, STATE_NAMES

NEXT = '[data-automation-id="bottom-navigation-next-button"], [data-automation-id="pageFooterNextButton"]'
ERRORS = '[data-automation-id="errorMessage"]'
ACTIVE_STEP = '[data-automation-id="progressBarActiveStep"]'
CONFIRM_RX = re.compile(r"successfully applied|application has been (received|submitted)|thank you for applying|"
                        r"you have applied|we have received your application|congratulations", re.I)
DECLINE_RX = re.compile(r"decline|do not wish|don't wish|prefer not|not to answer|do not want", re.I)
WD_DEBUG = bool(os.environ.get('WD_DEBUG'))


def tenant_of(url: str) -> str:
    host = urlsplit(url).netloc.lower()
    return host.split('.')[0] if 'myworkdayjobs.com' in host else ''


def derive_password(tenant: str) -> str:
    """Deterministic per-tenant password that meets Workday's rules
    (8+ chars, upper, lower, digit, special). Seed = GMAIL_APP_PASSWORD or
    WORKDAY_SEED; with neither we still return something stable-ish so a
    dry run can exercise the flow."""
    seed = os.environ.get('WORKDAY_SEED') or os.environ.get('GMAIL_APP_PASSWORD') or 'scipio-dev-seed'
    digest = hmac.new(seed.encode(), f'workday:{tenant}'.encode(), hashlib.sha256).digest()
    body = base64.b64encode(digest).decode().replace('+', 'x').replace('/', 'y').replace('=', '')[:12]
    return f'Wd!{body}9a'


async def _text(page, sel: str, timeout: int = 1500) -> str:
    try:
        return (await page.locator(sel).first.inner_text(timeout=timeout)).strip()
    except Exception:
        return ''


async def step_name(page) -> str:
    t = await _text(page, ACTIVE_STEP)
    return re.sub(r'current step \d+ of \d+', '', t, flags=re.I).strip().lower()


async def errors(page) -> list[str]:
    out = []
    try:
        for e in await page.locator(f'{ERRORS}, [data-automation-id="alertMessage"], [role="alert"]').all():
            t = (await e.inner_text()).strip()
            if t and not re.search(r'success|uploaded|saved|job alerts', t, re.I):
                out.append(t[:140])
    except Exception:
        pass
    return out


async def debug(page, tag: str):
    if not WD_DEBUG:
        return
    try:
        els = await page.evaluate(
            """Array.from(document.querySelectorAll('input, textarea, select, button[aria-haspopup], [data-automation-id="click_filter"], [data-automation-id^="formField-"], [data-automation-id*="Section"], [data-automation-id*="Button"], [data-automation-id="errorMessage"]')).map(e=>{const lab=(e.id&&document.querySelector('label[for="'+e.id+'"]')?.innerText)||e.getAttribute('aria-label')||'';return (e.getAttribute('data-automation-id')||'-')+' <'+e.tagName+' type='+(e.getAttribute('type')||'')+' id='+(e.id||'').slice(0,34)+'> '+lab.trim().slice(0,50)+' | '+((e.value||e.innerText||'').trim().slice(0,24))})""")
        print(f'  [wd:{tag}] {len(els)} controls')
        for e in els[:90]:
            print('       ', e)
        await page.screenshot(path=f'/tmp/wd_{tag}.png', full_page=True)
    except Exception as e:
        print(f'  [wd:{tag}] debug failed: {e}')


async def fill(page, sel: str, value: str) -> bool:
    if not value:
        return False
    try:
        loc = page.locator(sel).first
        if not await loc.is_visible(timeout=1500):
            return False
        cur = await loc.input_value()
        if cur and cur.strip():
            return True
        await loc.click()
        await loc.fill(value)
        return True
    except Exception:
        return False


async def choose(page, button_sel: str, wants: list[str], fallback_first: bool = False) -> str:
    """Open a Workday listbox button and pick the first option whose text
    matches any of `wants` (case-insensitive substring). Returns the text."""
    try:
        btn = page.locator(button_sel).first
        if not await btn.is_visible(timeout=1500):
            return ''
        cur = (await btn.inner_text()).strip().lower()
        if cur and cur not in ('select one', 'select', '') and any(w.lower() in cur for w in wants):
            return cur
        await btn.click()
        await asyncio.sleep(0.7)
        opts = page.locator('[role="option"]:visible, [data-automation-id="promptOption"]:visible, '
                            '[data-automation-id="promptLeafNode"]:visible')
        n = await opts.count()
        texts = [(await opts.nth(i).inner_text()).strip() for i in range(min(n, 60))]
        pick = next((i for i, t in enumerate(texts) if any(w.lower() in t.lower() for w in wants)), None)
        if pick is None and fallback_first and texts:
            pick = next((i for i, t in enumerate(texts) if t and t.lower() not in ('select one',)), None)
        if pick is None:
            await page.keyboard.press('Escape')
            return ''
        await opts.nth(pick).click()
        await asyncio.sleep(0.5)
        return texts[pick]
    except Exception:
        try:
            await page.keyboard.press('Escape')
        except Exception:
            pass
        return ''


async def type_prompt(page, container_sel: str, value: str) -> bool:
    """Multiselect search prompts (source, skills, field of study): type and
    take the first suggestion."""
    try:
        box = page.locator(f'{container_sel} input').first
        if not await box.is_visible(timeout=1500):
            return False
        await box.click()
        await box.fill(value)
        await asyncio.sleep(1.2)
        await page.keyboard.press('Enter')
        await asyncio.sleep(1.0)
        opt = page.locator('[data-automation-id="promptOption"]:visible, [role="option"]:visible').first
        if await opt.count():
            await opt.click()
            await asyncio.sleep(0.5)
        await page.keyboard.press('Escape')
        return True
    except Exception:
        return False


async def set_radio(page, radio) -> None:
    """Workday radios are custom-painted: check() on the input does not flip
    them. Click the label, then the input, then force it through the DOM."""
    try:
        rid = await radio.get_attribute('id')
        if rid:
            lab = page.locator(f'label[for="{rid}"]').first
            if await lab.count():
                await lab.click()
                await asyncio.sleep(0.4)
                if await radio.is_checked():
                    return
        await radio.click(force=True)
        await asyncio.sleep(0.4)
        if await radio.is_checked():
            return
        await radio.evaluate("r => { r.click(); r.checked = true; r.dispatchEvent(new Event('change', {bubbles: true})); }")
    except Exception:
        pass


async def check(page, sel: str) -> bool:
    try:
        box = page.locator(sel).first
        if await box.is_visible(timeout=1000) and not await box.is_checked():
            await box.check(force=True)
        return True
    except Exception:
        return False


async def press_submit(page, texts: tuple, last_field_sel: str | None = None) -> bool:
    """Workday renders an aria-hidden decoy submit (tabindex=-2) next to the
    real one; click the VISIBLE button by its text, else press Enter in the
    last field."""
    # the account forms carry ONE click_filter overlay (aria-label "Submit",
    # not the visible text) inside the <form> that holds the email field
    if last_field_sel:
        try:
            inform = page.locator(f'form:has({last_field_sel}) [data-automation-id="click_filter"]').last
            if await inform.count() and await inform.is_visible(timeout=800):
                await inform.click(timeout=5000)
                return True
        except Exception:
            pass
    # scope to the modal when the form lives in one (CVS): the header has its
    # own "Sign In" button and an unscoped click lands there
    scopes = ['[data-automation-id="popUpDialog"] ', '[role="dialog"] ', '']
    for scope in scopes:
        if scope and not await page.locator(scope.strip()).count():
            continue
        for t in texts:
            # the real click target is a div[role=button] "click_filter" overlay
            # with the label; the <button> under it is aria-hidden and intercepted
            for sel in (f'{scope}form [data-automation-id="click_filter"][aria-label="{t}"]',
                        f'{scope}[data-automation-id="click_filter"][aria-label="{t}"]',
                        f'{scope}form [role="button"][aria-label="{t}"]:visible',
                        f'{scope}[role="button"][aria-label="{t}"]:visible',
                        f'{scope}button:has-text("{t}"):visible:not([aria-hidden="true"])'):
                try:
                    cands = page.locator(sel)
                    n = await cands.count()
                    if not n:
                        continue
                    # the page header carries its own "Sign In": when several
                    # match, take the lowest one on the page (the form's)
                    best, best_y = None, -1
                    for i in range(n):
                        c = cands.nth(i)
                        try:
                            if not await c.is_visible(timeout=500):
                                continue
                            box = await c.bounding_box()
                        except Exception:
                            continue
                        if box and box['y'] > best_y:
                            best, best_y = c, box['y']
                    if best is None:
                        continue
                    await best.click(timeout=5000)
                    return True
                except Exception:
                    continue
        if scope:
            break   # a modal exists but had no such button: do not fall through to the header
    if last_field_sel:
        try:
            await page.locator(last_field_sel).first.press('Enter')
            return True
        except Exception:
            pass
    return False


async def click_next(page) -> None:
    """Save and Continue / Submit. The first click right after the last fill
    gets swallowed by the field's blur re-render (9/7 trace: no request was
    fired), so blur first, click, and confirm the step actually changed;
    retry twice before giving up. Validation errors end the retries early."""
    prev = await step_name(page)
    prev_url = page.url
    await close_popups(page)
    try:
        await page.mouse.click(5, 300)   # blur whatever field was last typed into
    except Exception:
        pass
    await asyncio.sleep(1.0)
    for attempt in range(3):
        btn = page.locator(NEXT).first
        try:
            await btn.scroll_into_view_if_needed()
            await btn.click(timeout=5000)
        except Exception:
            overlay = page.locator('[data-automation-id="click_filter"][aria-label*="Next" i], '
                                   '[data-automation-id="click_filter"][aria-label*="Submit" i], '
                                   '[data-automation-id="click_filter"][aria-label*="Save" i]').first
            try:
                if await overlay.count():
                    await overlay.click(timeout=5000)
                else:
                    await btn.click(force=True, timeout=5000)
            except Exception:
                pass
        await asyncio.sleep(3)
        try:
            await page.wait_for_load_state('networkidle', timeout=15000)
        except Exception:
            pass
        if await errors(page):
            return
        if (await step_name(page)) != prev or page.url != prev_url:
            return
        await asyncio.sleep(2)


# ----------------------------------------------------------------- inbox

def _imap_messages(minutes: int = 20, from_hint: str = 'workday'):
    """Recent messages (subject, body, date) from the Workday tenant mailers,
    newest first. Same three-folder rule as the Greenhouse code reader."""
    user = os.environ.get('GMAIL_IMAP_USER')
    pw = os.environ.get('GMAIL_APP_PASSWORD')
    if not pw:
        return []
    if not user:
        import json
        user = json.loads((Path(__file__).parent / 'profile.json').read_text())['email']
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    out = []
    try:
        M = imaplib.IMAP4_SSL('imap.gmail.com', timeout=60)
        M.login(user, pw)
        for box in ('INBOX', '"[Gmail]/Trash"', '"[Gmail]/Spam"'):
            try:
                M.select(box, readonly=True)
                typ, data = M.search(None, f'(FROM "{from_hint}")')
            except Exception:
                continue
            for mid in data[0].split()[-8:]:
                try:
                    typ, msg_data = M.fetch(mid, '(BODY.PEEK[])')
                    msg = email_mod.message_from_bytes(msg_data[0][1])
                    dt = parsedate_to_datetime(msg['Date'])
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    if dt < cutoff:
                        continue
                    body = ''
                    for part in msg.walk():
                        if part.get_content_type() in ('text/plain', 'text/html'):
                            body += part.get_payload(decode=True).decode(errors='ignore')
                    subj = str(email_mod.header.make_header(email_mod.header.decode_header(msg.get('Subject', ''))))
                    out.append((dt, subj, body))
                except Exception:
                    continue
        M.logout()
    except Exception as e:
        print(f'  [!] workday imap: {e}')
    out.sort(key=lambda x: x[0], reverse=True)
    return out


async def wait_for_link(tenant: str, pattern: str, timeout: int = 240) -> str | None:
    """Poll the inbox for a Workday link (verify / reset) for this tenant."""
    if not os.environ.get('GMAIL_APP_PASSWORD'):
        print('  [~] no GMAIL_APP_PASSWORD: cannot read the inbox for the Workday link')
        return None
    rx = re.compile(pattern, re.I)
    start = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - start < timeout:
        for dt, subj, body in _imap_messages():
            if tenant.lower() not in body.lower() and tenant.lower() not in subj.lower():
                continue
            m = rx.search(body.replace('&amp;', '&'))
            if m:
                return m.group(0)
        await asyncio.sleep(12)
    return None


async def wait_for_code(tenant: str, timeout: int = 240) -> str | None:
    if not os.environ.get('GMAIL_APP_PASSWORD'):
        return None
    start = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - start < timeout:
        for dt, subj, body in _imap_messages():
            if tenant.lower() not in body.lower() and tenant.lower() not in subj.lower():
                continue
            text = re.sub(r'<[^>]+>', ' ', body)
            m = re.search(r'\b(\d{6})\b', text)
            if m:
                return m.group(1)
        await asyncio.sleep(12)
    return None


# --------------------------------------------------------------- account

async def ensure_account(page, profile: dict, tenant: str) -> str:
    """Land on step 2 with a signed-in candidate. Returns 'created',
    'signed_in', 'reset', or an 'error: ...' string."""
    email = profile['email']
    pw = derive_password(tenant)
    await asyncio.sleep(2)

    async def past_account() -> bool:
        # signed in = the header shows the account menu, or the progress bar
        # moved past step 1, or the footer's Save and Continue is up. Poll:
        # the bar lags the auth response by a few seconds.
        for _ in range(6):
            if await page.locator('[data-automation-id="utilityButtonAccountTasksMenu"], '
                                  '[data-automation-id="pageFooterNextButton"], '
                                  '[data-automation-id="bottom-navigation-next-button"]').count():
                return True
            s = await step_name(page)
            if s and 'create account' not in s and 'sign in' not in s:
                return True
            await asyncio.sleep(2)
        return False

    async def dismiss_banner():
        for sel in ('[data-automation-id="bannerCloseButton"]', 'button:has-text("Accept"):visible',
                    'button:has-text("I Accept"):visible', 'button[aria-label="Close"]:visible'):
            try:
                b = page.locator(sel).first
                if await b.is_visible(timeout=600):
                    await b.click()
                    await asyncio.sleep(0.8)
            except Exception:
                continue

    async def open_email_form() -> bool:
        """Get to a page with the email field: through the SSO chooser
        (CVS: Google / LinkedIn / email) when there is one."""
        for _ in range(4):
            if await page.locator('[data-automation-id="email"]').count():
                return True
            sso = page.locator('[data-automation-id="SignInWithEmailButton"], button:has-text("Sign in with email")').first
            try:
                if await sso.is_visible(timeout=1500):
                    await dismiss_banner()
                    await sso.click()
                    await asyncio.sleep(3)
                    continue
            except Exception:
                pass
            await asyncio.sleep(2)
        return bool(await page.locator('[data-automation-id="email"]').count())

    async def do_signin() -> bool:
        if not await page.locator('[data-automation-id="signInSubmitButton"]').count():
            link = page.locator('[data-automation-id="signInLink"]').first
            if await link.is_visible(timeout=1500):
                await link.click()
                await asyncio.sleep(2)
        if not await fill(page, '[data-automation-id="email"]', email):
            return False
        await fill(page, '[data-automation-id="password"]', pw)
        await press_submit(page, ('Sign In',), '[data-automation-id="password"]')
        await asyncio.sleep(4)
        return await past_account()

    async def do_create() -> bool:
        if not await page.locator('[data-automation-id="createAccountSubmitButton"]').count():
            link = page.locator('[data-automation-id="createAccountLink"]').first
            if await link.is_visible(timeout=1500):
                await link.click()
                await asyncio.sleep(2)
        if not await fill(page, '[data-automation-id="email"]', email):
            return False
        await fill(page, '[data-automation-id="password"]', pw)
        await fill(page, '[data-automation-id="verifyPassword"]', pw)
        await check(page, '[data-automation-id="createAccountCheckbox"]')
        await check(page, 'input[type="checkbox"]:visible')
        await press_submit(page, ('Create Account',), '[data-automation-id="verifyPassword"]')
        await asyncio.sleep(4)
        if await past_account():
            return True
        body = (await _text(page, 'body', 3000)).lower()
        if re.search(r'verif(y|ication) (your )?email|verification code|check your (inbox|email)|sent (you )?an? (email|link)', body):
            print('  [*] Workday wants email verification: reading the inbox')
            link = await wait_for_link(tenant, r'https://[^\s"\'<>]*myworkday[^\s"\'<>]*(verif|activ)[^\s"\'<>]*')
            if link:
                await page.goto(link, wait_until='domcontentloaded', timeout=60000)
                await asyncio.sleep(3)
            else:
                code = await wait_for_code(tenant)
                if code and await fill(page, 'input[type="text"]:visible', code):
                    await page.keyboard.press('Enter')
                    await asyncio.sleep(3)
        return await past_account()

    async def do_reset() -> bool:
        fp = page.locator('[data-automation-id="forgotPasswordLink"]').first
        if not await fp.is_visible(timeout=1500):
            return False
        await fp.click()
        await asyncio.sleep(2)
        await fill(page, '[data-automation-id="email"]', email)
        await press_submit(page, ('Submit', 'Send', 'Reset', 'Continue'), '[data-automation-id="email"]')
        link = await wait_for_link(tenant, r'https://[^\s"\'<>]*myworkday[^\s"\'<>]*(reset|password|resetPassword)[^\s"\'<>]*')
        if not link:
            return False
        await page.goto(link, wait_until='domcontentloaded', timeout=60000)
        await asyncio.sleep(3)
        await fill(page, '[data-automation-id="password"], input[type="password"]', pw)
        await fill(page, '[data-automation-id="verifyPassword"]', pw)
        await press_submit(page, ('Submit', 'Reset Password', 'Save', 'Continue'), '[data-automation-id="verifyPassword"]')
        await asyncio.sleep(3)
        return await do_signin()

    if await past_account():
        return 'signed_in'   # persistent profile remembered the session
    await dismiss_banner()
    if not await open_email_form():
        await debug(page, 'no_email_form')
        return 'error: no sign-in form rendered'

    create_mode = bool(await page.locator('[data-automation-id="createAccountSubmitButton"]').count())
    if create_mode:
        if await do_create():
            print(f'  [+] Workday account created for {tenant}')
            return 'created'
        errs = ' | '.join(await errors(page)).lower()
        await debug(page, 'after_create')
        print(f'  [~] create did not advance ({errs[:100] or "no error text"}); trying sign-in')
        if await do_signin():
            print(f'  [+] Signed in to {tenant}')
            return 'signed_in'
    else:
        if await do_signin():
            print(f'  [+] Signed in to {tenant}')
            return 'signed_in'
        errs = ' | '.join(await errors(page)).lower()
        await debug(page, 'after_signin')
        print(f'  [~] sign-in did not advance ({errs[:100] or "no error text"}); trying create')
        if await do_create():
            print(f'  [+] Workday account created for {tenant}')
            return 'created'
    print('  [~] password rejected: resetting via email')
    if await do_reset():
        print(f'  [+] Password reset and signed in to {tenant}')
        return 'reset'
    await debug(page, 'account_failed')
    return f"error: account flow failed: {' | '.join(await errors(page))[:140]}"


# ------------------------------------------------------------------ steps

SKIP_LABELS = ('address line 2', 'extension', 'middle name', 'preferred name', 'i have a preferred',
               'consent to receive', 'text messag', 'suffix', 'prefix')
DECLINE_KEYS = ('gender', 'ethnic', 'race', 'veteran', 'disab', 'hispanic', 'sexual orientation')


def wd_value(label: str, profile: dict):
    """Workday label -> (kind, value). kind: text | choose | prompt | radio | skip."""
    l = (label or '').lower()
    if len(l) < 45 and any(k in l for k in SKIP_LABELS):
        return ('skip', None)   # field labels only; a question mentioning "extensions" is not a skip
    if 'hear about' in l or 'how did you' in l or l.startswith('source'):
        return ('prompt', 'LinkedIn')
    if 'previously' in l or 'previous worker' in l or ('ever been employed' in l) or ('worked' in l and 'before' in l):
        return ('radio', ['no'])
    if l.strip() in ('name', 'full name', 'your name', 'signature'):
        return ('text', f"{profile['first_name']} {profile['last_name']}")
    if l.startswith('first name') or l == 'first name*' or 'legal first' in l:
        return ('text', profile['first_name'])
    if l.startswith('last name') or 'legal last' in l:
        return ('text', profile['last_name'])
    if 'address line 1' in l or l.startswith('address') and 'line' in l:
        return ('text', profile.get('address1', ''))
    if l.startswith('city'):
        return ('text', profile.get('city', ''))
    if l.startswith('state') or 'province' in l or 'region' in l:
        return ('choose', [STATE_NAMES.get((profile.get('state') or '').upper(), profile.get('state', ''))])
    if 'postal' in l or 'zip' in l:
        return ('text', profile.get('zip', ''))
    if 'country phone' in l or 'phone code' in l:
        return ('prompt', 'United States of America')
    if 'phone device' in l or 'phone type' in l:
        return ('choose', ['Mobile', 'Cell'])
    if 'phone' in l:
        return ('text', re.sub(r'\D', '', profile.get('phone', '')))
    if l.startswith('country'):
        return ('choose', ['United States of America', 'United States'])
    if 'email' in l:
        return ('text', profile['email'])
    if 'linkedin' in l:
        return ('text', profile.get('linkedin', ''))
    if 'website' in l or 'portfolio' in l or 'github' in l:
        return ('text', profile.get('github', '') or profile.get('linkedin', ''))
    if any(k in l for k in DECLINE_KEYS):
        return ('choose', ['decline', 'do not wish', "don't wish", 'prefer not', 'not to answer', 'i do not', 'choose not'])
    if 'pay expectation' in l or 'salary expectation' in l or 'compensation expectation' in l or 'desired salary' in l:
        return ('salary', str(profile.get('desired_salary', '')))
    # screening yes/no canon for the questions every big employer asks
    NO = ('currently work at', 'currently employed by', 'ever been employed', 'ever worked for', 'contingent worker',
          'spouse or partner', 'family member', 'related to', 'debarred', 'excluded from', 'sanction', 'disciplinary',
          'convicted', 'license revoked', 'non-compete', 'noncompete', 'conflict of interest', 'government official',
          'need employment sponsorship', 'need employer', 'need sponsorship', 'require sponsorship', 'visa sponsorship')
    YES = ('i authorize', 'i acknowledge', 'i agree', 'i understand', 'i certify', 'i consent', 'can you provide proof',
           'at least 18', 'willing to', 'able to perform', 'able to work', 'background check', 'drug screen', 'drug test',
           'abide by', 'comply with', 'legally authorized', 'authorized to work', 'eligible to work')
    if any(k in l for k in NO):
        return ('choose', ['no'])
    if any(k in l for k in YES):
        return ('choose', ['yes', 'i acknowledge', 'i agree', 'accept'])
    return None


def pick_salary_option(texts: list[str], desired: str) -> int | None:
    """Pay-expectation dropdowns are ranges ("$60,000 - $80,000", "Under
    $20,000", "$220,000+"). Take the range containing the desired figure,
    else the lowest range above it, else the second option."""
    try:
        want = int(re.sub(r'[^0-9]', '', desired or '') or 0)
    except ValueError:
        want = 0
    ranges = []
    for i, t in enumerate(texts):
        tl = t.lower()
        nums = [int(n.replace(',', '')) for n in re.findall(r'\d[\d,]{2,}', t)]
        nums = [n * 1000 if n < 1000 else n for n in nums]
        if not nums or 'decline' in tl or 'select' in tl:
            continue
        if len(nums) >= 2:
            lo, hi = min(nums), max(nums)
        elif re.search(r'under|less than|below|up to', tl):
            lo, hi = 0, nums[0]
        elif re.search(r'over|above|more than|\+|or more|greater', tl):
            lo, hi = nums[0], 10 ** 9
        else:
            lo, hi = nums[0], nums[0]
        ranges.append((i, lo, hi))
    if not ranges:
        return None
    if want:
        inside = [r for r in ranges if r[1] <= want <= r[2]]
        if inside:
            # a figure on a boundary ("$60,000") belongs to the range it opens
            return max(inside, key=lambda r: r[1])[0]
        above = [r for r in ranges if r[1] >= want]
        if above:
            return min(above, key=lambda r: r[1])[0]
    return ranges[min(1, len(ranges) - 1)][0]


async def field_label(fld) -> str:
    try:
        if await fld.locator('input[type="radio"]').count():
            # radio groups: the question is the block's first text line; the
            # first <label> is just the "Yes" option
            txt = (await fld.inner_text(timeout=800)).strip()
            first = next((ln.strip() for ln in txt.splitlines() if ln.strip() and ln.strip().lower() not in ('yes', 'no')), '')
            if first:
                return re.sub(r'\s*\*\s*$', '', first).strip()
        t = (await fld.locator('label, legend').first.inner_text(timeout=600)).strip()
    except Exception:
        t = ''
    if not t:
        try:
            t = (await fld.locator('[aria-label]').first.get_attribute('aria-label')) or ''
        except Exception:
            t = ''
    return re.sub(r'\s*\*\s*$', '', t).strip()


async def pick_open_option(page, wants: list[str], fallback_first: bool = False):
    await asyncio.sleep(0.8)
    opts = page.locator('[role="option"]:visible, [data-automation-id="promptOption"]:visible, '
                        '[data-automation-id="promptLeafNode"]:visible, li[role="presentation"]:visible')
    n = await opts.count()
    texts = [(await opts.nth(i).inner_text()).strip() for i in range(min(n, 80))]
    pick = next((i for i, t in enumerate(texts) if any(w and w.lower() in t.lower() for w in wants)), None)
    if pick is None and fallback_first:
        pick = next((i for i, t in enumerate(texts) if t and t.lower() not in ('select one',)), None)
    if pick is None:
        return None, texts
    await opts.nth(pick).click()
    await asyncio.sleep(0.6)
    return texts[pick], texts


PROMPT_CATEGORIES = ('job board', 'job boards', 'social media', 'social', 'online', 'internet', 'website', 'other')


async def pick_prompt(page, fld, inp, wants: list[str]) -> str:
    """Workday multiselect prompt (source, field of study, skills). The menu is
    hierarchical and every row, category or leaf, looks the same
    (promptOption inside promptLeafNode with data-automation-checked), so the
    only reliable signal is the field text flipping to "1 item selected".
    Click a row matching wants; if nothing got selected it was a category,
    so the (now deeper) list is scanned again. Then category names, then
    typing the first want, then the first row that actually selects."""
    OPT = '[data-automation-id="promptOption"]:visible:not([data-automation-id="selectedItem"] *)'

    async def selected() -> bool:
        try:
            return bool(re.search(r'\b[1-9]\d* items? selected', (await fld.inner_text(timeout=800)).lower()))
        except Exception:
            return False

    async def rows():
        loc = page.locator(OPT)
        n = await loc.count()
        return loc, [(await loc.nth(i).inner_text()).strip() for i in range(min(n, 80))]

    async def try_click(loc, i, t) -> bool:
        await loc.nth(i).click()
        await asyncio.sleep(1.0)
        return await selected()

    await inp.click()
    await asyncio.sleep(1.0)
    tried = set()
    for attempt in range(4):
        loc, texts = await rows()
        if not texts:
            break
        # 1. a row matching what we want
        for i, t in enumerate(texts):
            if t and t not in tried and any(w and w.lower() in t.lower() for w in wants):
                tried.add(t)
                if await try_click(loc, i, t):
                    return t
                break   # it was a category: list changed, rescan
        else:
            # 2. a category we know holds job-board style answers
            cat = next((i for i, t in enumerate(texts) if t and t not in tried
                        and any(c in t.lower() for c in PROMPT_CATEGORIES)), None)
            if cat is not None:
                tried.add(texts[cat])
                if await try_click(loc, cat, texts[cat]):
                    return texts[cat]
                continue
            # 3. type the first want (searchable prompts: field of study, skills)
            if attempt == 0 and wants and wants[0]:
                try:
                    await inp.fill(wants[0])
                    await asyncio.sleep(1.2)
                    await page.keyboard.press('Enter')
                    await asyncio.sleep(1.2)
                    continue
                except Exception:
                    pass
            break
    # 4. first row that actually selects
    loc, texts = await rows()
    for i, t in enumerate(texts[:6]):
        if t and t not in tried:
            if await try_click(loc, i, t):
                return t
            loc, texts = await rows()
    await page.keyboard.press('Escape')
    return ''


async def close_popups(page) -> None:
    """A prompt menu left open swallows the next clicks (the radio after the
    source prompt never got set). Escape, then a click on empty page margin."""
    for _ in range(2):
        if not await page.locator('[data-automation-id="promptLeafNode"]:visible, [role="listbox"]:visible').count():
            return
        await page.keyboard.press('Escape')
        await asyncio.sleep(0.4)
    try:
        await page.mouse.click(5, 300)
        await asyncio.sleep(0.4)
    except Exception:
        pass


async def answer_form_fields(page, profile: dict, job_info: dict | None, result: dict, ai_answer=None):
    """One pass over every formField-* block on the page. Workday wraps each
    control in <div data-automation-id="formField-<name>"> with a <label>;
    the control is a listbox button, a search prompt input (no type attr),
    a radio group, a checkbox, or a text input/textarea."""
    f = result['filled']
    fields = await page.locator('[data-automation-id^="formField-"]').all()
    for fld in fields:
        try:
            label = await field_label(fld)
            if not label or 'robots' in label.lower():
                continue
            ll = label.lower()
            spec = wd_value(label, profile)
            if spec and spec[0] == 'skip':
                continue
            await close_popups(page)
            # --- radio group
            radios = fld.locator('input[type="radio"]')
            if await radios.count():
                if any([await r.is_checked() for r in await radios.all()]):
                    continue
                wants = (spec[1] if spec and spec[0] == 'radio' else None) or _desired_options(label) or []
                if any(k in ll for k in DECLINE_KEYS):
                    wants = ['decline', 'do not wish', "don't wish", 'prefer not', 'not to answer', 'i do not', 'choose not']
                texts = []
                for r in await radios.all():
                    texts.append((await r.evaluate("o => (o.id && document.querySelector(`label[for=\"${o.id}\"]`)?.innerText) || o.closest('label')?.innerText || ''")).strip())
                pick = next((i for i, t in enumerate(texts) if any(w.lower() in t.lower() for w in wants)), None)
                if pick is None and ai_answer and len(label) > 20:
                    guess = (ai_answer(f"{label} Options: {' | '.join(texts)}") or '').lower()
                    pick = next((i for i, t in enumerate(texts) if t.lower() and (t.lower() in guess or guess in t.lower())), None)
                if pick is None:
                    result['missed'].append(label[:60]); continue
                await set_radio(page, radios.nth(pick))
                qa_note('radio', label, texts[pick], 'answer-table'); f.append(f'radio:{label[:40]}')
                continue
            # --- listbox dropdown
            btn = fld.locator('button[aria-haspopup="listbox"]').first
            if await btn.count() and await btn.is_visible(timeout=400):
                cur = (await btn.inner_text()).strip().lower()
                wants = (list(spec[1]) if spec and isinstance(spec[1], list) else None) or _desired_options(label)
                if not wants and not (spec and spec[0] == 'salary'):
                    v = _profile_value_for(label, profile)
                    wants = [v] if v else []
                wants = wants or []
                is_salary = bool(spec and spec[0] == 'salary')
                if cur and cur not in ('select one', 'select', 'select one required', '') \
                        and not is_salary and (not wants or any(w.lower() in cur for w in wants)):
                    continue
                await close_popups(page)
                await btn.scroll_into_view_if_needed()
                await btn.click()
                texts = []
                for _try in range(3):   # options render a beat after the click
                    await asyncio.sleep(0.9)
                    opts = page.locator('[role="option"]:visible, [data-automation-id="promptOption"]:visible')
                    n = await opts.count()
                    texts = [(await opts.nth(i).inner_text()).strip() for i in range(min(n, 80))]
                    if texts:
                        break
                    await btn.click()
                pick = None
                if is_salary:
                    pick = pick_salary_option(texts, spec[1])
                    if pick is not None and texts[pick].strip().lower() == cur:
                        await page.keyboard.press('Escape')
                        continue   # saved answer already right
                if pick is None:
                    pick = next((i for i, t in enumerate(texts) if any(w and w.lower() in t.lower() for w in wants)), None)
                if pick is None and ai_answer and texts and len(label) > 12:
                    guess = (ai_answer(f"{label} Options: {' | '.join(texts[:15])}") or '').strip().lower()
                    pick = next((i for i, t in enumerate(texts) if t.lower() == guess), None)
                    if pick is None and guess:
                        pick = next((i for i, t in enumerate(texts) if t.lower() in guess or guess in t.lower()), None)
                if pick is None:
                    await page.keyboard.press('Escape')
                    if 'select' in cur or not cur:
                        result['missed'].append(label[:60])
                    continue
                await opts.nth(pick).click()
                await asyncio.sleep(0.6)
                qa_note('dropdown', label, texts[pick], 'answer-table'); f.append(f'dd:{label[:40]}')
                continue
            # --- checkbox
            cb = fld.locator('input[type="checkbox"]')
            if await cb.count():
                if any(k in ll for k in ('agree', 'acknowledge', 'certify', 'consent to the', 'terms', 'accept', 'confirm')) \
                        and 'text messag' not in ll:
                    if not await cb.first.is_checked():
                        await cb.first.check(force=True); f.append(f'cb:{label[:40]}')
                continue
            # --- text / search prompt
            inp = fld.locator('input:not([type="hidden"]), textarea').first
            if not await inp.count():
                continue
            if (await inp.get_attribute('data-automation-id') or '') == 'beecatcher':
                continue
            itype = (await inp.get_attribute('type')) or ''
            try:
                if (await inp.input_value()).strip():
                    continue
            except Exception:
                pass
            is_prompt = itype == '' and await inp.evaluate("e => e.tagName === 'INPUT'")
            if is_prompt:
                ftxt = (await fld.inner_text(timeout=800)).lower()
                if re.search(r'\b[1-9]\d* items? selected', ftxt) or await fld.locator('[data-automation-id="selectedItem"]').count():
                    continue   # already has a selection (phone code defaults to +1; a saved source)
            value = spec[1] if spec and spec[0] in ('text', 'prompt', 'salary') else _profile_value_for(label, profile)
            if not value and ai_answer and len(label) > 25 and not is_prompt:
                value = ai_answer(label) or ''
            if not value:
                result['missed'].append(label[:60]); continue
            if is_prompt:
                wants = [value] + (['LinkedIn', 'Indeed', 'Glassdoor', 'Internet', 'Job Board', 'Company Website', 'Career Site', 'Other']
                                   if 'hear' in ll or 'source' in ll else [])
                picked = await pick_prompt(page, fld, inp, wants)
                await close_popups(page)
                if picked:
                    qa_note('prompt', label, picked, 'answer-table'); f.append(f'prompt:{label[:40]}')
                else:
                    result['missed'].append(label[:60])
            else:
                await inp.click()
                await inp.fill(value)
                qa_note('text', label, value, 'profile'); f.append(f'text:{label[:40]}')
        except Exception as e:
            result.setdefault('warnings', []).append(f'{label[:40]}: {str(e)[:60]}')
            continue


async def my_information(page, profile: dict, result: dict):
    await answer_form_fields(page, profile, None, result)


async def add_block(page, section_sel: str) -> bool:
    """Click the Add button inside a My Experience section."""
    try:
        sec = page.locator(section_sel).first
        if not await sec.count():
            return False
        btn = sec.locator('button:has-text("Add"), [data-automation-id="Add"], [data-automation-id="add-button"]').first
        if await btn.is_visible(timeout=1500):
            await btn.click()
            await asyncio.sleep(1.5)
            return True
    except Exception:
        pass
    return False


async def my_experience(page, profile: dict, result: dict):
    f = result['filled']
    exp = (profile.get('experience') or [{}])[0]
    edus = profile.get('education') or [{}]
    edu = next((e for e in edus if 'bachelor' in (e.get('degree') or '').lower()), edus[0])
    # resume first: many tenants parse it and pre-fill the blocks
    try:
        inp = page.locator('[data-automation-id="file-upload-input-ref"], input[type="file"]').first
        already = await page.locator('[data-automation-id="file-upload-successful"], [data-automation-id="delete-file"], '
                                     '[data-automation-id*="uploadedFile" i]').count()
        if already:
            print('  [*] Workday: a resume is already attached to this draft')
            f.append('resume')
            # earlier runs attached duplicates: keep exactly one file
            dels = page.locator('[data-automation-id="delete-file"]')
            while await dels.count() > 1:
                await dels.last.click()
                await asyncio.sleep(1.2)
                print('  [~] Workday: removed a duplicate resume attachment')
        elif await inp.count():
            await inp.set_input_files(profile['resume_path'])
            await asyncio.sleep(4)
            f.append('resume')
            print('  [+] Workday: resume uploaded')
    except Exception as e:
        print(f'  [!] Workday resume upload failed: {e}')
    # one work block if the section is empty
    sec = '[data-automation-id="workExperienceSection"]'
    try:
        has_block = await page.locator(f'{sec} [data-automation-id="jobTitle"]').count()
    except Exception:
        has_block = 0
    if await page.locator(sec).count() and not has_block:
        await add_block(page, sec)
    if await fill(page, '[data-automation-id="jobTitle"]', exp.get('title') or profile.get('current_title', '')): f.append('jobTitle')
    await fill(page, '[data-automation-id="company"]', exp.get('company') or profile.get('current_company', ''))
    await fill(page, '[data-automation-id="location"]', f"{profile.get('city', '')}, {profile.get('state', '')}")
    if (exp.get('end') or 'present').lower() == 'present':
        await check(page, '[data-automation-id="currentlyWorkHere"]')
    bits = (exp.get('start') or '').replace('-', ' ').split()
    month = next((b for b in bits if b.isalpha()), '')
    year = next((b for b in bits if b.isdigit() and len(b) == 4), '')
    if month and year:
        mnum = str(datetime.strptime(month[:3], '%b').month) if month[:3].title() in [datetime(2000, m, 1).strftime('%b') for m in range(1, 13)] else ''
        await fill(page, '[data-automation-id="startDate-dateSectionMonth-input"], [data-automation-id="dateSectionMonth-input"]', mnum)
        await fill(page, '[data-automation-id="startDate-dateSectionYear-input"], [data-automation-id="dateSectionYear-input"]', year)
    await fill(page, '[data-automation-id="description"]', (exp.get('bullets') or [''])[0] if isinstance(exp.get('bullets'), list) else exp.get('description', ''))
    # education
    esec = '[data-automation-id="educationSection"]'
    try:
        has_edu = await page.locator(f'{esec} [data-automation-id="school"]').count()
    except Exception:
        has_edu = 0
    if await page.locator(esec).count() and not has_edu:
        await add_block(page, esec)
    if await fill(page, '[data-automation-id="school"]', edu.get('school', '')): f.append('school')
    await choose(page, '[data-automation-id="degree"]', ["Bachelor", "Bachelor's", 'BS', 'B.S'], fallback_first=False)
    if await page.locator('[data-automation-id="formField-fieldOfStudy"]').count():
        await type_prompt(page, '[data-automation-id="formField-fieldOfStudy"]', edu.get('field', 'Business'))
    # linkedin
    await fill(page, '[data-automation-id="linkedinQuestion"]', profile.get('linkedin', ''))


async def voluntary(page, profile: dict, result: dict):
    for sel in ('[data-automation-id="gender"]', '[data-automation-id="ethnicityDropdown"]', '[data-automation-id="veteranStatus"]',
                '[data-automation-id="hispanicOrLatino"]'):
        await choose(page, sel, ['decline', 'do not wish', "don't wish", 'prefer not', 'not to answer', 'i do not'])
    await check(page, '[data-automation-id="agreementCheckbox"], [data-automation-id="termsAndConditions"] input[type="checkbox"]')


async def self_identify(page, profile: dict, result: dict):
    """Voluntary disability self-identification (CC-305): full name, today's
    date typed section by section, and the "I do not want to answer" box."""
    f = result['filled']
    name = f"{profile['first_name']} {profile['last_name']}"
    for sel in ('[data-automation-id="formField-name"] input', 'input[id$="--name"]', '[data-automation-id="name"]'):
        if await fill(page, sel, name):
            f.append('selfid:name')
            break
    today = datetime.now()
    try:
        month = page.locator('[data-automation-id="dateSectionMonth-input"]').first
        if await month.count() and not (await month.input_value()).strip():
            # the sections are 0.09px-wide spinbuttons behind display divs:
            # focus the month, then type MMDDYYYY, the widget auto-advances
            await month.focus()
            await page.keyboard.type(f'{today.month:02d}{today.day:02d}{today.year}', delay=90)
            await asyncio.sleep(0.5)
            f.append('selfid:date')
    except Exception as e:
        result.setdefault('warnings', []).append(f'selfid date: {str(e)[:60]}')
    # disability status: checkboxes or radios, take the decline option
    for kind in ('checkbox', 'radio'):
        boxes = page.locator(f'[data-automation-id="formField-disabilityStatus"] input[type="{kind}"], '
                             f'input[type="{kind}"][id*="disab" i]')
        n = await boxes.count()
        for i in range(n):
            box = boxes.nth(i)
            try:
                lab = (await box.evaluate("o => (o.id && document.querySelector(`label[for=\"${o.id}\"]`)?.innerText) || o.closest('label')?.innerText || ''")).strip()
            except Exception:
                lab = ''
            if DECLINE_RX.search(lab):
                await set_radio(page, box)
                f.append('selfid:decline')
                break
        else:
            continue
        break


# ------------------------------------------------------------------- main

async def apply(page, url: str, profile: dict, job_info: dict | None, dry_run: bool, ai_answer=None) -> dict:
    """Drive a myworkdayjobs posting from the job page to the confirmation.
    Result mirrors apply_to_url's dict: status, filled, missed, ats, notes."""
    result = {'url': url, 'status': 'error', 'ats': 'workday', 'filled': [], 'missed': [], 'steps': [], 'errors': []}
    tenant = tenant_of(url)
    base = url.split('?')[0].rstrip('/')
    apply_url = base if base.endswith('/apply/applyManually') else re.sub(r'/apply.*$', '', base) + '/apply/applyManually'
    await page.goto(apply_url, wait_until='domcontentloaded', timeout=60000)
    try:
        await page.wait_for_load_state('networkidle', timeout=20000)
    except Exception:
        pass
    await asyncio.sleep(2)
    body = (await _text(page, 'body', 3000)).lower()
    if 'no longer' in body and ('available' in body or 'accepting' in body) or 'job not found' in body:
        result['status'] = 'closed'
        return result
    await debug(page, 'step1')
    acct = await ensure_account(page, profile, tenant)
    result['account'] = acct
    if acct.startswith('error'):
        result['status'] = 'needs_account'
        result['errors'].append(acct)
        print(f'  [!] Workday account: {acct}')
        return result

    seen = set()
    reloads = 0
    for _ in range(14):
        # transient "Something went wrong / please refresh" card (VPS error on
        # a resumed draft): reload up to twice, it clears
        try:
            body = (await page.locator('body').inner_text(timeout=3000)).lower()
        except Exception:
            body = ''
        if 'something went wrong' in body and 'refresh' in body and reloads < 2:
            reloads += 1
            print(f'  [~] Workday error card, reloading ({reloads}/2)')
            await page.reload(wait_until='domcontentloaded')
            await asyncio.sleep(4)
            try:
                await page.wait_for_load_state('networkidle', timeout=20000)
            except Exception:
                pass
            for sel in ('button:has-text("Continue Application"):visible', 'button:has-text("Continue"):visible',
                        '[data-automation-id="continueButton"]'):
                try:
                    if await page.locator(sel).first.is_visible(timeout=800):
                        await page.locator(sel).first.click()
                        await asyncio.sleep(3)
                        break
                except Exception:
                    continue
            continue
        step = await step_name(page)
        result['steps'].append(step)
        if not step:
            break
        if not await page.locator('[data-automation-id^="formField-"], input[type="file"], [data-automation-id="pageFooterNextButton"]').count():
            await asyncio.sleep(3)
            continue
        if step in seen and step != 'review':
            # did not advance: read the errors once more and stop
            errs = await errors(page)
            result['errors'] += errs
            result['status'] = 'filled_not_submitted'
            print(f"  [!] Workday stuck on '{step}': {' | '.join(errs)[:200]}")
            print(f"      filled={result['filled']} missed={result['missed']} warnings={result.get('warnings')}")
            try:
                head = (await page.locator('body').inner_text(timeout=3000))[:600].replace(chr(10), ' | ')
                print(f"      page head: {head}")
                checked = await page.evaluate("Array.from(document.querySelectorAll('input[type=radio]')).map(r=>r.id+':'+r.checked)")
                chips = await page.evaluate("Array.from(document.querySelectorAll('[data-automation-id=selectedItem]')).map(e=>e.innerText.trim())")
                print(f"      radios={checked} chips={chips}")
            except Exception:
                pass
            await debug(page, f'stuck_{step[:12]}')
            return result
        seen.add(step)
        print(f'  [*] Workday step: {step}')
        if 'information' in step:
            await answer_form_fields(page, profile, job_info, result, ai_answer)
        elif 'experience' in step:
            await my_experience(page, profile, result)
            await answer_form_fields(page, profile, job_info, result, ai_answer)
        elif 'question' in step:
            await answer_form_fields(page, profile, job_info, result, ai_answer)
        elif 'voluntary' in step or 'disclosure' in step:
            await voluntary(page, profile, result)
            await answer_form_fields(page, profile, job_info, result, ai_answer)
        elif 'identify' in step:
            await self_identify(page, profile, result)
            await answer_form_fields(page, profile, job_info, result, ai_answer)
        elif 'review' in step:
            await debug(page, 'review')
            if dry_run:
                result['status'] = 'dry_run'
                print('  [~] Workday: reached Review — DRY RUN, not submitting')
                return result
            await click_next(page)   # the Next button reads "Submit" here
            await asyncio.sleep(3)
            txt = (await _text(page, 'body', 5000))
            if CONFIRM_RX.search(txt) or 'applied' in (await step_name(page)):
                result['status'] = 'submitted'
                print('  [+] Workday: application submitted (confirmation seen)')
            else:
                result['status'] = 'submit_unconfirmed'
                result['errors'] += await errors(page)
            return result
        else:
            await answer_form_fields(page, profile, job_info, result, ai_answer)
        await debug(page, f'filled_{step[:12]}')
        if WD_DEBUG:
            try:
                snap = await page.evaluate("({radios: Array.from(document.querySelectorAll('input[type=radio]')).map(r=>r.id+':'+r.checked), chips: Array.from(document.querySelectorAll('[data-automation-id=selectedItem]')).map(e=>e.innerText.trim()), url: location.href.slice(-40)})")
                print(f'      before next: {snap} filled={result["filled"]} missed={result["missed"]}')
            except Exception:
                pass
        await click_next(page)
        if WD_DEBUG:
            try:
                snap = await page.evaluate("({step: (document.querySelector('[data-automation-id=progressBarActiveStep]')||{}).innerText, url: location.href.slice(-40), alerts: Array.from(document.querySelectorAll('[role=alert],[data-automation-id*=rror i],[data-automation-id*=lert i]')).map(e=>e.innerText.trim().slice(0,80))})")
                print(f'      after next: {snap}')
            except Exception:
                pass
        errs = await errors(page)
        if errs:
            print(f"  [~] Workday '{step}' validation: {' | '.join(errs)[:160]} — one repair pass")
            result['errors'] += errs
            await answer_form_fields(page, profile, job_info, result, ai_answer)
            await click_next(page)
    result['status'] = result['status'] if result['status'] != 'error' else 'filled_not_submitted'
    return result
