---
description: Professional engineering standards that apply to every code change.
globs: **/*
alwaysApply: true
---

# Base Engineering Standards

## Instruction precedence and scope
- Read repository instructions, build manifests, tests, and nearby code before changing behavior.
- Follow established project architecture and naming unless the task explicitly requires changing them.
- Make the smallest coherent change that completely solves the problem; separate unrelated cleanup and formatting.
- Preserve backward compatibility unless a breaking change is requested, documented, and migrated.
- Do not invent requirements, APIs, dependencies, or infrastructure. State consequential assumptions.

## Design and architecture
- Keep business rules independent from transport, UI, persistence, and framework details.
- Give each module one clear responsibility and expose the smallest useful public interface.
- Pass dependencies explicitly at boundaries; avoid hidden global state and service-locator patterns.
- Prefer composition and simple data flow over deep inheritance, premature abstraction, or clever metaprogramming.
- Remove duplication only when the duplicated concept and its change reasons are genuinely the same.
- Make invariants explicit in types, constructors, validation, and tests rather than relying on comments.
- Keep public API contracts stable and document inputs, outputs, side effects, failure modes, and compatibility constraints.

## Readability and naming
- Use domain names that reveal intent; avoid unexplained abbreviations, vague names, and misleading terminology.
- Keep functions focused and at one abstraction level. Extract helpers when they clarify intent or isolate policy.
- Prefer straightforward control flow with early returns over deeply nested branches.
- Write comments for rationale, constraints, non-obvious tradeoffs, and external quirks—not a narration of the code.
- Delete dead code and stale comments instead of leaving commented-out implementations.

## Error handling and reliability
- For every I/O, network, database, subprocess, parsing, and external-service operation, define a deliberate failure strategy: timeout, expected-error handling, cleanup, contextual propagation, and user-safe reporting.
- Catch only errors the current layer can recover from, translate, compensate for, or enrich. Otherwise allow propagation to an appropriate boundary handler.
- Catch specific error types. Never use an empty handler, silent `pass`, ignored rejection, or success-looking fallback that hides failure.
- Preserve the original cause when translating errors and add actionable context without leaking secrets.
- Keep protected regions narrow so programming defects are not mistaken for expected operational failures.
- Clean up acquired resources on every path with scoped cleanup, context managers, `defer`, `using`, RAII, or `finally` as appropriate.
- Apply timeouts to remote calls. Retry only transient, idempotent operations with bounded attempts, backoff, jitter, and cancellation support.
- Make database/state transitions atomic where partial success would violate invariants; explicitly commit or roll back.
- Fail closed for authorization and other security controls. Do not convert security failures into permissive defaults.

## Inputs, outputs, and security
- Treat request data, files, environment variables, database values, external API responses, and deserialized content as untrusted at their boundary.
- Validate type, syntax, length, range, allowed values, encoding, and cross-field invariants before use.
- Use parameterized database queries and context-appropriate output encoding; never construct executable code or commands from raw input.
- Normalize and constrain filesystem paths to an allowed root before access; defend against traversal and unsafe archive extraction.
- Enforce authentication and authorization server-side for every protected action and object, including ownership checks.
- Keep secrets out of source, fixtures, URLs, logs, exceptions, and generated artifacts. Use the platform's secret store or environment injection.
- Use established cryptographic libraries and secure random generators; never design custom cryptography.
- Minimize privileges, exposed data, dependencies, and attack surface. Use safe defaults and explicit allowlists.

## State, concurrency, and data integrity
- Define ownership and lifecycle for mutable state, connections, files, locks, tasks, and background workers.
- Avoid shared mutable state where possible. When unavoidable, synchronize consistently and document the protected invariant.
- Make cancellation, shutdown, and partial-failure behavior explicit; do not leak workers, locks, handles, or transactions.
- Use idempotency keys or equivalent safeguards for externally retried state-changing operations.
- Use appropriate data types for the domain: exact decimal arithmetic for money, timezone-aware timestamps, bounded integers where overflow matters, and explicit units.
- Keep schema and API migrations backward compatible across rolling deployments when required.

## Observability
- Use the project's logging abstraction and severity conventions; do not use ad-hoc console output in application paths.
- Log once at the layer that owns handling. Include stable event names and useful identifiers, not sensitive payloads.
- Preserve stack/cause information for unexpected failures and expose sanitized errors to users.
- Add metrics or traces for important latency, availability, retry, queue, and business outcomes when the project already supports them.
- Avoid high-cardinality labels and unbounded logging inside hot paths.

## Testing
- Add or update tests in the same change for new behavior, bug fixes, boundary conditions, and failure paths.
- Choose the lowest-cost test that proves the contract: unit for pure policy, integration for boundaries, end-to-end for critical user flows.
- Test observable behavior rather than private implementation details. A test should fail for the defect it claims to prevent.
- Keep tests deterministic and independent: control time, randomness, network, filesystem, process environment, and shared state.
- Assert meaningful outcomes, including cleanup, rollback, authorization denial, and error translation—not merely status codes or lack of exceptions.
- Do not weaken, skip, or delete a valid test to make a change pass without documenting why its contract changed.
- Run focused checks while iterating, then the relevant broader suite, formatter, linter, type checker, and build before handoff.

## Performance and dependencies
- Prefer clear correct code until measurement identifies a meaningful bottleneck.
- Avoid accidental unbounded work, N+1 I/O, repeated parsing, unnecessary copies, and loading large datasets fully into memory.
- Preserve streaming, pagination, batching, caching, and backpressure semantics where the system relies on them.
- Reuse existing dependencies and standard-library capabilities. Add production dependencies only with clear value and after checking maintenance, license, security, and transitive cost.
- Pin or lock dependencies using the repository's existing strategy; do not perform unrelated upgrades.

## Change safety and version control
- Before overwriting or deleting any existing project file, create and verify a timestamped backup under `.codex/backups/`, preserving the relative project path.
- For multi-file operations, back up all targets before the first mutation; on failure restore every original and remove files created by the failed operation.
- Inspect the working tree before editing. Preserve unrelated user changes and never use destructive reset/checkout operations without explicit approval.
- Keep generated files reproducible and modify their source rather than hand-editing outputs when a generator exists.
- Keep commits focused and never commit secrets, local credentials, caches, build products, or personal configuration unless the repository explicitly requires them.

## Completion standard
- The requested behavior is implemented without known regressions or silent failure paths.
- Relevant tests and quality checks pass, or each unrun/failed check is reported with its exact reason.
- Configuration, migrations, public behavior, and operational consequences are documented where applicable.
- The handoff identifies changed files, validation performed, remaining risks, and safe next steps.

## Reference baseline
- Google Engineering Practices: https://google.github.io/eng-practices/review/reviewer/looking-for.html
- Google guidance on small changes: https://google.github.io/eng-practices/review/developer/small-cls.html
- OWASP Secure Coding Practices: https://owasp.org/www-project-secure-coding-practices-quick-reference-guide/
