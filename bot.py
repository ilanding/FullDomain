import asyncio
import uuid
import time
from pathlib import Path

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config import load_config
from storage import Store
from utils import base_html_name, names, valid_domain
from spaceship import Spaceship
from cloudflare import Cloudflare
from pages import Pages

cfg = load_config()
bot = Bot(cfg.bot_token)
dp = Dispatcher()
store = Store(cfg)
cf = Cloudflare(cfg)
sp = Spaceship(cfg)
pages = Pages(cf)
sess = {}


async def spaceship_domain_info(domain):
    """Directly verify whether a domain is actually registered in Spaceship.

    This is a fallback for cases where the async operation remains pending even
    though the registration has already completed in the Spaceship account.
    """
    url = f"{cfg.sp_base}/domains/{domain}"
    headers = {
        "X-API-Key": cfg.sp_key,
        "X-API-Secret": cfg.sp_secret,
        "Accept": "application/json",
    }
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
    """Move a confirmed Spaceship registration into Cloudflare setup."""
    if x.get("zone_id"):
        return
    z = await cf.zone_create(dom)
    x.update(
        status="pending",
        zone_id=z["id"],
        nameservers=z.get("name_servers", []),
        last_error=None,
    )
    hosts = z.get("name_servers", [])
    if hosts:
        await sp.nameservers(dom, hosts)


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ok(x):
    return x.from_user.id in cfg.admins


def two_col(buttons):
    """Turn a flat button list into a 2-column Telegram keyboard."""
    return [buttons[i:i + 2] for i in range(0, len(buttons), 2)]


async def menu(m):
    await m.answer(
        "Main Menu",
        reply_markup=kb([
            [InlineKeyboardButton(text="📁 Create Project", callback_data="pnew")],
            [InlineKeyboardButton(text="📂 My Projects", callback_data="plist")],
            [InlineKeyboardButton(text="🌐 Domains", callback_data="dmenu")],
            [InlineKeyboardButton(text="📧 Email Routing", callback_data="emenu")],
            [InlineKeyboardButton(text="📊 Deployments", callback_data="deploy")],
        ]),
    )


@dp.message(Command("start"))
async def start(m):
    if ok(m):
        await menu(m)


@dp.callback_query(F.data == "pnew")
async def pnew(c):
    sess[c.from_user.id] = {"s": "pname"}
    await c.message.edit_text("Project name?")
    await c.answer()


@dp.callback_query(F.data == "plist")
async def plist(c):
    text = "\n".join("📁 " + x for x in store.data["projects"]) or "No projects"
    await c.message.edit_text(text)
    await c.answer()


@dp.callback_query(F.data == "dmenu")
async def dmenu(c):
    await c.message.edit_text(
        "Domains",
        reply_markup=kb([
            [InlineKeyboardButton(text="🛒 Buy Domains", callback_data="buy")],
            [InlineKeyboardButton(text="➕ Add Existing Domains", callback_data="add")],
            [InlineKeyboardButton(text="🔄 Check Status", callback_data="check")],
            [InlineKeyboardButton(text="📋 My Domains", callback_data="dl")],
            [InlineKeyboardButton(text="❌ Failed Domains", callback_data="failed_domains")],
        ]),
    )
    await c.answer()


@dp.callback_query(F.data == "buy")
async def buy(c):
    sess[c.from_user.id] = {"s": "buynames"}
    await c.message.edit_text("Base names bhejo: sonu monu raju")
    await c.answer()


@dp.callback_query(F.data == "add")
async def add(c):
    sess[c.from_user.id] = {"s": "add"}
    await c.message.edit_text("Existing domains one per line.")
    await c.answer()


