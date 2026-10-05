# mfgops-automation

Automation for MfgOps Process 1 (B2B Push to Arrow), starting with **Order Validation**:
orders booked in Oracle Fusion are fetched, routed (Standard / Special Instructions / EUS),
checked against business rules, and queued for a MfgOps user to approve, override or send
back to OM. In the POC a person confirms every decision; nothing is released automatically.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env      # then fill in values; .env is gitignored
make run                  # pipeline: poll, route, validate, close (Ctrl+C to stop)
make ui                   # Streamlit UI on http://localhost:8501
```

The UI also has a **Fetch & validate now** button, so `make ui` alone is enough for a demo.
With the default `FUSION_MODE=mock`, sample orders from `src/utils/mock_data.py` are used,
including the three review candidates from the dry run on the backlog extract.

## How it is organized

| Layer | Folder | Files |
|---|---|---|
| Integration (Fusion, email) | `src/integration_layer/` | `fetch_orders_from_fusion.py`, `push_orders_to_b2b.py`, `send_back_to_om.py` |
| Intelligence (decisions) | `src/intelligence_layer/` | `order_router.py`, `order_validation.py`, `llm_assist.py` |
| Knowledge (SQLite) | `src/knowledge_layer/` | `db.py`, `rule_store.py`, `reference_store.py`, `order_store.py` |
| UI (Streamlit) | `src/ui_layer/` | `app.py` |
| Shared helpers | `src/utils/` | `config.py`, `models.py`, `mock_data.py`, `logging_config.py`, `time_helpers.py` |

`src/app.py` ties them together: every `POLL_INTERVAL_SECONDS` it syncs orders, validates
new ones and closes released ones. It also serves `/healthz` for Kubernetes probes.

**Rules decide, the LLM explains.** Routing and pass/fail are deterministic
(`order_router.py`, `order_validation.py`). The LLM (via the LiteLLM proxy) is used in one
place only, `llm_assist.explain_discrepancies()`, to explain findings in plain language.
Without LiteLLM settings it falls back to a template.

## Order lifecycle

`ROUTED` → validated → `IN_REVIEW` (or `WAITING_ON_OM` if a document is missing) →
user decides:

- **Approve & Push** (AI said approve) or **Override & Push** (AI said reject; comment
  required, e.g. "EUS approved by email") → `AWAITING_USPO` → closed when the USPO appears.
- **Send back to OM** (comment required) → `SENT_BACK_TO_OM`. When OM corrects the order in
  Fusion, the next sync updates it in place and validates it again.

Closed orders are deleted from the working tables (Fusion keeps the full record) and a
small permanent row goes to `order_history`, which feeds the Analytics tab. Every human
decision is kept in `order_decisions`.

## Extending it

- **New rule over existing facts**: Knowledge Addition tab (or `rule_store.add_rule`). No code.
  A rule with the same domain, scope and checked facts replaces the older one.
- **New reference entry** (EUS country, cord mapping, SKU ratio…): Knowledge Addition tab.
- **New kind of check needing new data**: add a fact in
  `order_validation.build_order_facts()` and `FACT_DESCRIPTIONS`, then add rules over it.
- **EUS workstream**: add EUS modules beside the existing ones and fill `orders.eus_case_id`
  and `orders.documents_received`; the Order Validation code already reads them.

## Going live

Live mode is wired but waits on details from Fusion IT. Until then each live function
raises `NotImplementedError` explaining what's missing:

- Integration user credentials (Basic Auth or OAuth) → `FUSION_*` in `.env`
- BIP path and parameters of "Infoblox Orders Backlog Report", the status that marks an
  order ready to release, and the field mapping → `fetch_backlog_live()`
- ESS job definition of "Infoblox Priority Release Pause Task" → `submit_priority_release_job_live()`
- Where the USPO appears → `check_uspo_status()`
- MS Graph app registration and sender mailbox → `send_back_to_om.send_live()`

## Development

`make lint` (black + flake8) and `make test` (pytest). Tests use a temp database and never
call Fusion or the LLM.
