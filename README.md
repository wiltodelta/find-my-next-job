# Find My Next Job

Job scraper for VC portfolio job boards. Configure it to search for any position type.

## Quick start

```bash
# Install dependencies
curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync
uv run playwright install chromium

# Edit config.py to change what you're searching for
# Look for URL_FILTERS section and change the job type

# (Optional) Login to YC Work at a Startup for more jobs
uv run python job_checker.py --login

# Run the scraper
uv run python job_checker.py
```

Results are saved to `new_jobs/` folder.

---

## Requirements

- Python 3.13+
- [uv](https://github.com/astral-sh/uv) - Fast Python package installer

## Installation

1. Install uv (if not already installed):
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

2. Install dependencies:
```bash
uv sync
```

3. Install Playwright browsers:
```bash
uv run playwright install chromium
```

## Usage

### Basic Usage

Scrape all enabled sources:
```bash
uv run python job_checker.py
```

### Scrape Specific Sources

By source ID:
```bash
uv run python job_checker.py --ids a16z yc index
```

List all available sources:
```bash
uv run python job_checker.py --list
```

### Advanced Options

Change days threshold (default: 7):
```bash
uv run python job_checker.py --days 14
```

Combine source and days filter:
```bash
uv run python job_checker.py --ids yc --days 30
```

### YC Work at a Startup (requires login)

Y Combinator's Work at a Startup requires authentication. Login once to save your session:

```bash
uv run python job_checker.py --login
```

This opens a browser window. Log in with your YC account, then press Enter in the terminal to save the session. The auth state is saved to `yc_auth_state.json` (excluded from git).

After login, you can scrape YC:
```bash
uv run python job_checker.py --ids yc
```

## Configuration

### Sources

Edit `sources.json` to add, remove, or configure job sources.

Source fields:
| Field | Required | Description |
|-------|----------|-------------|
| `id` | Yes | Unique identifier for the source |
| `name` | Yes | Display name |
| `url` | Yes | Base URL of the job board (without filter parameters) |
| `parser` | Yes | Parser type (see below) |
| `enabled` | No | Set to `false` to disable (default: `true`) |

Example source:

```json
{
  "id": "a16z",
  "name": "Andreessen Horowitz",
  "url": "https://portfoliojobs.a16z.com/jobs",
  "parser": "consider",
  "enabled": true
}
```

Sources are sorted by rank from [TIME's America's Top Venture Capital Firms of 2025](https://time.com/7309945/top-venture-capital-firms-usa-2025/).

### Parser types

| Parser | Platform | URL filter example | Description |
|--------|----------|-------------------|-------------|
| `consider` | [Consider.co](https://consider.co) | `?jobTypes=...` | Most VC portfolio boards |
| `getro` | [Getro](https://www.getro.com) | `?q=...` | Alternative job board platform |
| `yc` | [Work at a Startup](https://www.workatastartup.com) | URL params | Requires login (see above) |
| `index` | Index Ventures | None | Custom startup jobs board with pagination |

**How to identify parser type:**
1. Open the job board URL in browser
2. Look at the page footer or source code:
   - "Powered by Consider" → use `consider`
   - "Powered by Getro" → use `getro`
3. For unknown platforms, check if the URL structure matches existing parsers

**How to find URL filters:**
1. Open a job board (e.g., `https://jobs.accel.com/jobs`)
2. Use the built-in filters on the site to search for the position you need
3. Copy the filter parameters from the resulting URL:
   - Consider.co: `?jobTypes=...` (e.g., `?jobTypes=Engineering+Manager`)
   - Getro: `?q=...` (e.g., `?q=engineering%20manager`)
4. Configure filters in `config.py` under `URL_FILTERS`

### Filtering settings

Edit `config.py` to:
- Change default days threshold (`DEFAULT_DAYS_THRESHOLD`)
- Modify job title keywords (`JOB_TITLE_KEYWORDS`) - used for sources without URL-based filtering
- Configure URL filter parameters for different platforms (`URL_FILTERS`):
  - `consider`: e.g., `jobTypes=Engineering+Manager` or `jobTypes=Software+Engineer`
  - `getro`: e.g., `q=engineering%20manager` or `q=backend%20developer`

## Output

- `state.json` - Tracks last scrape time and known job URLs per source
- `new_jobs/new_jobs_YYYY-MM-DD_HH-MM-SS.json` - New jobs found in each run
- `yc_auth_state.json` - Browser session for YC (created by `--login`, excluded from git)

### Job fields

Each job includes:
| Field | Description |
|-------|-------------|
| `title` | Job title |
| `company` | Company name |
| `source_id` | Source identifier |
| `url` | Direct link to job posting |
| `location` | Job location (extracted from job card) |
| `posted_date` | Date when job was posted (ISO format) |
| `potential_duplicate` | True if same company+title seen recently |

### Duplicate detection

Jobs are marked as potential duplicates when the same company+title combination appears within 7 days across any source. This helps identify jobs posted on multiple VC portfolio boards.

Duplicate jobs have `"potential_duplicate": true` in the JSON output and are marked with `[DUP]` in console output.

## Supported sources

Currently 41 VC portfolio job boards are configured:

- **Consider.co** (16 sources): a16z, Sequoia, Greylock, Kleiner Perkins, Bessemer, Lightspeed, NEA, Battery, GV, USV, etc.
- **Getro** (23 sources): Accel, General Catalyst, Khosla, Thrive, Insight Partners, Coatue, Redpoint, etc.
- **Y Combinator**: Work at a Startup
- **Index Ventures**: Custom startup jobs board

All sources filter jobs using URL filters and keywords defined in `config.py`.

## Development

Run code quality checks:
```bash
./maintain.sh
```
