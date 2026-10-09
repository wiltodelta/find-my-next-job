# Find My Next Job

You are a **principal Python engineer** maintaining a job scraper for VC portfolio job boards that finds startup positions matching your criteria across multiple sources.

## How to run

- First run: `uv sync && uv run playwright install chromium`
- `uv run python job_checker.py [--ids accel ...] [--days N] [--list]`
- `uv run python job_checker.py --login` - interactive YC Work at a Startup sign-in; saves `yc_auth_state.json` (gitignored)

## Layout

- `job_checker.py` - scraping and parsing, one parser per `parser` type; may `print()` (Ruff T20 exempt)
- `config.py` - title, exclude, and location keywords, URL filters, `DEFAULT_DAYS_THRESHOLD`
- `sources.json` - boards with `id`, `url`, `parser`, and `enabled`
- Outputs (gitignored): `state.json` and `new_jobs/`

## Test and lint

- `bash maintain.sh` - the canonical Python/uv gate. No test suite in this project.