@dp.callback_query(F.data == "check")
async def check(c):
    active = pending = error = 0
    lines = ["🔄 DOMAIN STATUS CHECK", ""]
    changed = False

    for dom, x in store.data["domains"].items():
        status = x.get("status")

        # Registration can complete at Spaceship even when the async operation
        # endpoint is still reporting pending. Verify the real domain record as
        # a fallback, but do not hammer the endpoint because it has a per-domain
        # rate limit.
        if status in ("registration_pending", "error") and not x.get("zone_id"):
            op = x.get("operation_id")
            op_result = None
            op_error = None

            if op:
                try:
                    op_result = await sp.operation(op)
                    rs = str(op_result.get("status", "pending")).lower()
                    if rs == "success":
                        await finish_registration(dom, x)
                        status = x["status"]
                        changed = True
                    elif rs == "failed":
                        op_error = op_result.get("error") or op_result.get("message") or str(op_result)
                    else:
                        status = "registration_pending"
                except Exception as e:
                    op_error = str(e)
            else:
                op_error = "No Spaceship operation ID was saved."

            # Direct domain lookup is the source-of-truth fallback. If the
            # domain already exists in the account, continue automatically.
            if status in ("registration_pending", "error") and not x.get("zone_id"):
                try:
                    info = await spaceship_domain_info(dom)
                    x["last_spaceship_verify_ts"] = time.time()
                    if domain_is_registered(info):
                        await finish_registration(dom, x)
                        status = x["status"]
                        x["spaceship_lifecycle_status"] = info.get("lifecycleStatus")
                        x["spaceship_verification_status"] = info.get("verificationStatus")
                        changed = True
                    elif op_result is not None and str(op_result.get("status", "")).lower() == "failed":
                        x["status"] = "error"
                        x["last_error"] = str(op_error or "Spaceship registration failed")
                        status = "error"
                        changed = True
                    else:
                        x["status"] = "registration_pending"
                        if op_error and not str(op_error).startswith("Spaceship domain verification"):
                            x["last_error"] = None
                        status = "registration_pending"
                except Exception as e:
                    # A temporary verification/API error must not turn a real
                    # pending registration into a false failure.
                    if op_result is not None and str(op_result.get("status", "")).lower() == "failed":
                        x["status"] = "error"
                        x["last_error"] = str(op_error or e)
                        status = "error"
                        changed = True
                    else:
                        x["status"] = "registration_pending"
                        status = "registration_pending"
                        x["last_error"] = None

        # Then check Cloudflare activation if registration has completed.
        if status == "pending" and x.get("zone_id"):
            try:
                z = await cf.zone_get(x["zone_id"])
                x["status"] = z.get("status", "pending")
                status = x["status"]
                changed = True
            except Exception as e:
                x["last_error"] = str(e)

        if status == "active":
            active += 1
            lines.append(f"🟢 {dom} — Active")
        elif status == "registration_pending":
            pending += 1
            lines.append(f"⏳ {dom} — Registration pending")
        elif status == "pending":
            pending += 1
            lines.append(f"🟡 {dom} — Cloudflare pending")
        elif status == "error":
            error += 1
            err = x.get("last_error") or "Unknown error"
            lines.append(f"🔴 {dom} — Error\n   {str(err)[:250]}")

    if changed:
        await store.save("Manual domain status check")

    lines.extend(["", f"🟢 Active: {active}", f"🟡 Pending: {pending}", f"🔴 Error: {error}"])
    lines.append("")
    lines.append("Tap 🔄 Check Status again anytime to refresh.")

    await c.message.edit_text("\n".join(lines), reply_markup=kb([
        [InlineKeyboardButton(text="🔄 Check Status", callback_data="check")],
        [InlineKeyboardButton(text="⬅️ Domains", callback_data="dmenu")],
    ]))
    await c.answer("Status refreshed")

@dp.callback_query(F.data == "dl")
async def dl(c):
    # My Domains = only successfully registered/active domains.
    active = [d for d, x in store.data["domains"].items() if x.get("status") == "active"]
    if not active:
        text = "📋 MY DOMAINS\n\nNo active domains yet."
    else:
        text = "📋 MY DOMAINS\n\n" + "\n".join(f"🟢 {d}" for d in sorted(active))
    await c.message.edit_text(text, reply_markup=kb([
        [InlineKeyboardButton(text="🔄 Check Status", callback_data="check")],
        [InlineKeyboardButton(text="❌ Failed Domains", callback_data="failed_domains")],
        [InlineKeyboardButton(text="⬅️ Domains", callback_data="dmenu")],
    ]))
    await c.answer()


