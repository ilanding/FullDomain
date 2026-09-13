import asyncio
import hashlib
import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramBadRequest

from config import load_config
from storage import Store
from utils import base_html_name, names, valid_domain
from spaceship import Spaceship
from cloudflare import Cloudflare

WORKERS_SUBDOMAIN = os.getenv("CF_WORKERS_SUBDOMAIN", "websiz.workers.dev").strip().rstrip("/")

cfg = load_config()
bot = Bot(cfg.bot_token)
dp = Dispatcher()
store = Store(cfg)
cf = Cloudflare(cfg)
sp = Spaceship(cfg)
sess = {}


# -------------------- UI helpers --------------------

async def safe_edit_text(message, text, reply_markup=None, **kwargs):
    try:
        return await message.edit_text(text, reply_markup=reply_markup, **kwargs)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            return message
        raise


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def two_col(buttons):
    return [buttons[i:i + 2] for i in range(0, len(buttons), 2)]


def ok(x):
    return x.from_user.id in cfg.admins


def project(pid):
    return store.data.setdefault("projects", {}).get(pid)


def project_title(pid):
    p = project(pid) or {}
    return p.get("name", pid)


def project_domains(pid, active_only=False):
    out = []
    for d, x in store.data.get("domains", {}).items():
        if x.get("project_id") != pid:
            continue
        if active_only and x.get("status") != "active":
            continue
        out.append((d, x))
    return sorted(out)


def project_sites(pid):
    return sorted(
        [x for x in store.data.get("websites", {}).values() if x.get("project_id") == pid],
        key=lambda x: x.get("original_filename", "").lower(),
    )


def project_deployments(pid):
    return sorted(
        [x for x in store.data.get("deployments", {}).values() if x.get("project_id") == pid],
        key=lambda x: x.get("created_at", 0),
        reverse=True,
    )


async def menu(m):
    await m.answer(
        "🏠 Main Menu",
        reply_markup=kb([
            [InlineKeyboardButton(text="➕ Create Project", callback_data="pnew")],
            [InlineKeyboardButton(text="📁 My Projects", callback_data="plist")],
            [InlineKeyboardButton(text="🌐 My Domains", callback_data="mydomains")],
        ]),
    )


async def project_menu(message, pid):
    p = project(pid)
    if not p:
        return await safe_edit_text(message, "❌ Project not found.")
    ds = project_domains(pid)
    sites = project_sites(pid)
    deps = project_deployments(pid)
    text = (
        f"📁 {p['name']}\n\n"
        f"🌐 Domains: {len(ds)}\n"
        f"💻 Websites: {len(sites)}\n"
        f"🚀 Deployments: {len(deps)}\n"
        f"📧 Email Routing: {len(p.get('email_routing', []))}\n"
    )
    await safe_edit_text(message, text, reply_markup=kb([
        [InlineKeyboardButton(text="🌐 Domains", callback_data=f"pd:{pid}")],
        [InlineKeyboardButton(text="💻 Website", callback_data=f"pw:{pid}")],
        [InlineKeyboardButton(text="🚀 Deployments", callback_data=f"pv:{pid}")],
        [InlineKeyboardButton(text="📧 Email Routing", callback_data=f"pe:{pid}")],
        [InlineKeyboardButton(text="⬅️ My Projects", callback_data="plist")],
    ]))


# -------------------- Spaceship helpers --------------------

