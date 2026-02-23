# SayF.Agency No‑JS Local MVP — Route & UI Inventory (vFinal‑patched)

This document is a **repertoire of pages + tabs + main actions** so you can sanity‑check flows fast.

## 1) Public site (lead intake)
- **GET /**  
  Public landing + lead intake entry points.
- **Static assets**
  - **GET /static/logo.svg**
  - **GET /static/favicon.ico** (if present)

## 2) Admin portal (owner)
- **GET /admin/login**
- **POST /admin/login**  
  Credentials (default): `admin / admin`
- **GET /admin?tab=...** (tabs)
  1. `notifications` — To‑Do categories (new leads, follow‑ups, calls, jobs, overdue, missed replies)
  2. `leads` — lead list + lead modal actions
  3. `calendar` — month/week/day views + event modals
  4. `clients` — tenant/client list + overview modals
  5. `pnl` — real‑time P&L
  6. `messages` — inbox view (placeholder / deterministic)
  7. `employees` — agency employees CRUD + view modal

### Admin key modal routes (via query params)
- Lead modal: `/admin?tab=leads&modal=view_lead&lead_id=...`
- New event from lead: `/admin?tab=leads&modal=new_event&lead_id=...`
- View event: `/admin?tab=calendar&modal=view_event&event_id=...`
- View tenant/client: `/admin?tab=clients&modal=view_tenant&tenant_id=...`

## 3) Admin employees (sales/support mode)
Same admin URL, but restricted view:
- `/admin?tab=...&mode=employee`
- Expected: **no P&L**, limited controls, only assigned leads/clients/events.

## 4) Tenant portal (moving company owner)
- **GET /client/login**
- **POST /client/login**  
  Credentials (default): `DemenageursPlus / admin`
- **GET /client?tenant=T000&tab=...** (tabs)
  - `notifications`, `leads`, `calendar`, `clients`, `pnl`, `messages` *(depending on role)*

## 5) Tenant employees (driver/mover)
- Uses same `/client` portal but role is `client_employee` (normalized to tenant_employee in UI gating).
- Should show calendar + assigned jobs; should not show sensitive financial tabs.

---

# Quick QA checklist (manual)
1. Login Admin → click each tab → no crashes.
2. Leads → open a lead modal → update fields → save (POST /cmd).
3. Convert lead to client → verify it appears under Clients.
4. Calendar → open event → verify details and back button works.
5. Client portal login → loads dashboard (no errors).
6. Static logo loads everywhere (no 404).

See `tools/smoke_test.py` for an automated route smoke test.