@dp.callback_query(F.data == "failed_domains")
async def failed_domains(c):
    failed = [
        (d, x.get("last_error") or "Unknown error")
        for d, x in store.data["domains"].items()
        if x.get("status") == "error"
    ]
    if not failed:
        await c.message.edit_text("❌ No failed domains.", reply_markup=kb([
            [InlineKeyboardButton(text="⬅️ Domains", callback_data="dmenu")]
        ]))
        return await c.answer()

    lines = ["❌ FAILED DOMAINS", ""]
    buttons = []
    for d, err in sorted(failed):
        lines.append(f"🔴 {d}\n   {str(err)[:300]}")
        buttons.append(InlineKeyboardButton(text=f"🔄 Retry {d}", callback_data="retry:" + d))
    rows = two_col(buttons)
    rows.append([InlineKeyboardButton(text="🔄 Check Status", callback_data="check")])
    rows.append([InlineKeyboardButton(text="⬅️ Domains", callback_data="dmenu")])
    await c.message.edit_text("\n".join(lines), reply_markup=kb(rows))
    await c.answer()


@dp.callback_query(F.data.startswith("retry:"))
async def retry_purchase(c):
    dom = c.data[6:]
    d = store.data["domains"].get(dom)
    if not d or d.get("status") != "error":
        return await c.answer("Domain retry unavailable", show_alert=True)

    try:
        # Re-check availability and CURRENT price before every retry.
        r = await sp.availability([dom])
        items = r if isinstance(r, list) else r.get("domains", r.get("results", []))
        item = next((q for q in items if str(q.get("domain", "")).lower() == dom.lower()), None)
        if not item or item.get("result") != "available":
            d["last_error"] = "Domain is no longer available for registration."
            await store.save("Retry availability failed")
            return await c.message.edit_text(
                f"❌ {dom}\n\nDomain is no longer available for registration.",
                reply_markup=kb([[InlineKeyboardButton(text="⬅️ Domains", callback_data="dmenu")]])
            )

        price = item.get("price")
        sess[c.from_user.id] = {
            "s": "retry_confirm",
            "pending_buy": dom,
            "pending_price": price,
        }
        price_text = f"\n💵 Current price: {price}" if price else ""
        await c.message.edit_text(
            f"🔄 Retry purchase\n\n🌐 {dom}{price_text}\n\nConfirm purchase?",
            reply_markup=kb([
                [InlineKeyboardButton(text="✅ Confirm Purchase", callback_data="retry_confirm")],
                [InlineKeyboardButton(text="❌ Cancel", callback_data="retry_cancel")],
            ])
        )
    except Exception as e:
        await c.message.edit_text(
            f"❌ Could not re-check {dom}:\n{e}",
            reply_markup=kb([[InlineKeyboardButton(text="⬅️ Domains", callback_data="dmenu")]])
        )
    await c.answer()


@dp.callback_query(F.data == "retry_cancel")
async def retry_cancel(c):
    sess.pop(c.from_user.id, None)
    await c.message.edit_text("❌ Retry cancelled", reply_markup=kb([
        [InlineKeyboardButton(text="❌ Failed Domains", callback_data="failed_domains")],
        [InlineKeyboardButton(text="⬅️ Domains", callback_data="dmenu")],
    ]))
    await c.answer()