async def spaceship_domain_info(domain):
    url = f"{cfg.sp_base}/domains/{domain}"
    headers = {"X-API-Key": cfg.sp_key, "X-API-Secret": cfg.sp_secret, "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, headers=headers)
    if r.status_code == 404:
        return None
    if r.status_code >= 400:
        raise RuntimeError(f"Spaceship domain verification {r.status_code}: {r.text[:500]}")
    return r.json()


def domain_is_registered(info):
    if not isinstance(info, dict):
        return False
    lifecycle = str(info.get("lifecycleStatus", "")).lower()
    verification = str(info.get("verificationStatus", "")).lower()
    return lifecycle in {"registered", "active"} or verification == "success"


async def finish_registration(dom, x):
    if not x.get("registration_confirmed"):
        x["registration_confirmed"] = True
        x["registration_confirmed_at"] = time.time()
    x["registration_status"] = "registered"

    if x.get("zone_id"):
        return True
    try:
        z = await cf.zone_create(dom)
        hosts = z.get("name_servers", [])
        x.update(
            status="pending",
            zone_id=z["id"],
            nameservers=hosts,
            last_error=None,
            cloudflare_error=None,
        )
        if hosts:
            try:
                await sp.nameservers(dom, hosts)
            except Exception as e:
                x["cloudflare_error"] = f"Nameserver update failed: {e}"
                x["last_error"] = x["cloudflare_error"]
        return True
    except Exception as e:
        x["status"] = "registered_no_cloudflare"
        x["cloudflare_error"] = str(e)
        x["last_error"] = str(e)
        return False


# -------------------- Spaceship contact --------------------

HARD_CODED_CONTACT_DETAILS = {
    "firstName": "Shubham",
    "lastName": "Raj",
    "email": "Testnewpremium@gmail.com",
    "phone": "+91.8294647843",
    "address1": "New Delhi",
    "city": "Than Singh Nagar",
    "stateProvince": "DL",
    "postalCode": "110005",
    "country": "IN",
}


async def create_spaceship_contact(details=None):
    headers = {"X-API-Key": cfg.sp_key, "X-API-Secret": cfg.sp_secret, "Content-Type": "application/json"}
    body = dict(details or HARD_CODED_CONTACT_DETAILS)
    body["country"] = "IN"
    async with httpx.AsyncClient(timeout=45) as client:
        r = await client.put(f"{cfg.sp_base}/contacts", headers=headers, json=body)
    if r.status_code >= 400:
        raise RuntimeError(f"Spaceship {r.status_code}: {r.text[:500]}")
    data = r.json()
    contact_id = data.get("contactId")
    if not contact_id:
        raise RuntimeError(f"Spaceship did not return contactId: {data}")
    return contact_id


async def get_or_create_spaceship_contact():
    settings = store.data.setdefault("settings", {})
    contacts = settings.get("spaceship_contacts")
    if contacts and contacts.get("registrant"):
        return contacts
    contact_id = await create_spaceship_contact()
    contacts = {"registrant": contact_id, "admin": contact_id, "tech": contact_id, "billing": contact_id}
    settings["spaceship_contact_id"] = contact_id
    settings["spaceship_contacts"] = contacts
    await store.save("Create/save Spaceship contact")
    return contacts


# -------------------- Start / Projects --------------------

@dp.message(Command("start"))
async def start(m):
    if ok(m):
        await menu(m)


@dp.callback_query(F.data == "pnew")
async def pnew(c):
    sess[c.from_user.id] = {"s": "pname"}
    await safe_edit_text(c.message, "📝 Project name bhejo:")
    await c.answer()


@dp.callback_query(F.data == "plist")
async def plist(c):
    projects = store.data.get("projects", {})
    if not projects:
        return await safe_edit_text(c.message, "📁 No projects yet.", reply_markup=kb([
            [InlineKeyboardButton(text="➕ Create Project", callback_data="pnew")],
            [InlineKeyboardButton(text="⬅️ Home", callback_data="home")],
        ]))
    rows = []
    for pid, p in sorted(projects.items(), key=lambda q: q[1].get("name", "").lower()):
        name = p.get("name", pid)
        rows.append([
            InlineKeyboardButton(text=f"👁 View — {name}", callback_data=f"viewp:{pid}"),
            InlineKeyboardButton(text="🗑 Delete", callback_data=f"delp:{pid}"),
        ])
    rows.append([InlineKeyboardButton(text="⬅️ Home", callback_data="home")])
    await safe_edit_text(c.message, "📁 My Projects", reply_markup=kb(rows))
    await c.answer()


@dp.callback_query(F.data == "home")
async def home(c):
    await safe_edit_text(c.message, "🏠 Main Menu", reply_markup=kb([
        [InlineKeyboardButton(text="➕ Create Project", callback_data="pnew")],
        [InlineKeyboardButton(text="📁 My Projects", callback_data="plist")],
        [InlineKeyboardButton(text="🌐 My Domains", callback_data="mydomains")],
    ]))
    await c.answer()


@dp.callback_query(F.data.startswith("viewp:"))
async def viewp(c):
    pid = c.data[6:]
    sess[c.from_user.id] = {"s": "project", "project": pid}
    await project_menu(c.message, pid)
    await c.answer()


@dp.callback_query(F.data.startswith("delp:"))
async def delp(c):
    pid = c.data[5:]
    p = project(pid)
    if not p:
        return await c.answer("Project not found", show_alert=True)
    sess[c.from_user.id] = {"s": "delete_project", "project": pid}
    await safe_edit_text(c.message,
        f"⚠️ Delete project?\n\n📁 {p.get('name', pid)}\n\nProject record aur uske linked records delete honge. Spaceship/Cloudflare resources automatically delete nahi kiye jayenge.",
        reply_markup=kb([
            [InlineKeyboardButton(text="⚠️ Yes, Delete", callback_data="delp_yes")],
            [InlineKeyboardButton(text="❌ Cancel", callback_data="plist")],
        ]))
    await c.answer()


@dp.callback_query(F.data == "delp_yes")
async def delp_yes(c):
    s = sess.get(c.from_user.id, {})
    pid = s.get("project")
    p = project(pid)
    if not p:
        return await c.answer("Project not found", show_alert=True)
    # Remove websites, deployments, domains, email records linked to project.
    for k in ["websites", "deployments", "domains"]:
        for key, item in list(store.data.get(k, {}).items()):
            if item.get("project_id") == pid:
                store.data[k].pop(key, None)
    store.data.get("projects", {}).pop(pid, None)
    await store.save("Delete project")
    sess.pop(c.from_user.id, None)
    await safe_edit_text(c.message, "✅ Project deleted.", reply_markup=kb([
        [InlineKeyboardButton(text="📁 My Projects", callback_data="plist")],
        [InlineKeyboardButton(text="⬅️ Home", callback_data="home")],
    ]))
    await c.answer()


@dp.message(F.text)
async def text_message(m):
    s = sess.get(m.from_user.id, {})
    st = s.get("s")

    if st == "pname":
        name = (m.text or "").strip()
        if not name:
            return await m.answer("❌ Project name required.")
        pid = uuid.uuid4().hex[:10]
        store.data.setdefault("projects", {})[pid] = {
            "id": pid,
            "name": name,
            "websites": [],
            "email_routing": [],
            "created_at": time.time(),
        }
        await store.save("Create project")
        sess[m.from_user.id] = {"s": "project", "project": pid}
        await m.answer(f"✅ Project created: {name}")
        await project_menu(await m.answer("Loading project..."), pid)
        return

    if st == "buynames":
        pid = s.get("project")
        cand = [n + t for n in names(m.text or "") for t in cfg.tlds]
        out = []
        for i in range(0, len(cand), 20):
            r = await sp.availability(cand[i:i + 20])
            items = r if isinstance(r, list) else r.get("domains", r.get("results", []))
            for q in items:
                if q.get("result") == "available":
                    out.append((q["domain"], q.get("price")))
        sess[m.from_user.id] = {"s": "buylist", "items": out, "project": pid}
        if not out:
            return await m.answer("❌ No available domains found.")
        buttons = [InlineKeyboardButton(text=f"💰 {d}" + (f" — {p}" if p else ""), callback_data="b:" + d) for d, p in out]
        rows = two_col(buttons)
        rows.append([InlineKeyboardButton(text="✅ Done", callback_data="donebuy")])
        rows.append([InlineKeyboardButton(text="⬅️ Project", callback_data=f"viewp:{pid}")])
        await m.answer("Available domains:", reply_markup=kb(rows))
        return

    if st == "email_destination":
        pid = s.get("project")
        email = (m.text or "").strip()
        if "@" not in email:
            return await m.answer("❌ Valid Gmail/destination email bhejo.")
        try:
            # Reuse an existing Cloudflare destination if it already exists.
            existing = await _cf_email_destination_info(email)
            if existing:
                settings = store.data.setdefault("settings", {})
                settings["email_destination"] = email
                settings["email_destination_id"] = existing.get("id")
                await store.save("Use existing email destination")
                sess[m.from_user.id] = {"s": "project", "project": pid}
                if existing.get("verified"):
                    msg = f"✅ Destination already verified hai: {email}\n\nAb Email Routing me Setup All kar sakte ho."
                else:
                    msg = f"⏳ Destination mil gaya, lekin abhi verified nahi hai: {email}\n\nCloudflare verification email verify karo, phir Check Verification dabao."
                await m.answer(msg, reply_markup=kb([[InlineKeyboardButton(text="📧 Email Routing", callback_data=f"pe:{pid}")]]))
                return

            addr = await cf.email_destination(email)
            store.data.setdefault("settings", {})["email_destination"] = email
            store.data["settings"]["email_destination_id"] = addr.get("id")
            await store.save("Email destination created")
            sess[m.from_user.id] = {"s": "project", "project": pid}
            await m.answer(
                f"📧 Destination added: {email}\n\nCloudflare verification email bhejega.\nVerification complete hone ke baad project me Email Routing → Check Verification & Setup All dabao.",
                reply_markup=kb([[InlineKeyboardButton(text="📧 Email Routing", callback_data=f"pe:{pid}")]])
            )
        except Exception as e:
            await m.answer(f"❌ Destination create/check nahi hua:\n{e}")
        return

    if st == "email_prefix":
        pid = s.get("project")
        prefix = re.sub(r"[^a-zA-Z0-9._-]", "", (m.text or "").strip().lower())
        if not prefix:
            return await m.answer("❌ Prefix invalid.")
        sess[m.from_user.id] = {"s": "project", "project": pid}
        await m.answer("⏳ Verification check karke project ke saare active domains setup ho rahe hain...")
        await bulk_email_setup(m, pid, prefix)
        return


# -------------------- Project domains --------------------

@dp.callback_query(F.data.startswith("pd:"))
async def pd(c):
    pid = c.data[3:]
    if not project(pid):
        return await c.answer("Project not found", show_alert=True)
    rows = [[InlineKeyboardButton(text="🛒 Buy Domains", callback_data=f"buy:{pid}")]]
    for d, x in project_domains(pid):
        status = x.get("status", "unknown")
        icon = "🟢" if status == "active" else "🟡" if status == "pending" else "🟠" if status == "registered_no_cloudflare" else "🔴"
        rows.append([InlineKeyboardButton(text=f"{icon} {d}", callback_data=f"dinfo:{pid}:{d}")])
    rows.append([InlineKeyboardButton(text="🔄 Check Status", callback_data=f"pcheck:{pid}")])
    rows.append([InlineKeyboardButton(text="⬅️ Project", callback_data=f"viewp:{pid}")])
    await safe_edit_text(c.message, f"🌐 {project_title(pid)} — Domains", reply_markup=kb(rows))
    await c.answer()


@dp.callback_query(F.data.startswith("buy:"))
async def buy_start(c):
    pid = c.data[4:]
    if not project(pid):
        return await c.answer("Project not found", show_alert=True)
    sess[c.from_user.id] = {"s": "buynames", "project": pid}
    await safe_edit_text(c.message, "🛒 Base names bhejo: sonu monu raju")
    await c.answer()


@dp.callback_query(F.data.startswith("dinfo:"))
async def dinfo(c):
    _, pid, dom = c.data.split(":", 2)
    d = store.data.get("domains", {}).get(dom)
    if not d:
        return await c.answer("Domain not found", show_alert=True)
    text = (
        f"🌐 {dom}\n\n"
        f"Status: {d.get('status')}\n"
        f"Website: {d.get('website_id') or 'Not assigned'}\n"
        f"Nameservers: {', '.join(d.get('nameservers') or []) or '—'}"
    )
    await safe_edit_text(c.message, text, reply_markup=kb([[InlineKeyboardButton(text="⬅️ Domains", callback_data=f"pd:{pid}")]]))
    await c.answer()


@dp.callback_query(F.data.startswith("pcheck:"))
async def pcheck(c):
    pid = c.data[7:]
    changed = await check_project_domains(pid)
    if changed:
        await store.save("Project domain status check")
    await pd_render(c.message, pid, "🔄 Status refreshed")
    await c.answer("Status refreshed")


async def pd_render(message, pid, prefix=None):
    rows = [[InlineKeyboardButton(text="🛒 Buy Domains", callback_data=f"buy:{pid}")]]
    for d, x in project_domains(pid):
        status = x.get("status", "unknown")
        icon = "🟢" if status == "active" else "🟡" if status == "pending" else "🟠" if status == "registered_no_cloudflare" else "🔴"
        rows.append([InlineKeyboardButton(text=f"{icon} {d}", callback_data=f"dinfo:{pid}:{d}")])
    rows.append([InlineKeyboardButton(text="🔄 Check Status", callback_data=f"pcheck:{pid}")])
    rows.append([InlineKeyboardButton(text="⬅️ Project", callback_data=f"viewp:{pid}")])
    await safe_edit_text(message, ((prefix + "\n\n") if prefix else "") + f"🌐 {project_title(pid)} — Domains", reply_markup=kb(rows))


async def check_project_domains(pid):
    changed = False
    for dom, x in project_domains(pid):
        status = x.get("status")
        if status == "registered_no_cloudflare" and not x.get("zone_id"):
            before = json.dumps(x, sort_keys=True, default=str)
            await finish_registration(dom, x)
            changed |= before != json.dumps(x, sort_keys=True, default=str)
        elif status == "registration_pending" and not x.get("zone_id"):
            op = x.get("operation_id")
            if op:
                try:
                    r = await sp.operation(op)
                    rs = str(r.get("status", "pending")).lower()
                    if rs == "success":
                        await finish_registration(dom, x)
                        changed = True
                    elif rs == "failed":
                        x["status"] = "error"
                        x["last_error"] = str(r.get("error") or r.get("message") or r)
                        changed = True
                except Exception as e:
                    x["last_error"] = str(e)
            try:
                info = await spaceship_domain_info(dom)
                if domain_is_registered(info):
                    await finish_registration(dom, x)
                    changed = True
            except Exception:
                pass
        elif status == "pending" and x.get("zone_id"):
            try:
                z = await cf.zone_get(x["zone_id"])
                new = z.get("status", "pending")
                if new != x.get("status"):
                    x["status"] = new
                    changed = True
            except Exception as e:
                x["last_error"] = str(e)
    return changed


# -------------------- Domain purchase --------------------

@dp.callback_query(F.data.startswith("b:"))
async def b(c):
    dom = c.data[2:]
    s = sess.get(c.from_user.id, {})
    if not any(d == dom for d, _ in s.get("items", [])):
        return await c.answer("Already removed", show_alert=True)
    price = next((p for d, p in s["items"] if d == dom), None)
    s["items"] = [x for x in s["items"] if x[0] != dom]
    s["pending_buy"] = dom
    s["pending_price"] = price
    s["s"] = "confirm"
    await safe_edit_text(c.message,
        f"🛒 {dom}\n💵 Price: {price or 'shown by Spaceship'}\n\nConfirm purchase?",
        reply_markup=kb([
            [InlineKeyboardButton(text="✅ Confirm Purchase", callback_data="confirm")],
            [InlineKeyboardButton(text="❌ Cancel", callback_data="cancel")],
        ]))
    await c.answer()


async def restore_buy_list(c):
    s = sess.get(c.from_user.id, {})
    rows = two_col([InlineKeyboardButton(text=f"💰 {d}" + (f" — {p}" if p else ""), callback_data="b:" + d) for d, p in s.get("items", [])])
    rows.append([InlineKeyboardButton(text="✅ Done", callback_data="donebuy")])
    rows.append([InlineKeyboardButton(text="⬅️ Domains", callback_data=f"pd:{s.get('project')}")])
    await safe_edit_text(c.message, "Available domains:", reply_markup=kb(rows))


@dp.callback_query(F.data == "cancel")
async def cancel(c):
    s = sess.get(c.from_user.id, {})
    if s.get("pending_buy"):
        s.setdefault("items", []).append((s["pending_buy"], s.get("pending_price")))
    s["items"] = sorted(s.get("items", []), key=lambda x: x[0])
    s["s"] = "buylist"
    s.pop("pending_buy", None)
    s.pop("pending_price", None)
    await restore_buy_list(c)
    await c.answer("Cancelled")


@dp.callback_query(F.data == "confirm")
async def confirm(c):
    s = sess.get(c.from_user.id, {})
    dom = s.get("pending_buy")
    pid = s.get("project")
    if not dom or not pid:
        return await c.answer("Purchase session expired", show_alert=True)
    try:
        contacts = await get_or_create_spaceship_contact()
        r = await sp.register(dom, contacts)
        op = r.headers.get("spaceship-async-operationid")
        store.data.setdefault("domains", {})[dom] = {
            "domain": dom,
            "registrar": "spaceship",
            "status": "registration_pending",
            "operation_id": op,
            "assigned": False,
            "project_id": pid,
            "created_at": time.time(),
        }
        await store.save("Purchase submitted")
        await safe_edit_text(c.message, f"⏳ {dom} registration submitted.\n\nProject: {project_title(pid)}\nUse Domains → Check Status.", reply_markup=kb([[InlineKeyboardButton(text="🔄 Check Status", callback_data=f"pcheck:{pid}")]]))
    except Exception as e:
        s.setdefault("items", []).append((dom, s.get("pending_price")))
        s["items"] = sorted(s["items"], key=lambda x: x[0])
        await safe_edit_text(c.message, f"❌ Purchase request failed:\n{e}")
    s["s"] = "buylist"
    s.pop("pending_buy", None)
    s.pop("pending_price", None)
    await c.answer()


@dp.callback_query(F.data == "donebuy")
async def donebuy(c):
    pid = sess.get(c.from_user.id, {}).get("project")
    sess[c.from_user.id] = {"s": "project", "project": pid} if pid else {}
    await safe_edit_text(c.message, "✅ Purchase session ended.", reply_markup=kb([[InlineKeyboardButton(text="⬅️ Project", callback_data=f"viewp:{pid}")]]))
    await c.answer()


# -------------------- My Domains --------------------

@dp.callback_query(F.data == "mydomains")
async def mydomains(c):
    rows = []
    for d, x in sorted(store.data.get("domains", {}).items()):
        pid = x.get("project_id")
        p = project(pid) if pid else None
        label = f"{d} — {p.get('name') if p else 'Unassigned'}"
        rows.append([InlineKeyboardButton(text=label[:60], callback_data=f"gd:{d}")])
    rows.append([InlineKeyboardButton(text="⬅️ Home", callback_data="home")])
    await safe_edit_text(c.message, "🌐 My Domains", reply_markup=kb(rows))
    await c.answer()


@dp.callback_query(F.data.startswith("gd:"))
async def gd(c):
    dom = c.data[3:]
    x = store.data.get("domains", {}).get(dom)
    if not x:
        return await c.answer("Not found", show_alert=True)
    pid = x.get("project_id")
    await safe_edit_text(c.message, f"🌐 {dom}\n\nStatus: {x.get('status')}\nProject: {project_title(pid) if pid else 'Unassigned'}", reply_markup=kb([
        [InlineKeyboardButton(text="⬅️ My Domains", callback_data="mydomains")],
        [InlineKeyboardButton(text="🏠 Home", callback_data="home")],
    ]))
    await c.answer()


# -------------------- Website / Pages --------------------

@dp.callback_query(F.data.startswith("pw:"))
async def pw(c):
    pid = c.data[3:]
    if not project(pid):
        return await c.answer("Project not found", show_alert=True)
    # Website is an action area only. Uploaded HTML is never shown as a
    # separate list here. Successfully deployed websites live under Deployments.
    rows = [
        [InlineKeyboardButton(text="📤 Upload HTML", callback_data=f"uploadhtml:{pid}")],
        [InlineKeyboardButton(text="🚀 View Deployments", callback_data=f"pv:{pid}")],
        [InlineKeyboardButton(text="⬅️ Project", callback_data=f"viewp:{pid}")],
    ]
    await safe_edit_text(
        c.message,
        f"💻 Websites — {project_title(pid)}\n\n📤 HTML upload karo.\n\nSuccessfully deployed/live websites Deployments ke andar dikhengi.",
        reply_markup=kb(rows),
    )
    await c.answer()


@dp.callback_query(F.data.startswith("uploadhtml:"))
async def uploadhtml(c):
    pid = c.data.split(":", 1)[1]
    sess[c.from_user.id] = {"s": "html", "project": pid}
    await safe_edit_text(c.message, "📤 HTML file(s) upload karo.\n\nHar uploaded HTML ko Worker ke andar index.html content ke roop me deploy kiya jayega.")
    await c.answer()


@dp.callback_query(F.data.startswith("site:"))
async def site(c):
    _, pid, wid = c.data.split(":", 2)
    w = store.data.get("websites", {}).get(wid)
    if not w:
        return await c.answer("Website not found", show_alert=True)
    text = f"📄 {w.get('original_filename')}\n\nStatus: {w.get('status')}"
    rows = []
    if w.get("status") == "deployed":
        if not w.get("domain"):
            rows.append([InlineKeyboardButton(text="🌐 Assign Domain", callback_data=f"assignsite:{pid}:{wid}")])
        else:
            text += f"\n\n🌐 Assigned Domain: https://{w['domain']}"
        if w.get("worker_url"):
            text += f"\n☁️ Worker: {w['worker_url']}"
        rows.append([InlineKeyboardButton(text="✏️ Change HTML", callback_data=f"changehtml:{pid}:{wid}")])
    elif w.get("status") in {"uploaded", "deploy_failed"}:
        rows.append([InlineKeyboardButton(text="✏️ Upload/Replace HTML", callback_data=f"changehtml:{pid}:{wid}")])
    rows.append([InlineKeyboardButton(text="⬅️ Website", callback_data=f"pw:{pid}")])
    await safe_edit_text(c.message, text, reply_markup=kb(rows))
    await c.answer()


@dp.callback_query(F.data.startswith("changehtml:"))
async def changehtml(c):
    _, pid, wid = c.data.split(":", 2)
    w = store.data.get("websites", {}).get(wid)
    if not w or w.get("project_id") != pid:
        return await c.answer("Website not found", show_alert=True)
    sess[c.from_user.id] = {"s": "replace_html", "project": pid, "website": wid}
    await safe_edit_text(c.message, "✏️ New HTML file bhejo.\n\nYe isi Worker/subdomain ko update karega; naya subdomain nahi banega.", reply_markup=kb([[InlineKeyboardButton(text="⬅️ Website", callback_data=f"site:{pid}:{wid}")]]))
    await c.answer()


async def workers_script_exists(script_name):
    url = f"https://api.cloudflare.com/client/v4/accounts/{cfg.cf_account}/workers/scripts/{script_name}"
    headers = {"Authorization": f"Bearer {cfg.cf_token}"}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, headers=headers)
    if r.status_code == 404:
        return False
    if r.status_code >= 400:
        raise RuntimeError(f"Cloudflare Workers {r.status_code}: {r.text[:800]}")
    return True


