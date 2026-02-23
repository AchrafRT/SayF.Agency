#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Worker: processes command files into JSON "DB" files.
Standard library only.
"""

from __future__ import annotations
import json, os, time, traceback, uuid
from typing import Dict, Any, Optional, Tuple

from .utils import read_json, write_json, ensure_dir, now_iso, safe_join, require_safe_id

def _next_id(prefix: str, existing: Dict[str, Any]) -> str:
    # Deterministic-ish: count+1
    n = len(existing.keys()) + 1
    return f"{prefix}{n:04d}"

def _load_map(path: str) -> Dict[str, Any]:
    obj = read_json(path, {})
    return obj if isinstance(obj, dict) else {}

def _save_map(path: str, obj: Dict[str, Any]) -> None:
    write_json(path, obj)

def tenant_paths(tenant_dir: str) -> Dict[str, str]:
    return {
        "leads": os.path.join(tenant_dir, "leads.json"),
        "leads_archive": os.path.join(tenant_dir, "leads_archive.json"),
        "clients": os.path.join(tenant_dir, "clients.json"),
        "jobs": os.path.join(tenant_dir, "jobs.json"),
        "calendar": os.path.join(tenant_dir, "calendar.json"),
        "pnl": os.path.join(tenant_dir, "pnl.json"),
        "trucks": os.path.join(tenant_dir, "trucks.json"),
        "employees": os.path.join(tenant_dir, "employees.json"),
        "messages_email": os.path.join(tenant_dir, "messages_email.json"),
        "messages_sms": os.path.join(tenant_dir, "messages_sms.json"),
        "quotes": os.path.join(tenant_dir, "quotes.json"),
        "invoices": os.path.join(tenant_dir, "invoices.json"),
        "docs_meta": os.path.join(tenant_dir, "docs_meta.json"),
        "templates": os.path.join(tenant_dir, "templates.json"),
        "logs": os.path.join(tenant_dir, "logs", "worker.log"),
        "processed": os.path.join(tenant_dir, "processed"),
        "docs": os.path.join(tenant_dir, "docs"),
    }

def log_line(paths: Dict[str, str], msg: str) -> None:
    ensure_dir(os.path.dirname(paths["logs"]))
    with open(paths["logs"], "a", encoding="utf-8") as f:
        f.write(f"[{now_iso()}] {msg}\n")

def process_command_file(tenant_dir: str, cmd_path: str) -> Tuple[bool, str]:
    paths = tenant_paths(tenant_dir)
    try:
        with open(cmd_path, "r", encoding="utf-8") as f:
            cmdobj = json.load(f)
        cmd = cmdobj.get("cmd")
        payload = cmdobj.get("payload") or {}
        if not cmd:
            return False, "missing cmd"

        ok, detail = execute(tenant_dir, cmd, payload)

        ensure_dir(paths["processed"])
        base = os.path.basename(cmd_path)
        dst = os.path.join(paths["processed"], base)
        os.replace(cmd_path, dst)
        log_line(paths, f"{cmd} -> {ok} {detail}")
        return ok, detail
    except Exception as e:
        try:
            log_line(paths, f"ERROR {e}\n{traceback.format_exc()}")
        except Exception:
            pass
        return False, f"exception: {e}"

def execute(tenant_dir: str, cmd: str, payload: Dict[str, Any]) -> Tuple[bool, str]:
    cmd = require_safe_id(cmd, "cmd")
    paths = tenant_paths(tenant_dir)

    leads = _load_map(paths["leads"])
    clients = _load_map(paths["clients"])
    jobs = _load_map(paths["jobs"])
    cal = _load_map(paths["calendar"])
    pnl = read_json(paths["pnl"], {"entries": []})
    trucks = _load_map(paths["trucks"])
    employees = _load_map(paths["employees"])

    def save_all():
        _save_map(paths["leads"], leads)
        _save_map(paths["clients"], clients)
        _save_map(paths["jobs"], jobs)
        _save_map(paths["calendar"], cal)
        write_json(paths["pnl"], pnl)
        _save_map(paths["trucks"], trucks)
        _save_map(paths["employees"], employees)

    if cmd == "create_lead":
        lid = _next_id("L", leads)
        leads[lid] = {
            "id": lid,
            "created_at": now_iso(),
            "status": payload.get("status","new"),
            "name": payload.get("name",""),
            "phone": payload.get("phone",""),
            "email": payload.get("email",""),
            "company_name": payload.get("company_name",""),
            "industry": payload.get("industry",""),
            "website": payload.get("website",""),
            "from_address": payload.get("from_address",""),
            "to_address": payload.get("to_address",""),
            "move_date": payload.get("move_date",""),
            "property_type": payload.get("property_type",""),
            "property_size": payload.get("property_size",""),
            "notes": payload.get("notes",""),
            "source": payload.get("source","website"),
            "value_est": float(payload.get("value_est") or 0),
        }
        save_all()
        return True, f"created {lid}"

    if cmd == "delete_lead":
        lid = payload.get("id")
        if lid in leads:
            leads.pop(lid)
            save_all()
            return True, f"deleted {lid}"
        return False, "not found"

    if cmd == "convert_to_client":
        lid = payload.get("lead_id")
        if lid not in leads:
            return False, "lead not found"
        lead = leads.pop(lid)

        # Archive the lead instead of deleting it (audit trail)
        leads_archive = _load_map(paths["leads_archive"])
        leads_archive[lead.get("id", lid)] = dict(lead)
        _save_map(paths["leads_archive"], leads_archive)

        # Create a *pending* client record.
        # Business rule: client becomes visible in the Clients tab only after at least one job is completed.
        cid = _next_id("C", clients)
        clients[cid] = {
            "id": cid,
            "created_at": now_iso(),
            "status": "pending",
            "visible": False,
            "source_lead_id": lead.get("id", lid),
            "name": lead.get("name",""),
            "phone": lead.get("phone",""),
            "email": lead.get("email",""),
            "company_name": lead.get("company_name",""),
            "industry": lead.get("industry",""),
            "website": lead.get("website",""),
            "address": lead.get("to_address",""),
            "tags": ["converted_pending"],
            "notes": lead.get("notes",""),
        }

        # Create first job (minimal, can be refined later)
        move_date = payload.get("move_date") or lead.get("move_date","")
        time_str = payload.get("time") or "09:00"
        truck_id = payload.get("truck_id") or ""
        emp_ids = payload.get("employee_ids") or []
        if isinstance(emp_ids, str):
            emp_ids = [x.strip() for x in emp_ids.split(",") if x.strip()]

        jid = _next_id("J", jobs)
        jobs[jid] = {
            "id": jid,
            "client_id": cid,
            "created_at": now_iso(),
            "status": "scheduled",
            "from_address": lead.get("from_address",""),
            "to_address": lead.get("to_address",""),
            "move_date": move_date,
            "property_type": lead.get("property_type",""),
            "property_size": lead.get("property_size",""),
            "price": float(payload.get("price") or lead.get("value_est") or 0),
            "truck_id": truck_id,
            "employee_ids": emp_ids,
            "notes": "Auto-created from lead conversion.",
        }

        # Calendar event (only if a date exists)
        if move_date:
            eid = _next_id("E", cal)
            cal[eid] = {
                "id": eid,
                "type": "job",
                "title": f"Move: {clients[cid]['name']}",
                "date": move_date,
                "time": time_str,
                "related": {"client_id": cid, "job_id": jid},
            }
            save_all()
            return True, f"pending client {cid}, job {jid}, event {eid}"

        save_all()
        return True, f"pending client {cid}, job {jid}"
    if cmd == "create_client":
        cid = _next_id("C", clients)
        clients[cid] = {
            "id": cid,
            "created_at": now_iso(),
            "name": payload.get("name",""),
            "phone": payload.get("phone",""),
            "email": payload.get("email",""),
            "address": payload.get("address",""),
            "tags": payload.get("tags") or [],
            "notes": payload.get("notes",""),
        }
        save_all()
        return True, f"created {cid}"

    if cmd == "create_job":
        cid = payload.get("client_id")
        if cid not in clients:
            return False, "client not found"
        jid = _next_id("J", jobs)
        jobs[jid] = {
            "id": jid,
            "client_id": cid,
            "created_at": now_iso(),
            "status": payload.get("status","scheduled"),
            "from_address": payload.get("from_address",""),
            "to_address": payload.get("to_address",""),
            "move_date": payload.get("move_date",""),
            "price": float(payload.get("price") or 0),
            "truck_id": payload.get("truck_id") or "",
            "employee_ids": payload.get("employee_ids") or [],
            "notes": payload.get("notes",""),
        }
        # optional calendar
        if payload.get("add_to_calendar") == "1":
            eid = _next_id("E", cal)
            cal[eid] = {
                "id": eid,
                "type": "job",
                "title": f"Job: {clients[cid]['name']}",
                "date": payload.get("move_date",""),
                "time": payload.get("time","09:00"),
                "related": {"client_id": cid, "job_id": jid},
            }
        save_all()
        return True, f"created {jid}"

    if cmd == "create_event":
        eid = _next_id("E", cal)
        cal[eid] = {
            "id": eid,
            "type": payload.get("type","followup"),
            "title": payload.get("title",""),
            "date": payload.get("date",""),
            "time": payload.get("time",""),
            "related": payload.get("related") or {},
        }
        save_all()
        return True, f"created {eid}"

    if cmd == "delete_event":
        eid = payload.get("id")
        if eid in cal:
            cal.pop(eid)
            save_all()
            return True, f"deleted {eid}"
        return False, "not found"

    if cmd == "create_truck":
        tid = _next_id("T", trucks)
        trucks[tid] = {
            "id": tid,
            "created_at": now_iso(),
            "make": payload.get("make",""),
            "model": payload.get("model",""),
            "year": payload.get("year",""),
            "consumption_per_100km": float(payload.get("consumption_per_100km") or 0),
            "gas_price_assumption": float(payload.get("gas_price_assumption") or 2.0),
            "insurance_payment_date": payload.get("insurance_payment_date",""),
            "loan_payment_date": payload.get("loan_payment_date",""),
            "notes": payload.get("notes",""),
        }
        save_all()
        return True, f"created {tid}"

    if cmd == "create_employee":
        eid = _next_id("EMP", employees)
        # Create a new employee record. Allow optional username/password for sub-tenant logins.
        employees[eid] = {
            "id": eid,
            "created_at": now_iso(),
            "name": payload.get("name", ""),
            "role": payload.get("role", "mover"),
            "phone": payload.get("phone", ""),
            "email": payload.get("email", ""),
            "hourly_rate": float(payload.get("hourly_rate") or 0),
            "notes": payload.get("notes", ""),
            # Store username/password as provided; password may be hashed externally
            "username": payload.get("username", ""),
            "password": payload.get("password", ""),
        }
        save_all()
        return True, f"created {eid}"

    if cmd == "delete_truck":
        tid = payload.get("id")
        if tid in trucks:
            trucks.pop(tid)
            save_all()
            return True, f"deleted {tid}"
        return False, "not found"

    if cmd == "delete_employee":
        eid = payload.get("id")
        if eid in employees:
            employees.pop(eid)
            save_all()
            return True, f"deleted {eid}"
        return False, "not found"

    if cmd == "update_employee":
        # Update an existing employee's details and credentials. Expect 'employee_id' field.
        eid = (payload.get("employee_id") or payload.get("id") or "").strip()
        if not eid or eid not in employees:
            return False, "employee not found"
        emp = employees[eid]
        # Update scalar fields if provided and non-empty
        for key in ("name", "role", "phone", "email", "hourly_rate", "notes", "username"):
            if key in payload and payload.get(key) != "":
                if key == "hourly_rate":
                    try:
                        emp[key] = float(payload.get(key))
                    except Exception:
                        pass
                else:
                    emp[key] = payload.get(key)
        # Only update password when provided and non-empty
        if payload.get("password"):
            emp["password"] = payload.get("password")
        save_all()
        return True, f"updated {eid}"

    if cmd == "pnl_add_entry":
        entry = {
            "id": uuid.uuid4().hex[:10],
            "created_at": now_iso(),
            "date": payload.get("date",""),
            "type": payload.get("type","revenue"),
            "amount": float(payload.get("amount") or 0),
            "note": payload.get("note",""),
            "truck_id": payload.get("truck_id") or "",
            "employee_id": payload.get("employee_id") or "",
        }
        if not isinstance(pnl, dict):
            pnl = {"entries": []}
        pnl.setdefault("entries", []).append(entry)
        write_json(paths["pnl"], pnl)
        return True, f"added pnl {entry['id']}"

    if cmd == "upload_doc":
        # payload: entity_type, entity_id, filename, rel_path (already stored file)
        return True, "doc registered"

    if cmd == "update_lead_status":
        lid = payload.get("id") or payload.get("lead_id")
        status = (payload.get("status") or "").strip()
        if not lid or lid not in leads:
            return False, "lead not found"
        if not status:
            return False, "missing status"
        leads[lid]["status"] = status
        leads[lid]["updated_at"] = now_iso()
        save_all()
        return True, f"lead {lid} status -> {status}"

    if cmd == "update_client_status":
        cid = payload.get("id") or payload.get("client_id")
        status = (payload.get("status") or "").strip()
        if not cid or cid not in clients:
            return False, "client not found"
        if not status:
            return False, "missing status"
        clients[cid]["status"] = status
        clients[cid]["updated_at"] = now_iso()
        save_all()
        return True, f"client {cid} status -> {status}"

    if cmd == "send_message":
        channel = (payload.get("channel") or "").strip().lower()
        if channel not in ("email","sms"):
            return False, "invalid channel"
        cid = (payload.get("client_id") or "").strip()
        if not cid:
            return False, "missing client_id"
        direction = (payload.get("direction") or "out").strip().lower()
        if direction not in ("out","in"):
            direction = "out"
        msg = {
            "id": payload.get("id") or _next_id("M", {}),
            "ts": now_iso(),
            "direction": direction,
            "client_id": cid,
            "lead_id": (payload.get("lead_id") or "").strip() or None,
            "subject": (payload.get("subject") or "").strip(),
            "body": (payload.get("body") or "").strip(),
        }
        pkey = "messages_email" if channel=="email" else "messages_sms"
        mdb = read_json(paths.get(pkey), {"threads": {}})
        if not isinstance(mdb, dict):
            mdb = {"threads": {}}
        threads = mdb.get("threads") or {}
        thread = threads.get(cid) or []
        thread.append(msg)
        threads[cid] = thread
        mdb["threads"] = threads
        write_json(paths.get(pkey), mdb)
        return True, f"sent {channel}"

    if cmd == "create_quote":
        cid = (payload.get("client_id") or "").strip()
        if not cid:
            return False, "missing client_id"
        qdb = read_json(paths.get("quotes"), {})
        if not isinstance(qdb, dict):
            qdb = {}
        qid = payload.get("id") or f"Q{str(uuid.uuid4())[:8]}"

        # Determine if this quote uses a predefined package.  Packages correspond to
        # subscription models for agency tenants (e.g. annual subscription or onboarding).
        pkg = (payload.get("package") or "").strip().lower()
        def fnum(x, d=0.0):
            try:
                return float(x)
            except:
                return d

        if pkg:
            # Predefined packages: 'annual' or 'onboarding'.  Each package has
            # fixed hours and base price.  Tax is applied at the standard rate
            # unless overridden via payload['tax_rate'].
            packages = {
                "annual": {"hours": 8736.0, "subtotal": 981.0},
                "onboarding": {"hours": 0.45, "subtotal": 99.0},
            }
            if pkg not in packages:
                return False, "unknown package"
            hours = packages[pkg]["hours"]
            subtotal = packages[pkg]["subtotal"]
            rate = 0.0
            movers = fnum(payload.get("movers"))
            extras = fnum(payload.get("extras"))
            # Extras can still be added to a package
            subtotal = subtotal + extras
            tax_rate = fnum(payload.get("tax_rate"), 0.14975)
            tax = subtotal * tax_rate
            total = subtotal + tax
            quote = {
                "id": qid,
                "created_at": now_iso(),
                "client_id": cid,
                "lead_id": (payload.get("lead_id") or "").strip() or None,
                "status": payload.get("status") or "draft",
                "package": pkg,
                "hours": hours,
                "rate": rate,
                "movers": movers,
                "extras": extras,
                "subtotal": round(subtotal, 2),
                "tax_rate": tax_rate,
                "tax": round(tax, 2),
                "total": round(total, 2),
                "notes": (payload.get("notes") or "").strip(),
            }
        else:
            # Compute totals based on number of movers and hourly rate.  The
            # previous formula included truck and per-km fees; those are now
            # optional.  If 'movers' is provided, multiply hours * rate * movers.
            hours = fnum(payload.get("hours"))
            rate = fnum(payload.get("rate"))
            movers = fnum(payload.get("movers")) or 1.0
            truck_fee = fnum(payload.get("truck_fee"))
            km = fnum(payload.get("km"))
            km_rate = fnum(payload.get("km_rate"))
            extras = fnum(payload.get("extras"))
            if payload.get("movers") is not None:
                subtotal = hours * rate * movers + extras
            else:
                subtotal = hours * rate + truck_fee + km * km_rate + extras
            tax_rate = fnum(payload.get("tax_rate"), 0.14975)
            tax = subtotal * tax_rate
            total = subtotal + tax
            quote = {
                "id": qid,
                "created_at": now_iso(),
                "client_id": cid,
                "lead_id": (payload.get("lead_id") or "").strip() or None,
                "status": payload.get("status") or "draft",
                "hours": hours,
                "rate": rate,
                "movers": movers,
                "truck_fee": truck_fee,
                "km": km,
                "km_rate": km_rate,
                "extras": extras,
                "subtotal": round(subtotal, 2),
                "tax_rate": tax_rate,
                "tax": round(tax, 2),
                "total": round(total, 2),
                "notes": (payload.get("notes") or "").strip(),
            }
        qdb[qid] = quote
        write_json(paths.get("quotes"), qdb)
        return True, f"created quote {qid}"

    if cmd == "accept_quote":
        qid = (payload.get("quote_id") or payload.get("id") or "").strip()
        qdb = read_json(paths.get("quotes"), {})
        if not isinstance(qdb, dict) or qid not in qdb:
            return False, "quote not found"
        qdb[qid]["status"] = "accepted"
        qdb[qid]["accepted_at"] = now_iso()
        write_json(paths.get("quotes"), qdb)
        return True, f"accepted {qid}"

    if cmd == "create_invoice":
        qid = (payload.get("quote_id") or "").strip()
        invdb = read_json(paths.get("invoices"), {})
        if not isinstance(invdb, dict):
            invdb = {}
        qdb = read_json(paths.get("quotes"), {})
        quote = qdb.get(qid) if isinstance(qdb, dict) else None
        if not quote:
            return False, "quote not found"
        iid = payload.get("id") or f"I{str(uuid.uuid4())[:8]}"
        deposit = 0.0
        try: deposit = float(payload.get("deposit") or 0.0)
        except: deposit = 0.0
        total = float(quote.get("total") or 0.0)
        balance = max(0.0, total - deposit)
        inv = {
            "id": iid,
            "created_at": now_iso(),
            "client_id": quote.get("client_id"),
            "quote_id": qid,
            "status": payload.get("status") or "unpaid",
            "total": round(total,2),
            "deposit": round(deposit,2),
            "balance": round(balance,2),
            "notes": (payload.get("notes") or "").strip(),
        }
        invdb[iid] = inv
        write_json(paths.get("invoices"), invdb)
        return True, f"created invoice {iid}"

    if cmd == "mark_invoice_paid":
        iid = (payload.get("invoice_id") or payload.get("id") or "").strip()
        invdb = read_json(paths.get("invoices"), {})
        if not isinstance(invdb, dict) or iid not in invdb:
            return False, "invoice not found"
        invdb[iid]["status"] = "paid"
        invdb[iid]["paid_at"] = now_iso()
        write_json(paths.get("invoices"), invdb)
        return True, f"paid {iid}"



    if cmd == "archive_lead":
        # Mark a lead as archived without converting it into a client.  Do not remove
        # the lead from the active leads list; simply update its status.  A record
        # of the archive timestamp is stored on the lead for auditing.  This
        # ensures the archived lead is not mistakenly promoted into the clients
        # list and avoids the bug where archived leads appear as clients.
        lid = payload.get("id") or payload.get("lead_id")
        if not lid or lid not in leads:
            return False, "lead not found"
        lead = leads[lid]
        # Update status to archived; preserve existing status in tags if present.
        lead["status"] = "archived"
        lead["archived_at"] = now_iso()
        lead["updated_at"] = now_iso()
        # Persist the updated lead back to leads.json
        save_all()
        return True, f"archived {lid}"

    if cmd == "update_truck":
        """Update an existing truck's details.  Expects 'truck_id' or 'id'."""
        tid_update = (payload.get("truck_id") or payload.get("id") or "").strip()
        if not tid_update or tid_update not in trucks:
            return False, "truck not found"
        trk = trucks[tid_update]
        # Update fields if provided. Empty strings are ignored.
        for key in ("make", "model", "year", "consumption_per_100km", "gas_price_assumption",
                    "insurance_payment_date", "loan_payment_date", "notes"):
            if key in payload and str(payload.get(key)) != "":
                val = payload.get(key)
                # Cast numeric fields
                if key in ("consumption_per_100km", "gas_price_assumption"):
                    try:
                        trk[key] = float(val)
                    except:
                        continue
                else:
                    trk[key] = val
        trk["updated_at"] = now_iso()
        save_all()
        return True, f"updated {tid_update}"

    if cmd == "cancel_job":
        """Cancel a job and mark its status as canceled."""
        jid = (payload.get("job_id") or payload.get("id") or "").strip()
        if not jid or jid not in jobs:
            return False, "job not found"
        jobs[jid]["status"] = "canceled"
        jobs[jid]["canceled_at"] = now_iso()
        # Remove any calendar event related to this job
        for eid, ev in list(cal.items()):
            rel = ev.get("related") or {}
            if rel.get("job_id") == jid:
                cal.pop(eid, None)
                break
        save_all()
        return True, f"canceled {jid}"

    if cmd == "reschedule_job":
        """Reschedule a job by updating its move date/time.  Optionally updates the related calendar event."""
        jid = (payload.get("job_id") or payload.get("id") or "").strip()
        if not jid or jid not in jobs:
            return False, "job not found"
        new_date = payload.get("move_date") or ""
        new_time = payload.get("time") or ""
        if new_date:
            jobs[jid]["move_date"] = new_date
        if new_time:
            jobs[jid]["time"] = new_time
        jobs[jid]["updated_at"] = now_iso()
        # Update existing calendar event if present
        for eid, ev in cal.items():
            rel = ev.get("related") or {}
            if rel.get("job_id") == jid:
                if new_date:
                    ev["date"] = new_date
                if new_time:
                    ev["time"] = new_time
                cal[eid] = ev
                break
        save_all()
        return True, f"rescheduled {jid}"

    if cmd == "complete_job":
        jid = payload.get("id")
        if jid not in jobs:
            return False, "job not found"
        jobs[jid]["status"] = "completed"
        jobs[jid]["completed_at"] = now_iso()

        # Promotion rule: once a job is completed, the related client becomes visible/active.
        cid = jobs[jid].get("client_id")
        if cid and cid in clients:
            clients[cid]["status"] = "active"
            clients[cid]["visible"] = True
            clients[cid]["activated_at"] = now_iso()
            tags = clients[cid].get("tags") or []
            if "active" not in tags:
                tags.append("active")
            clients[cid]["tags"] = tags

        # Mark related calendar event completed (if any)
        try:
            ev_id = str(jobs[jid].get('event_id') or '')
            if ev_id and ev_id in cal:
                cal[ev_id]['completed'] = True
                cal[ev_id]['completed_at'] = now_iso()
        except Exception:
            pass

        save_all()
        return True, f"completed {jid}"
    return False, f"unknown cmd {cmd}"
