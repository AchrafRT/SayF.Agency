#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SayF.Agency Deterministic Multi-Tenant CRM (Zero-Dependency)
- Standard library only (http.server)
- File-tree JSON storage
- Command bus (writes commands) + worker (processes immediately for deterministic UX)
"""

from __future__ import annotations
import html
import json
import mimetypes
import os
import sys
import time
import datetime
import calendar
import urllib.parse
import hashlib
import urllib.request
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Any, Optional, Tuple, List

from core.utils import read_json, write_json, safe_join, render_template, now_iso, ensure_dir, clamp
from core.auth import hash_password, verify_password, new_session, get_session, delete_session, find_user, upsert_user
from core.command_bus import write_command
from core.worker import process_command_file, tenant_paths

# -------------------------
# Simple i18n (EN/FR)
# Keep vocabulary very easy (<= 3rd grade)
# -------------------------
TRANSLATIONS = {
    "en": {
        "nav.notifications": "Notifications",
        "nav.messages": "Messages",
        "nav.leads": "Leads",
        "nav.calendar": "Calendar",
        "nav.clients": "Clients",
        "nav.employees": "Employees",
        "nav.pnl": "Profit / Loss",
        "nav.settings": "Settings",
        "nav.assets": "Trucks & Team",
        "btn.open": "Open",
        "btn.details": "Details",
        "btn.new": "New",
        "btn.save": "Save",
        "btn.update": "Update",
        "btn.close": "Close",
        "btn.delete": "Delete",
        "btn.archive": "Archive",
        "btn.complete": "Done",
        "btn.cancel": "Cancel",
        "btn.reschedule": "Reschedule",
        "btn.unarchive": "Unarchive",
        "status.archived": "archived",
        "title.inbox": "Inbox",
        "title.todo": "To-Do",
        "title.leads": "Leads",
        "title.clients": "Clients",
        "title.calendar": "Calendar",
        "title.settings": "Settings",
        "msg.none": "None.",
        "label.status": "Status",
        "label.notes": "Notes",
        "label.phone": "Phone",
        "label.email": "Email",
        "label.address": "Address",
        "label.from": "From",
        "label.to": "To",
    },
    "fr": {
        "nav.notifications": "Alertes",
        "nav.messages": "Messages",
        "nav.leads": "Prospects",
        "nav.calendar": "Calendrier",
        "nav.clients": "Clients",
        "nav.employees": "Équipe",
        "nav.pnl": "Argent",
        "nav.settings": "Réglages",
        "nav.assets": "Camions & Équipe",
        "btn.open": "Ouvrir",
        "btn.details": "Détails",
        "btn.new": "Nouveau",
        "btn.save": "Sauver",
        "btn.update": "Mettre",
        "btn.close": "Fermer",
        "btn.delete": "Supprimer",
        "btn.archive": "Archiver",
        "btn.complete": "Fait",
        "btn.cancel": "Annuler",
        "btn.reschedule": "Replanifier",
        "btn.unarchive": "Restaurer",
        "status.archived": "archivé",
        "title.inbox": "Boîte",
        "title.todo": "À faire",
        "title.leads": "Prospects",
        "title.clients": "Clients",
        "title.calendar": "Calendrier",
        "title.settings": "Réglages",
        "msg.none": "Aucun.",
        "label.status": "État",
        "label.notes": "Notes",
        "label.phone": "Téléphone",
        "label.email": "Courriel",
        "label.address": "Adresse",
        "label.from": "Départ",
        "label.to": "Arrivée",
    }
}

def _tr(lang: str, key: str, fallback: str = "") -> str:
    lang = (lang or "en").lower()
    if lang not in ("en", "fr"):
        lang = "en"
    return (TRANSLATIONS.get(lang, {}).get(key)
            or TRANSLATIONS.get("en", {}).get(key)
            or fallback
            or key)

APP_NAME = "SayF.Agency CRM (Local MVP)"
ROOT = os.path.abspath(os.path.dirname(__file__))
DATA = os.path.join(ROOT, "data")
PUBLIC = os.path.join(ROOT, "public")
PORTALS = os.path.join(ROOT, "portals")
STATIC = os.path.join(ROOT, "static")
USERS_PATH = os.path.join(DATA, "users.json")
SESSIONS_PATH = os.path.join(DATA, "sessions.json")
TENANTS_DIR = os.path.join(DATA, "tenants")

def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

def _render_file(path: str, ctx: Dict[str, Any]) -> str:
    # Render an on-disk HTML template by filling {{keys}} from ctx.
    return render_template(_read_text(path), ctx)


# -----------------------------------------------------------------------------
# Helper to fetch the category of a tenant.  Reads from data/tenants.json and
# returns the category string for the given tenant_id.  Defaults to an empty
# string if unknown.
def _tenant_category(tid: str) -> str:
    try:
        tenants = read_json(os.path.join(DATA, "tenants.json"), [])
        if isinstance(tenants, list):
            for t in tenants:
                if str(t.get("tenant_id")) == str(tid):
                    return str(t.get("category", ""))
    except Exception:
        pass
    return ""

def _split_host(host: str) -> Tuple[str, str]:
    host = (host or "").strip()
    if ":" in host:
        h, p = host.rsplit(":", 1)
        return h.lower(), p
    return host.lower(), ""

def _is_local_host(hostname: str) -> bool:
    hn = (hostname or "").lower()
    return hn in ("localhost", "127.0.0.1", "0.0.0.0") or hn.endswith(".localhost")

def _strip_known_subdomain(hostname: str) -> str:
    hn = (hostname or "").lower()
    for pfx in ("admin.", "portal.", "client.", "app."):
        if hn.startswith(pfx):
            return hn[len(pfx):]
    return hn

def _admin_host_for(hostname: str) -> str:
    return "admin." + _strip_known_subdomain(hostname)

def _portal_host_for(hostname: str) -> str:
    return "portal." + _strip_known_subdomain(hostname)

def _boot_seed() -> None:
    ensure_dir(DATA)
    ensure_dir(TENANTS_DIR)
    ensure_dir(STATIC)

    # users
    udb = read_json(USERS_PATH, {"users": []})
    if not udb.get("users"):
        # Admin: admin/admin
        upsert_user(USERS_PATH, {
            "username": "admin",
            "password_hash": hash_password("admin"),
            "role": "owner",
            "tenant_id": None,
            "display_name": "SayF.Agency Admin"
        })
        # Client: DemenageursPlus / admin
        upsert_user(USERS_PATH, {
            "username": "DemenageursPlus",
            "password_hash": hash_password("admin"),
            "role": "client_owner",
            "tenant_id": "T001",
            "display_name": "DemenageursPlus Owner"
        })
        # Client: SayFAgency / admin (agency tenant portal)
        upsert_user(USERS_PATH, {
            "username": "SayFAgency",
            "password_hash": hash_password("admin"),
            "role": "client_owner",
            "tenant_id": "T000",
            "display_name": "SayF.Agency (Tenant Portal)"
        })
        # Client: PlomberieNova / admin
        upsert_user(USERS_PATH, {
            "username": "PlomberieNova",
            "password_hash": hash_password("admin"),
            "role": "client_owner",
            "tenant_id": "T002",
            "display_name": "PlomberieNova Owner"
        })


    # sessions
    if not os.path.exists(SESSIONS_PATH):
        write_json(SESSIONS_PATH, {})

    # tenants (demo datasets)
    # T000: SayF.Agency (agency flow; NO moving trucks/jobs)
    t000 = os.path.join(TENANTS_DIR, "T000")
    if not os.path.exists(t000):
        _create_tenant_demo("T000", "SayF.Agency", category="agency")
    else:
        _ensure_tenant_files(t000)

    # T001: Moving company tenant
    t001 = os.path.join(TENANTS_DIR, "T001")
    if not os.path.exists(t001):
        _create_tenant_demo("T001", "DemenageursPlus", category="moving_company")
    else:
        _ensure_tenant_files(t001)

    # T002: Another tenant (non-moving) for admin demo (optional)
    t002 = os.path.join(TENANTS_DIR, "T002")
    if not os.path.exists(t002):
        _create_tenant_demo("T002", "PlomberieNova", category="service_company")
    else:
        _ensure_tenant_files(t002)

def _create_tenant_demo(tid: str, name: str, category: str) -> None:
    tdir = os.path.join(TENANTS_DIR, tid)
    ensure_dir(tdir)
    ensure_dir(os.path.join(tdir, "inbox"))
    ensure_dir(os.path.join(tdir, "processed"))
    ensure_dir(os.path.join(tdir, "docs"))
    ensure_dir(os.path.join(tdir, "logs"))
    ensure_dir(os.path.join(tdir, "tmp"))

    write_json(os.path.join(tdir, "meta.json"), {
        "tenant_id": tid,
        "name": name,
        "category": category,
        "created_at": now_iso(),
    })

    # Demo data
    leads = {
        "L0001": {
            "id": "L0001",
            "created_at": now_iso(),
            "status": "new",
            "name": "Marie Tremblay",
            "phone": "514-555-0188",
            "email": "marie@example.com",
            "from_address": "Plateau-Mont-Royal, Montreal",
            "to_address": "Laval (Chomedey)",
            "move_date": time.strftime("%Y-%m-%d"),
            "notes": "Needs packing help. Elevator available. Wants quote today.",
            "source": "website",
            "value_est": 1299.0,
        },
        "L0002": {
            "id": "L0002",
            "created_at": now_iso(),
            "status": "followup",
            "name": "Jean Dupont",
            "phone": "438-555-0142",
            "email": "jean@example.com",
            "from_address": "Longueuil",
            "to_address": "Brossard",
            "move_date": time.strftime("%Y-%m-%d"),
            "notes": "Small move (studio). Price-sensitive.",
            "source": "email",
            "value_est": 499.0,
        }
    }
    clients = {
        "C0001": {
            "id": "C0001",
            "created_at": now_iso(),
            "name": "Sophie Nguyen",
            "phone": "514-555-0111",
            "email": "sophie@example.com",
            "address": "Rosemont, Montreal",
            "tags": ["vip"],
            "notes": "Prefers SMS. Repeat customer.",
        }
    }
    jobs = {
        "J0001": {
            "id": "J0001",
            "client_id": "C0001",
            "created_at": now_iso(),
            "status": "scheduled",
            "from_address": "Rosemont, Montreal",
            "to_address": "Ahuntsic, Montreal",
            "move_date": time.strftime("%Y-%m-%d"),
            "price": 1450.0,
            "truck_id": "T0001",
            "employee_ids": ["EMP0001","EMP0002"],
            "notes": "2 bedrooms. Disassembly bed + couch."
        }
    }
    calendar = {
        "E0001": {"id":"E0001","type":"call","title":"Call lead: Jean Dupont","date":time.strftime("%Y-%m-%d"),"time":"10:30","related":{"lead_id":"L0002"}},
        "E0002": {"id":"E0002","type":"job","title":"Move: Sophie Nguyen","date":time.strftime("%Y-%m-%d"),"time":"13:00","related":{"client_id":"C0001","job_id":"J0001"}},
        "E0003": {"id":"E0003","type":"followup","title":"Follow-up: Marie Tremblay quote","date":time.strftime("%Y-%m-%d"),"time":"16:30","related":{"lead_id":"L0001"}},
    }
    trucks = {
        "T0001": {
            "id":"T0001",
            "created_at": now_iso(),
            "make":"Hino",
            "model":"20ft Cube Truck",
            "year":"2010",
            "consumption_per_100km": 22.0,
            "gas_price_assumption": 2.0,
            "insurance_payment_date": time.strftime("%Y-%m-") + "25",
            "loan_payment_date": time.strftime("%Y-%m-") + "05",
            "notes":"Main workhorse. Keep tires checked.",
        }
    }
    employees = {
        "EMP0001": {"id":"EMP0001","created_at":now_iso(),"name":"Alex","role":"driver","phone":"514-555-0123","email":"alex@dplus.ca","hourly_rate":28.0,"notes":"Has Class 3."},
        "EMP0002": {"id":"EMP0002","created_at":now_iso(),"name":"Karim","role":"mover","phone":"514-555-0456","email":"karim@dplus.ca","hourly_rate":22.0,"notes":"Fast + careful."},
    }
    pnl = {
        "entries": [
            {"id":"p1","created_at":now_iso(),"date":time.strftime("%Y-%m-%d"),"type":"revenue","amount":1450.0,"note":"Deposit + balance (Sophie job)","truck_id":"T0001","employee_id":""},
            {"id":"p2","created_at":now_iso(),"date":time.strftime("%Y-%m-%d"),"type":"expense","amount":210.0,"note":"Gas + tolls","truck_id":"T0001","employee_id":""},
            {"id":"p3","created_at":now_iso(),"date":time.strftime("%Y-%m-%d"),"type":"expense","amount":320.0,"note":"Labor (Alex+Karim)","truck_id":"","employee_id":"EMP0001"},
        ]
    }

    
    # If this is the agency tenant, use an agency-specific demo dataset (no moving trucks/jobs).
    if category == "agency":
        leads = {
            "L0001": {"id":"L0001","created_at":now_iso(),"status":"new","name":"Julien","phone":"514-555-0101","email":"julien@business.ca","company_name":"Global Service Résidentiel","industry":"Home Services","website":"https://example.com","notes":"Wants $981/year software + onboarding.","source":"kijiji","value_est":981.0},
            "L0002": {"id":"L0002","created_at":now_iso(),"status":"booking_pending","name":"Raphael","phone":"438-555-0102","email":"raphael@business.ca","company_name":"Jeux Gonflable","industry":"Events","website":"https://example.com","notes":"Interested in $99 consultation.","source":"referral","value_est":99.0},
        }
        clients = {
            "C0001": {"id":"C0001","created_at":now_iso(),"status":"active","visible":True,"name":"Jayson","phone":"514-555-0111","email":"jayson@business.ca","company_name":"Ultratek","industry":"Tech","website":"https://example.com","address":"Montreal","plan":"software_yearly","price_yearly":981.0,"renewal_date":(datetime.date.today()+datetime.timedelta(days=335)).isoformat(),"notes":"Onboarded. Renewal scheduled."}
        }
        jobs = {
            "J0001": {"id":"J0001","client_id":"C0001","created_at":now_iso(),"status":"completed","type":"onboarding","title":"Onboarding + Portal Setup","move_date":time.strftime("%Y-%m-%d"),"price":981.0,"event_id":"EV0001"}
        }
        calendar = {
            "EV0001": {"id":"EV0001","type":"setup","title":"Onboarding (Ultratek)","date":time.strftime("%Y-%m-%d"),"time":"10:00","notes":"Collect payment, provision access.","completed":True,"completed_at":now_iso(),"related":{"client_id":"C0001","job_id":"J0001"}},
            "EV0002": {"id":"EV0002","type":"renewal_reminder","title":"Renewal Reminder (Ultratek)","date":(datetime.date.today()+datetime.timedelta(days=314)).isoformat(),"time":"09:00","notes":"Send 3-week renewal reminder.","completed":False,"related":{"client_id":"C0001"}},
        }
        trucks = {}
        employees = {
            "EMP0001": {"id":"EMP0001","created_at":now_iso(),"status":"active","name":"Nadia","role":"sales","phone":"514-555-0201","email":"nadia@sayf.agency","hourly_rate":0.0,"notes":"Handles lead follow-ups + bookings."},
            "EMP0002": {"id":"EMP0002","created_at":now_iso(),"status":"active","name":"Omar","role":"support","phone":"514-555-0202","email":"omar@sayf.agency","hourly_rate":0.0,"notes":"Client onboarding + renewals."},
        }
        pnl = {"entries":[
            {"id":"p1","created_at":now_iso(),"date":time.strftime("%Y-%m-%d"),"type":"revenue","amount":99.0,"note":"Consultation booking (Raphael)","truck_id":"","employee_id":""},
            {"id":"p2","created_at":now_iso(),"date":time.strftime("%Y-%m-%d"),"type":"revenue","amount":981.0,"note":"Yearly software (Ultratek)","truck_id":"","employee_id":""},
        ]}
    write_json(os.path.join(tdir, "leads.json"), leads)
    write_json(os.path.join(tdir, "clients.json"), clients)
    write_json(os.path.join(tdir, "jobs.json"), jobs)
    write_json(os.path.join(tdir, "calendar.json"), calendar)
    write_json(os.path.join(tdir, "trucks.json"), trucks)
    write_json(os.path.join(tdir, "employees.json"), employees)
    write_json(os.path.join(tdir, "pnl.json"), pnl)

    # messaging / quotes / invoices / documents / templates
    write_json(os.path.join(tdir, "messages_email.json"), {"threads": {
        "C0001": [
            {"id":"ME1","ts":now_iso(),"direction":"in","client_id":"C0001","subject":"Question about pricing","body":"Hi, can you confirm the quote includes packing?"},
            {"id":"ME2","ts":now_iso(),"direction":"out","client_id":"C0001","subject":"Re: Question about pricing","body":"Yes — packing is included as an optional line item. We can adjust based on your needs."},
        ]
    }})
    write_json(os.path.join(tdir, "messages_sms.json"), {"threads": {
        "C0001": [
            {"id":"MS1","ts":now_iso(),"direction":"out","client_id":"C0001","subject":"","body":"Hi Sophie — your quote is ready. Want me to send the booking link?"},
            {"id":"MS2","ts":now_iso(),"direction":"in","client_id":"C0001","subject":"","body":"Yes please 👍"},
        ]
    }})
    # Demo quotes / invoices / docs
    meta = read_json(os.path.join(tdir, "meta.json"), {})
    cat = (meta.get("category") or "").strip()
    if cat == "moving_company":
        q_demo = {
            "Q0001": {
                "id": "Q0001",
                "client_id": "C0001",
                "created_at": now_iso(),
                "status": "sent",
                "items": [
                    {"label": "Move service", "qty": 1, "unit": 1299.0},
                    {"label": "Deposit", "qty": 1, "unit": -299.0},
                ],
                "total": 1000.0,
                "note": "Demo quote",
            }
        }
        i_demo = {
            "I0001": {
                "id": "I0001",
                "client_id": "C0001",
                "created_at": now_iso(),
                "status": "unpaid",
                "total": 1299.0,
                "deposit": 299.0,
                "balance": 1000.0,
                "note": "Balance due",
            }
        }
    else:
        q_demo = {
            "Q0001": {
                "id": "Q0001",
                "client_id": "C0001",
                "created_at": now_iso(),
                "status": "sent",
                "items": [
                    {"label": "Year plan", "qty": 1, "unit": 981.0},
                    {"label": "Cards + Flyers", "qty": 1, "unit": 0.0},
                ],
                "total": 981.0,
                "note": "Year plan + onboarding",
            }
        }
        i_demo = {
            "I0001": {
                "id": "I0001",
                "client_id": "C0001",
                "created_at": now_iso(),
                "status": "paid",
                "total": 981.0,
                "deposit": 0.0,
                "balance": 0.0,
                "note": "Year plan paid",
            }
        }
    write_json(os.path.join(tdir, "quotes.json"), q_demo)
    write_json(os.path.join(tdir, "invoices.json"), i_demo)
    docs_dir = os.path.join(tdir, "docs")
    os.makedirs(docs_dir, exist_ok=True)
    sample_doc_rel = "welcome.txt"
    sample_doc_abs = os.path.join(docs_dir, sample_doc_rel)
    if not os.path.exists(sample_doc_abs):
        with open(sample_doc_abs, "w", encoding="utf-8") as f:
            f.write("Welcome!\n\nThis is a demo document.\n")
    write_json(os.path.join(tdir, "docs_meta.json"), {"docs": [
        {"id": "D0001", "client_id": "C0001", "job_id": "J0001", "name": "Welcome", "path": sample_doc_rel, "created_at": now_iso()}
    ]})
    # Default templates (can be customized later)
    write_json(os.path.join(tdir, "templates.json"), {"templates": {
        "quote_email": "Hello {{client_name}},\n\nHere is your quote total: ${{quote_total}}.\n\nThanks,\n{{company_name}}",
        "quote_sms_notify": "Hi {{client_name}} — your quote is ready: ${{quote_total}}. Reply YES for booking link.",
        "booking_sms": "Booking link: {{booking_link}} (deposit ${{deposit}}).",
        "booking_confirm_email": "Confirmed! Your booking is scheduled for {{job_date}} {{job_time}}.\n\n{{company_name}}",
        "invoice_sms": "Invoice: Total ${{invoice_total}}. Deposit ${{deposit}}. Balance ${{balance}}. Pay: {{payment_link}}",
        "invoice_email": "Hello {{client_name}},\n\nInvoice summary:\nTotal: ${{invoice_total}}\nDeposit: ${{deposit}}\nBalance: ${{balance}}\n\nPay here: {{payment_link}}\n\n{{company_name}}",
        "tenant_onboarding_email": "Welcome {{tenant_name}}!\n\nPortal: {{portal_link}}\nLogin: {{tenant_username}}\nPassword: {{tenant_password}}\n\nEmbed this form on your site:\n{{embed_code}}\n\n— SayF.Agency",
        "tenant_onboarding_sms": "Your portal is ready: {{portal_link}} (user: {{tenant_username}} / pass: {{tenant_password}})."
    }})


def _get_cookie(handler: BaseHTTPRequestHandler, name: str) -> str:
    hdr = handler.headers.get("Cookie","")
    c = cookies.SimpleCookie()
    c.load(hdr)
    if name in c:
        return c[name].value
    return ""

def _set_cookie(handler: BaseHTTPRequestHandler, name: str, value: str, path: str = "/") -> None:
    c = cookies.SimpleCookie()
    c[name] = value
    c[name]["path"] = path
    c[name]["httponly"] = True
    handler.send_header("Set-Cookie", c.output(header="").strip())

def _pref_from_headers(headers):
    # preferences stored in cookies: theme={dark|light}, lang={en|fr}
    def _c(name: str) -> str:
        try:
            c = headers.get('Cookie','')
            for part in c.split(';'):
                if '=' not in part:
                    continue
                k,v = part.strip().split('=',1)
                if k.strip()==name:
                    return v.strip()
        except Exception:
            return ''
        return ''

    theme = _c('theme') or 'dark'
    if theme not in ('dark','light'):
        theme = 'dark'
    lang = _c('lang') or 'en'
    if lang not in ('en','fr'):
        lang = 'en'
    return theme, lang

def _pref_links(next_path: str, theme: str, lang: str):
    t_new = 'light' if theme=='dark' else 'dark'
    t_label = 'Light' if theme=='dark' else 'Dark'
    l_new = 'fr' if lang=='en' else 'en'
    l_label = 'FR' if lang=='en' else 'EN'
    return (
        f"/pref/theme?value={t_new}&next={urllib.parse.quote(next_path)}",
        t_label,
        f"/pref/lang?value={l_new}&next={urllib.parse.quote(next_path)}",
        l_label,
    )


def _clear_cookie(handler: BaseHTTPRequestHandler, name: str, path: str="/") -> None:
    c = cookies.SimpleCookie()
    c[name] = ""
    c[name]["path"] = path
    c[name]["expires"] = "Thu, 01 Jan 1970 00:00:00 GMT"
    handler.send_header("Set-Cookie", c.output(header="").strip())

def _redirect(handler: BaseHTTPRequestHandler, url: str) -> None:
    handler.send_response(302)
    handler.send_header("Location", url)
    handler.end_headers()

def _send_html(handler: BaseHTTPRequestHandler, html_text: str, status: int = 200) -> None:
    data = html_text.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)

def _send_json(handler: BaseHTTPRequestHandler, obj: Any, status: int = 200) -> None:
    data = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)

def _send_file(handler: BaseHTTPRequestHandler, path: str) -> None:
    if not os.path.exists(path) or not os.path.isfile(path):
        _send_html(handler, "<h1>404</h1>", 404)
        return
    ctype, _ = mimetypes.guess_type(path)
    ctype = ctype or "application/octet-stream"
    with open(path, "rb") as f:
        data = f.read()
    handler.send_response(200)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)

def _portal_kind(handler: BaseHTTPRequestHandler) -> str:
    host = (handler.headers.get("Host") or "").lower()
    path = handler.path
    if host.startswith("admin.") or path.startswith("/admin"):
        return "admin"
    if host.startswith("client.") or path.startswith("/client"):
        return "client"
    return "public"

def _q(handler: BaseHTTPRequestHandler) -> Tuple[str, Dict[str, List[str]]]:
    parsed = urllib.parse.urlparse(handler.path)
    qs = urllib.parse.parse_qs(parsed.query)
    return parsed.path, qs

def _tenant_list() -> List[Dict[str, Any]]:
    out = []
    if not os.path.exists(TENANTS_DIR):
        return out
    for d in sorted(os.listdir(TENANTS_DIR)):
        p = os.path.join(TENANTS_DIR, d, "meta.json")
        meta = read_json(p, {})
        if meta:
            out.append(meta)
    return out

def _tenant_dir(tid: str) -> str:
    return safe_join(TENANTS_DIR, tid)


def _tenant_path(tid: str, filename: str) -> str:
    return os.path.join(_tenant_dir(tid), filename)

def _ensure_tenant_files(tdir: str) -> None:
    # New module files (messages/quotes/invoices/docs/templates) — safe to call repeatedly.
    for fn, default in [
        ("messages_email.json", {"threads": {}}),
        ("messages_sms.json", {"threads": {}}),
        ("quotes.json", {}),
        ("invoices.json", {}),
        ("docs_meta.json", {"docs": []}),
        ("templates.json", {"templates": {}}),
        ("leads_archive.json", {}),
    ]:
        p = os.path.join(tdir, fn)
        if not os.path.exists(p):
            write_json(p, default)

def _get_templates(tdir: str) -> Dict[str, str]:
    t = read_json(os.path.join(tdir, "templates.json"), {"templates": {}})
    return (t.get("templates") or {}) if isinstance(t, dict) else {}

def _render_thread(messages: List[Dict[str, Any]], channel: str) -> str:
    # simple conversation bubbles
    out = []
    for m in messages:
        dirn = m.get("direction","out")
        klass = "bubble out" if dirn=="out" else "bubble in"
        head = ""
        if channel == "email":
            subj = html.escape(m.get("subject","") or "(no subject)")
            head = f"<div class='muted small' style='font-weight:800'>{subj}</div>"
        body = html.escape(m.get("body",""))
        ts = html.escape(m.get("ts",""))
        out.append(f"""<div class='{klass}'>
          {head}
          <div style='white-space:pre-wrap'>{body}</div>
          <div class='muted small' style='margin-top:6px'>{ts}</div>
        </div>""")
    if not out:
        return "<div class='muted'>No messages yet.</div>"
    return "<div class='thread'>" + "".join(out) + "</div>"

def _render_messages_inbox(kind: str, tid: str, channel: str, qs: Dict[str, List[str]]) -> str:
    tdir = _tenant_dir(tid)
    _ensure_tenant_files(tdir)
    mdb = read_json(os.path.join(tdir, "messages_email.json" if channel=="email" else "messages_sms.json"), {"threads": {}})
    threads = (mdb.get("threads") or {}) if isinstance(mdb, dict) else {}
    clients = read_json(os.path.join(tdir, "clients.json"), {})
    # list newest by last ts
    rows = []
    def last_ts(cid):
        msgs = threads.get(cid) or []
        return msgs[-1].get("ts","") if msgs else ""
    for cid in sorted(threads.keys(), key=lambda c: last_ts(c), reverse=True):
        c = (clients.get(cid) or {})
        name = html.escape(c.get("name","") or cid)
        ts = html.escape(last_ts(cid))
        link = f"/{kind}?tenant={urllib.parse.quote(tid)}&tab=messages&channel={urllib.parse.quote(channel)}&modal=view_client&client_id={urllib.parse.quote(cid)}&subtab={( 'email' if channel=='email' else 'sms')}"
        rows.append(f"<tr><td><a href='{link}'><b>{name}</b></a><div class='muted small'>{html.escape(cid)}</div></td><td class='right'>{ts}</td></tr>")
    if not rows:
        rows_html = "<tr><td colspan='2' class='muted'>No threads yet.</td></tr>"
    else:
        rows_html = "".join(rows)
    pill = lambda key,label: f"<a class='{('pill' if channel==key else 'pill muted')}' style='margin-right:8px' href='/{kind}?tenant={urllib.parse.quote(tid)}&tab=messages&channel={urllib.parse.quote(key)}'>{html.escape(label)}</a>"
    top = pill("email","Email Inbox") + pill("sms","SMS Inbox")
    return f"""<div>{top}</div>
    <div class='spacer'></div>
    <table class='table'>
      <thead><tr><th>Thread</th><th class='right'>Last</th></tr></thead>
      <tbody>{rows_html}</tbody>
    </table>"""


def _require_session(handler: BaseHTTPRequestHandler, want_role_prefix: str) -> Optional[Dict[str, Any]]:
    """Return the active session dict if present and authorized.

    want_role_prefix is a simple prefix match used by the portal routers:
      - "owner"  -> admin portal (role "owner")
      - "client" -> client portal (roles "client_*" and optionally "owner")
    """
    sid = _get_cookie(handler, "sid")
    s = get_session(SESSIONS_PATH, sid)
    if not s:
        return None
    role = str(s.get("role", "") or "")
    # allow admin to view client portal when explicitly checked
    if want_role_prefix == "client":
        if not (role.startswith("client") or role.startswith("owner")):
            return None
        return s
    if want_role_prefix:
        if not role.startswith(want_role_prefix):
            return None
    return s

def _norm_role(role: str) -> str:
    """Normalize role strings across older/newer flows.

    Client portal logins store roles like: client_owner, client_employee.
    Dashboards expect: tenant_owner, tenant_employee, admin_employee.

    We keep the session role unchanged (still used for access checks),
    but normalize for UI gating and tab visibility.
    """
    r = (role or "").strip().lower()

    # Common / legacy aliases
    if r in ("", None):
        return ""
    if r == "admin":
        return "owner"
    if r == "employee":
        return "admin_employee"

    # Allow admin to view tenant portal when logging in through /client/login
    if r == "owner":
        return "tenant_owner"

    # Client portal stored roles
    if r == "client_owner":
        return "tenant_owner"
    if r == "client_employee":
        return "tenant_employee"

    # Sometimes the stored role may already be tenant_*
    if r.startswith("tenant_"):
        return r

    # Admin employee types (agency staff)
    if r in ("sales", "support", "admin_employee"):
        return "admin_employee"

    # Tenant employee types (moving company staff)
    if r in ("driver", "mover", "tenant_employee"):
        return "tenant_employee"

    # Fallback: best-effort mapping for unexpected client_* roles
    if r.startswith("client_"):
        suffix = r[len("client_"):]
        if suffix == "owner":
            return "tenant_owner"
        if suffix == "employee":
            return "tenant_employee"

    return r


def _login_page(kind: str, error: str = "", theme: str = "dark", lang: str = "en", next_path: str = "/") -> str:
    tpl_path = os.path.join(PORTALS, kind, "login.html")
    tpl = open(tpl_path, "r", encoding="utf-8").read()
    return render_template(tpl, {
        "app_name": APP_NAME,
        "error": html.escape(error),
        "error_block": (f"<div class='flash' style='border-color: rgba(248,113,113,.35); background: rgba(248,113,113,.08)'>"+html.escape(error)+"</div>" if error else ""),
        "kind": kind,
    
        "theme": theme,
        "lang": lang,
})

def _read_form(handler: BaseHTTPRequestHandler) -> Dict[str, Any]:
    """
    Read POST form data with **no cgi** (works on Python 3.8+ and future versions).
    Supports:
      - application/x-www-form-urlencoded (default HTML forms)
      - multipart/form-data (basic; enough for simple text fields + small uploads)
    Returns a dict; for uploads returns {"filename":..., "content_type":..., "data_b64":...}
    """
    ctype = (handler.headers.get("Content-Type") or "").strip()
    length = int(handler.headers.get("Content-Length") or "0")
    raw = handler.rfile.read(length) if length > 0 else b""

    # 1) x-www-form-urlencoded
    if ctype.startswith("application/x-www-form-urlencoded") or ctype == "" or ctype.startswith("text/plain"):
        try:
            qs = raw.decode("utf-8", errors="replace")
        except Exception:
            qs = ""
        parsed = urllib.parse.parse_qs(qs, keep_blank_values=True)
        return {k: (v[0] if isinstance(v, list) and v else "") for k, v in parsed.items()}

    # 2) multipart/form-data (basic parser)
    if ctype.startswith("multipart/form-data"):
        m = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', ctype)
        boundary = (m.group(1) or m.group(2)) if m else None
        if not boundary:
            return {}
        boundary_bytes = ("--" + boundary).encode("utf-8", errors="ignore")
        parts = raw.split(boundary_bytes)
        out: Dict[str, Any] = {}
        for part in parts:
            part = part.strip()
            if not part or part == b"--":
                continue
            if part.startswith(b"--"):
                continue
            # split headers/body
            CRLF = b"\x0d\x0a"
            CRLF2 = b"\x0d\x0a\x0d\x0a"
            if CRLF2 not in part:
                continue
            header_blob, body = part.split(CRLF2, 1)
            body = body.rstrip(CRLF)
            headers = header_blob.decode("utf-8", errors="replace").split("\r\n")
            hmap = {}
            hmap = {}
            for h in headers:
                if ":" in h:
                    k, v = h.split(":", 1)
                    hmap[k.strip().lower()] = v.strip()
            disp = hmap.get("content-disposition", "")
            # name + filename
            nm = re.search(r'name="([^"]+)"', disp)
            if not nm:
                continue
            name = nm.group(1)
            fnm = re.search(r'filename="([^"]*)"', disp)
            if fnm and fnm.group(1) != "":
                # file upload
                import base64
                out[name] = {
                    "filename": fnm.group(1),
                    "content_type": hmap.get("content-type", "application/octet-stream"),
                    "data_b64": base64.b64encode(body).decode("ascii"),
                }
            else:
                out[name] = body.decode("utf-8", errors="replace")
        return out

    # unknown
    return {}

def _admin_dashboard(session: Dict[str, Any], qs: Dict[str, List[str]], theme: str="dark", lang: str="en", path: str="") -> str:
    theme = theme or "dark"
    lang = lang or "en"
    path = path or ""

    # next_path is used by the preference toggles to return the user to the
    # current page. When not provided, fall back to the admin root.
    next_path = path or "/admin"

    tpl_path = os.path.join(PORTALS, "admin", "admin.html")
    toggle_theme_url, toggle_theme_label, toggle_lang_url, toggle_lang_label = _pref_links(next_path, theme, lang)
    tpl = open(tpl_path, "r", encoding="utf-8").read()

    tenants = _tenant_list()
    sel = qs.get("tenant", [tenants[0]["tenant_id"] if tenants else ""])[0]
    tab = qs.get("tab", ["notifications"])[0]
    flash = qs.get("flash", [""])[0]

    # aggregate KPIs from selected tenant
    tdir = _tenant_dir(sel) if sel else ""
    leads = read_json(os.path.join(tdir, "leads.json"), {}) if sel else {}
    cal = read_json(os.path.join(tdir, "calendar.json"), {}) if sel else {}
    pnl = read_json(os.path.join(tdir, "pnl.json"), {"entries":[]}) if sel else {"entries":[]}

    today = time.strftime("%Y-%m-%d")
    leads_today = sum(1 for x in (leads or {}).values() if str(x.get("created_at","")).startswith(today))
    deposits_today = sum(1 for e in pnl.get("entries",[]) if e.get("date")==today and e.get("type")=="revenue")
    closes_today = sum(1 for e in pnl.get("entries",[]) if e.get("date")==today and "balance" in (e.get("note","").lower()))
    ad_spend_today = sum(e.get("amount",0) for e in pnl.get("entries",[]) if e.get("date")==today and e.get("type")=="expense" and "ad" in (e.get("note","").lower()))
    roas_today = (sum(e.get("amount",0) for e in pnl.get("entries",[]) if e.get("date")==today and e.get("type")=="revenue") / ad_spend_today) if ad_spend_today else 0.0
    # render categorized notifications (clickable)
    today = time.strftime("%Y-%m-%d")
    new_leads = []
    for lid, l in list(leads.items())[:50]:
        new_leads.append(f"<li><a href='/admin?tenant={urllib.parse.quote(sel)}&tab=notifications&modal=view_lead&lead_id={urllib.parse.quote(lid)}'><b>New lead</b> {html.escape(l.get('name',''))} — {html.escape(l.get('phone',''))}</a></li>")

    jobs_today = []
    overdue = []
    for eid, e in list(cal.items())[:200]:
        date = str(e.get("date",""))
        link = f"/admin?tenant={urllib.parse.quote(sel)}&tab=notifications"
        rel = e.get("related") or {}
        if isinstance(rel, dict) and rel.get("client_id"):
            link += f"&modal=view_client&client_id={urllib.parse.quote(str(rel.get('client_id')))}&subtab=jobs"
        item = f"<li><a href='{link}'><b>{html.escape(str(e.get('type','event')).title())}</b> {html.escape(e.get('title',''))} — {html.escape(date)} {html.escape(e.get('time',''))}</a></li>"
        if date == today:
            jobs_today.append(item)
        elif date and date < today:
            overdue.append(item)

    def _section(title, items):
        return f"""<div class='card mini' style='margin-bottom:12px;'>
          <div style='font-weight:900'>{html.escape(title)}</div>
          <div class='spacer'></div>
          <ul class='list'>""" + ("".join(items) if items else "<li class='muted'>None.</li>") + "</ul></div>"

    notif_html = _section("New leads", new_leads[:15]) + _section("Jobs today", jobs_today[:15]) + _section("Overdue", overdue[:15])


    # tenant select options
    tenant_opts = "".join(
        f"<option value='{html.escape(t['tenant_id'])}' {'selected' if t['tenant_id']==sel else ''}>{html.escape(t.get('name',t['tenant_id']))} ({html.escape(t.get('category',''))})</option>"
        for t in tenants
    ) or "<option value=''>No tenants</option>"

    # leads table
    lead_rows = ""
    for lid, l in leads.items():
        # Include status column between phone and move date
        lead_rows += f"""
        <tr>
          <td><span class="pill">{html.escape(lid)}</span></td>
          <td>{html.escape(l.get('name',''))}<div class="muted small">{html.escape(l.get('email',''))}</div></td>
          <td>{html.escape(l.get('phone',''))}</td>
          <td>{html.escape(l.get('status',''))}</td>
          <td>{html.escape(l.get('move_date',''))}</td>
          <td>${float(l.get('value_est') or 0):,.2f}</td>
          <td class="right">
            <a class="btn btn-ghost" href="/admin?tenant={urllib.parse.quote(sel)}&tab=leads&modal=view_lead&lead_id={urllib.parse.quote(lid)}">Open</a>
          </td>
        </tr>
        """
    if not lead_rows:
        # 7 columns when status column is included
        lead_rows = "<tr><td colspan='7' class='muted'>No leads.</td></tr>"

    # calendar list
    cal_rows = ""
    for eid, e in cal.items():
        cal_rows += f"""
        <tr>
          <td><span class="pill">{html.escape(eid)}</span></td>
          <td>{html.escape(e.get('type',''))}</td>
          <td>{html.escape(e.get('title',''))}</td>
          <td>{html.escape(e.get('date',''))} {html.escape(e.get('time',''))}</td>
          <td class="right">
            <a class="btn btn-ghost danger" href="/admin?tenant={urllib.parse.quote(sel)}&tab=calendar&modal=delete_event&event_id={urllib.parse.quote(eid)}">Delete</a>
          </td>
        </tr>
        """
    if not cal_rows:
        cal_rows = "<tr><td colspan='5' class='muted'>No events.</td></tr>"

    # modal rendering
    modal = qs.get("modal", [""])[0]
    self_back = next_path
    self_back = next_path
    modal_html = ""
    if modal == "new_lead":
        modal_html = _modal_new_lead(sel, kind="admin")
    elif modal == "new_event":
        modal_html = _modal_new_event(sel, kind="admin")
    elif modal == "new_tenant":
        modal_html = _modal_new_tenant()
    elif modal == "view_lead":
        lead_id = qs.get("lead_id", [""])[0]
        subtab = qs.get("subtab", ["overview"])[0]
        modal_html = _modal_view_lead(sel, lead_id, kind="admin", subtab=subtab)
    elif modal == "view_client":
        cid = qs.get("client_id", [""])[0]
        subtab = qs.get("subtab", ["overview"])[0]
        modal_html = _modal_view_client(sel, cid, subtab=subtab, kind="admin")
    elif modal == "view_tenant":
        tid_view = qs.get("tenant_id", [""])[0]
        modal_html = _modal_view_tenant_admin(tid_view, lang=lang)
    elif modal == "archive_lead":
        lead_id = qs.get("lead_id", [""])[0]
        modal_html = _modal_confirm_delete(sel, "archive_lead", {"id": lead_id}, back=f"/admin?tenant={urllib.parse.quote(sel)}&tab=leads")
    elif modal == "unarchive_lead":
        lead_id = qs.get("lead_id", [""])[0]
        modal_html = _modal_unarchive_lead(sel, lead_id, kind="admin")
    elif modal == "complete_job":
        jid = qs.get("job_id", [""])[0]
        modal_html = _modal_confirm_delete(sel, "complete_job", {"id": jid}, back=f"/admin?tenant={urllib.parse.quote(sel)}&tab=clients")
    elif modal == "view_event":
        event_id = qs.get("event_id", [""])[0]
        modal_html = _modal_view_event(sel, event_id, kind="admin", back=self_back)
    elif modal == "delete_event":
        event_id = qs.get("event_id", [""])[0]
        modal_html = _modal_confirm_delete(sel, "delete_event", {"id": event_id}, back=f"/admin?tenant={urllib.parse.quote(sel)}&tab=calendar")
    elif modal == "view_employee":
        emp_id = qs.get("employee_id", [""])[0]
        modal_html = _modal_view_employee(sel, emp_id, kind="admin")
    elif modal == "view_truck":
        truck_id = qs.get("truck_id", [""])[0]
        modal_html = _modal_view_truck(sel, truck_id, kind="admin")
    elif modal == "reschedule_job":
        jid = qs.get("job_id", [""])[0]
        cid_param = qs.get("client_id", [""])[0]
        modal_html = _modal_reschedule_job(sel, jid, cid_param, kind="admin")
    elif modal == "cancel_job":
        jid = qs.get("job_id", [""])[0]
        cid_param = qs.get("client_id", [""])[0]
        modal_html = _modal_cancel_job(sel, jid, cid_param, kind="admin")



    # tab content
    tab_html = ""
    if tab == "notifications":
        tab_html = _render_file(os.path.join(PORTALS, "admin", "tabs", "notifications.html"), {"notif_html": notif_html})
    elif tab == "leads":
        tab_html = _render_file(os.path.join(PORTALS, "admin", "tabs", "leads.html"), {"lead_rows": lead_rows})
    elif tab == "messages":
        # Inbox for admin portal. Use the selected tenant instead of an undefined variable.
        channel = qs.get("channel", ["email"])[0] or "email"
        if channel not in ("email", "sms"):
            channel = "email"
        # Admin portal must generate /admin links, not /client links
        inbox = _render_messages_inbox("admin", sel, channel, qs)
        tab_html = _render_file(os.path.join(PORTALS, "admin", "tabs", "messages.html"), {"inbox": inbox})
    elif tab == "calendar":
        cal_view = qs.get("cal_view", ["month"])[0]
        cal_date = qs.get("cal_date", [time.strftime("%Y-%m-%d")])[0]
        cal_html = _render_calendar(sel, "admin", cal, cal_view, cal_date)
        tab_html = _render_file(os.path.join(PORTALS, "admin", "tabs", "calendar.html"), {"cal_html": cal_html})
    elif tab == "clients":
        # Admin-level client onboarding list = tenants
        rows = ""
        for t in tenants:
            rows += f"""
            <tr>
              <td><span class="pill">{html.escape(t['tenant_id'])}</span></td>
              <td>{html.escape(t.get('name',''))}<div class="muted small">{html.escape(t.get('category',''))}</div></td>
              <td class="right">
                <a class="btn btn-ghost" href="/admin?tenant={urllib.parse.quote(sel)}&tab=clients&modal=view_tenant&tenant_id={urllib.parse.quote(t['tenant_id'])}">{html.escape(_tr(lang,'btn.details','Details'))}</a>
                <a class="btn btn-ghost" href="/client?tenant={urllib.parse.quote(t['tenant_id'])}">Open portal</a>
              </td>
            </tr>
            """
        tab_html = _render_file(os.path.join(PORTALS, "admin", "tabs", "clients.html"), {})
        if qs.get("modal",[""])[0] == "new_tenant":
            tab_html += _modal_new_tenant()
    elif tab == "pnl":
        revenue = sum(e.get("amount",0) for e in pnl.get("entries",[]) if e.get("type")=="revenue")
        expense = sum(e.get("amount",0) for e in pnl.get("entries",[]) if e.get("type")=="expense")
        profit = revenue - expense
        entries = ""
        for e in pnl.get("entries",[])[:30]:
            sign = "+" if e.get("type")=="revenue" else "-"
            entries += f"<tr><td>{html.escape(e.get('date',''))}</td><td>{html.escape(e.get('note',''))}</td><td class='right'>{sign}${float(e.get('amount') or 0):,.2f}</td></tr>"
        if not entries:
            entries = "<tr><td colspan='3' class='muted'>No entries.</td></tr>"
        tab_html = _render_file(os.path.join(PORTALS, "admin", "tabs", "pnl.html"), {"entries": entries})
        if qs.get("modal",[""])[0] == "new_pnl":
            tab_html += _modal_new_pnl(sel, kind="admin")
    elif tab == "employees":
        # Agency employee management lives under the selected tenant (category=agency).
        employees = read_json(os.path.join(tdir, "employees.json"), {}) if sel else {}
        rows = ""
        for eid, e in (employees or {}).items():
            status = str(e.get("status") or "active")
            rows += f"""
            <tr>
              <td><span class='pill'>{html.escape(eid)}</span></td>
              <td>{html.escape(e.get('name',''))}<div class='muted small'>{html.escape(e.get('role',''))}</div></td>
              <td>{html.escape(e.get('phone',''))}<div class='muted small'>{html.escape(e.get('email',''))}</div></td>
              <td>{html.escape(status)}</td>
              <td class='right'>
                <a class='btn btn-ghost' href='/admin?tenant={urllib.parse.quote(sel)}&tab=employees&modal=view_employee&employee_id={urllib.parse.quote(eid)}'>Open</a>
                <form method='post' action='/cmd' style='display:inline'>
                  <input type='hidden' name='tenant' value='{html.escape(sel)}'/>
                  <input type='hidden' name='cmd' value='delete_employee'/>
                  <input type='hidden' name='employee_id' value='{html.escape(eid)}'/>
                  <input type='hidden' name='back' value='/admin?tenant={urllib.parse.quote(sel)}&tab=employees'/>
                  <button class='btn btn-ghost danger' type='submit'>Delete</button>
                </form>
              </td>
            </tr>
            """
        if not rows:
            rows = "<tr><td colspan='5' class='muted'>No employees yet.</td></tr>"

        tab_html = _render_file(os.path.join(PORTALS, "admin", "tabs", "employees.html"), {"rows": rows})
        if qs.get('modal',[""])[0] == 'new_employee':
            # Pass 'admin' to ensure the modal returns to the admin employees tab
            tab_html += _modal_new_employee(sel, kind="admin")


    elif tab == "settings":
        base_next = f"/admin?tenant={urllib.parse.quote(sel)}&tab=settings"
        pwd_form = f"""
        <form method='POST' action='/admin/settings/password'>
        <input type='hidden' name='back' value='{html.escape(base_next)}'/>
        <div class='grid2'>
        <div class='field'><label>Current password</label><input type='password' name='current' required/></div>
        <div class='field'><label>New password</label><input type='password' name='new' required/></div>
        <div class='field'><label>Confirm new password</label><input type='password' name='confirm' required/></div>
        <div class='field'><label>&nbsp;</label><button class='btn' type='submit'>Update Password</button></div>
        </div>
        </form>
        """
        tab_html = _render_file(os.path.join(PORTALS, "admin", "tabs", "settings.html"), {"pwd_form": pwd_form})

    else:
        tab_html = "<div class='card'><h2>Unknown tab</h2></div>"

    return render_template(tpl, {
            "app_name": APP_NAME,
            "username": html.escape(session.get("username","")),
            "role": html.escape(session.get("role","")),
            "tenant_select_options": tenant_opts,
            "selected_tenant": html.escape(sel),
            "tab": html.escape(tab),
            "flash": html.escape(flash),
            "flash_block": (f"<div class='flash'>"+html.escape(flash)+"</div>" if flash else ""),
            "kpi_leads_today": str(leads_today),
            "kpi_deposits_today": str(deposits_today),
            "kpi_closes_today": str(closes_today),
            "kpi_ad_spend_today": f"${ad_spend_today:,.2f}",
            "kpi_roas_today": f"{roas_today:.2f}x",
            "tab_content": tab_html,
            "modal_html": modal_html,

            "nav_notifications": "active" if tab=="notifications" else "",
            "nav_messages": "active" if tab=="messages" else "",
            "nav_leads": "active" if tab=="leads" else "",
            "nav_calendar": "active" if tab=="calendar" else "",
            "nav_clients": "active" if tab=="clients" else "",
            "nav_employees": "active" if tab=="employees" else "",
            "nav_pnl": "active" if tab=="pnl" else "",
            "nav_settings": "active" if tab=="settings" else "",

            "lbl_notifications": html.escape(_tr(lang, "nav.notifications", "Notifications")),
            "lbl_messages": html.escape(_tr(lang, "nav.messages", "Messages")),
            "lbl_leads": html.escape(_tr(lang, "nav.leads", "Leads")),
            "lbl_calendar": html.escape(_tr(lang, "nav.calendar", "Calendar")),
            "lbl_clients": html.escape(_tr(lang, "nav.clients", "Clients")),
            "lbl_employees": html.escape(_tr(lang, "nav.employees", "Employees")),
            "lbl_pnl": html.escape(_tr(lang, "nav.pnl", "Profit / Loss")),
            "lbl_settings": html.escape(_tr(lang, "nav.settings", "Settings")),

            "theme": theme,
            "lang": lang,
            "toggle_theme_url": toggle_theme_url,
            "toggle_theme_label": toggle_theme_label,
            "toggle_lang_url": toggle_lang_url,
            "toggle_lang_label": toggle_lang_label,
        })

def _client_dashboard(session: Dict[str, Any], qs: Dict[str, List[str]], theme: str="dark", lang: str="en", path: str="") -> str:
    theme = theme or "dark"
    lang = lang or "en"
    path = path or ""

    # next_path is used by the preference toggles to return the user to the
    # current page. When not provided, fall back to the client root.
    next_path = path or "/client"

    tpl_path = os.path.join(PORTALS, "client", "client.html")
    toggle_theme_url, toggle_theme_label, toggle_lang_url, toggle_lang_label = _pref_links(next_path, theme, lang)
    tpl = open(tpl_path, "r", encoding="utf-8").read()

    # Determine tenant and requested tab/modal
    tid = qs.get("tenant", [session.get("tenant_id","")])[0] or ""
    tab = qs.get("tab", ["notifications"])[0]
    modal = qs.get("modal", [""])[0]
    flash = qs.get("flash", [""])[0]

    # Normalize role and compute employee flag once per request.  Employees are
    # sub‑tenants who should have restricted access to certain tabs (e.g., P&L,
    # assets) and only see data assigned to them.  Without defining this
    # variable, references to ``is_emp`` will trigger a NameError.
    role = _norm_role(session.get("role", ""))
    is_emp = role in ("admin_employee", "tenant_employee")

    # If an employee attempts to access restricted sections in the client
    # dashboard, reset the tab to notifications to prevent unauthorized views.
    if is_emp and tab in ("pnl", "assets"):
        tab = "notifications"

    tdir = _tenant_dir(tid) if tid else ""
    leads = read_json(os.path.join(tdir, "leads.json"), {}) if tid else {}
    clients = read_json(os.path.join(tdir, "clients.json"), {}) if tid else {}
    jobs = read_json(os.path.join(tdir, "jobs.json"), {}) if tid else {}
    cal = read_json(os.path.join(tdir, "calendar.json"), {}) if tid else {}
    trucks = read_json(os.path.join(tdir, "trucks.json"), {}) if tid else {}
    employees = read_json(os.path.join(tdir, "employees.json"), {}) if tid else {}
    pnl = read_json(os.path.join(tdir, "pnl.json"), {"entries":[]}) if tid else {"entries":[]}

    # dropdown options
    truck_opts = "<option value=''>All trucks</option>" + "".join(
        f"<option value='{html.escape(k)}'>{html.escape(v.get('make',''))} {html.escape(v.get('model',''))} ({html.escape(str(v.get('year','')))})</option>"
        for k,v in trucks.items()
    )
    emp_opts = "<option value=''>All employees</option>" + "".join(
        f"<option value='{html.escape(k)}'>{html.escape(v.get('name',''))} — {html.escape(v.get('role',''))}</option>"
        for k,v in employees.items()
    )
    # categorized notifications (clickable)
    today = time.strftime("%Y-%m-%d")
    new_leads = []
    for lid, l in list(leads.items())[:50]:
        new_leads.append(f"<li><a href='/client?tenant={urllib.parse.quote(tid)}&tab=notifications&modal=view_lead&lead_id={urllib.parse.quote(lid)}'><b>Lead</b> {html.escape(l.get('name',''))} — {html.escape(l.get('phone',''))}</a></li>")

    jobs_today = []
    overdue = []
    for eid, e in list(cal.items())[:200]:
        date = str(e.get("date",""))
        rel = e.get("related") or {}
        link = f"/client?tenant={urllib.parse.quote(tid)}&tab=notifications"
        if isinstance(rel, dict) and rel.get("client_id"):
            link += f"&modal=view_client&client_id={urllib.parse.quote(str(rel.get('client_id')))}&subtab=jobs"
        item = f"<li><a href='{link}'><b>{html.escape(str(e.get('type','event')).title())}</b> {html.escape(e.get('title',''))} — {html.escape(date)} {html.escape(e.get('time',''))}</a></li>"
        if date == today:
            jobs_today.append(item)
        elif date and date < today:
            overdue.append(item)

    def _section(title, items):
        return f"""<div class='card mini' style='margin-bottom:12px;'>
          <div style='font-weight:900'>{html.escape(title)}</div>
          <div class='spacer'></div>
          <ul class='list'>""" + ("".join(items) if items else "<li class='muted'>None.</li>") + "</ul></div>"

    notif_html = _section("New leads", new_leads[:15]) + _section("Jobs today", jobs_today[:15]) + _section("Overdue", overdue[:15])


    # leads table
    lead_rows = ""
    for lid, l in leads.items():
        # Include status column between phone and move date
        lead_rows += f"""
        <tr>
          <td><span class="pill">{html.escape(lid)}</span></td>
          <td>{html.escape(l.get('name',''))}<div class="muted small">{html.escape(l.get('email',''))}</div></td>
          <td>{html.escape(l.get('phone',''))}</td>
          <td>{html.escape(l.get('status',''))}</td>
          <td>{html.escape(l.get('move_date',''))}</td>
          <td>${float(l.get('value_est') or 0):,.2f}</td>
          <td class="right">
            <a class="btn btn-ghost" href="/client?tenant={urllib.parse.quote(tid)}&tab=leads&modal=view_lead&lead_id={urllib.parse.quote(lid)}">Open</a>
          </td>
        </tr>
        """
    if not lead_rows:
        # 7 columns when status column is included
        lead_rows = "<tr><td colspan='7' class='muted'>No leads.</td></tr>"

    # clients list with job counts
    client_rows = ""
    for cid, c in clients.items():
        # Business rule: Clients tab shows clients only after at least one job is completed.
        completed_jobs = [j for j in jobs.values() if j.get("client_id")==cid and j.get("status")=="completed"]
        if not c.get("visible") and not completed_jobs:
            continue

        job_count = sum(1 for j in jobs.values() if j.get("client_id")==cid)
        client_rows += f"""
        <tr>
          <td><span class="pill">{html.escape(cid)}</span></td>
          <td>{html.escape(c.get('name',''))}<div class="muted small">{html.escape(c.get('phone',''))} • {html.escape(c.get('email',''))}</div></td>
          <td>{job_count}</td>
          <td class="right">
            <a class="btn btn-ghost" href="/client?tenant={urllib.parse.quote(tid)}&tab=clients&modal=view_client&client_id={urllib.parse.quote(cid)}">Open</a>
          </td>
        </tr>
        """
    if not client_rows:
        client_rows = "<tr><td colspan='4' class='muted'>No clients.</td></tr>"

    # calendar rows
    cal_rows = ""
    for eid, e in cal.items():
        cal_rows += f"""
        <tr>
          <td><span class="pill">{html.escape(eid)}</span></td>
          <td>{html.escape(e.get('type',''))}</td>
          <td>{html.escape(e.get('title',''))}</td>
          <td>{html.escape(e.get('date',''))} {html.escape(e.get('time',''))}</td>
          <td class="right">
            <a class="btn btn-ghost danger" href="/client?tenant={urllib.parse.quote(tid)}&tab=calendar&modal=delete_event&event_id={urllib.parse.quote(eid)}">Delete</a>
          </td>
        </tr>
        """
    if not cal_rows:
        cal_rows = "<tr><td colspan='5' class='muted'>No events.</td></tr>"

    # trucks rows
    truck_rows = ""
    for tk, t in trucks.items():
        truck_rows += f"""
        <tr>
          <td><span class="pill">{html.escape(tk)}</span></td>
          <td>{html.escape(t.get('make',''))} {html.escape(t.get('model',''))}</td>
          <td>{html.escape(str(t.get('year','')))}</td>
          <td>{float(t.get('consumption_per_100km') or 0):.1f} L/100km</td>
          <td>${float(t.get('gas_price_assumption') or 2.0):.2f}/L</td>
          <td class="right">
            <a class="btn btn-ghost" href="/client?tenant={urllib.parse.quote(tid)}&tab=assets&modal=view_truck&truck_id={urllib.parse.quote(tk)}">Open</a>
            <a class="btn btn-ghost danger" href="/client?tenant={urllib.parse.quote(tid)}&tab=assets&modal=delete_truck&truck_id={urllib.parse.quote(tk)}">Delete</a>
          </td>
        </tr>
        """
    if not truck_rows:
        truck_rows = "<tr><td colspan='6' class='muted'>No trucks.</td></tr>"

    # employees rows
    emp_rows = ""
    for ek, e in employees.items():
        emp_rows += f"""
        <tr>
          <td><span class="pill">{html.escape(ek)}</span></td>
          <td>{html.escape(e.get('name',''))}</td>
          <td>{html.escape(e.get('role',''))}</td>
          <td>{html.escape(e.get('phone',''))}</td>
          <td class="right">
            <a class="btn btn-ghost" href="/client?tenant={urllib.parse.quote(tid)}&tab=assets&modal=view_employee&employee_id={urllib.parse.quote(ek)}">Open</a>
            <a class="btn btn-ghost danger" href="/client?tenant={urllib.parse.quote(tid)}&tab=assets&modal=delete_employee&employee_id={urllib.parse.quote(ek)}">Delete</a>
          </td>
        </tr>
        """
    if not emp_rows:
        emp_rows = "<tr><td colspan='5' class='muted'>No employees.</td></tr>"

    # pnl filtering
    filt_truck = qs.get("truck", [""])[0]
    filt_emp = qs.get("emp", [""])[0]
    entries = []
    for e in pnl.get("entries",[]):
        if filt_truck and e.get("truck_id") != filt_truck:
            continue
        if filt_emp and e.get("employee_id") != filt_emp:
            continue
        entries.append(e)
    revenue = sum(e.get("amount",0) for e in entries if e.get("type")=="revenue")
    expense = sum(e.get("amount",0) for e in entries if e.get("type")=="expense")
    profit = revenue - expense
    entry_rows = ""
    for e in entries[:50]:
        sign = "+" if e.get("type")=="revenue" else "-"
        entry_rows += f"<tr><td>{html.escape(e.get('date',''))}</td><td>{html.escape(e.get('note',''))}</td><td class='right'>{sign}${float(e.get('amount') or 0):,.2f}</td></tr>"
    if not entry_rows:
        entry_rows = "<tr><td colspan='3' class='muted'>No entries.</td></tr>"

    # modal routing
    modal_html = ""
    if modal == "new_lead":
        modal_html = _modal_new_lead(tid, kind="client")
    elif modal == "new_event":
        modal_html = _modal_new_event(tid, kind="client")
    elif modal == "new_truck":
        modal_html = _modal_new_truck(tid)
    elif modal == "new_employee":
        # Use kind='client' so the modal returns to the assets tab
        modal_html = _modal_new_employee(tid, kind="client")
    elif modal == "new_pnl":
        modal_html = _modal_new_pnl(tid, kind="client")
    elif modal == "view_lead":
        lead_id = qs.get("lead_id", [""])[0]
        subtab = qs.get("subtab", ["overview"])[0]
        modal_html = _modal_view_lead(tid, lead_id, kind="client", subtab=subtab)
    elif modal == "archive_lead":
        lead_id = qs.get("lead_id", [""])[0]
        modal_html = _modal_confirm_delete(tid, "archive_lead", {"id": lead_id}, back=f"/client?tenant={urllib.parse.quote(tid)}&tab=leads")
    elif modal == "unarchive_lead":
        lead_id = qs.get("lead_id", [""])[0]
        modal_html = _modal_unarchive_lead(tid, lead_id, kind="client")
    elif modal == "complete_job":
        jid = qs.get("job_id", [""])[0]
        modal_html = _modal_confirm_delete(tid, "complete_job", {"id": jid}, back=f"/client?tenant={urllib.parse.quote(tid)}&tab=clients")
    elif modal == "view_event":
        event_id = qs.get("event_id", [""])[0]
        modal_html = _modal_view_event(tid, event_id, kind="client", back=self_back)
    elif modal == "delete_event":
        event_id = qs.get("event_id", [""])[0]
        modal_html = _modal_confirm_delete(tid, "delete_event", {"id": event_id}, back=f"/client?tenant={urllib.parse.quote(tid)}&tab=calendar")
    elif modal == "delete_truck":
        truck_id = qs.get("truck_id", [""])[0]
        modal_html = _modal_confirm_delete(tid, "delete_truck", {"id": truck_id}, back=f"/client?tenant={urllib.parse.quote(tid)}&tab=assets")
    elif modal == "delete_employee":
        employee_id = qs.get("employee_id", [""])[0]
        modal_html = _modal_confirm_delete(tid, "delete_employee", {"id": employee_id}, back=f"/client?tenant={urllib.parse.quote(tid)}&tab=assets")
    elif modal == "view_employee":
        employee_id = qs.get("employee_id", [""])[0]
        modal_html = _modal_view_employee(tid, employee_id, kind="client")
    elif modal == "view_truck":
        truck_id = qs.get("truck_id", [""])[0]
        modal_html = _modal_view_truck(tid, truck_id, kind="client")
    elif modal == "reschedule_job":
        jid = qs.get("job_id", [""])[0]
        cid_param = qs.get("client_id", [""])[0]
        modal_html = _modal_reschedule_job(tid, jid, cid_param, kind="client")
    elif modal == "cancel_job":
        jid = qs.get("job_id", [""])[0]
        cid_param = qs.get("client_id", [""])[0]
        modal_html = _modal_cancel_job(tid, jid, cid_param, kind="client")
    elif modal == "view_client":
        cid = qs.get("client_id", [""])[0]
        subtab = qs.get("subtab", ["overview"])[0]
        modal_html = _modal_view_client(tid, cid, subtab=subtab, kind="client")

    # tab content

    tab_html = ""
    if tab == "notifications":
        tab_html = _render_file(os.path.join(PORTALS, "client", "tabs", "notifications.html"), {"notif_html": notif_html})
    elif tab == "leads":
        tab_html = _render_file(os.path.join(PORTALS, "client", "tabs", "leads.html"), {"lead_rows": lead_rows})
    elif tab == "messages":
        # Inbox for tenant portal. Allows viewing email/SMS threads and sending replies.
        channel = qs.get("channel", ["email"])[0] or "email"
        if channel not in ("email", "sms"):
            channel = "email"
        inbox = _render_messages_inbox("client", tid, channel, qs)
        tab_html = _render_file(os.path.join(PORTALS, "client", "tabs", "messages.html"), {"inbox": inbox})
    elif tab == "calendar":
        cal_view = qs.get("cal_view", ["month"])[0]
        cal_date = qs.get("cal_date", [time.strftime("%Y-%m-%d")])[0]
        cal_html = _render_calendar(tid, "client", cal, cal_view, cal_date)
        tab_html = _render_file(os.path.join(PORTALS, "client", "tabs", "calendar.html"), {"cal_html": cal_html})
    elif tab == "clients":
        tab_html = _render_file(os.path.join(PORTALS, "client", "tabs", "clients.html"), {"client_rows": client_rows})
        if modal == "new_client":
            tab_html += _modal_new_client(tid, kind="client")
    elif tab == "pnl":
        tab_html = _render_file(os.path.join(PORTALS, "client", "tabs", "pnl.html"), {"emp_opts": emp_opts, "entry_rows": entry_rows, "truck_opts": truck_opts})
    elif tab == "assets":
        tab_html = _render_file(os.path.join(PORTALS, "client", "tabs", "assets.html"), {"emp_rows": emp_rows, "truck_rows": truck_rows})
    elif tab == "settings":
        # Settings: theme/lang/password + embedded form code
        base_next = f"/client?tenant={urllib.parse.quote(tid)}&tab=settings"
        embed_url = f"/embed/moving?tenant={urllib.parse.quote(tid)}"
        iframe = html.escape(f"<iframe src=\"{embed_url}\" style=\"width:100%;height:720px;border:0;border-radius:16px;\"></iframe>")
        form_code = html.escape(f"<iframe src=\"{embed_url}\" style=\"width:100%;height:720px;border:0;border-radius:16px;\" loading=\"lazy\"></iframe>")
        pwd_form = f"""
          <form method='POST' action='/client/settings/password'>
            <input type='hidden' name='back' value='{html.escape(base_next)}'/>
            <div class='grid2'>
              <div class='field'><label>Current password</label><input type='password' name='current' required/></div>
              <div class='field'><label>New password</label><input type='password' name='new' required/></div>
              <div class='field'><label>Confirm new password</label><input type='password' name='confirm' required/></div>
              <div class='field'><label>&nbsp;</label><button class='btn' type='submit'>Update Password</button></div>
            </div>
          </form>
        """
        tab_html = _render_file(os.path.join(PORTALS, "client", "tabs", "settings.html"), {"embed_url": embed_url, "form_code": form_code, "pwd_form": pwd_form})
    else:
        tab_html = "<div class='card'><h2>Unknown tab</h2></div>"

    return render_template(tpl, {
        "app_name": APP_NAME,
        "username": html.escape(session.get("username","")),
        "role": html.escape(session.get("role","")),
        "tenant_id": html.escape(tid),
        "tab": html.escape(tab),
        "flash": html.escape(flash),
        "flash_block": (f"<div class='flash'>"+html.escape(flash)+"</div>" if flash else ""),
        "truck_select_options": truck_opts,
        "employee_select_options": emp_opts,
        "tab_content": tab_html,
        "modal_html": modal_html,

        "nav_notifications": "active" if tab=="notifications" else "",
        "nav_messages": "active" if tab=="messages" else "",
        "nav_leads": "active" if tab=="leads" else "",
        "nav_calendar": "active" if tab=="calendar" else "",
        "nav_clients": "active" if tab=="clients" else "",
        # Hide P&L and Assets tabs for employees.  When an employee is logged in,
        # these nav items receive the ``hidden-link`` class instead of active
        # state.  Owners retain full visibility.
        "nav_pnl": ("hidden-link" if is_emp else ("active" if tab=="pnl" else "")),
        "nav_assets": ("hidden-link" if is_emp else ("active" if tab=="assets" else "")),
        "nav_settings": "active" if tab=="settings" else "",

        "lbl_notifications": html.escape(_tr(lang, "nav.notifications", "Notifications")),
        "lbl_messages": html.escape(_tr(lang, "nav.messages", "Messages")),
        "lbl_leads": html.escape(_tr(lang, "nav.leads", "Leads")),
        "lbl_calendar": html.escape(_tr(lang, "nav.calendar", "Calendar")),
        "lbl_clients": html.escape(_tr(lang, "nav.clients", "Clients")),
        "lbl_pnl": html.escape(_tr(lang, "nav.pnl", "Profit / Loss")),
        "lbl_assets": html.escape(_tr(lang, "nav.assets", "Trucks & Team")),
        "lbl_settings": html.escape(_tr(lang, "nav.settings", "Settings")),
    
        "theme": theme,
        "lang": lang,
        "toggle_theme_url": toggle_theme_url,
        "toggle_theme_label": toggle_theme_label,
        "toggle_lang_url": toggle_lang_url,
        "toggle_lang_label": toggle_lang_label,
})

def _modal_shell(title: str, body_html: str, back_url: str) -> str:
    return f"""
    <div class="modal">
      <div class="modal-backdrop"></div>
      <div class="modal-card">
        <div class="row between">
          <div>
            <div class="modal-title">{html.escape(title)}</div>
          </div>
          <div>
            <a class="btn btn-ghost" href="{html.escape(back_url)}">Close</a>
          </div>
        </div>
        <div class="spacer"></div>
        {body_html}
      </div>
    </div>
    """

def _cmd_form(tenant: str, cmd: str, back: str, fields_html: str, submit_label: str = "Save") -> str:
    return f"""
    <form method="POST" action="/cmd">
      <input type="hidden" name="tenant" value="{html.escape(tenant)}"/>
      <input type="hidden" name="cmd" value="{html.escape(cmd)}"/>
      <input type="hidden" name="back" value="{html.escape(back)}"/>
      {fields_html}
      <div class="spacer"></div>
      <button class="btn" type="submit">{html.escape(submit_label)}</button>
    </form>
    """


def _parse_ymd(s: str) -> Optional[datetime.date]:
    try:
        return datetime.datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return None

def _render_calendar(tid: str, kind: str, cal: Dict[str, Any], view: str, date_str: str) -> str:
    # Server-rendered calendar views (no JS). view in {day,week,month,year}
    today = datetime.date.today()
    d = _parse_ymd(date_str) or today

    def link(v: str, d2: datetime.date) -> str:
        base = f"/{kind}?tenant={urllib.parse.quote(tid)}&tab=calendar&cal_view={urllib.parse.quote(v)}&cal_date={urllib.parse.quote(d2.strftime('%Y-%m-%d'))}"
        return base

    # view selector
    def pill(v: str, label: str) -> str:
        cls = "pill" if view == v else "pill muted"
        return f"<a class='{cls}' style='margin-right:8px' href='{link(v, d)}'>{html.escape(label)}</a>"

    selector = pill("day","Day") + pill("week","Week") + pill("month","Month") + pill("year","Year")

    # index events by date
    by_date: Dict[str, list] = {}
    for eid, e in (cal or {}).items():
        ds = str(e.get("date",""))
        if not ds:
            continue
        by_date.setdefault(ds, []).append((eid, e))

    # navigation
    if view == "day":
        prev_d, next_d = d - datetime.timedelta(days=1), d + datetime.timedelta(days=1)
        nav = f"<a class='btn btn-ghost' href='{link('day', prev_d)}'>←</a> <span class='pill'>{d.strftime('%Y-%m-%d')}</span> <a class='btn btn-ghost' href='{link('day', next_d)}'>→</a>"
        rows = ""
        ds = d.strftime("%Y-%m-%d")
        events = sorted(by_date.get(ds, []), key=lambda x: str(x[1].get("time","")))
        for hour in range(0,24):
            slot = f"{hour:02d}:00"
            items = []
            for eid, e in events:
                t = str(e.get("time",""))
                if t.startswith(f"{hour:02d}:"):
                    rel = e.get("related") or {}
                    open_link = f"/{kind}?tenant={urllib.parse.quote(tid)}&tab=calendar&modal=view_event&event_id={urllib.parse.quote(str(eid))}&cal_view=day&cal_date={urllib.parse.quote(ds)}"
                    if isinstance(rel, dict) and rel.get("client_id"):
                        open_link = f"/{kind}?tenant={urllib.parse.quote(tid)}&tab=calendar&modal=view_client&client_id={urllib.parse.quote(str(rel.get('client_id')))}&subtab=jobs"
                    done = bool(e.get("completed"))
                    prefix = "✅ " if done else ""
                    deco = "line-through" if done else "none"
                    items.append(f"<div class='cal-item{(' done' if done else '')}'><div class='title'>{prefix}<span style='text-decoration:{deco}'>{html.escape(str(e.get('type','event')).title())} • {html.escape(e.get('title',''))}</span></div><div class='muted small'>{html.escape(ds)} {html.escape(t)}</div>" + (f"<div class='spacer'></div><a class='btn btn-ghost' href='{open_link}'>Open</a>" if open_link else "") + "</div>")
            rows += f"<tr><td style='width:110px' class='muted'>{slot}</td><td>{''.join(items) if items else '<span class=muted>—</span>'}</td></tr>"
        table = f"<table class='table cal cal-day'><thead><tr><th style='width:110px'>Time</th><th>Items</th></tr></thead><tbody>{rows}</tbody></table>"
        return f"<div class='row between'><div>{selector}</div><div class='row'>{nav}</div></div><div class='spacer'></div>{table}"

    if view == "week":
        # Monday-start week
        start = d - datetime.timedelta(days=(d.weekday()))
        days = [start + datetime.timedelta(days=i) for i in range(7)]
        prev_w, next_w = start - datetime.timedelta(days=7), start + datetime.timedelta(days=7)
        nav = f"<a class='btn btn-ghost' href='{link('week', prev_w)}'>←</a> <span class='pill'>{days[0].strftime('%Y-%m-%d')} .. {days[-1].strftime('%Y-%m-%d')}</span> <a class='btn btn-ghost' href='{link('week', next_w)}'>→</a>"
        head = "<tr><th style='width:90px'>Time</th>" + "".join(f"<th>{x.strftime('%a')}<div class='muted small'>{x.strftime('%m-%d')}</div></th>" for x in days) + "</tr>"
        body = ""
        for hour in range(0,24):
            row = f"<tr><td class='muted'>{hour:02d}:00</td>"
            for day in days:
                ds = day.strftime("%Y-%m-%d")
                evs = [e for e in by_date.get(ds, []) if str(e[1].get('time','')).startswith(f'{hour:02d}:')]
                cell = ""
                for eid, e in evs:
                    rel = e.get("related") or {}
                    open_link = f"/{kind}?tenant={urllib.parse.quote(tid)}&tab=calendar&modal=view_event&event_id={urllib.parse.quote(str(eid))}&cal_view=day&cal_date={urllib.parse.quote(ds)}"
                    if isinstance(rel, dict) and rel.get("client_id"):
                        open_link = f"/{kind}?tenant={urllib.parse.quote(tid)}&tab=calendar&modal=view_client&client_id={urllib.parse.quote(str(rel.get('client_id')))}&subtab=jobs"
                    cell += "<div class='cal-item" + (" done" if e.get('completed') else "") + "'><div class='title'>" + ("✅ " if e.get('completed') else "") + "<span style='text-decoration:" + ("line-through" if e.get('completed') else "none") + "'>" + html.escape(e.get('title','')) + "</span></div>" + "<div class='muted small'>" + html.escape(str(e.get('type',''))) + " " + html.escape(str(e.get('time',''))) + "</div>" + ((f"<div class='spacer'></div><a class='btn btn-ghost' href='{open_link}'>Open</a>") if open_link else "") + "</div>"
                row += f"<td>{cell if cell else '<span class=muted>—</span>'}</td>"
            row += "</tr>"
            body += row
        table = f"<table class='table cal cal-week'><thead>{head}</thead><tbody>{body}</tbody></table>"
        return f"<div class='row between'><div>{selector}</div><div class='row'>{nav}</div></div><div class='spacer'></div>{table}"

    if view == "year":
        year = d.year
        nav = f"<a class='btn btn-ghost' href='{link('year', datetime.date(year-1,1,1))}'>←</a> <span class='pill'>{year}</span> <a class='btn btn-ghost' href='{link('year', datetime.date(year+1,1,1))}'>→</a>"
        months_html = ""
        for m in range(1,13):
            mstart = datetime.date(year,m,1)
            mlabel = mstart.strftime("%B")
            count = sum(1 for ds in by_date.keys() if ds.startswith(f"{year}-{m:02d}-"))
            months_html += f"<div class='card mini' style='margin-bottom:12px;'><div class='row between'><div style='font-weight:900'>{html.escape(mlabel)}</div><a class='btn btn-ghost' href='{link('month', mstart)}'>Open</a></div><div class='muted small'>{count} item(s)</div></div>"
        return f"<div class='row between'><div>{selector}</div><div class='row'>{nav}</div></div><div class='spacer'></div>{months_html}"

    # month (default)
    first = d.replace(day=1)
    _, last_day = calendar.monthrange(first.year, first.month)
    # start grid on Monday
    grid_start = first - datetime.timedelta(days=first.weekday())
    weeks = []
    cur = grid_start
    for _ in range(6):
        week = [cur + datetime.timedelta(days=i) for i in range(7)]
        weeks.append(week)
        cur += datetime.timedelta(days=7)

    prev_m = (first - datetime.timedelta(days=1)).replace(day=1)
    next_m = (first + datetime.timedelta(days=32)).replace(day=1)
    nav = f"<a class='btn btn-ghost' href='{link('month', prev_m)}'>←</a> <span class='pill'>{first.strftime('%B %Y')}</span> <a class='btn btn-ghost' href='{link('month', next_m)}'>→</a>"

    head = "<tr>" + "".join(f"<th>{x}</th>" for x in ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]) + "</tr>"
    body = ""
    for week in weeks:
        row = "<tr>"
        for day in week:
            ds = day.strftime("%Y-%m-%d")
            in_month = (day.month == first.month)
            cell_style = "" if in_month else "opacity:.45"
            evs = by_date.get(ds, [])
            items = ""
            for eid, e in sorted(evs, key=lambda x: str(x[1].get("time","")))[:3]:
                rel = e.get("related") or {}
                open_link = ""
                if isinstance(rel, dict) and rel.get("client_id"):
                    open_link = f"/{kind}?tenant={urllib.parse.quote(tid)}&tab=calendar&modal=view_client&client_id={urllib.parse.quote(str(rel.get('client_id')))}&subtab=jobs"
                title = html.escape(e.get("title",""))
                t = html.escape(str(e.get("time","")))
                ev_link = f"/{kind}?tenant={urllib.parse.quote(tid)}&tab=calendar&modal=view_event&event_id={urllib.parse.quote(str(eid))}&cal_view=month&cal_date={urllib.parse.quote(date_str)}"
                done = bool(e.get('completed'))
                items += ("<div class='cal-item" + (" done" if done else "") + "'>"
                          + "<div class='muted small'>" + html.escape(str(e.get('type',''))) + " • " + t + "</div>"
                          + "<div class='title'>" + ("✅ " if done else "")
                          + (f"<a href='{open_link}'>" + title + "</a>" if open_link else f"<a href='{ev_link}'>" + title + "</a>")
                          + "</div></div>")
            if len(evs) > 3:
                items += f"<div class='muted small' style='margin-top:6px;'>+{len(evs)-3} more</div>"
            row += f"<td style='vertical-align:top; {cell_style}'><a class='daynum' href='{link('day', day)}'>{day.day}</a>{items}</td>"
        row += "</tr>"
        body += row

    table = f"<table class='table cal cal-month'><thead>{head}</thead><tbody>{body}</tbody></table>"
    return f"<div class='row between'><div>{selector}</div><div class='row'>{nav}</div></div><div class='spacer'></div>{table}" 

def _modal_new_lead(tid: str, kind: str) -> str:
    back = f"/{kind}?tenant={urllib.parse.quote(tid)}&tab=leads"

    if kind == "admin":
        fields = """
        <div class="grid2">
          <div class="field"><label>Name</label><input name="name" required/></div>
          <div class="field"><label>Phone</label><input name="phone" required/></div>
          <div class="field"><label>Email</label><input name="email"/></div>
          <div class="field"><label>Company name</label><input name="company_name"/></div>
          <div class="field"><label>Industry</label><input name="industry"/></div>
          <div class="field"><label>Website (optional)</label><input name="website" placeholder="https://"/></div>
          <div class="field" style="grid-column:1/-1;"><label>Notes</label><textarea name="notes" rows="4"></textarea></div>
        </div>
        """
    else:
        # client portal (moving company): property + size instead of estimated value
        # Include address suggestions via OpenStreetMap.  The `from_address` and `to_address` inputs
        # use datalists that are populated on-the-fly using a small inline script.  Typing at
        # least 3 characters will trigger a debounced fetch to the `/geo` endpoint which returns
        # up to 8 suggestion strings.  These suggestions are inserted into the associated
        # datalist elements.  This provides map‑based auto‑fill without requiring any external JS
        # dependencies.
        fields = """
        <div class="grid2">
          <div class="field"><label>Name</label><input name="name" required/></div>
          <div class="field"><label>Phone</label><input name="phone" required/></div>
          <div class="field"><label>Email</label><input name="email"/></div>
          <div class="field"><label>Move Date</label><input type="date" name="move_date" onkeydown="return false;"/></div>
          <div class="field"><label>From</label>
            <input name="from_address" list="from_list" autocomplete="off"/>
            <datalist id="from_list"></datalist>
          </div>
          <div class="field"><label>To</label>
            <input name="to_address" list="to_list" autocomplete="off"/>
            <datalist id="to_list"></datalist>
          </div>
          <div class="field"><label>Property Type</label>
            <select name="property_type">
              <option value="apartment">apartment</option>
              <option value="house">house</option>
              <option value="condo">condo</option>
              <option value="office">office</option>
              <option value="storage">storage</option>
              <option value="other">other</option>
            </select>
          </div>
          <div class="field"><label>Property Size</label><input name="property_size" placeholder="e.g., 3 1/2, 5 1/2, 1200 sqft"/></div>
          <div class="field"><label>Status</label>
            <select name="status">
              <option value="new">new</option>
              <option value="followup">followup</option>
            </select>
          </div>
          <div class="field" style="grid-column:1/-1;"><label>Notes</label><textarea name="notes" rows="4"></textarea></div>
        </div>
        <script>
        (function(){
          function wire(inputName, listId){
            var input = document.querySelector('input[name="'+inputName+'"]');
            var list = document.getElementById(listId);
            if(!input || !list) return;
            var t=null;
            input.addEventListener('input', function(){
              var q = (input.value || '').trim();
              if(q.length < 3) return;
              if(t) clearTimeout(t);
              t=setTimeout(function(){
                fetch('/geo?q='+encodeURIComponent(q)).then(function(r){ return r.json(); }).then(function(items){
                  list.innerHTML='';
                  (items||[]).slice(0,8).forEach(function(it){
                    var opt = document.createElement('option');
                    opt.value = it;
                    list.appendChild(opt);
                  });
                }).catch(function(){});
              }, 250);
            });
          }
          wire('from_address','from_list');
          wire('to_address','to_list');
        })();
        </script>
        """

    body = _cmd_form(tid, "create_lead", back, fields, "Create Lead")
    return _modal_shell("New Lead", body, back)

def _modal_new_event(tid: str, kind: str) -> str:
    back = f"/{kind}?tenant={urllib.parse.quote(tid)}&tab=calendar"
    fields = """
    <div class="grid2">
      <div class="field"><label>Type</label>
        <select name="type">
          <option value="followup">followup</option>
          <option value="call">call</option>
          <option value="job">job</option>
          <option value="note">note</option>
        </select>
      </div>
      <div class="field"><label>Title</label><input name="title" required/></div>
      <div class="field"><label>Date</label><input type="date" name="date" required onkeydown="return false;"/></div>
      <div class="field"><label>Time</label><input type="time" name="time" onkeydown="return false;"/></div>
    </div>
    """
    body = _cmd_form(tid, "create_event", back, fields, "Create Event")
    return _modal_shell("New Event", body, back)

def _modal_new_client(tid: str, kind: str) -> str:
    back = f"/{kind}?tenant={urllib.parse.quote(tid)}&tab=clients"
    fields = """
    <div class="grid2">
      <div class="field"><label>Name</label><input name="name" required/></div>
      <div class="field"><label>Phone</label><input name="phone"/></div>
      <div class="field"><label>Email</label><input name="email"/></div>
      <div class="field"><label>Address</label>
        <input name="address" list="addr_list" autocomplete="off"/>
        <datalist id="addr_list"></datalist>
      </div>
      <div class="field" style="grid-column:1/-1;"><label>Notes</label><textarea name="notes" rows="4"></textarea></div>
    </div>
    <script>
    (function(){
      var input = document.querySelector('input[name="address"]');
      var list = document.getElementById('addr_list');
      if(!input || !list) return;
      var t=null;
      input.addEventListener('input', function(){
        var q = (input.value || '').trim();
        if(q.length < 3) return;
        if(t) clearTimeout(t);
        t=setTimeout(function(){
          fetch('/geo?q='+encodeURIComponent(q)).then(function(r){ return r.json(); }).then(function(items){
            list.innerHTML='';
            (items||[]).slice(0,8).forEach(function(it){
              var opt = document.createElement('option');
              opt.value = it;
              list.appendChild(opt);
            });
          }).catch(function(){});
        }, 250);
      });
    })();
    </script>
    """
    body = _cmd_form(tid, "create_client", back, fields, "Create Client")
    return _modal_shell("New Client", body, back)

def _modal_convert_lead(tid: str, lead_id: str, kind: str) -> str:
    back = f"/{kind}?tenant={urllib.parse.quote(tid)}&tab=leads"
    fields = f"""
    <input type="hidden" name="lead_id" value="{html.escape(lead_id)}"/>
    <div class="grid2">
      <div class="field"><label>Job Price</label><input name="price" type="number" step="0.01"/></div>
      <div class="field"><label>Time</label><input name="time" type="time" value="09:00" onkeydown="return false;"/></div>
      <div class="field" style="grid-column:1/-1;"><label>Truck ID (optional)</label><input name="truck_id" placeholder="T0001"/></div>
      <div class="field" style="grid-column:1/-1;"><label>Employee IDs (comma separated)</label><input name="employee_ids" placeholder="EMP0001,EMP0002"/></div>
    </div>
    """
    body = _cmd_form(tid, "convert_to_client", back, fields, "Convert to Client")
    return _modal_shell(f"Convert Lead {lead_id}", body, back)

def _modal_new_truck(tid: str) -> str:
    back = f"/client?tenant={urllib.parse.quote(tid)}&tab=assets"
    fields = """
    <div class="grid2">
      <div class="field"><label>Make</label><input name="make" required/></div>
      <div class="field"><label>Model</label><input name="model" required/></div>
      <div class="field"><label>Year</label><input name="year" required/></div>
      <div class="field"><label>Consumption / 100km (L)</label><input name="consumption_per_100km" type="number" step="0.1" required/></div>
      <div class="field"><label>Gas price assumption ($/L)</label><input name="gas_price_assumption" type="number" step="0.01" value="2.00"/></div>
      <div class="field"><label>Insurance payment date</label><input type="date" name="insurance_payment_date" onkeydown="return false;"/></div>
      <div class="field"><label>Loan payment date (if any)</label><input type="date" name="loan_payment_date" onkeydown="return false;"/></div>
      <div class="field" style="grid-column:1/-1;"><label>Notes</label><textarea name="notes" rows="3"></textarea></div>
    </div>
    """
    body = _cmd_form(tid, "create_truck", back, fields, "Create Truck")
    return _modal_shell("New Truck", body, back)

def _modal_new_employee(tid: str, kind: str = "client") -> str:
    """Create a new employee. Accepts kind ('client' or 'admin') to determine the return URL.

    When invoked from the admin panel, the modal should return to the Employees tab. When invoked from
    a tenant portal, it should return to the Assets tab. Username and password fields are included to
    allow creation of sub‑tenant logins.
    """
    # Determine back URL based on portal kind
    if kind == "admin":
        back = f"/admin?tenant={urllib.parse.quote(tid)}&tab=employees"
    else:
        back = f"/client?tenant={urllib.parse.quote(tid)}&tab=assets"

    # Build form fields, including username/password for login credentials
    fields = """
    <div class="grid2">
      <div class="field"><label>Name</label><input name="name" required/></div>
      <div class="field"><label>Role</label><input name="role" value="mover"/></div>
      <div class="field"><label>Phone</label><input name="phone"/></div>
      <div class="field"><label>Email</label><input name="email"/></div>
      <div class="field"><label>Hourly rate</label><input name="hourly_rate" type="number" step="0.01"/></div>
      <div class="field"><label>Username</label><input name="username" required/></div>
      <div class="field"><label>Password</label><input name="password" type="password" required/></div>
      <div class="field" style="grid-column:1/-1;"><label>Notes</label><textarea name="notes" rows="3"></textarea></div>
    </div>
    """
    body = _cmd_form(tid, "create_employee", back, fields, "Create Employee")
    return _modal_shell("New Employee", body, back)


def _modal_view_employee(tid: str, eid: str, kind: str = "client") -> str:
    """View and edit an existing employee's details.

    Displays the current employee information and provides a form to update credentials and basic fields.
    """
    # Determine return URL based on portal kind
    if kind == "admin":
        back = f"/admin?tenant={urllib.parse.quote(tid)}&tab=employees"
    else:
        back = f"/client?tenant={urllib.parse.quote(tid)}&tab=assets"
    tdir = _tenant_dir(tid)
    employees = read_json(os.path.join(tdir, "employees.json"), {})
    emp = employees.get(eid) or {}

    # Build update form fields; blank password means no change
    form_fields = f"""
    <input type='hidden' name='employee_id' value='{html.escape(eid)}'/>
    <div class='grid2'>
      <div class='field'><label>Username</label><input name='username' value='{html.escape(emp.get('username',''))}'/></div>
      <div class='field'><label>Password</label><input name='password' type='password' placeholder='Leave blank to keep'/></div>
      <div class='field'><label>Name</label><input name='name' value='{html.escape(emp.get('name',''))}'/></div>
      <div class='field'><label>Role</label><input name='role' value='{html.escape(emp.get('role',''))}'/></div>
      <div class='field'><label>Phone</label><input name='phone' value='{html.escape(emp.get('phone',''))}'/></div>
      <div class='field'><label>Email</label><input name='email' value='{html.escape(emp.get('email',''))}'/></div>
      <div class='field'><label>Hourly rate</label><input name='hourly_rate' type='number' step='0.01' value='{html.escape(str(emp.get('hourly_rate','')))}'/></div>
      <div class='field' style='grid-column:1/-1;'><label>Notes</label><textarea name='notes' rows='3'>{html.escape(emp.get('notes',''))}</textarea></div>
    </div>
    """
    update_form = _cmd_form(tid, "update_employee", back, form_fields, "Update Employee")

    body = f"""
    <div class='row between'>
      <div style='font-weight:900'>Employee {html.escape(eid)}</div>
    </div>
    <div class='spacer'></div>
    <div class='card mini'>
      <div><b>{html.escape(emp.get('name',''))}</b></div>
      <div class='muted'>{html.escape(emp.get('role',''))}</div>
      <div class='muted'>{html.escape(emp.get('phone',''))} • {html.escape(emp.get('email',''))}</div>
      <div class='spacer'></div>
      {update_form}
    </div>
    """
    return _modal_shell(f"Employee {eid}", body, back)

# -----------------------------------------------------------------------------
# Truck details modal
def _modal_view_truck(tid: str, tkid: str, kind: str = "client") -> str:
    """
    View and edit an existing truck's details.  Displays the current
    information about the truck and provides a form to update editable fields.
    """
    # Determine back URL based on portal type
    if kind == "admin":
        back = f"/admin?tenant={urllib.parse.quote(tid)}&tab=assets"
    else:
        back = f"/client?tenant={urllib.parse.quote(tid)}&tab=assets"
    tdir = _tenant_dir(tid)
    trucks = read_json(os.path.join(tdir, "trucks.json"), {})
    trk = trucks.get(tkid) or {}
    # Build update form fields; leaving a field blank will keep current value
    fields = f"""
    <input type='hidden' name='truck_id' value='{html.escape(tkid)}'/>
    <div class='grid2'>
      <div class='field'><label>Make</label><input name='make' value='{html.escape(trk.get('make',''))}'/></div>
      <div class='field'><label>Model</label><input name='model' value='{html.escape(trk.get('model',''))}'/></div>
      <div class='field'><label>Year</label><input name='year' value='{html.escape(str(trk.get('year','')))}'/></div>
      <div class='field'><label>Consumption (L/100km)</label><input name='consumption_per_100km' type='number' step='0.1' value='{html.escape(str(trk.get('consumption_per_100km','')))}'/></div>
      <div class='field'><label>Gas price ($/L)</label><input name='gas_price_assumption' type='number' step='0.01' value='{html.escape(str(trk.get('gas_price_assumption','')))}'/></div>
      <div class='field'><label>Insurance payment</label><input name='insurance_payment_date' type='date' value='{html.escape(trk.get('insurance_payment_date',''))}' onkeydown='return false;'/></div>
      <div class='field'><label>Loan payment</label><input name='loan_payment_date' type='date' value='{html.escape(trk.get('loan_payment_date',''))}' onkeydown='return false;'/></div>
      <div class='field' style='grid-column:1/-1;'><label>Notes</label><textarea name='notes' rows='3'>{html.escape(trk.get('notes',''))}</textarea></div>
    </div>
    """
    form = _cmd_form(tid, "update_truck", back, fields, "Update Truck")
    body = f"""
    <div class='row between'>
      <div style='font-weight:900'>Truck {html.escape(tkid)}</div>
    </div>
    <div class='spacer'></div>
    <div class='card mini'>
      <div><b>{html.escape(trk.get('make',''))} {html.escape(trk.get('model',''))}</b></div>
      <div class='muted'>{html.escape(str(trk.get('year','')))}</div>
      <div class='muted'>Consumption: {float(trk.get('consumption_per_100km') or 0):.1f} L/100km • Gas: ${float(trk.get('gas_price_assumption') or 0):.2f}/L</div>
      <div class='muted'>Insurance: {html.escape(trk.get('insurance_payment_date',''))} • Loan: {html.escape(trk.get('loan_payment_date',''))}</div>
      <div class='spacer'></div>
      {form}
    </div>
    """
    return _modal_shell(f"Truck {tkid}", body, back)

# -----------------------------------------------------------------------------
# Reschedule job modal
def _modal_reschedule_job(tid: str, jid: str, cid: str, kind: str = "client") -> str:
    """
    Modal to reschedule a job.  Provides inputs for new date and time.
    """
    # Determine back URL to return to the client's jobs tab
    back = f"/{kind}?tenant={urllib.parse.quote(tid)}&tab=clients&modal=view_client&client_id={urllib.parse.quote(cid)}&subtab=jobs"
    tdir = _tenant_dir(tid)
    jobs = read_json(os.path.join(tdir, "jobs.json"), {})
    j = jobs.get(jid) or {}
    fields = f"""
    <input type='hidden' name='job_id' value='{html.escape(jid)}'/>
    <div class='grid2'>
      <div class='field'><label>New date</label><input type='date' name='move_date' value='{html.escape(j.get('move_date',''))}' onkeydown='return false;'/></div>
      <div class='field'><label>New time</label><input type='time' name='time' value='{html.escape(j.get('time','09:00'))}' onkeydown='return false;'/></div>
    </div>
    """
    form = _cmd_form(tid, "reschedule_job", back, fields, "Update Job")
    body = f"""
    <div class='spacer'></div>
    {form}
    """
    return _modal_shell("Reschedule Job", body, back)

# -----------------------------------------------------------------------------
# Cancel job modal
def _modal_cancel_job(tid: str, jid: str, cid: str, kind: str = "client") -> str:
    """
    Modal to confirm cancellation of a job.
    """
    back = f"/{kind}?tenant={urllib.parse.quote(tid)}&tab=clients&modal=view_client&client_id={urllib.parse.quote(cid)}&subtab=jobs"
    # Build simple confirmation form
    fields = f"<input type='hidden' name='job_id' value='{html.escape(jid)}'/>"
    body = _cmd_form(tid, "cancel_job", back, fields + "<div class='muted'>Are you sure you want to cancel this job? This cannot be undone.</div>", "Confirm Cancel")
    return _modal_shell("Cancel Job", body, back)

def _modal_new_pnl(tid: str, kind: str) -> str:
    back = f"/{kind}?tenant={urllib.parse.quote(tid)}&tab=pnl"
    fields = f"""
    <div class="grid2">
      <div class="field"><label>Date</label><input type="date" name="date" value="{time.strftime('%Y-%m-%d')}" onkeydown="return false;"/></div>
      <div class="field"><label>Type</label>
        <select name="type">
          <option value="revenue">revenue</option>
          <option value="expense">expense</option>
        </select>
      </div>
      <div class="field"><label>Amount</label><input name="amount" type="number" step="0.01" required/></div>
      <div class="field"><label>Truck ID (optional)</label><input name="truck_id" placeholder="T0001"/></div>
      <div class="field"><label>Employee ID (optional)</label><input name="employee_id" placeholder="EMP0001"/></div>
      <div class="field" style="grid-column:1/-1;"><label>Note</label><input name="note" placeholder="Deposit, gas, labor, ads, etc."/></div>
    </div>
    """
    body = _cmd_form(tid, "pnl_add_entry", back, fields, "Add Entry")
    return _modal_shell("Add P&L Entry", body, back)



def _modal_view_event(tid: str, event_id: str, kind: str = 'admin', back: str = '/') -> str:
    """Render a modal for viewing a single calendar event.  This version
    resolves the tenant directory via _tenant_dir and uses _modal_shell to
    produce a consistent popup shell.  When the event is missing, a
    graceful message is shown with a Close button."""
    tdir = _tenant_dir(tid)
    cal = read_json(os.path.join(tdir, 'calendar.json'), {})
    e = cal.get(event_id) or {}
    if not e:
        body = f"<div class='muted'>Event not found.</div><div class='spacer'></div><a class='btn btn-ghost' href='{html.escape(back)}'>Close</a>"
        return _modal_shell('Event', body, back)

    title = html.escape(str(e.get('title','')))
    etype = html.escape(str(e.get('type','event')))
    date = html.escape(str(e.get('date','')))
    tm = html.escape(str(e.get('time','')))
    notes = html.escape(str(e.get('notes','')))

    rel = e.get('related') or {}
    rel_html = ''
    if isinstance(rel, dict):
        # If a client ID is linked, build a link to open that client.  Fall back
        # to older keys if provided (client or client_id).
        client_id = rel.get('client_id') or rel.get('client')
        if client_id:
            cid = str(client_id)
            open_client = (f"/{kind}?tenant={urllib.parse.quote(tid)}&tab=calendar&modal=view_client&client_id="
                           f"{urllib.parse.quote(cid)}&subtab=jobs")
            rel_html = ("<div class='spacer'></div>"
                        "<div class='muted small'>Related client</div>"
                        f"<div><a class='btn btn-ghost' href='{open_client}'>Open Client</a></div>")

    del_link = (f"/{kind}?tenant={urllib.parse.quote(tid)}&tab=calendar"
                f"&modal=delete_event&event_id={urllib.parse.quote(event_id)}")

    body = (
        "<div class='row between'>"
        + f"<div><div class='modal-title'>{etype} • {title}</div>"
        + f"<div class='muted small'>{date} {tm}</div></div>"
        + f"<a class='btn btn-ghost danger' href='{del_link}'>Delete</a>"
        + "</div>"
        + "<div class='spacer'></div>"
        + "<div class='muted small'>Notes</div>"
        + f"<div class='card mini' style='margin-top:8px'>{notes or '<span class=muted>—</span>'}</div>"
        + rel_html
        + "<div class='spacer'></div>"
        + f"<a class='btn btn-ghost' href='{html.escape(back)}'>Close</a>"
    )

    # Use _modal_shell to generate the event modal.  Pass the back URL for
    # consistent handling of the Close action.
    return _modal_shell('Event', body, back)
def _modal_confirm_delete(tid: str, cmd: str, payload: Dict[str, Any], back: str) -> str:
    fields = "".join(f"<input type='hidden' name='{html.escape(k)}' value='{html.escape(str(v))}'/>" for k,v in payload.items())
    body = _cmd_form(tid, cmd, back, fields + "<div class='muted'>This cannot be undone.</div>", "Confirm Delete")
    return _modal_shell("Confirm Delete", body, back)

# -----------------------------------------------------------------------------
# Lead archiving/unarchiving
#
# A separate modal is provided for unarchiving leads.  When a lead is archived,
# its status is set to 'archived' via the command bus.  Restoring (unarchiving)
# the lead simply updates its status back to a default active state (e.g.,
# 'followup').  This modal wraps a simple form that posts to the
# update_lead_status command with the new status.
def _modal_unarchive_lead(tid: str, lead_id: str, kind: str) -> str:
    """Render a confirmation modal for unarchiving a lead."""
    back = f"/{kind}?tenant={urllib.parse.quote(tid)}&tab=leads"
    # Hidden fields specify the lead ID and the new status.  Here we restore to
    # 'followup' status by default.  If other statuses are needed, adjust here.
    fields = (
        f"<input type='hidden' name='id' value='{html.escape(lead_id)}'/>"
        f"<input type='hidden' name='status' value='followup'/>"
    )
    body = _cmd_form(
        tid,
        "update_lead_status",
        back,
        fields + "<div class='muted'>This will restore the lead to active status.</div>",
        "Unarchive"
    )
    return _modal_shell("Unarchive Lead", body, back)

def _modal_new_tenant() -> str:
    back = "/admin?tab=clients"
    fields = """
    <div class="grid2">
      <div class="field"><label>Tenant ID</label><input name="tenant_id" placeholder="T002" required/></div>
      <div class="field"><label>Company name</label><input name="name" required/></div>
      <div class="field"><label>Category</label>
        <select name="category">
          <option value="moving_company">moving_company</option>
          <option value="general_business">general_business</option>
        </select>
      </div>
      <div class="field"><label>Client username</label><input name="username" placeholder="ClientName" required/></div>
      <div class="field"><label>Client password</label><input name="password" type="password" required/></div>
    </div>
    """

    embed = """
    <div class="spacer"></div>
    <div class="card mini">
      <div style="font-weight:900">Moving lead intake embed (for moving_company)</div>
      <div class="muted small">No JS. Drop this iframe on any website. Replace <b>T002</b> with the tenant ID above.</div>
      <div class="spacer"></div>
      <div class="field">
        <label>Embed code</label>
        <textarea rows="4" readonly style="width:100%;">&lt;iframe src=&quot;/embed/moving?tenant=T002&quot; style=&quot;width:100%;height:820px;border:0;border-radius:16px;&quot;&gt;&lt;/iframe&gt;</textarea>
      </div>
    </div>
    """

    body = f"""
    <form method="POST" action="/admin/new_tenant">
      {fields}
      {embed}
      <div class="spacer"></div>
      <button class="btn" type="submit">Create Tenant</button>
    </form>
    """
    return _modal_shell("New Tenant", body, back)

def _modal_view_lead(tid: str, lead_id: str, kind: str, subtab: str = "overview") -> str:
    base_back = f"/{kind}?tenant={urllib.parse.quote(tid)}&tab=leads"
    tdir = _tenant_dir(tid)
    leads = read_json(os.path.join(tdir, "leads.json"), {})
    trucks = read_json(os.path.join(tdir, "trucks.json"), {})
    employees = read_json(os.path.join(tdir, "employees.json"), {})

    l = leads.get(lead_id) or {}

    def _tab_link(key: str, label: str) -> str:
        active = "tab active" if subtab == key else "tab"
        return f"<a class='{active}' href='/{kind}?tenant={urllib.parse.quote(tid)}&tab=leads&modal=view_lead&lead_id={urllib.parse.quote(lead_id)}&subtab={urllib.parse.quote(key)}'>{html.escape(label)}</a>"

    tabs = "<div class='tabs'>" + _tab_link("overview","Overview") + _tab_link("jobs","Jobs / Events") + "</div>"

    # Tenants do not need a separate Jobs/Events tab for leads.  Merge jobs/events into the overview
    # by hiding the tab navigation entirely when viewing from the client portal.  This simplifies
    # the modal and aligns with the requirement to merge the overview and jobs subviews.
    if kind == "client":
        tabs = ""

    # Archive/unarchive link: if the lead is currently archived, provide an unarchive action
    if str(l.get("status", "")).lower() == "archived":
        archive_link = f"/{kind}?tenant={urllib.parse.quote(tid)}&tab=leads&modal=unarchive_lead&lead_id={urllib.parse.quote(lead_id)}"
        archive_label = "Unarchive"
    else:
        archive_link = f"/{kind}?tenant={urllib.parse.quote(tid)}&tab=leads&modal=archive_lead&lead_id={urllib.parse.quote(lead_id)}"
        archive_label = "Archive"

    # Conversion form differs by portal
    if kind == "admin":
        emp_opts = "<option value=''>—</option>" + "".join(
            f"<option value='{html.escape(eid)}'>{html.escape(e.get('name',''))} ({html.escape(eid)})</option>"
            for eid, e in employees.items()
        )
        convert_fields = f"""
        <input type="hidden" name="lead_id" value="{html.escape(lead_id)}"/>
        <div class="grid2">
          <div class="field" style="grid-column:1/-1;"><label>Employee</label><select name="employee_id_1">{emp_opts}</select></div>
        </div>
        """
    else:
        truck_opts = "<option value=''>—</option>" + "".join(
            f"<option value='{html.escape(tid2)}'>{html.escape(t.get('make',''))} {html.escape(t.get('model',''))} {html.escape(str(t.get('year','')))} ({html.escape(tid2)})</option>"
            for tid2, t in trucks.items()
        )
        emp_opts = "<option value=''>—</option>" + "".join(
            f"<option value='{html.escape(eid)}'>{html.escape(e.get('name',''))} ({html.escape(eid)})</option>"
            for eid, e in employees.items()
        )
        convert_fields = f"""
        <input type="hidden" name="lead_id" value="{html.escape(lead_id)}"/>
        <div class="grid2">
          <div class="field"><label>Schedule date</label><input type="date" name="move_date" value="{html.escape(l.get('move_date',''))}" onkeydown="return false;"/></div>
          <div class="field"><label>Schedule time</label><input type="time" name="time" value="09:00" onkeydown="return false;"/></div>
          <div class="field" style="grid-column:1/-1;"><label>Truck</label><select name="truck_id">{truck_opts}</select></div>
          <div class="field"><label>Employee 1</label><select name="employee_id_1">{emp_opts}</select></div>
          <div class="field"><label>Employee 2 (optional)</label><select name="employee_id_2">{emp_opts}</select></div>
          <div class="field"><label>Employee 3 (optional)</label><select name="employee_id_3">{emp_opts}</select></div>
        </div>
        """

    convert_form = _cmd_form(
        tid,
        "convert_to_client",
        base_back,
        convert_fields,
        "Convert to Client"
    )

    overview = f"""
    <div class="row between">
      <div>{tabs}</div>
      <div class="row">
        <a class="btn btn-ghost danger" href="{archive_link}">{archive_label}</a>
      </div>
    </div>
    <div class="spacer"></div>
    <div class="card mini">
      <div><b>{html.escape(l.get('name',''))}</b></div>
      <div class="muted">{html.escape(l.get('phone',''))} • {html.escape(l.get('email',''))}</div>
      <div class="muted">{html.escape(l.get('company_name',''))} • {html.escape(l.get('industry',''))}</div>
      <div class="muted">{html.escape(l.get('website',''))}</div>
      <div class="spacer"></div>
      <div class="row between">
        <div><span class="muted small">Status</span> <span class="pill">{html.escape(l.get('status',''))}</span></div>
        <div style="min-width:260px">
          {_cmd_form(tid, "update_lead_status", base_back, f"<input type='hidden' name='id' value='{html.escape(lead_id)}'/><div class='row' style='gap:8px'><select name='status'><option>new</option><option>contacted</option><option>followup</option><option>booking_pending</option><option>booked</option><option>in_progress</option><option>completed</option><option>lost</option><option>archived</option></select><button class='btn btn-ghost' type='submit'>Update</button></div>", "Save")}
        </div>
      </div>

      <div class="spacer"></div>
      <div class="muted small">Move</div>
      <div>{html.escape(l.get('move_date',''))} • {html.escape(l.get('from_address',''))} → {html.escape(l.get('to_address',''))}</div>
      <div class="muted small" style="margin-top:10px;">Property</div>
      <div>{html.escape(l.get('property_type',''))} • {html.escape(l.get('property_size',''))}</div>
      <div class="spacer"></div>
      <div class="muted small">Notes</div>
      <div>{html.escape(l.get('notes',''))}</div>
    </div>
    <div class="spacer"></div>
    <h3>Convert</h3>
    <div class="muted">Conversion is executed deterministically through the command bus.</div>
    <div class="spacer"></div>
    {convert_form}
    """

    jobs_tab = f"""
    <div class="row between">
      <div>{tabs}</div>
      <div class="row">
        <a class="btn btn-ghost danger" href="{archive_link}">{archive_label}</a>
      </div>
    </div>
    <div class="spacer"></div>
    <div class="card mini">
      <div class="muted">Jobs/Events live under the client after conversion.</div>
      <div class="spacer"></div>
      <a class="btn btn-ghost" href="/{kind}?tenant={urllib.parse.quote(tid)}&tab=leads&modal=view_lead&lead_id={urllib.parse.quote(lead_id)}&subtab=overview">Back to overview</a>
    </div>
    """

    body = overview if subtab != "jobs" else jobs_tab
    return _modal_shell(f"Lead {lead_id}", body, base_back)


def _modal_view_client(tid: str, cid: str, subtab: str = "overview", kind: str = "client") -> str:
    back = (f"/admin?tenant={urllib.parse.quote(tid)}&tab=clients" if kind == "admin"
            else f"/client?tenant={urllib.parse.quote(tid)}&tab=clients")
    tdir = _tenant_dir(tid)
    clients = read_json(os.path.join(tdir, "clients.json"), {})
    jobs = read_json(os.path.join(tdir, "jobs.json"), {})
    c = clients.get(cid) or {}

    def _tab_link(key: str, label: str) -> str:
        active = "tab active" if subtab == key else "tab"
        return f"<a class='{active}' href='/{kind}?tenant={urllib.parse.quote(tid)}&tab=clients&modal=view_client&client_id={urllib.parse.quote(cid)}&subtab={urllib.parse.quote(key)}'>{html.escape(label)}</a>"

    tabs = "<div class='tabs'>" + _tab_link("overview","Overview") + _tab_link("jobs","Jobs") + _tab_link("email","Email") + _tab_link("sms","SMS") + _tab_link("quotes","Quotes") + _tab_link("invoices","Invoices") + _tab_link("docs","Documents") + "</div>"

    # Jobs rows + actions (complete/reschedule/cancel)
    job_rows = ""
    for jid, j in jobs.items():
        if j.get("client_id") != cid:
            continue
        status = str(j.get("status", "")).lower()
        # Build action links: reschedule and cancel are always available.  Complete is
        # shown only if not already completed or canceled.
        res_link = f"/{kind}?tenant={urllib.parse.quote(tid)}&tab=clients&modal=reschedule_job&job_id={urllib.parse.quote(jid)}&client_id={urllib.parse.quote(cid)}"
        cancel_link = f"/{kind}?tenant={urllib.parse.quote(tid)}&tab=clients&modal=cancel_job&job_id={urllib.parse.quote(jid)}&client_id={urllib.parse.quote(cid)}"
        actions = [
            f"<a class='btn btn-ghost' href='{res_link}'>Reschedule</a>",
            f"<a class='btn btn-ghost danger' href='{cancel_link}'>Cancel</a>"
        ]
        if status not in ("completed", "canceled"):
            comp_link = f"/{kind}?tenant={urllib.parse.quote(tid)}&tab=clients&modal=complete_job&job_id={urllib.parse.quote(jid)}"
            actions.append(f"<a class='btn btn-ghost' href='{comp_link}'>Complete</a>")
        # Join actions with spacing
        actions_html = " ".join(actions)
        job_rows += f"""<tr>
          <td><span class='pill'>{html.escape(jid)}</span></td>
          <td>{html.escape(status)}</td>
          <td>{html.escape(j.get('move_date',''))}</td>
          <td class='right'>${float(j.get('price') or 0):,.2f}</td>
          <td class='right'>{actions_html}</td>
        </tr>"""
    if not job_rows:
        job_rows = "<tr><td colspan='5' class='muted'>No jobs yet.</td></tr>"

    overview = f"""
    <div>{tabs}</div>
    <div class="spacer"></div>
    <div class="card mini">
      <div><b>{html.escape(c.get('name',''))}</b></div>
      <div class="muted">{html.escape(c.get('phone',''))} • {html.escape(c.get('email',''))}</div>
      <div class="muted">{html.escape(c.get('company_name',''))} • {html.escape(c.get('industry',''))}</div>
      <div class="muted">{html.escape(c.get('website',''))}</div>
      <div class="muted">{html.escape(c.get('address',''))}</div>
      <div class="spacer"></div>
      <div class="muted small">Notes</div>
      <div>{html.escape(c.get('notes',''))}</div>
    </div>
    """

    jobs_html = f"""
    <div>{tabs}</div>
    <div class="spacer"></div>
    <h3>Jobs</h3>
    <table class="table">
      <thead><tr><th>ID</th><th>Status</th><th>Date</th><th class="right">Price</th><th class="right">Action</th></tr></thead>
      <tbody>{job_rows}</tbody>
    </table>
    """

    
    # Messages (Email/SMS)
    _ensure_tenant_files(tdir)
    m_email = read_json(os.path.join(tdir, "messages_email.json"), {"threads": {}})
    m_sms = read_json(os.path.join(tdir, "messages_sms.json"), {"threads": {}})
    email_thread = ((m_email.get("threads") or {}).get(cid) or []) if isinstance(m_email, dict) else []
    sms_thread = ((m_sms.get("threads") or {}).get(cid) or []) if isinstance(m_sms, dict) else []

    send_email_form = _cmd_form(tid, "send_message", back,
        f"""<input type='hidden' name='channel' value='email'/><input type='hidden' name='client_id' value='{html.escape(cid)}'/>
        <div class='grid2'>
          <div class='field' style='grid-column:1/-1;'><label>Subject</label><input name='subject' placeholder='Subject'/></div>
          <div class='field' style='grid-column:1/-1;'><label>Body</label><textarea name='body' rows='4' placeholder='Type email...'></textarea></div>
        </div>""", "Send Email")
    send_sms_form = _cmd_form(tid, "send_message", back,
        f"""<input type='hidden' name='channel' value='sms'/><input type='hidden' name='client_id' value='{html.escape(cid)}'/>
        <div class='grid2'>
          <div class='field' style='grid-column:1/-1;'><label>Message</label><textarea name='body' rows='3' placeholder='Type SMS...'></textarea></div>
        </div>""", "Send SMS")

    email_html = f"""<div>{tabs}</div><div class='spacer'></div>
      <h3>Email (Conversations)</h3>
      {_render_thread(email_thread, "email")}
      <div class='spacer'></div>
      <div class='card mini'><div style='font-weight:900'>Send</div><div class='spacer'></div>{send_email_form}</div>
    """
    sms_html = f"""<div>{tabs}</div><div class='spacer'></div>
      <h3>SMS (Conversations)</h3>
      {_render_thread(sms_thread, "sms")}
      <div class='spacer'></div>
      <div class='card mini'><div style='font-weight:900'>Send</div><div class='spacer'></div>{send_sms_form}</div>
    """

    # Quotes
    quotes = read_json(os.path.join(tdir, "quotes.json"), {})
    quote_rows = ""
    if isinstance(quotes, dict):
        for qid, q in sorted(quotes.items()):
            if q.get("client_id") != cid:
                continue
            quote_rows += f"<tr><td><span class='pill'>{html.escape(qid)}</span></td><td>{html.escape(q.get('status',''))}</td><td class='right'>${float(q.get('total') or 0):,.2f}</td></tr>"
    if not quote_rows:
        quote_rows = "<tr><td colspan='3' class='muted'>No quotes yet.</td></tr>"
    # Build quote form conditionally based on the tenant category.  Agencies use
    # predefined subscription packages; moving companies use hourly/mover formulas.
    tenant_cat = _tenant_category(tid)
    quote_form_fields = ""
    if tenant_cat == "agency":
        # Package selection: annual subscription or onboarding.  Extras and notes
        # remain available.  Use 'package' field to inform the command handler.
        quote_form_fields = f"""
        <input type='hidden' name='client_id' value='{html.escape(cid)}'/>
        <div class='grid2'>
          <div class='field'><label>Package</label>
            <select name='package'>
              <option value='annual'>Annual Subscription (8736h) – $981</option>
              <option value='onboarding'>Onboarding (27m) – $99</option>
            </select>
          </div>
          <div class='field'><label>Extras</label><input name='extras' value='0'/></div>
          <div class='field' style='grid-column:1/-1;'><label>Notes</label><textarea name='notes' rows='3' placeholder='Notes...'></textarea></div>
        </div>
        """
    else:
        # Hourly/mover formula for moving companies and other categories
        quote_form_fields = f"""
        <input type='hidden' name='client_id' value='{html.escape(cid)}'/>
        <div class='grid2'>
          <div class='field'><label>Hours</label><input name='hours' value='6'/></div>
          <div class='field'><label>Rate per mover ($/h)</label><input name='rate' value='120'/></div>
          <div class='field'><label>Movers</label>
            <select name='movers'>
              <option value='1'>1 mover</option>
              <option value='2'>2 movers</option>
              <option value='3'>3 movers</option>
            </select>
          </div>
          <div class='field'><label>Extras</label><input name='extras' value='0'/></div>
          <div class='field' style='grid-column:1/-1;'><label>Notes</label><textarea name='notes' rows='3' placeholder='Notes...'></textarea></div>
        </div>
        """
    quote_form = _cmd_form(tid, "create_quote", back, quote_form_fields, "Generate Quote")
    quotes_html = f"""<div>{tabs}</div><div class='spacer'></div>
      <h3>Quotes</h3>
      <table class='table'><thead><tr><th>ID</th><th>Status</th><th class='right'>Total</th></tr></thead><tbody>{quote_rows}</tbody></table>
      <div class='spacer'></div>
      <div class='card mini'><div style='font-weight:900'>Auto‑Generate Quote</div><div class='muted small'>Uses a package or hourly formula depending on tenant type.</div><div class='spacer'></div>{quote_form}</div>
    """

    # Invoices
    invoices = read_json(os.path.join(tdir, "invoices.json"), {})
    inv_rows = ""
    if isinstance(invoices, dict):
        for iid, inv in sorted(invoices.items()):
            if inv.get("client_id") != cid:
                continue
            inv_rows += f"<tr><td><span class='pill'>{html.escape(iid)}</span></td><td>{html.escape(inv.get('status',''))}</td><td class='right'>${float(inv.get('total') or 0):,.2f}</td><td class='right'>${float(inv.get('deposit') or 0):,.2f}</td><td class='right'>${float(inv.get('balance') or 0):,.2f}</td></tr>"
    if not inv_rows:
        inv_rows = "<tr><td colspan='5' class='muted'>No invoices yet.</td></tr>"
    # Build a dropdown of quotes for this client for invoice creation
    quote_opts = "".join(
        f"<option value='{html.escape(qid)}'>{html.escape(qid)} - ${float(q.get('total') or 0):,.2f}</option>"
        for qid, q in (quotes.items() if isinstance(quotes, dict) else [])
        if q.get("client_id") == cid
    )
    invoice_form = _cmd_form(tid, "create_invoice", back,
        f"""<div class='grid2'>
          <div class='field' style='grid-column:1/-1;'><label>From Quote</label><select name='quote_id'>{quote_opts}</select></div>
          <div class='field'><label>Deposit</label><input name='deposit' value='299'/></div>
          <div class='field'><label>Notes</label><input name='notes' placeholder='Deposit received? etc.'/></div>
        </div>""", "Generate Invoice")
    invoices_html = f"""<div>{tabs}</div><div class='spacer'></div>
      <h3>Invoices</h3>
      <table class='table'><thead><tr><th>ID</th><th>Status</th><th class='right'>Total</th><th class='right'>Deposit</th><th class='right'>Balance</th></tr></thead><tbody>{inv_rows}</tbody></table>
      <div class='spacer'></div>
      <div class='card mini'><div style='font-weight:900'>Auto‑Generate Invoice</div><div class='muted small'>Creates invoice from an accepted quote. Balance = Total − Deposit.</div><div class='spacer'></div>{invoice_form}</div>
    """

    # Documents
    docs = read_json(os.path.join(tdir, "docs_meta.json"), {"docs": []})
    docs_list = ""
    for d in (docs.get("docs") or []) if isinstance(docs, dict) else []:
        if d.get("client_id") != cid:
            continue
        fname = html.escape(d.get("filename",""))
        did = html.escape(d.get("id",""))
        link = f"/file?tenant={urllib.parse.quote(tid)}&doc_id={urllib.parse.quote(d.get('id',''))}"
        docs_list += f"<li><a href='{link}' target='_blank'>{fname}</a> <span class='muted small'>({did})</span></li>"
    if not docs_list:
        docs_list = "<li class='muted'>No documents linked.</li>"
    upload_form = f"""<form class='form' method='POST' action='/cmd' enctype='multipart/form-data'>
        <input type='hidden' name='tenant' value='{html.escape(tid)}'/>
        <input type='hidden' name='cmd' value='upload_doc'/>
        <input type='hidden' name='back' value='{html.escape(back)}'/>
        <input type='hidden' name='client_id' value='{html.escape(cid)}'/>
        <div class='field'><label>Upload</label><input type='file' name='file' required/></div>
        <button class='btn' type='submit'>Upload & Link</button>
    </form>"""
    docs_html = f"""<div>{tabs}</div><div class='spacer'></div>
      <h3>Documents</h3>
      <ul class='list'>{docs_list}</ul>
      <div class='spacer'></div>
      <div class='card mini'><div style='font-weight:900'>Add Document</div><div class='spacer'></div>{upload_form}</div>
    """

    # Choose body
    body = overview
    if subtab == "jobs":
        body = jobs_html
    elif subtab == "email":
        body = email_html
    elif subtab == "sms":
        body = sms_html
    elif subtab == "quotes":
        body = quotes_html
    elif subtab == "invoices":
        body = invoices_html
    elif subtab == "docs":
        body = docs_html

    return _modal_shell(f"Client {cid}", body, back)


def _modal_view_tenant_admin(tid: str, lang: str = "en") -> str:
    """Admin-only: view tenant details (onboarding + links)."""
    tid = (tid or "").strip()
    if not tid:
        return _modal_shell("Tenant", "<div class='muted'>Missing tenant.</div>", "/admin?tab=clients")
    tdir = _tenant_dir(tid)
    meta = read_json(os.path.join(tdir, "meta.json"), {})
    name = meta.get("name", tid)
    category = meta.get("category", "")

    # find main owner user for this tenant
    udb = read_json(USERS_PATH, {"users": []})
    owner_user = ""
    if isinstance(udb, dict):
        for u in (udb.get("users") or []):
            if u.get("tenant_id") == tid and str(u.get("role", "")).startswith("client"):
                owner_user = u.get("username", "")
                break

    portal_link = f"/client?tenant={urllib.parse.quote(tid)}"
    embed_code = html.escape(f"<form method='POST' action='http://YOURDOMAIN/intake'>... (use Settings to copy full code) ...</form>")
    body = f"""
    <div class='section-head'>
      <div>
        <div class='title-lg'>{html.escape(name)}</div>
        <div class='muted small'>{html.escape(tid)} • {html.escape(category)}</div>
      </div>
      <div class='row wrap'>
        <a class='btn btn-ghost' href='{portal_link}'>Open portal</a>
      </div>
    </div>
    <div class='spacer'></div>
    <div class='card mini'>
      <div class='muted small'>Login</div>
      <div><b>User:</b> {html.escape(owner_user or '—')}</div>
      <div class='muted small' style='margin-top:10px;'>Form code</div>
      <textarea rows='4' readonly>{embed_code}</textarea>
      <div class='muted small' style='margin-top:10px;'>Tip: Use Settings to copy the real embed code.</div>
    </div>
    """
    return _modal_shell(_tr(lang, 'btn.details', 'Details'), body, "/admin?tab=clients")

def _send_public_index(handler: BaseHTTPRequestHandler) -> None:
    # Serve public index.html but inject a one-time token to prevent duplicate submissions.
    tpl = open(os.path.join(PUBLIC, "index.html"), "r", encoding="utf-8").read()
    token = str(int(time.time())) + "-" + str(os.getpid())
    # set cookie with current token for validation if desired
    handler.send_response(200)
    _set_cookie(handler, "intake_token", token, "/")
    html_text = render_template(tpl, {"token": html.escape(token)})
    data = html_text.encode("utf-8")
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _send_embed_moving(handler: BaseHTTPRequestHandler, tenant: str) -> None:
    # Lightweight embeddable moving lead intake (iframe safe).
    # Uses a tiny bit of optional JS for free address suggestions (OpenStreetMap Nominatim).
    token = str(int(time.time())) + "-" + str(os.getpid())
    handler.send_response(200)
    _set_cookie(handler, "intake_token", token, "/")
    html_text = f"""<!doctype html>
