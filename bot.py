import asyncio
import uuid
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
    for d, x in store.data["domains"].items():
        if x.get("status") == "pending" and x.get("zone_id"):
            try:
                z = await cf.zone_get(x["zone_id"])
                x["status"] = z.get("status", "pending")
            except Exception:
                pass
        if x.get("status") == "active":
            active += 1
        elif x.get("status") in ("pending", "registration_pending"):
            pending += 1
        elif x.get("status") == "error":
            error += 1
    await store.save("Manual domain status check")
    await c.message.edit_text(
        f"🔄 STATUS SUMMARY\n\n🟢 Active: {active}\n🟡 Pending: {pending}\n🔴 Error: {error}"
    )
    await c.answer()


@dp.callback_query(F.data == "dl")
async def dl(c):
    lines = []
    for d, x in store.data["domains"].items():
        icon = "🟢" if x.get("status") == "active" else "🟡" if x.get("status") in ("pending", "registration_pending") else "🔴"
        lines.append(f"{icon} {d}")
    await c.message.edit_text("\n".join(lines) or "No domains")
    await c.answer()


# -------------------- Spaceship contact setup --------------------

CONTACT_FIELDS = [
    ("firstName", "First name"),
    ("lastName", "Last name"),
    ("email", "Email"),
    ("phone", "Phone (with country code, e.g. +91...)"),
    ("address1", "Address"),
    ("city", "City"),
    ("stateProvince", "State"),
    ("postalCode", "PIN / Postal code"),
]


async def create_spaceship_contact(details):
    headers = {
        "X-API-Key": cfg.sp_key,
        "X-API-Secret": cfg.sp_secret,
        "Content-Type": "application/json",
    }
    body = dict(details)
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


async def start_contact_setup(c):
    sess[c.from_user.id] = {"s": "contact", "field": 0, "details": {}}
    await c.message.edit_text("⚙️ One-time Spaceship contact setup\n\nFirst name bhejo:")
    await c.answer()


@dp.callback_query(F.data == "contact_setup")
async def contact_setup(c):
    await start_contact_setup(c)


@dp.callback_query(F.data == "contact_cancel")
async def contact_cancel(c):
    sess.pop(c.from_user.id, None)
    await c.message.edit_text("❌ Contact setup cancelled.")
    await c.answer()


async def save_single_spaceship_contact(uid, contact_id):
    store.data.setdefault("settings", {})["spaceship_contact_id"] = contact_id
    store.data["settings"]["spaceship_contacts"] = {
        "registrant": contact_id,
        "admin": contact_id,
        "tech": contact_id,
        "billing": contact_id,
    }
    await store.save("Save Spaceship contact")


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

    contacts = store.data.setdefault("settings", {}).get("spaceship_contacts")
    if not contacts:
        await c.message.edit_text(
            "⚙️ One-time Spaceship contact setup required.\n\n"
            "Ek baar details do; same contact sabhi domains ke liye automatically use hoga.",
            reply_markup=kb([
                [InlineKeyboardButton(text="⚙️ Setup Contact", callback_data="contact_setup")],
                [InlineKeyboardButton(text="❌ Cancel", callback_data="contact_cancel")],
            ]),
        )
        s["s"] = "waiting_contact"
        await c.answer()
        return

    try:
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
                if d.get("status") == "registration_pending" and d.get("operation_id"):
                    r = await sp.operation(d["operation_id"])
                    if r.get("status") == "success":
                        z = await cf.zone_create(dom)
                        d.update(
                            status="pending",
                            zone_id=z["id"],
                            nameservers=z.get("name_servers", []),
                        )
                        await sp.nameservers(dom, z.get("name_servers", []))
                        await store.save("NS updated")
                    elif r.get("status") == "failed":
                        d["status"] = "error"
                        await store.save("Registration failed")
                elif d.get("status") == "pending" and d.get("zone_id"):
                    z = await cf.zone_get(d["zone_id"])
                    if z.get("status") == "active":
                        d["status"] = "active"
                        await store.save("Domain active")
            except Exception:
                pass
        await asyncio.sleep(cfg.poll_seconds)


async def main():
    await store.load()
    asyncio.create_task(worker())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
