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

WORKERS_SUBDOMAIN = os.getenv("CF_WORKERS_SUBDOMAIN", "zerothtec.workers.dev").strip().rstrip("/")

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
    # Domain buttons should be one per row so the full domain + price is visible.
    return [[button] for button in buttons]


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
            [
                InlineKeyboardButton(text="➕ Create Project", callback_data="pnew"),
                InlineKeyboardButton(text="📁 My Projects", callback_data="plist"),
            ],
            [InlineKeyboardButton(text="🌐 My Domains", callback_data="mydomains")],
        ]),
    )


async def project_menu(message, pid):
    p = project(pid)
    if not p:
        return await safe_edit_text(message, "❌ Project not found.")

    # A Worker that is already deployed must appear under Deployments even
    # when an older bot version created the website record without creating
    # a deployment record. Reconcile before showing project counters.
    try:
        await reconcile_deployments()
    except Exception:
        pass

    ds = project_domains(pid)
    deps = project_deployments(pid)

    # "Websites" means websites that have a custom domain assigned.
    # A Worker deployed only to *.zerothtec.workers.dev is a Deployment, not
    # a custom-domain Website. This gives, for example: Domains 1, Websites 0,
    # Deployments 1 when a domain exists but has not been assigned yet.
    sites = [
        w for w in project_sites(pid)
        if w.get("status") == "deployed" and w.get("domain")
    ]

    text = (
        f"📁 {p['name']}\n\n"
        f"🌐 Domains: {len(ds)}\n"
        f"💻 Websites: {len(sites)}\n"
        f"🚀 Deployments: {len([d for d in deps if d.get('status') == 'live'])}\n"
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


async def finish_external_domain(dom, x):
    """Create and maintain a Cloudflare zone for a domain registered elsewhere."""
    x["registrar"] = "external"
    x["registration_confirmed"] = True
    x.setdefault("registration_confirmed_at", time.time())
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
        return True
    except Exception as e:
        x["status"] = "external_registered_no_cloudflare"
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
    await safe_edit_text(c.message, "📝 Enter the project name:")
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
        [
            InlineKeyboardButton(text="➕ Create Project", callback_data="pnew"),
            InlineKeyboardButton(text="📁 My Projects", callback_data="plist"),
        ],
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

    if st == "add_domain":
        pid = s.get("project")
        dom = (m.text or "").strip().lower()
        if not pid or not project(pid):
            sess.pop(m.from_user.id, None)
            return await m.answer("❌ Project session expired.")
        if not valid_domain(dom):
            return await m.answer("❌ Enter a valid domain name, for example: example.com")

        existing = store.data.get("domains", {}).get(dom)
        if existing and existing.get("project_id") and existing.get("project_id") != pid:
            return await m.answer("❌ This domain is already assigned to another project.")

        try:
            info = await spaceship_domain_info(dom)

            # Existing Spaceship domain: preserve the normal workflow.
            if domain_is_registered(info):
                if not existing:
                    existing = {
                        "domain": dom,
                        "registrar": "spaceship",
                        "project_id": pid,
                        "status": "registered_no_cloudflare",
                        "assigned": False,
                        "website_id": None,
                        "registration_confirmed": True,
                        "registration_confirmed_at": time.time(),
                    }
                    store.data.setdefault("domains", {})[dom] = existing
                else:
                    existing["project_id"] = pid
                    existing["registrar"] = "spaceship"
                    existing["registration_confirmed"] = True
                    existing["registration_confirmed_at"] = (
                        existing.get("registration_confirmed_at") or time.time()
                    )
                    if not existing.get("zone_id"):
                        existing["status"] = "registered_no_cloudflare"

                await finish_registration(dom, existing)
                await store.save("Add existing Spaceship domain")
                sess[m.from_user.id] = {"s": "project", "project": pid}

                if existing.get("zone_id"):
                    hosts = existing.get("nameservers") or []
                    ns_text = "\n".join(f"• {ns}" for ns in hosts) if hosts else "Cloudflare nameservers are being prepared."
                    await m.answer(
                        f"✅ {dom} was verified in Spaceship.\n\n"
                        "Cloudflare setup has started.\n\n"
                        f"Nameservers:\n{ns_text}\n\n"
                        "Use Check Status to monitor activation.",
                        reply_markup=kb([
                            [InlineKeyboardButton(text="🔄 Check Status", callback_data=f"pcheck:{pid}")],
                            [InlineKeyboardButton(text="⬅️ Domains", callback_data=f"pd:{pid}")],
                        ]),
                    )
                else:
                    await m.answer(
                        f"⚠️ {dom} was verified in Spaceship, but Cloudflare setup could not be completed yet.\n\n"
                        f"Error: {existing.get('last_error') or 'Unknown error'}",
                        reply_markup=kb([
                            [InlineKeyboardButton(text="🔄 Check Status", callback_data=f"pcheck:{pid}")],
                            [InlineKeyboardButton(text="⬅️ Domains", callback_data=f"pd:{pid}")],
                        ]),
                    )
                return

            # Not found in Spaceship: ask before creating a Cloudflare zone.
            sess[m.from_user.id] = {
                "s": "external_domain_confirm",
                "project": pid,
                "external_domain": dom,
            }
            await m.answer(
                f"ℹ️ {dom} was not found in your Spaceship account.\n\n"
                "Would you like to add this domain directly to Cloudflare?\n"
                "This is suitable for a domain registered with Hostinger or another registrar.",
                reply_markup=kb([
                    [InlineKeyboardButton(text="✅ Add to Cloudflare", callback_data="external_cf_yes")],
                    [InlineKeyboardButton(text="❌ Cancel", callback_data="external_cf_no")],
                ]),
            )
        except Exception as e:
            return await m.answer(f"❌ Domain verification failed:\n{str(e)[:700]}")
        return

    if st == "external_domain_confirm":
        return

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
            "email_destination": None,
            "email_destination_id": None,
            "email_prefix": None,
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
                    # Spaceship availability responses can expose pricing in
                    # different/nested fields. Always resolve the displayed
                    # purchase price instead of silently showing a blank price.
                    price = q.get("price")
                    if price is None:
                        for key in ("registrationPrice", "registerPrice", "registration_price", "priceUsd", "amount"):
                            if q.get(key) is not None:
                                price = q.get(key)
                                break
                    if isinstance(price, dict):
                        price = price.get("amount", price.get("value", price.get("price")))
                    # Official Spaceship availability response uses:
                    # premiumPricing: [{operation: "register", price: 10.99, currency: "USD"}]
                    if price is None:
                        premium = q.get("premiumPricing")
                        if isinstance(premium, list):
                            for entry in premium:
                                if not isinstance(entry, dict):
                                    continue
                                if str(entry.get("operation", "")).lower() == "register" or len(premium) == 1:
                                    price = entry.get("price", entry.get("amount", entry.get("value")))
                                    break
                    if price is None:
                        for container_key in ("pricing", "prices", "registration", "registrationPricing"):
                            container = q.get(container_key)
                            if isinstance(container, dict):
                                for key in ("price", "amount", "value", "registrationPrice", "registerPrice"):
                                    if container.get(key) is not None:
                                        price = container.get(key)
                                        break
                            elif isinstance(container, list):
                                for entry in container:
                                    if isinstance(entry, dict) and str(entry.get("operation", "")).lower() == "register":
                                        price = entry.get("price", entry.get("amount", entry.get("value")))
                                        break
                            if price is not None:
                                break

                    # The legacy /v1 availability endpoint can return an empty
                    # premiumPricing array for ordinary domains and omit the
                    # standard price entirely. Spaceship's current public
                    # pricing pages provide the regular first-year sale price
                    # for the TLDs this bot allows, so use that only as a
                    # display fallback. Premium/domain-specific API pricing
                    # above always takes precedence.
                    if price is None:
                        tld = "." + q["domain"].rsplit(".", 1)[-1].lower()
                        regular_tld_prices = {
                            ".sbs": 0.77,
                            ".rest": 1.05,
                            ".buzz": 0.88,
                            ".click": 1.04,
                        }
                        price = regular_tld_prices.get(tld)

                    out.append((q["domain"], price))
        sess[m.from_user.id] = {"s": "buylist", "items": out, "cart": [], "project": pid}
        if not out:
            return await m.answer("❌ No available domains found.")
        buttons = [InlineKeyboardButton(text=f"{d}\n{p}" if p else f"{d}", callback_data=f"b:{hashlib.sha1(d.encode()).hexdigest()[:12]}") for i, (d, p) in enumerate(out)]
        rows = two_col(buttons)
        rows.append([InlineKeyboardButton(text="🛒 Cart (0)", callback_data="cart")])
        rows.append([InlineKeyboardButton(text="⬅️ Project", callback_data=f"viewp:{pid}")])
        await m.answer("Available domains:", reply_markup=kb(rows))
        return

    if st == "email_destination":
        pid = s.get("project")
        p = project(pid)
        email = (m.text or "").strip()
        if not pid or not p:
            return await m.answer("❌ Project session expired.")
        if "@" not in email:
            return await m.answer("❌ Enter a valid Gmail or destination email.")
        try:
            # Destination is stored per-project. Cloudflare's destination
            # address itself is account-level and can be reused, but each
            # project keeps its own selected destination.
            existing = await _cf_email_destination_info(email)
            if existing:
                p["email_destination"] = email
                p["email_destination_id"] = existing.get("id")
                await store.save("Use project email destination")
                sess[m.from_user.id] = {"s": "email_prefix", "project": pid}
                if existing.get("verified"):
                    await m.answer(
                        f"✅ Destination set: {email}\n\n"
                        "Enter the email prefix.\n\nExample: info"
                    )
                else:
                    await m.answer(
                        f"📧 Destination set: {email}\n\n"
                        "Please verify the Cloudflare verification email.\n\n"
                        "Enter the email prefix.\n\nExample: info"
                    )
                return

            addr = await cf.email_destination(email)
            p["email_destination"] = email
            p["email_destination_id"] = addr.get("id")
            await store.save("Create project email destination")
            sess[m.from_user.id] = {"s": "email_prefix", "project": pid}
            await m.answer(
                f"📧 Destination set: {email}\n\n"
                "Cloudflare verification email bhejega.\n\n"
                "Enter the email prefix.\n\nExample: info"
            )
        except Exception as e:
            await m.answer(f"❌ Destination could not be created or checked:\n{e}")
        return

    if st == "email_prefix":
        pid = s.get("project")
        p = project(pid)
        prefix = re.sub(r"[^a-zA-Z0-9._-]", "", (m.text or "").strip().lower())
        if not pid or not p:
            return await m.answer("❌ Project session expired.")
        if not prefix:
            return await m.answer("❌ Prefix invalid.")
        p["email_prefix"] = prefix
        await store.save("Set project email prefix")
        sess[m.from_user.id] = {"s": "project", "project": pid}
        dest = p.get("email_destination")
        if not dest:
            return await m.answer("❌ Destination missing.", reply_markup=kb([[InlineKeyboardButton(text="📧 Email Routing", callback_data=f"pe:{pid}")]]))
        verified = await cf.email_destination_verified(dest)
        if verified:
            await m.answer(
                f"✅ Email settings saved\n\nDestination: {dest}\nPrefix: {prefix}\n\n"
                "Then select Setup All Project Domains.",
                reply_markup=kb([
                    [InlineKeyboardButton(text="⚡ Setup All Project Domains", callback_data=f"emailsetup:{pid}")],
                    [InlineKeyboardButton(text="⬅️ Email Routing", callback_data=f"pe:{pid}")],
                ]),
            )
        else:
            await m.answer(
                f"⏳ Email settings saved\n\nDestination: {dest}\nPrefix: {prefix}\n\n"
                "First verify the destination using the Cloudflare verification email, then select Check Verification.",
                reply_markup=kb([
                    [InlineKeyboardButton(text="🔄 Check Verification", callback_data=f"emailverify:{pid}")],
                    [InlineKeyboardButton(text="⬅️ Email Routing", callback_data=f"pe:{pid}")],
                ]),
            )
        return


# -------------------- Project domains --------------------

@dp.callback_query(F.data.startswith("pd:"))
async def pd(c):
    # Acknowledge the Telegram callback immediately so the button never
    # remains stuck in the loading state while the menu is being rebuilt.
    await c.answer()
    pid = c.data[3:]
    if not project(pid):
        return await c.message.answer("❌ Project not found.")
    try:
        rows = [
            [InlineKeyboardButton(text="🛒 Buy Domains", callback_data=f"buy:{pid}")],
            [InlineKeyboardButton(text="➕ Add Domain", callback_data=f"adddomain:{pid}")],
        ]
        # Telegram callback_data is limited to 64 bytes. Store the actual
        # domain in the user's session and use a short token in the button.
        import secrets
        domain_choices = {}
        for d, x in project_domains(pid):
            status = x.get("status", "unknown")
            icon = "🟢" if status == "active" else "🟡" if status == "pending" else "🟠" if status == "registered_no_cloudflare" else "🔴"
            token = secrets.token_hex(6)
            domain_choices[token] = d
            rows.append([InlineKeyboardButton(text=f"{icon} {d}", callback_data=f"dinfo:{token}")])
        sess.setdefault(c.from_user.id, {})["domain_info_choices"] = domain_choices
        sess[c.from_user.id]["domain_info_project"] = pid
        rows.append([InlineKeyboardButton(text="🔄 Check Status", callback_data=f"pcheck:{pid}")])
        rows.append([InlineKeyboardButton(text="⬅️ Project", callback_data=f"viewp:{pid}")])
        await safe_edit_text(c.message, f"🌐 {project_title(pid)} — Domains", reply_markup=kb(rows))
    except Exception as e:
        await c.message.answer(f"❌ Could not open Domains: {str(e)[:500]}")


@dp.callback_query(F.data.startswith("buy:"))
async def buy_start(c):
    pid = c.data[4:]
    if not project(pid):
        return await c.answer("Project not found", show_alert=True)
    sess[c.from_user.id] = {"s": "buynames", "project": pid}
    await safe_edit_text(c.message, "🛒 Enter base names: sonu monu raju")
    await c.answer()


@dp.callback_query(F.data.startswith("adddomain:"))
async def adddomain_start(c):
    pid = c.data[10:]
    if not project(pid):
        return await c.answer("Project not found", show_alert=True)
    sess[c.from_user.id] = {"s": "add_domain", "project": pid}
    await safe_edit_text(
        c.message,
        "➕ Enter the domain already registered in your Spaceship account.\n\nExample: example.sbs",
        reply_markup=kb([[InlineKeyboardButton(text="⬅️ Domains", callback_data=f"pd:{pid}")]]),
    )
    await c.answer()


@dp.callback_query(F.data == "external_cf_yes")
async def external_cf_yes(c):
    s = sess.get(c.from_user.id, {})
    pid = s.get("project")
    dom = s.get("external_domain")
    if not pid or not dom or not project(pid):
        return await c.answer("Domain setup session expired. Please start Add Domain again.", show_alert=True)

    await c.answer("Adding domain to Cloudflare...")
    try:
        existing = store.data.get("domains", {}).get(dom)
        if existing and existing.get("project_id") and existing.get("project_id") != pid:
            return await c.answer("This domain is already assigned to another project.", show_alert=True)

        if not existing:
            existing = {
                "domain": dom,
                "registrar": "external",
                "project_id": pid,
                "status": "external_registered_no_cloudflare",
                "assigned": False,
                "website_id": None,
                "registration_confirmed": True,
                "registration_confirmed_at": time.time(),
            }
            store.data.setdefault("domains", {})[dom] = existing
        else:
            existing["project_id"] = pid
            existing["registrar"] = "external"
            existing["registration_confirmed"] = True

        await safe_edit_text(
            c.message,
            f"⏳ Adding {dom} to Cloudflare...\n\nCreating the Cloudflare zone.",
        )

        ok_cf = await finish_external_domain(dom, existing)
        await store.save("Add external domain to Cloudflare")

        if not ok_cf or not existing.get("zone_id"):
            sess[c.from_user.id] = {"s": "project", "project": pid}
            return await safe_edit_text(
                c.message,
                f"❌ Cloudflare setup could not be completed for {dom}.\n\n"
                f"Error: {existing.get('last_error') or 'Unknown error'}",
                reply_markup=kb([
                    [InlineKeyboardButton(text="🔄 Check Status", callback_data=f"pcheck:{pid}")],
                    [InlineKeyboardButton(text="⬅️ Domains", callback_data=f"pd:{pid}")],
                ]),
            )

        hosts = existing.get("nameservers") or []
        ns_text = "\n".join(f"• {ns}" for ns in hosts) if hosts else "Cloudflare did not return nameservers."
        sess[c.from_user.id] = {"s": "project", "project": pid}

        await safe_edit_text(
            c.message,
            f"✅ {dom} was added to Cloudflare successfully.\n\n"
            "Set these Cloudflare nameservers at your current registrar:\n\n"
            f"{ns_text}\n\n"
            "After updating the nameservers, use Check Status. "
            "The remaining domain, website, custom-domain, and email-routing workflow is unchanged.",
            reply_markup=kb([
                [InlineKeyboardButton(text="🔄 Check Status", callback_data=f"pcheck:{pid}")],
                [InlineKeyboardButton(text="⬅️ Domains", callback_data=f"pd:{pid}")],
            ]),
        )
    except Exception as e:
        await safe_edit_text(
            c.message,
            f"❌ Cloudflare setup failed:\n{str(e)[:900]}",
            reply_markup=kb([
                [InlineKeyboardButton(text="⬅️ Domains", callback_data=f"pd:{pid}")]
            ]),
        )


@dp.callback_query(F.data == "external_cf_no")
async def external_cf_no(c):
    s = sess.get(c.from_user.id, {})
    pid = s.get("project")
    sess[c.from_user.id] = {"s": "project", "project": pid} if pid and project(pid) else {}
    await c.answer("Cancelled")
    if pid and project(pid):
        await safe_edit_text(
            c.message,
            "Domain was not added.",
            reply_markup=kb([[InlineKeyboardButton(text="⬅️ Domains", callback_data=f"pd:{pid}")]]),
        )
    else:
        await safe_edit_text(
            c.message,
            "Domain setup cancelled.",
            reply_markup=kb([[InlineKeyboardButton(text="🏠 Home", callback_data="home")]]),
        )


@dp.callback_query(F.data.startswith("dinfo:"))
async def dinfo(c):
    token = c.data[6:]
    session = sess.get(c.from_user.id, {})
    pid = session.get("domain_info_project")
    dom = session.get("domain_info_choices", {}).get(token)
    if not pid or not dom:
        return await c.answer("Domain selection expired. Please open Domains again.", show_alert=True)
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
    rows = [
        [InlineKeyboardButton(text="🛒 Buy Domains", callback_data=f"buy:{pid}")],
        [InlineKeyboardButton(text="➕ Add Domain", callback_data=f"adddomain:{pid}")],
    ]
    import secrets
    domain_choices = {}
    for d, x in project_domains(pid):
        status = x.get("status", "unknown")
        icon = "🟢" if status == "active" else "🟡" if status == "pending" else "🟠" if status == "registered_no_cloudflare" else "🔴"
        token = secrets.token_hex(6)
        domain_choices[token] = d
        rows.append([InlineKeyboardButton(text=f"{icon} {d}", callback_data=f"dinfo:{token}")])
    sess.setdefault(message.chat.id, {})["domain_info_choices"] = domain_choices
    sess[message.chat.id]["domain_info_project"] = pid
    rows.append([InlineKeyboardButton(text="🔄 Check Status", callback_data=f"pcheck:{pid}")])
    rows.append([InlineKeyboardButton(text="⬅️ Project", callback_data=f"viewp:{pid}")])
    await safe_edit_text(message, ((prefix + "\n\n") if prefix else "") + f"🌐 {project_title(pid)} — Domains", reply_markup=kb(rows))


async def check_project_domains(pid):
    changed = False
    for dom, x in project_domains(pid):
        status = x.get("status")
        if status == "external_registered_no_cloudflare" and not x.get("zone_id"):
            before = json.dumps(x, sort_keys=True, default=str)
            await finish_external_domain(dom, x)
            changed |= before != json.dumps(x, sort_keys=True, default=str)
        elif status == "registered_no_cloudflare" and not x.get("zone_id"):
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
    s = sess.get(c.from_user.id, {})
    items = s.get("items", [])
    token = c.data[2:]

    # Keep callback_data short. Telegram limits callback_data to 64 bytes,
    # so long domain names must not be embedded in the callback payload.
    # Numeric callbacks remain supported for compatibility with old messages.
    dom = None
    if token.isdigit():
        idx = int(token)
        if 0 <= idx < len(items):
            dom = items[idx][0]
    else:
        fingerprint = token.split(":", 1)[-1]
        for candidate, _price in items:
            digest = hashlib.sha1(candidate.encode()).hexdigest()
            if digest[:12] == fingerprint or digest[:8] == fingerprint:
                dom = candidate
                break
    if not dom:
        return await c.answer("Domain selection expired. Please fetch the domains again.", show_alert=True)

    available = None
    for attempt in range(3):
        try:
            r = await sp.availability([dom])
            results = r if isinstance(r, list) else r.get("domains", r.get("results", []))
            item = next((q for q in results if str(q.get("domain", "")).lower() == dom.lower()), None)
            if item is not None:
                result = str(item.get("result", "")).lower()
                if result == "available":
                    available = True
                    break
                if result in {"unavailable", "taken", "registered"}:
                    available = False
                    break
        except Exception:
            pass
        if attempt < 2:
            await asyncio.sleep(1 + attempt)

    if available is False:
        return await c.answer("This domain is no longer available for registration.", show_alert=True)
    if available is None:
        return await c.answer("Spaceship availability could not be verified right now. Please try again.", show_alert=True)

    cart = s.setdefault("cart", [])
    idx = next((i for i, (d, _p) in enumerate(items) if d == dom), None)
    if idx is None:
        return await c.answer("Domain selection expired. Please fetch the domains again.", show_alert=True)
    if idx in cart:
        cart.remove(idx)
        await c.answer("Removed from cart")
    else:
        cart.append(idx)
        await c.answer("Added to cart 🛒")
    await restore_buy_list(c)


@dp.callback_query(F.data == "cart")
async def show_cart(c):
    s = sess.get(c.from_user.id, {})
    items = s.get("items", [])
    cart = [i for i in s.get("cart", []) if 0 <= i < len(items)]
    s["cart"] = cart
    if not cart:
        return await c.answer("Cart empty", show_alert=True)
    lines = ["🛒 Your Cart", ""]
    total = 0.0
    for i in cart:
        d, price = items[i]
        lines.append(f"• {d}" + (f" — {price}" if price else ""))
        try:
            total += float(price)
        except Exception:
            pass
    if total:
        lines.append(f"\n💵 Approx total: {total:g}")
    lines.append("\nConfirming will submit all domains in the cart for purchase.")
    await safe_edit_text(c.message, "\n".join(lines), reply_markup=kb([
        [InlineKeyboardButton(text=f"✅ Confirm Purchase ({len(cart)})", callback_data="confirmcart")],
        [InlineKeyboardButton(text="⬅️ Back to Domains", callback_data="backbuy")],
    ]))
    await c.answer()


@dp.callback_query(F.data == "backbuy")
async def backbuy(c):
    await restore_buy_list(c)
    await c.answer()


@dp.callback_query(F.data == "confirmcart")
async def confirmcart(c):
    s = sess.get(c.from_user.id, {})
    pid = s.get("project")
    items = s.get("items", [])
    cart = [i for i in s.get("cart", []) if 0 <= i < len(items)]

    if not pid or not cart:
        return await c.answer("Cart empty", show_alert=True)

    # Move cart into a separate pending-purchase list.
    pending = [{"index": i, "domain": items[i][0], "price": items[i][1]} for i in cart]
    s["pending_purchases"] = pending
    s["cart"] = []
    s["s"] = "purchase_select"

    rows = []
    for n, item in enumerate(pending, 1):
        rows.append([
            InlineKeyboardButton(
                text=f"{item['domain']}\n{item['price']}" if item["price"] else f"{item['domain']}",
                callback_data=f"buyone:{n-1}"
            )
        ])

    rows.append([
        InlineKeyboardButton(text="⬅️ Back", callback_data=f"buy:{pid}")
    ])

    await safe_edit_text(
        c.message,
        "🛒 Cart ready\n\n"
        "Purchase the domains one at a time.\n"
        "Click a domain to submit its purchase request to Spaceship.",
        reply_markup=kb(rows)
    )
    await c.answer()


@dp.callback_query(F.data.startswith("buyone:"))
async def buyone(c):
    s = sess.get(c.from_user.id, {})
    pid = s.get("project")
    pending = s.get("pending_purchases", [])

    try:
        n = int(c.data.split(":", 1)[1])
    except Exception:
        return await c.answer("Invalid selection", show_alert=True)

    if n < 0 or n >= len(pending):
        return await c.answer("Domain not found", show_alert=True)

    item = pending[n]
    dom = item["domain"]

    # Prevent accidental duplicate purchase clicks.
    if item.get("status") == "submitted":
        return await c.answer("Already submitted", show_alert=True)

    try:
        await c.answer("Submitting purchase...")

        await safe_edit_text(
            c.message,
            f"⏳ Purchasing {dom}...\n\n"
            f"Submitting the request to Spaceship."
        )

        contacts = await get_or_create_spaceship_contact()
        r = await sp.register(dom, contacts)
        op = r.headers.get("spaceship-async-operationid")

        if not op:
            raise RuntimeError(
                f"Spaceship response me operation ID nahi mila. "
                f"HTTP {r.status_code}: {r.text[:500]}"
            )

        store.data.setdefault("domains", {})[dom] = {
            "domain": dom,
            "registrar": "spaceship",
            "status": "registration_pending",
            "operation_id": op,
            "assigned": False,
            "project_id": pid,
            "created_at": time.time(),
        }
        await store.save(f"Domain purchase submitted: {dom}")

        item["status"] = "submitted"
        item["operation_id"] = op

        rows = []
        for idx, p in enumerate(pending):
            if p.get("status") == "submitted":
                label = f"✅ {p['domain']}"
            elif p.get("status") == "failed":
                label = f"❌ {p['domain']}"
            else:
                label = f"💰 {p['domain']}"
            rows.append([
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"buyone:{idx}" if not p.get("status") else "noop"
                )
            ])

        rows.append([
            InlineKeyboardButton(text="🔄 Check Status", callback_data=f"pcheck:{pid}")
        ])
        rows.append([
            InlineKeyboardButton(text="⬅️ Project", callback_data=f"viewp:{pid}")
        ])

        await safe_edit_text(
            c.message,
            f"✅ Purchase request submitted\n\n"
            f"Domain: {dom}\n"
            f"Operation ID: {op}\n\n"
            f"Click the remaining domain buttons to submit each purchase request individually.",
            reply_markup=kb(rows)
        )

    except Exception as e:
        item["status"] = "failed"
        item["error"] = str(e)

        rows = []
        for idx, p in enumerate(pending):
            if p.get("status") == "submitted":
                label = f"✅ {p['domain']}"
            elif p.get("status") == "failed":
                label = f"❌ {p['domain']}"
            else:
                label = f"💰 {p['domain']}"
            rows.append([
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"buyone:{idx}" if not p.get("status") else "noop"
                )
            ])
        rows.append([
            InlineKeyboardButton(text="🔄 Check Status", callback_data=f"pcheck:{pid}")
        ])

        await safe_edit_text(
            c.message,
            f"❌ Purchase failed\n\n"
            f"Domain: {dom}\n"
            f"Error: {str(e)[:900]}",
            reply_markup=kb(rows)
        )