async def choose_worker_name(base):
    base = re.sub(r"[^a-z0-9-]", "-", base.lower()).strip("-") or "site"
    base = base[:63]
    for i in range(0, 1000):
        candidate = base if i == 0 else f"{base}{i}"
        if not await workers_script_exists(candidate):
            return candidate
    raise RuntimeError(f"No available Worker name found for {base}")


def worker_source_from_html(html_bytes):
    """Wrap raw HTML in a classic Cloudflare Worker script.

    We intentionally use the classic Worker format here instead of an ES-module
    `export default` script.  The single-script Workers API accepts this form
    reliably and the HTML itself is kept as base64 data, so any JS/CSS inside
    the uploaded HTML cannot break Worker parsing.
    """
    encoded = base64.b64encode(html_bytes).decode("ascii")
    source = (
        'const DATA = "' + encoded + '";\n'
        'function html() { const bytes = Uint8Array.from(atob(DATA), c => c.charCodeAt(0)); return new TextDecoder().decode(bytes); }\n'
        'addEventListener("fetch", event => { event.respondWith(new Response(html(), { status: 200, headers: { "content-type": "text/html; charset=UTF-8", "cache-control": "no-cache" } })); });\n'
    )
    return source.encode("utf-8")


async def deploy_worker(script_name, html_path):
    html_bytes = Path(html_path).read_bytes()
    source = worker_source_from_html(html_bytes)
    url = f"https://api.cloudflare.com/client/v4/accounts/{cfg.cf_account}/workers/scripts/{script_name}"
    headers = {
        "Authorization": f"Bearer {cfg.cf_token}",
        "Content-Type": "application/javascript",
    }
    # Upload a classic Worker script directly. This avoids the multipart/module
    # parser interpreting `export default` as invalid classic Worker syntax.
    async with httpx.AsyncClient(timeout=90) as client:
        r = await client.put(url, headers=headers, content=source)
        if r.status_code >= 400:
            raise RuntimeError(f"Cloudflare Worker deploy {r.status_code}: {r.text[:1200]}")
        sr = await client.post(
            f"{url}/subdomain",
            headers={**headers, "Content-Type": "application/json"},
            json={"enabled": True, "previews_enabled": False},
        )
        if sr.status_code >= 400:
            raise RuntimeError(f"Cloudflare workers.dev enable {sr.status_code}: {sr.text[:1200]}")
    return f"https://{script_name}.{WORKERS_SUBDOMAIN}"


