# Job Watcher: Spec Compliance Audit

Audit this project against the spec below. Do NOT fix or change any application code in this task. Only add tests and a report.

## Rules
- First inspect the codebase and map each spec item to the real module/function that implements it. Show me this map before writing tests.
- Write the tests in `tests/test_spec_compliance.py` (add small helper files in `tests/fixtures/` if needed). Use pytest.
- No live network calls and no real Ollama calls. Mock HTTP with the `responses`/`requests-mock` library or monkeypatch, and mock Ollama responses. Use a temporary SQLite DB (tmp_path) and temporary output/log folders.
- One test per numbered item below. Name each test `test_<id>_<short_name>` and put the spec ID in the docstring.
- If a feature is not implemented, the test must FAIL with a clear message "NOT IMPLEMENTED: <what is missing>". Do not skip, xfail, or weaken a test to make it pass.
- If a spec item is ambiguous, write the most reasonable test and note the assumption in a comment.
- When done, run `pytest tests/test_spec_compliance.py -v` and give me a table: ID | feature | PASS / FAIL / NOT IMPLEMENTED | one-line reason. Then list the failures in priority order.

## Tests

### A. Collectors
- A1 Remotive: given a fixture response, `fetch()` returns Job objects with title, company, location, url, description, posted time.
- A2 RemoteOK: same, and the first element of the feed (the legal/metadata item) is skipped.
- A3 Himalayas: same.
- A4 Adzuna: enabled only when ADZUNA_APP_ID and ADZUNA_APP_KEY are set; disabled (returns nothing, no error) when empty.
- A5 All collectors share one interface (`fetch() -> list[Job]`).
- A6 Graceful failure: when one collector raises or gets HTTP 500, the run logs the error and the other collectors still run.
- A7 Rate limiting: at least 1.0s between requests per source, 20s timeout, 1 retry with backoff on 429/5xx, then the source is skipped for that run.

### B. Storage and dedupe
- B1 A job with the same URL/ID inserted twice is stored once.
- B2 Only new jobs go on to filtering/scoring on a second run.
- B3 A job that failed LLM JSON scoring is stored as processed and is not re-scored on the next run.
- B4 BUG REGRESSION: when the embedding call fails (connection error or timeout), the job is NOT marked rejected. It stays unprocessed or pending, is retried on the next run, and a warning is logged.

### C. Cheap filter
- C1 Include keywords: titles with ui designer, ux designer, product designer, graphic designer, visual designer, interaction designer, winforms, wpf, desktop developer, c# developer, windows developer, ML intern, devops intern, FDE intern, SDE intern all pass.
- C2 Exclude keywords: senior, staff, principal, lead, manager, director, architect, 5+ years, 7+ years, 10+ years are dropped.
- C3 "DevOps Intern" and "ML Intern" are NOT dropped by the backend/devops/data rule, while a "Backend Engineer" with no include match is dropped.
- C4 Location: remote worldwide passes; hybrid and on-site in Bengaluru, Hyderabad, Chennai, Pune, Delhi NCR, Mumbai pass; on-site outside India is dropped.
- C5 Remote jobs that say "US only" or "must be in the US/EU" are dropped.
- C6 Work type: internship and entry-level full-time pass; contract, freelance, unpaid/volunteer are dropped.
- C7 Experience: 0-2 years passes; mid/senior is dropped unless the title looks junior.
- C8 Embedding threshold 0.35: a job at or above it passes; below it is dropped; a strong title keyword match passes even when similarity is lower.
- C9 The threshold is read from config (changing the config value changes the result).
- C10 Embedding backend is a config value (ollama nomic-embed-text by default; sentence_transformers is selectable).

### D. LLM scoring
- D1 Only jobs that passed the cheap filter are sent to the LLM (assert the mocked LLM is not called for rejected jobs).
- D2 The model name and the Ollama URL come from config.
- D3 Valid JSON with score (0-100), reasons, missing_skills, apply_priority is parsed correctly.
- D4 Invalid JSON triggers exactly one retry, then the job is skipped and marked processed.
- D5 The scoring prompt makes tech internships (ML/DevOps/FDE/SDE) rank highest, then UI/UX and product design, then C#/WinForms/WPF, then graphic design lowest. Check the prompt text, and check that apply_priority differs accordingly for mocked cases.
- D6 The profile.md content is included in or used by the scoring step.

### E. Output
- E1 Daily digest `output/YYYY-MM-DD.md` is created, uses the Asia/Kolkata date, and contains only jobs with score >= 60 (config value), with title, company, location, score, reasons, URL.
- E2 Daily CSV `output/YYYY-MM-DD.csv` is created with the same jobs.
- E3 Running twice on the same day merges unique jobs, with no duplicates.
- E4 Hourly update `output/hourly/YYYY-MM-DD_HH.md` lists ONLY jobs that are new in that run.
- E5 A run that finds nothing new writes no hourly file.
- E6 The score thresholds 60 and 75 are config values.

### F. Notifications
- F1 Discord webhook notifier sends only jobs with score >= 75 (config value).
- F2 Max 10 jobs per message, each with title, company, score, priority, link.
- F3 DISCORD_WEBHOOK_URL empty means the notifier is off and makes no HTTP call.
- F4 A webhook failure is logged and does not crash the run.
- F5 No Telegram code path is active by default.

### G. Scheduling, config, logging
- G1 `run.py --once` runs a single full cycle and exits with code 0.
- G2 Loop/interval mode exists and the default interval is 1 hour.
- G3 config.yaml loads; a missing required key gives a clear error.
- G4 Secrets are read from .env and never hardcoded (grep the source for tokens or URLs).
- G5 `--verbose` switches the log level to DEBUG; the default is INFO.
- G6 A rotating file handler is configured for logs/jobwatcher.log.
- G7 Timezone for daily file naming is Asia/Kolkata.

### H. Safety and project hygiene
- H1 No scraping of LinkedIn/Indeed HTML, no headless browser or Selenium/Playwright imports, no CAPTCHA bypass.
- H2 .gitignore contains .env, data/, output/, logs/, __pycache__/.
- H3 .env.example exists and lists DISCORD_BOT_TOKEN, DISCORD_JOBS_CHANNEL_ID, DISCORD_WEBHOOK_URL, ADZUNA_APP_ID, ADZUNA_APP_KEY.
- H4 requirements.txt exists and pins versions.
- H5 README documents: setup, `ollama pull llama3.2:3b` and `ollama pull nomic-embed-text`, Windows Task Scheduler steps for an hourly `run.py --once`, and Adzuna keys. Flag any statement that contradicts the code (the README currently says Adzuna is not implemented).
- H6 The project has exactly one source-of-truth folder: warn if another copy exists (for example Documents\Job_finder) with different file contents.

### I. LinkedIn (expected to be NOT IMPLEMENTED for now)
- I1 A LinkedIn route exists (IMAP alert-email collector or Discord channel collector). Report its status only; do not treat it as a blocker.

## Final report
After running the tests, print:
1. The ID | feature | status table.
2. A prioritized list of what to fix, with the file and function to change for each.
3. Any tests you had to make assumptions for.
Do not modify application code.