@dp.callback_query(F.data == "retry_confirm")
async def retry_confirm(c):
    s = sess.get(c.from_user.id, {})
    dom = s.get("pending_buy")
    if not dom:
        return await c.answer("Retry session expired", show_alert=True)

    try:
        contacts = await get_or_create_spaceship_contact()
        r = await sp.register(dom, contacts)
        op = r.headers.get("spaceship-async-operationid")
        store.data["domains"][dom] = {
            "domain": dom,
            "registrar": "spaceship",
            "status": "registration_pending",
            "operation_id": op,
            "assigned": False,
        }
        await store.save("Retry purchase submitted")
        await c.message.edit_text(
            f"⏳ {dom} retry registration submitted.\n\nUse 🔄 Check Status to see the result.",
            reply_markup=kb([[InlineKeyboardButton(text="🔄 Check Status", callback_data="check")]])
        )
    except Exception as e:
        await c.message.edit_text(f"❌ Retry purchase failed:\n{e}")
    finally:
        sess.pop(c.from_user.id, None)
    await c.answer()


# -------------------- Spaceship contact --------------------

# One fixed contact for ALL domain purchases.
# The bot creates this contact automatically on the first purchase and
# reuses the returned contact ID for registrant/admin/tech/billing.
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
    headers = {
        "X-API-Key": cfg.sp_key,
        "X-API-Secret": cfg.sp_secret,
        "Content-Type": "application/json",
    }
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
    contacts = {
        "registrant": contact_id,
        "admin": contact_id,
        "tech": contact_id,
        "billing": contact_id,
    }
    settings["spaceship_contact_id"] = contact_id
    settings["spaceship_contacts"] = contacts
    await store.save("Create/save Spaceship contact")
    return contacts

# -------------------- File upload --------------------

@dp.message(F.document)
async def doc(m):
    s = sess.get(m.from_user.id, {})
    if s.get("s") not in ("html", "bulk"):
        return
    try:
        base = base_html_name(m.document.file_name or "")
    except ValueError:
        return await m.answer("❌ Only .html")

    Path("runtime").mkdir(exist_ok=True)
    f = await bot.get_file(m.document.file_id)
    p = Path("runtime") / (uuid.uuid4().hex + ".html")
    await bot.download_file(f.file_path, p)

    wid = str(uuid.uuid4())
    store.data["websites"][wid] = {
        "id": wid,
        "project_id": s["project"],
        "original_filename": m.document.file_name,
        "base_name": base,
        "path": str(p),
        "domain": None,
        "pages_project": None,
        "status": "uploaded",
    }
    store.data["projects"][s["project"]]["websites"].append(wid)
    await store.save("HTML upload")
    await m.answer(f"✅ {m.document.file_name} added")