@dp.callback_query(F.data == "noop")
async def noop(c):
    await c.answer("Already processed", show_alert=True)

async def restore_buy_list(c):
    s = sess.get(c.from_user.id, {})
    items = s.get("items", [])
    cart = [i for i in s.setdefault("cart", []) if 0 <= i < len(items)]
    s["cart"] = cart
    buttons = []
    for i, (d, price) in enumerate(items):
        prefix = "🛒 " if i in cart else ""
        label = prefix + d + (f"\n{price}" if price else "")
        buttons.append(InlineKeyboardButton(text=label, callback_data=f"b:{hashlib.sha1(d.encode()).hexdigest()[:12]}"))
    rows = two_col(buttons)
    rows.append([InlineKeyboardButton(text=f"🛒 Cart ({len(cart)})", callback_data="cart")])
    rows.append([InlineKeyboardButton(text="⬅️ Domains", callback_data=f"pd:{s.get('project')}")])
    await safe_edit_text(c.message, "Available domains:", reply_markup=kb(rows))


@dp.callback_query(F.data == "cancel")
async def cancel(c):
    s = sess.get(c.from_user.id, {})
    s.pop("pending_buy", None)
    s.pop("pending_price", None)
    s["s"] = "buylist"
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
    await safe_edit_text(c.message, "📤 Upload HTML file(s).\n\nEach uploaded HTML file will be deployed as the index.html content of a Worker.")
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
    await safe_edit_text(c.message, "✏️ Upload the new HTML file.\n\nThis will update the same Worker/subdomain; no new subdomain will be created.", reply_markup=kb([[InlineKeyboardButton(text="⬅️ Website", callback_data=f"site:{pid}:{wid}")]]))
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
    """Wrap uploaded HTML inside an ES-module Cloudflare Worker."""
    encoded = base64.b64encode(html_bytes).decode("ascii")
    source = (
        'const DATA = "' + encoded + '";\n'
        'function getHtml() {\n'
        '  const bytes = Uint8Array.from(atob(DATA), c => c.charCodeAt(0));\n'
        '  return new TextDecoder().decode(bytes);\n'
        '}\n'
        'export default {\n'
        '  async fetch() {\n'
        '    return new Response(getHtml(), {\n'
        '      status: 200,\n'
        '      headers: {\n'
        '        "content-type": "text/html; charset=UTF-8",\n'
        '        "cache-control": "no-cache"\n'
        '      }\n'
        '    });\n'
        '  }\n'
        '};\n'
    )
    return source.encode("utf-8")


