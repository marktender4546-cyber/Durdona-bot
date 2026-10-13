"""
Kelin ko'ylaklar - Telegram bot + Mini App (view-only katalog)

Bitta jarayon ichida:
  - Telegram webhook (aiogram 3)
  - Mini App (webapp/index.html) va uning API'si (/api/catalog)
Render.com uchun mo'ljallangan (webhook rejimi).
"""
import asyncio
import html
import json
import logging
import os
import sqlite3
from pathlib import Path

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    KeyboardButton,
    MenuButtonWebApp,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    WebAppInfo,
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from texts import T

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("kelin-bot")

# --------------------------------------------------------------------------- #
# Sozlamalar (Render'da Environment Variables orqali beriladi)
# --------------------------------------------------------------------------- #
ROOT = Path(__file__).parent
BOT_TOKEN = os.environ["BOT_TOKEN"]
BASE_URL = (os.getenv("BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "kelin-secret-2026")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
PORT = int(os.getenv("PORT", "10000"))
DB_PATH = os.getenv("DB_PATH", str(ROOT / "users.db"))
PAGE_SIZE = 6

# --------------------------------------------------------------------------- #
# Katalog (data/catalog.json)
# --------------------------------------------------------------------------- #
CATALOG: dict = {}
PRODUCTS: dict[int, dict] = {}


def load_catalog() -> None:
    global CATALOG, PRODUCTS
    CATALOG = json.loads((ROOT / "data" / "catalog.json").read_text("utf-8"))
    PRODUCTS = {int(p["id"]): p for p in CATALOG["products"]}
    log.info("Katalog yuklandi: %d ta ko'ylak", len(PRODUCTS))


load_catalog()

# --------------------------------------------------------------------------- #
# Foydalanuvchilar bazasi (SQLite)
# --------------------------------------------------------------------------- #
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute(
    "CREATE TABLE IF NOT EXISTS users ("
    "id INTEGER PRIMARY KEY, lang TEXT, name TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP)"
)
db.commit()


def get_lang(uid: int):
    row = db.execute("SELECT lang FROM users WHERE id=?", (uid,)).fetchone()
    return row[0] if row else None


def set_lang(uid: int, lang: str, name: str = "") -> None:
    db.execute(
        "INSERT INTO users(id, lang, name) VALUES(?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET lang=excluded.lang, name=excluded.name",
        (uid, lang, name),
    )
    db.commit()


# --------------------------------------------------------------------------- #
# Yordamchi funksiyalar
# --------------------------------------------------------------------------- #
def t(lang: str, key: str, **kw) -> str:
    text = T.get(lang, T["uz"]).get(key) or T["uz"].get(key, key)
    return text.format(**kw) if kw else text


def tr(obj, lang: str) -> str:
    """{'uz': .., 'ru': ..} ko'rinishidagi maydondan kerakli tilni oladi."""
    if isinstance(obj, dict):
        return obj.get(lang) or obj.get("uz") or ""
    return obj or ""


def money(n: int, lang: str) -> str:
    return f"{int(n):,}".replace(",", " ") + " " + t(lang, "currency")


def abs_url(path: str) -> str:
    return path if path.startswith("http") else f"{BASE_URL}{path}"


def app_url(lang: str, pid: int | None = None) -> str:
    url = f"{BASE_URL}/app?lang={lang}"
    return url + (f"&p={pid}" if pid else "")


def B(text: str, data: str | None = None, url: str | None = None, app: str | None = None):
    if app:
        return InlineKeyboardButton(text=text, web_app=WebAppInfo(url=app))
    if url:
        return InlineKeyboardButton(text=text, url=url)
    return InlineKeyboardButton(text=text, callback_data=data)


def kb(rows: list) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def chunk(items: list, n: int = 2) -> list:
    return [items[i : i + n] for i in range(0, len(items), n)]


def filter_products(cid: str) -> list[dict]:
    items = list(PRODUCTS.values())
    if cid == "all":
        return items
    if cid in ("new", "sale"):
        return [p for p in items if p.get("tag") == cid]
    return [p for p in items if p.get("category") == cid]


def cat_title(cid: str, lang: str) -> str:
    if cid in ("all", "new", "sale"):
        return t(lang, cid)
    for c in CATALOG["categories"]:
        if c["id"] == cid:
            return tr(c["name"], lang)
    return t(lang, "all")


def caption(p: dict, lang: str) -> str:
    e = html.escape
    lines = [f"<b>{e(p['name'])}</b>"]
    price = f"💰 <b>{money(p['price'], lang)}</b>"
    if p.get("old_price"):
        price += f"  <s>{money(p['old_price'], lang)}</s>"
    lines.append(price)
    if p.get("sizes"):
        lines.append(f"📏 {t(lang, 'sizes')}: {e(p['sizes'])}")
    if p.get("colors"):
        lines.append(f"🎨 {t(lang, 'colors')}: {e(tr(p['colors'], lang))}")
    if p.get("fabric"):
        lines.append(f"🧵 {t(lang, 'fabric')}: {e(tr(p['fabric'], lang))}")
    if p.get("desc"):
        lines.append("\n" + e(tr(p["desc"], lang)))
    return "\n".join(lines)


def main_menu(lang: str) -> InlineKeyboardMarkup:
    return kb(
        [
            [B(t(lang, "btn_app"), app=app_url(lang))],
            [B(t(lang, "btn_cats"), "cats"), B(t(lang, "btn_contact"), "contact")],
            [B(t(lang, "btn_loc"), "loc"), B(t(lang, "btn_about"), "about")],
            [B(t(lang, "btn_lead"), "lead")],
            [B(t(lang, "btn_lang"), "lang")],
        ]
    )


def welcome_text(lang: str, name: str) -> str:
    return t(lang, "welcome", shop=html.escape(CATALOG["shop"]["name"]), name=html.escape(name))


async def show(cb: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    """Xabarni tahrirlaydi; agar rasmli xabar bo'lsa - o'chirib, yangisini yuboradi."""
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except Exception:
        try:
            await cb.message.delete()
        except Exception:
            pass
        await cb.message.answer(text, reply_markup=markup)
    await cb.answer()


# --------------------------------------------------------------------------- #
# Handlerlar
# --------------------------------------------------------------------------- #
router = Router()


def lang_kb(prefix: str = "setlang") -> InlineKeyboardMarkup:
    return kb([[B("🇺🇿 O'zbekcha", f"{prefix}:uz"), B("🇷🇺 Русский", f"{prefix}:ru")]])


@router.message(CommandStart())
async def cmd_start(m: Message):
    lang = get_lang(m.from_user.id)
    if not lang:
        await m.answer(T["uz"]["choose_lang_bi"], reply_markup=lang_kb())
        return
    await m.answer(welcome_text(lang, m.from_user.first_name), reply_markup=main_menu(lang))


@router.message(Command("menu"))
async def cmd_menu(m: Message):
    lang = get_lang(m.from_user.id) or "uz"
    await m.answer(welcome_text(lang, m.from_user.first_name), reply_markup=main_menu(lang))


@router.callback_query(F.data.startswith("setlang:"))
async def cb_setlang(cb: CallbackQuery):
    lang = cb.data.split(":")[1]
    set_lang(cb.from_user.id, lang, cb.from_user.full_name)
    await show(cb, welcome_text(lang, cb.from_user.first_name), main_menu(lang))


@router.callback_query(F.data == "lang")
async def cb_lang(cb: CallbackQuery):
    lang = get_lang(cb.from_user.id) or "uz"
    await show(cb, t(lang, "choose_lang"), lang_kb())


@router.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery):
    lang = get_lang(cb.from_user.id) or "uz"
    await show(cb, welcome_text(lang, cb.from_user.first_name), main_menu(lang))


@router.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery):
    await cb.answer()


# ---- Katalog ---------------------------------------------------------------
@router.callback_query(F.data == "cats")
async def cb_cats(cb: CallbackQuery):
    lang = get_lang(cb.from_user.id) or "uz"
    entries = [("all", f"👗 {t(lang, 'all')}")]
    if filter_products("new"):
        entries.append(("new", t(lang, "new")))
    if filter_products("sale"):
        entries.append(("sale", t(lang, "sale")))
    for c in CATALOG["categories"]:
        entries.append((c["id"], tr(c["name"], lang)))
    btns = [B(f"{label} ({len(filter_products(cid))})", f"cat:{cid}:0") for cid, label in entries]
    rows = chunk(btns, 2) + [[B(t(lang, "menu"), "menu")]]
    await show(cb, t(lang, "cats_title"), kb(rows))


@router.callback_query(F.data.startswith("cat:"))
async def cb_cat(cb: CallbackQuery):
    lang = get_lang(cb.from_user.id) or "uz"
    _, cid, page_s = cb.data.split(":")
    page = int(page_s)
    items = filter_products(cid)
    pages = max(1, -(-len(items) // PAGE_SIZE))
    part = items[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]

    rows = [[B(f"{p['name']} — {money(p['price'], lang)}", f"p:{p['id']}:{cid}")] for p in part]
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(B("‹", f"cat:{cid}:{page - 1}"))
        nav.append(B(f"{page + 1}/{pages}", "noop"))
        if page < pages - 1:
            nav.append(B("›", f"cat:{cid}:{page + 1}"))
        rows.append(nav)
    rows.append([B(t(lang, "back"), "cats")])

    text = f"<b>{html.escape(cat_title(cid, lang))}</b>\n"
    text += t(lang, "pick") if items else t(lang, "no_products")
    await show(cb, text, kb(rows))


@router.callback_query(F.data.startswith("p:"))
async def cb_product(cb: CallbackQuery):
    lang = get_lang(cb.from_user.id) or "uz"
    _, pid_s, cid = cb.data.split(":")
    p = PRODUCTS.get(int(pid_s))
    if not p:
        await cb.answer("—")
        return

    items = filter_products(cid) or [p]
    ids = [x["id"] for x in items]
    idx = ids.index(p["id"]) if p["id"] in ids else 0
    nav = []
    if idx > 0:
        nav.append(B("‹", f"p:{ids[idx - 1]}:{cid}"))
    nav.append(B(f"{idx + 1}/{len(ids)}", "noop"))
    if idx < len(ids) - 1:
        nav.append(B("›", f"p:{ids[idx + 1]}:{cid}"))

    rows = [nav, [B(t(lang, "btn_open_app"), app=app_url(lang, p["id"]))]]
    second = []
    if len(p.get("images", [])) > 1:
        second.append(B(t(lang, "btn_gallery"), f"gal:{p['id']}"))
    second.append(B(t(lang, "btn_ask"), "contact"))
    rows.append(second)
    rows.append([B(t(lang, "back"), f"cat:{cid}:{idx // PAGE_SIZE}")])
    markup, text = kb(rows), caption(p, lang)

    url = abs_url(p["images"][0]) if p.get("images") else None
    if url and cb.message.photo:  # rasmli xabarni silliq almashtirish
        try:
            await cb.message.edit_media(InputMediaPhoto(media=url, caption=text), reply_markup=markup)
            await cb.answer()
            return
        except Exception as e:
            log.warning("edit_media failed: %s", e)
    sent = False
    if url:
        try:
            await cb.message.answer_photo(url, caption=text, reply_markup=markup)
            sent = True
        except Exception as e:
            log.warning("answer_photo failed (%s): %s", url, e)
    if not sent:
        await cb.message.answer(text, reply_markup=markup)
    try:
        await cb.message.delete()
    except Exception:
        pass
    await cb.answer()


@router.callback_query(F.data.startswith("gal:"))
async def cb_gallery(cb: CallbackQuery):
    p = PRODUCTS.get(int(cb.data.split(":")[1]))
    if not p:
        await cb.answer()
        return
    try:
        media = [InputMediaPhoto(media=abs_url(u)) for u in p["images"][:10]]
        await cb.message.answer_media_group(media)
    except Exception as e:
        log.warning("gallery failed: %s", e)
    await cb.answer()


# ---- Aloqa, manzil, biz haqimizda ------------------------------------------
@router.callback_query(F.data == "contact")
async def cb_contact(cb: CallbackQuery):
    lang = get_lang(cb.from_user.id) or "uz"
    s, e = CATALOG["shop"], html.escape
    text = (
        f"<b>{e(s['name'])}</b>\n\n"
        f"📞 {e(s['phone'])}\n"
        f"💬 @{e(s['telegram'])}\n"
        f"📍 {e(tr(s['address'], lang))}\n"
        f"🕒 {e(tr(s['hours'], lang))}"
    )
    rows = [
        [B(t(lang, "btn_write"), url=f"https://t.me/{s['telegram']}")],
        [B("Instagram", url=s["instagram"])],
        [B(t(lang, "btn_lead"), "lead")],
        [B(t(lang, "menu"), "menu")],
    ]
    await show(cb, text, kb(rows))


@router.callback_query(F.data == "loc")
async def cb_location(cb: CallbackQuery):
    lang = get_lang(cb.from_user.id) or "uz"
    s = CATALOG["shop"]
    await cb.message.answer_location(s["lat"], s["lon"])
    await cb.message.answer(
        f"📍 {html.escape(tr(s['address'], lang))}\n🕒 {html.escape(tr(s['hours'], lang))}",
        reply_markup=kb([[B(t(lang, "menu"), "menu")]]),
    )
    await cb.answer()


@router.callback_query(F.data == "about")
async def cb_about(cb: CallbackQuery):
    lang = get_lang(cb.from_user.id) or "uz"
    s = CATALOG["shop"]
    text = f"<b>{html.escape(s['name'])}</b>\n\n{html.escape(tr(s['about'], lang))}"
    await show(cb, text, kb([[B(t(lang, "menu"), "menu")]]))


# ---- Maslahat so'rovi (telefon qoldirish) ----------------------------------
CANCEL_TEXTS = {T[l]["cancel"] for l in T}


@router.callback_query(F.data == "lead")
async def cb_lead(cb: CallbackQuery):
    lang = get_lang(cb.from_user.id) or "uz"
    markup = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t(lang, "share_phone"), request_contact=True)],
                  [KeyboardButton(text=t(lang, "cancel"))]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await cb.message.answer(t(lang, "lead_ask"), reply_markup=markup)
    await cb.answer()


@router.message(F.contact)
async def on_contact(m: Message):
    lang = get_lang(m.from_user.id) or "uz"
    u, c = m.from_user, m.contact
    note = (
        "🔔 <b>Yangi so'rov / Новая заявка</b>\n"
        f"👤 {html.escape(u.full_name)}"
        + (f" (@{u.username})" if u.username else "")
        + f"\n📱 {html.escape(c.phone_number)}\n🌐 {lang}\n"
        f'<a href="tg://user?id={u.id}">Profil</a>'
    )
    for admin in ADMIN_IDS:
        try:
            await m.bot.send_message(admin, note)
        except Exception as e:
            log.warning("admin %s ga yuborilmadi: %s", admin, e)
    await m.answer(t(lang, "lead_thanks"), reply_markup=ReplyKeyboardRemove())
    await m.answer(welcome_text(lang, u.first_name), reply_markup=main_menu(lang))


@router.message(F.text.in_(CANCEL_TEXTS))
async def on_cancel(m: Message):
    lang = get_lang(m.from_user.id) or "uz"
    await m.answer("👌", reply_markup=ReplyKeyboardRemove())
    await m.answer(welcome_text(lang, m.from_user.first_name), reply_markup=main_menu(lang))


# ---- Admin buyruqlari ------------------------------------------------------
def is_admin(m: Message) -> bool:
    return m.from_user.id in ADMIN_IDS


@router.message(Command("stats"), F.func(is_admin))
async def cmd_stats(m: Message):
    total = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    uz = db.execute("SELECT COUNT(*) FROM users WHERE lang='uz'").fetchone()[0]
    await m.answer(f"👥 Users: {total}\n🇺🇿 uz: {uz}\n🇷🇺 ru: {total - uz}\n👗 Products: {len(PRODUCTS)}")


@router.message(Command("reload"), F.func(is_admin))
async def cmd_reload(m: Message):
    load_catalog()
    await m.answer(f"✅ Katalog qayta yuklandi: {len(PRODUCTS)} ta ko'ylak")


@router.message(Command("broadcast"), F.func(is_admin))
async def cmd_broadcast(m: Message):
    text = (m.text or "").partition(" ")[2].strip()
    if not text:
        await m.answer("Foydalanish: /broadcast <xabar matni>")
        return
    ok = fail = 0
    for (uid,) in db.execute("SELECT id FROM users").fetchall():
        try:
            await m.bot.send_message(uid, text)
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)
    await m.answer(f"✅ Yuborildi: {ok}\n❌ Xato: {fail}")


