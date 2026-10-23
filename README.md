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

## Retrieve mailbox attachments

`src.graph_attachments` downloads attachments from messages sent by configured
addresses. It uses Microsoft Graph app-only authentication. The app registration
needs the Microsoft Graph **Mail.Read** application permission with admin
consent, and its credentials must be allowed to access the target mailbox.

Set these environment variables before running the command:

| Variable | Description |
| --- | --- |
| `GRAPH_TENANT_ID` | Microsoft Entra tenant ID |
| `GRAPH_CLIENT_ID` | App registration client ID |
| `GRAPH_CLIENT_SECRET` | App registration client secret |
| `GRAPH_MAILBOX` | Mailbox address or user ID to search |
| `GRAPH_SENDER_EMAILS` | Comma-separated sender email addresses |
| `ATTACHMENT_OUTPUT_DIR` | Destination directory; defaults to `attachments` |

Copy `.env.example` to `.env` and fill it in locally. `.env` is ignored by Git
so app secrets are not committed. Install runtime dependencies with
`pip install -r requirements.txt`, then run:

```sh
python -m src.graph_attachments
```

Each email gets its own folder under the configured output directory. The
folder name contains the email's UTC received timestamp and message ID, for
example `20261015_121500_AAMk.../`, and holds the real file attachments from
that email. Inline body resources such as signature logos and icons are
ignored, including Graph file attachments that are marked with a content ID or
use common body-resource names such as `image001.png` or `img-<uuid>.png`.

Reference attachments are the one exception: they are links to files in
OneDrive or SharePoint rather than real files, so Microsoft Graph serves no
content for them and there is nothing to save.

Successfully checked Graph message IDs are recorded in the
`processed_mail_messages` table in `DB_PATH`. Later mailbox checks skip those
messages instead of downloading historical attachments again. Deleting files
from the attachment folder does not delete this processing history.

When an email body contains a `Special instruction(s):` or
`Shipping instruction(s):` marker, the downloader extracts only the text
following that marker, stopping before signatures and forwarded-message
headers. It saves that extracted section—not the complete email body—as hidden
metadata in the email's timestamped folder. Bodies without either marker are
not retained. The EUS Approval tab displays the extracted section under
**Special instructions**:

- when no EUS is identified, a reviewer can share the instructions with the PO
  and Quote attachments;
- when an EUS is identified, sharing stays disabled until that EUS is approved,
  then the instructions, PO, Quote, and verified EUS are sent together;
- the `shared_mail_packages` table records who shared each email package and
  prevents an accidental duplicate share from the UI.

Order routing uses the Infoblox-required countries from **EUS Countries v3.0 —
01 August 2022**. China (`CN`), Hong Kong (`HK`), and South Korea (`KR`) are
therefore routed through the EUS workflow and held until an approved EUS is
linked. North Korea (`KP`) remains an embargoed destination and cannot be
released.

When an EUS attachment is approved or rejected in the UI, the outcome,
reviewer, comment, attachment path, and decision time are stored in the
`attachment_decisions` table in `DB_PATH` (default `data/mfgops.db`).

Downloaded mailbox attachment bytes and metadata are stored in the
`mailbox_attachments` table in the same SQLite database. The `attachments/`
directory is a materialized cache used by the document preview and review
flows; deleting a cached file does not delete the SQLite copy, and the UI
restores it from SQLite when it next loads.

The **EUS Approval** tab has two views. **Active EUS documents** (left) shows complete email folders in which no
document has been approved or rejected yet; **All EUS documents** (right)
shows every downloaded document grouped by email. Approving or rejecting any
file moves its whole email folder out of Active. This queue state is stored
in the `eus_documents` table.

### Automated EUS checks

For each newly downloaded timestamped email folder, the application automatically
extracts and compares text-searchable PDF attachments. The **EUS Approval** tab
shows the results in a collapsible **Automated EUS checks** section. It starts
minimized, and its title shows whether all checks passed or how many failed.
A **Re-run automated EUS checks** control is available when a manual rerun is
needed:

- identify one EUS and at least one supporting PO or Quote; each supporting
  document is optional when the other is present;
- compare the EUS quote number against the available PO and/or Quote; a match
  with either one passes, and at least one product line must match by
  quantity and part number or partner SKU (not every line is required);
- require **No** for question 6 wherever it appears in the EUS;
- require a signer title of manager or higher;
- validate the signer email format and require at least one document or
  signature date;
- accept a clearly visible handwritten/electronic signature on a scanned EUS,
  or verify that an embedded PDF signature is cryptographically intact and
  valid. A visible signature is treated as present but is not falsely described
  as cryptographically authenticated;
- require the end-user name, address, and website, verify that the website
  responds, and ask the AI whether the name, address, and website domain
  describe a legitimate organisation.

A new end user must pass every check. A repeat customer — an end user with an
approved EUS in an earlier email — passes when at least 70% of the checks pass;
the confidence score is shown in the review.

Special instructions are shared with only the PO and Quote. The EUS is
forwarded separately when it is approved, and sharing waits for that approval.

Every check is shown as pass or fail. A reviewer can still override a failed or
unverified automated result and approve manually. The extractor requires the
LiteLLM settings. Text-searchable pages are sent as text; scanned pages are
rendered locally and sent as page images for visual extraction.

## Send an attachment with comments

`src.send_attachment` sends one file from `GRAPH_MAILBOX` using Microsoft
Graph. The app registration needs the **Mail.Send** application permission with
admin consent in addition to the permissions used for downloading.

Provide the recipient, attachment, and subject on the command line:

```sh
python -m src.send_attachment \
  --recipient recipient@example.com \
  --attachment attachments/20261015_121500_AAMk.../invoice.pdf \
  --subject "Invoice review"
```

The command then prompts for comments. Enter one or more lines and finish with a
line containing only `.`. Successfully accepted messages are saved in the
mailbox's Sent Items folder. Attachments under 3 MB are sent in a single
request; larger ones are uploaded in chunks through a Microsoft Graph upload
session, up to the Graph ceiling of 150 MB.
