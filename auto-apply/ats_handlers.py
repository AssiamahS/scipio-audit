"""
ATS-specific form handlers for Greenhouse, Lever, Workday, and generic forms.
Each handler knows how to fill out its platform's application form.
"""
import asyncio
import re
from playwright.async_api import Page, Locator

# Common field mappings - maps form labels/names to profile keys
FIELD_MAP = {
    'first.name': 'first_name', 'first_name': 'first_name', 'fname': 'first_name',
    'last.name': 'last_name', 'last_name': 'last_name', 'lname': 'last_name',
    'full.name': '__full_name__',
    'email': 'email', 'e-mail': 'email', 'email.address': 'email',
    'phone': 'phone', 'mobile': 'phone', 'phone.number': 'phone', 'telephone': 'phone',
    'linkedin': 'linkedin', 'linkedin.url': 'linkedin', 'linkedin.profile': 'linkedin',
    'github': 'github', 'website': 'github', 'portfolio': 'github',
    'city': 'city', 'state': 'state', 'zip': 'zip', 'zipcode': 'zip', 'postal': 'zip',
    'location': 'location', 'address': 'location',
    'salary': 'desired_salary', 'desired.salary': 'desired_salary', 'expected.salary': 'desired_salary',
    'compensation': 'desired_salary',
    'years': 'years_experience', 'experience': 'years_experience',
    'current.company': 'current_company', 'current.employer': 'current_company',
    'current.title': 'current_title', 'job.title': 'current_title',
}


# per-application question/answer audit — the applier resets this before each
# application, prints it after, and stores it in apply_log.json so a failed
# submit shows exactly which questions were answered (and from where) and
# which required ones were left hanging
QA_LOG: list = []


def reset_qa_log():
    QA_LOG.clear()


def qa_note(kind: str, label: str, answer, source: str):
    QA_LOG.append({'kind': kind, 'label': (label or '').strip()[:120],
                   'answer': str(answer)[:120], 'source': source})


async def safe_fill(locator: Locator, value: str, timeout: int = 3000):
    """Safely fill a field, ignoring errors."""
    try:
        await locator.fill(value, timeout=timeout)
        return True
    except Exception:
        return False


async def safe_click(locator: Locator, timeout: int = 3000):
    """Safely click an element."""
    try:
        await locator.click(timeout=timeout)
        return True
    except Exception:
        return False


async def safe_select(locator: Locator, value: str, timeout: int = 3000):
    """Safely select from dropdown."""
    try:
        await locator.select_option(value=value, timeout=timeout)
        return True
    except Exception:
        try:
            await locator.select_option(label=value, timeout=timeout)
            return True
        except Exception:
            return False


def normalize_label(text: str) -> str:
    """Normalize a form label for matching."""
    text = text.lower().strip()
    text = re.sub(r'[^a-z0-9.]', '.', text)
    text = re.sub(r'\.+', '.', text).strip('.')
    return text


CONDITIONAL_LABEL_PREFIXES = ('if yes', 'if applicable', 'if so', 'if you answered')


def match_field(label: str, profile: dict) -> str | None:
    """Try to match a form label to a profile field."""
    # conditional follow-ups ("If yes, when and which company?") belong to a
    # question we answered No to — filling them contradicts that answer
    if (label or '').strip().lower().startswith(CONDITIONAL_LABEL_PREFIXES):
        return None
    norm = normalize_label(label)

    # Direct match
    if norm in FIELD_MAP:
        key = FIELD_MAP[norm]
        if key == '__full_name__':
            return f"{profile['first_name']} {profile['last_name']}"
        return str(profile.get(key, ''))

    # Partial match — only for short, field-like labels. Long labels are real
    # questions ("...please add the employee's full name, location, and position")
    # and partial-matching those stuffs identity data where it doesn't belong.
    if len(norm) > 40:
        return None
    for pattern, key in FIELD_MAP.items():
        if pattern in norm or norm in pattern:
            if key == '__full_name__':
                return f"{profile['first_name']} {profile['last_name']}"
            return str(profile.get(key, ''))

    return None


async def detect_ats(page: Page) -> str:
    """Detect which ATS the page is using."""
    url = page.url.lower()

    if 'greenhouse.io' in url or 'boards.greenhouse' in url:
        return 'greenhouse'
    if 'lever.co' in url or 'jobs.lever' in url:
        return 'lever'
    if 'myworkdayjobs.com' in url or 'workday.com' in url:
        return 'workday'
    if 'ashbyhq.com' in url:
        return 'ashby'
    if 'linkedin.com' in url:
        return 'linkedin'
    if 'indeed.com' in url:
        return 'indeed'

    # Check page content
    content = await page.content()
    content_lower = content.lower()

    if 'greenhouse' in content_lower:
        return 'greenhouse'
    if 'lever' in content_lower:
        return 'lever'
    if 'workday' in content_lower:
        return 'workday'

    return 'generic'


async def fill_greenhouse(page: Page, profile: dict) -> dict:
    """Handle Greenhouse ATS applications."""
    result = {'filled': [], 'missed': [], 'ats': 'greenhouse'}

    # Greenhouse uses #application-form or similar
    # Standard fields
    fields = [
        ('#first_name', profile['first_name']),
        ('#last_name', profile['last_name']),
        ('#email', profile['email']),
        ('#phone', profile['phone']),
        ('input[name*="first_name"]', profile['first_name']),
        ('input[name*="last_name"]', profile['last_name']),
        ('input[name*="email"]', profile['email']),
        ('input[name*="phone"]', profile['phone']),
    ]

    for selector, value in fields:
        loc = page.locator(selector).first
        if await safe_fill(loc, value):
            result['filled'].append(selector)

    # LinkedIn field
    linkedin_fields = page.locator('input[name*="linkedin"], input[placeholder*="LinkedIn"]')
    if await linkedin_fields.count() > 0:
        if await safe_fill(linkedin_fields.first, profile['linkedin']):
            result['filled'].append('linkedin')

    # Resume upload
    resume_uploaded = await upload_resume(page, profile['resume_path'])
    if resume_uploaded:
        result['filled'].append('resume')
    else:
        result['missed'].append('resume')

    # Try to fill any labeled inputs we recognize
    await fill_labeled_inputs(page, profile, result)

    return result


