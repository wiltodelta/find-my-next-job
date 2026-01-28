#!/usr/bin/env python3
"""
Job Checker Script

Scrapes job listings from VC portfolio job boards and saves new jobs
to timestamped JSON files. Tracks last scrape time per source to identify
fresh listings.

Usage:
    python job_checker.py

Requirements:
    - playwright (install with: pip install playwright && playwright install chromium)
"""

import argparse
import asyncio
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from playwright.async_api import Page, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeout

from config import DEFAULT_DAYS_THRESHOLD, JOB_TITLE_KEYWORDS, LOCATION_KEYWORDS, URL_FILTERS

# Auth state file for sites requiring login (e.g., YC Work at a Startup)
AUTH_STATE_FILE = Path(__file__).parent / "yc_auth_state.json"


@dataclass
class Job:
    """Represents a job listing."""

    title: str
    company: str | None
    source_id: str  # Source identifier for state tracking
    url: str
    location: str | None = None  # Job location (e.g., "Remote", "London")
    posted_date: str | None = None  # ISO format date string or None
    days_ago: int | None = None
    scraped_at: str = ""
    potential_duplicate: bool = False  # True if same company+title seen recently

    def __post_init__(self):
        if not self.scraped_at:
            self.scraped_at = datetime.now().isoformat()

    def get_duplicate_key(self) -> str | None:
        """
        Return a normalized key for duplicate detection.
        Returns None if company is not available.
        """
        if not self.company:
            return None
        # Normalize: lowercase, strip whitespace
        company_norm = self.company.lower().strip()
        title_norm = self.title.lower().strip()
        return f"{company_norm}|{title_norm}"


@dataclass
class SourceState:
    """Tracks state for a job source."""

    last_scraped: str  # ISO datetime of last scrape
    known_job_urls: list[str] = field(default_factory=list)


@dataclass
class Source:
    """Represents a job source configuration."""

    id: str
    name: str
    url: str
    parser: str
    enabled: bool = True


def apply_url_filter(url: str, parser: str) -> str:
    """Apply URL filter parameters based on parser type."""
    if parser not in URL_FILTERS:
        return url

    filter_param = URL_FILTERS[parser]

    # Check if URL already has the filter
    if filter_param in url:
        return url

    # Add filter parameter
    if "?" in url:
        return f"{url}&{filter_param}"
    else:
        return f"{url}?{filter_param}"


def load_sources(filepath: Path) -> list[Source]:
    """Load sources configuration from JSON file."""
    if not filepath.exists():
        print(f"Error: sources.json not found at {filepath}")
        return []

    try:
        with open(filepath, encoding="utf-8") as f:
            data = json.load(f)
            sources = []
            for s in data.get("sources", []):
                if s.get("enabled", True):
                    parser = s["parser"]
                    url = apply_url_filter(s["url"], parser)
                    sources.append(
                        Source(
                            id=s["id"],
                            name=s["name"],
                            url=url,
                            parser=parser,
                            enabled=s.get("enabled", True),
                        )
                    )
            return sources
    except Exception as e:
        print(f"Error loading sources: {e}")
        return []


def matches_job_title_keywords(title: str) -> bool:
    """Check if job title matches configured keywords."""
    title_lower = title.lower()
    return any(keyword in title_lower for keyword in JOB_TITLE_KEYWORDS)


def matches_location_keywords(location: str | None) -> bool:
    """Check if job location matches configured keywords.

    Returns True if:
    - location is None (include jobs without location info)
    - location contains any of the configured keywords
    """
    if location is None:
        return True
    location_lower = location.lower()
    return any(keyword in location_lower for keyword in LOCATION_KEYWORDS)


def clean_job_url(href: str, source_url: str) -> str:
    """Clean and normalize job URLs."""
    if not href:
        return href

    # Fix broken protocol-relative URLs that misinterpreted /jobs/ as //jobs/
    # This happens on some Getro sites where href is "//jobs/..."
    # resulting in "https://jobs/..."
    if "://jobs/" in href:
        domain_part = href.split("://")[1].split("/")[0]
        if domain_part == "jobs":
            # Extract domain from source_url
            source_parts = source_url.split("/")
            if len(source_parts) >= 3:
                domain = source_parts[2]
                href = href.replace("://jobs/", f"://{domain}/jobs/")

    # Remove duplicate slashes (except in protocol)
    if "://" in href:
        protocol, rest = href.split("://", 1)
        href = protocol + "://" + rest.replace("//", "/")

    return href


