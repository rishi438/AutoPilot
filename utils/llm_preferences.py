"""Resolve a user's persisted provider choice into safe LLM request options."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import get_settings
from models.database import UserWorkflowPreferences


def resolve_llm_request_options(preferences: dict[str, Any] | None) -> dict[str, Any]:
    """Return generation options that enforce the user's provider and model choice."""
    if not preferences:
        return {}

    if preferences.get("ai_provider") != "local":
        selected_model = preferences.get("preferred_model")
        return {"model": selected_model} if selected_model else {}

    settings = get_settings()
    approved_models = set(settings.local_llm_models)
    selected_model = preferences.get("local_model")
    if selected_model not in approved_models:
        selected_model = settings.local_llm_model
    if not selected_model:
        return {}

    return {
        "model": selected_model,
        "force_local": True,
        "local_reasoning_effort": preferences.get("local_reasoning_effort"),
    }


async def get_user_llm_request_options(
    db: AsyncSession, user_id: Any
) -> dict[str, Any]:
    """Load the current persisted provider choice for a user."""
    preferences = (
        await db.execute(
            select(UserWorkflowPreferences).where(
                UserWorkflowPreferences.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    return resolve_llm_request_options(preferences.to_dict() if preferences else None)
