import asyncio, os, random, time, json, hmac, hashlib
from urllib.parse import parse_qsl
import aiosqlite
from aiohttp import web
from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command
from aiogram.types import (Message, WebAppInfo, InlineKeyboardMarkup,
                           InlineKeyboardButton, MenuButtonWebApp, BotCommand)

TOKEN = os.getenv("BOT_TOKEN", "")
PORT = int(os.getenv("PORT", "8080"))
DB = os.getenv("DB_PATH", "bot.db")
WEBAPP_URL = os.getenv("WEBAPP_URL", "").rstrip("/")
if not WEBAPP_URL and os.getenv("RAILWAY_PUBLIC_DOMAIN"):
    WEBAPP_URL = "https://" + os.environ["RAILWAY_PUBLIC_DOMAIN"]
HERE = os.path.dirname(os.path.abspath(__file__))

# ---------- НАСТРОЙКИ ИГРЫ (можно менять) ----------
START_COINS, START_FREE, FEE = 100000, 10, 0.10
DAILY_COINS, DAILY_FREE, DAILY_CD, WHEEL_CD = 20000, 3, 86400, 3 * 3600
STAKES = {"n": (1000, 1, 1.0), "p": (5000, 3, 2.0), "e": (27777, 12, 2.7)}  # цена, x цены номеров, x шанс редких
BASE = [400, 1200, 4500, 40000, 600000, 10000000]  # цена: обычный..мифик
CHANCE = [0.13, 0.03, 0.004, 0.0002, 0.00002]      # необычный..мифик
BEAUTY = ["777", "77", "99", "97", "199", "177", "197", "799", "977", "78", "98", "178", "198",
          "11", "22", "33", "44", "55", "66"]
REGIONS = BEAUTY + ["01", "02", "03", "05", "10", "12", "13", "16", "21", "23", "24", "26", "29",
          "30", "34", "36", "38", "42", "47", "50", "52", "54", "59", "61", "63", "64", "72", "74",
          "76", "86", "93", "102", "116", "123", "124", "134", "152", "154", "161", "163", "174",
          "190", "196", "716", "750", "763"]
LET = "АВЕКМНОРСТУХ"
LOCK = asyncio.Lock()
router = Router()


def roll(boost):
    r, acc = random.random(), 0
    for i in range(5, 0, -1):
        acc += CHANCE[i - 1] * (boost if i >= 3 else 1)
        if r < acc: return i
    return 0


def make_plate(rar, region):
    L = lambda: random.choice(LET)
    if rar == 5:
        c = L(); return f"{c}777{c}{c}", "777"
    if rar == 4:
        c = L(); return f"{c}{random.choice(['666','777','001','007','999'])}{c}{c}", region
    if rar == 3:
        d = random.choice(["111", "222", "333", "444", "555", "888", "999", "002", "003", "005", "008"])
        return f"{L()}{d}{L()}{L()}", region
    if rar == 2:
        a, b = random.sample("123456789", 2)
        d = random.choice([a + a + b, a + b + b, a + b + a, a + "00"])
        return f"{L()}{d}{L()}{L()}", region
    if rar == 1:
        c = L(); return f"{L()}{random.randint(100, 999)}{c}{c}", region
    a, b = random.sample(LET, 2)
    return f"{L()}{random.randint(100, 999)}{a}{b}", region


async def one(db, sql, a=()):
    return await (await db.execute(sql, a)).fetchone()


async def rows(db, sql, a=()):
    return await (await db.execute(sql, a)).fetchall()


async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS p_users(id INTEGER PRIMARY KEY, name TEXT DEFAULT '', coins INTEGER, free INTEGER, xp INTEGER DEFAULT 0, last_daily INTEGER DEFAULT 0, last_wheel INTEGER DEFAULT 0)")
        await db.execute("CREATE TABLE IF NOT EXISTS p_plates(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, plate TEXT, region TEXT, rar INTEGER, value INTEGER)")
        await db.commit()


async def get_state(db, uid, name=""):
    await db.execute("INSERT OR IGNORE INTO p_users(id,name,coins,free) VALUES(?,?,?,?)", (uid, name, START_COINS, START_FREE))
    if name: await db.execute("UPDATE p_users SET name=? WHERE id=?", (name, uid))
    await db.commit()
    coins, free, xp, ld, lw = await one(db, "SELECT coins,free,xp,last_daily,last_wheel FROM p_users WHERE id=?", (uid,))
    now = int(time.time())
    pl = await rows(db, "SELECT id,plate,region,rar,value FROM p_plates WHERE user_id=? ORDER BY value DESC LIMIT 200", (uid,))
    cnt, tot = await one(db, "SELECT COUNT(*),COALESCE(SUM(value),0) FROM p_plates WHERE user_id=?", (uid,))
    lvl = 1 + int((xp / 10) ** 0.5)
    return {"coins": coins, "free": free, "level": lvl, "xp": xp, "xp_lo": 10 * (lvl - 1) ** 2, "xp_hi": 10 * lvl ** 2,
            "daily_in": max(0, ld + DAILY_CD - now), "wheel_in": max(0, lw + WHEEL_CD - now),
            "plates": [{"id": i, "plate": p, "region": r, "rar": q, "value": v} for i, p, r, q, v in pl],
            "count": cnt, "worth": tot, "fee": FEE,
            "cfg": {"stakes": {k: v[0] for k, v in STAKES.items()}, "regions": REGIONS, "beauty": BEAUTY}}