async def verify_worker_live(url, attempts=6, delay=2):
    """Confirm the public workers.dev URL is actually serving before calling it live."""
    last_error = "unknown"
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        for attempt in range(attempts):
            try:
                r = await client.get(url, headers={"Cache-Control": "no-cache"})
                ctype = (r.headers.get("content-type") or "").lower()
                if 200 <= r.status_code < 300 and ("text/html" in ctype or r.text):
                    return True
                last_error = f"HTTP {r.status_code}"
            except Exception as e:
                last_error = str(e)
            if attempt < attempts - 1:
                await asyncio.sleep(delay)
    raise RuntimeError(f"Worker deploy hua, lekin public URL live verify nahi hua ({last_error})")


async def deploy_website(w):
    worker_name = w.get("worker_name")
    if not worker_name:
        worker_name = await choose_worker_name(w["base_name"])
    # The Workers API response is the deployment confirmation. Do NOT make a
    # public HTTP request here: a newly-created workers.dev hostname can take
    # a few minutes to propagate and may temporarily return 404.
    url = await deploy_worker(worker_name, w["path"])
    w["worker_name"] = worker_name
    w["worker_url"] = url
    w["pages_project"] = None
    w["pages_url"] = None
    w["status"] = "deployed"
    w["deployed_at"] = time.time()
    w["last_error"] = None
    return url, "Cloudflare Worker deployment accepted"


