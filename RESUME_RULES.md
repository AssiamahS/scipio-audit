# Resume rules (hard, enforced by `auto-apply/resume_rules.py`)

Every resume scipio sends, static or tailored, must pass these. A failure
blocks the application (`blocked_rules`), it never "sends the best we have".
Sources: Harvard FAS Career Services resume guide (2024/25), Jobscan "Resume
Work Experience" (2025, Fortune-500 ATS study), Google XYZ formula (Laszlo
Bock; danbentivenga.substack.com 2025), The Random Recruiter (Substack, 2026),
Workplace StackExchange (short stints: 147646, consolidation: 168442),
finaldraftresumes.com + resumesolving.com bullet-count guides (2026, both
built from r/resumes threads), and Kevin Manu's JPMC resume (the benchmark
that moved him to Microsoft; see memory reference-kevin-manu-resume-benchmark).

## 1. Structure (ATS parse-safe)
- Single column, no tables, no text boxes, no graphics, no photo. PDF only.
- Standard headers in this order: SUMMARY, EXPERIENCE, SKILLS, EDUCATION,
  CERTIFICATIONS. Nothing creative ("My Journey").
- Contact line directly under the name: email | phone | linkedin | github.
- Reverse chronological. Each entry: **Title** first (what recruiters search),
  then Company, Location, Dates. Dates are `Month YYYY – Month YYYY` or
  `Month YYYY – Present`, identical format on every entry.
- One page. Word budget 700. Font 9–11pt, margins ≥ 0.35in.
- Filename uploaded to an ATS: `Sylvester-Assiamah-Resume.pdf`. Never a
  version number, never "final".

## 2. Bullet count (Relevance + Recency + Responsibility)
- Current role: 4–6 bullets.
- Previous roles within 5 years: 3–5 bullets each; a role of 5–18 months
  carries 2–4 (two strong metric bullets beat a re-worded duplicate).
- Short stints (≤ 4 months) and roles 7+ years back: 2–3 bullets. Never 0,
  never hidden (a gap gets asked about; a short job with two outcome bullets
  does not).
- Every employer/engagement gets its own bullets under its own header. No
  pooling three employers' bullets under one line.
- Whole resume: 15–26 bullets. Above that the reader is doing unpaid labor.
- No role may have more bullets than the role after it in time (older ≤ newer).

## 3. Bullet content (XYZ: outcome, number, method)
- Every bullet opens with a strong verb (past tense for past roles, present
  for the current one). Banned: responsible for, duties included, helped
  with, worked on, spearheaded, leveraged, orchestrated, synergy,
  results-driven, passionate, seamlessly, dynamic.
- ≥ 80% of bullets carry a number (%, $, count, time). The FIRST bullet of
  every role carries one. Scope counts when an exact figure is not known
  (200+ staff, 4 accounts, 30+ assets/month).
- Name the tool or system inside the bullet (ServiceNow, SQL, Epic, MLR
  review) so keyword matching happens in context, not only in SKILLS.
- One result per bullet. 1–2 lines, ≤ 230 characters.
- Only approved bank text, verbatim. Tailoring selects and orders; it never
  rewrites. A claim contradicting the career store (linkedin-mcp) is wrong
  no matter how well it reads.

## 4. Titles and consistency
- The title on each entry is the real title held there. A grouped contract
  period carries ONE umbrella title in the title slot and the real title
  under each engagement. Never a slash-title that contradicts the
  engagement titles beneath it.
- The headline under the name mirrors the target job title only when it is
  one Sylvester has honestly held (bank `target_titles`).
- Summary claims must attribute to the right employer (8 campaigns =
  Heartbeat, 4 accounts = FCB, Cosentyx = Catalyst).
- Every skill listed must be backed somewhere in the experience or
  certifications, or be a tool he uses at HMH today. No skill appears twice.

## 5. Tailoring
- Keyword echo ≥ 70% against the JD lexicon or the application is skipped
  as weak_match (existing fit gate).
- Within each role, order by JD relevance, but the strongest metric bullet
  stays first.
- Keep a master bank; tailor per submission; log which artifact went where.

## 6. Delivery
- Text extracts cleanly (pypdf), no embedded images, ≤ 200KB.
- A resume that fails any rule above is not sent. Fix the bank, then retry.
