"""
Configuration settings for the job checker.

This module contains all configurable settings including:
- Keywords for filtering job titles
- Default days threshold for job freshness
- URL filter parameters for different platforms
"""

# Default number of days to look back for fresh jobs
DEFAULT_DAYS_THRESHOLD = 7

# Keywords to filter job titles (case-insensitive)
# Used for sources that don't support URL-based filtering (YC, Index Ventures)
JOB_TITLE_KEYWORDS = [
    "engineering manager",
    "engineering lead",
    "head of engineering",
]

# Location keywords for filtering jobs (case-insensitive)
# Jobs are included if location contains any of these keywords, or if location is None
LOCATION_KEYWORDS = [
    "ca",
    "ca ",
    "california",
    "san francisco",
    "mountain view",
    "palo alto",
    "san jose",
    "los gatos",
    "sunnyvale",
    "santa clara",
    "cupertino",
    "menlo park",
    "redwood city",
    "remote",
    "anywhere",
    "usa",
    "us,",
    "us ",
    "united states",
]

# URL filter parameters for different platforms
# These are appended to base URLs when loading sources
URL_FILTERS = {
    "consider": "jobTypes=Engineering+Manager",
    "getro": "q=engineering%20manager",
}