@dp.message(F.document)
async def document_upload(m):
    s = sess.get(m.from_user.id, {})
    mode = s.get("s")
    if mode not in {"html", "replace_html"}:
        await m.answer("📄 HTML receive hua, lekin upload mode active nahi hai.\n\nProject → Website → 📤 Upload HTML se upload karo.")
        return
    pid = s.get("project")
    if not pid or not project(pid):
        return await m.answer("❌ Project session expired.")
    try:
        base = base_html_name(m.document.file_name or "")
    except ValueError:
        return await m.answer("❌ Sirf .html file upload karo.")
    Path("runtime").mkdir(exist_ok=True)
    f = await bot.get_file(m.document.file_id)
    p = Path("runtime") / (uuid.uuid4().hex + ".html")
    await bot.download_file(f.file_path, p)

    if mode == "replace_html":
        wid = s.get("website")
        w = store.data.get("websites", {}).get(wid)
        if not w or w.get("project_id") != pid:
            return await m.answer("❌ Website not found.")
        old_path = w.get("path")
        w["path"] = str(p)
        w["original_filename"] = m.document.file_name
        w["base_name"] = base
        await m.answer(f"✏️ {m.document.file_name} received.\n⏳ Existing Worker update ho raha hai...")
        try:
            url, _ = await deploy_website(w)
            for dep in store.data.get("deployments", {}).values():
                if dep.get("website_id") == wid and dep.get("status") == "live":
                    dep["status"] = "archived"
            history_count = len([x for x in store.data.get("deployments", {}).values() if x.get("website_id") == wid])
            depid = str(uuid.uuid4())
            store.data.setdefault("deployments", {})[depid] = {
                "id": depid, "project_id": pid, "website_id": wid,
                "worker_name": w.get("worker_name"), "worker_url": url,
                "original_filename": w.get("original_filename"),
                "domain": w.get("domain"), "status": "live", "version": history_count + 1,
                "created_at": time.time(),
            }
            await store.save("HTML website updated")
            sess[m.from_user.id] = {"s": "project", "project": pid}
            await m.answer(f"✅ Website updated\n\n📄 {m.document.file_name}\n🌐 {url}\n\nSame Worker/subdomain par update hua hai.", reply_markup=kb([
                [InlineKeyboardButton(text="🌐 Assign/Change Domain", callback_data=f"assignsite:{pid}:{wid}")],
                [InlineKeyboardButton(text="⬅️ Website", callback_data=f"pw:{pid}")],
            ]))
        except Exception as e:
            w["path"] = old_path or str(p)
            w["last_error"] = str(e)
            await store.save("HTML website update failed")
            await m.answer(f"❌ Website update failed:\n{e}")
        return

    wid = str(uuid.uuid4())
    w = {
        "id": wid, "project_id": pid, "original_filename": m.document.file_name,
        "base_name": base, "path": str(p), "domain": None,
        "worker_name": None, "worker_url": None, "pages_project": None,
        "pages_url": None, "status": "uploaded", "created_at": time.time(),
    }
    store.data.setdefault("websites", {})[wid] = w
    store.data["projects"][pid].setdefault("websites", []).append(wid)
    await m.answer(f"📤 {m.document.file_name} received.\n⏳ Cloudflare Workers par deploy ho raha hai...")
    try:
        url, _ = await deploy_website(w)
        depid = str(uuid.uuid4())
        store.data.setdefault("deployments", {})[depid] = {
            "id": depid, "project_id": pid, "website_id": wid,
            "worker_name": w.get("worker_name"), "worker_url": url,
            "original_filename": w.get("original_filename"), "domain": w.get("domain"),
            "status": "live", "version": 1, "created_at": time.time(),
        }
        await store.save("HTML deployed to Cloudflare Workers")
        sess[m.from_user.id] = {"s": "html", "project": pid}
        await m.answer(f"🚀 Cloudflare Worker deploy ho gaya!\n\n📄 {m.document.file_name}\n🌐 {url}\n\n⚠️ workers.dev URL ko active hone me kuch minutes lag sakte hain.\n\nAb custom domain assign karo ya next HTML upload karo:", reply_markup=kb([
            [InlineKeyboardButton(text="🌐 Assign Domain", callback_data=f"assignsite:{pid}:{wid}")],
            [InlineKeyboardButton(text="📤 Upload Another HTML", callback_data=f"uploadhtml:{pid}")],
            [InlineKeyboardButton(text="⬅️ Project", callback_data=f"viewp:{pid}")],
        ]))
    except Exception as e:
        w["status"] = "deploy_failed"
        w["last_error"] = str(e)
        await store.save("HTML deployment failed")
        await m.answer(f"❌ Cloudflare Workers deployment failed:\n{e}")