@dp.message()
async def txt(m):
    s = sess.get(m.from_user.id, {})
    st = s.get("s")

    if st == "pname":
        name = m.text.strip()
        store.data["projects"][name] = {"name": name, "websites": []}
        await store.save("Create project")
        sess[m.from_user.id] = {"s": "project", "project": name}
        await m.answer(
            f"📁 {name}",
            reply_markup=kb([
                [InlineKeyboardButton(text="📄 Single HTML", callback_data="hs")],
                [InlineKeyboardButton(text="📚 Bulk HTML", callback_data="hb")],
                [InlineKeyboardButton(text="🌐 Assign Domains", callback_data="assign")],
            ]),
        )
        return

    if st == "contact":
        field_index = s.get("field", 0)
        if field_index >= len(CONTACT_FIELDS):
            return
        key, label = CONTACT_FIELDS[field_index]
        value = (m.text or "").strip()
        if not value:
            return await m.answer(f"❌ {label} required. Dobara bhejo:")

        # Spaceship expects phone numbers in its API format:
        # +<country-code>.<subscriber-number>
        # Example for India: +91.9876543210
        if key == "phone":
            raw = value.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
            if raw.startswith("00"):
                raw = "+" + raw[2:]
            if raw.startswith("+91"):
                digits = raw[3:]
                if digits.isdigit() and len(digits) == 10:
                    value = "+91." + digits
                else:
                    return await m.answer("❌ Indian phone number galat hai. 10-digit mobile number bhejo, jaise 9876543210")
            elif raw.isdigit() and len(raw) == 10:
                value = "+91." + raw
            elif raw.startswith("+") and "." not in raw:
                # Generic international number: +<cc><number> -> +<cc>.<number>
                digits = raw[1:]
                if not digits.isdigit() or len(digits) < 7:
                    return await m.answer("❌ Phone format galat hai. Example: +91 9876543210")
                # For non-India numbers, take 1-3 digit country code. India is handled above.
                cc_len = 3 if len(digits) >= 10 else 1
                value = "+" + digits[:cc_len] + "." + digits[cc_len:]
            else:
                return await m.answer("❌ Phone format galat hai. Example: 9876543210 ya +91 9876543210")

        s["details"][key] = value
        next_index = field_index + 1
        if next_index < len(CONTACT_FIELDS):
            s["field"] = next_index
            await m.answer(CONTACT_FIELDS[next_index][1] + ":")
            return

        await m.answer("⏳ Spaceship contact create ho raha hai...")
        try:
            contact_id = await create_spaceship_contact(s["details"])
            await save_single_spaceship_contact(m.from_user.id, contact_id)
            sess.pop(m.from_user.id, None)
            await m.answer("✅ Contact setup complete. Yehi contact ab sabhi domains ke liye use hoga.")
        except Exception as e:
            # If Spaceship rejects the contact, let the user correct the phone
            # without restarting the whole setup.
            if "phone" in str(e).lower():
                s["field"] = 3
                await m.answer(
                    "❌ Contact create nahi hua.\n"
                    "Spaceship ko phone is format me chahiye: +91.9876543210\n\n"
                    "Apna 10-digit mobile number dobara bhejo:"
                )
            else:
                await m.answer(f"❌ Contact create nahi hua:\n{e}")
        return

    if st == "buynames":
        cand = [n + t for n in names(m.text) for t in cfg.tlds]
        out = []
        for i in range(0, len(cand), 20):
            r = await sp.availability(cand[i:i + 20])
            items = r if isinstance(r, list) else r.get("domains", r.get("results", []))
            for q in items:
                if q.get("result") == "available":
                    out.append((q["domain"], q.get("price")))

        sess[m.from_user.id] = {"s": "buylist", "items": out}
        buttons = [
            InlineKeyboardButton(
                text=f"💰 {d}" + (f" — {p}" if p else ""),
                callback_data="b:" + d,
            )
            for d, p in out
        ]
        rows = two_col(buttons)
        rows.append([InlineKeyboardButton(text="✅ Done Buying", callback_data="donebuy")])
        await m.answer("Available:", reply_markup=kb(rows))
        return

    if st == "add":
        for d in [x.strip().lower() for x in m.text.splitlines() if x.strip()]:
            if not valid_domain(d):
                continue
            try:
                z = await cf.zone_create(d)
                store.data["domains"][d] = {
                    "domain": d,
                    "registrar": "external",
                    "zone_id": z["id"],
                    "nameservers": z.get("name_servers", []),
                    "status": "pending",
                    "assigned": False,
                }
            except Exception as e:
                await m.answer(f"❌ {d}: {e}")
        await store.save("Add domains")
        sess.pop(m.from_user.id, None)
        await m.answer("✅ Done")


@dp.callback_query(F.data.in_({"hs", "hb"}))
async def hm(c):
    s = "html" if c.data == "hs" else "bulk"
    project = sess.get(c.from_user.id, {}).get("project")
    if not project:
        return await c.answer("Create/open a project first", show_alert=True)
    sess[c.from_user.id] = {"s": s, "project": project}
    await c.message.edit_text("HTML file(s) upload karo")
    await c.answer()


# -------------------- Domain purchase --------------------

@dp.callback_query(F.data.startswith("b:"))
async def b(c):
    dom = c.data[2:]
    s = sess.get(c.from_user.id, {})
    if not any(d == dom for d, p in s.get("items", [])):
        return await c.answer("Already removed", show_alert=True)

    price = next((p for d, p in s["items"] if d == dom), None)
    s["items"] = [x for x in s["items"] if x[0] != dom]
    s["pending_buy"] = dom
    s["pending_price"] = price
    s["s"] = "confirm"

    price_text = f"\n💵 Price: {price}" if price else ""
    await c.message.edit_text(
        f"🛒 {dom}{price_text}\n\nConfirm purchase?",
        reply_markup=kb([
            [InlineKeyboardButton(text="✅ Confirm Purchase", callback_data="confirm")],
            [InlineKeyboardButton(text="❌ Cancel", callback_data="cancel")],
        ]),
    )
    await c.answer()


