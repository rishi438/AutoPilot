from types import SimpleNamespace

from utils.llm_preferences import resolve_llm_request_options


def test_local_preference_forces_the_selected_local_model(monkeypatch):
    monkeypatch.setattr(
        "utils.llm_preferences.get_settings",
        lambda: SimpleNamespace(
            local_llm_models={"qwen3:14b": "Qwen 3"},
            local_llm_model="qwen2.5:14b",
        ),
    )

    assert resolve_llm_request_options(
        {
            "ai_provider": "local",
            "local_model": "qwen3:14b",
            "local_reasoning_effort": "high",
        }
    ) == {
        "model": "qwen3:14b",
        "force_local": True,
        "local_reasoning_effort": "high",
    }


def test_stale_local_preference_uses_the_configured_local_default(monkeypatch):
    monkeypatch.setattr(
        "utils.llm_preferences.get_settings",
        lambda: SimpleNamespace(
            local_llm_models={"qwen3:14b": "Qwen 3"},
            local_llm_model="qwen2.5:14b",
        ),
    )

    assert (
        resolve_llm_request_options(
            {"ai_provider": "local", "local_model": "removed-model"}
        )["model"]
        == "qwen2.5:14b"
    )


def test_cloud_preference_uses_the_selected_cloud_model():
    assert resolve_llm_request_options(
        {"ai_provider": "cloud", "preferred_model": "gemini-2.5-pro"}
    ) == {"model": "gemini-2.5-pro"}