async def attach_worker_custom_domain(worker_name, domain, zone_id):
    if not zone_id:
        raise RuntimeError("Cloudflare zone_id missing for domain")
    url = f"https://api.cloudflare.com/client/v4/accounts/{cfg.cf_account}/workers/domains"
    headers = {"Authorization": f"Bearer {cfg.cf_token}", "Content-Type": "application/json"}
    payload = {"hostname": domain, "service": worker_name, "zone_id": zone_id, "zone_name": domain}
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.put(url, headers=headers, json=payload)
    if r.status_code >= 400:
        raise RuntimeError(f"Cloudflare Worker custom domain {r.status_code}: {r.text[:1200]}")
    j = r.json()
    if not j.get("success", True):
        raise RuntimeError(str(j.get("errors")))
    return j.get("result")


# -------------------- Domain assignment --------------------

@dp.callback_query(F.data.startswith("assignsite:"))
async def assignsite(c):
    _, pid, wid = c.data.split(":", 2)
    w = store.data.get("websites", {}).get(wid)
    if not w:
        return await c.answer("Website not found", show_alert=True)
    # Only domains belonging to this project and not yet assigned to a website
    # are offered here. This keeps projects strictly separated.
    domains = [(d, x) for d, x in project_domains(pid, active_only=True) if not x.get("website_id")]
    if not domains:
        return await safe_edit_text(c.message, "❌ Is project me koi unassigned active domain nahi hai.", reply_markup=kb([[InlineKeyboardButton(text="⬅️ Website", callback_data=f"pw:{pid}")]]))
    rows = two_col([InlineKeyboardButton(text=d, callback_data=f"pickdomain:{pid}:{wid}:{d}") for d, _ in domains])
    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data=f"site:{pid}:{wid}")])
    await safe_edit_text(c.message, "🌐 Assign Domain\n\nActive unassigned domains:", reply_markup=kb(rows))
    await c.answer()


@dp.callback_query(F.data.startswith("pickdomain:"))
async def pickdomain(c):
    _, pid, wid, dom = c.data.split(":", 3)
    w = store.data.get("websites", {}).get(wid)
    d = store.data.get("domains", {}).get(dom)
    if not w or not d or d.get("status") != "active" or d.get("website_id"):
        return await c.answer("Domain unavailable", show_alert=True)
    if d.get("project_id") and d.get("project_id") != pid:
        return await c.answer("Ye domain kisi aur project me hai.", show_alert=True)
    try:
        await attach_worker_custom_domain(w["worker_name"], dom, d.get("zone_id"))
        w["domain"] = dom
        d["website_id"] = wid
        d["assigned"] = True
        d["project_id"] = pid
        w["custom_domain_url"] = f"https://{dom}"
        for dep in store.data.get("deployments", {}).values():
            if dep.get("website_id") == wid:
                dep["domain"] = dom
        await store.save("Assign custom domain")
        await safe_edit_text(c.message, f"✅ Domain assigned\n\n📄 {w['original_filename']}\n🌐 https://{dom}\n\nWorker: {w['worker_url']}", reply_markup=kb([[InlineKeyboardButton(text="💻 Website", callback_data=f"pw:{pid}")], [InlineKeyboardButton(text="⬅️ Project", callback_data=f"viewp:{pid}")]]))
    except Exception as e:
        await safe_edit_text(c.message, f"❌ Domain assign nahi hua:\n{e}", reply_markup=kb([[InlineKeyboardButton(text="⬅️ Website", callback_data=f"pw:{pid}")]]))
    await c.answer()


# -------------------- Deployments --------------------

async def reconcile_deployments():
    """Repair deployment records for websites created by an older bot version.

    If a website record says it is deployed and has a Worker URL, but no live
    deployment record exists, create the missing deployment record. This makes
    existing live websites appear under Deployments without redeploying them.
    """
    changed = False
    deployments = store.data.setdefault("deployments", {})
    for wid, w in store.data.get("websites", {}).items():
        if w.get("status") != "deployed" or not w.get("worker_url"):
            continue
        live = [d for d in deployments.values() if d.get("website_id") == wid and d.get("status") == "live"]
        if live:
            continue
        # Do not require a public HTTP check here. Existing workers can be
        # temporarily unavailable while workers.dev propagation completes.
        versions = [d for d in deployments.values() if d.get("website_id") == wid]
        depid = str(uuid.uuid4())
        deployments[depid] = {
            "id": depid,
            "project_id": w.get("project_id"),
            "website_id": wid,
            "worker_name": w.get("worker_name"),
            "worker_url": w.get("worker_url"),
            "original_filename": w.get("original_filename"),
            "domain": w.get("domain"),
            "status": "live",
            "version": len(versions) + 1,
            "created_at": w.get("deployed_at") or w.get("created_at") or time.time(),
            "reconciled": True,
        }
        changed = True
    if changed:
        await store.save("Reconcile existing live websites into deployments")
    return changed


