# Workday Unit 2 Operations & Execution Guide

## 1. Overview & Scope

Workday Unit 2 automates the **My Information** section of a Workday job application. It operates under a strict, verifiable contract:
- Pre-condition: Durable Stage 1 (`workday_unit1_completed`) completion receipt.
- Post-condition: Durable Stage 2 (`workday_unit2_completed`) receipt with detail `my_information_saved_next_section_ready`.
- Core Invariant: **At most one single Save click is claimed and executed per attempt.**

Unit 2 does **not** fill experience/skills, answer application questionnaires, or click Final Submit.

---

## 2. Stage 2 Lifecycle & Visible States

The API and Dashboard UI report four unambiguous Stage 2 states:

| API Progress Truth | Dashboard Badge | Meaning |
|---|---|---|
| Unit 1 complete, no Unit 2 start | `Stage 1 complete` | Stage 1 authenticated; application is queued and ready for Stage 2. |
| Unit 2 started / claimed / retry-ready | `Stage 2 in progress` | Worker is actively resuming session, mapping approved fields, or claiming Save. |
| Open Unit 2 hold | `Stage 2 review required` | Worker halted safely before irreversible actions (e.g. unknown question or captcha). |
| Durable Unit 2 completion | `Stage 2 complete` | My Information form saved; next section hydrated; top-level status remains `applying`. |

Top-level application status (such as `applying`, `blocked`, or `failed`) remains visible and tracked independently of stage progress receipts.

---

## 3. Execution Phase Guarantees

Unit 2 transitions through explicit irreversible boundaries:

```text
[PREFLIGHT]
   │
   ├─► Lease available? (POST /worker/unit2/queue/next)
   │
[BROWSER_ACQUISITION]
   │
   ├─► Borrow existing Playwright page from persistent browser profile
   │
[SESSION_RESUME]
   │
   ├─► Verify My Information URL path, tenant scope, and draft continuity (Dual observation)
   │
[FORM_PREPARATION]
   │
   ├─► Server-side approved field mapping (zero LLM / PortalControlResolver)
   │
[SAVE_CLAIM & CLICK]
   │
   ├─► Atomic DB claim: save_claim_count (0 -> 1)
   ├─► Exactly ONE Save and Continue click (never retried)
   │
[NEXT_SECTION_CHECKPOINT]
   │
   ├─► Dual observation of allowlisted next section (My Experience, Application Questions, etc.)
   │
[FINALIZATION]
   │
   └─► Atomic DB commit: workday_unit2_completed + clear lease
```

---

## 4. Safety & Concurrency Invariants

1. **Atomic Database Save Fence**:
   - `POST /api/v1/automation/worker/unit2/queue/{application_id}/save-claim` locks the `WorkdayUnit2Attempt` row (`with_for_update()`).
   - Only returns `claimed_now` on the initial $0 \rightarrow 1$ transition. Any concurrent or subsequent call returns `already_claimed` with zero browser clicks.
2. **Deterministic Button Resolution**:
   - Resolves only `<button>` or `[role='button']` with exact accessible name `Save and Continue` or `Save & Continue`.
   - Disallows `<a>` links, disabled buttons, generic `Next` controls, and multiple matches.
3. **Closed Next-Section Allowlist**:
   - Only transitions to `MY_EXPERIENCE`, `APPLICATION_QUESTIONS`, `VOLUNTARY_DISCLOSURES`, or `SELF_IDENTIFICATION`.
   - Immediately fails closed to `review_required` on `Review and Submit`, `Submit`, confirmation, or unknown pages.
4. **Post-Claim Observe-Only Recovery**:
   - If an attempt was claimed but lost connection or entered review, recovery leasing issues an `observe_only` lease.
   - Observation mode verifies next-section hydration and completes without ever calling Save again.

---

## 5. Hold Remediation & Re-entry

When an operator resolves a Stage 2 hold (via profile answer, session renewal, or rescan):
- The application status is restored to `applying` (never left in generic `retrying`).
- A `workday_unit2_retry_ready` event is recorded.
- The application immediately becomes eligible for foreground leasing by the Unit 2 runner.

---

## 6. Forbidden Operations

In Unit 2:
- **Zero calls** to auth broker or credential vaults.
- **Zero calls** to LLM / `PortalControlResolver`.
- **Zero calls** to background autonomous daemons.
- **Zero clicks** on Review, Submit, or Next section controls.
