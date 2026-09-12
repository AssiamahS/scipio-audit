# scipio (code mirror for review)

Read-only mirror of a private job-search automation repo, published so external reviewers can audit the code.
Everything personal is stripped: the tracker (`jobs.json`), the application log, resumes and their word bank,
confirmations/rejections feeds, profile, screenshots, briefs. Redacted `*.example.json` files show each schema.

Pipeline (auto-apply/): `discover_boards.py` + `aggregators.py` + `bbb_nj*.py` (supply) -> `scout.py` (fetch, title/location
filter, `fit_check.py` screen, `ai_engine.py` score, wishlist) -> `process_queue.py` (one slice per wake) ->
`applier.py` (selection guards, `tailor.py` per-JD resume from `bank.json`, `ats_handlers.py`/`workday.py` form fill,
`captcha.py`, confirmation detection) -> `rejections.py` (IMAP sweep: confirmations, rejections, interviews) ->
`funnel_stats.py` / `resume_score.py` (published stats) -> `digest.py`. Scheduling: `.slyci/workflows/apply.yml` on a
GitHub Codespace via `cloud_runner.sh`, woken by `worker/worker.js` crons.