@dp.callback_query(F.data.startswith("pv:"))
async def pv(c):
    pid = c.data[3:]
    if not project(pid):
        return await c.answer("Project not found", show_alert=True)
    await reconcile_deployments()
    deps = [d for d in project_deployments(pid) if d.get("status") == "live"]
    if not deps:
        text = "🚀 Deployments\n\nNo live/deployed websites yet."
        rows = [[InlineKeyboardButton(text="📤 Upload HTML", callback_data=f"uploadhtml:{pid}")], [InlineKeyboardButton(text="⬅️ Project", callback_data=f"viewp:{pid}")]]
    else:
        lines = ["🚀 Deployments", ""]
        rows = []
        seen = set()
        for d in deps:
            wid = d.get("website_id")
            if wid in seen:
                continue
            seen.add(wid)
            w = store.data.get("websites", {}).get(wid, {})
            domain = d.get("domain") or w.get("domain") or "Not assigned"
            lines.append(
                f"🌐 {d.get('worker_name')}\n"
                f"📄 {d.get('original_filename', w.get('original_filename', 'website.html'))}\n"
                f"🔗 {d.get('worker_url')}\n"
                f"Custom Domain: {domain}\n"
                f"Status: 🟢 Deployed\n"
            )
            rows.append([
                InlineKeyboardButton(text=f"✏️ Change HTML — {d.get('worker_name')}", callback_data=f"changehtml:{pid}:{wid}"),
                InlineKeyboardButton(text="🌐 Assign/Change Domain", callback_data=f"assignsite:{pid}:{wid}"),
            ])
        text = "\n".join(lines).rstrip()
        rows.append([InlineKeyboardButton(text="📤 Upload HTML", callback_data=f"uploadhtml:{pid}")])
        rows.append([InlineKeyboardButton(text="⬅️ Project", callback_data=f"viewp:{pid}")])
    await safe_edit_text(c.message, text, reply_markup=kb(rows))
    await c.answer()


# -------------------- Email Routing bulk --------------------

@dp.callback_query(F.data.startswith("pe:"))
async def pe(c):
    pid = c.data[3:]
    p = project(pid)
    if not p:
        return await c.answer("Project not found", show_alert=True)
    domains = [d for d, _ in project_domains(pid, active_only=True)]
    settings = store.data.setdefault("settings", {})
    dest = settings.get("email_destination")
    prefix = p.get("email_prefix")
    lines = [f"📧 Email Routing — {p['name']}", "", f"Active domains: {len(domains)}"]
    lines.append(f"Destination: {dest or 'Not set'}")
    lines.append(f"Prefix: {prefix or 'Not set'}")
    rows = []
    if not dest:
        rows.append([InlineKeyboardButton(text="➕ Set Gmail/Destination", callback_data=f"setdest:{pid}")])
    else:
        rows.append([InlineKeyboardButton(text="⚡ Setup All Project Domains", callback_data=f"emailsetup:{pid}")])
        rows.append([InlineKeyboardButton(text="🔄 Check Verification", callback_data=f"emailverify:{pid}")])
    rows.append([InlineKeyboardButton(text="⬅️ Project", callback_data=f"viewp:{pid}")])
    await safe_edit_text(c.message, "\n".join(lines), reply_markup=kb(rows))
    await c.answer()


@dp.callback_query(F.data.startswith("setdest:"))
async def setdest(c):
    pid = c.data[8:]
    sess[c.from_user.id] = {"s": "email_destination", "project": pid}
    await safe_edit_text(c.message, "📧 Destination email/Gmail bhejo.\n\nExample: yourname@gmail.com")
    await c.answer()


@dp.callback_query(F.data.startswith("emailsetup:"))
async def emailsetup(c):
    pid = c.data[10:]
    if not project(pid):
        return await c.answer("Project not found", show_alert=True)
    sess[c.from_user.id] = {"s": "email_prefix", "project": pid}
    await safe_edit_text(c.message, "📧 Email prefix bhejo.\n\nExample: info\n\nIs prefix ko project ke ALL active domains par create kiya jayega.")
    await c.answer()


@dp.callback_query(F.data.startswith("emailverify:"))
async def emailverify(c):
    pid = c.data[12:]
    dest = store.data.setdefault("settings", {}).get("email_destination")
    if not dest:
        return await c.answer("Destination set nahi hai.", show_alert=True)
    try:
        verified = await cf.email_destination_verified(dest)
        if verified:
            await safe_edit_text(c.message, f"✅ {dest} verified hai.", reply_markup=kb([[InlineKeyboardButton(text="⚡ Setup All Project Domains", callback_data=f"emailsetup:{pid}")], [InlineKeyboardButton(text="⬅️ Email Routing", callback_data=f"pe:{pid}")]]))
        else:
            await safe_edit_text(c.message, f"⏳ {dest} abhi verified nahi hai.\n\nCloudflare verification email open karke verify karo, phir yahan Check Verification dabao.", reply_markup=kb([[InlineKeyboardButton(text="🔄 Check Verification", callback_data=f"emailverify:{pid}")], [InlineKeyboardButton(text="⬅️ Email Routing", callback_data=f"pe:{pid}")]]))
    except Exception as e:
        await safe_edit_text(c.message, f"❌ Verification check failed:\n{e}", reply_markup=kb([[InlineKeyboardButton(text="⬅️ Email Routing", callback_data=f"pe:{pid}")]]))
    await c.answer()