async def restore_buy_list_message(c):
    s = sess.get(c.from_user.id, {})
    items = s.get("items", [])
    buttons = [
        InlineKeyboardButton(
            text=f"💰 {d}" + (f" — {p}" if p else ""),
            callback_data="b:" + d,
        )
        for d, p in items
    ]
    rows = two_col(buttons)
    rows.append([InlineKeyboardButton(text="✅ Done Buying", callback_data="donebuy")])
    await c.message.edit_text("Available:", reply_markup=kb(rows))


@dp.callback_query(F.data == "cancel")
async def cancel(c):
    s = sess.get(c.from_user.id, {})
    dom = s.get("pending_buy")
    price = s.get("pending_price")
    if dom:
        s.setdefault("items", []).append((dom, price))
    s["items"] = sorted(s.get("items", []), key=lambda x: x[0])
    s["s"] = "buylist"
    s.pop("pending_buy", None)
    s.pop("pending_price", None)
    await restore_buy_list_message(c)
    await c.answer("Cancelled")


@dp.callback_query(F.data == "confirm")
async def confirm(c):
    s = sess.get(c.from_user.id, {})
    dom = s.get("pending_buy")
    if not dom:
        return await c.answer("Purchase session expired", show_alert=True)

    try:
        # Create the one hard-coded Spaceship contact automatically on the
        # first purchase; reuse the same contact for every later domain.
        contacts = await get_or_create_spaceship_contact()
        r = await sp.register(dom, contacts)
        op = r.headers.get("spaceship-async-operationid")
        store.data["domains"][dom] = {
            "domain": dom,
            "registrar": "spaceship",
            "status": "registration_pending",
            "operation_id": op,
            "assigned": False,
        }
        await store.save("Purchase submitted")
        await c.message.edit_text(f"⏳ {dom} registration submitted")
    except Exception as e:
        # Put the domain back into the list if registration request failed.
        s.setdefault("items", []).append((dom, s.get("pending_price")))
        s["items"] = sorted(s["items"], key=lambda x: x[0])
        await c.message.edit_text(f"❌ Purchase request failed:\n{e}")

    s["s"] = "buylist"
    s.pop("pending_buy", None)
    s.pop("pending_price", None)
    await c.answer()


@dp.callback_query(F.data == "donebuy")
async def done(c):
    sess.pop(c.from_user.id, None)
    await c.message.edit_text("📊 Purchase session ended. Use 🔄 Check Status after propagation.")
    await c.answer()


# -------------------- Domain assignment --------------------

@dp.callback_query(F.data == "assign")
async def assign(c):
    project = sess.get(c.from_user.id, {}).get("project")
    if not project:
        return await c.message.edit_text("Open a project first")

    sites = [x for x in store.data["websites"].values() if x["project_id"] == project and not x.get("domain")]
    domains = [x for x in store.data["domains"].values() if x.get("status") == "active" and not x.get("assigned")]
    if not sites or not domains:
        return await c.message.edit_text("No unassigned HTML or active domains.")

    sess[c.from_user.id] = {
        "s": "assign",
        "project": project,
        "site": sites[0]["id"],
        "rest": [x["id"] for x in sites[1:]],
    }
    await show_domains(c.from_user.id, c.message)
    await c.answer()


async def show_domains(uid, message=None):
    s = sess[uid]
    ds = [x for x in store.data["domains"].values() if x.get("status") == "active" and not x.get("assigned")]
    buttons = [InlineKeyboardButton(text=d["domain"], callback_data="pick:" + d["domain"]) for d in ds]
    rows = two_col(buttons)
    rows.append([
        InlineKeyboardButton(text="⏭ Skip", callback_data="skip"),
        InlineKeyboardButton(text="❌ Cancel", callback_data="ac"),
    ])
    text = f"Select domain for {store.data['websites'][s['site']]['original_filename']}"
    if message:
        await message.edit_text(text, reply_markup=kb(rows))
    else:
        await bot.send_message(uid, text, reply_markup=kb(rows))


