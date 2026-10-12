# mfgops-automation

Automation for MfgOps Process 1 (B2B Push to Arrow), starting with **Order Review**:
orders booked in Oracle Fusion are fetched, put in one of four categories (Standard /
Special Instructions / EUS / EUS + Special Instructions), checked by fixed rules and an AI
judge, and queued for a MfgOps user to approve, send back to OM, or hold. In the POC a
person confirms every decision; nothing is released automatically.

## Quick start

Needs Python 3.12 (see `.python-version`).

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env      # then fill in values; .env is gitignored
make run                  # service: checks Fusion and reviews new orders (Ctrl+C to stop)
make ui                   # Streamlit UI on http://localhost:8501
```

The UI has a **Check Fusion for new orders** button, so `make ui` alone is enough for a demo.

### Where the orders come from (`FUSION_MODE` in `.env`)

| Mode | Orders |
|---|---|
| `file` | A real **Orders Backlog Report** `.xls` exported from Fusion (`BACKLOG_FILE_PATH`), plus 10 invented demo orders that the AI should reject or hold (`INCLUDE_DEMO_ORDERS=false` leaves them out). To review a newer report, replace the file and click the button again. |
| `mock` (default) | Invented sample orders from `src/utils/mock_data.py`; needs no files. Used by the tests. |
| `live` | Fusion Dev1 (not wired yet, see "Going live"). |

Notes on the backlog report: it has one row per order line (read by column heading, so
column order doesn't matter). Its "Purchase Order" column is the **Arrow PO (USPO)**, not
the customer's PO, and it has no end-customer country. Return, RMA and depot orders are
skipped (the "Order types not checked" list). Keep real report files out of the repo.

## How it is organized

| Layer | Folder | Files |
|---|---|---|
| Integration (Fusion, email) | `src/integration_layer/` | `fetch_orders_from_fusion.py`, `read_backlog_report.py`, `push_orders_to_b2b.py`, `send_back_to_om.py` |
| Intelligence (decisions) | `src/intelligence_layer/` | `order_review_workflow.py`, `order_router.py`, `rule_checks.py`, `ai_order_judge.py`, `rules_assistant.py`, `llm_client.py` |
| Knowledge (SQLite) | `src/knowledge_layer/` | `db.py`, `rule_store.py`, `reference_store.py`, `order_store.py`, `learning_request_store.py` |
| UI (Streamlit) | `src/ui_layer/` | `app.py` |
| Shared helpers | `src/utils/` | `config.py`, `models.py`, `mock_data.py`, `logging_config.py`, `time_helpers.py` |

`src/app.py` ties them together: every `POLL_INTERVAL_SECONDS` it fetches orders and starts
the review workflow for new and corrected ones. It also serves `/healthz` for Kubernetes.

## The review workflow (LangGraph)

`order_review_workflow.py` is one LangGraph graph per order; the whole flow is in that file:

```
decide category → run fixed checks → AI verdict → send to review queue
   → [PAUSE: wait for the reviewer]
   → release to Arrow | email OM | keep on hold (→ wait again)
   → learn from disagreement → done
```

- **Pausing costs nothing.** While an order waits for a person, the graph is saved to
  `WORKFLOW_DB_PATH` and nothing runs. A button click resumes it.
- **Only the "AI verdict" step uses the AI.** Routing, fixed checks, release, emails and
  database writes are plain code.
- **Guard rails.** Hard rules run first and the AI can't weaken them: a rejecting hard rule
  means reject; a holding one means at least hold. Embargo and banned-customer rules can
  never be overridden, so those orders can't be released at all. A missing EUS is a hold,
  which the reviewer may approve (e.g. the EUS was already sent to Arrow by email).
- **Release is the last step.** The USPO Arrow creates shows up in the backlog report and
  isn't read back.

| AI suggested | Reviewer chose | What happens |
|---|---|---|
| Release | Approve | Released to Arrow; leaves active orders, stays in all orders |
| Send to OM | Approve | Released (unless never-releasable); the AI asks the reviewer why, in the Rules Assistant |
| Release | Send to OM | OM gets an email with the comment and order details; the AI asks why |
| Send to OM | Send to OM | OM gets an email with the comment and order details |
| Hold, or reviewer holds | — | Stays in the queue until decided; not counted as a disagreement |

## Rules

One `rules` table holds both kinds of rule; each rule leaves the columns it doesn't need empty:

- **Hard rules** (fixed checks): `conditions` on the facts built in
  `rule_checks.build_order_facts()` — embargo, banned customer, mandatory fields, cord type
  vs country, SFP+ vs 10GE, EUS / OM email received.
- **Soft rules** (AI judgement): plain-English `guidance` for the AI — cords per appliance,
  blue-ink stamp, Arrow email comments, letter-of-credit orders.

The Rules Assistant tab shows the current rules and lists, read fresh from the database on
every page refresh. Rules and list entries that are new or changed since the page was
opened are marked 🆕 / ✏️, and the section opens by itself.

Both use `applies_to_routes` and `scope` to say which orders they cover, so an EU order
and a Thailand order get different rules. Lists such as embargoed countries and the cord
type per country live in `reference_lists`.

**Rules Assistant.** MfgOps users add, change, remove or look up rules and list entries by
chatting (`rules_assistant.py`, also a LangGraph graph). The AI works through fixed tools
that call `rule_store` / `reference_store` (it never writes SQL), and every change is shown
in plain words and only saved after the user clicks **Yes**. Removed rules are retired, not
deleted. Rules are kept general, never tied to one order number.

## Storage

- `orders` / `order_lines` / `rule_findings`: orders still being worked on.
- `all_orders`: one row per order ever seen, with the AI verdict, reviewer decision and
  whether they agreed. Feeds the Dashboard.
- `order_decisions`: every reviewer decision, kept forever.
- `learning_requests`: disagreements the AI still wants to learn from.
- `WORKFLOW_DB_PATH` (default `data/workflow_checkpoints.db`): paused workflows and chats.

## AI (LiteLLM)

The AI is called through Infoblox's LiteLLM proxy (OpenAI-compatible) using
`LITELLM_BASE_URL`, `LITELLM_API_KEY` and `LITELLM_MODEL` from `.env`; change the model
there. With any of them blank the AI is off: orders are judged by the hard rules only and
the soft rules are listed for the reviewer to check by hand.

Use a model name your proxy actually offers (list them with the proxy's `/v1/models`).
Tested on 10 real orders against `claude-sonnet-5`: **`gpt-5.4-mini`** gave the same verdict
on all 10, returned valid answers every time, and was about 2.5× faster, so it is the
default. `gpt-5-mini` is a reasoning model that often returns empty answers here; avoid it.

## Going live

Live mode is wired but waits on details from Fusion IT. Until then each live function
raises `NotImplementedError` explaining what's missing:

- Integration user credentials (Basic Auth or OAuth) → `FUSION_*` in `.env`
- BIP path and parameters of "Infoblox Orders Backlog Report", the status that marks an
  order ready to release, and the field mapping → `fetch_backlog_live()`
- ESS job definition of "Infoblox Priority Release Pause Task" → `submit_priority_release_job_live()`
- MS Graph app registration and sender mailbox → `send_back_to_om.send_live()`

## Development

`make lint` (black + flake8) and `make test` (pytest). Tests use a temp database, never call
Fusion, and replace the AI with canned replies.
