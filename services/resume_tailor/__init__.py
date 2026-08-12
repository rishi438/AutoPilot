"""Provide deterministic building blocks for job-specific resume tailoring."""

from .jd_parser import (
    JobDescriptionAnalysis,
    JobDescriptionParseError,
    parse_job_description,
)

__all__ = [
    "JobDescriptionAnalysis",
    "JobDescriptionParseError",
    "parse_job_description",
]
