# Workday Unit 1 operations and live-checkpoint guide

Unit 1 starts from a server-issued, user-owned `workday_application` lease and
stops at a stable, authenticated Basic Information page, called My Information
by some Workday tenants. It does not navigate beyond that page, accept legal
terms, or submit an application. The legacy account-gate lease remains a
separate explicitly selected operation.

## Configuration and fixed limits

- `workday_transition_history_limit`: default `10`; allowed range `1`-`100`.
- `workday_account_lock_cooldown_hours`: default `6`; allowed range `1`-`168`.
- Authentication submits per attempt: fixed at `1` and not configurable.

Values outside the configurable ranges are rejected during settings validation.

## State and action boundary

The observer emits only these versioned structural states:

```text
JOB_PAGE -> APPLY_CHOICES -> ACCOUNT_PAGE/LOGIN_FORM
         -> AUTH_FORM_STRUCTURALLY_READY -> AUTH_OUTCOME_PENDING
         -> AUTHENTICATED_APPLICATION_READY  [STOP]
```

Every transition rechecks the approved HTTPS origin, canonical Workday tenant,
fresh structural signature, and one unique allowlisted control before acting.
The final checkpoint also requires the leased job/application, private account
binding, and the exact Basic Information or My Information heading plus a
hydrated field or one unique enabled Next/Save and Continue control to match in
two consecutive fresh observations. The checkpoint observes that control but
cannot click it. Information-step **Next/Save and Continue** and final
application **Submit** remain forbidden in every Unit 1 path.

Known CURRENT and compatible VERIFIED history transitions replay directly with
zero local-LLM calls. Only confirmed pre-auth structural drift reaches the
bounded one-shot repair path. Native browser conditions are typed before
structural replay: job-unavailable, CAPTCHA/OTP, transient, wrong tenant, and
account-lock outcomes take their deterministic routes without catalog or LLM
work.

An existing authenticated session is accepted only after private account
binding and final checkpoint proof. A `LOGIN_FORM` is hydrated through bounded
read-only observations; the private auth broker is called only after
`AUTH_FORM_STRUCTURALLY_READY`. Broker success is provisional: the gate remains
`AUTH_OUTCOME_PENDING` until the two stable final checkpoint observations pass.
Observe-only recovery performs only fresh read-only post-submit observation and
guarded review/cooldown handling; it cannot navigate, mutate the browser, read
the vault, call the LLM, or create a retry loop.

## Shared transition catalog

Lookup is event-driven: the compatible `CURRENT` VERIFIED version runs first,
followed by compatible VERIFIED parents, newest to oldest, up to the configured
history limit. QUARANTINED, RETIRED, incompatible, or older out-of-limit rows do
not execute. There is no success count, popularity score, or ranking.

A previous version becomes CURRENT only after it safely reaches its expected
state and wins an atomic compare-and-set. Pre-auth structural repair may append
one immutable VERIFIED child and set it CURRENT only after independent action
validation and expected-state verification. Failed verification changes no
catalog history.

Shared rows contain structural compatibility keys and the allowlisted recipe
fields `schema_version`, `semantic_role`, `intent_key`, `scope_key`, and
`require_unique`. They contain no user ID, application ID, account reference,
email, credential, secret, answer, cookie, token, raw page content, browser
state, cooldown, attempt, application link, or notification.

## Private gate, cooldown, and recovery

Gate state is private to the user, opaque account reference, and canonical
tenant/site. Cooldowns, attempts, application links, notices, and account data
never enter the shared catalog.

- An expired pre-submit lease with no authentication submit is abandoned and
  may be safely reacquired. A pre-submit startup failure releases both leases.
- A submitted attempt persists `AUTH_OUTCOME_PENDING`; only the same application
  may receive read-only observation. Expiry, crash, timeout, or network
  ambiguity cannot authorize another submit.
- The configured lock cooldown is passed to every production gate store. A
  trusted future portal unlock deadline is retained only as bounded UTC data,
  and the enforced wait is the maximum of the existing cooldown, user minimum,
  configured default, and trusted portal deadline.
- Unknown or ambiguous post-submit state becomes `REVIEW_REQUIRED` and defers
  all automatic work until explicitly resolved.
- CAPTCHA or OTP creates a user hold; rejected credentials create a credential
  hold; wrong origin/tenant creates a security hold; unknown states fail closed.
- A trusted temporary-account-lock signal starts or lengthens the cooldown and
  creates one private notice per gate generation. It never shortens an existing
  wait or immediately requeues work.
- **Keep** preserves the wait. **Extend** accepts only a future server-UTC time,
  only lengthens the wait, and revokes pre-submit probes. **Delete** requires
  confirmation and soft-deletes only the selected owned application while
  preserving the gate, cooldown, account, credential, profile, resume, other
  applications, shared catalog, and ambiguous audit state.

Operator-visible outcomes are defer with next eligible time, bounded backoff,
security/user/credential/safe hold, review required, skipped unavailable job,
cooldown notice, or Unit 1 complete. Public events and notices contain only safe
identifiers, status, messages, and server-UTC times.

## Migration recovery boundary

Revisions `20260827_040`, `20260828_041`, and `20260828_042` add the shared
catalog, private gate/attempt data, and cooldown notices. Before production
upgrade, back up PostgreSQL and verify the restore procedure. Prove upgrade to
head and downgrade to the prior revision only in a disposable database first.
If an upgrade fails, stop workers and roll the disposable or production database
back through Alembic only after confirming no newer code is writing the new
schema. A schema downgrade is not a substitute for restoring lost data.

## Secret-free user-run live checkpoint

This checklist requires separate authorization. Use the existing
`scripts/run_workday_account_gate.py` entry point directly, or its foreground
`scripts/run_workday_retry.ps1` launcher; do not create another runner.
Never paste credentials, email, cookies, tokens, answers, raw page content, or
unredacted logs into the acceptance record.

The PowerShell launcher calls the scoped `retry-latest-review` worker route and
then invokes the existing Python runner. On Windows it stores only a DPAPI
encrypted worker token under ignored `.tmp/state/`; the token is bound to the
current Windows user and machine, is never put on the command line, and is
discarded and requested again when the API reports it invalid or expired.
With the current application recorded in `.tmp/learnings.txt`, run:

```powershell
.\scripts\run_workday_retry.ps1
```

Use `-ResetToken` to discard the encrypted cache and prompt for a replacement.

1. Confirm the application, one-day worker device, approved HTTPS Workday job
   URL, expected tenant/site, and private vault account binding are the intended
   user-owned records.
2. Start the already-configured services using the project's normal visible
   operator workflow. Do not run the worker in a hidden/background terminal.
3. Run exactly one targeted account-gate action for that application.
4. At each visible transition, confirm the expected job, origin/tenant, and
   account dialog before allowing the next action.
5. Stop immediately on an account lock, security hold, CAPTCHA/OTP, invalid
   credentials, unexpected page, or post-submit ambiguity. Do not retry.
6. Success is only a stable authenticated Basic Information or tenant-equivalent
   My Information page with a visible hydrated field or one unique enabled
   Next/Save and Continue control. Record safe state names and timestamps only.
7. Stop there. Do not click information-step **Next/Save and Continue** and do
   not click final application **Submit**.

Offline mocks, fixtures, in-memory databases, and focused tests are not evidence
that PostgreSQL migrations or the live Workday flow were exercised.
