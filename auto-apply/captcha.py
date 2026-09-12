"""CAPTCHA handling — the engine's problem, never the user's.

History: Lever (hCaptcha) never got a single confirmed submit, July through
September; Ashby's spam banner and the odd Greenhouse reCAPTCHA parked jobs
as manual_review for a human. 9/9: no human step is allowed, so a challenge
is now solved in place:

  1. checkbox — hCaptcha / reCAPTCHA v2 render a checkbox iframe; a real
     Chrome profile on a residential exit usually passes on the click alone.
  2. service — CAPTCHA_API_KEY set: a 2captcha-compatible solver (2captcha,
     CapSolver, anti-captcha all speak in.php/res.php) returns a token for
     the page's sitekey; the token goes into the response textarea the form
     posts, exactly where the widget would have put it.

Neither succeeded -> the caller retries the job on a later wake (requeue.py),
it never lands in a hand-apply list.

Env: CAPTCHA_API_KEY, CAPTCHA_API_URL (default https://2captcha.com).
"""
from __future__ import annotations

import asyncio
import os
import time

import httpx

SERVICE_URL = os.environ.get('CAPTCHA_API_URL', 'https://2captcha.com').rstrip('/')
SOLVE_TIMEOUT = 150  # seconds to wait for a worker-solved token

_SITEKEY_JS = """
() => {
  const q = (s) => document.querySelector(s);
  const h = q('.h-captcha[data-sitekey], [data-hcaptcha-sitekey], iframe[src*="hcaptcha.com"]');
  if (h) {
    let key = h.dataset?.sitekey || h.dataset?.hcaptchaSitekey || '';
    if (!key && h.src) { const m = h.src.match(/sitekey=([0-9a-f-]+)/i); key = m ? m[1] : ''; }
    if (key) return {kind: 'hcaptcha', sitekey: key};
  }
  const g = q('.g-recaptcha[data-sitekey], iframe[src*="recaptcha/api2/anchor"], iframe[src*="recaptcha/enterprise/anchor"]');
  if (g) {
    let key = g.dataset?.sitekey || '';
    if (!key && g.src) { const m = g.src.match(/[?&]k=([A-Za-z0-9_-]+)/); key = m ? m[1] : ''; }
    const invisible = (g.dataset?.size || '') === 'invisible' || !!g.closest?.('.grecaptcha-badge');
    if (key && !invisible) return {kind: 'recaptcha', sitekey: key};
  }
  const t = q('.cf-turnstile[data-sitekey]');
  if (t) return {kind: 'turnstile', sitekey: t.dataset.sitekey};
  return null;
}
"""

_TOKEN_JS = """
() => {
  const ta = document.querySelector('textarea[name="h-captcha-response"], textarea[name="g-recaptcha-response"], input[name="cf-turnstile-response"]');
  return ta && ta.value ? ta.value.length : 0;
}
"""

_INJECT_JS = """
([kind, token]) => {
  const names = kind === 'hcaptcha' ? ['h-captcha-response', 'g-recaptcha-response']
              : kind === 'recaptcha' ? ['g-recaptcha-response']
              : ['cf-turnstile-response'];
  let n = 0;
  const host = document.querySelector('.h-captcha, .g-recaptcha, .cf-turnstile, form') || document.body;
  for (const name of names) {
    if (!document.querySelector(`textarea[name="${name}"], input[name="${name}"]`)) {
      const ta = document.createElement('textarea'); ta.name = name; ta.style.display = 'none'; host.appendChild(ta);
    }
  }
  for (const name of names) {
    for (const el of document.querySelectorAll(`textarea[name="${name}"], input[name="${name}"]`)) {
      el.style.display = 'block';
      el.value = token;
      el.dispatchEvent(new Event('input', {bubbles: true}));
      el.dispatchEvent(new Event('change', {bubbles: true}));
      n++;
    }
  }
  // reCAPTCHA v2 sites often gate submit on the widget callback
  try {
    const cfg = window.___grecaptcha_cfg;
    if (kind === 'recaptcha' && cfg && cfg.clients) {
      for (const c of Object.values(cfg.clients)) {
        const walk = (o, d) => {
          if (!o || d > 4) return false;
          for (const v of Object.values(o)) {
            if (typeof v === 'function' && v.length <= 1 && /callback|function\\s*\\(/.test(v.toString())) { try { v(token); return true; } catch (e) {} }
            if (v && typeof v === 'object' && walk(v, d + 1)) return true;
          }
          return false;
        };
        walk(c, 0);
      }
    }
  } catch (e) {}
  return n;
}
"""


