import asyncio,uuid
from pathlib import Path
from aiogram import Bot,Dispatcher,F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup,InlineKeyboardButton
from config import load_config
from storage import Store
from utils import base_html_name,names,valid_domain
from spaceship import Spaceship
from cloudflare import Cloudflare
from pages import Pages
cfg=load_config(); bot=Bot(cfg.bot_token); dp=Dispatcher(); store=Store(cfg)
cf=Cloudflare(cfg); sp=Spaceship(cfg); pages=Pages(cf); sess={}
def kb(rows):return InlineKeyboardMarkup(inline_keyboard=rows)
def ok(x):return x.from_user.id in cfg.admins
async def menu(m):await m.answer("Main Menu",reply_markup=kb([[InlineKeyboardButton(text="📁 Create Project",callback_data="pnew")],[InlineKeyboardButton(text="📂 My Projects",callback_data="plist")],[InlineKeyboardButton(text="🌐 Domains",callback_data="dmenu")],[InlineKeyboardButton(text="📧 Email Routing",callback_data="emenu")],[InlineKeyboardButton(text="📊 Deployments",callback_data="deploy")]]))
@dp.message(Command("start"))
async def start(m):
    if ok(m):await menu(m)
@dp.callback_query(F.data=="pnew")
async def pnew(c):sess[c.from_user.id]={"s":"pname"};await c.message.answer("Project name?");await c.answer()
@dp.callback_query(F.data=="plist")
async def plist(c):
    await c.message.answer("\n".join("📁 "+x for x in store.data["projects"]) or "No projects");await c.answer()
@dp.callback_query(F.data=="dmenu")
async def dmenu(c):await c.message.answer("Domains",reply_markup=kb([[InlineKeyboardButton(text="🛒 Buy Domains",callback_data="buy")],[InlineKeyboardButton(text="➕ Add Existing Domains",callback_data="add")],[InlineKeyboardButton(text="🔄 Check Status",callback_data="check")],[InlineKeyboardButton(text="📋 My Domains",callback_data="dl")]]));await c.answer()
@dp.callback_query(F.data=="buy")
async def buy(c):sess[c.from_user.id]={"s":"buynames"};await c.message.answer("Base names bhejo: sonu monu raju");await c.answer()
@dp.callback_query(F.data=="add")
async def add(c):sess[c.from_user.id]={"s":"add"};await c.message.answer("Existing domains one per line.");await c.answer()
@dp.callback_query(F.data=="check")
async def check(c):
    active=pending=error=0
    for d,x in store.data["domains"].items():
        if x.get("status")=="pending" and x.get("zone_id"):
            try:
                z=await cf.zone_get(x["zone_id"]);x["status"]=z.get("status","pending")
            except:pass
        if x.get("status")=="active":active+=1
        elif x.get("status")=="pending":pending+=1
        elif x.get("status")=="error":error+=1
    await store.save("Manual domain status check");await c.message.answer(f"🔄 STATUS SUMMARY\n\n🟢 Active: {active}\n🟡 Pending: {pending}\n🔴 Error: {error}");await c.answer()
@dp.callback_query(F.data=="dl")
async def dl(c):
    lines=[]
    for d,x in store.data["domains"].items():lines.append(f"{'🟢' if x.get('status')=='active' else '🟡' if x.get('status')=='pending' else '🔴'} {d}")
    await c.message.answer("\n".join(lines) or "No domains");await c.answer()
@dp.message(F.document)
async def doc(m):
    s=sess.get(m.from_user.id,{})
    if s.get("s") not in ("html","bulk"):return
    try:base=base_html_name(m.document.file_name or "")
    except ValueError:return await m.answer("❌ Only .html")
    Path("runtime").mkdir(exist_ok=True);f=await bot.get_file(m.document.file_id);p=Path("runtime")/(uuid.uuid4().hex+".html");await bot.download_file(f.file_path,p)
    wid=str(uuid.uuid4());store.data["websites"][wid]={"id":wid,"project_id":s["project"],"original_filename":m.document.file_name,"base_name":base,"path":str(p),"domain":None,"pages_project":None,"status":"uploaded"}
    store.data["projects"][s["project"]]["websites"].append(wid);await store.save("HTML upload");await m.answer(f"✅ {m.document.file_name} added")