async def fill_lever(page: Page, profile: dict) -> dict:
    """Handle Lever ATS applications."""
    result = {'filled': [], 'missed': [], 'ats': 'lever'}

    # Lever typically uses name attributes
    fields = [
        ('input[name="name"]', f"{profile['first_name']} {profile['last_name']}"),
        ('input[name="email"]', profile['email']),
        ('input[name="phone"]', profile['phone']),
        ('input[name="org"]', profile['current_company']),
        ('input[name="urls[LinkedIn]"]', profile['linkedin']),
        ('input[name="urls[GitHub]"]', profile['github']),
        ('input[name="urls[Portfolio]"]', profile['github']),
    ]

    for selector, value in fields:
        loc = page.locator(selector).first
        if await safe_fill(loc, value):
            result['filled'].append(selector)

    # Resume upload
    if await upload_resume(page, profile['resume_path']):
        result['filled'].append('resume')
    else:
        result['missed'].append('resume')

    # Lever text areas (cover letter, additional info)
    await fill_labeled_inputs(page, profile, result)

    return result


async def fill_workday(page: Page, profile: dict) -> dict:
    """Handle Workday ATS applications - the most complex."""
    result = {'filled': [], 'missed': [], 'ats': 'workday'}

    # Workday uses data-automation-id attributes
    wd_fields = [
        ('[data-automation-id="legalNameSection_firstName"]', profile['first_name']),
        ('[data-automation-id="legalNameSection_lastName"]', profile['last_name']),
        ('[data-automation-id="email"]', profile['email']),
        ('[data-automation-id="phone-number"]', profile['phone']),
        ('[data-automation-id="addressSection_city"]', profile['city']),
        ('[data-automation-id="addressSection_postalCode"]', profile['zip']),
    ]

    for selector, value in wd_fields:
        loc = page.locator(selector).first
        if await safe_fill(loc, value):
            result['filled'].append(selector)

    # Workday uses "How Did You Hear About Us" - try selecting
    hear_about = page.locator('[data-automation-id="source"]').first
    await safe_select(hear_about, 'Job Board')

    # Resume upload (Workday often has a specific upload area)
    if await upload_resume(page, profile['resume_path']):
        result['filled'].append('resume')
    else:
        result['missed'].append('resume')

    await fill_labeled_inputs(page, profile, result)

    return result


async def fill_ashby(page: Page, profile: dict) -> dict:
    """Handle Ashby ATS applications (jobs.ashbyhq.com).

    The /application route lands straight on the form. System fields use
    _systemfield_* ids, every custom question has a label[for=<id>], and
    boolean questions render as Yes/No button pairs instead of checkboxes.
    """
    result = {'filled': [], 'missed': [], 'ats': 'ashby'}

    fields = [
        ('#_systemfield_name', f"{profile['first_name']} {profile['last_name']}"),
        ('#_systemfield_email', profile['email']),
        ('input[id*="phone" i]', profile['phone']),
    ]
    for selector, value in fields:
        loc = page.locator(selector).first
        if await safe_fill(loc, value):
            result['filled'].append(selector)

    # resume goes to the dedicated system field — the first file input on
    # the page is the "autofill from resume" drop zone, not the attachment
    resume_ok = False
    try:
        sys_resume = page.locator('#_systemfield_resume')
        if await sys_resume.count() > 0:
            await sys_resume.set_input_files(profile['resume_path'])
            await asyncio.sleep(1)
            resume_ok = True
    except Exception:
        pass
    if not resume_ok:
        resume_ok = await upload_resume(page, profile['resume_path'])
    result['filled' if resume_ok else 'missed'].append('resume')

    await fill_labeled_inputs(page, profile, result)
    await fill_ashby_location(page, profile, result)
    await answer_ashby_booleans(page, profile, result)
    await answer_ashby_radios(page, profile, result)

    return result


async def fill_ashby_location(page: Page, profile: dict, result: dict):
    """Ashby's Location field is an id-less combobox — find it by the label
    in its container, type the city, pick the first suggestion."""
    for cb in await page.locator('input[role="combobox"]:visible').all():
        try:
            if await cb.input_value():
                continue
            qtext = await cb.evaluate("""el => {
              for (let n = el.parentElement; n; n = n.parentElement) {
                const l = n.querySelector('label');
                if (l && l.innerText.trim()) return l.innerText.trim();
              }
              return '';
            }""")
            if 'location' not in qtext.lower():
                continue
            await cb.click()
            await cb.press_sequentially(profile.get('city', ''), delay=60)
            await asyncio.sleep(2)
            opt = page.locator('[role="option"]').first
            if await opt.is_visible(timeout=4000):
                await opt.click()
                qa_note('typeahead', qtext, profile.get('city', ''), 'profile')
                result['filled'].append('location')
            else:
                await page.keyboard.press('Escape')
        except Exception:
            continue


async def answer_ashby_radios(page: Page, profile: dict, result: dict):
    """Answer required Ashby radio groups.

    The question's label points at the bare question id while each radio in
    the group is named <uuid>_<question id> — match by stripping the uuid
    prefix, then pick the option the answer canon wants. Voluntary EEOC
    groups are left untouched."""
    radios = await page.locator('input[type="radio"]').all()
    groups: dict = {}
    for r in radios:
        name = await r.get_attribute('name') or ''
        if name:
            groups.setdefault(name, []).append(r)
    for name, opts in groups.items():
        try:
            checked = False
            for o in opts:
                if await o.is_checked():
                    checked = True
                    break
            if checked:
                continue
            qid = name.split('_', 1)[-1] if '_' in name else name
            qlabel = page.locator(f'label[for="{qid}"], label[for="_{qid}"]').first
            if await qlabel.count() == 0:
                continue
            question = (await qlabel.inner_text()).strip()
            if question.lower().startswith(('gender', 'race', 'veteran')):
                continue
            wants = _desired_options(question)
            if not wants and ('hear about' in question.lower() or 'how did you' in question.lower()):
                wants = ['linkedin']
            if not wants:
                continue
            texts = []
            for o in opts:
                oid = await o.get_attribute('id') or ''
                t = ''
                if oid:
                    ol = page.locator(f'label[for="{oid}"]')
                    if await ol.count() > 0:
                        t = (await ol.first.inner_text()).strip()
                texts.append(t)
            pick = next((i for i, t in enumerate(texts)
                         if any(w.lower() in t.lower() for w in wants)), None)
            if pick is None:
                continue
            await opts[pick].check(force=True)
            qa_note('radio', question, texts[pick], 'dropdown_answers')
            result['filled'].append(f'radio:{question[:40]}')
            await asyncio.sleep(0.3)
        except Exception:
            continue


