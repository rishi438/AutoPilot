from sqlalchemy import CheckConstraint

from models.database import AutomationWorkerDevice


def test_worker_device_schema_allows_only_supported_workday_scopes() -> None:
    constraints = {
        constraint.name: str(constraint.sqltext)
        for constraint in AutomationWorkerDevice.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert constraints == {
        "ck_worker_device_scope": (
            "scope IN ('workday_account_gate', 'workday_application')"
        )
    }
