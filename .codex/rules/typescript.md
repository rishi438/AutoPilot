---
description: Production TypeScript standards for strict types, runtime boundaries, APIs, async work, and tests.
globs: **/*.ts, **/*.tsx, **/*.mts, **/*.cts
alwaysApply: false
---

# TypeScript Engineering Standards

## Runtime and compiler configuration
- Follow the repository's runtime, module system, package manager, formatter, typed ESLint configuration, test runner, and lock file.
- Enable `strict` for new projects. Prefer `noUncheckedIndexedAccess`, `exactOptionalPropertyTypes`, `noImplicitOverride`, and switch fall-through checks when compatible.
- Do not weaken compiler or lint settings to make one implementation pass.
- Preserve the supported TypeScript/runtime versions and public declaration compatibility.
- Treat type-checking failures as defects, not warnings to suppress.

## Type boundaries
- Give exported functions, public methods, callbacks, and shared data contracts explicit types; infer obvious local types.
- Treat JSON, caught values, storage, environment/config values, database rows, and third-party responses as `unknown` until validated or narrowed.
- Prefer `unknown` over `any`. Any unavoidable `any` must be localized at an integration boundary and justified.
- Static types do not validate runtime input; use the project's schema validator or explicit guards at trust boundaries.
- Keep framework/persistence models separate from public DTOs and domain types when their guarantees or lifecycle differ.
- Use type-only imports/exports where appropriate and avoid unintentionally exposing implementation dependency types.

## Domain modeling
- Use discriminated unions for state machines and mutually exclusive variants rather than unrelated optional fields.
- Make switches over closed unions exhaustive with a `never` check.
- Model absence explicitly; do not make fields optional merely to avoid constructing a valid object.
- Use `readonly` for values consumers must not mutate and return read-only collection views where appropriate.
- Prefer literal unions for closed values unless runtime enum behavior or external interoperability requires an enum.
- Avoid `const enum` in published libraries because consumers and toolchains may disagree about inlining.
- Use distinct/branded IDs or values when mixing structurally identical primitives would create meaningful business risk.
- Prefer interfaces for extensible object contracts and type aliases for unions, tuples, primitives, and compositions, while honoring repository convention.

## Assertions and suppressions
- Type assertions and non-null assertions perform no runtime check; replace them with narrowing, validation, or stronger modeling where possible.
- Put a necessary assertion beside the invariant that proves it safe and explain non-obvious cases.
- Never use double assertions (`value as unknown as T`) to force incompatible models together.
- Do not use `@ts-ignore`.
- Use `@ts-expect-error` only for a known and tested compiler/third-party limitation, include a reason, and remove it when obsolete.
- Prefer `satisfies` or a typed variable/return value when checking object literals without unwanted widening.
- Do not silence a type error by making a broad area optional, nullable, or `any`.

## APIs, functions, and state
- Keep modules cohesive, exports minimal, and side effects behind explicit adapters.
- Use options objects for long or ambiguous argument lists and validate them at entry.
- Keep public generics constrained and comprehensible; avoid conditional-type puzzles when a simpler contract works.
- Avoid overloads whose implementation cannot distinguish cases safely.
- Use primitive `string`, `number`, and `boolean`, never wrapper object types.
- Avoid mutable exported singletons and hidden global state; pass dependencies explicitly.
- Do not expose mutable internal objects or collections when mutation can violate invariants.

## Async work and errors
- Await or return every promise; deliberately detached work requires explicit ownership, cancellation, and error reporting.
- Use bounded concurrency and appropriate `Promise.all`, sequential, or `allSettled` semantics.
- Propagate `AbortSignal` and explicit timeouts through cancellable I/O.
- Use `unknown` for caught values and narrow them with `instanceof`, predicates, or validator functions.
- Do not assume every thrown value is an `Error`; normalize foreign thrown values at an application boundary.
- Use typed domain errors or result unions when callers are expected to branch on recoverable outcomes.
- Preserve `cause` when translating exceptions and do not branch on mutable message text.
- Remove listeners, timers, subscriptions, streams, and tasks when their owner is disposed.

## Security and runtime correctness
- Validate all externally sourced data before treating it as a typed domain value.
- Use parameterized database operations, safe subprocess argument APIs, and context-appropriate output encoding.
- Never construct HTML, SQL, shell commands, executable code, or unrestricted paths from raw input.
- Protect against prototype pollution by allowlisting keys and avoiding unsafe recursive merges.
- Keep secrets out of source, browser bundles, URLs, telemetry, errors, and logs.
- Use secure randomness APIs for tokens and define exact decimal/timezone behavior for money and time.

## Testing
- Test runtime validation separately from compile-time type claims.
- Add type-level tests for public generic APIs when inference and rejected states form part of the contract.
- Assert observable behavior and stable error properties, not private implementation order.
- Control time, randomness, network, filesystem, process environment, storage, globals, and async cleanup.
- Test malformed inputs, missing/extra fields, union variants, rejected promises, cancellation, timeouts, and partial failure.
- Add regression coverage for repaired defects and separate unit, integration, component, and end-to-end tests.

## Quality gate
- Run formatter, type-aware ESLint, `tsc --noEmit` (or project equivalent), unit tests, declaration/build output checks, and relevant integration tests.
- Keep lint/type suppressions narrow and justified; enable unused suppression reporting.
- Verify supported module formats and type declarations for published packages.

## Reference baseline
- TypeScript strict mode: https://www.typescriptlang.org/tsconfig/strict.html
- TypeScript narrowing: https://www.typescriptlang.org/docs/handbook/2/narrowing.html
- TypeScript functions: https://www.typescriptlang.org/docs/handbook/2/functions.html
- Google TypeScript Style Guide: https://google.github.io/styleguide/tsguide.html
- typescript-eslint typed linting: https://typescript-eslint.io/getting-started/typed-linting/
