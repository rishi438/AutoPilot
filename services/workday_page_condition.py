"""Credential-free runtime condition for one Workday Unit 1 page."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from services.portal_account_automation import NativeAccountPageState
from services.workday_transition_contracts import WorkdayFailureClass

_PRE_SUBMIT_FAILURES = frozenset(
    {
        WorkdayFailureClass.JOB_UNAVAILABLE,
        WorkdayFailureClass.PRE_SUBMIT_CAPTCHA_OR_OTP,
        WorkdayFailureClass.PRE_SUBMIT_TRANSIENT,
        WorkdayFailureClass.WRONG_ORIGIN_OR_TENANT,
    }
)


@dataclass(frozen=True, slots=True)
class WorkdayUnit1PageCondition:
    """Typed, credential-free facts captured at one page boundary."""

    native_state: NativeAccountPageState
    pre_submit_failure: WorkdayFailureClass | None = None
    already_authenticated: bool = False
    account_dialog_active: bool = False
    confirmed_account_lock: bool = False
    trusted_unlock_time: datetime | None = None

    def __post_init__(self) -> None:
        if self.pre_submit_failure not in (None, *_PRE_SUBMIT_FAILURES):
            raise ValueError(
                "The page condition contains an invalid pre-submit failure."
            )
        if self.confirmed_account_lock != (
            self.native_state is NativeAccountPageState.ACCOUNT_TEMPORARILY_LOCKED
        ):
            raise ValueError(
                "The page condition lock fact does not match native state."
            )
        if self.trusted_unlock_time is not None:
            if (
                self.trusted_unlock_time.tzinfo is None
                or self.trusted_unlock_time.utcoffset() is None
                or self.trusted_unlock_time.utcoffset().total_seconds() != 0
            ):
                raise ValueError("The trusted unlock time must be UTC.")
            object.__setattr__(
                self, "trusted_unlock_time", self.trusted_unlock_time.astimezone(UTC)
            )


class WorkdayPageConditionError(RuntimeError):
    """A typed pre-submit condition that must be routed before structure work."""

    def __init__(self, condition: WorkdayUnit1PageCondition) -> None:
        if condition.pre_submit_failure is None:
            raise ValueError("A condition error requires a pre-submit failure.")
        super().__init__("A trusted Workday page condition requires routing.")
        self.condition = condition
        self.failure_class = condition.pre_submit_failure
