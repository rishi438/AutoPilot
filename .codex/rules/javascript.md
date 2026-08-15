---
description: Production JavaScript standards for modules, async work, errors, runtime safety, and tests.
globs: **/*.js, **/*.mjs, **/*.cjs, **/*.jsx
alwaysApply: false
---

# JavaScript Engineering Standards

## Runtime and project conventions
- Follow the repository's Node/browser runtime targets, package manager, module system, formatter, ESLint configuration, test runner, and lock file.
- Do not mix ESM and CommonJS without an explicit interoperability boundary.
- Keep browser-only, Node-only, worker-only, and shared code in clearly defined modules.
- Prefer platform capabilities over adding a package for small operations; review maintenance, security, license, and bundle/runtime cost before adding dependencies.
- Do not mix unrelated formatting, package upgrades, or module conversion into a focused change.

## Language usage
- Use `const` by default and `let` only for reassignment; never use `var`.
- Use strict equality except for a documented intentional `value == null` check when repository style permits it.
- Distinguish `??` from `||`; do not replace valid `0`, `false`, or empty-string values accidentally.
- Use braces for control flow and explicit conversions when coercion would obscure the contract.
- Never use `eval`, `new Function`, `with`, primitive wrapper constructors, or mutation of built-in prototypes.
- Avoid automatic semicolon insertion hazards and follow the configured formatter.
- Use optional chaining only when absence is valid; do not use it to hide broken invariants.

## Modules and API boundaries
- Keep modules cohesive and exports minimal. Follow the repository's named/default export convention.
- Avoid dependency cycles, mutable exported bindings, hidden globals, and undocumented side-effect imports.
- Put I/O and framework integration at boundaries; keep business rules in deterministic, side-effect-light functions.
- Validate configuration once at startup and pass trusted configuration explicitly.
- Do not expose internal mutable objects; return immutable views, copies, or controlled operations where mutation would violate invariants.
- Remember object/array spread is shallow and does not clone nested data or class instances.
- Use JSDoc on public/shared JavaScript APIs when it materially improves contracts, tooling, or generated documentation.

## Functions and state
- Keep functions focused and prefer pure transformations for policy and calculations.
- Avoid long positional argument lists and multiple boolean flags; use a validated options object.
- Avoid surprising `this` binding. Use methods for object behavior and arrow functions for lexical callback capture.
- Do not mutate arguments unless the API explicitly owns them and the behavior is documented.
- Make state transitions explicit and avoid module-level mutable singletons that complicate tests and concurrency.
- Remove event listeners, observers, subscriptions, timers, and resources when their owner is disposed.

## Promises and asynchronous work
- Await or return every promise. A deliberately detached promise must have explicit ownership and rejection handling.
- Return promises from `.then()` callbacks so downstream completion and rejection remain linked.
- Use `Promise.all` only for independent work with acceptable fail-fast semantics; use sequential execution, bounded concurrency, or `allSettled` when behavior differs.
- Handle a rejection once at the layer that can recover, translate, or report it. Do not add catches that merely hide failure.
- Use `AbortSignal` and explicit deadlines for cancellable external work and propagate cancellation.
- In Node, handle required EventEmitter `error` events and respect stream backpressure.
- Avoid synchronous filesystem, crypto, compression, subprocess, or CPU-heavy APIs on request/event-loop paths.
- Bound queues, retries, polling, parallel requests, and worker/task creation.

## Errors
- Throw `Error` instances or defined subclasses, never strings or arbitrary objects.
- Give recoverable/operational errors stable types or codes; do not branch on human-readable messages.
- Preserve underlying causes with `new Error(message, { cause })` when translating failures.
- Catch only errors the layer can recover from or meaningfully translate; rethrow unexpected failures.
- Keep protected regions narrow. Do not let a catch intended for one operation swallow later programming defects.
- Empty catches require a precise comment explaining why ignoring the failure is correct.
- Reserve `finally` for cleanup and never return from it because it can suppress errors or earlier returns.
- Do not expose stack traces, internal paths, SQL, tokens, or raw upstream messages to users.

## Runtime boundaries and security
- Treat parsed JSON, storage, DOM attributes, environment variables, database records, message payloads, and third-party responses as untrusted until validated.
- Validate type, shape, length, range, allowed values, and cross-field invariants at the boundary.
- Use parameterized queries, direct process argument arrays, and context-appropriate output encoding.
- For DOM content, prefer `textContent` and safe property APIs; use HTML injection only with trusted or rigorously sanitized content.
- Never construct executable code, SQL, shell commands, file paths, URLs, or HTML by directly concatenating untrusted input.
- Protect against prototype pollution by allowlisting accepted object keys and using safe merge practices.
- Keep secrets out of source, client bundles, URLs, errors, analytics, and logs.
- Use Web Crypto or Node `crypto` secure randomness for tokens; never use `Math.random` for security decisions.

## Logging and observability
- Use the project's structured logger for production code rather than ad-hoc `console.log`.
- Include operation context and stable identifiers, not full request/response objects.
- Log at the boundary that owns reporting; do not log and rethrow the same failure at each layer.
- Redact credentials, authorization headers, cookies, tokens, and sensitive personal data.
- Avoid unbounded or per-item logging in high-volume paths.

## Testing
- Assert observable behavior rather than private call order or implementation detail.
- Await asynchronous tests and subtests; ensure no promise, timer, listener, or worker survives test completion.
- Isolate clock, randomness, environment, filesystem, network, browser storage, and global/module state.
- Restore mocks, fake timers, listeners, DOM changes, and globals after each test.
- Use data-driven cases for boundaries and malformed inputs; add a regression test for repaired defects.
- Test rejections, cancellation, timeouts, partial failure, stream errors, and concurrency limits where relevant.
- Keep unit, integration, component, browser, and end-to-end suites distinct.

## Quality gate
- Run the formatter, ESLint, tests, build/bundle checks, and relevant browser or Node integration tests.
- Require a reason for ESLint suppressions and enable unused-disable reporting when supported.
- Treat unhandled promise rejections, resource leaks, and flaky tests as defects.

## Reference baseline
- Google JavaScript Style Guide: https://google.github.io/styleguide/jsguide.html
- MDN promises: https://developer.mozilla.org/en-US/docs/Web/JavaScript/Guide/Using_promises
- MDN try/catch: https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Statements/try...catch
- Node errors: https://nodejs.org/api/errors.html
- Node test runner: https://nodejs.org/api/test.html