async def answer_radio_groups(page: Page, profile: dict, result: dict):
    """Generic required-radio answering — lever's redesigned form asks work
    eligibility/sponsorship as radio pairs no other sweep touches. Groups by
    name; the question is the first ancestor line that isn't an option label;
    answers come from the DROPDOWN_ANSWERS canon. EEO/voluntary groups and
    anything the canon doesn't cover are left for the QA audit to flag."""
    radios = await page.locator('input[type="radio"]').all()
    groups: dict = {}
    for r in radios:
        name = await r.get_attribute('name') or ''
        if name:
            groups.setdefault(name, []).append(r)
    for name, opts in groups.items():
        try:
            if any([await o.is_checked() for o in opts]):
                continue
            question = await opts[0].evaluate("""inp => {
              const optText = Array.from(document.getElementsByName(inp.name)).map(o => {
                const l = o.id && document.querySelector(`label[for="${CSS.escape(o.id)}"]`);
                return l ? l.innerText.trim() : (o.closest('label')?.innerText.trim() || '');
              }).join('|');
              for (let el = inp.parentElement, i = 0; el && i < 6; el = el.parentElement, i++) {
                const t = (el.innerText || '').trim();
                if (!t) continue;
                const first = t.split('\\n')[0].trim();
                if (first && !optText.includes(first) && first.length > 8 && first.length < 250)
                  return first;
              }
              return '';
            }""")
            ql = (question or '').lower()
            if not question or ql.startswith(('gender', 'race', 'veteran', 'disability')) \
                    or 'voluntary' in ql or 'pronoun' in ql:
                continue
            wants = _desired_options(question)
            if not wants:
                continue
            texts = []
            for o in opts:
                oid = await o.get_attribute('id') or ''
                t = ''
                if oid:
                    ol = page.locator(f'label[for="{oid}"]')
                    if await ol.count() > 0:
                        t = (await ol.first.inner_text()).strip()
                if not t:
                    t = (await o.evaluate("o => o.closest('label')?.innerText || ''")).strip()
                texts.append(t)
            pick = next((i for i, t in enumerate(texts)
                         if t and any(w.lower() in t.lower() for w in wants)), None)
            if pick is None:
                continue
            await opts[pick].check(force=True)
            qa_note('radio', question, texts[pick], 'answer-table')
            result['filled'].append(f'radio:{question[:40]}')
            await asyncio.sleep(0.3)
        except Exception:
            continue


async def answer_ashby_booleans(page: Page, profile: dict, result: dict):
    """Click Yes/No button pairs on Ashby boolean questions.

    Each question lives in a container with a label and two buttons; the
    label text picks the answer through the same DROPDOWN_ANSWERS canon
    the comboboxes use. Unmatched questions are left alone so the QA
    audit flags them instead of a guess getting submitted.
    """
    yes_buttons = await page.locator('button:visible', has_text=re.compile(r'^\s*Yes\s*$')).all()
    for yes_btn in yes_buttons:
        try:
            question = await yes_btn.evaluate("""btn => {
              for (let el = btn.parentElement; el; el = el.parentElement) {
                const label = el.querySelector('label');
                if (label && label.innerText.trim()) return label.innerText.trim();
              }
              return '';
            }""")
            if not question:
                continue
            wants = _desired_options(question)
            if not wants:
                continue
            want_yes = any(w in ('yes', 'agree', 'acknowledge') for w in wants)
            target = yes_btn
            if not want_yes:
                sibling_no = yes_btn.locator(
                    'xpath=following-sibling::button[normalize-space()="No"]'
                    ' | preceding-sibling::button[normalize-space()="No"]').first
                if await sibling_no.count() == 0:
                    continue
                target = sibling_no
            await target.click()
            answer = 'Yes' if want_yes else 'No'
            qa_note('boolean', question, answer, 'dropdown_answers')
            result['filled'].append(f'bool:{question[:40]}')
            await asyncio.sleep(0.3)
        except Exception:
            continue


async def fill_generic(page: Page, profile: dict) -> dict:
    """Handle generic/unknown application forms."""
    result = {'filled': [], 'missed': [], 'ats': 'generic'}

    # Try common input patterns
    inputs = await page.locator('input[type="text"], input[type="email"], input[type="tel"], input[type="url"], input[type="number"]').all()

    for inp in inputs:
        try:
            # Get identifying info
            name = await inp.get_attribute('name') or ''
            placeholder = await inp.get_attribute('placeholder') or ''
            label_text = ''

            # Try to find associated label
            inp_id = await inp.get_attribute('id')
            if inp_id:
                label = page.locator(f'label[for="{inp_id}"]')
                if await label.count() > 0:
                    label_text = await label.first.inner_text()

            # Try aria-label
            aria = await inp.get_attribute('aria-label') or ''

            # Match against profile
            for text in [name, placeholder, label_text, aria]:
                if not text:
                    continue
                value = match_field(text, profile)
                if value:
                    current = await inp.input_value()
                    if not current:  # Don't overwrite existing values
                        await safe_fill(inp, value)
                        result['filled'].append(text)
                    break
        except Exception:
            continue

    # Resume upload
    if await upload_resume(page, profile['resume_path']):
        result['filled'].append('resume')

    return result


async def fill_labeled_inputs(page: Page, profile: dict, result: dict):
    """Fill inputs by finding their labels."""
    inputs = await page.locator('input:visible, textarea:visible, select:visible').all()

    for inp in inputs:
        try:
            current = await inp.input_value()
            if current:  # Already filled
                continue

            # Get all identifying attributes
            name = await inp.get_attribute('name') or ''
            placeholder = await inp.get_attribute('placeholder') or ''
            aria = await inp.get_attribute('aria-label') or ''
            inp_id = await inp.get_attribute('id') or ''

            label_text = ''
            if inp_id:
                label = page.locator(f'label[for="{inp_id}"]')
                if await label.count() > 0:
                    label_text = await label.first.inner_text()

            for text in [label_text, name, placeholder, aria]:
                if not text:
                    continue
                value = match_field(text, profile)
                if value:
                    tag = await inp.evaluate('el => el.tagName')
                    if tag == 'SELECT':
                        await safe_select(inp, value)
                    else:
                        await safe_fill(inp, value)
                    result['filled'].append(text)
                    break
        except Exception:
            continue


