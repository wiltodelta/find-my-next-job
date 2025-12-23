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

from config import DEFAULT_DAYS_THRESHOLD, JOB_TITLE_KEYWORDS, URL_FILTERS


@dataclass
class Job:
    """Represents a job listing."""

    title: str
    company: str | None
    source_id: str  # Source identifier for state tracking
    url: str
    posted_date: str | None = None  # ISO format date string or None
    days_ago: int | None = None
    scraped_at: str = ""

    def __post_init__(self):
        if not self.scraped_at:
            self.scraped_at = datetime.now().isoformat()


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

    Page structure for each job card:
    - Company section with logo and name
    - Job cards with: heading (job title), badges (date, salary, location), Apply link
    - Date is in element with class 'job-list-badge-posted'
    """
    jobs = []

    try:
        await page.goto(url, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(2000)  # Extra wait for JS rendering

        # Scroll to load all jobs
        for _ in range(5):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1000)

        # Find all job title headings (h2 or h3 with links inside job cards)
        headings = await page.query_selector_all("h2.job-list-job-title, h3.job-list-job-title, h2, h3")

        for heading in headings:
            try:
                # Get the job title link inside the heading
                title_link = await heading.query_selector("a")
                if not title_link:
                    continue

                title = await title_link.inner_text()
                href = await title_link.evaluate("el => el.href")
                href = clean_job_url(href, url)

                if not title or not href:
                    continue

                # Skip non-job links
                if any(skip in href.lower() for skip in ["privacy", "terms", "about", "blog", "contact"]):
                    continue

                # Find company name and posting date
                company = None
                days_ago = None
                posted_date = None

                # Get the job card container (climb up to find the card)
                # Structure: heading -> job-details -> job-card (contains company and date badge)
                card = await heading.evaluate_handle(
                    """el => {
                        // Try to find the job card container
                        let parent = el.parentElement;
                        for (let i = 0; i < 5 && parent; i++) {
                            // Look for a container that has the posted badge
                            if (parent.querySelector && parent.querySelector('.job-list-badge-posted')) {
                                return parent;
                            }
                            parent = parent.parentElement;
                        }
                        // Fallback to grandparent
                        return el.parentElement?.parentElement?.parentElement;
                    }"""
                )

                if card:
                    # Look for date badge with class 'job-list-badge-posted'
                    date_badge = await card.query_selector(".job-list-badge-posted")  # type: ignore
                    if date_badge:
                        date_text = await date_badge.inner_text()
                        if date_text:
                            days_ago_parsed = parse_relative_date(date_text)
                            if days_ago_parsed is not None:
                                days_ago = days_ago_parsed
                                posted_date = (datetime.now() - timedelta(days=days_ago)).date().isoformat()

                    # If no badge found, try text content
                    if days_ago is None:
                        card_text = await card.evaluate("el => el.textContent")  # type: ignore
                        if card_text and "posted" in card_text.lower():
                            days_ago_parsed = parse_relative_date(card_text)
                            if days_ago_parsed is not None:
                                days_ago = days_ago_parsed
                                posted_date = (datetime.now() - timedelta(days=days_ago)).date().isoformat()

                    # Find company name from logo alt text or parent container
                    # Company logos have alt like "Harness logo", "Mews logo", etc.
                    company_name = await card.evaluate(
                        """el => {
                            // Look for company logo in parent containers
                            let parent = el;
                            for (let i = 0; i < 8 && parent; i++) {
                                // Look for logo image with alt containing "logo"
                                const logo = parent.querySelector('img[alt*="logo"]');
                                if (logo) {
                                    const alt = logo.getAttribute('alt') || '';
                                    // Extract company name by removing " logo" suffix
                                    const name = alt.replace(/\\s*logo\\s*$/i, '').trim();
                                    if (name && name.length > 0) {
                                        return name;
                                    }
                                }
                                parent = parent.parentElement;
                            }
                            return null;
                        }"""
                    )  # type: ignore

                    if company_name:
                        company = company_name

                    # Fallback: look for company link that's not job title or Apply
                    if not company:
                        all_links = await card.query_selector_all("a")  # type: ignore
                        for link in all_links:
                            link_text = await link.inner_text()
                            link_href = await link.evaluate("el => el.href")

                            if not link_text or not link_text.strip():
                                continue

                            text_clean = link_text.strip()
                            if not text_clean:
                                continue

                            # Skip job title link, Apply button, and Read more
                            if (
                                link_href == href
                                or text_clean.lower() == title.strip().lower()
                                or text_clean.lower() == "apply"
                                or "read more" in text_clean.lower()
                            ):
                                continue
                            # Skip job board links
                            if any(x in link_href for x in ["greenhouse", "lever", "ashby", "gem.com"]):
                                continue
                            # Skip navigation/utility links
                            if any(x in link_href.lower() for x in ["privacy", "terms", "about", "blog", "all jobs"]):
                                continue
                            if "all jobs" in text_clean.lower() or "matching job" in text_clean.lower():
                                continue

                            # This is likely the company name
                            company = text_clean
                            break

                # Fallback company extraction from URL
                if not company:
                    if "greenhouse.io/" in href:
                        m = re.search(r"greenhouse\.io/([^/]+)", href)
                        if m:
                            company = m.group(1).replace("-", " ").title()
                    elif "ashbyhq.com/" in href:
                        m = re.search(r"ashbyhq\.com/([^/?]+)", href)
                        if m:
                            company = m.group(1).replace("-", " ").title()
                    elif "lever.co/" in href:
                        m = re.search(r"lever\.co/([^/]+)", href)
                        if m:
                            company = m.group(1).replace("-", " ").title()
                    elif "gem.com/" in href:
                        m = re.search(r"gem\.com/([^/?]+)", href)
                        if m:
                            company = m.group(1).replace("-", " ").title()

                # Filter by manager keywords
                if not matches_job_title_keywords(title):
                    continue

                # Filter by days threshold if date available
                if days_ago is not None and days_ago > days_threshold:
                    continue

                jobs.append(
                    Job(
                        title=title.strip(),
                        company=company.strip() if company else None,
                        source_id=source_id,
                        url=href,
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
    These sites usually have search queries in URL and show dates like "3 days".
    """
    jobs = []

    try:
        await page.goto(url, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(3000)

        # Find all job title headings (h4 is common for Getro)
        headings = await page.query_selector_all("h4")

        for heading in headings:
            try:
                # Get the job title link inside or as the heading
                title_link = await heading.query_selector("a")
                if not title_link:
                    # In some Getro versions, h4 itself might not have <a>, but <a> is nearby
                    parent = await heading.evaluate_handle("el => el.parentElement")
                    if parent:
                        title_link = await parent.query_selector("a")  # type: ignore

                if not title_link:
                    continue

                title = await title_link.inner_text()
                href = await title_link.evaluate("el => el.href")
                href = clean_job_url(href, url)

                if not title or not href:
                    continue

                # Find company name and posting date
                company = None
                days_ago = None
                posted_date = None

                # Get container
                container = await heading.evaluate_handle(
                    'el => el.closest(\'div[class*="item"], div[class*="card"], li\')'
                )
                if not container:
                    container = await heading.evaluate_handle("el => el.parentElement?.parentElement")

                if container:
                    # Get text for date parsing
                    container_text = await container.evaluate("el => el.textContent")  # type: ignore
                    if container_text:
                        text_lower = container_text.lower()
                        if "today" in text_lower or "just now" in text_lower:
                            days_ago = 0
                        elif "yesterday" in text_lower:
                            days_ago = 1
                        else:
                            # Look for "X days", "X hours", etc.
                            date_match = re.search(r"(\d+)\s*(?:hour|day|week|month)s?", text_lower)
                            if date_match:
                                days_ago_parsed = parse_relative_date(date_match.group(0))
                                if days_ago_parsed is not None:
                                    days_ago = days_ago_parsed

                        if days_ago is not None:
                            posted_date = (datetime.now() - timedelta(days=days_ago)).date().isoformat()

                    # Find company (usually another link in the same container)
                    all_links = await container.query_selector_all("a")  # type: ignore
                    for link in all_links:
                        link_text = await link.inner_text()
                        link_href = await link.evaluate("el => el.href")

                        link_text_clean = link_text.strip()
                        if not link_text_clean:
                            continue

                        # Skip title link and "Read more" links
                        if (
                            link_href == href
                            or link_text_clean.lower() == title.strip().lower()
                            or "read more" in link_text_clean.lower()
                            or link_text_clean.lower() == "apply"
                        ):
                            continue

                        company = link_text_clean
                        break

                # Filter by manager keywords
                if not matches_job_title_keywords(title):
                    continue

                # Filter by days threshold
                if days_ago is not None and days_ago > days_threshold:
                    continue

                jobs.append(
                    Job(
                        title=title.strip(),
                        company=company.strip() if company else None,
                        source_id=source_id,
                        url=href,
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
    Scrape jobs from Y Combinator's Work at a Startup.
    This site shows posting dates in format 'X days ago'.

    Page structure:
    - Each job card has a company link with date: "Company (Batch) • Description (X days ago)"
    - Below that is the job title link
    - Then job metadata (fulltime, location, etc.)
    """
    jobs = []

    try:
        await page.goto(url, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(3000)  # Wait for dynamic content

        # Find all job cards - they contain company info links with dates
        # Structure: link with "Company (Batch) • Description (X days ago)"
        company_links = await page.query_selector_all('a[href*="/companies/"]')

        for company_link in company_links:
            try:
                company_text = await company_link.inner_text()

                if not company_text:
                    continue

                # Check if this link contains date info (indicates it's a job listing header)
                if "ago)" not in company_text.lower():
                    continue

                # Extract date from parentheses at the end: "(7 days ago)"
                date_match = re.search(r"\(([^)]*(?:ago|hour|day|week|month)[^)]*)\)$", company_text, re.IGNORECASE)
                days_ago = None
                if date_match:
                    date_text = date_match.group(1)
                    days_ago = parse_relative_date(date_text)
                    # Remove date from text
                    company_text = company_text[: date_match.start()].strip()

                # Parse company name: "SnapMagic (S15) • AI copilot for electronics design"
                parts = company_text.split("•")
                company = parts[0].strip() if parts else None

                # Find the parent container and look for the job title link
                parent = await company_link.evaluate_handle("el => el.closest('div')?.parentElement")
                if not parent:
                    continue

                # Find the job title link (usually a sibling or nearby element)
                job_title_link = await parent.query_selector('a[href*="/jobs/"]')  # type: ignore
                if not job_title_link:
                    # Try another level up
                    grandparent = await parent.evaluate_handle("el => el.parentElement")
                    if grandparent:
                        job_title_link = await grandparent.query_selector('a[href*="/jobs/"]')  # type: ignore

                title = "Software Engineer"
                href = ""

                if job_title_link:
                    title = await job_title_link.inner_text()
                    href = await job_title_link.evaluate("el => el.href")
                    href = clean_job_url(href, url)

                if not title or not href:
                    continue

                # Filter by manager keywords
                if not matches_job_title_keywords(title):
                    continue

                # Filter by days threshold if date available
                if days_ago is not None and days_ago > days_threshold:
                    continue

                # Calculate posted_date from days_ago
                posted_date = None
                if days_ago is not None:
                    posted_date = (datetime.now() - timedelta(days=days_ago)).date().isoformat()

                jobs.append(
                    Job(
                        title=title.strip(),
                        company=company.strip() if company else None,
                        source_id=source_id,
                        url=href,
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


async def scrape_index_ventures(
    page: Page, source_id: str, source_name: str, base_url: str, days_threshold: int
) -> list[Job]:
    """
    Scrape jobs from Index Ventures startup jobs page.
    Navigates through multiple pages and filters by date (last 7 days).

    Job format in link name: "Title Company Location... Category Stage Size Date"
    Example: "Engineering Team Leader Remote Remote Asia Administration Future Of Work Series C
    1000+ Mon, December 22, 2025"
    """
    jobs = []
    seen_urls = set()

    # Parse pages until we find only old jobs (>7 days)
    page_num = 1
    while True:
        try:
            # Build URL with page number
            # If base_url ends with /1, replace it; otherwise append page number
            if base_url.endswith("/1"):
                url = base_url.replace("/1", f"/{page_num}")
            elif base_url.endswith("/"):
                url = f"{base_url}{page_num}"
            else:
                url = f"{base_url}/{page_num}"

            await page.goto(url, wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(3000)

            # Find all links
            all_links = await page.query_selector_all("a")

            page_jobs_count = 0
            page_fresh_count = 0  # Count fresh jobs (within threshold) before filtering
            for link in all_links:
                try:
                    href = await link.evaluate("el => el.href")
                    href = clean_job_url(href, url)
                    name = await link.get_attribute("aria-label") or await link.inner_text()

                    if not href or not name:
                        continue

                    # Remove newlines and normalize whitespace
                    name = " ".join(name.split())

                    # Check if link contains a date pattern at the end
                    date_match = re.search(r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s+(\w+)\s+(\d+),\s+(\d{4})$", name)
                    if not date_match:
                        continue

                    # Avoid duplicates
                    if href in seen_urls:
                        continue
                    seen_urls.add(href)

                    # Remove date from name to get job info
                    job_text = name[: date_match.start()].strip()

                    # Parse date
                    date_str = date_match.group(0)
                    try:
                        posted = datetime.strptime(date_str, "%a, %B %d, %Y")
                        days_ago = (datetime.now() - posted).days
                        posted_date = posted.date().isoformat()
                    except Exception:
                        continue

                    # Filter by days threshold
                    if days_ago > days_threshold:
                        continue

                    page_fresh_count += 1  # This job is within the date threshold

                    # Split by spaces to extract title
                    # Format: "Title CompanyName Location... Category Stage Size"
                    parts = job_text.split()
                    if len(parts) < 2:
                        continue

                    # Title is typically the first few words before company name
                    # Let's take first 3-5 words as title
                    title = " ".join(parts[:5])  # Take first 5 words as title approximation

                    # Company can be extracted from URL or text (tricky)
                    company = None
                    if "/startup-jobs/" in href:
                        # URL format: /startup-jobs/company-name/...
                        url_parts = href.split("/")
                        # After absolute URL normalization, /startup-jobs/ is at index 3 or 4
                        # https://www.indexventures.com/startup-jobs/company/title
                        try:
                            idx = url_parts.index("startup-jobs")
                            if len(url_parts) > idx + 1:
                                company = url_parts[idx + 1].replace("-", " ").title()
                        except ValueError:
                            pass

                    # Filter by manager keywords
                    if not matches_job_title_keywords(title):
                        continue

                    jobs.append(
                        Job(
                            title=title.strip(),
                            company=company.strip() if company else None,
                            source_id=source_id,
                            url=href,
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

    # Exclude days_ago (temporary field used for date calculation) and None values
    exclude_fields = {"days_ago"}
    output = {
        "scraped_at": datetime.now().isoformat(),
        "total_jobs": len(sorted_jobs),
        "jobs": [
            {k: v for k, v in asdict(job).items() if v is not None and k not in exclude_fields} for job in sorted_jobs
        ],
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

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        user_agent = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
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

    # Find new jobs since last scrape
    new_jobs = find_new_jobs(filtered_jobs, state)

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
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Scrape time: {scrape_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Total jobs found: {len(all_jobs)}")
    print(f"Jobs within {args.days} days: {len(filtered_jobs)}")
    print(f"New jobs since last run: {len(new_jobs)}")
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
                print(f"  • {job.title} @ {job.company}{date_info}")
            if len(jobs) > 10:
                print(f"  ... and {len(jobs) - 10} more")


if __name__ == "__main__":
    asyncio.run(main())