async def act(uid, name, action, p):
    async with LOCK, aiosqlite.connect(DB) as db:
        s = await get_state(db, uid, name)
        now = int(time.time())
        if action == "state":
            return s
        if action == "spin":
            k = p.get("stake", "n"); reg = p.get("region") or None
            if k not in STAKES or (reg and reg not in REGIONS): return {"error": "Неверные параметры"}
            cost, vm, boost = STAKES[k]
            if p.get("free") and k == "n" and not reg and s["free"] > 0:
                await db.execute("UPDATE p_users SET free=free-1 WHERE id=?", (uid,))
            else:
                price = int(cost * (1 if not reg else (1.4 if reg in BEAUTY else 1.2)))
                if s["coins"] < price: return {"error": "Не хватает монет"}
                await db.execute("UPDATE p_users SET coins=coins-? WHERE id=?", (price, uid))
            rar = roll(boost)
            plate, reg = make_plate(rar, reg or random.choice(REGIONS))
            val = int(BASE[rar] * vm * random.uniform(0.85, 1.15) * (1.6 if reg in BEAUTY else 1))
            cur = await db.execute("INSERT INTO p_plates(user_id,plate,region,rar,value) VALUES(?,?,?,?,?)", (uid, plate, reg, rar, val))
            await db.execute("UPDATE p_users SET xp=xp+? WHERE id=?", (1 + rar * 3, uid))
            await db.commit()
            s = await get_state(db, uid, name)
            s["spin"] = {"id": cur.lastrowid, "plate": plate, "region": reg, "rar": rar, "value": val, "sell": int(val * (1 - FEE))}
            return s
        if action == "sell":
            if p.get("id"):
                rs = await rows(db, "SELECT id,value FROM p_plates WHERE id=? AND user_id=?", (int(p["id"]), uid))
            else:
                rs = await rows(db, "SELECT id,value FROM p_plates WHERE user_id=? AND rar<=2", (uid,))
            if not rs: return {"error": "Нечего продавать"}
            total = int(sum(v for _, v in rs) * (1 - FEE))
            await db.executemany("DELETE FROM p_plates WHERE id=?", [(i,) for i, _ in rs])
            await db.execute("UPDATE p_users SET coins=coins+? WHERE id=?", (total, uid))
            await db.commit()
            s = await get_state(db, uid, name); s["sold"] = total
            return s
        if action == "daily":
            if s["daily_in"] > 0: return {"error": "Бонус дня ещё не готов"}
            await db.execute("UPDATE p_users SET coins=coins+?,free=free+?,last_daily=? WHERE id=?", (DAILY_COINS, DAILY_FREE, now, uid))
            await db.commit()
            s = await get_state(db, uid, name); s["msg"] = f"+{DAILY_COINS} ₽ и +{DAILY_FREE} бесплатных круток"
            return s
        if action == "wheel":
            if s["wheel_in"] > 0: return {"error": "Рулетка ещё не готова"}
            r = random.random()
            if r < 0.02: c, f = 100000, 0
            elif r < 0.30: c, f = 0, 2
            elif r < 0.55: c, f = 0, 1
            else: c, f = random.randint(1000, 15000), 0
            await db.execute("UPDATE p_users SET coins=coins+?,free=free+?,last_wheel=? WHERE id=?", (c, f, now, uid))
            await db.commit()
            s = await get_state(db, uid, name)
            s["msg"] = f"Выигрыш: {c} ₽" if c else f"Выигрыш: {f} беспл. крутки"
            return s
        if action == "top":
            rs = await rows(db, "SELECT u.id,u.name,u.coins+COALESCE((SELECT SUM(value) FROM p_plates WHERE user_id=u.id),0) c FROM p_users u ORDER BY c DESC LIMIT 10")
            s["top"] = [{"name": n or "Игрок", "cap": c, "me": i == uid} for i, n, c in rs]
            return s
    return {"error": "unknown"}


def check_init(init_data):
    try:
        d = dict(parse_qsl(init_data, keep_blank_values=True))
        h = d.pop("hash")
        sc = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
        key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(hmac.new(key, sc.encode(), hashlib.sha256).hexdigest(), h): return None
        u = json.loads(d["user"])
        return int(u["id"]), (u.get("first_name") or "")[:30]
    except Exception:
        return None


async def api(request):
    try: body = await request.json()
    except Exception: body = {}
    who = check_init(body.get("initData", ""))
    if not who: return web.json_response({"error": "Ошибка авторизации"}, status=401)
    p = body.get("p") or {}
    return web.json_response(await act(who[0], who[1], request.match_info["action"], p if isinstance(p, dict) else {}))


async def index(request):
    return web.FileResponse(os.path.join(HERE, "index.html"), headers={"Cache-Control": "no-store"})


def kb():
    if not WEBAPP_URL: return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎮 Играть", web_app=WebAppInfo(url=WEBAPP_URL))]])


@router.message(Command("start", "help"))
async def h_start(m: Message):
    await m.answer("🚘 Крути автономера, собирай редкие и продавай!\nЖми «Играть».", reply_markup=kb())


async def main():
    if not TOKEN: raise SystemExit("Нет BOT_TOKEN")
    await init_db()
    bot = Bot(TOKEN); dp = Dispatcher(); dp.include_router(router)
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_post("/api/{action}", api)
    runner = web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    await bot.set_my_commands([BotCommand(command="start", description="Играть")])
    if WEBAPP_URL:
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="🎮 Играть", web_app=WebAppInfo(url=WEBAPP_URL)))
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