async def deploy_worker(script_name, html_path):
    """Upload HTML, ensure deployment, and enable workers.dev."""
    html_bytes = Path(html_path).read_bytes()
    source = worker_source_from_html(html_bytes)
    base = f"https://api.cloudflare.com/client/v4/accounts/{cfg.cf_account}/workers/scripts/{script_name}"
    headers = {"Authorization": f"Bearer {cfg.cf_token}"}

    metadata = {
        "main_module": "worker.js",
        "compatibility_date": "2026-01-01",
    }
    files = [
        ("metadata", (None, json.dumps(metadata), "application/json")),
        ("worker.js", ("worker.js", source, "application/javascript+module")),
    ]

    async with httpx.AsyncClient(timeout=90) as client:
        r = await client.put(base, headers=headers, files=files)
        if r.status_code >= 400:
            raise RuntimeError(f"Cloudflare Worker upload {r.status_code}: {r.text[:1500]}")
        j = r.json()
        if not j.get("success", True):
            raise RuntimeError(f"Cloudflare Worker upload failed: {j.get('errors')}")

        # Upload normally creates a deployment. If none is returned,
        # explicitly deploy the newest version at 100%.
        result = j.get("result") or {}
        if not (result.get("deployments") or []):
            vr = await client.get(
                f"{base}/versions",
                headers=headers,
                params={"per_page": 1},
            )
            if vr.status_code >= 400:
                raise RuntimeError(f"Cloudflare version lookup {vr.status_code}: {vr.text[:1200]}")
            versions = (vr.json().get("result") or [])
            if isinstance(versions, dict):
                versions = versions.get("result") or versions.get("items") or []
            if not versions or not versions[0].get("id"):
                raise RuntimeError("Cloudflare latest Worker version ID nahi mila.")

            dr = await client.post(
                f"{base}/deployments",
                headers={**headers, "Content-Type": "application/json"},
                json={
                    "strategy": "percentage",
                    "versions": [{"percentage": 100, "version_id": versions[0]["id"]}],
                    "annotations": {
                        "workers/message": "Telegram HTML deployment"
                    },
                },
            )
            if dr.status_code >= 400:
                raise RuntimeError(f"Cloudflare deployment {dr.status_code}: {dr.text[:1500]}")
            if not dr.json().get("success", True):
                raise RuntimeError(f"Cloudflare deployment failed: {dr.json().get('errors')}")

        # Explicitly enable the Worker on workers.dev.
        sr = await client.post(
            f"{base}/subdomain",
            headers={**headers, "Content-Type": "application/json"},
            json={"enabled": True, "previews_enabled": True},
        )
        if sr.status_code >= 400:
            raise RuntimeError(f"Cloudflare workers.dev enable {sr.status_code}: {sr.text[:1500]}")
        if not sr.json().get("success", True):
            raise RuntimeError(f"Cloudflare workers.dev enable failed: {sr.json().get('errors')}")

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
        await m.answer("📄 HTML was received, but upload mode is not active.\n\nGo to Project → Website → 📤 Upload HTML to upload it.")
        return
    pid = s.get("project")
    if not pid or not project(pid):
        return await m.answer("❌ Project session expired.")
    filename = m.document.file_name or "website.html"
    try:
        base = base_html_name(filename)
    except ValueError:
        return await m.answer("❌ Only .html files are supported.")

    Path("runtime").mkdir(exist_ok=True)
    f = await bot.get_file(m.document.file_id)
    p = Path("runtime") / (uuid.uuid4().hex + ".html")
    await bot.download_file(f.file_path, p)

    # ---------------- Existing website: replace HTML ----------------
    if mode == "replace_html":
        wid = s.get("website")
        w = store.data.get("websites", {}).get(wid)
        if not w or w.get("project_id") != pid:
            p.unlink(missing_ok=True)
            return await m.answer("❌ Website not found.")

        await m.answer(f"📄 {filename} received.\n⏳ The HTML is being updated on the same Worker...")
        candidate = dict(w)
        candidate["path"] = str(p)
        candidate["original_filename"] = filename
        candidate["base_name"] = base
        try:
            url, _ = await deploy_website(candidate)

            # Only commit the new HTML metadata after Cloudflare accepted the
            # deployment. A failed update therefore leaves the old website intact.
            for key in ("path", "original_filename", "base_name", "worker_name", "worker_url", "status", "deployed_at", "last_error"):
                if key in candidate:
                    w[key] = candidate[key]
            w["status"] = "deployed"
            w["last_error"] = None

            for dep in store.data.get("deployments", {}).values():
                if dep.get("website_id") == wid and dep.get("status") == "live":
                    dep["status"] = "archived"
            versions = [d for d in store.data.get("deployments", {}).values() if d.get("website_id") == wid]
            depid = str(uuid.uuid4())
            store.data.setdefault("deployments", {})[depid] = {
                "id": depid, "project_id": pid, "website_id": wid,
                "worker_name": w.get("worker_name"), "worker_url": url,
                "original_filename": w.get("original_filename"),
                "domain": w.get("domain"), "status": "live",
                "version": len(versions) + 1, "created_at": time.time(),
            }
            await store.save("HTML website updated")
            sess[m.from_user.id] = {"s": "project", "project": pid}
            await m.answer(
                f"✅ Website updated\n\n📄 {filename}\n🌐 {url}\n\nSame Worker/subdomain par update hua hai.",
                reply_markup=kb([
                    [InlineKeyboardButton(text="🌐 Assign/Change Domain", callback_data=f"assignsite:{pid}:{wid}")],
                    [InlineKeyboardButton(text="🚀 Deployments", callback_data=f"pv:{pid}")],
                    [InlineKeyboardButton(text="⬅️ Project", callback_data=f"viewp:{pid}")],
                ]),
            )
        except Exception as e:
            p.unlink(missing_ok=True)
            await m.answer(f"❌ Website update failed:\n{e}", reply_markup=kb([[InlineKeyboardButton(text="🚀 Deployments", callback_data=f"pv:{pid}")]]))
        return

    # ---------------- New website ----------------
    # Do NOT persist an HTML/website record before deployment succeeds. This
    # keeps the project clean if the user uploads a file and deployment fails.
    temp = {
        "id": str(uuid.uuid4()), "project_id": pid, "original_filename": filename,
        "base_name": base, "path": str(p), "domain": None,
        "worker_name": None, "worker_url": None, "status": "uploaded",
        "created_at": time.time(),
    }
    await m.answer(f"📄 {filename} received.\n⏳ The Cloudflare Worker is being deployed...")
    try:
        url, _ = await deploy_website(temp)
        wid = temp["id"]
        store.data.setdefault("websites", {})[wid] = temp
        store.data["projects"][pid].setdefault("websites", []).append(wid)
        depid = str(uuid.uuid4())
        store.data.setdefault("deployments", {})[depid] = {
            "id": depid, "project_id": pid, "website_id": wid,
            "worker_name": temp.get("worker_name"), "worker_url": url,
            "original_filename": filename, "domain": None,
            "status": "live", "version": 1, "created_at": time.time(),
        }
        await store.save("HTML deployed to Cloudflare Workers")
        sess[m.from_user.id] = {"s": "html", "project": pid}
        await m.answer(
            f"🚀 Deployment successful!\n\n📄 {filename}\n🌐 {url}\n\n⚠️ workers.dev/HTTPS activation me kuch minutes lag sakte hain; isse deployment failure nahi maana jayega.",
            reply_markup=kb([
                [InlineKeyboardButton(text="🌐 Assign Domain", callback_data=f"assignsite:{pid}:{wid}")],
                [InlineKeyboardButton(text="✏️ Change HTML", callback_data=f"changehtml:{pid}:{wid}")],
                [InlineKeyboardButton(text="🚀 Deployments", callback_data=f"pv:{pid}")],
                [InlineKeyboardButton(text="⬅️ Project", callback_data=f"viewp:{pid}")],
            ]),
        )
    except Exception as e:
        p.unlink(missing_ok=True)
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
        return await safe_edit_text(c.message, "❌ This project has no unassigned active domains.", reply_markup=kb([[InlineKeyboardButton(text="⬅️ Website", callback_data=f"pw:{pid}")]]))
    # Telegram callback_data is limited to 64 bytes. Domain names can make
    # the old pickdomain:<pid>:<wid>:<domain> payload too long, which causes
    # BUTTON_DATA_INVALID. Keep the actual domain in the user's session and
    # send only a short callback token.
    import secrets
    choices = {}
    rows = []
    for d, _ in domains:
        token = secrets.token_hex(6)
        choices[token] = d
        rows.append([InlineKeyboardButton(text=d, callback_data=f"pickdomain:{token}")])
    sess.setdefault(c.from_user.id, {})["domain_choices"] = choices
    sess[c.from_user.id]["domain_assign_project"] = pid
    sess[c.from_user.id]["domain_assign_website"] = wid

    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data=f"site:{pid}:{wid}")])
    await safe_edit_text(c.message, "🌐 Assign Domain\n\nActive unassigned domains:", reply_markup=kb(rows))
    await c.answer()


