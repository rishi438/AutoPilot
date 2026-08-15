---
description: Production Python standards for structure, typing, failures, async work, security, and tests.
globs: **/*.py, **/*.pyi
alwaysApply: false
---

# Python Engineering Standards

## Project conventions and tooling
- Treat `pyproject.toml`, lock files, supported Python version, formatter, linter, type checker, and test configuration as authoritative.
- Preserve the repository's package layout and import style; do not introduce a parallel dependency or configuration system.
- Use an isolated environment and the project's package manager. Never install project dependencies into the system interpreter.
- Prefer the standard library when it adequately solves the problem; justify new runtime dependencies.
- Do not reformat, rename, or modernize unrelated code in a focused behavior change.

## Structure and dependency boundaries
- Keep modules cohesive and separate domain policy from HTTP, CLI, database, filesystem, queue, and framework adapters.
- Pass external services through explicit constructor/function dependencies rather than importing mutable global clients throughout the codebase.
- Avoid circular imports, wildcard imports, runtime `sys.path` manipulation, and imports whose hidden side effects initialize application state.
- Keep public exports intentional. Prefix internal helpers with `_` and do not expose internals solely to make tests convenient.
- Put process startup, logging configuration, signal handling, and dependency wiring in explicit entry points.
- Use `if __name__ == "__main__":` for executable module behavior; importing a module must not launch work.

## Types and domain models
- Type public functions, methods, attributes, callbacks, and non-obvious return values. Let obvious local types be inferred.
- Use `X | None` only when absence is a real domain state and handle it explicitly.
- Prefer `collections.abc` abstractions such as `Iterable`, `Sequence`, and `Mapping` for inputs that do not require mutation.
- Use `Protocol` for behavioral contracts, `TypedDict` for typed mapping-shaped boundary data, and dataclasses or validated models for structured domain data.
- Prefer enums or literal unions for closed values rather than scattered strings.
- Treat `Any`, `cast`, `# type: ignore`, and unchecked model construction as exceptions: localize them at integration boundaries and explain why they are safe.
- Type hints support static analysis; they do not validate JSON, environment variables, database rows, files, or API responses at runtime.
- Validate untrusted input before constructing trusted domain objects.

## Functions, state, and APIs
- Keep functions focused, make side effects visible, and return values rather than mutating distant state.
- Never use mutable default arguments; use `None`, a sentinel, or a factory and create the mutable value inside.
- Prefer keyword-only parameters when multiple arguments share a type or call-site meaning is unclear.
- Replace clusters of boolean flags with an enum, configuration object, or separate operations.
- Prefer immutable value objects where practical and copy retained mutable input when callers may mutate it.
- Do not use mutable class attributes for instance state.
- Keep decorators transparent: preserve metadata with `functools.wraps` and retain the wrapped function's error/cancellation semantics.
- Document public contracts, side effects, units, thread/async safety, raised domain exceptions, and non-obvious complexity.

## Exceptions and cleanup
- Catch the narrowest exception the current layer can recover from, compensate for, translate, or enrich.
- Keep `try` blocks narrow so unrelated defects are not intercepted as expected failures.
- Do not use bare `except`, `except Exception`, or exception swallowing for ordinary control flow.
- A broad catch is acceptable only at a process/task boundary; report it with stack context and convert it to a defined failure result or re-raise.
- Translate infrastructure failures into domain failures only at a meaningful boundary and preserve the cause with `raise DomainError(...) from err`.
- Use `raise` rather than `raise err` to preserve the active traceback.
- Never silently `pass` in an exception handler. A deliberately ignored failure requires a precise local reason.
- Use context managers for files, locks, cursors, connections, transactions, temporary resources, and other acquired handles.
- Do not `return`, `break`, or `continue` from `finally`; it can suppress active failures.
- Include the failed operation and safe identifiers in errors; never leak secrets or sensitive payloads.