@dp.message()
async def txt(m):
    s=sess.get(m.from_user.id,{});st=s.get("s")
    if st=="pname":
        name=m.text.strip();store.data["projects"][name]={"name":name,"websites":[]};await store.save("Create project");sess[m.from_user.id]={"s":"project","project":name};await m.answer(f"📁 {name}",reply_markup=kb([[InlineKeyboardButton(text="📄 Single HTML",callback_data="hs")],[InlineKeyboardButton(text="📚 Bulk HTML",callback_data="hb")],[InlineKeyboardButton(text="🌐 Assign Domains",callback_data="assign")]]))
    elif st=="buynames":
        cand=[n+t for n in names(m.text) for t in cfg.tlds];out=[]
        for i in range(0,len(cand),20):
            r=await sp.availability(cand[i:i+20]);items=r if isinstance(r,list) else r.get("domains",r.get("results",[]))
            for q in items:
                if q.get("result")=="available":out.append((q["domain"],q.get("price")))
        sess[m.from_user.id]={"s":"buylist","items":out};rows=[[InlineKeyboardButton(text=f"💰 Buy {d}"+(f" — {p}" if p else ""),callback_data="b:"+d)]for d,p in out]+[[InlineKeyboardButton(text="✅ Done Buying",callback_data="donebuy")]]
        await m.answer("Available:",reply_markup=kb(rows))
    elif st=="add":
        for d in [x.strip().lower() for x in m.text.splitlines() if x.strip()]:
            if not valid_domain(d):continue
            try:
                z=await cf.zone_create(d);store.data["domains"][d]={"domain":d,"registrar":"external","zone_id":z["id"],"nameservers":z.get("name_servers",[]),"status":"pending","assigned":False}
            except Exception as e:await m.answer(f"❌ {d}: {e}")
        await store.save("Add domains");sess.pop(m.from_user.id,None);await m.answer("✅ Done")
@dp.callback_query(F.data.in_({"hs","hb"}))
async def hm(c):
    s="html" if c.data=="hs" else "bulk";project=sess.get(c.from_user.id,{}).get("project")
    if not project:return await c.answer("Create/open a project first",show_alert=True)
    sess[c.from_user.id]={"s":s,"project":project};await c.message.answer("HTML file(s) upload karo");await c.answer()
@dp.callback_query(F.data.startswith("b:"))
async def b(c):
    dom=c.data[2:];s=sess.get(c.from_user.id,{})
    if not any(d==dom for d,p in s.get("items",[])):return await c.answer("Already removed",show_alert=True)
    s["items"]=[x for x in s["items"] if x[0]!=dom];s["pending_buy"]=dom;s["s"]="confirm"
    await c.message.answer(f"🛒 {dom}\nConfirm purchase?",reply_markup=kb([[InlineKeyboardButton(text="✅ Confirm Purchase",callback_data="confirm")],[InlineKeyboardButton(text="❌ Cancel",callback_data="cancel")]]));await c.answer()
@dp.callback_query(F.data=="cancel")
async def cancel(c):
    s=sess.get(c.from_user.id,{});dom=s.get("pending_buy");s["items"].append((dom,None));s["s"]="buylist";await c.message.answer("❌ Cancelled");await c.answer()
@dp.callback_query(F.data=="confirm")
async def confirm(c):
    s=sess.get(c.from_user.id,{});dom=s.get("pending_buy");contacts=store.data["settings"].get("spaceship_contacts")
    if not contacts:return await c.message.answer("❌ Configure Spaceship contact IDs in settings first.")
    try:
        r=await sp.register(dom,contacts);op=r.headers.get("spaceship-async-operationid")
        store.data["domains"][dom]={"domain":dom,"registrar":"spaceship","status":"registration_pending","operation_id":op,"assigned":False}
        await store.save("Purchase submitted");await c.message.answer(f"⏳ {dom} registration submitted")
    except Exception as e:await c.message.answer(f"❌ {e}")
    s["s"]="buylist";s.pop("pending_buy",None);await c.answer()