@dp.callback_query(F.data.startswith("pick:"))
async def pick(c):
    dom = c.data[5:]
    s = sess.get(c.from_user.id, {})
    d = store.data["domains"].get(dom)
    if not d or d.get("assigned") or d.get("status") != "active":
        return await c.answer("Unavailable", show_alert=True)

    w = store.data["websites"][s["site"]]
    d["assigned"] = True
    d["project_id"] = s["project"]
    d["website_id"] = w["id"]
    w["domain"] = dom
    await store.save("Assign domain")
    await c.message.edit_text(f"✅ {w['original_filename']} → {dom}")

    if s["rest"]:
        s["site"] = s["rest"].pop(0)
        await show_domains(c.from_user.id, c.message)
    else:
        sess.pop(c.from_user.id, None)
        await c.message.edit_text("✅ Assignment complete.")
    await c.answer()


@dp.callback_query(F.data == "skip")
async def skip(c):
    s = sess.get(c.from_user.id)
    if not s or not s["rest"]:
        sess.pop(c.from_user.id, None)
        await c.message.edit_text("⏭ Assignment finished.")
        return await c.answer()
    s["site"] = s["rest"].pop(0)
    await show_domains(c.from_user.id, c.message)
    await c.answer()


@dp.callback_query(F.data == "ac")
async def ac(c):
    sess.pop(c.from_user.id, None)
    await c.message.edit_text("❌ Assignment cancelled")
    await c.answer()


# -------------------- Background status worker --------------------

async def worker():
    while True:
        for dom, d in list(store.data["domains"].items()):
            try:
                if d.get("status") == "registration_pending" and not d.get("zone_id"):
                    op_result = None
                    op_error = None
                    op = d.get("operation_id")

                    if op:
                        try:
                            op_result = await sp.operation(op)
                        except Exception as e:
                            op_error = str(e)

                    rs = str((op_result or {}).get("status", "pending")).lower()
                    if rs == "success":
                        await finish_registration(dom, d)
                        await store.save("Registration completed and NS updated")
                        continue

                    # The async operation can lag behind the real account state.
                    # Direct verification is rate-limited by Spaceship, so the
                    # background worker performs it at most once every 4 minutes
                    # per domain. Manual Check Status can still verify immediately.
                    last_verify = float(d.get("last_spaceship_verify_ts", 0) or 0)
                    if time.time() - last_verify >= 240:
                        try:
                            info = await spaceship_domain_info(dom)
                            d["last_spaceship_verify_ts"] = time.time()
                            if domain_is_registered(info):
                                await finish_registration(dom, d)
                                d["spaceship_lifecycle_status"] = info.get("lifecycleStatus")
                                d["spaceship_verification_status"] = info.get("verificationStatus")
                                await store.save("Registration verified from domain info")
                                continue
                        except Exception:
                            # Do not mark a pending registration as failed because
                            # of a temporary verification error or API rate limit.
                            d["last_spaceship_verify_ts"] = time.time()
                            d["last_error"] = None

                    if rs == "failed":
                        d["status"] = "error"
                        d["last_error"] = (op_result or {}).get("error") or (op_result or {}).get("message") or str(op_result)
                        await store.save("Registration failed")
                    else:
                        d["status"] = "registration_pending"
                        d["last_error"] = None

                elif d.get("status") == "pending" and d.get("zone_id"):
                    z = await cf.zone_get(d["zone_id"])
                    if z.get("status") == "active":
                        d["status"] = "active"
                        await store.save("Domain active")
            except Exception as e:
                d["last_error"] = str(e)
                try:
                    await store.save("Domain status check error")
                except Exception:
                    pass
        await asyncio.sleep(cfg.poll_seconds)

async def main():
    await store.load()
    asyncio.create_task(worker())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
