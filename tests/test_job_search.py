from services.job_search import FoundJob, canonical_ats_url, score_job


def _job(title: str, description: str = "") -> FoundJob:
    return FoundJob(
        "greenhouse",
        "1",
        "Example",
        "mnc",
        title,
        None,
        "https://example.test/jobs/1",
        description,
    )


def test_score_without_requested_role_does_not_meet_default_threshold() -> None:
    score, reasons = score_job(
        _job("Python Developer", "Python Rust"),
        primary_skills=["Python"],
        secondary_skills=["Rust"],
        roles=["DevOps"],
    )
    assert score == 0.25
    assert reasons == [
        "Primary skills in posting: Python",
        "Secondary skills in posting: Rust",
    ]


def test_score_rewards_role_and_primary_skills() -> None:
    score, reasons = score_job(
        _job("Senior Software Engineer", "Python services"),
        primary_skills=["Python"],
        secondary_skills=[],
        roles=["Software Engineer"],
    )
    assert score == 0.65
    assert reasons == [
        "Role match: Software Engineer",
        "Primary skills in posting: Python",
    ]


def test_canonical_ats_url_removes_tracking_parameters_for_cross_portal_deduplication() -> (
    None
):
    assert (
        canonical_ats_url(
            "https://ACME.wd1.myworkdayjobs.com/en-US/jobs/job/123/?utm_source=naukri&ref=x"
        )
        == "https://acme.wd1.myworkdayjobs.com/en-US/jobs/job/123"
    )
