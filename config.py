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

# URL filter parameters for different platforms
# These are appended to base URLs when loading sources
URL_FILTERS = {
    "consider": "jobTypes=Engineering+Manager",
    "getro": "q=engineering%20manager",
}
