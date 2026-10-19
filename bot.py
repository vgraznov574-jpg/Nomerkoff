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
SPIN_COST, COOLDOWN, START_COINS = 10, 3600, 100
LOCK = asyncio.Lock()
router = Router()


def value(n):
    s = f"{n:03d}"
    if s == "777": return 1000, "Джекпот"
    if len(set(s)) == 1: return 500, "Тройка"
    if s in ("123", "234", "345", "456", "567", "678", "789"): return 200, "Подряд"
    if s[0] == s[2]: return 100, "Зеркало"
    if len(set(s)) == 2: return 20, "Пара"
    return 5, "Обычный"


async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, coins INTEGER NOT NULL, last_free INTEGER NOT NULL DEFAULT 0)")
        await db.execute("CREATE TABLE IF NOT EXISTS numbers(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, num INTEGER NOT NULL)")
        await db.commit()


async def get_state(db, uid):
    await db.execute("INSERT OR IGNORE INTO users(id,coins) VALUES(?,?)", (uid, START_COINS))
    c = await db.execute("SELECT coins,last_free FROM users WHERE id=?", (uid,))
    coins, last = await c.fetchone()
    c = await db.execute("SELECT num,COUNT(*) FROM numbers WHERE user_id=? GROUP BY num", (uid,))
    inv, total = [], 0
    for num, cnt in await c.fetchall():
        v, k = value(num)
        inv.append({"num": f"{num:03d}", "count": cnt, "value": v, "kind": k})
        total += v * cnt
    inv.sort(key=lambda x: -x["value"])
    await db.commit()
    return {"coins": coins, "free_in": max(0, last + COOLDOWN - int(time.time())),
            "inventory": inv, "total": total}


async def do_state(uid):
    async with LOCK, aiosqlite.connect(DB) as db:
        return await get_state(db, uid)


async def do_spin(uid, free):
    async with LOCK, aiosqlite.connect(DB) as db:
        st = await get_state(db, uid)
        if free:
            if st["free_in"] > 0: return {"error": "Бесплатный спин ещё не готов"}
            await db.execute("UPDATE users SET last_free=? WHERE id=?", (int(time.time()), uid))
        else:
            if st["coins"] < SPIN_COST: return {"error": "Не хватает монет"}
            await db.execute("UPDATE users SET coins=coins-? WHERE id=?", (SPIN_COST, uid))
        n = random.randint(0, 999)
        await db.execute("INSERT INTO numbers(user_id,num) VALUES(?,?)", (uid, n))
        await db.commit()
        v, k = value(n)
        st = await get_state(db, uid)
        st["spin"] = {"num": f"{n:03d}", "value": v, "kind": k}
        return st


async def do_sell(uid):
    async with LOCK, aiosqlite.connect(DB) as db:
        st = await get_state(db, uid)
        if not st["inventory"]: return {"error": "Инвентарь пуст"}
        total = st["total"]
        await db.execute("DELETE FROM numbers WHERE user_id=?", (uid,))
        await db.execute("UPDATE users SET coins=coins+? WHERE id=?", (total, uid))
        await db.commit()
        st = await get_state(db, uid)
        st["sold"] = total
        return st


def check_init(init_data):
    try:
        d = dict(parse_qsl(init_data, keep_blank_values=True))
        h = d.pop("hash")
        s = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
        key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(key, s.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, h): return None
        return int(json.loads(d["user"])["id"])
    except Exception:
        return None


async def api(request):
    try: body = await request.json()
    except Exception: body = {}
    uid = check_init(body.get("initData", ""))
    if not uid: return web.json_response({"error": "Ошибка авторизации"}, status=401)
    a = request.match_info["action"]
    if a == "state": res = await do_state(uid)
    elif a == "spin": res = await do_spin(uid, False)
    elif a == "free": res = await do_spin(uid, True)
    elif a == "sell": res = await do_sell(uid)
    else: res = {"error": "unknown"}
    return web.json_response(res)


async def index(request):
    return web.FileResponse(os.path.join(HERE, "index.html"),
                            headers={"Cache-Control": "no-store"})


def kb():
    if not WEBAPP_URL: return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="🎮 Открыть игру", web_app=WebAppInfo(url=WEBAPP_URL))]])


def inv_text(st):
    if not st["inventory"]: return "Инвентарь пуст. Крутите: /spin"
    lines = [f"{i['num']} ×{i['count']} — {i['value']} 🪙 ({i['kind']})" for i in st["inventory"]]
    return "\n".join(lines) + f"\n\nВсего можно продать за {st['total']} 🪙"


@router.message(Command("start", "help"))
async def h_start(m: Message):
    await do_state(m.from_user.id)
    await m.answer("🎰 Номеркофф\nКрути номера 000–999 и продавай!\n\n"
                   f"/spin — крутить ({SPIN_COST} 🪙)\n/free — бесплатный спин раз в час\n"
                   "/inventory — номера\n/balance — баланс\n/sellall — продать всё",
                   reply_markup=kb())


@router.message(Command("balance"))
async def h_bal(m: Message):
    st = await do_state(m.from_user.id)
    await m.answer(f"💰 Баланс: {st['coins']} 🪙")


@router.message(Command("inventory"))
async def h_inv(m: Message):
    await m.answer(inv_text(await do_state(m.from_user.id)))


async def spin_reply(m, free):
    r = await do_spin(m.from_user.id, free)
    if "error" in r: return await m.answer("⚠️ " + r["error"])
    s = r["spin"]
    await m.answer(f"🎰 Выпало: {s['num']}\n{s['kind']} — {s['value']} 🪙\nБаланс: {r['coins']} 🪙")


@router.message(Command("spin"))
async def h_spin(m: Message): await spin_reply(m, False)


@router.message(Command("free"))
async def h_free(m: Message): await spin_reply(m, True)


@router.message(Command("sellall"))
async def h_sell(m: Message):
    r = await do_sell(m.from_user.id)
    if "error" in r: return await m.answer("⚠️ " + r["error"])
    await m.answer(f"✅ Продано на {r['sold']} 🪙\nБаланс: {r['coins']} 🪙")


async def main():
    if not TOKEN: raise SystemExit("Нет BOT_TOKEN")
    await init_db()
    bot = Bot(TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_post("/api/{action}", api)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    await bot.set_my_commands([
        BotCommand(command="start", description="Начать"),
        BotCommand(command="spin", description="Крутить номер"),
        BotCommand(command="free", description="Бесплатный спин"),
        BotCommand(command="inventory", description="Мои номера"),
        BotCommand(command="balance", description="Баланс"),
        BotCommand(command="sellall", description="Продать всё")])
    if WEBAPP_URL:
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(
            text="🎮 Играть", web_app=WebAppInfo(url=WEBAPP_URL)))
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
