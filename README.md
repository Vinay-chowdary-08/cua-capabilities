# CUA Capabilities

Discover reusable computer-use **capabilities** against a hostile legacy credit-union core, save versioned YAML artifacts, and **replay them without an LLM**.

Written so another agent (or a tired human) can clone and run it cold.

## Honest note

The approved capability used for replay evidence is **seeded** (`reviewed_by: seed`).
Live Anthropic discovery evidence (`evidence/01_*`) requires `ANTHROPIC_API_KEY`.
Without a key the pack leaves a marker only — it will not invent an LLM transcript.

## Quick start

```bash
uv sync --extra dev
uv run playwright install chromium
cp .env.example .env

uv run cua seed
uv run cua serve-target --variant tenant_a --port 8800
# other terminal:
uv run cua operator --port 8900

uv run cua replay cucore.member.read_savings_balance --input member_number=12345
uv run cua replay cucore.member.read_savings_balance --input member_number=99999
uv run cua replay cucore.member.read_savings_balance --input member_number=12345 --fault interstitial
uv run cua replay cucore.member.read_savings_balance --input member_number=12345 --fault error500
uv run cua replay cucore.member.read_savings_balance --input member_number=12345 --tenant tenant_b

# Discovery defaults to mock. Live Anthropic evidence (assignment 01):
#   put ANTHROPIC_API_KEY in .env, then:
#   CUA_MOCK_LLM=0 uv run python scripts/generate_evidence_pack.py
uv run cua discover \
  --goal "read the savings balance for member {member_number}" \
  --param member_number=12345 \
  --id cucore.member.read_savings_balance
uv run cua approve capabilities/cucore.member.read_savings_balance.yaml
```

Login: `teller` / `teller`.

Rebuild committed evidence:

```bash
uv run python scripts/generate_evidence_pack.py
```

## Ports

| Service | URL |
|---------|-----|
| Target CU core | http://127.0.0.1:8800 |
| Operator console | http://127.0.0.1:8900/operator |

## Layout

See `REPORT.md`. Focal contract: `cua/artifact/schema.py`. Credentials are scripted at login and never sent to the LLM. Live discovery sends **redacted** screenshots only.

## Tests

```bash
uv run cua serve-target --port 8800 &
uv run cua seed
uv run pytest -q
```

Integration tests **fail** (not skip) if the target is down.