@dp.callback_query(F.data.startswith("pickdomain:"))
async def pickdomain(c):
    token = c.data.split(":", 1)[1]
    s = sess.get(c.from_user.id, {})
    pid = s.get("domain_assign_project")
    wid = s.get("domain_assign_website")
    dom = s.get("domain_choices", {}).get(token)
    if not pid or not wid or not dom:
        return await c.answer("Domain selection expired. Please select Assign Domain again.", show_alert=True)
    w = store.data.get("websites", {}).get(wid)
    d = store.data.get("domains", {}).get(dom)
    if not w or not d or d.get("status") != "active" or d.get("website_id"):
        return await c.answer("Domain unavailable", show_alert=True)
    if d.get("project_id") and d.get("project_id") != pid:
        return await c.answer("This domain belongs to another project.", show_alert=True)
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
        await safe_edit_text(c.message, f"❌ Domain assignment failed:\n{e}", reply_markup=kb([[InlineKeyboardButton(text="⬅️ Website", callback_data=f"pw:{pid}")]]))
    await c.answer()


# -------------------- Deployments --------------------

async def reconcile_deployments():
    """Create missing deployment records for already-created Worker websites.

    Older bot versions could save a website/Worker without a deployment record.
    If a Worker URL exists, treat that as the persisted deployment evidence and
    recover the missing Deployment record without making a new Worker.
    """
    changed = False
    deployments = store.data.setdefault("deployments", {})
    for wid, w in store.data.get("websites", {}).items():
        if not w.get("worker_url"):
            continue
        if w.get("status") in {"deploy_failed", "error"}:
            continue
        live = [d for d in deployments.values() if d.get("website_id") == wid and d.get("status") == "live"]
        if live:
            # Keep the latest known URL/name in the live deployment record.
            for d in live:
                changed_fields = False
                for key in ("worker_name", "worker_url", "domain", "original_filename"):
                    value = w.get(key)
                    if value is not None and d.get(key) != value:
                        d[key] = value
                        changed_fields = True
                changed = changed or changed_fields
            continue
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
        await store.save("Reconcile existing Worker deployments")
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
            website_name = d.get("worker_name") or w.get("original_filename") or "Website"
            rows.append([
                InlineKeyboardButton(text=f"✏️ {website_name}", callback_data=f"changehtml:{pid}:{wid}"),
                InlineKeyboardButton(
                    text="🌐 Assigned" if (d.get("domain") or w.get("domain")) else "🌐 Assign Domain",
                    callback_data=f"assignsite:{pid}:{wid}",
                ),
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
    dest = p.get("email_destination")
    prefix = p.get("email_prefix")
    lines = [f"📧 Email Routing — {p['name']}", "", f"Active domains: {len(domains)}"]
    lines.append(f"Destination: {dest or 'Not set'}")
    lines.append(f"Prefix: {prefix or 'Not set'}")
    rows = []
    if not dest:
        rows.append([InlineKeyboardButton(text="➕ Set Gmail/Destination", callback_data=f"setdest:{pid}")])
    elif not prefix:
        rows.append([InlineKeyboardButton(text="➕ Set Email Prefix", callback_data=f"emailsetup:{pid}")])
        rows.append([InlineKeyboardButton(text="🔄 Check Verification", callback_data=f"emailverify:{pid}")])
    else:
        rows.append([InlineKeyboardButton(text="⚡ Setup All Project Domains", callback_data=f"emailsetup:{pid}")])
        rows.append([InlineKeyboardButton(text="🔄 Check Verification", callback_data=f"emailverify:{pid}")])
        rows.append([InlineKeyboardButton(text="✏️ Change Destination", callback_data=f"setdest:{pid}")])
    rows.append([InlineKeyboardButton(text="⬅️ Project", callback_data=f"viewp:{pid}")])
    await safe_edit_text(c.message, "\n".join(lines), reply_markup=kb(rows))
    await c.answer()


@dp.callback_query(F.data.startswith("setdest:"))
async def setdest(c):
    pid = c.data[8:]
    sess[c.from_user.id] = {"s": "email_destination", "project": pid}
    await safe_edit_text(c.message, "📧 Enter the destination email/Gmail address.\n\nExample: yourname@gmail.com")
    await c.answer()


@dp.callback_query(F.data.startswith("emailsetup:"))
async def emailsetup(c):
    pid = c.data[11:]
    p = project(pid)
    if not p:
        return await c.answer("Project not found", show_alert=True)
    if not p.get("email_destination"):
        sess[c.from_user.id] = {"s": "email_destination", "project": pid}
        await safe_edit_text(c.message, "📧 Enter the destination email/Gmail address.\n\nExample: yourname@gmail.com")
        return await c.answer()
    if not p.get("email_prefix"):
        sess[c.from_user.id] = {"s": "email_prefix", "project": pid}
        await safe_edit_text(c.message, "📧 Enter the email prefix.\n\nExample: info\n\nThis prefix will be created on ALL active domains in the project.")
        return await c.answer()
    sess[c.from_user.id] = {"s": "project", "project": pid}
    await safe_edit_text(c.message, "⏳ Checking verification and setting up all active project domains...")
    await bulk_email_setup(c.message, pid, p["email_prefix"])
    await c.answer()


@dp.callback_query(F.data.startswith("emailverify:"))
async def emailverify(c):
    pid = c.data[12:]
    p = project(pid)
    if not p:
        return await c.answer("Project not found", show_alert=True)
    dest = p.get("email_destination")
    if not dest:
        return await c.answer("Destination is not set.", show_alert=True)
    try:
        verified = await cf.email_destination_verified(dest)
        if verified:
            await safe_edit_text(c.message, f"✅ {dest} is verified.", reply_markup=kb([[InlineKeyboardButton(text="⚡ Setup All Project Domains", callback_data=f"emailsetup:{pid}")], [InlineKeyboardButton(text="⬅️ Email Routing", callback_data=f"pe:{pid}")]]))
        else:
            await safe_edit_text(c.message, f"⏳ {dest} is not verified yet.\n\nOpen the Cloudflare verification email, complete verification, then select Check Verification here.", reply_markup=kb([[InlineKeyboardButton(text="🔄 Check Verification", callback_data=f"emailverify:{pid}")], [InlineKeyboardButton(text="⬅️ Email Routing", callback_data=f"pe:{pid}")]]))
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


async def bulk_email_setup(message, pid, prefix, retry_failed_only=False):
    p = project(pid)
    dest = p.get("email_destination") if p else None
    if not dest or not p:
        await safe_edit_text(message, "❌ Destination/project missing.", reply_markup=kb([[InlineKeyboardButton(text="⬅️ Project", callback_data=f"viewp:{pid}")]]))
        return 0, 0, 0

    verified = await cf.email_destination_verified(dest)
    if not verified:
        await safe_edit_text(
            message,
            f"⏳ {dest} is not verified yet.\n\nVerification email me verify karo, phir Email Routing → Check Verification.",
            reply_markup=kb([[InlineKeyboardButton(text="🔄 Check Verification", callback_data=f"emailverify:{pid}")]])
        )
        return 0, 0, 0

    p["email_prefix"] = prefix
    active_domains = [d for d, _ in project_domains(pid, active_only=True)]
    if not active_domains:
        await safe_edit_text(message, "❌ This project has no active domains.", reply_markup=kb([[InlineKeyboardButton(text="⬅️ Project", callback_data=f"viewp:{pid}")]]))
        return 0, 0, 0

    ok_count = skipped = failed = 0
    created_list = []
    active_list = []
    failed_list = []
    records = store.data.setdefault("email_routing", {})

    # Retry button sirf unhi failed addresses ko dobara try karega.
    if retry_failed_only:
        targets = []
        for dom in active_domains:
            address = f"{prefix}@{dom}"
            rec = records.get(address) or {}
            if rec.get("status") == "error" and rec.get("project_id") == pid:
                targets.append(dom)
        if not targets:
            await safe_edit_text(
                message,
                "✅ Retry ke liye koi failed email routing nahi mila.",
                reply_markup=kb([[InlineKeyboardButton(text="⬅️ Email Routing", callback_data=f"pe:{pid}")]])
            )
            return 0, 0, 0
        active_domains = targets

    for dom in active_domains:
        d = store.data["domains"][dom]
        zone_id = d.get("zone_id")
        address = f"{prefix}@{dom}"
        existing = records.get(address)

        if not retry_failed_only and existing and existing.get("status") == "active":
            skipped += 1
            active_list.append(address)
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
                active_list.append(address)
            else:
                rule = await cf.email_rule(zone_id, address, dest)
                ok_count += 1
                created_list.append(address)

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
            error_text = str(e)
            failed_list.append((address, error_text))
            records[address] = {
                "address": address, "destination": dest, "domain": dom,
                "project_id": pid, "prefix": prefix, "status": "error",
                "error": error_text,
            }

    await store.save("Bulk email routing")

    lines = [
        "📧 Bulk Email Routing",
        "",
        f"✅ Created: {ok_count}",
        f"⏭ Already active: {skipped}",
        f"❌ Failed: {failed}",
        "",
        f"Destination: {dest}",
        f"Prefix: {prefix}",
    ]

    if created_list:
        lines += ["", "✅ Created:"]
        lines.extend(f"• {x}" for x in created_list)

    if active_list:
        lines += ["", "⏭ Already active:"]
        lines.extend(f"• {x}" for x in active_list)

    if failed_list:
        lines += ["", "❌ Failed:"]
        for address, error_text in failed_list:
            lines.append(f"• {address}")
            lines.append(f"  Reason: {error_text[:500]}")

    rows = []
    if failed_list:
        rows.append([InlineKeyboardButton(text="🔄 Retry Failed", callback_data=f"emailretry:{pid}")])
    rows.append([InlineKeyboardButton(text="⬅️ Email Routing", callback_data=f"pe:{pid}")])
    rows.append([InlineKeyboardButton(text="⬅️ Project", callback_data=f"viewp:{pid}")])

    await safe_edit_text(message, "\n".join(lines), reply_markup=kb(rows))
    return ok_count, skipped, failed


@dp.callback_query(F.data.startswith("emailretry:"))
async def emailretry(c):
    pid = c.data[11:]
    p = project(pid)
    if not p:
        return await c.answer("Project not found", show_alert=True)

    dest = p.get("email_destination")
    prefix = p.get("email_prefix")
    if not dest:
        return await c.answer("Destination is not set.", show_alert=True)
    if not prefix:
        return await c.answer("Email prefix is not set.", show_alert=True)

    await c.answer("Retrying failed email routing...")
    await safe_edit_text(c.message, "⏳ Retrying failed email routing setup...")
    await bulk_email_setup(c.message, pid, prefix, retry_failed_only=True)


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
    # Check the account-level destination list by email. The saved
    # destination_id can become stale if the destination was recreated
    # in Cloudflare, so do not rely on that ID for verification checks.
    target = email.strip().lower()
    base = f"https://api.cloudflare.com/client/v4/accounts/{cfg.cf_account}/email/routing/addresses"
    headers = {
        "Authorization": f"Bearer {cfg.cf_token}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        for page in range(1, 11):
            r = await client.get(
                base,
                headers=headers,
                params={"page": page, "per_page": 50},
            )
            if r.status_code >= 400:
                raise RuntimeError(
                    f"Cloudflare verification check {r.status_code}: {r.text[:700]}"
                )

            j = r.json()
            if not j.get("success"):
                raise RuntimeError(str(j.get("errors")))

            for item in j.get("result", []) or []:
                if str(item.get("email", "")).strip().lower() == target:
                    # Keep the current ID in storage in case the destination
                    # was recreated and its ID changed.
                    # Refresh the destination ID for the project that owns this
                    # destination. Do not use a global account setting here.
                    for pid, p in store.data.get("projects", {}).items():
                        if str(p.get("email_destination", "")).strip().lower() == target:
                            if item.get("id") and p.get("email_destination_id") != item.get("id"):
                                p["email_destination_id"] = item.get("id")
                                await store.save("Refresh project email destination ID")
                            break

                    # Cloudflare's `verified` field is a timestamp;
                    # null/missing means the address is not verified.
                    return bool(item.get("verified"))

            info = j.get("result_info") or {}
            total_pages = int(info.get("total_pages") or page)
            if page >= total_pages:
                break

    return False


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
                if d.get("status") == "external_registered_no_cloudflare" and not d.get("zone_id"):
                    before = json.dumps(d, sort_keys=True, default=str)
                    await finish_external_domain(dom, d)
                    after = json.dumps(d, sort_keys=True, default=str)
                    if after != before:
                        await store.save("External domain Cloudflare setup changed")
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