async def widget(page) -> dict | None:
    """The visible, unsolved challenge widget on this page, or None."""
    try:
        w = await page.evaluate(_SITEKEY_JS)
    except Exception:
        return None
    if not w:
        return None
    try:
        if await page.evaluate(_TOKEN_JS):
            return None  # already carries a token
    except Exception:
        pass
    return w


async def _has_token(page) -> bool:
    try:
        return bool(await page.evaluate(_TOKEN_JS))
    except Exception:
        return False


async def click_checkbox(page, kind: str, wait: float = 20.0) -> bool:
    """Click the widget's checkbox and wait for a token. A challenge grid
    that opens afterwards is left for the service step."""
    sel = ('iframe[src*="hcaptcha.com"][src*="checkbox"], iframe[src*="hcaptcha.com"]'
           if kind == 'hcaptcha' else
           'iframe[src*="recaptcha/api2/anchor"], iframe[src*="recaptcha/enterprise/anchor"]')
    try:
        frame_el = page.locator(sel).first
        if not await frame_el.is_visible(timeout=3000):
            return False
        handle = await frame_el.element_handle()
        frame = await handle.content_frame() if handle else None
        if frame is None:
            return False
        box = frame.locator('#checkbox, .recaptcha-checkbox-border, [role="checkbox"]').first
        await box.click(timeout=5000)
    except Exception:
        return False
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if await _has_token(page):
            return True
        await asyncio.sleep(1)
    return False


def _service_key() -> str:
    return os.environ.get('CAPTCHA_API_KEY', '').strip()


def service_available() -> bool:
    return bool(_service_key())


async def solve_with_service(page, kind: str, sitekey: str) -> str | None:
    key = _service_key()
    if not key:
        return None
    method = {'hcaptcha': 'hcaptcha', 'recaptcha': 'userrecaptcha', 'turnstile': 'turnstile'}[kind]
    params = {'key': key, 'method': method, 'sitekey': sitekey, 'pageurl': page.url, 'json': 1}
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            r = await client.post(f'{SERVICE_URL}/in.php', data=params)
            body = r.json()
        except Exception as e:
            print(f'  [!] captcha service unreachable: {e}')
            return None
        if body.get('status') != 1:
            print(f"  [!] captcha service refused: {body.get('request')}")
            return None
        task_id = body['request']
        deadline = time.monotonic() + SOLVE_TIMEOUT
        await asyncio.sleep(10)
        while time.monotonic() < deadline:
            try:
                r = await client.get(f'{SERVICE_URL}/res.php',
                                     params={'key': key, 'action': 'get', 'id': task_id, 'json': 1})
                body = r.json()
            except Exception:
                await asyncio.sleep(5)
                continue
            if body.get('status') == 1:
                return body['request']
            if body.get('request') != 'CAPCHA_NOT_READY':
                print(f"  [!] captcha service failed: {body.get('request')}")
                return None
            await asyncio.sleep(5)
    print('  [!] captcha service timed out')
    return None


async def solve(page) -> bool:
    """Try to get a token onto the page. True when the form now carries one."""
    w = await widget(page)
    if not w:
        return await _has_token(page)
    kind, sitekey = w['kind'], w['sitekey']
    print(f"  [*] {kind} widget (sitekey {sitekey[:8]}…) — solving")
    if kind != 'turnstile' and await click_checkbox(page, kind):
        print('  [+] captcha passed on the checkbox click')
        return True
    token = await solve_with_service(page, kind, sitekey)
    if not token:
        if not service_available():
            print('  [~] no CAPTCHA_API_KEY — set one (2captcha/CapSolver) to solve challenges; retrying on a later wake')
        return False
    try:
        n = await page.evaluate(_INJECT_JS, [kind, token])
    except Exception as e:
        print(f'  [!] token inject failed: {e}')
        return False
    print(f'  [+] captcha token injected into {n} field(s)')
    return n > 0