def parse_relative_date(text: str) -> int | None:
    """
    Parse relative date strings like '3 days ago', 'about 7 hours ago', '30+ days ago'.
    Returns the number of days ago, or None if parsing fails.
    """
    text = text.lower().strip()

    # Handle "just now", "today"
    if "just now" in text or "today" in text:
        return 0

    # Handle "yesterday"
    if "yesterday" in text:
        return 1

    # Handle "less than X day" -> treat as that many days
    less_than_match = re.search(r"less than\s*(\d+)\s*days?", text)
    if less_than_match:
        return max(0, int(less_than_match.group(1)) - 1)

    # Handle hours
    hours_match = re.search(r"(\d+)\s*hours?(?:\s*ago)?", text)
    if hours_match:
        return 0  # Less than a day

    # Handle "30+ days" or "X+ days"
    plus_days_match = re.search(r"(\d+)\+\s*days?(?:\s*ago)?", text)
    if plus_days_match:
        return int(plus_days_match.group(1))

    # Handle days
    days_match = re.search(r"(\d+)\s*days?(?:\s*ago)?", text)
    if days_match:
        return int(days_match.group(1))

    # Handle weeks
    weeks_match = re.search(r"(\d+)\s*weeks?(?:\s*ago)?", text)
    if weeks_match:
        return int(weeks_match.group(1)) * 7

    # Handle months
    months_match = re.search(r"(\d+)\s*months?(?:\s*ago)?", text)
    if months_match:
        return int(months_match.group(1)) * 30

    return None


async def scrape_consider_site(
    page: Page, source_id: str, source_name: str, url: str, days_threshold: int = 7
) -> list[Job]:
    """
    Scrape jobs from Consider.co platform sites (a16z, Sequoia, Battery, etc.).

    DOM structure (standard view):
    - div.job-list-job: job card container
    - a.job-list-job-company-link: company name
    - h2.job-list-job-title a / h3.job-list-job-title a: job title and URL
    - .job-list-badge-locations: location
    - .job-list-badge-posted: date (e.g., "Posted less than 1 day ago")

    DOM structure (grouped view - e.g., Sequoia):
    - div.grouped-job-result: company container
    - img[alt*="logo"]: company logo with name in alt (e.g., "Harvey logo")
    - div.job-list-job: job cards inside company container
    """
    jobs = []
    seen_urls = set()

    try:
        await page.goto(url, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(2000)

        # Scroll to load all jobs
        for _ in range(5):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1000)

        # Find all job cards
        job_cards = await page.query_selector_all("div.job-list-job")

        for card in job_cards:
            try:
                # Extract title and URL from h2/h3.job-list-job-title a
                title_el = await card.query_selector("h2.job-list-job-title a, h3.job-list-job-title a")
                if not title_el:
                    continue

                title = await title_el.inner_text()
                href = await title_el.evaluate("el => el.href")
                href = clean_job_url(href, url)

                if not title or not href:
                    continue

                if href in seen_urls:
                    continue

                # Extract company: first try standard view, then grouped view
                company_el = await card.query_selector("a.job-list-job-company-link")
                company = await company_el.inner_text() if company_el else None

                # If no company found, try grouped view (parent container with logo)
                if not company:
                    company = await card.evaluate(
                        """el => {
                            const grouped = el.closest('.grouped-job-result');
                            if (grouped) {
                                const logo = grouped.querySelector('img[alt*="logo"]');
                                if (logo && logo.alt) {
                                    // Remove " logo" suffix from alt text
                                    return logo.alt.replace(/ logo$/i, '');
                                }
                            }
                            return null;
                        }"""
                    )

                # Extract location from .job-list-badge-locations
                location_el = await card.query_selector(".job-list-badge-locations")
                location = await location_el.inner_text() if location_el else None

                # Extract date from .job-list-badge-posted
                date_el = await card.query_selector(".job-list-badge-posted")
                date_text = await date_el.inner_text() if date_el else None

                days_ago = None
                posted_date = None

                if date_text:
                    days_ago = parse_relative_date(date_text)
                    if days_ago is not None:
                        posted_date = (datetime.now() - timedelta(days=days_ago)).date().isoformat()

                # Filter by days threshold
                if days_ago is not None and days_ago > days_threshold:
                    continue

                seen_urls.add(href)

                # Filter by manager keywords
                if not matches_job_title_keywords(title):
                    continue

                jobs.append(
                    Job(
                        title=title.strip(),
                        company=company.strip() if company else None,
                        source_id=source_id,
                        url=href,
                        location=location.strip() if location else None,
                        posted_date=posted_date,
                        days_ago=days_ago,
                    )
                )
            except Exception:
                continue

    except PlaywrightTimeout:
        print(f"  Timeout loading {source_name}")
    except Exception as e:
        print(f"  Error scraping {source_name}: {e}")

    return jobs