async def upload_resume(page: Page, resume_path: str) -> bool:
    """Try to upload resume via file input."""
    try:
        # Find file input
        file_inputs = page.locator('input[type="file"]')
        count = await file_inputs.count()
        if count > 0:
            await file_inputs.first.set_input_files(resume_path)
            await asyncio.sleep(1)  # Wait for upload processing
            return True

        # Try clicking upload button to trigger file dialog
        upload_btns = page.locator('button:has-text("Upload"), button:has-text("resume"), a:has-text("Upload"), [data-automation-id="file-upload"]')
        if await upload_btns.count() > 0:
            # Use file chooser
            async with page.expect_file_chooser(timeout=5000) as fc_info:
                await upload_btns.first.click()
            file_chooser = await fc_info.value
            await file_chooser.set_files(resume_path)
            await asyncio.sleep(1)
            return True
    except Exception:
        pass
    return False


# label patterns -> acceptable option patterns, tried in order
DROPDOWN_ANSWERS = [
    # sponsorship must outrank work-auth: "require immigration sponsorship
    # for work authorization" contains 'work authorization' and the old
    # ordering answered YES — a knockout — on 51 real QA records (7/22-8/23,
    # incl. every connectionshealthsolutions submit)
    (['require immigration sponsorship', 'require sponsorship',
      'require a visa', 'require visa', 'need sponsorship',
      'commence (sponsor', 'sponsor an immigration'], ['no']),
    (['authorized to work', 'work authorization', 'legally authorized',
      'eligible to work', 'lawfully'], ['yes']),
    (['sponsorship', 'visa'], ['no']),
    (['acknowledge', 'agree to', 'work location expectation', 'understand'],
     ['yes', 'acknowledge', 'agree']),
    (['country'], ['united states']),
    (['gender', 'race', 'ethnicity', 'hispanic', 'veteran', 'disability'],
     ['decline', "don't wish", 'do not wish', 'prefer not']),
    (['hear about'], ['linkedin', 'job board', 'other']),
    (['at least 18', '18 years'], ['yes']),
    (['previously worked', 'ever worked for', 'former employee',
      'current employee', 'currently employed'], ['no']),
    (['relative', 'family member'], ['no']),
    (['currently live', 'reside in'], ['yes']),
    (['non-compete', 'noncompete', 'non compete'], ['no']),
    (['healthcare data', 'ehr/emr', 'clinical data', 'claims data'], ['yes']),
    (['pronoun'], ['prefer not', 'decline', 'not to say']),
    (['security clearance', 'active clearance'], ['no']),
    (['previously applied', 'applied to', 'applied before'], ['no']),
    # 8/22-8/23 blockers seen in submitted-screenshots: PMG asks about prior
    # interviews, LTS asks about federal agency experience — both honest No
    (['interviewed with', 'previously interviewed', 'ever interviewed'], ['no']),
    (['federal government', 'government agency', 'federal agency'], ['no']),
    (['referred by', 'referral'], ['no']),
    (['background check', 'drug screen', 'drug test'], ['yes', 'acknowledge', 'agree']),
    (['willing to travel', 'able to travel'], ['yes']),
    (['overtime', 'weekends', 'shift'], ['yes']),
    (['vaccin'], ['yes']),
    (['u.s. person', 'us person', 'export control'], ['yes']),
    (['finra', 'series 7', 'series 63'], ['no']),
    (['english'], ['fluent', 'native', 'yes']),
    (['onsite', 'on-site', 'hybrid', 'in office', 'in-office'], ['yes']),
    (['commute', 'commuting distance'], ['yes']),
    (['notice period'], ['two weeks', '2 weeks']),
    # NOT 'start date': that collides with employment-block "Start date year"
    (['when can you start', 'availability to start'], ['two weeks', '2 weeks', 'immediately']),
    (['based in'], ['yes']),
    (['able to obtain', 'obtain and maintain'], ['yes']),
    (['consent to receive', 'consent to be contacted', 'do you consent',
      'receive updates from a recruiter'], ['yes']),
    (['preferred name'], ['no']),
    (['device type'], ['mobile', 'cell']),
    (['availability to work'], ['full-time', 'full time', 'immediately']),
    (['working preferences', 'work arrangement', 'work environment'],
     ['remote', 'flexible', 'hybrid']),
    # catch-all: "are you a current <Company> employee?"-style phrasings where
    # the company name splits the keywords — keep LAST so specifics win first
    (['employee'], ['no']),
]

_JS_LABEL = """el => {
  if (el.labels && el.labels.length) return el.labels[0].innerText;
  const al = el.getAttribute('aria-label'); if (al) return al;
  const alb = el.getAttribute('aria-labelledby');
  if (alb) {
    const t = alb.split(' ').map(i => (document.getElementById(i)||{}).innerText||'').join(' ');
    if (t.trim()) return t;
  }
  let p = el.parentElement;
  for (let d = 0; d < 4 && p; d++, p = p.parentElement) {
    const l = p.querySelector('label');
    if (l && l.innerText.trim()) return l.innerText;
  }
  return el.id || el.getAttribute('name') || '';
}"""


REQUIREMENT_QUESTION = ('meet all', 'meet the following', 'meet these', 'meet each',
                        'minimum qualification', 'minimum requirement', 'basic qualification',
                        'following requirement', 'following qualification', 'required qualification')