## Async and concurrency
- Use async code for concurrent I/O, not merely because a framework supports it.
- Never run blocking network, filesystem, subprocess, sleep, or CPU-heavy work directly on the event-loop thread; use an async API or appropriate executor.
- Every created task must have an owner that awaits it, cancels it, and observes its failure.
- Do not create unbounded tasks; use semaphores, workers, bounded queues, and backpressure.
- Set explicit deadlines at external I/O boundaries and propagate cancellation. Do not accidentally swallow `CancelledError` or equivalent cancellation signals.
- Use `TaskGroup` or the project's structured-concurrency mechanism when sibling task lifetime and failure should be linked.
- Protect shared mutable state with the appropriate lock; prefer immutable data, queues, or message passing.
- Define shutdown behavior for workers, queues, executors, connections, and background tasks.

## Logging and observability
- Use `logger = logging.getLogger(__name__)`; do not use `print` for application logging.
- Configure handlers, levels, destinations, and formatting in the application entry point, not library modules.
- Use parameterized logging (`logger.info("item=%s", item_id)`) so formatting is deferred.
- Use the appropriate severity and stable event/operation identifiers.
- Use `logger.exception(...)` only while handling an exception; avoid manually duplicating traceback text.
- Log an error at the boundary that owns reporting; do not log and re-raise it at every layer.
- Never log passwords, API tokens, cookies, authorization headers, private keys, or complete sensitive payloads.

## Files, serialization, time, and money
- Use `pathlib.Path`, explicit encodings, and context-managed file access.
- Resolve and verify user-controlled paths remain within an allowed root before reading, writing, extracting, moving, or deleting.
- Write durable configuration/state through a temporary file plus atomic replacement where partial writes would corrupt it.
- Use timezone-aware datetimes and make UTC storage versus display conversion explicit.
- Use `Decimal` or integer minor units for money when binary floating-point rounding is unacceptable; define rounding policy.
- Never deserialize untrusted pickle, marshal, or unsafe YAML data.
- Bound input size and depth before parsing JSON, XML, archives, images, or compressed content.

## Database and network boundaries
- Use parameterized queries and explicit transaction boundaries; roll back partial business operations.
- Configure connection, read, write, and total timeouts for outbound calls.
- Reuse managed clients/pools according to library lifecycle; do not create a new connection pool per request.
- Retry only transient, idempotent operations with bounded attempts, backoff, jitter, and cancellation.
- Validate status, content type, size, and schema of external responses before trusting data.

## Testing
- Follow the project's pytest or unittest convention and name tests after observable behavior.
- Cover success, failure, boundary, empty, null-like, malformed-input, cleanup, rollback, and authorization cases.
- Keep tests deterministic by controlling clock, randomness, filesystem, network, subprocesses, environment, locale, and shared state.
- Prefer fixtures for resource lifecycle, `tmp_path` for filesystem work, and parametrization for genuinely data-driven cases.
- Mock at architectural boundaries, not every internal call; over-mocking hides integration defects and couples tests to implementation.
- Add a regression test for every repaired defect when practical.
- Separate unit, integration, contract, and end-to-end tests and mark tests that require external infrastructure.
- Test async cancellation, task cleanup, timeout, and partial-failure behavior where relevant.

## Completion checks
- Run the configured formatter (for example Black/Ruff), linter, type checker, focused tests, then the relevant broader suite.
- Resolve warnings rather than globally suppressing them. Keep suppressions narrow and documented.
- Update docstrings and public documentation when behavior, contracts, configuration, or failure modes change.

## Reference baseline
- PEP 8: https://peps.python.org/pep-0008/
- Python exceptions: https://docs.python.org/3/tutorial/errors.html
- Python typing best practices: https://typing.python.org/en/latest/reference/best_practices.html
- Python logging: https://docs.python.org/3/howto/logging.html
- asyncio development guidance: https://docs.python.org/3/library/asyncio-dev.html
- pytest good practices: https://docs.pytest.org/en/stable/explanation/goodpractices.html
