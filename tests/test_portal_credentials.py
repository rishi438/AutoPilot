from __future__ import annotations

import re
from copy import deepcopy

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from api.credential_vault import (
    RevealPortalCredentialRequest,
    StoreExistingPortalCredentialRequest,
)
from config.settings import Settings
from services.portal_credentials import (
    PortalCredentialError,
    PortalCredentialRepository,
    PortalVaultCollections,
    decrypt_portal_password,
    derive_portal_password,
    encrypt_portal_password,
    generate_portal_password,
    normalize_portal_login_url,
    safe_credential_view,
)
from services.portal_account_automation import (
    NativeAccountAction,
    NativeAccountPageState,
    NativePortalAccountCoordinator,
    derive_workday_portal_scope,
)


class _Cursor:
    def __init__(self, documents):
        self.documents = documents

    def sort(self, field, direction):
        self.documents.sort(key=lambda item: item[field], reverse=direction < 0)
        return self

    async def to_list(self, *, length):
        return deepcopy(self.documents[:length])


class _Collection:
    def __init__(self):
        self.documents = []

    def find(self, query):
        return _Cursor(
            [
                deepcopy(document)
                for document in self.documents
                if all(document.get(key) == value for key, value in query.items())
            ]
        )

    async def find_one(self, query, projection=None):
        for document in self.documents:
            if all(document.get(key) == value for key, value in query.items()):
                result = deepcopy(document)
                if projection is not None:
                    result = {key: result[key] for key in projection if key in result}
                return result
        return None

    async def find_one_and_update(self, query, update, *, upsert, return_document):
        del return_document
        for document in self.documents:
            if all(document.get(key) == value for key, value in query.items()):
                document.update(deepcopy(update.get("$set", {})))
                return deepcopy(document)
        if not upsert:
            return None
        document = deepcopy(update.get("$setOnInsert", {}))
        document.update(deepcopy(update.get("$set", {})))
        self.documents.append(document)
        return deepcopy(document)

    async def update_one(self, query, update):
        for document in self.documents:
            if all(document.get(key) == value for key, value in query.items()):
                document.update(deepcopy(update.get("$set", {})))
                for key, value in update.get("$inc", {}).items():
                    document[key] = document.get(key, 0) + value
                return

    async def insert_one(self, document):
        self.documents.append(deepcopy(document))


def _repository() -> tuple[PortalCredentialRepository, _Collection, _Collection]:
    credentials = _Collection()
    events = _Collection()
    key = Fernet.generate_key().decode("ascii")
    repository = PortalCredentialRepository(
        PortalVaultCollections(credentials=credentials, events=events),
        key,
        Fernet.generate_key().decode("ascii"),
    )
    return repository, credentials, events


def _metadata() -> dict[str, str]:
    return {
        "portal_scope": "workday:wf:wellsfargojobs",
        "portal_name": "Wells Fargo Workday",
        "portal_login_url": "https://wd1.myworkdaysite.com/en-US/recruiting/wf/WellsFargoJobs",
        "account_email": "candidate@example.com",
    }


def test_generated_password_satisfies_visible_workday_requirements() -> None:
    password = generate_portal_password()

    assert len(password) == 24
    assert re.search(r"[a-z]", password)
    assert re.search(r"[A-Z]", password)
    assert re.search(r"[0-9]", password)
    assert re.search(r"[^A-Za-z0-9]", password)


def test_derived_password_is_recoverable_but_unique_per_workday_tenant() -> None:
    recovery_key = Fernet.generate_key().decode("ascii")
    values = {
        "recovery_key": recovery_key,
        "account_email": "candidate@example.com",
        "portal_scope": "workday:wf:wellsfargojobs",
    }

    first = derive_portal_password(**values)
    recovered = derive_portal_password(**values)
    other_tenant = derive_portal_password(
        recovery_key=recovery_key,
        account_email="candidate@example.com",
        portal_scope="workday:acme:external",
    )

    assert first == recovered
    assert first != other_tenant
    assert len(first) == 24
    assert re.search(r"[a-z]", first)
    assert re.search(r"[A-Z]", first)
    assert re.search(r"[0-9]", first)
    assert re.search(r"[^A-Za-z0-9]", first)


def test_portal_password_round_trip_never_embeds_plaintext() -> None:
    key = Fernet.generate_key().decode("ascii")

    encrypted = encrypt_portal_password("Unique!Portal9Password", key)

    assert "Unique!Portal9Password" not in encrypted
    assert decrypt_portal_password(encrypted, key) == "Unique!Portal9Password"