def _desired_options(label: str) -> list | None:
    l = (label or '').lower()
    # "Do you meet ALL of the following requirements? Master's degree ...
    # PMP ..." is a knockout question and the answer is decided by what he
    # actually holds, never by a pattern table. Blue Water Thinking 7/22-7/30:
    # three "Yes" answers on a Master's/PMP-required role, rejected in 6 days.
    # Honest No beats a lie that costs the slot AND the reputation.
    if any(p in l for p in REQUIREMENT_QUESTION):
        try:
            from fit_check import credential_gaps, years_required, TOTAL_YEARS
            gaps = credential_gaps(l, require_ctx=False)
            yrs = years_required(l)
            if gaps or (yrs and yrs > TOTAL_YEARS):
                qa_note('policy', label, f"No — lacks {', '.join(g['credential'] for g in gaps) or f'{yrs}+ years'}", 'knockout-honesty')
                return ['no']
        except Exception:
            pass
        return ['yes']
    for patterns, wants in DROPDOWN_ANSWERS:
        if any(p in l for p in patterns):
            return wants
    return None


async def answer_dropdowns(page: Page, profile: dict, result: dict):
    """Answer required dropdowns: native <select> and react comboboxes
    (modern greenhouse job-boards uses the latter)."""
    for sel in await page.locator('select:visible').all():
        try:
            label = await sel.evaluate(_JS_LABEL)
            wants = _desired_options(label)
            if not wants or await sel.input_value():
                continue
            opts = await sel.evaluate("el => Array.from(el.options).map(o => o.textContent)")
            for want in wants:
                m = next((o for o in opts if want in o.lower()), None)
                if m:
                    await sel.select_option(label=m.strip())
                    result['filled'].append(f'dropdown:{label.strip()[:40]}')
                    qa_note('select', label, m.strip(), 'answer-table')
                    break
        except Exception:
            continue

    combos = await page.locator(
        '[role="combobox"]:visible, button[aria-haspopup="listbox"]:visible'
    ).all()
    for cb in combos:
        try:
            label = await cb.evaluate(_JS_LABEL)
            wants = _desired_options(label)
            if not wants:
                continue
            current = ((await cb.inner_text()) or '').strip().lower()
            if current and 'select' not in current:
                continue  # already answered
            await cb.click()
            await asyncio.sleep(0.6)
            opts = page.locator('[role="option"]:visible')
            texts = [(await opts.nth(i).inner_text()).strip()
                     for i in range(min(await opts.count(), 30))]
            chosen = None
            for want in wants:
                chosen = next((i for i, t in enumerate(texts) if want in t.lower()), None)
                if chosen is not None:
                    break
            if chosen is not None:
                await opts.nth(chosen).click()
                result['filled'].append(f'dropdown:{label.strip()[:40]}')
                qa_note('combobox', label, texts[chosen], 'answer-table')
            else:
                await page.keyboard.press('Escape')
        except Exception:
            try:
                await page.keyboard.press('Escape')
            except Exception:
                pass


STATE_NAMES = {
    'AL': 'Alabama', 'AK': 'Alaska', 'AZ': 'Arizona', 'AR': 'Arkansas',
    'CA': 'California', 'CO': 'Colorado', 'CT': 'Connecticut', 'DE': 'Delaware',
    'FL': 'Florida', 'GA': 'Georgia', 'HI': 'Hawaii', 'ID': 'Idaho',
    'IL': 'Illinois', 'IN': 'Indiana', 'IA': 'Iowa', 'KS': 'Kansas',
    'KY': 'Kentucky', 'LA': 'Louisiana', 'ME': 'Maine', 'MD': 'Maryland',
    'MA': 'Massachusetts', 'MI': 'Michigan', 'MN': 'Minnesota', 'MS': 'Mississippi',
    'MO': 'Missouri', 'MT': 'Montana', 'NE': 'Nebraska', 'NV': 'Nevada',
    'NH': 'New Hampshire', 'NJ': 'New Jersey', 'NM': 'New Mexico', 'NY': 'New York',
    'NC': 'North Carolina', 'ND': 'North Dakota', 'OH': 'Ohio', 'OK': 'Oklahoma',
    'OR': 'Oregon', 'PA': 'Pennsylvania', 'RI': 'Rhode Island', 'SC': 'South Carolina',
    'SD': 'South Dakota', 'TN': 'Tennessee', 'TX': 'Texas', 'UT': 'Utah',
    'VT': 'Vermont', 'VA': 'Virginia', 'WA': 'Washington', 'WV': 'West Virginia',
    'WI': 'Wisconsin', 'WY': 'Wyoming', 'DC': 'District of Columbia',
}


async def fill_location_autocomplete(page: Page, profile: dict, result: dict):
    """Greenhouse's Location (City) is a typeahead: type the city, pick the
    suggestion matching the profile's state. The chosen value renders as
    container text, NOT input.value — verify there."""
    city = profile.get('city') or (profile.get('location', '').split(',')[0].strip())
    if not city:
        return
    state = STATE_NAMES.get((profile.get('state') or '').upper(), profile.get('state', ''))
    abbrev = (profile.get('state') or '').strip()

    def _is_home_state(t):
        # suggestions render either the full name ("<city>, NJ,
        # United States") or the abbreviation ("<city>, NJ, USA") —
        # accept both, word-bounded so NJ != "NJuna"
        tl = t.lower()
        if state and state.lower() in tl:
            return True
        return bool(abbrev) and bool(
            re.search(rf'\b{re.escape(abbrev)}\b', t, re.I))

    def _state_pick(ts):
        if not (state or abbrev):
            return None
        p = next((i for i, t in enumerate(ts)
                  if _is_home_state(t)
                  and t.lower().startswith(city.lower())), None)
        if p is None:
            p = next((i for i, t in enumerate(ts) if _is_home_state(t)), None)
        return p

    for selector in ('input#candidate-location', 'input[id*="location" i]',
                     'input[name*="location" i]', 'input[placeholder*="location" i]',
                     'input[aria-label*="location" i]'):
        el = page.locator(selector).first
        try:
            if await el.count() == 0 or not await el.is_visible():
                continue
            # Greenhouse question inputs carry their full question text as the
            # accessible label ("...employee's full name, location, and
            # position") and match these substring selectors. Same rule as
            # _profile_value_for: long label = real question, hands off.
            label_text = await el.evaluate(
                "el => (el.getAttribute('aria-label') || el.labels?.[0]?.innerText"
                " || el.getAttribute('placeholder') || '').trim()")
            if len(label_text) > 40 or 'referr' in label_text.lower():
                continue
            container_text = await el.evaluate(
                "el => (el.closest('div[class]')?.parentElement?.innerText || '').trim()")
            if city.lower() in container_text.lower():
                # already chosen — but only trust it if it's OUR state.
                # fill_profile_details once picked Manalapan FL and this
                # early-return then locked the wrong value in (8/23)
                if not (state or abbrev) or _is_home_state(container_text):
                    return
            await el.fill('')
            await el.click()
            await el.press_sequentially(city, delay=50)
            await asyncio.sleep(2.5)
            opts = page.locator('[role="option"]:visible')
            texts = [(await opts.nth(i).inner_text()).strip()
                     for i in range(min(await opts.count(), 10))]
            if not texts:
                # No suggestions = not the typeahead (plain text field caught
                # by a broad selector). Undo the typing and keep looking.
                await el.fill('')
                continue
            pick = _state_pick(texts)
            if pick is None and (state or abbrev):
                # city-only query ranked another state first (Manalapan FL
                # beat Manalapan NJ, 8/22) — re-query with the state in it
                # before ever settling for a wrong-state suggestion
                await el.fill('')
                await el.press_sequentially(f"{city}, {abbrev or state}", delay=50)
                await asyncio.sleep(2.5)
                texts = [(await opts.nth(i).inner_text()).strip()
                         for i in range(min(await opts.count(), 10))]
                pick = _state_pick(texts)
            if pick is None and texts:
                pick = 0
            if pick is not None:
                await opts.nth(pick).click()
                await asyncio.sleep(1)
            chosen = await el.evaluate(
                "el => (el.closest('div[class]')?.parentElement?.innerText || '').trim()")
            if city.lower() in chosen.lower():
                result['filled'].append(f'location:{chosen[:40]}')
                qa_note('typeahead', 'Location (City)', chosen[:60], 'profile')
            else:
                result['missed'].append('location')
                qa_note('typeahead', 'Location (City)', '', 'MISSED')
            return
        except Exception:
            continue