async def scrape_getro_site(
    page: Page, source_id: str, source_name: str, url: str, days_threshold: int = 7
) -> list[Job]:
    """
    Scrape jobs from Getro-powered sites (Khosla, Antler, General Catalyst, etc.).

    DOM structure uses schema.org microdata:
    - div[itemprop="title"]: job title
    - a[data-testid="job-title-link"]: job URL
    - meta[itemprop="name"] or a[data-testid="link"]: company name
    - meta[itemprop="address"]: location
    - meta[itemprop="datePosted"]: date in ISO format (YYYY-MM-DD)
    """
    jobs = []
    seen_urls = set()

    try:
        await page.goto(url, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(3000)

        # Find all job cards using the job-info class or job title links
        job_cards = await page.query_selector_all('a[data-testid="job-title-link"]')

        for job_link in job_cards:
            try:
                href = await job_link.evaluate("el => el.href")
                href = clean_job_url(href, url)

                if not href or href in seen_urls:
                    continue

                # Get parent container with schema.org data
                container = await job_link.evaluate_handle(
                    """el => {
                        let parent = el.parentElement;
                        for (let i = 0; i < 8 && parent; i++) {
                            if (parent.querySelector('[itemprop="address"]') ||
                                parent.querySelector('[itemprop="datePosted"]')) {
                                return parent;
                            }
                            parent = parent.parentElement;
                        }
                        return el.closest('.job-info') || el.parentElement?.parentElement;
                    }"""
                )

                if not container:
                    continue

                # Extract data using schema.org microdata selectors
                card_data = await container.evaluate(  # type: ignore
                    r"""el => {
                        const data = {
                            title: null, company: null, location: null, datePosted: null
                        };

                        // Title: div[itemprop="title"]
                        const titleEl = el.querySelector('[itemprop="title"]');
                        if (titleEl) {
                            data.title = titleEl.textContent?.trim();
                        }

                        // Company: meta[itemprop="name"] (content) or a[data-testid="link"]
                        const companyMeta = el.querySelector('meta[itemprop="name"]');
                        if (companyMeta) {
                            data.company = companyMeta.getAttribute('content');
                        } else {
                            const companyLink = el.querySelector('a[data-testid="link"]');
                            if (companyLink) {
                                data.company = companyLink.textContent?.trim();
                            }
                        }

                        // Location: try multiple selectors
                        // 1. meta[itemprop="addressLocality"] inside div[itemprop="jobLocation"]
                        const jobLocationDiv = el.querySelector('div[itemprop="jobLocation"]');
                        if (jobLocationDiv) {
                            const addressLocalityMeta = jobLocationDiv.querySelector(
                                'meta[itemprop="addressLocality"]'
                            );
                            if (addressLocalityMeta) {
                                data.location = addressLocalityMeta.getAttribute('content');
                            }
                        }
                        // 2. Fallback: look for visible location text in span (usually next to location icon)
                        if (!data.location) {
                            // Find the span that contains location text (usually after an SVG icon)
                            const spans = el.querySelectorAll('span');
                            for (const span of spans) {
                                const text = span.textContent?.trim() || '';
                                // Location patterns: contains comma, or common location keywords
                                if (text && text.length > 2 && text.length < 100) {
                                    if (text.includes(',') ||
                                        text.includes('USA') ||
                                        text.includes('UK') ||
                                        text.includes('Remote') ||
                                        text.match(/^[A-Z][a-z]+,\s*[A-Z]{2}/)) {
                                        // Check it's not a date or job type
                                        if (!text.match(/^(fulltime|parttime|contract|intern)$/i) &&
                                            !text.match(/^\d+ (day|week|month)s? ago$/i) &&
                                            !text.match(/^(Today|Yesterday)$/i)) {
                                            data.location = text;
                                            break;
                                        }
                                    }
                                }
                            }
                        }

                        // Date: meta[itemprop="datePosted"] (content) - ISO format
                        const dateMeta = el.querySelector('meta[itemprop="datePosted"]');
                        if (dateMeta) {
                            data.datePosted = dateMeta.getAttribute('content');
                        }

                        return data;
                    }"""
                )

                if not card_data:
                    continue

                title = card_data.get("title")
                company = card_data.get("company")
                location = card_data.get("location")
                date_posted_str = card_data.get("datePosted")

                if not title:
                    continue

                # Parse ISO date (YYYY-MM-DD) to calculate days_ago
                days_ago = None
                posted_date = None

                if date_posted_str:
                    try:
                        posted = datetime.strptime(date_posted_str, "%Y-%m-%d")
                        days_ago = (datetime.now() - posted).days
                        posted_date = posted.date().isoformat()
                    except ValueError:
                        pass

                # Filter by days threshold
                if days_ago is not None and days_ago > days_threshold:
                    continue

                seen_urls.add(href)

                # Filter by manager keywords
                if not matches_job_title_keywords(title):
                    continue

                jobs.append(
                    Job(
                        title=title.strip(),
                        company=company.strip() if company else None,
                        source_id=source_id,
                        url=href,
                        location=location.strip() if location else None,
                        posted_date=posted_date,
                        days_ago=days_ago,
                    )
                )
            except Exception:
                continue

    except PlaywrightTimeout:
        print(f"  Timeout loading {source_name}")
    except Exception as e:
        print(f"  Error scraping {source_name}: {e}")

    return jobs


async def scrape_yc_jobs(page: Page, source_id: str, source_name: str, url: str, days_threshold: int = 7) -> list[Job]:
    """
    Scrape jobs from Y Combinator's Work at a Startup (workatastartup.com).

    DOM structure (list-compact layout):
    - Company card container with company info and job listings
    - span.company-name: company name
    - a[href*="/jobs/"].font-medium: job title and URL
    - Metadata spans after job title: location, job type, etc.
    - Date in company link text: "(X days ago)" or "(about X hours ago)"

    Requires login - use --login flag first to save auth state.
    """
    seen_urls: set[str] = set()
    jobs: list[Job] = []

    try:
        await page.goto(url, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(3000)

        # Scroll to load more content
        for _ in range(5):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1000)

        # Extract all jobs using JavaScript for better performance
        jobs_data = await page.evaluate(
            """() => {
                const results = [];
                const jobLinks = document.querySelectorAll('a[href*="/jobs/"].font-medium');
                const dateRe = /\\((\\d+\\s+days?\\s+ago|about\\s+\\d+\\s+hours?\\s+ago|today|yesterday)\\)/i;
                for (const jobLink of jobLinks) {
                    try {
                        const title = jobLink.textContent?.trim();
                        const href = jobLink.href;
                        if (!title || !href) continue;
                        let container = jobLink;
                        for (let i = 0; i < 10 && container; i++) {
                            if (container.querySelector && container.querySelector('span.company-name')) break;
                            container = container.parentElement;
                        }
                        if (!container) continue;
                        const companyEl = container.querySelector('span.company-name');
                        const company = companyEl?.textContent?.trim() || null;
                        const companyLink = container.querySelector('a[href*="/companies/"]');
                        let dateText = null;
                        if (companyLink) {
                            const linkText = companyLink.textContent || '';
                            const dateMatch = linkText.match(dateRe);
                            if (dateMatch) dateText = dateMatch[1];
                        }
                        const jobNameDiv = jobLink.closest('.job-name');
                        let location = null;
                        if (jobNameDiv) {
                            const metaDiv = jobNameDiv.nextElementSibling;
                            if (metaDiv) {
                                const firstSpan = metaDiv.querySelector('span');
                                if (firstSpan) location = firstSpan.textContent?.trim() || null;
                            }
                        }
                        results.push({ title, url: href, company, location, date: dateText });
                    } catch (e) { continue; }
                }
                return results;
            }"""
        )

        for job_data in jobs_data:
            try:
                href = job_data.get("url", "")
                title = job_data.get("title", "")

                if not title or not href:
                    continue

                href = clean_job_url(href, url)

                if href in seen_urls:
                    continue

                # Filter by manager keywords
                if not matches_job_title_keywords(title):
                    continue

                company = job_data.get("company")
                location = job_data.get("location")
                date_text = job_data.get("date")

                days_ago = None
                posted_date = None

                if date_text:
                    days_ago = parse_relative_date(date_text)
                    if days_ago is not None:
                        posted_date = (datetime.now() - timedelta(days=days_ago)).date().isoformat()

                # Filter by days threshold
                if days_ago is not None and days_ago > days_threshold:
                    continue

                seen_urls.add(href)

                jobs.append(
                    Job(
                        title=title.strip(),
                        company=company.strip() if company else None,
                        source_id=source_id,
                        url=href,
                        location=location.strip() if location else None,
                        posted_date=posted_date,
                        days_ago=days_ago,
                    )
                )

            except Exception:
                continue

        print(f"    Found {len(jobs)} matching jobs")

    except PlaywrightTimeout:
        print(f"  Timeout loading {source_name}")
    except Exception as e:
        print(f"  Error scraping {source_name}: {e}")

    return jobs


async def scrape_index_ventures(
    page: Page, source_id: str, source_name: str, base_url: str, days_threshold: int
) -> list[Job]:
    """
    Scrape jobs from Index Ventures startup jobs page.
    Navigates through multiple pages and filters by date (last 7 days).

    DOM structure:
    - li.result contains each job card
    - h3.result__title: job title
    - h4.result__company: company name
    - a.result__link[href]: job URL
    - ul.result__category-list__locations span: location (e.g., "Remote", "London")
    - ul.result__category-list__date span: date (e.g., "Tue, December 23, 2025")
    """
    jobs = []
    seen_urls = set()

    # Parse pages until we find only old jobs (>7 days)
    page_num = 1
    while True:
        try:
            # Build URL with page number
            if base_url.endswith("/1"):
                url = base_url.replace("/1", f"/{page_num}")
            elif base_url.endswith("/"):
                url = f"{base_url}{page_num}"
            else:
                url = f"{base_url}/{page_num}"

            await page.goto(url, wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(3000)

            # Find all job cards
            job_cards = await page.query_selector_all("li.result")

            page_jobs_count = 0
            page_fresh_count = 0

            for card in job_cards:
                try:
                    # Extract job link and URL
                    link = await card.query_selector("a.result__link")
                    if not link:
                        continue

                    href = await link.evaluate("el => el.href")
                    href = clean_job_url(href, url)

                    if not href or href in seen_urls:
                        continue

                    # Extract title from h3.result__title
                    title_el = await card.query_selector("h3.result__title")
                    title = await title_el.inner_text() if title_el else None

                    if not title:
                        continue

                    # Extract company from h4.result__company
                    company_el = await card.query_selector("h4.result__company")
                    company = await company_el.inner_text() if company_el else None

                    # Extract location from ul.result__category-list__locations span
                    location_el = await card.query_selector("ul.result__category-list__locations span")
                    location = await location_el.inner_text() if location_el else None

                    # Extract date from ul.result__category-list__date span
                    date_el = await card.query_selector("ul.result__category-list__date span")
                    date_str = await date_el.inner_text() if date_el else None

                    days_ago = None
                    posted_date = None

                    if date_str:
                        try:
                            posted = datetime.strptime(date_str.strip(), "%a, %B %d, %Y")
                            days_ago = (datetime.now() - posted).days
                            posted_date = posted.date().isoformat()
                        except ValueError:
                            pass

                    # Filter by days threshold
                    if days_ago is not None and days_ago > days_threshold:
                        continue

                    seen_urls.add(href)
                    page_fresh_count += 1

                    # Filter by manager keywords
                    if not matches_job_title_keywords(title):
                        continue

                    jobs.append(
                        Job(
                            title=title.strip(),
                            company=company.strip() if company else None,
                            source_id=source_id,
                            url=href,
                            location=location.strip() if location else None,
                            posted_date=posted_date,
                            days_ago=days_ago,
                        )
                    )
                    page_jobs_count += 1

                except Exception:
                    continue

            print(f"    Page {page_num}: {page_jobs_count} jobs added ({page_fresh_count} fresh jobs found)")

            # If no fresh jobs on this page, all remaining pages will be older
            if page_fresh_count == 0:
                print("    No more fresh jobs, stopping pagination")
                break

            page_num += 1

            # Safety limit to prevent infinite loops
            if page_num > 20:
                print("    Reached page limit (20), stopping")
                break

        except PlaywrightTimeout:
            print(f"  Timeout loading page {page_num}")
            break
        except Exception as e:
            print(f"  Error on page {page_num}: {e}")
            break

    return jobs


async def scrape_source(page: Page, source: Source, days_threshold: int) -> list[Job]:
    """Route to the appropriate scraper based on the source parser type."""
    if source.parser == "yc":
        return await scrape_yc_jobs(page, source.id, source.name, source.url, days_threshold)
    elif source.parser == "index":
        return await scrape_index_ventures(page, source.id, source.name, source.url, days_threshold)
    elif source.parser == "consider":
        return await scrape_consider_site(page, source.id, source.name, source.url, days_threshold)
    elif source.parser == "getro":
        return await scrape_getro_site(page, source.id, source.name, source.url, days_threshold)
    else:
        print(f"  Unknown parser type: {source.parser}")
        return []


def load_recent_jobs(new_jobs_dir: Path, days: int = 7) -> list[dict]:
    """
    Load jobs from new_jobs files within the specified number of days.
    Returns a list of job dictionaries.
    """
    recent_jobs = []
    cutoff_date = datetime.now() - timedelta(days=days)

    if not new_jobs_dir.exists():
        return recent_jobs

    for filepath in new_jobs_dir.glob("new_jobs_*.json"):
        try:
            # Extract date from filename: new_jobs_2025-12-24_09-23-20.json
            filename = filepath.stem  # new_jobs_2025-12-24_09-23-20
            date_part = filename.replace("new_jobs_", "")  # 2025-12-24_09-23-20
            file_date = datetime.strptime(date_part, "%Y-%m-%d_%H-%M-%S")

            if file_date >= cutoff_date:
                with open(filepath, encoding="utf-8") as f:
                    data = json.load(f)
                    for job in data.get("jobs", []):
                        recent_jobs.append(job)
        except (ValueError, json.JSONDecodeError) as e:
            print(f"Warning: Could not parse {filepath.name}: {e}")
            continue

    return recent_jobs


def build_duplicate_keys(jobs: list[dict]) -> set[str]:
    """
    Build a set of duplicate detection keys from job dictionaries.
    Key format: "company|title" (lowercase, stripped).
    """
    keys = set()
    for job in jobs:
        company = job.get("company")
        title = job.get("title")
        if company and title:
            key = f"{company.lower().strip()}|{title.lower().strip()}"
            keys.add(key)
    return keys


def mark_potential_duplicates(jobs: list[Job], existing_keys: set[str]) -> list[Job]:
    """
    Mark jobs as potential duplicates if their company+title matches existing keys.
    Also marks duplicates within the current batch.
    """
    seen_in_batch: set[str] = set()

    for job in jobs:
        key = job.get_duplicate_key()
        if key:
            if key in existing_keys or key in seen_in_batch:
                job.potential_duplicate = True
            seen_in_batch.add(key)

    return jobs


def load_state(filepath: Path) -> dict[str, SourceState]:
    """Load scraping state from JSON file."""
    if not filepath.exists():
        return {}

    try:
        with open(filepath, encoding="utf-8") as f:
            data = json.load(f)
            return {
                source: SourceState(last_scraped=state["last_scraped"], known_job_urls=state.get("known_job_urls", []))
                for source, state in data.get("sources", {}).items()
            }
    except Exception as e:
        print(f"Warning: Could not load state: {e}")
        return {}


def save_state(state: dict[str, SourceState], filepath: Path):
    """Save scraping state to JSON file."""
    output = {
        "last_updated": datetime.now().isoformat(),
        "sources": {
            source: {
                "last_scraped": s.last_scraped,
                "known_job_urls": s.known_job_urls,
            }
            for source, s in state.items()
        },
    }

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)


def save_jobs(jobs: list[Job], filepath: Path, metadata: dict | None = None):
    """Save jobs to JSON file, sorted from newest to oldest by posted_date."""
    # Sort by posted_date descending (newest first), jobs without date at the end
    sorted_jobs = sorted(jobs, key=lambda j: j.posted_date or "0000-00-00", reverse=True)

    # Exclude days_ago (temporary field) and False-valued potential_duplicate
    exclude_fields = {"days_ago"}

    def job_to_dict(job: Job) -> dict:
        result = {}
        for k, v in asdict(job).items():
            if k in exclude_fields:
                continue
            if v is None:
                continue
            # Only include potential_duplicate if True
            if k == "potential_duplicate" and v is False:
                continue
            result[k] = v
        return result

    output = {
        "scraped_at": datetime.now().isoformat(),
        "total_jobs": len(sorted_jobs),
        "jobs": [job_to_dict(job) for job in sorted_jobs],
    }
    if metadata:
        output.update(metadata)

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)


