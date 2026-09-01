# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.1.0] — 2026-09-01

### Added

- **Existing-Account Authentication Flow**: `existing_account_credentials_required` and `account_discovery_retry_exhausted` hold codes for Workday portals with pre-existing account login walls.
- **Automation Progress Tracking**: `derive_automation_progress()` projects sub-stage progress (unit1 in-progress / completed / review / retry) from durable events without mutating `ApplicationStatus`.
- **Stage 2 Eligibility Check**: `is_stage2_eligible()` determines when an application is ready for the next automation stage.
- **Automation Test API**: New `/automation-test` endpoint with full test coverage for dry-run and diagnostic workflows.
- **Unit1 Completion Guard**: Lease endpoint prevents re-leasing applications that already completed Stage 1 via durable `workday_unit1_completed` event check.

### Changed

- **Auth Broker**: Expanded with account-discovery retry logic and cooldown-elapsed reclaim paths in worker retry-review-hold recovery.
- **Playwright Worker**: Hardened `workday_playwright_worker` and `unit1_orchestrator` for new auth-gate transitions.
- **Portal Credentials**: Extended for existing-account credential resolution.
- **Retry Logic**: Improved worker retry-review-hold recovery to handle `gate_retryable` states including `cooling_down` with elapsed cooldowns, `auth_outcome_pending`, and expired attempt reclaim.
- **Dashboard UI**: Updated to surface automation progress stages and sub-stage status.

### Files Changed

- **API**: `automation.py`, `applications.py`, `automation_test.py` (new)
- **Services**: `application_automation.py`, `workday_auth_broker.py`, `workday_playwright_worker.py`, `workday_unit1_orchestrator.py`, `workday_unit1_runtime.py`, `workday_transition_repair.py`, `workday_transition_engine.py`, `workday_transition_catalog.py`, `workday_transition_contracts.py`, `workday_state_observer.py`, `workday_failure_router.py`, `workday_account_gate_store.py`, `workday_worker_api.py`, `portal_credentials.py`, `local_workday_runner.py`
- **Tests**: `test_application_automation.py`, `test_application_progress.py` (new), `test_automation_test_api.py` (new), `test_workday_playwright_worker.py`, `test_workday_gate_lease_wiring.py`, `test_workday_state_observer.py`, `test_workday_task04_integration.py`, `test_workday_task07_integration.py`, `test_workday_unit1_orchestrator.py`
- **UI**: `dashboard/index.html`, `static/js/dashboard-home.js`, `static/js/types.js`
- **Scripts**: `run_workday_account_gate.py`, `run_workday_retry.ps1`
- **Config**: `.dockerignore`
