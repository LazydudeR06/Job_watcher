$readme = @'
# Local Job Watcher

A local-first Python app that checks public job feeds, filters listings using `profile.md`, scores matches with Ollama, and writes Markdown and CSV reports. The currently implemented feeds are Remotive, RemoteOK, and Himalayas. Adzuna is disabled and not implemented. Telegram and Discord notifications are not configured.

## Requirements

- Windows and Python 3.11 or newer
- Ollama running locally
- Ollama models `nomic-embed-text` and `llama3.2`

## Setup

Open PowerShell in the project directory:

```powershell
cd C:\Users\abhin\Documents\Job_finder
py -3.11 -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

If you already created and activated `venv`, skip the virtual-environment creation and activation commands. If your environment folder is named `.venv`, activate `.\.venv\Scripts\Activate.ps1` instead.

Make sure Ollama is running and both models are installed:

```powershell
ollama list
```

If needed, install the models:

```powershell
ollama pull nomic-embed-text
ollama pull llama3.2
```

Review `profile.md` and `config.yaml`. `profile.md` contains personal details; don't publish it in a public repository unless you intend to share them.

## Run

Run one collection cycle:

```powershell
python run.py --once --verbose
```

Run continuously, collecting immediately and repeating every 15 minutes:

```powershell
python run.py --interval 900 --verbose
```

Stop continuous mode with **Ctrl+C**. The interval can be changed in `config.yaml` or with `--interval` (seconds).

## What happens

1. The app fetches public listings from enabled sources.
2. New jobs are saved in `data/jobs.db`; already-seen jobs are deduplicated.
3. Title, work-type, and location rules filter jobs. Remaining candidates are checked against the profile with `nomic-embed-text`.
4. Filter survivors are scored by the configured Ollama model. Valid scores and rationales are saved in SQLite.
5. Scored matches are written to `output/digests/` and `output/hourly/`.

A run may fetch jobs but store zero because they were already in the database. It may also produce no reports if no jobs survive filtering and scoring.

## Files and privacy

- `profile.md`: your candidate profile; keep private if it contains personal information.
- `config.yaml`: feeds, models, URLs, and thresholds.
- `data/jobs.db`: local job database.
- `output/digests/`: daily Markdown digests.
- `output/hourly/`: hourly CSV files.
- `logs/jobwatcher.log`: rotating application log.
- `.env.example`: environment-variable template; copy to `.env` only if you need optional secrets.

Keep `.env`, the database, generated reports, logs, virtual environments, and Python cache files out of Git. The repository `.gitignore` is intended to do this.

## Troubleshooting

- **`No module named 'yaml'`:** activate the project's virtual environment, then run `python -m pip install -r requirements.txt`.
- **Ollama connection refused:** start Ollama and verify it responds at `http://localhost:11434`.
- **Embedding errors:** verify `nomic-embed-text` is installed with `ollama list`; transient Ollama errors are logged in `logs/jobwatcher.log`.
- **Fetched but stored zero:** the listings may already exist in `data/jobs.db`.
- **No matches:** inspect the filter messages and job locations/titles; the rules intentionally exclude senior roles and some location restrictions.
'@
$destinations = @(
    'C:\Users\abhin\copilot-worktrees\pyton_jobbot\lazyduder06-bookish-engine\README.md',
    'C:\Users\abhin\Documents\Job_finder\README.md'
)
foreach ($destination in $destinations) {
    Set-Content -LiteralPath $destination -Value $readme -Encoding UTF8
    Write-Output "Wrote $destination"
}