@dp.callback_query(F.data=="donebuy")
async def done(c):
    s=sess.pop(c.from_user.id,{});await c.message.answer("📊 Purchase session ended. Use 🔄 Check Status after propagation.");await c.answer()
@dp.callback_query(F.data=="assign")
async def assign(c):
    project=sess.get(c.from_user.id,{}).get("project")
    if not project:return await c.message.answer("Open a project first")
    sites=[x for x in store.data["websites"].values() if x["project_id"]==project and not x.get("domain")]
    domains=[x for x in store.data["domains"].values() if x.get("status")=="active" and not x.get("assigned")]
    if not sites or not domains:return await c.message.answer("No unassigned HTML or active domains.")
    sess[c.from_user.id]={"s":"assign","project":project,"site":sites[0]["id"],"rest":[x["id"] for x in sites[1:]]}
    await show_domains(c.from_user.id)
async def show_domains(uid):
    s=sess[uid];ds=[x for x in store.data["domains"].values() if x.get("status")=="active" and not x.get("assigned")]
    rows=[[InlineKeyboardButton(text=d["domain"],callback_data="pick:"+d["domain"])]for d in ds]+[[InlineKeyboardButton(text="⏭ Skip",callback_data="skip"),InlineKeyboardButton(text="❌ Cancel",callback_data="ac")]]
    await bot.send_message(uid,f"Select domain for {store.data['websites'][s['site']]['original_filename']}",reply_markup=kb(rows))
@dp.callback_query(F.data.startswith("pick:"))
async def pick(c):
    dom=c.data[5:];s=sess[c.from_user.id];d=store.data["domains"].get(dom)
    if not d or d.get("assigned") or d.get("status")!="active":return await c.answer("Unavailable",show_alert=True)
    w=store.data["websites"][s["site"]];d["assigned"]=True;d["project_id"]=s["project"];d["website_id"]=w["id"];w["domain"]=dom
    await store.save("Assign domain");await c.message.answer(f"✅ {w['original_filename']} → {dom}")
    if s["rest"]:s["site"]=s["rest"].pop(0);await show_domains(c.from_user.id)
    else:sess.pop(c.from_user.id,None);await c.message.answer("✅ Assignment complete.")
    await c.answer()
@dp.callback_query(F.data=="skip")
async def skip(c):
    s=sess.get(c.from_user.id)
    if not s or not s["rest"]:sess.pop(c.from_user.id,None);return await c.answer()
    s["site"]=s["rest"].pop(0);await show_domains(c.from_user.id);await c.answer()
@dp.callback_query(F.data=="ac")
async def ac(c):sess.pop(c.from_user.id,None);await c.message.answer("❌ Assignment cancelled");await c.answer()
async def worker():
    while True:
        # Registration completion + CF onboarding runs in background.
        for dom,d in list(store.data["domains"].items()):
            try:
                if d.get("status")=="registration_pending" and d.get("operation_id"):
                    r=await sp.operation(d["operation_id"])
                    if r.get("status")=="success":
                        z=await cf.zone_create(dom);d.update(status="pending",zone_id=z["id"],nameservers=z.get("name_servers",[]))
                        await sp.nameservers(dom,z.get("name_servers",[]));await store.save("NS updated")
                    elif r.get("status")=="failed":d["status"]="error";await store.save("Registration failed")
                elif d.get("status")=="pending" and d.get("zone_id"):
                    z=await cf.zone_get(d["zone_id"])
                    if z.get("status")=="active":d["status"]="active";await store.save("Domain active")
            except Exception:pass
        await asyncio.sleep(cfg.poll_seconds)
async def main():
    await store.load();asyncio.create_task(worker());await dp.start_polling(bot)
if __name__=="__main__":asyncio.run(main())
