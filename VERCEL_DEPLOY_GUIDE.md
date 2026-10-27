# LoomOS — Vercel presentation deployment

## Important limitation
This package is configured for a Vercel-hosted presentation using SQLite in `/tmp`.
Vercel's temporary filesystem is not durable or shared across all serverless instances.
Records created during a session may be lost after a cold start/redeployment, and different
instances may show different data. Do not use this setup for real customer or production data.
This limitation cannot be eliminated without persistent storage.

## Deploy to Vercel
1. Extract this ZIP.
2. In Vercel, open the existing LoomOS project and ensure the **Root Directory** is the
   folder containing `app.py`, `requirements.txt`, `schema.sql`, `templates/`, and `public/`.
3. Push this folder to the GitHub repository connected to Vercel, or import it as a new project.
   Vercel should detect Flask automatically.
4. In Vercel → Project → Settings → Environment Variables, add:
   - `LOOMOS_SECRET_KEY` = a long, unique random value (at least 32 characters).
   - `LOOMOS_SEED_SAMPLE_DATA` = `1` (loads sample records and demo logins on a new/empty instance).
   Apply to Production and Preview, then redeploy.
5. After deployment, open `/health`. It should return JSON with `"status":"ok"`.
6. If the database starts empty, open `/setup` to create the first administrator.

## Demo login accounts (when sample seeding is enabled)
- Company administrator: `admin@loomos.local` / `LoomOS@2026`
- Sales manager: `sales@loomos.local` / `Sales@2026`
- Production manager: `production@loomos.local` / `Factory@2026`
- Inventory manager: `inventory@loomos.local` / `Stock@2026`
- Finance executive: `finance@loomos.local` / `Finance@2026`
- Quality inspector: `quality@loomos.local` / `Quality@2026`
- Customer portal: `rhea@northstar.example` / `Customer@2026`

These are demo credentials only. Do not use them for real data. Change/remove them before any production use.

## Local testing
Use Python 3.12+, install `requirements.txt`, then run:
`python -m unittest discover -s tests -v`

## Included changes
- Vercel detection selects `/tmp/loomos.db` instead of attempting to write inside the deployed source directory.
- Static assets are copied into `public/static` for Vercel CDN delivery.
- `.python-version` selects Python 3.12.
- `vercel.json` excludes test and documentation files from the function bundle.
