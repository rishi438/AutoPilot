"""Constants"""

LLM_REASONING_TIMEOUTS = {"low": 190, "medium": 310, "high": 610}

_INVALID_JOB_TITLES = frozenset(
    {"unknown", "n/a", "na", "none", "null", "job application"}
)
