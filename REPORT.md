# REPORT — Computer-use capability dig & replay

I built this so the durable object is not the chat with the model. It is a small
YAML capability that a dumb replay engine can run later, with evidence you can
diff. Start at `capabilities/` and `evidence/`, then come back here for the why.

## Honest provenance

The **approved** replay artifact (`capabilities/cucore.member.read_savings_balance.yaml`)
is **seeded** (`reviewed_by: seed`). That is deliberate: the assignment’s
non-negotiable live discovery run needs `ANTHROPIC_API_KEY`. Without a key we
refuse to invent Anthropic `message_id` rows. `evidence/01_discovery_read_balance/`
contains only `LIVE_REQUIRED.txt` + a skipped result — no leftover mock theater.

When a key is present, `scripts/generate_evidence_pack.py` runs a live Anthropic
discovery, logs `agent_turn` rows with `message_id` / usage, and ships a real
`trace.zip`. Live model traffic uses **redacted** screenshots (`safe_png`), not
raw pixels.

## Architecture

```
 Surface  ←→  discover (agent + recorder)  →  capability YAML
    ↑                                              │
    └──────── replay engine ←──────────────────────┘
                 ↓
              Result + evidence/
```

Policy and redaction sit on every act. Handoff freezes the same Chromium session
when something gets sticky. Operator console and in-process replay share
`cua.handoff.control.SHARED_CONTROLLER`. Resume enters `VERIFYING`; **Verify OK**
is a separate action (no auto-verify). `HumanCapture` installs click/change
listeners when handoff is enabled.

Why this shape: one Python process, Playwright, a deliberately hostile local
teller app. That covers the error zoo without Redis, Docker, or a fake auth
stack. Clone, `uv sync`, run the target, replay. Done.

Desktop: `desktop_stub.py` is an interface stub (UIA/AX map), not a driver.
Do not read it as a working desktop surface.

## Artifact schema

Open `capabilities/cucore.member.read_savings_balance.yaml`.

Locators are tried in order: role/name, then label-proximity / table-cell
(these actually work on legacy markup), then text, then CSS as a last resort.
Each strategy must hit exactly one node. Two matches is a failure, not a coin flip.

Every step has a plain-language `intent`. Declared outcomes (`member_not_found`,
`permission_denied`) are part of the contract. Inputs/outputs carry sensitivity
tags. Semver + `draft|approved|deprecated` gate unattended irreversible replay.
Tenant diffs live in `capabilities/overrides/tenant_b/…` as a merge-patch by
step id — **failing tenant_a strategies stay first** so drift is measurable.

## Determinism & error handling

Loop per step: detectors → policy → locate → act → expect → success checks.
Waits are load-state / DOM conditions with timeouts. No blind `time.sleep`.

| What we saw | Class | What we do |
|-------------|-------|------------|
| Balance extracted | SUCCESS | return outputs |
| “No member found…” | BUSINESS_OUTCOME | `member_not_found` |
| System notice | recoverable | click Acknowledge, log it |
| Injected 500 | FAILED / APP_ERROR | keep screenshot |
| Session died | NEEDS_HUMAN → resume | lease → operator → verify → continue; `intervention_id` sticks |
| Bad input pattern | INPUT_INVALID | fail before opening a browser |
| Fallback locator used | drift | `preferred_strategy` ≠ `strategy_used` (full labels) |

## Heterogeneity & multi-tenant

Everything UI-shaped goes through `Surface`. The web impl walks frames, computes
`near_label`, and stamps set-of-marks on the shot the model sees.

`07_cross_tenant_replay` runs the tenant_a artifact under tenant_b labels via
overlay. Drift signals name the preferred label that missed
(e.g. `role:Member Search` → `role:Holder Lookup`).

## Escalation & handoff

Automation owns the session until it does not. On stuck / session death /
irreversible confirm: `AWAITING_HUMAN` → claim → `HUMAN` → Resume → `VERIFYING`
→ Verify OK → automation (or abort).

Evidence run `06` walks that path in-process with a scripted operator (same
Chromium, CDP port armed, `HumanCapture` installed): fault → lease → claim →
re-login → restore Member Search → resume → verify → SUCCESS with
`intervention_id` + `human_handoff` recovery. The FastAPI `/operator` UI talks
to the same shared controller when run in-process with replay.

## Safety

Allowlist origins and actions in `policy/default.yaml`. Irreversible clicks are
confirm-over-block. Redaction: params → `{{inputs.x}}`, financial/PII masked in
logs, regex backstops, black boxes before screenshots are saved **or** sent to
the model. Login is scripted from env — the model never sees the password.

Honest limits: regex misses, visual PII, hosted-model egress. Discovery belongs
on sandbox data.

## Cuts

Skipped on purpose: polished co-browse UI, a real desktop driver, job queues,
vault integration, LLM fallback mid-replay. Next: single-step assisted fallback
keyed off `intent`, multi-run stability scores, tenant canaries.

## Evidence committed

| Dir | What you'll find |
|-----|------------------|
| `01_discovery_read_balance/` | Live Anthropic when keyed; otherwise marker-only skip (no fake transcript) |
| `02_replay_success/` | SUCCESS + balance + `trace.zip` |
| `03_replay_member_not_found/` | BUSINESS_OUTCOME |
| `04_replay_interstitial_recovered/` | SUCCESS + recovery event |
| `05_replay_app_error_failed/` | FAILED / APP_ERROR + screenshot + trace |
| `06_handoff_session_expired/` | Lease / human re-login / resume / verify + `intervention_id` + operator log + trace |
| `07_cross_tenant_replay/` | SUCCESS + labeled drift_signals |

Rebuild: `uv run python scripts/generate_evidence_pack.py`