<html>
<head>
  <meta charset='utf-8'/>
  <meta name='viewport' content='width=device-width, initial-scale=1'/>
  <title>Moving Lead Intake</title>
  <style>
    :root{{--bg:#0b0f17;--panel:#111827;--line:#223047;--text:#e5e7eb;--muted:#9ca3af;--radius:16px;--font: ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Arial;}}
    *{{box-sizing:border-box}} body{{margin:0;font-family:var(--font);background:var(--bg);color:var(--text);}}
    .wrap{{padding:14px}}
    .card{{background:linear-gradient(180deg, rgba(17,24,39,.92), rgba(17,24,39,.78));border:1px solid var(--line);border-radius:var(--radius);padding:14px}}
    .grid2{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}
    .field label{{display:block;font-size:12px;color:var(--muted);margin-bottom:6px}}
    input,select,textarea{{width:100%;padding:10px 12px;border-radius:12px;border:1px solid var(--line);background:#0f172a;color:var(--text);outline:none}}
    .btn{{display:inline-block;padding:10px 14px;border-radius:12px;border:1px solid var(--line);background:#1f2937;color:var(--text);font-weight:800;cursor:pointer}}
    .muted{{color:var(--muted);font-size:12px}}
    @media (max-width:720px){{.grid2{{grid-template-columns:1fr}}}}
  </style>
</head>
<body>
  <div class='wrap'>
    <div class='card'>
      <div style='font-weight:900;font-size:18px'>Moving Lead Intake</div>
      <div class='muted'>No JS. Submits directly into the tenant CRM.</div>
      <div style='height:10px'></div>
      <form method='POST' action='/intake'>
        <input type='hidden' name='tenant' value='{html.escape(tenant)}'/>
        <input type='hidden' name='token' value='{html.escape(token)}'/>
        <div class='grid2'>
          <div class='field'><label>Name</label><input name='name' required/></div>
          <div class='field'><label>Phone</label><input name='phone' required/></div>
          <div class='field'><label>Email</label><input name='email'/></div>
          <div class='field'><label>Move date</label><input type='date' name='move_date' onkeydown='return false;'/></div>
          <div class='field'><label>From</label><input name='from_address' list='from_list' autocomplete='off'/><datalist id='from_list'></datalist></div>
          <div class='field'><label>To</label><input name='to_address' list='to_list' autocomplete='off'/><datalist id='to_list'></datalist></div>
          <div class='field'><label>Property type</label>
            <select name='property_type'>
              <option value='apartment'>apartment</option><option value='house'>house</option><option value='condo'>condo</option>
              <option value='office'>office</option><option value='storage'>storage</option><option value='other'>other</option>
            </select>
          </div>
          <div class='field'><label>Property size</label><input name='property_size' placeholder='e.g., 3 1/2, 5 1/2, 1200 sqft'/></div>
          <div class='field' style='grid-column:1/-1;'><label>Notes</label><textarea name='notes' rows='4'></textarea></div>
        </div>
        <div style='height:12px'></div>
        <button class='btn' type='submit'>Submit</button>
      </form>
      <script>
      (function(){{
        function wire(inputName, listId){{
          var el = document.querySelector('input[name="'+inputName+'"]');
          var dl = document.getElementById(listId);
          if(!el || !dl) return;
          var t=null;
          el.addEventListener('input', function(){{
            var q = (el.value||'').trim();
            if(q.length < 3) return;
            if(t) clearTimeout(t);
            t=setTimeout(function(){{
              fetch('/geo?q='+encodeURIComponent(q)).then(function(r){{return r.json();}}).then(function(items){{
                dl.innerHTML='';
                (items||[]).slice(0,8).forEach(function(it){{
                  var opt = document.createElement('option');
                  opt.value = it;
                  dl.appendChild(opt);
                }});
              }}).catch(function(){{}});
            }}, 250);
          }});
        }}
        wire('from_address','from_list');
        wire('to_address','to_list');
      }})();
      </script>
    </div>
  </div>
</body>
</html>"""
    data = html_text.encode("utf-8")
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)

def _intake_fingerprint(payload: dict) -> str:
    """Deterministic fingerprint for deduping website intake submissions (no JS, no DB)."""
    # Normalize fields to reduce accidental duplicates from whitespace/casing.
    fields = [
        (payload.get('name') or '').strip().lower(),
        (payload.get('phone') or '').strip(),
        (payload.get('email') or '').strip().lower(),
        (payload.get('company_name') or '').strip().lower(),
        (payload.get('industry') or '').strip().lower(),
        (payload.get('website') or '').strip().lower(),
        (payload.get('from_address') or '').strip().lower(),
        (payload.get('to_address') or '').strip().lower(),
        (payload.get('move_date') or '').strip(),
        (payload.get('property_type') or '').strip().lower(),
        (payload.get('property_size') or '').strip().lower(),
        (payload.get('notes') or '').strip().lower(),
    ]
    raw = "|".join(fields).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def _intake_dedupe_claim(tdir: str, fingerprint: str) -> bool:
    """
    Returns True if this fingerprint is NEW (claimed), False if it's a DUPLICATE.
    Implemented via exclusive file create to be safe under concurrent POSTs.
    """
    ddir = os.path.join(tdir, 'db', 'intake_dedupe')
    ensure_dir(ddir)
    fpath = os.path.join(ddir, fingerprint + '.txt')
    try:
        with open(fpath, 'x', encoding='utf-8') as f:
            f.write(str(int(time.time())) + '\n')
        return True
    except FileExistsError:
        return False


def _geo_suggest(q: str) -> list:
    """Free address suggestions via OpenStreetMap Nominatim.
    Runs server-side to keep the UI simple.
    """
    q = (q or "").strip()
    if len(q) < 3:
        return []
    try:
        url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode({
            "format": "json",
            "q": q,
            "addressdetails": "0",
            "limit": "8",
        })
        req = urllib.request.Request(url, headers={
            "User-Agent": "SayF.Agency CRM (local MVP)"
        })
        with urllib.request.urlopen(req, timeout=4) as r:
            raw = r.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
        out = []
        if isinstance(data, list):
            for it in data:
                if isinstance(it, dict) and it.get("display_name"):
                    out.append(str(it.get("display_name"))[:180])
        return out
    except Exception:
        return []



class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        kind = _portal_kind(self)
        path, qs = _q(self)
        theme, lang = _pref_from_headers(self.headers)

        # Host gating: keep admin portal separate from tenant/client portal.
        hn, port = _split_host(self.headers.get('Host',''))
        if path.startswith('/admin') and not (_is_local_host(hn) or hn.startswith('admin.')):
            target = _admin_host_for(hn) + (f":{port}" if port else "")
            return _redirect(self, f"http://{target}{self.path}")
        if path.startswith('/client') and not _is_local_host(hn) and hn.startswith('admin.'):
            target = _portal_host_for(hn) + (f":{port}" if port else "")
            return _redirect(self, f"http://{target}{self.path}")
        # convenience: if user hits / on admin subdomain, go to /admin
        if path == '/' and not _is_local_host(hn) and hn.startswith('admin.'):
            return _redirect(self, '/admin/login')

        # preferences (theme/lang cookies)
        if path.startswith('/pref/theme') or path.startswith('/pref/lang'):
            which = 'theme' if path.startswith('/pref/theme') else 'lang'
            value = (qs.get('value', [''])[0] or '').strip().lower()
            nxt = (qs.get('next', ['/'])[0] or '/').strip()
            if not nxt.startswith('/'):
                nxt = '/'
            if which=='theme' and value not in ('dark','light'):
                value = 'dark'
            if which=='lang' and value not in ('en','fr'):
                value = 'en'
            self.send_response(302)
            _set_cookie(self, which, value, '/')
            self.send_header('Location', nxt)
            self.end_headers()
            return

        # static assets (logo, icons)
        if path.startswith('/static/'):
            return self._serve_static(STATIC, path)


        # normalize /admin and /client prefixes
        if path == "/admin":
            qs.setdefault("tab", ["notifications"])
            return _send_html(self, _admin_dashboard(_require_session(self, "owner") or {}, qs, theme, lang, self.path) if _require_session(self, "owner") else _login_redirect("admin"))
        if path == "/client":
            sess = _require_session(self, "client") or _require_session(self, "owner")
            if not sess:
                return _send_html(self, _login_page("client", "", theme, lang, self.path))
            return _send_html(self, _client_dashboard(sess, qs, theme, lang, self.path))

        if path == "/file":
            tid = (qs.get("tenant", [""])[0] or "").strip()
            doc_id = (qs.get("doc_id", [""])[0] or "").strip()
            if not tid or not doc_id:
                return _send_html(self, "<h1>400</h1>", 400)
            tdir = _tenant_dir(tid)
            meta = read_json(os.path.join(tdir, "docs_meta.json"), {"docs": []})
            doc = None
            for d in (meta.get("docs") or []) if isinstance(meta, dict) else []:
                if d.get("id") == doc_id:
                    doc = d
                    break
            if not doc:
                return _send_html(self, "<h1>404</h1>", 404)
            fpath = os.path.join(tdir, "docs", doc.get("stored_name",""))
            return _send_file(self, fpath)


        if path.startswith("/admin/"):
            sub = path[len("/admin"):]
            if sub == "/login":
                return _send_html(self, _login_page("admin", qs.get("error",[""])[0], theme, lang, self.path))
            if sub == "/logout":
                sid = _get_cookie(self, "sid")
                self.send_response(302)
                _clear_cookie(self, "sid", "/")
                delete_session(SESSIONS_PATH, sid)
                self.send_header("Location", "/admin/login")
                self.end_headers()
                return
            # admin dashboard alias
            if sub == "" or sub == "/":
                return _send_html(self, _admin_dashboard(_require_session(self, "owner") or {}, qs, theme, lang, self.path) if _require_session(self, "owner") else _login_redirect("admin"))
            # static assets
            return self._serve_static(PORTALS, path)

        if path.startswith("/client/"):
            sub = path[len("/client"):]
            if sub == "/login":
                return _send_html(self, _login_page("client", qs.get("error",[""])[0], theme, lang, self.path))
            if sub == "/logout":
                sid = _get_cookie(self, "sid")
                self.send_response(302)
                _clear_cookie(self, "sid", "/")
                delete_session(SESSIONS_PATH, sid)
                self.send_header("Location", "/client/login")
                self.end_headers()
                return
            return self._serve_static(PORTALS, path)

        # docs serving
        if path == "/doc":
            tid = qs.get("tenant", [""])[0]
            rel = qs.get("path", [""])[0]
            if not tid or not rel:
                return _send_html(self, "<h1>Bad request</h1>", 400)
            tdir = _tenant_dir(tid)
            try:
                p = safe_join(os.path.join(tdir, "docs"), rel)
            except Exception:
                return _send_html(self, "<h1>Unsafe path</h1>", 400)
            return _send_file(self, p)

        # public
        if path == "/" or path == "/index.html":
            return _send_public_index(self)
        if path == "/embed/moving":
            tenant = qs.get("tenant", ["T001"])[0]
            return _send_embed_moving(self, tenant)
        if path == "/geo":
            qv = qs.get("q", [""])[0]
            items = _geo_suggest(qv)
            payload = json.dumps(items).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if path == "/thanks":
            return _send_file(self, os.path.join(PUBLIC, "thanks.html"))

        # public static
        if path.startswith("/public/"):
            return self._serve_static(ROOT, path)

        # default: try public file
        public_try = os.path.join(PUBLIC, path.lstrip("/"))
        if os.path.exists(public_try):
            return _send_file(self, public_try)

        _send_html(self, "<h1>404</h1>", 404)

    def do_POST(self):
        path, qs = _q(self)
        theme, lang = _pref_from_headers(self.headers)

        # Host gating for POSTs as well.
        hn, port = _split_host(self.headers.get('Host',''))
        if path.startswith('/admin') and not (_is_local_host(hn) or hn.startswith('admin.')):
            target = _admin_host_for(hn) + (f":{port}" if port else "")
            return _redirect(self, f"http://{target}{self.path}")
        if path.startswith('/client') and not _is_local_host(hn) and hn.startswith('admin.'):
            target = _portal_host_for(hn) + (f":{port}" if port else "")
            return _redirect(self, f"http://{target}{self.path}")


        if path == "/admin/login" or path == "/admin/login/":
            form = _read_form(self)
            return self._handle_login("admin", form)

        if path == "/client/login" or path == "/client/login/":
            form = _read_form(self)
            return self._handle_login("client", form)

        if path == "/client/settings/password":
            form = _read_form(self)
            return self._handle_change_password(form, kind="client")

        if path == "/admin/settings/password":
            form = _read_form(self)
            return self._handle_change_password(form, kind="admin")

        if path == "/cmd":
            form = _read_form(self)
            return self._handle_cmd(form)

        if path == "/intake":
            form = _read_form(self)
            return self._handle_intake(form)

        if path == "/admin/new_tenant":
            form = _read_form(self)
            return self._handle_new_tenant(form)

        _send_html(self, "<h1>404</h1>", 404)

    def _handle_change_password(self, form: Dict[str, Any], kind: str = "client") -> None:
        sid = _get_cookie(self, "sid")
        sess = get_session(SESSIONS_PATH, sid)
        if not sess:
            return _redirect(self, f"/{kind}/login")

        # role gating
        if kind == "admin" and not str(sess.get("role","")).startswith("owner"):
            return _redirect(self, "/admin/login?error=Not%20authorized")
        if kind == "client" and not str(sess.get("role","")).startswith(("client","owner")):
            return _redirect(self, "/client/login?error=Not%20authorized")

        current = (form.get("current") or "").strip()
        new1 = (form.get("new") or "").strip()
        new2 = (form.get("confirm") or "").strip()
        back = (form.get("back") or ("/admin" if kind=="admin" else "/client")).strip()
        if not back.startswith("/"):
            back = "/"

        u = find_user(USERS_PATH, sess.get("username",""))
        if not u or not verify_password(current, u.get("password_hash","")):
            return _redirect(self, back + ("&flash=Wrong%20current%20password" if "?" in back else "?flash=Wrong%20current%20password"))
        if not new1 or len(new1) < 6:
            return _redirect(self, back + ("&flash=Password%20too%20short" if "?" in back else "?flash=Password%20too%20short"))
        if new1 != new2:
            return _redirect(self, back + ("&flash=Passwords%20do%20not%20match" if "?" in back else "?flash=Passwords%20do%20not%20match"))

        upsert_user(USERS_PATH, {
            "username": u.get("username"),
            "password_hash": hash_password(new1),
            "role": u.get("role"),
            "tenant_id": u.get("tenant_id"),
            "display_name": u.get("display_name"),
        })
        return _redirect(self, back + ("&flash=Password%20updated" if "?" in back else "?flash=Password%20updated"))

    def _serve_static(self, base_dir: str, req_path: str) -> None:
        try:
            rel = req_path.lstrip("/")
            if rel.startswith("static/"):
                rel = rel[len("static/") :]
            p = safe_join(base_dir, rel)
        except Exception:
            return _send_html(self, "<h1>400</h1>", 400)
        return _send_file(self, p)

    def _handle_login(self, kind: str, form: Dict[str, Any]) -> None:
        username = (form.get("username") or "").strip()
        password = (form.get("password") or "").strip()

        u = find_user(USERS_PATH, username)
        if not u or not verify_password(password, u.get("password_hash","")):
            return _redirect(self, f"/{kind}/login?error=Invalid%20credentials")

        # role gate
        if kind == "admin" and u.get("role") != "owner":
            return _redirect(self, f"/admin/login?error=Not%20an%20admin")
        if kind == "client" and not u.get("role","").startswith(("client","owner")):
            return _redirect(self, f"/client/login?error=Not%20a%20client")

        sid = new_session(SESSIONS_PATH, u["username"], u["role"], u.get("tenant_id"))
        self.send_response(302)
        _set_cookie(self, "sid", sid, "/")
        self.send_header("Location", "/admin?tab=notifications" if kind=="admin" else f"/client?tenant={urllib.parse.quote(u.get('tenant_id') or '')}&tab=notifications")
        self.end_headers()

    def _handle_cmd(self, form: Dict[str, Any]) -> None:
        sid = _get_cookie(self, "sid")
        sess = get_session(SESSIONS_PATH, sid)
        if not sess:
            return _redirect(self, "/admin/login")

        tenant = (form.get("tenant") or "").strip()
        cmd = (form.get("cmd") or "").strip()
        back = (form.get("back") or "/").strip()

        # sanitize payload
        payload = {}
        for k, v in form.items():
            if k in ("tenant","cmd","back"):
                continue
            if isinstance(v, list):
                payload[k] = v
            else:
                payload[k] = str(v)

        # normalize employee slots (employee_id_1..3 -> employee_ids)
        emp_slots = []
        for k in ("employee_id_1","employee_id_2","employee_id_3"):
            v = payload.get(k)
            if isinstance(v, str) and v.strip():
                emp_slots.append(v.strip())
        if emp_slots and not payload.get("employee_ids"):
            payload["employee_ids"] = emp_slots
        # remove slot keys to keep payload clean
        for k in ("employee_id_1","employee_id_2","employee_id_3"):
            if k in payload:
                payload.pop(k, None)

        # special parse for employee_ids csv
        if "employee_ids" in payload and isinstance(payload["employee_ids"], str):
            payload["employee_ids"] = [x.strip() for x in payload["employee_ids"].split(",") if x.strip()]

        
        # Handle document upload (multipart) before writing the command
        if cmd == "upload_doc":
            tdir = _tenant_dir(tenant)
            _ensure_tenant_files(tdir)
            f = payload.get("file")
            client_id = (payload.get("client_id") or "").strip()
            job_id = (payload.get("job_id") or "").strip()
            if isinstance(f, dict) and f.get("data_b64"):
                import base64, uuid
                ensure_dir(os.path.join(tdir, "docs"))
                stored = f"{int(time.time())}_{uuid.uuid4().hex[:8]}_{os.path.basename(f.get('filename') or 'upload.bin')}"
                data = base64.b64decode((f.get("data_b64") or "").encode("utf-8"))
                fp = os.path.join(tdir, "docs", stored)
                with open(fp, "wb") as out:
                    out.write(data)
                meta = read_json(os.path.join(tdir, "docs_meta.json"), {"docs": []})
                if not isinstance(meta, dict):
                    meta = {"docs": []}
                doc_id = "D" + uuid.uuid4().hex[:8]
                meta["docs"].append({
                    "id": doc_id,
                    "created_at": now_iso(),
                    "filename": f.get("filename") or stored,
                    "stored_name": stored,
                    "content_type": f.get("content_type") or "application/octet-stream",
                    "client_id": client_id or None,
                    "job_id": job_id or None,
                })
                write_json(os.path.join(tdir, "docs_meta.json"), meta)
                # Replace payload to a deterministic reference (worker doesn't need binary)
                payload.pop("file", None)
                payload["doc_id"] = doc_id
                payload["stored_name"] = stored
            else:
                return _redirect(self, back + ("&" if "?" in back else "?") + "flash=" + urllib.parse.quote("FAIL: missing file"))

        # immediate processing
        tdir = _tenant_dir(tenant)
        cmd_path = write_command(tdir, cmd, payload)
        ok, detail = process_command_file(tdir, cmd_path)

        sep = "&" if ("?" in back) else "?"
        msg = urllib.parse.quote(("OK: " if ok else "FAIL: ") + detail)
        return _redirect(self, back + f"{sep}flash={msg}")

    def _handle_intake(self, form: Dict[str, Any]) -> None:
        # Public intake form posts here; prevents duplicate submission by using a one-time token in cookie.
        token = (form.get("token") or "").strip() or _get_cookie(self, "intake_token")
        last = _get_cookie(self, "intake_token_used")
        if token and token == last:
            # already used
            return _redirect(self, "/thanks?dup=1")

        # choose tenant
        tenant_raw = (form.get('tenant') or form.get('tenant_hint') or 'T001').strip()
        tenant = tenant_raw if tenant_raw.startswith('T') and tenant_raw[1:].isdigit() else 'T001'
        payload = {
            "status": "new",
            "name": (form.get("name") or "").strip(),
            "phone": (form.get("phone") or "").strip(),
            "email": (form.get("email") or "").strip(),
            "notes": (form.get("notes") or "").strip(),
            "source": "website",
        }
        # Two intake modes:
        # 1) Agency/general lead (public website): company fields
        # 2) Moving lead (embed): move fields + property
        if (form.get("company_name") or form.get("industry") or form.get("website")):
            payload["company_name"] = (form.get("company_name") or "").strip()
            payload["industry"] = (form.get("industry") or "").strip()
            payload["website"] = (form.get("website") or "").strip()
        else:
            payload["from_address"] = (form.get("from_address") or "").strip()
            payload["to_address"] = (form.get("to_address") or "").strip()
            payload["move_date"] = (form.get("move_date") or "").strip()
            payload["property_type"] = (form.get("property_type") or "").strip()
            payload["property_size"] = (form.get("property_size") or "").strip()

        tdir = _tenant_dir(tenant)
        # hard dedupe: prevents rapid double-click / multi-submit storms before cookies update
        fp = _intake_fingerprint(payload)
        if not _intake_dedupe_claim(tdir, fp):
            return _redirect(self, "/thanks?dup=1")

        cmd_path = write_command(tdir, "create_lead", payload)
        process_command_file(tdir, cmd_path)

        # mark token used
        self.send_response(302)
        _set_cookie(self, "intake_token_used", token or str(int(time.time())), "/")
        self.send_header("Location", "/thanks")
        self.end_headers()

    def _handle_new_tenant(self, form: Dict[str, Any]) -> None:
        # admin only
        sess = _require_session(self, "owner")
        if not sess:
            return _redirect(self, "/admin/login")

        tenant_id = (form.get("tenant_id") or "").strip()
        name = (form.get("name") or "").strip()
        category = (form.get("category") or "").strip() or "moving_company"
        username = (form.get("username") or "").strip()
        password = (form.get("password") or "").strip()

        if not tenant_id or not name or not username or not password:
            return _redirect(self, "/admin?tab=clients&flash=Missing%20fields")

        tdir = os.path.join(TENANTS_DIR, tenant_id)
        if os.path.exists(tdir):
            return _redirect(self, "/admin?tab=clients&flash=Tenant%20already%20exists")

        _create_tenant_demo(tenant_id, name, category=category)
        upsert_user(USERS_PATH, {
            "username": username,
            "password_hash": hash_password(password),
            "role": "client_owner",
            "tenant_id": tenant_id,
            "display_name": f"{name} Owner"
        })
        return _redirect(self, "/admin?tab=clients&flash=Tenant%20created")

def _login_redirect(kind: str):
    return _login_page(kind, "Please login.")

def main():
    _boot_seed()
    host = "0.0.0.0"
    port = int(os.environ.get("PORT","8000"))
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"[RUN] {APP_NAME}")
    print(f"[RUN] http://127.0.0.1:{port}/  (public site)")
    print(f"[RUN] http://127.0.0.1:{port}/admin/login  (admin portal)")
    print(f"[RUN] http://127.0.0.1:{port}/client/login (client portal)")
    print("")
    print("[CREDS] admin portal: admin / admin")
    print("[CREDS] client portal: DemenageursPlus / admin")
    httpd.serve_forever()

if __name__ == "__main__":
    main()