async def _cf_existing_email_rule(zone_id, address):
    """Return an existing routing rule for this exact destination address, if any."""
    url = f"https://api.cloudflare.com/client/v4/zones/{zone_id}/email/routing/rules"
    headers = {"Authorization": f"Bearer {cfg.cf_token}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=30) as client:
        for page in range(1, 11):
            r = await client.get(url, headers=headers, params={"page": page, "per_page": 100})
            if r.status_code >= 400:
                raise RuntimeError(f"Cloudflare {r.status_code}: {r.text[:700]}")
            j = r.json()
            if not j.get("success"):
                raise RuntimeError(str(j.get("errors")))
            for rule in j.get("result", []) or []:
                for matcher in rule.get("matchers", []) or []:
                    if matcher.get("type") == "literal" and matcher.get("field") == "to" and str(matcher.get("value", "")).lower() == address.lower():
                        return rule
            info = j.get("result_info") or {}
            total_pages = int(info.get("total_pages") or page)
            if page >= total_pages:
                break
    return None


async def bulk_email_setup(message, pid, prefix):
    settings = store.data.setdefault("settings", {})
    dest = settings.get("email_destination")
    p = project(pid)
    if not dest or not p:
        await safe_edit_text(message, "❌ Destination/project missing.", reply_markup=kb([[InlineKeyboardButton(text="⬅️ Project", callback_data=f"viewp:{pid}")]]))
        return 0, 0, 0
    verified = await cf.email_destination_verified(dest)
    if not verified:
        await safe_edit_text(message, f"⏳ {dest} abhi verified nahi hai.\n\nVerification email me verify karo, phir Email Routing → Check Verification.", reply_markup=kb([[InlineKeyboardButton(text="🔄 Check Verification", callback_data=f"emailverify:{pid}")]]))
        return 0, 0, 0
    p["email_prefix"] = prefix
    active_domains = [d for d, _ in project_domains(pid, active_only=True)]
    if not active_domains:
        await safe_edit_text(message, "❌ Is project me koi active domain nahi hai.", reply_markup=kb([[InlineKeyboardButton(text="⬅️ Project", callback_data=f"viewp:{pid}")]]))
        return 0, 0, 0
    ok_count = skipped = failed = 0
    records = store.data.setdefault("email_routing", {})
    for dom in active_domains:
        d = store.data["domains"][dom]
        zone_id = d.get("zone_id")
        address = f"{prefix}@{dom}"
        existing = records.get(address)
        if existing and existing.get("status") == "active":
            skipped += 1
            continue
        try:
            try:
                await cf.email_dns(zone_id)
            except Exception:
                pass
            existing_rule = await _cf_existing_email_rule(zone_id, address)
            if existing_rule:
                rule = existing_rule
                skipped += 1
            else:
                rule = await cf.email_rule(zone_id, address, dest)
                ok_count += 1
            records[address] = {
                "address": address, "destination": dest, "domain": dom,
                "zone_id": zone_id, "project_id": pid, "prefix": prefix,
                "rule_id": rule.get("id") if isinstance(rule, dict) else None,
                "status": "active", "created_at": time.time(),
            }
            if address not in p.setdefault("email_routing", []):
                p["email_routing"].append(address)
        except Exception as e:
            failed += 1
            records[address] = {
                "address": address, "destination": dest, "domain": dom,
                "project_id": pid, "prefix": prefix, "status": "error",
                "error": str(e),
            }
    await store.save("Bulk email routing")
    await safe_edit_text(message,
        f"📧 Bulk Email Routing\n\n✅ Created: {ok_count}\n⏭ Already active: {skipped}\n❌ Failed: {failed}\n\nDestination: {dest}\nPrefix: {prefix}",
        reply_markup=kb([[InlineKeyboardButton(text="⬅️ Email Routing", callback_data=f"pe:{pid}")], [InlineKeyboardButton(text="⬅️ Project", callback_data=f"viewp:{pid}")]]))
    return ok_count, skipped, failed


async def _cf_email_destination_info(email):
    """Find an existing Cloudflare destination, preferring verified addresses.

    Cloudflare returns `verified` as a timestamp; null means not verified.
    Destination addresses are account-level and reusable across domains.
    """
    base = f"https://api.cloudflare.com/client/v4/accounts/{cfg.cf_account}/email/routing/addresses"
    headers = {"Authorization": f"Bearer {cfg.cf_token}", "Content-Type": "application/json"}
    target = email.strip().lower()
    async with httpx.AsyncClient(timeout=30) as client:
        # First ask Cloudflare for verified addresses only. This avoids a false
        # negative when the verified destination is not on page 1.
        for verified_filter in (True, None):
            for page in range(1, 11):
                params = {"page": page, "per_page": 50}
                if verified_filter is not None:
                    params["verified"] = "true"
                r = await client.get(base, headers=headers, params=params)
                if r.status_code >= 400:
                    raise RuntimeError(f"Cloudflare {r.status_code}: {r.text[:500]}")
                j = r.json()
                if not j.get("success"):
                    raise RuntimeError(str(j.get("errors")))
                for item in j.get("result", []) or []:
                    if str(item.get("email", "")).strip().lower() == target:
                        return item
                info = j.get("result_info") or {}
                total_pages = int(info.get("total_pages") or page)
                if page >= total_pages:
                    break
    return None


async def _cf_email_destination_verified(email):
    item = await _cf_email_destination_info(email)
    return bool(item and item.get("verified"))


# Keep this local so the bot works with the existing Cloudflare module too.
cf.email_destination_verified = _cf_email_destination_verified


# -------------------- Cloudflare diagnostic --------------------

@dp.callback_query(F.data == "cfdiag")
async def cfdiag(c):
    token = cfg.cf_token or ""
    fingerprint = hashlib.sha256(token.encode()).hexdigest()[:10] if token else "missing"
    result = [f"🔐 Cloudflare diagnostic\n\nToken fingerprint: {fingerprint}"]
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get("https://api.cloudflare.com/client/v4/user/tokens/verify", headers={"Authorization": f"Bearer {token}"})
        result.append(f"Token verify HTTP: {r.status_code}")
        try:
            j = r.json()
            result.append(f"Token status: {j.get('result', {}).get('status', 'unknown')}")
            if not j.get("success"):
                result.append(f"Error: {j.get('errors')}")
        except Exception:
            pass
    except Exception as e:
        result.append(f"Verify error: {e}")
    result.append(f"Account ID: {cfg.cf_account or 'missing'}")
    await safe_edit_text(c.message, "\n".join(result), reply_markup=kb([[InlineKeyboardButton(text="⬅️ Home", callback_data="home")]]))
    await c.answer()


# -------------------- Background worker --------------------

async def worker():
    while True:
        for dom, d in list(store.data.get("domains", {}).items()):
            try:
                if d.get("status") == "registered_no_cloudflare" and not d.get("zone_id"):
                    before = json.dumps(d, sort_keys=True, default=str)
                    await finish_registration(dom, d)
                    after = json.dumps(d, sort_keys=True, default=str)
                    if after != before:
                        await store.save("Cloudflare setup state changed")
                    continue
                if d.get("status") == "registration_pending" and not d.get("zone_id"):
                    op = d.get("operation_id")
                    if op:
                        try:
                            r = await sp.operation(op)
                            rs = str(r.get("status", "pending")).lower()
                            if rs == "success":
                                await finish_registration(dom, d)
                                await store.save("Registration completed")
                                continue
                            if rs == "failed":
                                d["status"] = "error"
                                d["last_error"] = str(r.get("error") or r.get("message") or r)
                                await store.save("Registration failed")
                                continue
                        except Exception as e:
                            d["last_error"] = str(e)
                    last_verify = float(d.get("last_spaceship_verify_ts", 0) or 0)
                    if time.time() - last_verify >= 240:
                        try:
                            info = await spaceship_domain_info(dom)
                            d["last_spaceship_verify_ts"] = time.time()
                            if domain_is_registered(info):
                                await finish_registration(dom, d)
                                await store.save("Registration verified")
                        except Exception:
                            d["last_spaceship_verify_ts"] = time.time()
                elif d.get("status") == "pending" and d.get("zone_id"):
                    z = await cf.zone_get(d["zone_id"])
                    if z.get("status") == "active":
                        d["status"] = "active"
                        await store.save("Domain active")
            except Exception as e:
                err = str(e)
                if d.get("last_error") != err:
                    d["last_error"] = err
                    try:
                        await store.save("Domain worker error")
                    except Exception:
                        pass
        await asyncio.sleep(cfg.poll_seconds)


async def main():
    await store.load()
    try:
        await reconcile_deployments()
    except Exception:
        # Do not prevent the bot from starting if GitHub persistence is temporarily unavailable.
        pass
    asyncio.create_task(worker())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
