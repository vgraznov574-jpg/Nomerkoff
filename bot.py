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
UP_CHANCE, UP_COST = [0.30, 0.22, 0.10, 0.05, 0.02], 0.25   # шанс апгрейда по редкости, цена = 25% стоимости
TASKS = [("Сделать 10 круток", "spins", 10, {"free": 2}),
         ("Продать 5 номеров", "sells", 5, {"coins": 10000}),
         ("Выбить синий номер или лучше", "rares", 1, {"coins": 5000, "free": 1})]
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
        for col in ["streak INTEGER DEFAULT 0", "tday INTEGER DEFAULT 0", "tspins INTEGER DEFAULT 0",
                    "tsells INTEGER DEFAULT 0", "trares INTEGER DEFAULT 0", "tclaim INTEGER DEFAULT 0",
                    "lvl_paid INTEGER DEFAULT 1"]:
            try: await db.execute("ALTER TABLE p_users ADD COLUMN " + col)
            except Exception: pass
        await db.commit()


async def lvl_check(db, uid):
    xp, paid = await one(db, "SELECT xp,lvl_paid FROM p_users WHERE id=?", (uid,))
    lv = 1 + int((xp / 10) ** 0.5)
    if lv <= paid: return None
    rw = sum(l * 5000 for l in range(paid + 1, lv + 1))
    await db.execute("UPDATE p_users SET coins=coins+?,lvl_paid=? WHERE id=?", (rw, lv, uid))
    return {"level": lv, "reward": rw}


def calc_value(rar, vm, reg):
    return int(BASE[rar] * vm * random.uniform(0.85, 1.15) * (1.6 if reg in BEAUTY else 1))


async def get_state(db, uid, name=""):
    await db.execute("INSERT OR IGNORE INTO p_users(id,name,coins,free) VALUES(?,?,?,?)", (uid, name, START_COINS, START_FREE))
    if name: await db.execute("UPDATE p_users SET name=? WHERE id=?", (name, uid))
    await db.commit()
    coins, free, xp, ld, lw, streak, tday, ts, tl, tr, tc = await one(
        db, "SELECT coins,free,xp,last_daily,last_wheel,streak,tday,tspins,tsells,trares,tclaim FROM p_users WHERE id=?", (uid,))
    now = int(time.time())
    if tday != now // 86400:
        await db.execute("UPDATE p_users SET tday=?,tspins=0,tsells=0,trares=0,tclaim=0 WHERE id=?", (now // 86400, uid))
        await db.commit(); ts = tl = tr = tc = 0
    nxt = streak + 1 if ld and now - ld <= 2 * DAILY_CD else 1
    have = {"spins": ts, "sells": tl, "rares": tr}
    tasks = [{"id": i, "name": n, "have": min(have[k], need), "need": need, "rw": rw, "claimed": bool(tc >> i & 1)}
             for i, (n, k, need, rw) in enumerate(TASKS)]
    pl = await rows(db, "SELECT id,plate,region,rar,value FROM p_plates WHERE user_id=? ORDER BY value DESC LIMIT 200", (uid,))
    cnt, tot = await one(db, "SELECT COUNT(*),COALESCE(SUM(value),0) FROM p_plates WHERE user_id=?", (uid,))
    lvl = 1 + int((xp / 10) ** 0.5)
    return {"coins": coins, "free": free, "level": lvl, "xp": xp, "xp_lo": 10 * (lvl - 1) ** 2, "xp_hi": 10 * lvl ** 2,
            "daily_in": max(0, ld + DAILY_CD - now), "wheel_in": max(0, lw + WHEEL_CD - now),
            "plates": [{"id": i, "plate": p, "region": r, "rar": q, "value": v} for i, p, r, q, v in pl],
            "count": cnt, "worth": tot, "fee": FEE, "tasks": tasks,
            "daily_streak": nxt, "daily_reward": DAILY_COINS + 5000 * (min(nxt, 7) - 1),
            "cfg": {"stakes": {k: v[0] for k, v in STAKES.items()}, "regions": REGIONS, "beauty": BEAUTY, "up": UP_CHANCE, "up_cost": UP_COST}}


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
            val = calc_value(rar, vm, reg)
            cur = await db.execute("INSERT INTO p_plates(user_id,plate,region,rar,value) VALUES(?,?,?,?,?)", (uid, plate, reg, rar, val))
            await db.execute("UPDATE p_users SET xp=xp+?,tspins=tspins+1,trares=trares+? WHERE id=?",
                             (1 + rar * 3, 1 if rar >= 2 else 0, uid))
            lu = await lvl_check(db, uid)
            await db.commit()
            s = await get_state(db, uid, name)
            if lu: s["levelup"] = lu
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
            await db.execute("UPDATE p_users SET coins=coins+?,tsells=tsells+? WHERE id=?", (total, len(rs), uid))
            await db.commit()
            s = await get_state(db, uid, name); s["sold"] = total
            return s
        if action == "daily":
            if s["daily_in"] > 0: return {"error": "Бонус дня ещё не готов"}
            rw, day = s["daily_reward"], s["daily_streak"]
            await db.execute("UPDATE p_users SET coins=coins+?,free=free+?,last_daily=?,streak=? WHERE id=?",
                             (rw, DAILY_FREE, now, s["daily_streak"], uid))
            await db.commit()
            s = await get_state(db, uid, name); s["msg"] = f"День {day}: +{rw} ₽ и +{DAILY_FREE} беспл. крутки"
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
        if action == "upgrade":
            r = await one(db, "SELECT id,plate,region,rar,value FROM p_plates WHERE id=? AND user_id=?", (int(p.get("id") or 0), uid))
            if not r or r[3] >= 5: return {"error": "Этот номер нельзя улучшить"}
            pid, plate, reg, rar, val = r
            cost = int(val * UP_COST)
            if s["coins"] < cost: return {"error": "Не хватает монет"}
            await db.execute("UPDATE p_users SET coins=coins-? WHERE id=?", (cost, uid))
            await db.execute("DELETE FROM p_plates WHERE id=?", (pid,))
            up = {"ok": False, "cost": cost, "old": plate}
            if random.random() < UP_CHANCE[rar]:
                nr = rar + 1
                np_, nreg = make_plate(nr, reg)
                nv = int(val * BASE[nr] / BASE[rar] * random.uniform(0.85, 1.15))
                cur = await db.execute("INSERT INTO p_plates(user_id,plate,region,rar,value) VALUES(?,?,?,?,?)", (uid, np_, nreg, nr, nv))
                await db.execute("UPDATE p_users SET xp=xp+? WHERE id=?", (2 + nr * 2, uid))
                up = {"ok": True, "cost": cost, "plate": np_, "region": nreg, "rar": nr, "value": nv}
            lu = await lvl_check(db, uid)
            await db.commit()
            s = await get_state(db, uid, name); s["up"] = up
            if lu: s["levelup"] = lu
            return s
        if action == "task":
            i = int(p.get("id", -1))
            if not 0 <= i < len(TASKS): return {"error": "Нет такого задания"}
            t = s["tasks"][i]
            if t["claimed"] or t["have"] < t["need"]: return {"error": "Задание не выполнено"}
            rw = t["rw"]
            await db.execute("UPDATE p_users SET coins=coins+?,free=free+?,tclaim=tclaim|? WHERE id=?",
                             (rw.get("coins", 0), rw.get("free", 0), 1 << i, uid))
            await db.commit()
            s = await get_state(db, uid, name); s["msg"] = "Награда получена"
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