@router.message()
async def fallback(m: Message):
    lang = get_lang(m.from_user.id)
    if not lang:
        await m.answer(T["uz"]["choose_lang_bi"], reply_markup=lang_kb())
        return
    await m.answer(t(lang, "unknown"), reply_markup=main_menu(lang))


# --------------------------------------------------------------------------- #
# Web server: webhook + Mini App + API
# --------------------------------------------------------------------------- #
async def on_startup(bot: Bot):
    if not BASE_URL:
        log.error("BASE_URL / RENDER_EXTERNAL_URL topilmadi - webhook o'rnatilmadi!")
        return
    await bot.set_webhook(
        f"{BASE_URL}/webhook",
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=True,
        allowed_updates=["message", "callback_query"],
    )
    await bot.set_my_commands(
        [BotCommand(command="start", description="Boshlash / Начать"),
         BotCommand(command="menu", description="Menyu / Меню")]
    )
    await bot.set_chat_menu_button(
        menu_button=MenuButtonWebApp(text="Katalog", web_app=WebAppInfo(url=f"{BASE_URL}/app"))
    )
    log.info("Webhook o'rnatildi: %s/webhook", BASE_URL)


async def h_health(_):
    return web.json_response({"ok": True})


async def h_root(_):
    raise web.HTTPFound("/app")


async def h_app(_):
    return web.FileResponse(ROOT / "webapp" / "index.html", headers={"Cache-Control": "no-cache"})


async def h_catalog(_):
    return web.json_response(CATALOG, headers={"Cache-Control": "no-cache"})


def main():
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    dp.startup.register(on_startup)

    app = web.Application()
    app.router.add_get("/", h_root)
    app.router.add_get("/health", h_health)
    app.router.add_get("/app", h_app)
    app.router.add_get("/api/catalog", h_catalog)
    app.router.add_static("/static", ROOT / "webapp" / "static", follow_symlinks=False)
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=WEBHOOK_SECRET).register(app, path="/webhook")
    setup_application(app, dp, bot=bot)

    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