def _is_date_part_label(label: str) -> bool:
    l = (label or '').lower()
    return len(l) <= 30 and ('month' in l or 'year' in l) and ('start' in l or 'end' in l)


def _profile_value_for(label: str, profile: dict) -> str:
    """Best-effort profile value for a required field the first pass skipped —
    employment/education blocks, address parts, salary, source."""
    l = (label or '').lower()
    if l.strip().startswith(CONDITIONAL_LABEL_PREFIXES):
        return ''
    exp = (profile.get('experience') or [{}])[0]
    edu = (profile.get('education') or [{}])[0]

    def date_part(raw: str, part: str) -> str:
        raw = raw or ''
        if raw.strip().lower() == 'present':
            # ongoing role: greenhouse rejects future/current end dates —
            # the "Current role" checkbox is the right answer, not a date
            return ''
        bits = raw.replace('-', ' ').split()
        if part == 'month':
            return next((b for b in bits if b.isalpha()), '')
        return next((b for b in bits if b.isdigit() and len(b) == 4), '')

    # employment/education date parts absolutely first — "Start date year" must
    # never be treated as a "when can you start" question
    if len(l) <= 30 and ('month' in l or 'year' in l) and ('start' in l or 'end' in l):
        part = 'month' if 'month' in l else 'year'
        return date_part(exp.get('start') if 'start' in l else exp.get('end'), part)

    # explicit question patterns next — a long label is a question, and
    # letting field rules see it stuffs identity data into yes/no dropdowns
    # ("relatives employed by this ORGANIZATION" is not a company-name field)
    # BEFORE the answer table: "Preferred First Name" is a TEXT field, but
    # the table's preferred-name->No rule (meant for "do you have a preferred
    # name?" dropdowns) put the literal string "No" into it on 8 QA records
    if 'preferred' in l and 'name' in l and not l.strip().startswith(('do ', 'have ')):
        return profile.get('last_name', '') if 'last' in l else profile.get('first_name', '')

    wants = _desired_options(l)
    if wants:
        return wants[0].title() if wants[0] in ('yes', 'no', 'united states') else ''
    if 'salary' in l or 'compensation' in l or ('hourly' in l and (
            'rate' in l or 'pay' in l or 'wage' in l)) or ('rate' in l and 'pay' in l):
        annual = profile.get('desired_salary', '')
        # "salary requirements (hourly rate)" got 120000/hr on 8/23 —
        # convert annual to an hourly figure when the label asks for one
        if 'hourly' in l or 'per hour' in l or '/hr' in l:
            try:
                return str(round(float(str(annual).replace(',', '')) / 2080))
            except (TypeError, ValueError):
                return ''
        return str(annual)
    if 'hear about' in l or 'how did you' in l:
        return 'LinkedIn'
    # identity fields that boards love to wrap in a sentence of instructions —
    # safe past the length guard because the keyword pins the meaning
    if 'phone' in l and 'number' in l:
        return profile.get('phone', '')
    if 'linkedin' in l:
        return profile.get('linkedin', '')
    if 'working location' in l or 'current location' in l:
        return f"{profile.get('city', '')}, {profile.get('state', '')}"
    if 'availability' in l and 'work' in l:
        return 'Full-time; available to start within two weeks'
    if len(l) > 40:
        return ''  # unmatched real question — leave it to the ai fallback

    # short QUESTIONS slip past the len>40 guard and hit field rules —
    # "Are you open to an hourly position?" was answering with his job
    # title via the 'position' rule. Interrogatives are never field labels.
    if re.match(r'\s*(are|do|did|have|has|will|would|can|could|is|was)\s+(you|your|there)\b', l):
        return ''
    # conditional follow-ups ("If yes, when and which company?") must stay
    # blank when the parent answer was No — 18 QA records had HMH typed into
    # "which company" after "have you ever worked for X?" = No
    if re.match(r'\s*if\s+(yes|so|applicable|any)\b', l):
        return ''
    if 'company' in l or 'employer' in l or 'organization' in l:
        return exp.get('company') or profile.get('current_company', '')
    if 'school' in l or 'university' in l or 'institution' in l:
        return edu.get('school', '')
    if 'degree' in l:
        return edu.get('degree', '')
    if 'discipline' in l or 'major' in l or 'field of study' in l:
        return edu.get('field', '')
    if 'title' in l or 'position' in l or 'role' in l:
        return exp.get('title') or profile.get('current_title', '')
    if 'start' in l:
        return date_part(exp.get('start'), 'month' if 'month' in l else 'year' if 'year' in l else 'month')
    if 'end' in l or 'graduation' in l:
        return date_part(exp.get('end'), 'month' if 'month' in l else 'year' if 'year' in l else 'year')
    if 'state' in l or 'province' in l:
        return STATE_NAMES.get((profile.get('state') or '').upper(), profile.get('state', ''))
    if 'location' in l:
        # "Location (City)" typeaheads: a bare city query ranks Manalapan FL
        # above Manalapan NJ (8/23 screenshot) — always qualify with the state
        return f"{profile.get('city', '')}, {profile.get('state', '')}"
    if 'city' in l:
        return profile.get('city', '')
    if 'zip' in l or 'postal' in l:
        return profile.get('zip', '')
    if 'address' in l:
        return profile.get('address1', '')
    if 'country' in l:
        return profile.get('country', 'United States')
    if 'pay' in l:
        return str(profile.get('desired_salary', ''))
    return ''


