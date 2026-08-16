"""Public ATS-board discovery and transparent, deterministic job matching."""

from __future__ import annotations

import asyncio
import hashlib
import random
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx

from utils.cache import CACHE_VERSION, cache_get, cache_set


SUPPORTED_SOURCES = frozenset({"greenhouse", "lever"})
VALID_COMPANY_TIERS = frozenset({"mnc", "indian_mnc", "startup", "mid_startup"})
INDEXED_ATS_CACHE_TTL_SECONDS = 60 * 60 * 24
_INDEXED_ATS_RESULT_LIMIT = 25
_TRACKING_PARAMETERS = frozenset({"gclid", "fbclid", "ref", "source"})
_ATS_URL_PATTERN = re.compile(
    r"https?://[^\s\"'<>]*(?:myworkdayjobs\.com|(?:job-)?boards\.greenhouse\.io)[^\s\"'<>]*",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class FoundJob:
    source: str
    external_id: str
    company_name: str
    company_tier: str | None
    title: str
    location: str | None
    job_url: str
    description: str


def canonical_ats_url(url: str) -> str | None:
    """Normalize a public Workday or Greenhouse URL for cross-portal deduplication."""
    parsed = urlparse(url)
    host = parsed.hostname.lower() if parsed.hostname else ""
    if not (host.endswith("myworkdayjobs.com") or host.endswith("greenhouse.io")):
        return None
    query = urlencode(
        sorted(
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.lower().startswith("utm_")
            and key.lower() not in _TRACKING_PARAMETERS
        )
    )
    return urlunparse(("https", host, parsed.path.rstrip("/"), "", query, ""))


def _ats_url_from_html(page_html: str) -> str | None:
    for match in _ATS_URL_PATTERN.finditer(page_html):
        canonical_url = canonical_ats_url(match.group(0))
        if canonical_url:
            return canonical_url
    return None


def _search_google(query: str) -> list[str]:
    """Run one bounded query using the pinned googlesearch-python package."""
    from googlesearch import search

    return list(
        search(
            query,
            num_results=_INDEXED_ATS_RESULT_LIMIT,
            lang="en",
            sleep_interval=random.uniform(1.0, 2.5),
        )
    )


async def _resolve_ats_url(candidate_url: str, client: httpx.AsyncClient) -> str | None:
    canonical_url = canonical_ats_url(candidate_url)
    if canonical_url:
        return canonical_url
    response = await client.get(candidate_url)
    response.raise_for_status()
    return _ats_url_from_html(response.text)


def _indexed_cache_key(role: str, location: str) -> str:
    digest = hashlib.sha256(
        f"{role.casefold()}\0{location.casefold()}".encode()
    ).hexdigest()
    return f"{CACHE_VERSION}:indexed_ats:{digest}"


async def fetch_indexed_ats_jobs(role: str, location: str = "") -> list[FoundJob]:
    """Find public Workday/Greenhouse links via Google, caching results for 24 hours."""
    cache_key = _indexed_cache_key(role, location)
    cached = await cache_get(cache_key)
    if cached and isinstance(cached.get("data"), list):
        return [FoundJob(**row) for row in cached["data"] if isinstance(row, dict)]

    location_terms = f' "{location}"' if location else ""
    queries = [
        f'site:myworkdayjobs.com "{role}"{location_terms}',
        f'site:boards.greenhouse.io "{role}"{location_terms}',
        f'site:naukri.com "{role}"{location_terms}',
        f'site:instahyre.com "{role}"{location_terms}',
        f'site:hirist.com "{role}"{location_terms}',
    ]
    candidates: list[str] = []
    for index, query in enumerate(queries):
        candidates.extend(await asyncio.to_thread(_search_google, query))
        if index < len(queries) - 1:
            await asyncio.to_thread(time.sleep, random.uniform(1.0, 2.5))

    found: dict[str, FoundJob] = {}
    timeout = httpx.Timeout(15.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        for candidate_url in dict.fromkeys(candidates):
            try:
                ats_url = await _resolve_ats_url(candidate_url, client)
            except httpx.HTTPError:
                continue
            if not ats_url or ats_url in found:
                continue
            hostname = urlparse(ats_url).hostname or "Unknown employer"
            source = "workday" if "myworkdayjobs.com" in hostname else "greenhouse"
            found[ats_url] = FoundJob(
                source=source,
                external_id=hashlib.sha256(ats_url.encode()).hexdigest(),
                company_name=hostname,
                company_tier=None,
                title=role,
                location=location or None,
                job_url=ats_url,
                description="",
            )

    jobs = list(found.values())
    await cache_set(
        cache_key,
        [
            {field: getattr(job, field) for field in job.__dataclass_fields__}
            for job in jobs
        ],
        INDEXED_ATS_CACHE_TTL_SECONDS,
    )
    return jobs


def score_job(
    job: FoundJob,
    *,
    primary_skills: list[str],
    secondary_skills: list[str],
    roles: list[str],
) -> tuple[float, list[str]]:
    """Score only supplied preferences; never infer qualifications or company data."""
    text = f"{job.title} {job.description}".casefold()
    reasons: list[str] = []
    role_hits = [role for role in roles if role.casefold() in job.title.casefold()]
    primary_hits = [skill for skill in primary_skills if skill.casefold() in text]
    secondary_hits = [skill for skill in secondary_skills if skill.casefold() in text]
    if role_hits:
        reasons.append("Role match: " + ", ".join(role_hits))
    if primary_hits:
        reasons.append("Primary skills in posting: " + ", ".join(primary_hits))
    if secondary_hits:
        reasons.append("Secondary skills in posting: " + ", ".join(secondary_hits))
    # Role drives relevance; secondary skills cannot make an unrelated role eligible.
    score = min(
        1.0,
        (0.45 if role_hits else 0)
        + min(0.4, 0.2 * len(primary_hits))
        + min(0.15, 0.05 * len(secondary_hits)),
    )
    return score, reasons


async def fetch_source(source: dict[str, Any]) -> list[FoundJob]:
    """Fetch a user-configured, public Greenhouse or Lever board. No credentials are stored."""
    kind = str(source["kind"]).casefold()
    board = str(source["board"]).strip()
    company_name = str(source["company_name"]).strip()
    tier = source.get("company_tier")
    if kind not in SUPPORTED_SOURCES or not board or not company_name:
        raise ValueError("Each source needs a supported kind, board, and company_name.")
    timeout = httpx.Timeout(15.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        if kind == "greenhouse":
            response = await client.get(
                f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs",
                params={"content": "true"},
            )
            response.raise_for_status()
            return [
                FoundJob(
                    kind,
                    str(row["id"]),
                    company_name,
                    tier,
                    row["title"],
                    (row.get("location") or {}).get("name"),
                    row["absolute_url"],
                    row.get("content") or "",
                )
                for row in response.json().get("jobs", [])
            ]
        response = await client.get(
            f"https://api.lever.co/v0/postings/{board}", params={"mode": "json"}
        )
        response.raise_for_status()
        return [
            FoundJob(
                kind,
                str(row["id"]),
                company_name,
                tier,
                row["text"],
                (row.get("categories") or {}).get("location"),
                row["hostedUrl"],
                row.get("descriptionPlain") or row.get("description") or "",
            )
            for row in response.json()
        ]