def find_new_jobs(
    jobs: list[Job],
    source_state: dict[str, SourceState],
) -> list[Job]:
    """
    Find jobs that are new since the last scrape.

    For sources with dates (YC): job is new if posted after last scrape
    For sources without dates: job is new if URL wasn't seen before
    """
    new_jobs = []

    for job in jobs:
        source_id = job.source_id
        state = source_state.get(source_id)

        if state is None:
            # First time scraping this source - all jobs are new
            new_jobs.append(job)
            continue

        # Check if URL is already known
        if job.url in state.known_job_urls:
            continue

        # For sources with dates, also check if posted after last scrape
        if job.posted_date:
            last_scraped = datetime.fromisoformat(state.last_scraped).date()
            posted = datetime.fromisoformat(job.posted_date).date()

            if posted >= last_scraped:
                new_jobs.append(job)
        else:
            # No date info - new if URL is unknown
            new_jobs.append(job)

    return new_jobs


def update_state(
    state: dict[str, SourceState],
    jobs: list[Job],
    scrape_time: datetime,
) -> dict[str, SourceState]:
    """Update state with newly scraped jobs."""
    # Group jobs by source_id
    jobs_by_source: dict[str, list[Job]] = {}
    for job in jobs:
        if job.source_id not in jobs_by_source:
            jobs_by_source[job.source_id] = []
        jobs_by_source[job.source_id].append(job)

    # Update state for each source
    for source_id, source_jobs in jobs_by_source.items():
        current_urls = [job.url for job in source_jobs]

        if source_id in state:
            # Merge with existing URLs (keep history)
            existing_urls = set(state[source_id].known_job_urls)
            all_urls = list(existing_urls | set(current_urls))
            state[source_id] = SourceState(
                last_scraped=scrape_time.isoformat(),
                known_job_urls=all_urls,
            )
        else:
            state[source_id] = SourceState(
                last_scraped=scrape_time.isoformat(),
                known_job_urls=current_urls,
            )

    return state


