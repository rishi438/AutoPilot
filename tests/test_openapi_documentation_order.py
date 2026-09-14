"""OpenAPI documentation presentation settings."""

from main import app


def test_swagger_ui_sorts_all_tags_and_operations_alphabetically() -> None:
    assert app.swagger_ui_parameters["tagsSorter"] == "alpha"
    assert app.swagger_ui_parameters["operationsSorter"] == "alpha"
