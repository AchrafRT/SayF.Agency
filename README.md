# SayF.Agency Deterministic Multi‑Tenant CRM (No JS / No DB)

## Run (local)
```bash
cd SayF_Agency_NoJS_MVP
python3 server.py
```

Open:
- Public: http://127.0.0.1:8000/
- Admin:  http://127.0.0.1:8000/admin/login
- Client: http://127.0.0.1:8000/client/login

## Admin subdomain separation (production-style)
This build enforces **admin portal on `admin.*` host** (and blocks `/client` on the admin host).

Locally, `localhost/127.0.0.1` is allowed for convenience.

If you want to test the subdomain behavior locally:
1. Add to `/etc/hosts`:
   - `127.0.0.1 admin.localhost`
   - `127.0.0.1 portal.localhost`
2. Use:
   - Admin:  http://admin.localhost:8000/admin/login
   - Portal: http://portal.localhost:8000/client/login

Creds:
- admin/admin
- DemenageursPlus/admin

## Storage
All state is stored in `data/tenants/<TENANT_ID>/*.json` plus `data/users.json` and `data/sessions.json`.
No external dependencies.

## Intake
Public site posts to `/intake` -> writes a `create_lead` command file to tenant inbox -> worker processes immediately -> lead appears in dashboard.

## Deterministic rule
All changes happen through command files and JSON storage. No background jobs required for the MVP.
