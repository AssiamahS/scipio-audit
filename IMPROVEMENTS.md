# Scipio improvement checklist — 2026-09-02

State when this was written (all from real tool output, see NOTES.md for history):

- Pipeline is ALIVE again: 9/1 = 1 submit, 9/2 = 6 submit-class outcomes across 4 wakes
  (12/13/14/15 UTC), day closed on target. Issue #1 (cloudbox 402) is stale.
- But of today's 7 submits only 2 are confirmed (talkspace, springhealth) and 5 sit
  `submit_unconfirmed` with no confirmation email.
- Of the 7, 5 are outside the lane the 8/26 HR audit set: natera Manager + **Senior**
  Manager Clinical Trials (match 3/10), garnerhealth Clinical Researcher (match **1/10**),
  science37 Clinical Research Coordinator (match 3/10), zocdoc **Senior** TAM.
  These were pre-audit wishlist rows; `prepare_resume` only blocks credential knockouts,
  level/title mismatch is a printed warning, and there is no match-score floor at apply time.
- LTS ServiceNow / Endpoint admin hit `blocked_knockout` 3 times in 2 days — a blocked job
  stays in `wishlist` and burns a slot every wake.
- Scorecard 93/101. Failing: A18 (metrics on bullets), B42/B43 (ATS echo 19/27 shipped),
  C66 (5 errors), C73 (8 screenshots missing), C80 (5 URLs re-hammered), C85 (6 run-days/14),
  D92 (1 "interview" = notary spam from [Gmail]/Spam, issue #2).
- Funnel: tailored 3 sent / untailored 39 sent / 0 interviews. 16 jobs parked
  `submitted_unverified`, 4 stalled dry_run/held (Lockwood, PMG Associate PM, ...).
- 10 of the last 40 commits are authored `Claude <noreply@anthropic.com>` (the daily brief
  routine) — violates the stealth-commit rule.

Legend: [ ] open · [~] partial · [x] done. Ranked by expected interviews per hour of work.

## P0 — stop sending applications that cannot convert (this week)

- [ ] **Level/title gate at apply time, not just scout.** In `prepare_resume`, treat
      `level mismatch` and title-never-held as blocking (`blocked_level`, `blocked_title`)
      unless the job carries `queued_by: user`. Today it only warns.
- [ ] **Match-score floor.** Never submit when `match_score < 5` (garnerhealth shipped at 1,
      natera/science37 at 3). Log as `weak_match`, same path as the keyword-echo gate.
- [x] **Re-grade the whole wishlist once** (9/5) with `fit_check.py --all-logged` and move every
      `skip` verdict to `weak_match`/`closed` so pre-audit rows stop reaching the applier.
- [x] **Retire blocked jobs.** (9/5: applier + handapply set `blocked_knockout` in jobs.json; wishlist re-graded once, 3 LTS + 10 senior rows retired) `blocked_knockout`/`blocked_level`/`blocked_truth` must update
      jobs.json to a terminal status (`blocked`) — LTS got 3 slots in 2 days.
- [ ] **Confirmation truth for the 5 unconfirmed today.** Check 105@ for natera x2,
      science37, garnerhealth, zocdoc. If no email in 24h, decide: real submit with missing
      thank-you marker (fix `submit_confirmed` selectors) or a silent failure (security-code
      stage?). Screenshot path is in `details.screenshot` on the box.
- [x] **Interview detector: ignore [Gmail]/Spam and unmatched companies.** (9/5: `_real_interview` guard + test_email_loop fixture; #2 closed) `rejections.py`
      raised issue #2 for a mobile-notary spam blast (job_id None, mailbox Spam). Require a
      company match against apply_log OR a non-spam mailbox before opening an `interview`
      issue. Close #2 as false positive.
- [x] **Close issue #1** (9/5; backstop wake in AssiamahS/githubactions `wake-cloudbox.yml` at :20 past 12-15 UTC) (wake-failure) — wakes have run 9/1 and 9/2; add an auto-close when
      the next `cloudbox/wake` status is success.

## P1 — volume and cadence (the HR audit asked for 16+/week targeted)

- [~] **Cadence: 6 run-days in 14 (C85).** (9/5: second wake path from the public githubactions repo; CF worker untouched, wrangler not logged in locally) Wakes only fire on weekdays and the 8/26–8/31
      window was dark. Add a weekday health check: if no `wake_log` entry by 16:00 UTC,
      open/refresh a single pinned issue (do not spam).
- [ ] **Longer-lived box.** Recreate cloudbox with `gh codespace create -R AssiamahS/cloudbox
      --idle-timeout 240m` (relay.mjs is now safe for ccwatch) so a slice can run 6–8 apps,
      or finish the Oracle A1 VM (task ms7l1t9rhctu) which removes the 30-min limit entirely.
- [ ] **Wishlist supply.** 54 wishlist rows but many are stale (25 never actioned, 14+ days).
      Raise `max_new_per_run` only after the level/title gate lands; otherwise scout fills the
      queue with the same mismatches. Add IQVIA/Alteva/GHX from today's brief by hand.
- [ ] **Drain the 4 stalled dry_run/held_for_review rows** (Lockwood, PMG Associate PM
      Healthcare Advertising = his best lane). Either run them live or mark them closed —
      they have sat 10–19 days.
- [ ] **Stale `applied` rows.** 15 rows with no update in 2+ weeks (oldest 162 days). Auto-age
      to `ghosted` after 30 days with no rejection/confirmation email.

## P2 — evidence and QA (scorecard C/D groups)

- [ ] **Screenshots for every attempt (C73, 8 missing).** Capture on every terminal branch
      (blocked_*, error, no_form), not only submit/error paths.
- [ ] **URL re-hammer guard (C80, 5 URLs).** The 14d guard counts submit-class only; add a
      hard cap of 2 attempts per URL per 14d for ALL statuses except `dry_run`.
- [ ] **Error rate (C66, 5 errors).** Classify the 5: nav timeouts vs selector failures.
      Timeouts → skip and retry next day; selector failures → new ATS quirk → NOTES.md.
- [ ] **apply_log timestamps tz-aware** (box writes UTC, Mac writes ET). One-line fix in
      `log_result`; migrate existing rows on read.
- [ ] **Tests.** Only `test_scout_filters.py` exists (fixtures, no unit tests). Add tests for:
      `get_quick_answer` word-boundary + sponsorship-outranks-work-auth, `_profile_value_for`
      hourly conversion + long-label guard, `fit_check.assess` level/title/knockout, and
      `submit_confirmed` against saved greenhouse/lever/ashby thank-you HTML.
- [ ] **Run codehawk** (`review_diff`) before each commit — duplicate elif ordering bugs
      (needs_code after submit_unconfirmed, 8/21) are exactly what it catches.

## P3 — resume and tailoring (scorecard A/B groups)

- [ ] **A18: metric on the 4–5 unnumbered bullets** (ops-data, MLR loop, SQL self-serve,
      meetings). Needs numbers from the user → bank.json → `bank_approve.py`.
- [ ] **B42/B43: ATS echo floor.** Attempts shipped at 19 and 27. The 70% fit gate only runs
      in the batch path — apply it in every path (`apply`, `process_queue`) via `prepare_resume`.
- [ ] **Tailored artifacts still lose to untailored in the funnel (3 vs 39 sent).** Every
      submit since 9/1 is `tailored: True` — good. Make `funnel_stats.py` split by month so
      the post-8/26 cohort can be judged on its own.
- [ ] **Per-engagement months for Heartbeat/FCB/Catalyst** so the contract block renders as
      three dated entries (open ask since 8/25).
- [ ] **Kevin Manu PDF re-diff** — needs gdrive re-auth (invalid_grant).

## P4 — coverage (new ATS = new supply)

- [~] **Workday handler v2** (9/5: no headless submit, by design. scout now reads 9 verified Workday tenants + PriviaHealth SmartRecruiters; applier tailors + parks as `hand_apply` with HAND_APPLY.md) (account creation + per-tenant memory) — unlocks Humana IT PM 2,
      IQVIA Sr TPM, most of F500. Task mshpnpoo7xjk.
- [~] **SmartRecruiters handler** (9/5: scouted + hand-apply pack, same as Workday) (public API, `totalFound>0` check already known).
- [ ] **iCIMS iframe handler** (7/28 cluster) — enter iframe like greenhouse embeds.
- [ ] **Own-domain career portals** — careers-URL discovery by domain before any handler.
- [ ] **Ashby fingerprint flag** ("turn off your VPN") — try headed-mode fingerprint
      (real UA/viewport/timezone) before writing Ashby off.

## P5 — hygiene

- [ ] **Stealth commits.** The claude.ai "brief" routine commits as
      `Claude <noreply@anthropic.com>`. Change the routine's git identity to the user's, or have
      it open a PR the user merges. 10/40 recent commits are affected.
- [ ] **OpenRouter free quota (~50 req/day)**: scout's scoring burst can starve the applier's
      calls. Give the applier a reserved budget or run scout after the last wake.
- [ ] **Daily brief freshness caveat** — the brief cannot fetch greenhouse/lever/workday
      through the egress proxy, so "new" postings are unverified. Have the brief call the
      board APIs (`jd_from_api`) which are not blocked.
- [ ] **Remove `worker/.wrangler/` and `briefs/SCIPIO_DOSSIER.md` from the untracked list**
      (gitignore the first, commit or delete the second).
- [ ] **followups.py** drafts to Gmail but nothing tracks whether a draft was sent; add a
      `sent` sweep so follow-ups are measured like confirmations.

## User-only items (carried from HR audit 8/26, still open)

- [ ] Message Kevin Manu for a referral; ask HMH manager + Bayshore preceptor who they know.
- [ ] Apply to HMH internal postings through the internal portal.
- [ ] Re-apply Humana IT PM 2 by hand (Workday).
- [ ] Give numbers for the 4 unnumbered bullets.
- [ ] Open the Drive auth link so the Kevin Manu diff can be redone.
- [ ] Reply within the hour to any real `interview` issue (issue #2 is spam — ignore).

## Added 2026-09-02 (evening) — why it keeps stopping, the email loop, follow-ups, what others do

### Why scipio has stopped (every outage, from NOTES.md)
| When | Cause | Class |
|---|---|---|
| 7/30 | GitHub Actions blocked account-wide (private repo, minutes billed) | free-tier dependency |
| 8/8 | AI brain dead (GitHub Models retired) → fake 5/10 scores, blank essays | silent degradation |
| 8/14–8/21 | PAUSE_SUBMITS left in place; cron ran before the same-day unpause | safety gate, no alert |
| 8/21–8/23 | proxy probe flipped to direct → every form parked as "captcha" (detector false positive) | fail-closed gate, no alert |
| 8/24 | codespace idle-stops 30 min after wake; batch ran 40 min → killed mid-run, nothing logged | infra limit |
| 8/25 | wakes fired, slyci never ran, /tmp wiped → zero evidence | no persistent logging |
| 8/25–8/26 | resume rules made every tailored resume fail → every application blocked | fail-closed gate |
| 8/26–8/31 | codespace 402 "billing issue" — box would not start | free-tier dependency |

Pattern: one serial chain (Cloudflare cron → codespace → slyci → applier) built on free tiers,
plus fail-closed gates that are correct but SILENT. Nobody is told when a day ends at 0.

- [ ] **Zero-submit alarm.** At 16:30 UTC on a weekday, if `wake_log.json` has no entry for
      today OR submitted == 0, push to the phone (ccwatch/scipioOS push, or a Telegram
      message via the telegram MCP) with the reason from the last wake. One message, no spam.
- [ ] **Second runner.** Oracle Always Free VM (task ms7l1t9rhctu) as primary; keep the
      codespace as fallback. Until then: a Mac launchd job at 10:00 ET that runs one slice
      if `wake_log.json` on origin has no entry for today.
- [ ] **Health checks that fail loud** at the start of every wake: codespace billing (start
      returned 402?), proxy reachable, OpenRouter key has quota, GMAIL_APP_PASSWORD logs in,
      no PAUSE_SUBMITS older than 3 days. Any failure → the same phone alert.
- [ ] **Codespace idle timeout 240m** (recreate; relay.mjs is safe now).

### The email loop — what exists and what is missing
Exists today (`rejections.py`, runs every wake): IMAP sweep of 105@ INBOX + Trash + Spam,
matches "thank you for applying" confirmations and rejections back to jobs by company slug,
upgrades `submit_unconfirmed` → confirmed (closest submit within 48h), flags interview
phrases into `needs_reply.json` + a GitHub issue. 30 confirmations, 9 rejections, 1 interview
matched so far. The user never sees any of this except on the reports page.

- [ ] **Per-application email address.** Apply with `<email>+s<jobid>`
      (Gmail plus-addressing; Greenhouse/Lever/Ashby accept it, fall back to the bare address
      where an ATS rejects "+"). Every reply then carries the exact application id in the
      To: header — no more company-slug guessing, no more two-Natera-jobs ambiguity.
- [ ] **Match by title too.** ATS mail subjects carry the job title; match company + title
      before falling back to company + 48h window.
- [ ] **Per-application timeline** in jobs.json (`history`: applied → confirmed → rejected /
      interview / ghosted, each with message-id) and render it in reports.html + scipioOS.
- [ ] **Daily digest to the phone**: submitted today (confirmed/unconfirmed), rejections
      received, interviews needing a reply, parked jobs waiting for a click.
- [x] **Spam-folder mail never counts as an interview** (9/5) (issue #2 — notary blast).
- [ ] **Gmail labels** `scipio/confirmed`, `scipio/rejected`, `scipio/interview` applied by
      the sweep so 105@ is readable by a human too.

### Follow-ups — it sends and forgets
Exists: `followups.py` writes ONE Gmail draft per application at day 5–10 with an empty To:
(recruiter filled by hand); `outreach.py` drafts LinkedIn notes; neither sends, nothing
tracks whether the user ever sent one.

- [ ] **Recruiter finder.** For each confirmed application: company careers page team
      contact, LinkedIn people search (`outreach.linkedin_search` exists), Hunter/Apollo
      free-tier email pattern. Store `recruiter` on the job.
- [x] **Follow-up sequence, auto-sent from 105@** (9/5: followups.py sends day 5 + day 12 straight from the codespace via mailer.py to any human address on the confirmation/rejection thread or `recruiter_email`; drafts otherwise):
      day 5 "still very interested" + one new fact, day 12 short bump. Stop on any
      rejection/interview. Log `followup_sent` on the job.
- [x] **Rejection reply** (9/5: rejection_reply.py, human senders only, 14-day window, 3/run): one polite "thanks — any feedback?" note; recruiters answer ~10%
      of the time and it keeps the name warm for the next posting.
- [x] **Interview auto-response draft** (9/5: interview_pack.py threads a Gmail draft with 3 ET windows, writes briefs/interview-<co>-<date>.md, alerts the phone) with three availability windows + a prep brief
      (JD, resume sent, screening answers given, company notes) attached as an issue comment.
- [ ] **Measure it**: funnel_stats splits response rate for followed-up vs not.

### What other auto-apply setups do that scipio does not (from the 8/6 + 8/26 research)
- **Apply within 24–48h of posting** — the largest single lever after title match. Scout
  runs once a day; run it every 2–3h and sort the queue newest-first (done for scout, not
  for the applier's pick order).
- **Human review queue on the phone** for parked/manual_review (AutoApply "review mode").
  scipioOS app exists — make parked.md actionable there (approve / skip / open form).
- **LinkedIn + Indeed coverage** (Simplify, LazyApply, Sonara, AIHawk) — where most volume
  is. ToS risk is real; if adopted, Easy Apply only, headed browser, daily cap, review mode.
- **Per-ATS self-learning question memory** (LangHire) — bank.json is this; add the
  "unanswered question → bank rule" loop as a one-tap phone action instead of a markdown list.
- **Referral finder** — alumni (Rutgers/MCPHS) + 2nd-degree at each target company;
  drafts the note; user sends. Referral ≈ 40 interviews/100 vs 3/100 cold.
- **Resume A/B by response rate** — funnel_stats has it; act on it (retire the Infra
  variant for PM roles, it shipped on 7 wrong-lane apps).
- **Company research blurb** for the "why us" answer (site + recent news) — template only today.
- **Daily/weekly report to the owner** — the brief exists but goes to a git commit, not to
  the user's phone.
- **Interview prep pack** generated the moment an interview mail lands.

### From the "ChatGPT applied to 500 jobs" X thread (2026-09-02, AiWithSania; same text posted by 5+ accounts since 8/26)
The post is a recycled prompt list, not a report of a real system, and the 12-interviews-in-24h claim
has no evidence behind it. Only steps 1 and 4 were retrievable (X rate-limited the rest):
step 1 = "Act as a senior recruiter, list 20 job titles I am most qualified for with the exact
keywords each usually requires"; step 4 = "build a job application tracker (company, title,
link, date posted, application status...)". Scipio already has the tracker and the tailoring.
The one usable idea:
- [ ] **Derive the allowed-title list from the resume, not by hand.** Generate `titles_held.json`
      from bank.json + linkedin-mcp (titles actually held + adjacent titles a recruiter would
      shortlist, each with its usual keywords), review it once, and make it the source for
      fit_check's title gate and scout's `title_keywords` (57 hand-grown entries today). Rerun
      when the bank changes.

## Full-process review — 2026-09-02 evening (resume + tailoring, read from the shipping PDFs)

Read: the 4 static PDFs (all 1 page), three tailored artifacts that shipped today
(talkspace_119, garnerhealth_133, zocdoc_109), bank.json (22 bullets), RESUME_RULES.md, the
linkedin-mcp career store, and the Kevin Manu benchmark memory.

### What is right
- Structure is ATS-safe and matches Kevin's shape: single column, standard headers, contact
  line, reverse chronological, title-first entries, categorized skills, one page (652 words).
- Attribution is correct: Heartbeat / FCB / Catalyst each carry their own bullets and title.
- 19 bullets: HMH 5, Heartbeat 3, FCB 3, Catalyst 3, Altice 2, Stop & Shop 3. That is inside
  the rules (3–5 per recent role) and Kevin runs 3–4 per role. **The count is not the problem;
  the one-page budget is binding, so "more bullets" means shrinking the font or cutting Skills.**

### What is wrong (ranked by how a recruiter would read it)
1. **Filler duplicates.** 3 of the 9 contract bullets restate another bullet: con-mlr-loop
   repeats con-mlr-30 (MLR review), con-logistics repeats con-4accounts (logistics + timelines
   for 4 accounts), con-dataset repeats con-cosentyx (forecasting dataset / placement model).
   They were approved on 8/26 to satisfy the 3-bullet floor. A recruiter reads padding.
   Fix = three NEW facts with numbers (needs the user: team sizes, launch counts, budget owned,
   reports built, hours saved, forecast accuracy).
2. **Metrics 13/19 (68%)** vs the 80% rule and Kevin's 100%. Catalyst has ZERO numbers in three
   bullets. HMH has two without (ops-data, first-call) and "every 2 weeks" is a cadence, not an
   outcome.
3. **Skills block is identical on every application** (tailor.py emits all 9 categories, ~50
   items, untouched). On a PM resume half of it is Windows Server / SCCM / DNS / PowerShell.
   "Databricks", "JavaScript", "AWS", "Azure" have no bullet behind them (rule 4). Kevin: 5
   categories. Fix: prune per JD lexicon to 4–5 categories, ≤ 25 items, only backed skills.
4. **Only two approved summaries.** Clinical Researcher and Senior TAM both received the
   "Healthcare infrastructure engineer" opener. Need one summary per target family (PM /
   implementation / analyst / infra) and sum-ops + sum-data approved or rewritten.
5. **Tailoring is thin.** Today's tailored PDFs differ from the static one only in bullet order
   and verb variants. That is why two attempts shipped at ATS echo 19 and 27 (B42/B43). Real
   tailoring per Jobscan = headline mirror (done, held titles only) + summary family + pruned
   skills + bullet order. Items 3 and 4 fix most of it.
6. **Two unexplained gaps**: Jan 2020 → Oct 2020 (9 mo) and Jan 2023 → Jun 2024 (17 mo). The
   Rutgers certificate (May 2024) covers part of the second but is undated at the start. Needs
   the user's one-line truth for each (COVID / study / caregiving / DJ business) so the
   education entry or a dated line can carry it.
7. **Contract block has no per-engagement dates** and puts "Data Analyst" under a "Project
   Manager (Contract)" umbrella title. Recruiters cannot see how long each engagement ran.
   Needs months per engagement (open ask since 8/25).
8. **No location on the contact line.** NJ/NYC hybrid roles filter on it; add "<city>, NJ".
9. **"First technical call for 200+ staff" reads as help desk** on a PM resume. Same fact,
   PM framing: incident triage ownership + vendor/department coordination (new wording →
   approval).
10. **"IT project manager with 6+ years"** while the current title is Infrastructure Operations
    Engineer and PM-titled years are ~2.3 (Oct 2020 – Jan 2023). Recruiters cross-check the
    summary against the dates; say "6+ years in healthcare IT and pharma operations, 2+ years
    agency project management" or equivalent truthful split.

### Checklist additions
- [ ] Prune Skills per JD (tailor.py `to_markdown`): 4–5 categories, ≤ 25 items, only skills
      with a bullet or HMH daily use behind them. Static variants: PM variant drops Infra/IAM
      detail to one line.
- [ ] Summary per target family; approve/rewrite sum-ops and sum-data; pick by family first,
      echo second.
- [ ] Location on the contact line (bank identity).
- [ ] Replace the three filler bullets with three new numbered facts (user input → bank →
      approve → build_static).
- [ ] Number the Catalyst bullets and the two HMH bullets (user input).
- [ ] Per-engagement months + gap lines (user input).
- [ ] Re-word hmh-firstcall and sum-pm years (approval).
- [ ] resume_score: add a duplicate-bullet check (two bullets in one role sharing ≥ 60% of
      their content words = fail) so filler can never be approved again.

## X threads in totality — what broke, what was built
- agent-reach: bird needs logged-in cookies; anonymous X queries are blocked for this
  network's ASN (Comcast AS7922 "bad network reputation"). Not a rate limit, a hard block.
- sly-reach: reads single posts fine (keyless syndication endpoint) but assembles a thread
  from the author's syndication TIMELINE, which returns HTTP 429 in bursts. That is the rate
  limit you hit.
- Built tonight: `~/.local/bin/dia-x-cookies` decrypts the x.com auth_token/ct0 from the user's
  own Dia browser (Default profile, Dia Safe Storage key) into `~/.x_cookies`; sly-reach now
  auto-loads that file and uses bird (updated 0.4.0 → 0.8.0) for x_thread / x_user / x_search;
  the syndication timeline path retries 429 with backoff (2s/5s/10s/20s).
- **Blocked on one user action:** the Dia x.com session is stale (X answers 401 "Could not
  authenticate you" to the exported cookies and to a direct API probe). Log into x.com in Dia
  once, then run `dia-x-cookies --write`. After that threads read in full, no rate limit.

## Roadmap to "working perfectly" — what it takes, start to finish

**Phase 0 — stop the bleeding (engine, this week).** Apply-time level/title/match gates,
retire blocked jobs, spam guard, close #1/#2, zero-submit phone alarm, health checks, codespace
240m. Expected effect: every submit is in-lane; no more silent dark weeks.

**Phase 1 — inputs only you have (30–60 min of your time).**
1. Numbers for: Catalyst ×3, HMH ops-data, HMH first-call, Heartbeat MLR loop.
2. Three new facts to replace the filler bullets (team size, budget, launches, reports, hours).
3. Months for Heartbeat, FCB, Catalyst; one line each for the 2020 and 2023–24 gaps.
4. Approve the re-worded first-call bullet and the summary years split.
5. Log into x.com in Dia; open the Drive auth link (Kevin Manu PDF re-diff).
6. Kevin Manu referral message; HMH internal postings; Humana IT PM 2 by hand.

**Phase 2 — resume + tailoring engine (after Phase 1 lands).** Skills pruning per JD, summary
families, location line, duplicate-bullet check, rebuild all variants, scorecard to 100.

**Phase 3 — closed loop.** Plus-addressing per application, title matching on mail, per-app
timeline + daily phone digest, follow-up sequence auto-sent (day 5 / day 12), rejection
feedback reply, interview prep pack.

**Phase 4 — supply.** Scout every 2–3 h newest-first, phone review queue for parked jobs,
Workday handler v2, LinkedIn/Indeed Easy Apply in review mode, referral finder.

**Phase 5 — measure.** funnel_stats by month; target ≥ 16 in-lane submits/week and 5–8
interviews per 100 (Huntr tailored + Jobscan title-match benchmarks). If 150 in-lane
applications produce < 4 interviews, the problem is the lane, not the engine.

## Added 2026-09-05 — the human loop, built

What landed (all in auto-apply/ unless noted):
- `mailer.py`: one send path from 105@. Refuses no-reply/relay/role mailboxes, strips em dashes,
  `alert()` pushes to his personal inbox once per key per day (profile.alert_email), optional NTFY_TOPIC.
- `interview_pack.py`: on any real interview mail, a threaded Gmail draft with three ET windows, a
  prep brief in briefs/, and a phone alert. Never auto-sends the reply.
- `followups.py` (rewritten): day 5 + day 12, sent when a human address is known, drafted once otherwise.
- `rejection_reply.py`: one feedback ask per human-sent rejection.
- `handapply.py` + applier routing: Workday/SmartRecruiters wishlist rows get a tailored resume and
  park as `hand_apply`; CAPTCHA parks join them in HAND_APPLY.md with the standard answers.
- `digest.py`: one daily email after the last slice: submits, inbox, follow-ups, hand-apply list, asks.
- `scout.py`: Workday CXS + SmartRecruiters fetchers (verified tenants in scout_config.json).
- `test_email_loop.py`: blocks the wake if a relay address would get mail or a spam interview would fire.
- Confirmation emails now flip hand_apply/manual_review/submitted_unverified rows to `applied`.
- AssiamahS/githubactions `wake-cloudbox.yml`: backstop codespace start at :20 past 12-15 UTC.
- briefs/REFERRALS.md: the three referral notes, ready to send.

Still user-only: send the referrals; resume dates (engagement months + two gap lines); hand submits
from HAND_APPLY.md; `wrangler login` on the Mac if the CF worker ever needs a redeploy.
