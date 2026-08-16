# Justfile — cross-platform alternative to Makefile (Options A and C).
# Works on macOS, Linux, and Windows (PowerShell / cmd) without WSL2.
#
# No shebang recipes: Windows `just` otherwise requires `cygpath` (Git for Windows).
#
# Install just:
#   macOS/Linux:  brew install just
#   Windows:      winget install Casey.Just
#
# Option A (Docker):  just start   (migrations run inside the app container before uvicorn)
# Option C (Manual):  just setup && just migrate && just dev
#
# One-time sibling copy for safe Just/Docker tests (macOS/Linux; needs python3):
#   just sandbox-for-testing

# `just` defaults to sh on Windows. This setting is Windows-only, so macOS and
# Linux retain their normal shell behavior. Keep it for Just 1.51 compatibility.
set windows-shell := ["powershell.exe", "-NoLogo", "-NoProfile", "-Command"]

# Cross-platform paths into the virtual environment
python_cmd := if os() == "windows" { "python" }              else { "python3" }
python     := if os() == "windows" { "venv\\Scripts\\python" } else { "venv/bin/python" }
pip        := if os() == "windows" { "venv\\Scripts\\pip" }    else { "venv/bin/pip" }

# ---------------------------------------------------------------------------
# Generate .env with random secrets if it doesn't exist.
# Windows: PowerShell only (Docker Option A needs no system Python).
# Unix:    scripts/create_dotenv_if_missing.py
# ---------------------------------------------------------------------------
[unix]
[private]
_create-env:
    {{python_cmd}} scripts/create_dotenv_if_missing.py

[windows]
[private]
_create-env:
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts/create_dotenv_if_missing.ps1

# Sibling clone at ../autopilot-just-sandbox + SANDBOX_README.md (does not touch this repo's .env or DB).
[unix]
sandbox-for-testing:
    {{python_cmd}} scripts/make_just_test_sandbox.py

[windows]
sandbox-for-testing:
    {{python_cmd}} scripts/make_just_test_sandbox.py

# ---------------------------------------------------------------------------
# Option A — Docker/podman
# ---------------------------------------------------------------------------

# Checks that Docker/podman is installed and the daemon is running.
# Does NOT install or start Docker/podman — the user is responsible for that.
[windows]
[private]
_ensure-podman:
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts/container_runtime.ps1 ensure podman

[unix]
[private]
_ensure-podman:
    docker info

[windows]
[private]
_compose-podman +ARGS:
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts/container_runtime.ps1 compose podman {{ARGS}}

[unix]
[private]
_compose-podman +ARGS:
    docker compose {{ARGS}}

[windows]
[private]
_ensure-docker:
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts/container_runtime.ps1 ensure docker

[unix]
[private]
_ensure-docker:
    docker info

[windows]
[private]
_compose-docker +ARGS:
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts/container_runtime.ps1 compose docker {{ARGS}}

[unix]
[private]
_compose-docker +ARGS:
    docker compose {{ARGS}}

# Generate .env + start existing images (foreground)
start: _ensure-podman _create-env
    just _compose-podman pull --ignore-buildable
    just _compose-podman up

# Generate .env + rebuild images, then start all services (foreground)
rebuild: _ensure-podman _create-env
    just _compose-podman pull --ignore-buildable
    just _compose-podman up --build

# Generate .env + start existing images (background)
start-d: _ensure-docker _create-env
    just _compose-docker pull --ignore-buildable
    just _compose-docker up -d

# Generate .env + rebuild images, then start all services (background)
rebuild-d: _ensure-docker _create-env
    just _compose-docker pull --ignore-buildable
    just _compose-docker up --build -d

# Stop all services, keep data
docker-down:
    just _compose-docker down

# Stop all services and wipe all data
docker-reset:
    just _compose-docker down -v

# Tail the app logs
docker-logs:
    just _compose-docker logs -f app

# Build the Docker image
docker-build:
    just _compose-docker build

# Podman lifecycle commands for services started with `just start`.
podman-down:
    just _compose-podman down

podman-reset:
    just _compose-podman down -v

podman-logs:
    just _compose-podman logs -f app

podman-build:
    just _compose-podman build

# Start or stop the independent local SonarQube security stack.
security-up: _ensure-podman
    just _compose-podman --project-name autopilot-security -f docker-compose.security.yml --profile security up -d

security-down: _ensure-podman
    just _compose-podman --project-name autopilot-security -f docker-compose.security.yml --profile security down

# Passive DAST scan of the locally running application. The Compose network
# avoids Podman Desktop's unreliable host.containers.internal gateway. It never
# runs active attack rules; start the app first with `just start`.
zap-baseline: _ensure-podman
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_zap_baseline.ps1

# Static security scan of source and configuration. A finding exits non-zero.
semgrep: _ensure-podman
    podman run --rm --volume "${PWD}:/src:ro" --workdir /src semgrep/semgrep semgrep scan --config auto --error --exclude .pytest_cache --exclude .sonar --exclude .tmp .

# ---------------------------------------------------------------------------
# Option C — Manual (you run PostgreSQL and Redis yourself)
# ---------------------------------------------------------------------------

# A venv is reusable. Avoid recreating it when its Windows interpreter is in use.
[windows]
[private]
_create-venv:
    if (-not (Test-Path 'venv\\Scripts\\python.exe')) { python -m venv venv } else { Write-Output '  venv already exists - skipping.' }

[unix]
[private]
_create-venv:
    test -x venv/bin/python || {{python_cmd}} -m venv venv

# Full setup: venv + Python/Node deps + frontend build + .env
setup: _create-env _npm-install build-frontend _create-venv
    {{python}} -m pip install --upgrade pip
    {{python}} -m pip install -r requirements.txt
    @echo ""
    @echo " Setup complete!"
    @echo " Edit .env with your DATABASE_URL and REDIS_URL, then:"
    @echo "   just migrate   - run database migrations"
    @echo "   just dev       - start the app at http://localhost:8000"
    @echo ""

# Install Node dependencies (runs inside ui/ — avoids && which breaks PowerShell 5.x)
[private]
[working-directory: 'ui']
_npm-install:
    npm install

# Build frontend assets (esbuild minify + content-hash)
[working-directory: 'ui']
build-frontend:
    npm run build

# Run Alembic database migrations against the local DB.
# Uses scripts/run_alembic.py so ./alembic/ does not shadow the installed package (see Makefile).
migrate:
    {{python}} scripts/run_alembic.py upgrade

# Show current Alembic revision
migrate-status:
    {{python}} scripts/run_alembic.py current

# Start the FastAPI dev server with auto-reload (services must already be running)
dev: build-frontend
    {{python}} -m uvicorn main:app --reload --host 0.0.0.0 --port 8000

# Run the test suite
test:
    {{python}} -m pytest tests/ -v

# Run the test suite with the project-wide coverage threshold.
test-cov:
    {{python}} -m pytest tests/ -v --cov=. --cov-report=term-missing --cov-report=html:coverage_html --cov-report=xml:coverage.xml --cov-config=.coveragerc --cov-fail-under=45

# Run the linter
lint:
    {{python}} -m ruff check .