_JS_IS_INVALID = """el => {
  if (el.getAttribute('aria-invalid') === 'true') return true;
  let p = el.parentElement;
  for (let d = 0; d < 4 && p; d++, p = p.parentElement) {
    const t = (p.innerText || '');
    if (t.length < 400 && /field is required|is required|please select|please enter/i.test(t)) return true;
  }
  return false;
}"""


async def _pick_visible_option(page: Page, wants: list, fallback_first: bool = True) -> bool:
    """Pick from an open listbox: first option matching wants, else the first
    non-placeholder option."""
    await asyncio.sleep(0.6)
    opts = page.locator('[role="option"]:visible')
    texts = [(await opts.nth(i).inner_text()).strip()
             for i in range(min(await opts.count(), 30))]
    pick = None
    for want in wants:
        pick = next((i for i, t in enumerate(texts) if want.lower() in t.lower()), None)
        if pick is not None:
            break
    if pick is None and fallback_first:
        pick = next((i for i, t in enumerate(texts)
                     if t and not re.search(r'select|choose', t, re.I)), None)
    if pick is not None:
        await opts.nth(pick).click()
        return True
    await page.keyboard.press('Escape')
    return False


async def repair_required_fields(page: Page, profile: dict, answer_fn=None) -> int:
    """After a blocked submit, greenhouse flags every unanswered required
    control (aria-invalid / 'this field is required'). Answer each from the
    profile, the dropdown answer table, or answer_fn(label). Returns count."""
    fixed = 0
    controls = await page.locator(
        'input:visible, textarea:visible, select:visible, '
        'button[aria-haspopup="listbox"]:visible').all()
    for el in controls:
        try:
            if not await el.evaluate(_JS_IS_INVALID):
                continue
            tag = await el.evaluate("el => el.tagName.toLowerCase()")
            typ = (await el.get_attribute('type') or '').lower()
            role = (await el.get_attribute('role') or '').lower()
            label = ((await el.evaluate(_JS_LABEL)) or '').strip()
            value = _profile_value_for(label, profile)

            source = 'profile' if value else 'first-option'

            if tag == 'select':
                wants = ([value] if value else []) + (_desired_options(label) or [])
                opts = await el.evaluate("el => Array.from(el.options).map(o => o.textContent)")
                pick = None
                for want in wants:
                    pick = next((o for o in opts if want.lower() in o.lower()), None)
                    if pick:
                        break
                if not pick:
                    pick = next((o for o in opts if o.strip()
                                 and not re.search(r'select|choose|--', o, re.I)), None)
                if pick:
                    await el.select_option(label=pick.strip())
                    fixed += 1
                    qa_note('select', label, pick.strip(), f'repair:{source}')
            elif typ == 'checkbox':
                await el.check()
                fixed += 1
                qa_note('checkbox', label, 'checked', 'repair:required')
            elif tag == 'button' or (role == 'combobox' and tag != 'input'):
                await el.click()
                wants = ([value] if value else []) + (_desired_options(label) or [])
                if await _pick_visible_option(page, wants):
                    fixed += 1
                    qa_note('combobox', label, value or '(first option)', f'repair:{source}')
            elif tag == 'input' and (
                    role == 'combobox'
                    or (await el.get_attribute('aria-autocomplete') or '')
                    or (await el.get_attribute('aria-haspopup') or '')
                    or await el.get_attribute('aria-expanded') is not None):
                # react-select typeahead: type then pick a suggestion
                if not value and answer_fn and not _is_date_part_label(label):
                    value = answer_fn(label) or ''
                    source = 'ai'
                await el.click()
                if value:
                    await el.press_sequentially(str(value)[:40], delay=40)
                wants = ([value] if value else []) + (_desired_options(label) or [])
                picked = await _pick_visible_option(page, wants)
                if not picked and value:
                    # typed text may have filtered the list to nothing —
                    # clear it and pick from the unfiltered options
                    await el.click()
                    await page.keyboard.press('Control+A')
                    await page.keyboard.press('Delete')
                    await asyncio.sleep(0.4)
                    picked = await _pick_visible_option(page, wants)
                if picked:
                    fixed += 1
                    qa_note('typeahead', label, value or '(first option)', f'repair:{source}')
            elif tag in ('input', 'textarea') and typ not in ('file', 'radio', 'button', 'submit'):
                if not value and answer_fn and not _is_date_part_label(label):
                    value = answer_fn(label)
                    source = 'ai'
                if value:
                    await el.fill(str(value))
                    fixed += 1
                    qa_note(tag, label, value, f'repair:{source}')
        except Exception:
            try:
                await page.keyboard.press('Escape')
            except Exception:
                pass
    return fixed


