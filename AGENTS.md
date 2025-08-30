# Repository Guidelines

## Project Structure & Module Organization
- `internet_status_dashboard.py`: Dash app and Flask server, Redis caching, SQLite reads.
- `check_internet.sh`: Ping loop, writes metrics to `logs/internet_status.db`.
- `power_cycle_nbn.py` / `power_cycle_nbn_override.py`: Tapo P100 control + event logging.
- `Dockerfile`, `docker-compose.yml`, `supervisord.conf`: Container build/run; Redis + dashboard + checker.
- `logs/`: SQLite DB and runtime artifacts (mounted to host in compose).
- `screenshots/`: UI snapshots used in docs.

## Build, Test, and Development Commands
- Build & run container: `docker-compose up --build -d` — starts Redis (separate service), checker, and Gunicorn/Dash on `:8050`.
- View logs: `docker-compose logs -f local-network-monitor` — tail combined process logs.
- Stop services: `docker-compose down` — stops and removes the container.
- Local dev (no Docker): `pip install -r requirements.txt`; run Redis separately; start UI with `python3 internet_status_dashboard.py`; run a sample check with `bash scripts/check_internet.sh` to populate the DB.

## Coding Style & Naming Conventions
- Python: PEP 8, 4‑space indent, snake_case for functions/vars; constants and env keys in UPPER_SNAKE.
- Shell: `bash` with `set -euo pipefail`; prefer clear variable names and quoted expansions.
- Files: lower_snake for scripts; keep single‑purpose modules at repo root.
- Formatting/Linting: no enforced tool yet; prefer `black` and `ruff` for contributions.

## Testing Guidelines
- Current status: no automated tests in repo.
- If adding tests, use `pytest`; place under `tests/`, mirror module names (e.g., `tests/test_internet_status_dashboard.py`).
- Focus areas: data filtering (`filter_data_by_date`), DB read helpers, and Tapo control error handling (mock I/O).
- Run: `pytest -q` (add `pytest` to dev dependencies).

## Commit & Pull Request Guidelines
- Commits: use imperative, concise subjects; optional scope/prefix (observed: `add:`, `perf:`, `Feature/...`). Reference issues (e.g., `#3`) when applicable.
- PRs: include summary, rationale, test steps, screenshots for UI changes, and any config/env changes. Link related issues.

## Security & Configuration Tips
- Secrets: do not commit credentials. Prefer `.env` with `docker-compose` (`env_file: .env`) and git‑ignore it.
- Volumes: mount `./logs:/app/logs` to persist the SQLite DB across restarts.
- Env keys: `INTERNET_CHECK_TARGETS`, `FAILURE_THRESHOLD`, `DISPLAY_TZ`, Tapo credentials; document changes in PRs.