def test_portal_url_removes_query_fragment_and_rejects_embedded_credentials() -> None:
    assert (
        normalize_portal_login_url(
            "https://wd1.myworkdaysite.com/account?token=discard#fragment"
        )
        == "https://wd1.myworkdaysite.com/account"
    )
    with pytest.raises(PortalCredentialError, match="must not contain credentials"):
        normalize_portal_login_url("https://user:password@example.com/account")


def test_workday_scope_is_tenant_stable_and_never_uses_job_id() -> None:
    first = derive_workday_portal_scope(
        "https://wd1.myworkdaysite.com/en-US/recruiting/wf/WellsFargoJobs/job/One_R-1"
    )
    second = derive_workday_portal_scope(
        "https://wd1.myworkdaysite.com/en-US/recruiting/wf/WellsFargoJobs/job/Two_R-2/apply"
    )
    other_tenant = derive_workday_portal_scope(
        "https://acme.wd5.myworkdayjobs.com/en-US/External/job/Three_R-3"
    )

    assert first == second == "workday:wf:wellsfargojobs"
    assert other_tenant == "workday:acme:external"
    assert "r-1" not in first and "r-2" not in second


def test_safe_view_excludes_ciphertext_and_user_id() -> None:
    document = {
        "_id": "credential-id",
        "user_id": "user-id",
        "password_encrypted": "ciphertext",
        "portal_scope": "workday:wf:wellsfargojobs",
        "portal_name": "Wells Fargo Workday",
        "portal_login_url": "https://wd1.myworkdaysite.com/account",
        "account_email": "candidate@example.com",
        "credential_source": "generated",
        "status": "pending_registration",
        "created_at": "created",
        "updated_at": "updated",
    }

    safe = safe_credential_view(document)

    assert "password" not in " ".join(safe).lower()
    assert "user_id" not in safe


@pytest.mark.asyncio
async def test_generation_reuses_one_password_per_portal_scope() -> None:
    repository, credentials, events = _repository()

    first, first_created = await repository.generate_if_missing(
        user_id="user-1", **_metadata()
    )
    encrypted_first = credentials.documents[0]["password_encrypted"]
    second, second_created = await repository.generate_if_missing(
        user_id="user-1", **_metadata()
    )

    assert first_created is True
    assert second_created is False
    assert first["id"] == second["id"]
    assert credentials.documents[0]["password_encrypted"] == encrypted_first
    assert [event["event_type"] for event in events.documents] == [
        "credential_generated"
    ]


@pytest.mark.asyncio
async def test_existing_password_is_encrypted_and_owned_reveal_is_audited() -> None:
    repository, credentials, events = _repository()
    await repository.store_existing(
        user_id="user-1",
        password="Existing!Portal9Password",
        **_metadata(),
    )
    credential = credentials.documents[0]

    assert credential["password_encrypted"] != "Existing!Portal9Password"
    assert (
        await repository.reveal_for_user(
            user_id="user-2", credential_id=credential["_id"]
        )
        is None
    )
    assert (
        await repository.reveal_for_user(
            user_id="user-1", credential_id=credential["_id"]
        )
        == "Existing!Portal9Password"
    )
    assert events.documents[-1]["event_type"] == "credential_revealed"
    assert "password" not in " ".join(events.documents[-1]).lower()


@pytest.mark.asyncio
async def test_worker_secret_is_owned_short_lived_and_repr_safe() -> None:
    repository, _credentials, events = _repository()
    await repository.store_existing(
        user_id="user-1",
        password="WorkerOnly!Portal9Password",
        **_metadata(),
    )

    assert (
        await repository.credential_for_worker(
            user_id="user-2", portal_scope=_metadata()["portal_scope"]
        )
        is None
    )
    credential = await repository.credential_for_worker(
        user_id="user-1", portal_scope=_metadata()["portal_scope"]
    )

    assert credential is not None
    assert credential.password == "WorkerOnly!Portal9Password"
    assert "WorkerOnly!Portal9Password" not in repr(credential)
    assert events.documents[-1]["event_type"] == "credential_used_by_worker"
    assert "password" not in " ".join(events.documents[-1]).lower()


@pytest.mark.asyncio
async def test_worker_secret_rejects_unrecognized_credential_status() -> None:
    repository, credentials, _events = _repository()
    await repository.generate_if_missing(user_id="user-1", **_metadata())
    credentials.documents[0]["status"] = "unknown"

    with pytest.raises(PortalCredentialError, match="status is invalid"):
        await repository.credential_for_worker(
            user_id="user-1",
            portal_scope=_metadata()["portal_scope"],
        )