async def fill_profile_details(page: Page, profile: dict, result: dict):
    """Proactively fill empty profile-backed fields the platform handlers
    don't know — education block, employment extras. Same answering logic as
    the repair sweep, keyed on 'empty + short label maps to profile' instead
    of 'flagged required', so optional sections don't ship blank."""
    for el in await page.locator('input:visible, select:visible').all():
        try:
            tag = await el.evaluate("el => el.tagName.toLowerCase()")
            typ = (await el.get_attribute('type') or '').lower()
            label = ((await el.evaluate(_JS_LABEL)) or '').strip()
            if typ == 'checkbox':
                # ongoing role: "Current role" replaces the end date
                exp_end = ((profile.get('experience') or [{}])[0].get('end') or '')
                if (exp_end.lower() == 'present'
                        and any(k in label.lower() for k in ('current role', 'currently work'))
                        and not await el.is_checked()):
                    await el.check()
                    qa_note('checkbox', label, 'checked', 'profile-detail')
                # required agreement/consent boxes ("I agree to the Application
                # Acknowledgement", "consent to the processing of...") — these
                # block the submit until checked
                elif (any(k in label.lower() for k in
                          ('agree', 'acknowledg', 'consent', 'certify',
                           'authorize', 'receive updates'))
                        and not await el.is_checked()):
                    await el.check()
                    qa_note('checkbox', label, 'checked', 'agreement')
                continue
            if typ in ('radio', 'file', 'submit', 'button', 'email', 'tel'):
                continue
            value = _profile_value_for(label, profile)
            if not value:
                continue
            try:
                if await el.input_value():
                    continue
            except Exception:
                continue
            if tag == 'select':
                opts = await el.evaluate("el => Array.from(el.options).map(o => o.textContent)")
                first_word = value.split()[0].lower()
                pick = next((o for o in opts if value.lower() in o.lower()), None) or \
                    (next((o for o in opts if len(first_word) > 3 and first_word in o.lower()), None))
                if pick:
                    await el.select_option(label=pick.strip())
                    qa_note('select', label, pick.strip(), 'profile-detail')
            elif (await el.get_attribute('role') or '') == 'combobox' \
                    or (await el.get_attribute('aria-autocomplete') or '') \
                    or (await el.get_attribute('aria-haspopup') or '') \
                    or await el.get_attribute('aria-expanded') is not None:
                await el.click()
                await el.press_sequentially(str(value)[:40], delay=40)
                wants = [value]
                if 'location' in label.lower():
                    # suggestions may spell the state either way — accept
                    # "<city>, NJ, USA" and "<city>, NJ, ..."
                    # but never a wrong-state match (Manalapan FL, 8/23)
                    ab = (profile.get('state') or '').strip()
                    full = STATE_NAMES.get(ab.upper(), ab)
                    ct = profile.get('city', '')
                    wants = [f"{ct}, {ab}", f"{ct}, {full}", full]
                if await _pick_visible_option(page, wants, fallback_first=False):
                    qa_note('typeahead', label, value, 'profile-detail')
                else:
                    await page.keyboard.press('Escape')
            else:
                await el.fill(str(value))
                qa_note('input', label, value, 'profile-detail')
        except Exception:
            try:
                await page.keyboard.press('Escape')
            except Exception:
                pass


async def audit_required_fields(page: Page) -> list:
    """Read-only sweep after the final submit attempt: labels of controls
    still flagged required/invalid — i.e. the questions we could not answer."""
    missing, seen = [], set()
    controls = await page.locator(
        'input:visible, textarea:visible, select:visible, '
        'button[aria-haspopup="listbox"]:visible').all()
    for el in controls:
        try:
            if not await el.evaluate(_JS_IS_INVALID):
                continue
            tag = await el.evaluate("el => el.tagName.toLowerCase()")
            label = ((await el.evaluate(_JS_LABEL)) or '').strip()[:120]
            if label and label not in seen:
                seen.add(label)
                missing.append({'kind': tag, 'label': label})
        except Exception:
            continue
    return missing


async def handle_sponsorship_question(page: Page, profile: dict):
    """Handle work authorization / sponsorship questions."""
    # Common patterns
    auth_patterns = [
        'authorized to work',
        'work authorization',
        'legally authorized',
        'eligible to work',
    ]
    sponsorship_patterns = [
        'sponsorship',
        'visa',
        'require sponsorship',
        'need sponsorship',
    ]

    # Find radio buttons or selects related to authorization
    radios = await page.locator('input[type="radio"]').all()
    for radio in radios:
        try:
            label = page.locator(f'label[for="{await radio.get_attribute("id")}"]')
            if await label.count() > 0:
                text = (await label.first.inner_text()).lower()
                name = (await radio.get_attribute('name') or '').lower()

                # Work authorization - select "Yes"
                for pattern in auth_patterns:
                    if pattern in name or pattern in text:
                        if 'yes' in text:
                            await safe_click(radio)

                # Sponsorship - select "No"
                for pattern in sponsorship_patterns:
                    if pattern in name or pattern in text:
                        if 'no' in text:
                            await safe_click(radio)
        except Exception:
            continue


async def handle_eeo_questions(page: Page):
    """Handle optional EEO/demographic questions - decline to answer."""
    decline_options = page.locator(
        'option:has-text("Decline"), option:has-text("Prefer not"), '
        'input[value*="decline"], input[value*="prefer not"]'
    )
    count = await decline_options.count()
    for i in range(count):
        try:
            await safe_click(decline_options.nth(i))
        except Exception:
            continue


# Main dispatcher
async def fill_application(page: Page, profile: dict) -> dict:
    """Detect ATS and fill the application form."""
    ats = await detect_ats(page)

    handlers = {
        'greenhouse': fill_greenhouse,
        'lever': fill_lever,
        'workday': fill_workday,
        'ashby': fill_ashby,
        'generic': fill_generic,
        'linkedin': fill_generic,
        'indeed': fill_generic,
    }

    handler = handlers.get(ats, fill_generic)
    result = await handler(page, profile)

    # human-pace: brief think-pauses between form sections instead of
    # machine-gunning every sweep back to back
    from pacing import human_pause
    import asyncio as _aio

    # Always try these
    await fill_location_autocomplete(page, profile, result)
    await _aio.sleep(human_pause())
    await fill_profile_details(page, profile, result)
    await _aio.sleep(human_pause())
    await answer_dropdowns(page, profile, result)
    # second pass: catches comboboxes that were blocked by an open
    # overlay/portal during the first sweep
    await answer_dropdowns(page, profile, result)
    await _aio.sleep(human_pause())
    await handle_sponsorship_question(page, profile)
    await answer_radio_groups(page, profile, result)
    await handle_eeo_questions(page)

    return result
