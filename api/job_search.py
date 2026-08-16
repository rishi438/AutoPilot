"""Job discovery API: saved rules, public board refresh, review queue, and failures."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from requests import RequestException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import JobDiscovery, UserJobSearchPreference
from services.job_search import fetch_indexed_ats_jobs, fetch_source, score_job
from utils.auth import get_current_user_with_complete_profile
from utils.database import get_database

router = APIRouter()
logger = logging.getLogger(__name__)


def _user_id(user: dict[str, Any]) -> uuid.UUID:
    return uuid.UUID(str(user.get("id") or user.get("_id")))


class BoardSource(BaseModel):
    kind: Literal["greenhouse", "lever"]
    board: str = Field(min_length=1, max_length=150, pattern=r"^[A-Za-z0-9._-]+$")
    company_name: str = Field(min_length=1, max_length=500)
    company_tier: Literal["mnc", "indian_mnc", "startup", "mid_startup"]


class JobSearchPreferencesRequest(BaseModel):
    primary_skills: list[str] = Field(min_length=1, max_length=15)
    secondary_skills: list[str] = Field(default_factory=list, max_length=15)
    roles: list[str] = Field(min_length=1, max_length=10)
    company_tiers: list[Literal["mnc", "indian_mnc", "startup", "mid_startup"]] = Field(
        min_length=1
    )
    sources: list[BoardSource] = Field(default_factory=list, max_length=100)
    min_match_score: float = Field(default=0.65, ge=0, le=1)
    require_review_before_apply: bool = True

    @field_validator("primary_skills", "secondary_skills", "roles")
    @classmethod
    def normalize_terms(cls, values: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(v.strip() for v in values if v.strip()))
        if len(cleaned) != len(values):
            raise ValueError("Terms must be non-empty and unique.")
        return cleaned


class DiscoveryResponse(BaseModel):
    id: str
    source: str
    company_name: str
    company_tier: str | None
    title: str
    location: str | None
    job_url: str
    match_score: float
    match_reasons: list[str]
    status: str
    failure_code: str | None
    failure_detail: str | None


def _response(row: JobDiscovery) -> DiscoveryResponse:
    return DiscoveryResponse(
        id=str(row.id),
        source=row.source,
        company_name=row.company_name,
        company_tier=row.company_tier,
        title=row.title,
        location=row.location,
        job_url=row.job_url,
        match_score=row.match_score,
        match_reasons=row.match_reasons or [],
        status=row.status,
        failure_code=row.failure_code,
        failure_detail=row.failure_detail,
    )


@router.put("/preferences")
async def save_preferences(
    body: JobSearchPreferencesRequest,
    current_user: dict[str, Any] = Depends(get_current_user_with_complete_profile),
    db: AsyncSession = Depends(get_database),
):
    user_id = _user_id(current_user)
    row = (
        await db.execute(
            select(UserJobSearchPreference).where(
                UserJobSearchPreference.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    data = body.model_dump()
    data["sources"] = [source.model_dump() for source in body.sources]
    if row is None:
        row = UserJobSearchPreference(user_id=user_id, **data)
        db.add(row)
    else:
        for key, value in data.items():
            setattr(row, key, value)
    await db.commit()
    return {"message": "Job-search preferences saved", "sources": len(data["sources"])}


@router.get("/preferences")
async def get_preferences(
    current_user: dict[str, Any] = Depends(get_current_user_with_complete_profile),
    db: AsyncSession = Depends(get_database),
):
    """Return saved choices and exactly what is still needed before discovery."""
    pref = (
        await db.execute(
            select(UserJobSearchPreference).where(
                UserJobSearchPreference.user_id == _user_id(current_user)
            )
        )
    ).scalar_one_or_none()
    if pref is None:
        return {
            "primary_skills": [],
            "secondary_skills": [],
            "roles": [],
            "company_tiers": [],
            "sources": [],
            "min_match_score": 0.65,
            "require_review_before_apply": True,
            "missing": ["primary skills", "target roles", "company tiers"],
        }
    missing = []
    if not pref.primary_skills:
        missing.append("primary skills")
    if not pref.roles:
        missing.append("target roles")
    if not pref.company_tiers:
        missing.append("company tiers")
    return {
        "primary_skills": pref.primary_skills,
        "secondary_skills": pref.secondary_skills,
        "roles": pref.roles,
        "company_tiers": pref.company_tiers,
        "sources": pref.sources,
        "min_match_score": pref.min_match_score,
        "require_review_before_apply": pref.require_review_before_apply,
        "missing": missing,
    }


@router.post("/refresh", response_model=list[DiscoveryResponse])
async def refresh_discoveries(
    current_user: dict[str, Any] = Depends(get_current_user_with_complete_profile),
    db: AsyncSession = Depends(get_database),
):
    user_id = _user_id(current_user)
    pref = (
        await db.execute(
            select(UserJobSearchPreference).where(
                UserJobSearchPreference.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    if pref is None:
        raise HTTPException(400, "Save job-search preferences before discovering jobs.")
    rows: list[JobDiscovery] = []
    failures: list[str] = []
    for source in pref.sources:
        try:
            jobs = await fetch_source(source)
        except Exception as exc:
            logger.warning(
                "Job-board refresh failed for %s: %s", source.get("company_name"), exc
            )
            failures.append(f"{source.get('company_name')}: board unavailable")
            continue
        for job in jobs:
            if job.company_tier not in pref.company_tiers:
                continue
            score, reasons = score_job(
                job,
                primary_skills=pref.primary_skills,
                secondary_skills=pref.secondary_skills,
                roles=pref.roles,
            )
            if score < pref.min_match_score:
                continue
            existing = (
                await db.execute(
                    select(JobDiscovery).where(
                        JobDiscovery.user_id == user_id,
                        JobDiscovery.source == job.source,
                        JobDiscovery.external_id == job.external_id,
                    )
                )
            ).scalar_one_or_none()
            values = dict(
                company_name=job.company_name,
                company_tier=job.company_tier,
                title=job.title,
                location=job.location,
                job_url=job.job_url,
                description=job.description,
                match_score=score,
                match_reasons=reasons,
                status=(
                    "review_required"
                    if pref.require_review_before_apply
                    else "ready_to_apply"
                ),
                failure_code=None,
                failure_detail=None,
            )
            if existing is None:
                existing = JobDiscovery(
                    user_id=user_id,
                    source=job.source,
                    external_id=job.external_id,
                    **values,
                )
                db.add(existing)
            else:
                for key, value in values.items():
                    setattr(existing, key, value)
            rows.append(existing)
    for role in pref.roles:
        try:
            jobs = await fetch_indexed_ats_jobs(role)
        except (ImportError, OSError, RequestException) as exc:
            logger.warning("Indexed ATS discovery failed for role %s: %s", role, exc)
            continue
        for job in jobs:
            existing = (
                await db.execute(
                    select(JobDiscovery).where(
                        JobDiscovery.user_id == user_id,
                        JobDiscovery.source == job.source,
                        JobDiscovery.external_id == job.external_id,
                    )
                )
            ).scalar_one_or_none()
            values = dict(
                company_name=job.company_name,
                company_tier=None,
                title=job.title,
                location=job.location,
                job_url=job.job_url,
                description=None,
                match_score=pref.min_match_score,
                match_reasons=["Role query matched", "Public ATS URL resolved"],
                status="review_required",
                failure_code=None,
                failure_detail=None,
            )
            if existing is None:
                existing = JobDiscovery(
                    user_id=user_id,
                    source=job.source,
                    external_id=job.external_id,
                    **values,
                )
                db.add(existing)
            else:
                for key, value in values.items():
                    setattr(existing, key, value)
            rows.append(existing)
    await db.commit()
    if failures:
        logger.info("Partial job-board refresh: %s", "; ".join(failures))
    return [_response(row) for row in rows]


@router.get("/discoveries", response_model=list[DiscoveryResponse])
async def list_discoveries(
    current_user: dict[str, Any] = Depends(get_current_user_with_complete_profile),
    db: AsyncSession = Depends(get_database),
):
    rows = (
        (
            await db.execute(
                select(JobDiscovery)
                .where(JobDiscovery.user_id == _user_id(current_user))
                .order_by(
                    JobDiscovery.match_score.desc(), JobDiscovery.discovered_at.desc()
                )
            )
        )
        .scalars()
        .all()
    )
    return [_response(row) for row in rows]


@router.post("/discoveries/{discovery_id}/attempt", response_model=DiscoveryResponse)
async def record_attempt(
    discovery_id: uuid.UUID,
    outcome: Literal["opened", "filled", "applied", "failed"],
    failure_code: str | None = Query(default=None, max_length=80),
    failure_detail: str | None = Query(default=None, max_length=2000),
    current_user: dict[str, Any] = Depends(get_current_user_with_complete_profile),
    db: AsyncSession = Depends(get_database),
):
    row = (
        await db.execute(
            select(JobDiscovery).where(
                JobDiscovery.id == discovery_id,
                JobDiscovery.user_id == _user_id(current_user),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Discovered job not found")
    if outcome == "failed" and not failure_code:
        raise HTTPException(422, "failure_code is required for a failed attempt")
    row.status, row.failure_code, row.failure_detail, row.updated_at = (
        outcome,
        failure_code if outcome == "failed" else None,
        failure_detail if outcome == "failed" else None,
        datetime.now(UTC),
    )
    await db.commit()
    return _response(row)