@pytest.mark.asyncio
async def test_workday_gate_tries_recoverable_login_then_reuses_tenant_credential() -> (
    None
):
    repository, credentials, events = _repository()
    coordinator = NativePortalAccountCoordinator(repository)
    first_url = (
        "https://wd1.myworkdaysite.com/en-US/recruiting/wf/"
        "WellsFargoJobs/job/One_R-1/apply"
    )
    second_url = (
        "https://wd1.myworkdaysite.com/en-US/recruiting/wf/"
        "WellsFargoJobs/job/Two_R-2/apply"
    )

    registration = await coordinator.plan_workday_action(
        user_id="user-1",
        job_or_apply_url=first_url,
        portal_name="Wells Fargo Workday",
        account_email="candidate@example.com",
        page_state=NativeAccountPageState.LOGIN_REQUIRED,
    )
    stored_ciphertext = credentials.documents[0]["password_encrypted"]
    login = await coordinator.plan_workday_action(
        user_id="user-1",
        job_or_apply_url=second_url,
        portal_name="Wells Fargo Workday",
        account_email="candidate@example.com",
        page_state=NativeAccountPageState.LOGIN_REQUIRED,
    )

    assert registration.action is NativeAccountAction.ATTEMPT_LOGIN
    assert login.action is NativeAccountAction.ATTEMPT_LOGIN
    assert registration.credential is None
    assert login.credential is None
    assert registration.account_ref == login.account_ref
    assert registration.account_ref is not None
    assert credentials.documents[0]["password_encrypted"] == stored_ciphertext
    assert [event["event_type"] for event in events.documents] == [
        "credential_generated",
    ]


@pytest.mark.asyncio
async def test_generated_password_recovers_after_vault_collection_loss() -> None:
    encryption_key = Fernet.generate_key().decode("ascii")
    recovery_key = Fernet.generate_key().decode("ascii")
    original_credentials = _Collection()
    original = PortalCredentialRepository(
        PortalVaultCollections(
            credentials=original_credentials,
            events=_Collection(),
        ),
        encryption_key,
        recovery_key,
    )
    await original.generate_if_missing(user_id="user-1", **_metadata())
    original_secret = await original.credential_for_worker(
        user_id="user-1", portal_scope=_metadata()["portal_scope"]
    )

    recovered = PortalCredentialRepository(
        PortalVaultCollections(credentials=_Collection(), events=_Collection()),
        encryption_key,
        recovery_key,
    )
    await recovered.generate_if_missing(user_id="user-1", **_metadata())
    recovered_secret = await recovered.credential_for_worker(
        user_id="user-1", portal_scope=_metadata()["portal_scope"]
    )

    assert original_secret is not None
    assert recovered_secret is not None
    assert original_secret.password == recovered_secret.password


@pytest.mark.asyncio
async def test_explicit_account_not_found_registers_but_bad_password_holds() -> None:
    repository, credentials, _events = _repository()
    coordinator = NativePortalAccountCoordinator(repository)
    await repository.store_existing(
        user_id="user-1",
        password="Existing!Portal9Password",
        **_metadata(),
    )

    register = await coordinator.plan_workday_action(
        user_id="user-1",
        job_or_apply_url=_metadata()["portal_login_url"],
        portal_name="Wells Fargo Workday",
        account_email="candidate@example.com",
        page_state=NativeAccountPageState.LOGIN_ACCOUNT_NOT_FOUND,
    )
    hold = await coordinator.plan_workday_action(
        user_id="user-1",
        job_or_apply_url=_metadata()["portal_login_url"],
        portal_name="Wells Fargo Workday",
        account_email="candidate@example.com",
        page_state=NativeAccountPageState.LOGIN_INVALID_CREDENTIALS,
    )

    assert register.action is NativeAccountAction.REGISTER_ACCOUNT
    assert hold.action is NativeAccountAction.CREATE_HOLD
    assert hold.hold_code == "native_credentials_required"
    assert hold.credential is None
    assert len(credentials.documents) == 1


@pytest.mark.asyncio
async def test_explicit_registration_page_prepares_registration() -> None:
    repository, credentials, _events = _repository()
    coordinator = NativePortalAccountCoordinator(repository)

    plan = await coordinator.plan_workday_action(
        user_id="user-1",
        job_or_apply_url=_metadata()["portal_login_url"],
        portal_name="Wells Fargo Workday",
        account_email="candidate@example.com",
        page_state=NativeAccountPageState.REGISTRATION_REQUIRED,
    )

    assert plan.action is NativeAccountAction.REGISTER_ACCOUNT
    assert plan.credential is not None
    assert len(credentials.documents) == 1


@pytest.mark.asyncio
async def test_active_credential_on_registration_page_opens_login() -> None:
    repository, credentials, _events = _repository()
    coordinator = NativePortalAccountCoordinator(repository)
    await repository.store_existing(
        user_id="user-1",
        password="Existing!Portal9Password",
        **_metadata(),
    )

    plan = await coordinator.plan_workday_action(
        user_id="user-1",
        job_or_apply_url=_metadata()["portal_login_url"],
        portal_name="Wells Fargo Workday",
        account_email="candidate@example.com",
        page_state=NativeAccountPageState.REGISTRATION_REQUIRED,
    )

    assert plan.action is NativeAccountAction.ATTEMPT_LOGIN
    assert plan.credential is None
    assert plan.account_ref is not None
    assert len(credentials.documents) == 1