async def login_yc():
    """
    Interactive login to YC Work at a Startup.

    Opens a browser window for manual login. After successful login,
    saves the browser state to AUTH_STATE_FILE for future use.
    """
    print("Opening browser for YC Work at a Startup login...")
    print("Please log in manually. The browser will close automatically after login.")
    print()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        # Navigate to login page
        await page.goto("https://www.workatastartup.com/companies")

        # Wait for user to log in and press Enter
        print("Log in to your account, then press Enter to save the session...")
        input()

        # Save browser state
        await context.storage_state(path=str(AUTH_STATE_FILE))
        print(f"Auth state saved to: {AUTH_STATE_FILE}")

        await browser.close()
        return True


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Scrape job listings from VC portfolio job boards.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python job_checker.py                      # Scrape all enabled sources
  python job_checker.py --ids yc             # Scrape only Y Combinator
  python job_checker.py --ids a16z sequoia   # Scrape a16z and Sequoia
  python job_checker.py --days 14            # Scrape with 14 days threshold
  python job_checker.py --ids yc --days 30   # Scrape YC with 30 days threshold
  python job_checker.py --list               # List available source IDs
  python job_checker.py --login              # Login to YC Work at a Startup
        """,
    )
    parser.add_argument(
        "--ids",
        "-i",
        nargs="+",
        dest="source_ids",
        metavar="ID",
        help="Source IDs to scrape (default: all enabled sources)",
    )
    parser.add_argument(
        "--list", "-l", action="store_true", dest="list_sources", help="List available source IDs and exit"
    )
    parser.add_argument(
        "--login", action="store_true", help="Login to YC Work at a Startup (saves auth state for future runs)"
    )
    parser.add_argument(
        "--days",
        "-d",
        type=int,
        default=DEFAULT_DAYS_THRESHOLD,
        help=f"Filter jobs posted within N days (default: {DEFAULT_DAYS_THRESHOLD})",
    )
    return parser.parse_args()


async def main():
    """Main entry point."""
    args = parse_args()

    scrape_time = datetime.now()
    timestamp = scrape_time.strftime("%Y-%m-%d_%H-%M-%S")

    output_dir = Path(__file__).parent
    state_file = output_dir / "state.json"
    new_jobs_dir = output_dir / "new_jobs"

    # Create new_jobs directory if it doesn't exist
    new_jobs_dir.mkdir(exist_ok=True)
    new_jobs_file = new_jobs_dir / f"new_jobs_{timestamp}.json"

    # Load sources configuration
    sources_file = output_dir / "sources.json"
    all_sources = load_sources(sources_file)
    if not all_sources:
        print("No sources configured. Check sources.json")
        return

    # Handle --list flag
    if args.list_sources:
        print("Available source IDs:")
        for source in all_sources:
            print(f"  {source.id:<20} {source.name}")
        return

    # Handle --login flag
    if args.login:
        success = await login_yc()
        if success:
            print("Login successful! You can now scrape YC with: python job_checker.py --ids yc")
        return

    # Filter sources by IDs if specified
    if args.source_ids:
        requested_ids = set(args.source_ids)
        sources = [s for s in all_sources if s.id in requested_ids]

        # Check for invalid IDs
        valid_ids = {s.id for s in all_sources}
        invalid_ids = requested_ids - valid_ids
        if invalid_ids:
            print(f"Warning: Unknown source IDs: {', '.join(invalid_ids)}")
            print("Use --list to see available IDs")

        if not sources:
            print("No valid sources specified.")
            return
    else:
        sources = all_sources

    print("=" * 60)
    print("Job Checker - Scraping VC Portfolio Job Boards")
    print(f"Scrape time: {scrape_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Days threshold: {args.days}")
    print("=" * 60)
    print(f"\nScraping {len(sources)} of {len(all_sources)} sources")

    # Load state
    state = load_state(state_file)
    if state:
        print(f"\nLoaded state for {len(state)} sources")
        for source, s in state.items():
            last = datetime.fromisoformat(s.last_scraped)
            print(f"  • {source}: last scraped {last.strftime('%Y-%m-%d %H:%M')}, {len(s.known_job_urls)} known jobs")
    else:
        print("\nNo previous state found - first run")

    all_jobs: list[Job] = []

    # Check if we need auth for YC
    has_auth_state = AUTH_STATE_FILE.exists()
    yc_sources = [s for s in sources if s.parser == "yc"]

    if yc_sources and not has_auth_state:
        print("\nSkipping YC sources (no auth). Run: python job_checker.py --login")
        sources = [s for s in sources if s.parser != "yc"]

    if not sources:
        print("No sources to scrape.")
        return

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        user_agent = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )

        # Create context with auth state if available (for YC)
        if has_auth_state:
            print(f"\nUsing saved auth state from: {AUTH_STATE_FILE}")
            context = await browser.new_context(
                user_agent=user_agent,
                storage_state=str(AUTH_STATE_FILE),
            )
        else:
            context = await browser.new_context(user_agent=user_agent)

        page = await context.new_page()

        for source in sources:
            print(f"\nScraping: {source.name} [{source.id}]")
            print(f"  URL: {source.url}")
            print(f"  Parser: {source.parser}")

            jobs = await scrape_source(page, source, args.days)
            print(f"  Found: {len(jobs)} jobs")

            all_jobs.extend(jobs)

        await browser.close()

    # Remove duplicates based on URL
    unique_jobs = {}
    for job in all_jobs:
        if job.url not in unique_jobs:
            unique_jobs[job.url] = job
    all_jobs = list(unique_jobs.values())

    # Filter jobs within date threshold (for those with dates)
    filtered_jobs = []
    for job in all_jobs:
        if job.days_ago is not None:
            if job.days_ago <= args.days:
                filtered_jobs.append(job)
        else:
            # Include jobs without date info
            filtered_jobs.append(job)

    # Filter jobs by location
    location_filtered_jobs = [job for job in filtered_jobs if matches_location_keywords(job.location)]
    filtered_out_count = len(filtered_jobs) - len(location_filtered_jobs)
    if filtered_out_count > 0:
        print(f"Filtered out {filtered_out_count} jobs by location")
    filtered_jobs = location_filtered_jobs

    # Find new jobs since last scrape
    new_jobs = find_new_jobs(filtered_jobs, state)

    # Mark potential duplicates (same company+title within last 7 days)
    if new_jobs:
        recent_jobs = load_recent_jobs(new_jobs_dir, days=7)
        existing_keys = build_duplicate_keys(recent_jobs)
        new_jobs = mark_potential_duplicates(new_jobs, existing_keys)

    # Update and save state
    state = update_state(state, filtered_jobs, scrape_time)
    save_state(state, state_file)

    # Save new jobs with timestamp
    if new_jobs:
        # Group new jobs by source for metadata
        sources_with_new = list(set(job.source_id for job in new_jobs))
        save_jobs(
            new_jobs,
            new_jobs_file,
            {
                "sources_with_new_jobs": sources_with_new,
                "previous_scrape_times": {
                    source: state[source].last_scraped for source in sources_with_new if source in state
                },
            },
        )

    # Print summary
    duplicate_count = sum(1 for j in new_jobs if j.potential_duplicate) if new_jobs else 0
    unique_count = len(new_jobs) - duplicate_count if new_jobs else 0

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Scrape time: {scrape_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Total jobs found: {len(all_jobs)}")
    print(f"Jobs within {args.days} days: {len(filtered_jobs)}")
    print(f"New jobs since last run: {len(new_jobs)}")
    if new_jobs:
        print(f"  - Unique: {unique_count}")
        print(f"  - Potential duplicates: {duplicate_count}")
    print(f"\nState saved to: {state_file}")
    if new_jobs:
        print(f"New jobs saved to: {new_jobs_file}")

    # Show new jobs by source
    if new_jobs:
        print("\n" + "-" * 60)
        print("NEW JOBS BY SOURCE:")
        print("-" * 60)

        # Group by source
        by_source: dict[str, list[Job]] = {}
        for job in new_jobs:
            if job.source_id not in by_source:
                by_source[job.source_id] = []
            by_source[job.source_id].append(job)

        for source, jobs in sorted(by_source.items()):
            print(f"\n{source} ({len(jobs)} new):")
            for job in jobs[:10]:  # Show first 10 per source
                date_info = f" ({job.days_ago}d ago)" if job.days_ago is not None else ""
                dup_marker = " [DUP]" if job.potential_duplicate else ""
                print(f"  • {job.title} @ {job.company}{date_info}{dup_marker}")
            if len(jobs) > 10:
                print(f"  ... and {len(jobs) - 10} more")


if __name__ == "__main__":
    asyncio.run(main())
