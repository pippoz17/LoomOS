# LoomOS — Textile ERP + CRM

**From Fibre to Fulfilment.** LoomOS is a local-first operational management platform for textile and garment businesses. It connects customer management, enquiries, quotations, sales orders, production, warehouse inventory, quality checks, procurement, dispatch, receivables, reports and a separate customer portal.

## This build starts as a real workspace

LoomOS no longer preloads fictional companies or exposes shared sample passwords by default. On first launch, the application opens a one-time setup page where you configure your company identity and create the first administrator account. The workspace then starts with an empty database, so records entered are the company’s own records. Customer portal access is enabled explicitly for a customer and does not use a shared default password.

No public site can contain a company’s real customer, order, inventory or financial data until those records are entered or imported by authorised staff. Carrier APIs, email/SMS delivery and an external language model also require provider credentials before they can be enabled. The local rule-based analysis and manually updated dispatch workflow are available without those integrations.

## Company and customer workspaces

- **Company / employee side:** `/login` — CRM, customers, enquiries and quotations, sales orders, production, inventory, quality, procurement, dispatch, invoices, analytics, reports, tasks, user management and settings. Access is checked on the server by role.
- **Customer / user side:** `/customer/login` — customer-specific order history, progress milestones, shipment updates, invoice balances and update requests. Customers can change their portal password from Account settings. Access is restricted to the signed-in customer’s records.
- **First-time setup:** `/setup` — creates the company configuration and first administrator only when no administrator exists. Once configured, this route is closed.

## Core workflows

- CRM lead pipeline and customer 360 view.
- Enquiry and quotation creation, status management and guarded quotation-to-order conversion.
- Order timelines and secure token-based customer tracking pages.
- Production batches, floor progress updates, automatic finished-goods stock posting on batch completion, and order-stage updates.
- Inventory item master and auditable stock receipts/issues/adjustments, with negative available stock prevention.
- Purchase order approval and goods receipts that post to the inventory ledger.
- Quality inspections and corrective-action tasks.
- Partial dispatch quantities, finished-goods stock deductions through the inventory ledger, shipment events and customer-visible tracking. An order is marked delivered only after its full dispatched quantity has been delivered.
- Invoices, partial payments, balance checking and overpayment prevention.
- CSV reports, explainable delivery-risk rules, material reorder suggestions, what-if calculations, sustainability records and QR-enabled product passports.
- Staff roles, customer-specific access checks, audit records and CSRF protection on form actions.

Analytics use operational records in the database; the dashboard no longer fabricates a monthly revenue trend. Heuristic scores and what-if calculations are estimates, not validated ML predictions.

## Requirements

- Python 3.10 or newer
- pip
- Internet access for the initial package installation

## Run locally on Windows

If you are replacing an earlier LoomOS folder that contains an existing database, run `START_NEW_WORKSPACE.bat` to back up the old database and initialise a new company workspace. Do not delete the backup unless you have verified it is no longer needed.


Open PowerShell in the extracted project directory. Activation is optional, so PowerShell execution-policy restrictions can be avoided by using the venv Python directly.

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

Open **http://127.0.0.1:5000**. If the workspace has not been configured yet, complete the one-time setup form. Your administrator sign-in is created by you; there is no hard-coded default administrator password.

## Optional fixture data for automated tests only

The test suite uses fictional records isolated in a temporary test database. If you specifically need a separate throwaway fixture workspace, set `LOOMOS_SEED_SAMPLE_DATA=1` before starting a fresh database. Do not enable that setting for a real company workspace. The production/default startup leaves fixture seeding disabled.

## Configuration

Copy `.env.example` values into your hosting provider or shell environment. The app reads OS environment variables; it does not automatically load a `.env` file.

- `LOOMOS_SECRET_KEY`: unique random secret; production startup requires at least 32 characters.
- `LOOMOS_DATABASE`: database file path; defaults to `instance/loomos.db`.
- `LOOMOS_ENV`: `development` by default; set `production` for deployed operation.
- `LOOMOS_COOKIE_SECURE`: set to `1` only when HTTPS terminates in front of the app.
- `LOOMOS_HOST`: defaults to `127.0.0.1` for local use.
- `PORT`: defaults to `5000`.
- `FLASK_DEBUG`: defaults to `0`; keep disabled in production.

## Deployment notes

On a compatible persistent Linux host, a basic process can start with:

```bash
gunicorn --workers 1 --bind 0.0.0.0:${PORT:-5000} app:app
```

The current database adapter is SQLite, which is suitable for local operation and a single persistent instance, not concurrent multi-instance serverless hosting. Before real company deployment, migrate the data layer to managed PostgreSQL, configure backups, HTTPS, a secret manager, monitoring, verified access policies and a recovery process. Do not deploy confidential customer or financial records until these controls have been reviewed. A Vercel-style ephemeral serverless filesystem is not suitable for this SQLite database without a proper external database migration.

## Tests

Install the requirements, then run:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The automated tests load fixtures only into a temporary database. They cover authentication, route permissions, customer record isolation, inventory ledger updates and selected financial/order safeguards.

## Project structure

```text
LOOMOS/
  app.py                 Flask routes and business logic
  schema.sql             Relational SQLite schema and indexes
  requirements.txt       Python dependencies
  START_LOOMOS.bat       Windows install/start helper
  .env.example           Environment variable reference
  templates/             Company pages, customer portal and first-run setup
  static/style.css       Responsive application styling
  static/app.js          Command palette, scenario calculations and analysis UI
  tests/                 Automated workflow tests
  instance/              Persistent database created at runtime
```


## Vercel deployment
See `VERCEL_DEPLOY_GUIDE.md`. This no-external-database deployment uses temporary SQLite and is for presentations/testing only; data persistence is not guaranteed on Vercel.
