# mfgops-automation

Automation for MfgOps Process 1 (B2B Push to Arrow), starting with **Order Review**:
orders booked in Oracle Fusion are read from the backlog report Fusion mails every ~15
minutes, put in one of four categories (Standard / Special Instructions / EUS / EUS +
Special Instructions), checked by fixed rules and an AI judge, and queued for a MfgOps user
to approve, send back to OM, or hold. In the POC a person confirms every decision and
releases the order in Fusion by hand; nothing is released automatically.

## Quick start

Needs Python 3.12 (see `.python-version`).

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env      # then fill in values; .env is gitignored
make mail-check           # read-only check of the mailbox and the newest backlog mail
make run                  # service: reads the backlog and reviews new orders (Ctrl+C to stop)
make ui                   # Streamlit UI on http://localhost:8501
```

The UI has a **Check for new orders** button, so `make ui` alone is enough for a demo.
`make reset-order-data` backs up the databases and clears all orders (rules are kept).

### Where the orders come from (`FUSION_MODE` in `.env`)

| Mode | Orders |
|---|---|
| `mail` | The newest **Infoblox Orders Backlog Report** mail Fusion sends to `GRAPH_API_MAILBOX` (from `BACKLOG_SENDER`, attachment `Infoblox Orders Backlog Report.xls`), read through MS Graph. Older unread report mails are skipped, since each report is the full backlog. |
| `file` | A saved backlog report `.xls` (`BACKLOG_FILE_PATH`). |
| `mock` (default) | Only the 5 dummy orders. Used by the tests. |
| `live` | Fusion API (not available: IT can't provide it before the Computron launch). |

In `mail` and `file` mode the 5 **dummy orders** are added (`INCLUDE_DEMO_ORDERS=false`
leaves them out). They are invented to show the AI rejecting or holding orders, their
numbers start with `dummy_`, and they are always listed after the real orders.

Notes on the backlog report: it has one row per order line (read by column heading, so
column order doesn't matter; a missing column stops the read with a clear message). Its
"Purchase Order" column is the **USPO** Fusion creates on release, not the customer's PO,
and it has no end-customer country. Return, RMA and depot orders are skipped (the "Order
types not checked" list). Only orders with a **blank USPO** are reviewed; a USPO means the
order was already released to Arrow. Real report files stay under `data/` (gitignored).

## How it is organized

| Layer | Folder | Files |
|---|---|---|
| Integration (Fusion, email) | `src/integration_layer/` | `fetch_orders_from_fusion.py`, `fetch_backlog_from_mail.py`, `graph_mail_client.py`, `read_backlog_report.py`, `push_orders_to_b2b.py`, `send_back_to_om.py`, `send_order_mail_to_arrow.py`, `outbound_mail.py` |
| Intelligence (decisions) | `src/intelligence_layer/` | `order_review_workflow.py`, `order_router.py`, `rule_checks.py`, `ai_order_judge.py`, `rules_assistant.py`, `llm_client.py` |
| Knowledge (SQLite) | `src/knowledge_layer/` | `db.py`, `rule_store.py`, `reference_store.py`, `order_store.py`, `learning_request_store.py`, `mail_store.py` |
| UI (Streamlit) | `src/ui_layer/` | `app.py` |
| Shared helpers | `src/utils/` | `config.py`, `models.py`, `mock_data.py`, `logging_config.py`, `time_helpers.py` |

`src/app.py` ties them together: every `POLL_INTERVAL_SECONDS` it fetches orders and starts
the review workflow for new and corrected ones. It also serves `/healthz` for Kubernetes.

## The review workflow (LangGraph)

`order_review_workflow.py` is one LangGraph graph per order; the whole flow is in that file:

```
decide category → run fixed checks → AI verdict → send to review queue
   → [PAUSE: wait for the reviewer]
   → approve (released in Fusion by hand) | email OM | keep on hold (→ wait again)
   → learn from disagreement
   → (approved) [PAUSE: wait for Fusion's USPO] → finish release (+ Arrow mail) → done
```

- **Pausing costs nothing.** While an order waits for a person, the graph is saved to
  `WORKFLOW_DB_PATH` and nothing runs. A button click resumes it.
- **Only the "AI verdict" step uses the AI.** Routing, fixed checks, release, emails and
  database writes are plain code.
- **Guard rails.** Hard rules run first and the AI can't weaken them: a rejecting hard rule
  means reject; a holding one means at least hold. Embargo and banned-customer rules can
  never be overridden, so those orders can't be released at all. A missing EUS is a hold,
  which the reviewer may approve (e.g. the EUS was already sent to Arrow by email).
- **Approve means "I released it in Fusion".** Without Fusion API access, the reviewer runs
  the Priority Release in Fusion and then clicks Approve. The order stays listed as
  "Approved — waiting for the USPO" until its USPO shows up in a backlog report mail; then
  it is released, leaves active orders, and the USPO is shown in All orders. Special
  Instructions orders then get the Arrow mail. Dummy orders are released straight away.
- **No USPO is shown before release.** An order released in Fusion without a decision here
  is closed as "released outside this tool". An order missing from two reports in a row
  (e.g. cancelled) is closed as "no longer in the Fusion backlog".

| AI suggested | Reviewer chose | What happens |
|---|---|---|
| Release | Approve | Waits for the USPO, then released; leaves active orders, stays in all orders |
| Send to OM | Approve | Released (unless never-releasable); the AI asks the reviewer why, in the Rules Assistant |
| Release | Send to OM | OM gets an email with the comment and order details; the AI asks why |
| Send to OM | Send to OM | OM gets an email with the comment and order details |
| Hold, or reviewer holds | — | Stays in the queue until decided; not counted as a disagreement |

## Rules

One `rules` table holds both kinds of rule; each rule leaves the columns it doesn't need empty:

- **Hard rules** (fixed checks): `conditions` on the facts built in
  `rule_checks.build_order_facts()` — embargoed country or region, banned customer,
  mandatory fields (shipping method, FOB, ship-to contact name and phone), forwarder
  details on Freight Collect orders, cord count (too few = send to OM, too many or mixed
  types = hold), cord type vs ultimate destination, SFP+ vs 10GE, T-PSU600-AC vs appliance,
  EUS / OM email received.
- **Soft rules** (AI judgement): plain-English `guidance` for the AI — cords per appliance,
  ship-from vs destination, blue-ink stamp, Arrow email comments, letter-of-credit orders,
  1GbE SFP transceivers.

New seed rules and list entries reach an existing database once, on start
(`upgrade_seed_rules` / `upgrade_reference_lists`); rules a user edited or retired, and list
entries a user changed or deleted, are left alone.

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
- `mail_messages`: every backlog report mail read and every email sent (metadata only, no
  bodies); stops a report being read twice or an email being sent twice.
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

## Mail (MS Graph)

`graph_mail_client.py` signs in as the app registration in `.env` (`GRAPH_API_TENANT_ID`,
`GRAPH_API_CLIENT_ID`, `GRAPH_API_CLIENT_SECRET`; app-only, Mail.Read + Mail.Send on the
mailbox). `EMAIL_MODE=live` sends from `GRAPH_API_SENDER_MAILBOX` (or the monitored
mailbox); `mock` writes `.html` files to `OUTBOX_DIR`. While `EMAIL_REDIRECT_TO` is set,
every email goes to that address, with the intended recipient shown at the top. A failed
email to OM changes nothing, so the reviewer can simply try again.

## Going live with the Fusion API

Fusion API access is not available yet. `FUSION_MODE=live` raises `NotImplementedError`
explaining what's missing; the mail mode needs none of it:

- Integration user credentials (Basic Auth or OAuth) → `FUSION_*` in `.env`
- BIP path and parameters of "Infoblox Orders Backlog Report" → `fetch_backlog_live()`
- ESS job definition of "Infoblox Priority Release Pause Task" →
  `submit_priority_release_job_live()` (then Approve submits the release itself)

## Development

`make lint` (black + flake8) and `make test` (pytest). Tests use a temp database, never call
Fusion, and replace the AI with canned replies.
