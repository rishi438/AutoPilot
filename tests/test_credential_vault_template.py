from pathlib import Path


def test_existing_portal_password_does_not_request_current_site_password() -> None:
    template = Path("ui/dashboard/credential-vault.html").read_text(encoding="utf-8")
    existing_form = template.split('<form id="existingCredentialForm"', maxsplit=1)[
        1
    ].split("</form>", maxsplit=1)[0]

    assert 'id="existingPassword"' in existing_form
    assert 'autocomplete="new-password"' in existing_form
    assert 'autocomplete="current-password"' not in existing_form