@pytest.mark.asyncio
async def test_account_exists_and_access_challenges_fail_closed() -> None:
    repository, credentials, _events = _repository()
    coordinator = NativePortalAccountCoordinator(repository)
    common = {
        "user_id": "user-1",
        "job_or_apply_url": _metadata()["portal_login_url"],
        "portal_name": "Wells Fargo Workday",
        "account_email": "candidate@example.com",
    }

    account_exists = await coordinator.plan_workday_action(
        **common,
        page_state=NativeAccountPageState.REGISTRATION_ACCOUNT_EXISTS,
    )
    captcha = await coordinator.plan_workday_action(
        **common,
        page_state=NativeAccountPageState.CAPTCHA,
    )
    otp = await coordinator.plan_workday_action(
        **common,
        page_state=NativeAccountPageState.OTP,
    )

    assert account_exists.hold_code == "native_credentials_required"
    assert captcha.hold_code == "captcha"
    assert otp.hold_code == "otp"
    assert credentials.documents == []


@pytest.mark.asyncio
async def test_registration_confirmation_marks_saved_credential_active() -> None:
    repository, credentials, events = _repository()
    coordinator = NativePortalAccountCoordinator(repository)
    await repository.generate_if_missing(user_id="user-1", **_metadata())

    plan = await coordinator.plan_workday_action(
        user_id="user-1",
        job_or_apply_url=_metadata()["portal_login_url"],
        portal_name="Wells Fargo Workday",
        account_email="candidate@example.com",
        page_state=NativeAccountPageState.REGISTRATION_COMPLETE,
    )

    assert plan.action is NativeAccountAction.CONTINUE_APPLICATION
    assert credentials.documents[0]["status"] == "active"
    assert events.documents[-1]["event_type"] == ("credential_registration_succeeded")


@pytest.mark.asyncio
async def test_account_confirmation_without_saved_credential_holds() -> None:
    repository, _credentials, _events = _repository()
    coordinator = NativePortalAccountCoordinator(repository)

    plan = await coordinator.plan_workday_action(
        user_id="user-1",
        job_or_apply_url=_metadata()["portal_login_url"],
        portal_name="Wells Fargo Workday",
        account_email="candidate@example.com",
        page_state=NativeAccountPageState.LOGIN_COMPLETE,
    )

    assert plan.action is NativeAccountAction.CREATE_HOLD
    assert plan.hold_code == "native_credentials_required"


def test_secret_request_models_hide_and_forbid_password_material() -> None:
    request = StoreExistingPortalCredentialRequest(
        **_metadata(), password="Existing!Portal9Password"
    )

    assert "Existing!Portal9Password" not in repr(request)
    with pytest.raises(ValidationError):
        RevealPortalCredentialRequest(
            current_password="Autopilot!Password9",
            unexpected_secret="must-fail",
        )


def _vault_settings(**overrides) -> Settings:
    values = {
        "jwt_secret": "VaultTests!SecureRandomJwtKey123456789",
        "database_url": "postgresql://test:test@localhost:5432/test",
        "portal_vault_enabled": True,
        "portal_vault_mongodb_url": "mongodb://app:test@portal-vault:27017/autopilot_vault",
        "portal_vault_encryption_key": Fernet.generate_key().decode("ascii"),
        "portal_vault_password_recovery_key": Fernet.generate_key().decode("ascii"),
        "base_url": "http://localhost:8000",
        "debug": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_local_compose_vault_can_use_internal_non_tls_transport() -> None:
    settings = _vault_settings()

    assert settings.portal_vault_enabled is True


def test_external_production_vault_requires_tls() -> None:
    with pytest.raises(ValidationError, match="must enable TLS"):
        _vault_settings(
            base_url="https://autopilot.example.com",
            portal_vault_mongodb_url=(
                "mongodb://app:test@mongo.example.com:27017/autopilot_vault"
            ),
        )


def test_enabled_vault_requires_complete_configuration() -> None:
    with pytest.raises(ValidationError, match="MONGODB_URL is required"):
        _vault_settings(portal_vault_mongodb_url=None)

    with pytest.raises(ValidationError, match="ENCRYPTION_KEY is required"):
        _vault_settings(portal_vault_encryption_key=None)

    with pytest.raises(ValidationError, match="PASSWORD_RECOVERY_KEY is required"):
        _vault_settings(portal_vault_password_recovery_key=None)
