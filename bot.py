import asyncio
import os
import sqlite3
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, PreCheckoutQuery, CallbackQuery
from aiogram.filters import CommandStart, Command
from aiogram.exceptions import TelegramForbiddenError
from aiogram.utils.deep_linking import decode_payload
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://shrimpgames.zabeyda.lol?v=5")
CHAT_URL = "https://t.me/shrimpgames_chat"
CHAT_ID = "@shrimpgames_chat"
ADMIN_ID = 7308147004
LOG_GROUP_ID = int(os.getenv('LOG_GROUP_ID', '0'))  # ID приватной лог-группы
DB_PATH = os.getenv("DB_PATH", "shrimp.db")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Аукцион — состояние хранится в БД
def _auction():
    from database import get_auction_state
    return get_auction_state()

def AUCTION_ACTIVE():
    return _auction()["active"]

def AUCTION_TITLE():
    return _auction()["title"]

def AUCTION_LINK():
    return _auction()["link"]

def AUCTION_DEADLINE():
    return _auction()["deadline"]

_bear_claimed = False

async def launch_bear_airdrop():
    global _bear_claimed
    _bear_claimed = False
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎁 Схватить Мишку!", callback_data="bear_airdrop")
    ]])
    text = "🪂 <b>АИРДРОП GIFT!</b>\n\n🐻 В чат упал подарок <b>Мишка</b>\nПервый кто нажмёт кнопку — получит его!"
    await bot.send_message(CHAT_ID, text, parse_mode="HTML", reply_markup=kb)




ITEM_NAMES = {
    "shield": "🛡️ Крышануться", "double_vote": "💎 Двустволка",
    "resurrect": "✨ Постанова", "killer": "💀 Киллер", "spy": "🔍 Стукач",
}

RULES_TEXT = (
    "🗡 <b>Правила Разборок на Районе</b>\n\n"
    "— Сначала идёт регистрация игроков\n"
    "— Затем начинаются подряд раунды голосования по 15 минут каждый\n"
    "— Проголосуй за того кого хочешь выбить\n"
    "— Кто набрал больше всего голосов — вылетает (минимум двое за раунд)\n"
    "— Последние 5 выживших забирают призы 🏆\n\n"
    "@shrimpgamesbot"
)


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


async def log_to_group(text: str):
    """Пишем подробный лог в приватную группу — отключено"""
    return


def is_new_user(user_id):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,))
    exists = c.fetchone()
    conn.close()
    return exists is None


def get_game_players_ids(game_id):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT user_id FROM players WHERE game_id=? AND is_alive=1", (game_id,))
    rows = c.fetchall()
    conn.close()
    return [r["user_id"] for r in rows]


def get_all_players_with_info(game_id):
    conn = db()
    c = conn.cursor()
    c.execute("""SELECT u.user_id, u.username, u.first_name
                 FROM players p JOIN users u ON p.user_id=u.user_id
                 WHERE p.game_id=?""", (game_id,))
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def start_game_in_db(game_id):
    conn = db()
    c = conn.cursor()
    voting_ends = (datetime.utcnow() + timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
    c.execute("UPDATE games SET status='active', current_day=1, started_at=CURRENT_TIMESTAMP, voting_ends=? WHERE id=?",
              (voting_ends, game_id))
    # +games_played для всех участников
    c.execute("UPDATE users SET games_played=games_played+1 WHERE user_id IN (SELECT user_id FROM players WHERE game_id=?)", (game_id,))
    conn.commit()
    conn.close()


def get_active_game():
    conn = db()
    c = conn.cursor()
    c.execute("SELECT * FROM games WHERE status IN ('waiting','active') ORDER BY id DESC LIMIT 1")
    g = c.fetchone()
    conn.close()
    return dict(g) if g else None


def add_item_db(user_id, item_type, game_id):
    conn = db()
    c = conn.cursor()
    c.execute("INSERT INTO items (user_id, item_type, game_id, status) VALUES (?,?,?,'active')", (user_id, item_type, game_id))
    conn.commit()
    conn.close()


def get_all_referrals():
    conn = db()
    c = conn.cursor()
    c.execute("""SELECT u.user_id, u.username, u.first_name,
                        inv.username as inviter_username, inv.first_name as inviter_name,
                        (SELECT COUNT(*) FROM users r WHERE r.ref_by=u.user_id) as ref_count
                 FROM users u LEFT JOIN users inv ON u.ref_by=inv.user_id
                 ORDER BY ref_count DESC, u.created_at DESC""")
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── ФИЛЬТР ССЫЛОК ────────────────────────────────────────────
_ALLOWED_LINKS = {
    "t.me/shrimpgames_chat",
    "t.me/shrimpgames_channel",
    "t.me/shrimpgamesbot",
}

import re as _re

def _has_forbidden_link(message: Message) -> bool:
    import re
    text = message.text or message.caption or ""
    entities = list(message.entities or []) + list(message.caption_entities or [])

    for ent in entities:
        etype = ent.type
        # Обычный @username — пропускаем
        if etype == "mention":
            val = text[ent.offset : ent.offset + ent.length].lstrip("@").lower()
            # Блокируем только если это бот (@xxxbot или @xxx_bot)
            if val.endswith("bot") or "_bot" in val:
                # Но разрешаем наш бот
                if val not in {"shrimpgamesbot"}:
                    return True
            continue
        # Ссылки из entities: url, text_link
        if etype in ("url", "text_link"):
            url = ent.url if etype == "text_link" else text[ent.offset : ent.offset + ent.length]
            url_clean = re.sub(r'^https?://', '', url).rstrip('/').lower()
            if not any(url_clean == a or url_clean.startswith(a + '/') for a in _ALLOWED_LINKS):
                return True

    # Дополнительно — ищем голые ссылки в тексте на случай если entity не поймал
    for m in re.finditer(r'(https?://|t\.me/)\S+', text, re.IGNORECASE):
        url_clean = re.sub(r'^https?://', '', m.group()).rstrip('/').lower()
        if not any(url_clean == a or url_clean.startswith(a + '/') for a in _ALLOWED_LINKS):
            return True

    return False

@dp.message(F.chat.username == "shrimpgames_chat")
async def link_filter(message: Message):
    if message.from_user and message.from_user.id == ADMIN_ID:
        return
    if _has_forbidden_link(message):
        try:
            await message.delete()
        except Exception:
            pass
    # Не останавливаем обработку других хендлеров

@dp.message(CommandStart())
async def start(message: Message):
    args = message.text.split(maxsplit=1)
    ref_by = None
    if len(args) > 1:
        try: ref_by = int(decode_payload(args[1]))
        except:
            try: ref_by = int(args[1])
            except: pass

    from database import is_user_banned, unmark_bot_blocked
    unmark_bot_blocked(message.from_user.id)
    if is_user_banned(message.from_user.id):
        await message.answer("🚫 Вы заблокированы в этом боте.")
        return

    new_user = is_new_user(message.from_user.id)
    from database import get_or_create_user
    get_or_create_user(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.first_name or "",
        ref_by if ref_by and ref_by != message.from_user.id else None
    )
    # Обновляем username и first_name если изменились
    from database import update_user_profile
    update_user_profile(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.first_name or ""
    )

    if new_user and message.from_user.id != ADMIN_ID:
        uname = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name
        if ref_by and ref_by != message.from_user.id:
            conn_r = db(); c_r = conn_r.cursor()
            c_r.execute("SELECT username, first_name FROM users WHERE user_id=?", (ref_by,))
            ref_row = c_r.fetchone(); conn_r.close()
            if ref_row and ref_row["username"]:
                ref_label = f"@{ref_row['username']}"
            elif ref_row and ref_row["first_name"]:
                ref_label = ref_row["first_name"]
            else:
                ref_label = f"ID {ref_by}"
            ref_info = f"\nРеферал от {ref_label}"
        else:
            ref_info = ""
        icon = "👥" if ref_info else "👤"
        notify = f"{icon} Новый юзер {uname}{ref_info}"
        try:
            sent = await bot.send_message(ADMIN_ID, notify, parse_mode="HTML")
            # Сохраняем message_id чтобы потом поставить ✅ когда зарегается в игру
            try:
                import sqlite3 as _sq
                _nc = _sq.connect(DB_PATH)
                _nc.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)",
                            (f"new_user_notify_{message.from_user.id}", str(sent.message_id)))
                _nc.commit(); _nc.close()
            except: pass
        except: pass
        await log_to_group(f"👤 <b>Новый игрок</b>\n{notify}")

    webapp_url = WEBAPP_URL
    if ref_by and ref_by != message.from_user.id:
        webapp_url = f"{WEBAPP_URL}?ref={ref_by}"

    # Статус текущей игры для онбординга
    try:
        _gc = db(); _gcc = _gc.cursor()
        _gcc.execute("SELECT status, number FROM games ORDER BY id DESC LIMIT 1")
        _grow = _gcc.fetchone(); _gc.close()
        _gstatus = _grow["status"] if _grow else None
        _gnum = _grow["number"] if _grow else None
    except: _gstatus = None; _gnum = None

    if _gstatus == "waiting":
        game_hint = (
            "\n\n📋 <b>Прямо сейчас открыта запись на игру!</b>\n"
            "Открой приложение → вкладка <b>Играть</b> → жми <b>Записаться</b>"
        )
    elif _gstatus == "active":
        game_hint = (
            "\n\n⚔️ <b>Прямо сейчас идёт игра!</b>\n"
            "Открой приложение → вкладка <b>Играть</b> → смотри кто выживет\n"
            "Следующая игра скоро — успей записаться"
        )
    else:
        game_hint = (
            "\n\n🔜 <b>Скоро стартует новая игра</b>\n"
            "Открой приложение → вкладка <b>Играть</b> → записывайся заранее"
        )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗡 Играть", web_app=WebAppInfo(url=webapp_url))],
        [
            InlineKeyboardButton(text="💬 Чат", url=CHAT_URL),
            InlineKeyboardButton(text="📢 Канал", url="https://t.me/shrimpgames_channel")
        ]
    ])

    await message.answer_photo(
        photo="https://shrimpgames.zabeyda.lol/static/icons/start_pic.png",
        caption=(
            "🗡 <b>Разборки на районе</b>\n\n"
            "Убирай конкурентов, строй союзы, используй связи.\n"
            "Кого завалят следующим — не тебя ли?\n\n"
            "Последние выжившие забирают призы"
            + game_hint
        ),
        parse_mode="HTML",
        reply_markup=kb
    )


@dp.callback_query(F.data.startswith("set_gender:"))
async def set_gender_callback(callback: CallbackQuery):
    gender = callback.data.split(":")[1]  # male или female
    from database import set_gender, get_gender, get_or_create_user
    # Сохраняем только если ещё не задан (защита от повторного нажатия)
    if get_gender(callback.from_user.id) is not None:
        await callback.answer("Пол уже выбран!")
        return
    set_gender(callback.from_user.id, gender)
    label = "Парень 👦" if gender == "male" else "Девушка 👧"
    await callback.message.edit_text(f"✅ Принято — {label}!")
    # Теперь показываем стандартное приветствие с кнопкой открыть
    webapp_url = WEBAPP_URL
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Открыть", web_app=WebAppInfo(url=webapp_url))],
        [
            InlineKeyboardButton(text="💬 Чат", url=CHAT_URL),
            InlineKeyboardButton(text="📢 Канал", url="https://t.me/shrimpgames_channel")
        ]
    ])
    await callback.message.answer_photo(
        photo="https://shrimpgames.zabeyda.lol/static/icons/start_pic.png",
        caption=(
            "🗡 <b>Разборки на районе</b>\\n\\n"
            "Убирай конкурентов, строй союзы, используй связи.\\n"
            "Кого завалят следующим — не тебя ли?\\n\\n"
            "Последние выжившие забирают призы"
        ),
        parse_mode="HTML",
        reply_markup=kb
    )
    await callback.answer()


# ── Admin: запустить игру вручную ──

@dp.message(Command("test"))
async def cmd_test(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    import random
    FAKE_PLAYERS = [
        {"user_id": 9000001, "username": "test_alice",   "first_name": "Alice"},
        {"user_id": 9000002, "username": "test_bob",     "first_name": "Bob"},
        {"user_id": 9000003, "username": "test_charlie", "first_name": "Charlie"},
        {"user_id": 9000004, "username": "test_diana",   "first_name": "Diana"},
    ]
    FAKE_IDS_LIST = [p["user_id"] for p in FAKE_PLAYERS]
    ph = ",".join("?" * len(FAKE_IDS_LIST))

    await message.answer("🧪 Сбрасываю и создаю тестовую игру...")

    conn = db()
    c = conn.cursor()

    # RESET
    c.execute("DELETE FROM votes")
    c.execute(f"DELETE FROM players WHERE user_id IN ({ph})", FAKE_IDS_LIST)
    c.execute(f"DELETE FROM items WHERE user_id IN ({ph})", FAKE_IDS_LIST)
    c.execute(f"DELETE FROM users WHERE user_id IN ({ph})", FAKE_IDS_LIST)
    c.execute("DELETE FROM games WHERE number=99")
    try: c.execute("DELETE FROM settings WHERE key NOT LIKE 'wheel_%'")
    except: pass
    conn.commit()

    # СОЗДАТЬ ИГРУ
    c.execute("SELECT MAX(number) as mx FROM games")
    row = c.fetchone()
    next_num = (row["mx"] or 0) + 1
    c.execute("INSERT INTO games (number, status, max_players, prize_desc) VALUES (?,?,?,?)",
              (next_num, "waiting", 0, "Тестовый приз"))
    game_id = c.lastrowid
    conn.commit()

    # ДОБАВИТЬ БОТОВ
    for p in FAKE_PLAYERS:
        c.execute("INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?,?,?)",
                  (p["user_id"], p["username"], p["first_name"]))
    conn.commit()
    for p in FAKE_PLAYERS:
        c.execute("INSERT OR IGNORE INTO players (game_id, user_id) VALUES (?,?)", (game_id, p["user_id"]))
    c.execute("INSERT OR IGNORE INTO players (game_id, user_id) VALUES (?,?)", (game_id, ADMIN_ID))
    conn.commit()

    # СТАРТ
    voting_ends = (datetime.utcnow() + timedelta(seconds=60)).strftime("%Y-%m-%d %H:%M:%S")
    c.execute("UPDATE games SET status='active', current_day=1, started_at=CURRENT_TIMESTAMP, voting_ends=? WHERE id=?",
              (voting_ends, game_id))
    c.execute("UPDATE users SET games_played=games_played+1 WHERE user_id IN (SELECT user_id FROM players WHERE game_id=?)",
              (game_id,))
    conn.commit()
    conn.close()

    await message.answer(f"✅ Стрелка #{next_num} запущена! 4 бота + ты.\nОткрой WebApp и голосуй 👇",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🗳 Открыть игру", web_app=WebAppInfo(url=WEBAPP_URL))
        ]]))

    # авто-цикл убран — используй: python3 test_game.py run

@dp.message(Command("startgame"))
async def cmd_startgame(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    game = get_active_game()
    if not game:
        await message.answer("Нет активной игры")
        return
    if game["status"] != "waiting":
        await message.answer("Игра уже запущена")
        return

    start_game_in_db(game["id"])
    players = get_all_players_with_info(game["id"])
    num = game.get("number", 1)

    # Уведомить каждого игрока (только с включёнными уведомлениями)
    vote_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗳 Голосовать", web_app=WebAppInfo(url=WEBAPP_URL))
    ]])
    for p in players:
        # Проверяем notifications_enabled
        conn_n = db()
        c_n = conn_n.cursor()
        c_n.execute("SELECT notifications_enabled FROM users WHERE user_id=?", (p["user_id"],))
        row_n = c_n.fetchone()
        conn_n.close()
        notif = row_n["notifications_enabled"] if row_n and row_n["notifications_enabled"] is not None else 1
        if notif == 0:
            continue
        try:
            await bot.send_message(
                p["user_id"],
                f"🗡 <b>Стрелка #{num} началась!</b>\n\n{RULES_TEXT}",
                parse_mode="HTML",
                reply_markup=vote_kb
            )
        except TelegramForbiddenError:
            from database import mark_bot_blocked
            mark_bot_blocked(p["user_id"])
        except: pass

    # Уведомить чат
    names = ", ".join([
        f"@{p['username']}" if p['username'] else p['first_name']
        for p in players
    ])
    try:
        await bot.send_message(
            CHAT_ID,
            f"🗡 <b>Стрелка #{num} началась!</b>\n\nУчастники: {names}\n\nГолосование каждые 15 минут. Кто выживет?",
            parse_mode="HTML"
        )
    except: pass

    await message.answer(f"✅ Стрелка #{num} запущена! Уведомлено {len(players)} игроков.")


# ── Stars payments ──
@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    payload = query.invoice_payload
    # Блокируем повторную вербовку до списания Stars
    if len(payload.split(":")) == 3 and payload.split(":")[1] == "recruit":
        try:
            parts = payload.split(":")
            buyer_id = int(parts[0])
            game = get_active_game()
            if game and game["status"] == "active":
                day = game["current_day"] or 1
                recruit_key = f"recruited_{game['id']}_{day}_{buyer_id}"
                conn = db(); c = conn.cursor()
                c.execute("SELECT value FROM settings WHERE key=?", (recruit_key,))
                already = c.fetchone(); conn.close()
                if already:
                    await query.answer(ok=False, error_message="Ты уже вербовал в этом раунде — нельзя дважды.")
                    return
        except: pass
    await query.answer(ok=True)


@dp.message(F.successful_payment)
async def successful_payment(message: Message):
    payload = message.successful_payment.invoice_payload
    stars = message.successful_payment.total_amount

    # Дисс — diss:{key}
    if payload.startswith("diss:"):
        try:
            diss_key = payload.split(":", 1)[1]
            phrase = _diss_pending.pop(diss_key, "💀 Дисс отправлен!")
            await bot.send_message(CHAT_ID, phrase)
        except Exception as e:
            await bot.send_message(CHAT_ID, "💀 Дисс отправлен!")
        return

    # Аукцион — auction:{uid}:{amount}
    if payload.startswith("auction:"):
        try:
            parts = payload.split(":")
            uid = int(parts[1])
            amount = int(parts[2])
            uname = message.from_user.username
            fname = message.from_user.first_name
            from database import add_auction_donation, get_auction_top
            add_auction_donation(uid, uname, fname, amount)
            rows = get_auction_top()
            medals = ["🥇", "🥈", "🥉"]
            lines = [f"💸 <b>{fname or uname}</b> задонатил {amount} ⭐\n\n📊 <b>Топ доноров:</b>\n"]
            for i, row in enumerate(rows):
                name = f"@{row['username']}" if row['username'] else (row['first_name'] or f"ID{row['user_id']}")
                lines.append(f"{medals[i]} {name} — {row['total']} ⭐")
            await send_bet_post(CHAT_ID)
            title = AUCTION_TITLE()
            name_str = f"@{uname}" if uname else (fname or f"ID{uid}")
            await bot.send_message(ADMIN_ID, f"💸 <b>Новая ставка в аукционе</b>\n👤 {name_str}\n⭐ {amount} звёзд\n🏆 {title}", parse_mode="HTML")
        except Exception as e:
            await bot.send_message(CHAT_ID, f"⭐ Новый донат в аукционе!")
        return

    # Гемы — пополнение баланса
    if payload.startswith("gems:"):
        try:
            parts = payload.split(":")
            uid = int(parts[1])
            amount = int(parts[2])
            from database import add_gems
            add_gems(uid, amount)
            # Отмечаем что купил гемы (для ачивки) + накапливаем сумму
            try:
                _c = db(); _cur = _c.cursor()
                try: _cur.execute("ALTER TABLE users ADD COLUMN gems_max_purchase INTEGER DEFAULT 0")
                except: pass
                _cur.execute("UPDATE users SET gems_purchased=COALESCE(gems_purchased,0)+1, gems_bought_total=COALESCE(gems_bought_total,0)+?, gems_max_purchase=MAX(COALESCE(gems_max_purchase,0),?) WHERE user_id=?", (amount, amount, uid))
                # Логируем покупку для статистики
                _cur.execute("CREATE TABLE IF NOT EXISTS gem_purchases (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, username TEXT, gems INTEGER, stars INTEGER, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)")
                _cur.execute("INSERT INTO gem_purchases (user_id, username, gems, stars) VALUES (?,?,?,?)", (uid, message.from_user.username or "", amount, amount))
                _c.commit(); _c.close()
            except: pass
            uname = message.from_user.username
            fname = message.from_user.first_name or ""
            name_str = f"@{uname}" if uname else fname
            await message.answer(f"💎 <b>+{amount} Гемов</b> зачислено на баланс!\n\nПиши /bank чтобы проверить баланс.", parse_mode="HTML")
            await bot.send_message(ADMIN_ID, f"💎 Новая покупка Гемов\n👤 {name_str} (ID: {uid})\n💰 {amount} Гемов за {amount} ⭐")
            # NFT DROP счётчик
            try:
                from database import add_nft_stars
                if add_nft_stars(amount):
                    drop_name = f"@{uname}" if uname else fname
                    await bot.send_message(
                        CHAT_ID,
                        f"🎁 <b>NFT DROP!</b>\n\n"
                        f"🔥 {drop_name} поймал NFT DROP\n"
                        f"🖼 NFT улетает в личку — ожидай!\n\n"
                        f"https://t.me/nft/PoolFloat-25212",
                        parse_mode="HTML"
                    )
                    await bot.send_message(ADMIN_ID, f"🎁 NFT DROP — отправь NFT игроку {drop_name} (ID: {uid})")
            except Exception:
                pass
        except Exception as e:
            await message.answer("✅ Оплата получена!")
        return

    # Казик — пополнение кредитов
    # Клан — {user_id}:clan_create:{game_id}
    if len(payload.split(":")) >= 3 and payload.split(":")[1] == "clan_create":
        try:
            parts = payload.split(":")
            user_id = int(parts[0])
            game_id = int(parts[2])
            import urllib.parse as _up
            clan_name = _up.unquote(parts[3]) if len(parts) > 3 else "Клан"
            clan_name = clan_name.strip()[:20] or "Клан"
        except: return
        try:
            conn = db(); c = conn.cursor()
            c.execute("CREATE TABLE IF NOT EXISTS clans (id INTEGER PRIMARY KEY AUTOINCREMENT, game_id INTEGER, leader_id INTEGER, name TEXT DEFAULT 'Клан', created_at DATETIME DEFAULT CURRENT_TIMESTAMP, UNIQUE(game_id, leader_id))")
            c.execute("CREATE TABLE IF NOT EXISTS clan_members (id INTEGER PRIMARY KEY AUTOINCREMENT, clan_id INTEGER, user_id INTEGER, joined_at DATETIME DEFAULT CURRENT_TIMESTAMP, UNIQUE(clan_id, user_id))")
            c.execute("CREATE TABLE IF NOT EXISTS clan_invites (id INTEGER PRIMARY KEY AUTOINCREMENT, clan_id INTEGER, from_id INTEGER, to_id INTEGER, status TEXT DEFAULT 'pending', created_at DATETIME DEFAULT CURRENT_TIMESTAMP, UNIQUE(clan_id, to_id))")
            c.execute("INSERT OR IGNORE INTO clans (game_id, leader_id, name) VALUES (?,?,?)", (game_id, user_id, clan_name))
            # Считаем создание клана для ачивки
            try: c.execute("ALTER TABLE users ADD COLUMN created_clan INTEGER DEFAULT 0")
            except: pass
            c.execute("UPDATE users SET created_clan=COALESCE(created_clan,0)+1 WHERE user_id=?", (user_id,))
            conn.commit(); conn.close()
        except: pass
        uname = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name
        await message.answer(f"⚔️ <b>Клан «{clan_name}» создан!</b>\n\nОткрой профиль любого игрока и предложи ему союз.", parse_mode="HTML")
        await log_to_group(f"⚔️ <b>Клан создан!</b>\n👤 {uname} (ID: {user_id})\n🏴 Название: «{clan_name}»")
        try:
            sp = message.successful_payment.total_amount
            await bot.send_message(ADMIN_ID, f"⚔️ <b>Создан клан</b>\n👤 {uname}\n🏴 «{clan_name}»\n💫 {sp} ⭐", parse_mode="HTML")
        except: pass
        import httpx as _hxcl, random as _rcl
        _clan_msgs = [
            f"⚔️ На районе появился новый клан «{clan_name}». Одиночки — задумайтесь.",
            f"🏰 Кто-то сколотил клан «{clan_name}». Район меняется.",
            f"🌑 В тени собрали «{clan_name}». Что они задумали — пока неизвестно.",
            f"🗡 Клан «{clan_name}» вышел на сцену. Ход сделан.",
            f"🔥 Кто-то объединяется. Клан «{clan_name}» уже в игре.",
            f"👁 На районе появилась группировка «{clan_name}». Следи за собой.",
            f"🤝 Союз создан. Клан «{clan_name}» начинает охоту.",
            f"💀 «{clan_name}» на районе. Кто-то явно не хочет играть в одиночку.",
            f"🔒 Двери закрыты. Клан «{clan_name}» работает по своим правилам.",
            f"⚡ Новая сила на районе — клан «{clan_name}». Берегитесь.",
            f"🏴 «{clan_name}» поднял флаг. Игра пошла по-другому.",
            f"🎯 Кто-то сделал ставку на командную игру. Клан «{clan_name}» создан.",
        ]
        try:
            async with _hxcl.AsyncClient() as _clcl:
                await _clcl.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={"chat_id": "@shrimpgames_chat", "text": _rcl.choice(_clan_msgs), "parse_mode": "HTML"})
        except: pass
        return

    # Вербовка — {user_id}:recruit:{target_id}
    if len(payload.split(":")) == 3 and payload.split(":")[1] == "recruit":
        try:
            parts = payload.split(":")
            buyer_id = int(parts[0])
            target_id = int(parts[2])
        except: return
        game = get_active_game()
        if not game or game["status"] != "active":
            await message.answer("❌ Игра уже не активна, голос не засчитан.")
            return
        day = game["current_day"] or 1
        try:
            conn = db(); c = conn.cursor()
            c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)",
                      (f"recruited_{game['id']}_{day}_{buyer_id}", "1"))
            # Находим голос покупателя в текущем раунде и добавляем +1 к weight
            c.execute("SELECT id, weight FROM votes WHERE game_id=? AND day_number=? AND voter_id=?",
                      (game["id"], day, buyer_id))
            existing = c.fetchone()
            if existing:
                c.execute("UPDATE votes SET weight=? WHERE id=?",
                          (existing["weight"] + 1, existing["id"]))
            else:
                # Покупатель не голосовал — записываем голос завербованного против target
                fake_voter_id = target_id + 8000000000
                c.execute("INSERT OR IGNORE INTO votes (game_id, day_number, voter_id, target_id, weight) VALUES (?,?,?,?,?)",
                          (game["id"], day, fake_voter_id, target_id, 1))
            conn.commit(); conn.close()
        except: pass
        uname = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name
        buyer_name = message.from_user.first_name or uname
        await message.answer("🤝 <b>Завербовал!</b>\n\nГолос пассивного игрока в этом раунде — твой. Итого у тебя 2 голоса.", parse_mode="HTML")
        try:
            sp = message.successful_payment.total_amount
            await bot.send_message(ADMIN_ID, f"🤝 <b>Вербовка</b>\n👤 {uname}\n💫 {sp} ⭐", parse_mode="HTML")
        except: pass
        # Сообщение завербованному
        import httpx as _hxr, random as _rr
        try:
            await bot.send_message(target_id,
                f"🤝 <b>Вас завербовал игрок {buyer_name}</b>\n\n"
                f"Вы отсутствовали более 5 раундов подряд — он купил ваш голос в этом раунде.\n"
                f"Проголосовать сможете со следующего раунда.", parse_mode="HTML")
        except: pass
        # Пост в чат
        _recruit_msgs = [
            f"🤝 {uname} завербовал союзника. Расклад меняется.",
            f"💰 {uname} купил чужой голос. Кто-то не знает что его используют.",
            f"🕶 {uname} нашёл нужного человека. Деньги решают всё.",
            f"🤑 {uname} не ждёт — действует. Чужой голос уже в кармане.",
            f"🗡 {uname} завербовал бездействующего. Пассив стал оружием.",
        ]
        try:
            async with _hxr.AsyncClient() as _clr:
                await _clr.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={"chat_id": "@shrimpgames_chat", "text": _rr.choice(_recruit_msgs), "parse_mode": "HTML"})
        except: pass
        return

    if payload.startswith("casino:"):
        try:
            _, uid_str, amount_str = payload.split(":")
            user_id = int(uid_str)
            amount = int(amount_str)
        except: return
        try:
            import httpx as _hxc
            async with _hxc.AsyncClient() as _cl:
                await _cl.post("http://localhost:8010/api/casino/add_credits",
                               json={"user_id": user_id, "amount": amount})
        except: pass
        uname = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name
        await message.answer(f"✅ Пополнено <b>{amount} игровых кредитов</b>!\n\nОткрой Казик в боте и крути слот 🎰",
                             parse_mode="HTML")
        await log_to_group(f"🎰 <b>Казик пополнение!</b>\n👤 {uname} (ID: {user_id})\n💫 {amount} Stars → {amount} кредитов")
        try:
            await bot.send_message(ADMIN_ID, f"🎰 <b>Казик — пополнение</b>\n👤 {uname}\n💫 {amount} ⭐", parse_mode="HTML")
        except: pass
        return
    try:
        parts = payload.split(":")
        user_id = int(parts[0])
        item_type = parts[1]
    except: return

    # Атака Чушпана
    if item_type == "bomzh_attack":
        uname = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name
        await message.answer("🔪 Оплата принята! Теперь введи ник того кого нужно опустить — напиши мне прямо сюда в формате: @никнейм или просто имя")
        await log_to_group(f"🔪 <b>Атака Чушпана оплачена!</b>\n👤 {uname} (ID:{user_id})")
        return

    # Покупка артефакта в магазине
    if item_type == "authority":
        try:
            authority_type = parts[2]
            price = int(parts[3])
        except: return
        from database import get_authority, set_authority, get_user_authority
        import random
        AUTHORITY_NAMES = {
            'mayor': 'Мэр', 'banker': 'Банкир', 'crime_boss': 'Вор в законе',
            'cop': 'Мент', 'escort': 'Эскортница', 'dealer': 'Барыч', 'dictator': 'Диктатор', 'krasotka': 'Красотка', 'milf': 'Милфа'
        }
        AUTHORITY_EMOJIS = {
            'mayor': '🏛', 'banker': '🏦', 'crime_boss': '👑',
            'cop': '👮', 'escort': '💋', 'dealer': '💊', 'dictator': '🎖', 'krasotka': '💋', 'milf': '🍷'
        }
        AUTHORITY_NAMES_INS = {
            'mayor': 'Мэром', 'banker': 'Банкиром', 'crime_boss': 'Вором в законе',
            'cop': 'Ментом', 'escort': 'Эскортницей', 'dealer': 'Барычем', 'dictator': 'Диктатором', 'krasotka': 'Красоткой', 'milf': 'Милфой'
        }
        AUTHORITY_NAMES_GEN = {
            'mayor': 'Мэра', 'banker': 'Банкира', 'crime_boss': 'Вора в законе',
            'cop': 'Мента', 'escort': 'Эскортницы', 'dealer': 'Барыча', 'dictator': 'Диктатора', 'krasotka': 'Красотки', 'milf': 'Милфы'
        }
        name = AUTHORITY_NAMES.get(authority_type, authority_type)
        name_ins = AUTHORITY_NAMES_INS.get(authority_type, name)
        name_gen = AUTHORITY_NAMES_GEN.get(authority_type, name)
        emoji = AUTHORITY_EMOJIS.get(authority_type, '👑')
        uname = message.from_user.username
        fname = message.from_user.first_name or ""
        name_str = f"@{uname}" if uname else fname
        old = get_authority(authority_type)
        set_authority(authority_type, user_id, uname or "", fname, price)
        old_nick = f"@{old['username']}" if old and old.get('username') else (old.get('first_name', '?') if old else '?')
        # Сообщение в чат
        BUY_MSGS = [
            f"{emoji} {name_str} стал {name_ins} района! 💸 {price} ⭐",
            f"{emoji} {name_str} занял кресло {name_gen}! 💸 {price} ⭐",
            f"{emoji} На районе новый {name} — {name_str}! 💸 {price} ⭐",
            f"{emoji} {name_str} купил статус {name_gen} за {price} ⭐ — авторитет на районе заработан!",
        ]
        OUTBID_MSGS = [
            f"{emoji} {name_str} скинул {old_nick} с кресла {name_gen}! 💸 {price} ⭐ — деньги решают",
            f"{emoji} {name_str} стал {name_ins}! {old_nick} подвинули за {price} ⭐ — на районе новый хозяин",
            f"{emoji} {name_str} занял место {name_gen} за {price} ⭐! {old_nick} скомпрометировали и выставили за дверь",
            f"{emoji} {name_str} — новый {name}! {old_nick} не удержал власть. Цена вопроса — {price} ⭐",
            f"{emoji} {name_str} купил кресло {name_gen} за {price} ⭐! {old_nick} купили с потрохами и выбросили",
            f"{emoji} {name_str} стал {name_ins} за {price} ⭐! {old_nick} сдал позиции — район не прощает слабых",
            f"{emoji} {name_str} — новый {name} района! {old_nick} вынесли с вещами за {price} ⭐ — власть не вечна",
            f"{emoji} {name_str} занял кресло {name_gen}! {old_nick} слили свои же. {price} ⭐ — такова политика",
        ]
        if old and old.get('user_id') and old['user_id'] != user_id:
            msg = random.choice(OUTBID_MSGS)
            # Уведомить предыдущего владельца
            try:
                old_uid = old['user_id']
                old_name = f"@{old['username']}" if old.get('username') else old.get('first_name', '')
                await bot.send_message(old_uid, f"😤 {old_name}, тебя скинули с должности <b>{name}</b>!\nТебя перекупили за {price} ⭐", parse_mode="HTML")
            except: pass
        else:
            msg = random.choice(BUY_MSGS)
        await bot.send_message(CHAT_ID, msg)
        # Установить тег в чате
        try:
            await bot.session.post(
                f"https://api.telegram.org/bot{bot._token}/setChatAdministratorCustomTitle",
                json={"chat_id": CHAT_ID, "user_id": user_id, "custom_title": name}
            )
        except: pass
        await message.answer(f"✅ Ты стал <b>{name_ins}</b> района!\n\nДолжность даёт тебе особые возможности.", parse_mode="HTML")
        try:
            await bot.send_message(ADMIN_ID, f"{emoji} <b>Авторитет куплен</b>\n👤 {name_str}\n🏛 {name}\n⭐ {price}", parse_mode="HTML")
        except: pass
        return

    if item_type.startswith("artifact:"):
        art_id = item_type.replace("artifact:", "")
        uname = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name
        art_names = {'phone':'📱 Смартфон','pistol':'🔫 Пистолет','car_key':'🔑 Ключи от тачки','credit_card':'💳 Кредитка','drugs':'💊 Таблетки'}
        art_name = art_names.get(art_id, 'Артефакт')
        from database import get_conn as _gc
        conn = _gc(); c = conn.cursor()
        # Добавляем только если ещё нет
        c.execute("SELECT id FROM bomzh_items WHERE user_id=? AND item_id=?", (user_id, art_id))
        if not c.fetchone():
            c.execute("INSERT INTO bomzh_items (user_id, username, item_id, item_name, permanent) VALUES (?,?,?,?,1)",
                      (user_id, message.from_user.username or "", art_id, art_name.split(' ', 1)[-1]))
            conn.commit()
        conn.close()
        await message.answer(f"✅ <b>{art_name}</b> добавлен в инвентарь!\n\nАртефакт работает во всех играх.", parse_mode="HTML")
        try:
            await bot.send_message(ADMIN_ID, f"🏺 <b>Артефакт куплен</b>\n👤 {uname}\n📦 {art_name}\n⭐ {message.successful_payment.total_amount}", parse_mode="HTML")
        except: pass
        return

    # Бомж донат
    if item_type.startswith("bomzh_"):
        donate_type = item_type.replace("bomzh_", "")
        stars = message.successful_payment.total_amount
        uname = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name
        from database import get_conn as _gc
        conn = _gc(); c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS bomzh_donations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, username TEXT, donate_type TEXT, stars INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("INSERT INTO bomzh_donations (user_id, username, donate_type, stars) VALUES (?,?,?,?)",
                  (user_id, uname, donate_type, stars))
        conn.commit(); conn.close()
        # Сообщение Чушпана в зависимости от уровня доната
        import random as _rnd
        if donate_type in ('coffee', 'beer'):
            chat_msgs = [
                'Спасибо, друг... ты добрый человек. Я тут всех знаю, всех вижу. Попрошу кое-кого — прикроют тебя в следующих битвах. Я обещаю. 🤝',
                'Эх, давно никто так не угощал... Я не забуду. Есть люди которые мне должны — скажу им чтоб за тобой присматривали в игре. 🤝',
                'Хороший ты человек, видно сразу. Попрошу своих — в следующей битве тебя не тронут. Слово Чушпана. 🤝'
            ]
        elif donate_type in ('cigs', 'bigmac'):
            chat_msgs = [
                'Эх братан... спасибо. Серьёзно. Знаю одного чела — он в компах шарит, мне задолжал. Попрошу его. Сломает базу данных и тебя здесь никто не найдёт. Я тебе обещаю. 💻',
                'Ты серьёзный человек, вижу. Есть у меня знакомый — хакер, старой закалки. Попрошу его чтоб твои следы в базе подчистил. Будешь невидимкой. 💻',
                'Знаю одного умника, он с компами на ты. Должен мне с прошлого года. Скажу ему — сделает так что тебя в базе не найдут. Договорились? 💻'
            ]
        elif donate_type == 'clothes':
            chat_msgs = [
                'Брат... это серьёзно. Я знаю плохих людей на районе. Они мне кое-что должны. Попрошу их — разберутся с твоими конкурентами. Молча и без лишних вопросов. 🔪',
                'Такое не забывается... У меня есть контакты, серьёзные ребята. Скажу им кто тебе мешает — разберутся тихо. Ни слова лишнего. 🔪',
                'Ты для меня теперь как родной. Знаю людей которые вопросы решают. Назови конкурентов — позвоню сегодня же. 🔪'
            ]
        elif donate_type == 'premium300':
            chat_msgs = [
                'Слушай... такого мне ещё никто не делал. Я поговорю с главным на районе. Лично. Попрошу чтобы тебя сделали Смотрящим района. Это не просто слова — я своё слово держу. 👑',
                'Брат... я не знаю как благодарить. Пойду к главному сегодня же. Скажу что есть достойный человек. Смотрящим района будешь — это серьёзно. 👑',
                'Такое уважение... Главный мне обязан, давняя история. Попрошу его лично — сделает тебя Смотрящим. Весь район будет знать твоё имя. 👑'
            ]
        else:
            chat_msgs = ['Спасибо, земляк... не забуду. 🤝']
        chat_text = _rnd.choice(chat_msgs)
        # Сохраняем сообщение в базу для WebApp
        try:
            conn2 = _gc(); c2 = conn2.cursor()
            c2.execute("CREATE TABLE IF NOT EXISTS bomzh_chat (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, text TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
            c2.execute("INSERT INTO bomzh_chat (user_id, text) VALUES (?,?)", (user_id, chat_text))
            conn2.commit(); conn2.close()
        except: pass
        await message.answer(f"🏚 Чушпан получил твою помощь! Спасибо, земляк 🙏\n💫 {stars} Stars\n\n💬 {chat_text}")
        await log_to_group(f"🏚 <b>Донат Чушпану!</b>\n👤 {uname} (ID:{user_id})\n📦 {donate_type} — {stars} ⭐")
        # Пуш админу лично
        try:
            await bot.send_message(7308147004, f"🏚 <b>Донат Чушпану!</b>\n👤 {uname}\n💫 {stars} ⭐ — {donate_type}", parse_mode="HTML")
        except: pass
        return

    game = get_active_game()
    game_id = game["id"] if game else None
    stars = message.successful_payment.total_amount
    uname = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name
    # Убеждаемся что юзер есть в БД (мог купить без /start)
    from database import get_or_create_user as _gocu
    _gocu(message.from_user.id, message.from_user.username or "", message.from_user.first_name or "", None)

    COMBO_ITEMS = ["killer", "resurrect", "shield", "hacker", "spy", "tiebreaker", "double_vote", "anon_msg", "anon_player", "black_mark"]

    if item_type == "combo":
        # Выдаём все связи
        for it in COMBO_ITEMS:
            add_item_db(user_id, it, game_id)
        try:
            conn = db(); c = conn.cursor()
            c.execute("CREATE TABLE IF NOT EXISTS purchases (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, item_type TEXT, stars INTEGER, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
            c.execute("INSERT INTO purchases (user_id, item_type, stars) VALUES (?,?,?)", (user_id, "combo", stars))
            conn.commit(); conn.close()
        except: pass
        await message.answer(
            f"✅ Оплата прошла!\n\n🎒 <b>Комбо-набор получен!</b>\nВсе 10 связей добавлены в твой инвентарь — удачи в игре!",
            parse_mode="HTML"
        )
        pay_log = f"⭐ <b>Покупка Комбо!</b>\n👤 {uname} (ID: {message.from_user.id})\n📦 Все 10 связей\n💫 {stars} Stars"
    else:
        add_item_db(user_id, item_type, game_id)
        try:
            conn = db(); c = conn.cursor()
            c.execute("CREATE TABLE IF NOT EXISTS purchases (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, item_type TEXT, stars INTEGER, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
            c.execute("INSERT INTO purchases (user_id, item_type, stars) VALUES (?,?,?)", (user_id, item_type, stars))
            conn.commit(); conn.close()
        except: pass
        item_name = ITEM_NAMES.get(item_type, item_type)

        # Постанова: если игрок сейчас мёртв в активной игре — воскрешаем сразу
        if item_type == "resurrect" and game and game["status"] == "active":
            try:
                conn_r = db(); c_r = conn_r.cursor()
                c_r.execute("SELECT is_alive FROM players WHERE game_id=? AND user_id=?", (game["id"], user_id))
                row_r = c_r.fetchone()
                if row_r and row_r["is_alive"] == 0:
                    # Воскрешаем — возвращаем в игру, жертва уже выбрала другая
                    c_r.execute("UPDATE players SET is_alive=1 WHERE game_id=? AND user_id=?", (game["id"], user_id))
                    # Помечаем предмет использованным
                    c_r.execute("UPDATE items SET status='used' WHERE user_id=? AND item_type='resurrect' AND status='active' LIMIT 1", (user_id,))
                    # Счётчик воскрешений
                    try: c_r.execute("ALTER TABLE users ADD COLUMN resurrected INTEGER DEFAULT 0")
                    except: pass
                    c_r.execute("UPDATE users SET resurrected=COALESCE(resurrected,0)+1 WHERE user_id=?", (user_id,))
                    conn_r.commit(); conn_r.close()
                    uname_r = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name
                    await message.answer(
                        "🎭 <b>Постанова сработала!</b>\n\nТы инсценировал свою смерть — и вернулся в игру!\n\nОткрой бота и продолжай бороться за победу 👊",
                        parse_mode="HTML"
                    )
                    # Событие в чат
                    import httpx as _hxres
                    try:
                        async with _hxres.AsyncClient(timeout=8) as _clres:
                            await _clres.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                                json={"chat_id": "@shrimpgames_chat",
                                      "text": f"🎭 {uname_r} замутил Постанову — инсценировал смерть и вернулся в игру!",
                                      "parse_mode": "HTML"})
                    except: pass
                    pay_log = f"⭐ <b>Покупка + активация Постановы!</b>\n👤 {uname} (ID: {message.from_user.id})\n💫 {stars} Stars"
                else:
                    conn_r.close()
                    await message.answer(f"✅ Оплата прошла!\n\n{item_name} добавлен в твой инвентарь.\nАвтоматически сработает если тебя выберут на выбывание.")
                    pay_log = f"⭐ <b>Покупка!</b>\n👤 {uname} (ID: {message.from_user.id})\n📦 {item_name}\n💫 {stars} Stars"
            except Exception as _re:
                await message.answer(f"✅ Оплата прошла!\n\n{item_name} добавлен в инвентарь.")
                pay_log = f"⭐ <b>Покупка!</b>\n👤 {uname} (ID: {message.from_user.id})\n📦 {item_name}\n💫 {stars} Stars"
        else:
            await message.answer(f"✅ Оплата прошла!\n\n{item_name} добавлен в твой инвентарь.\nИспользовать можно в игре.")
            pay_log = f"⭐ <b>Покупка!</b>\n👤 {uname} (ID: {message.from_user.id})\n📦 {item_name}\n💫 {stars} Stars"

    try:
        await bot.send_message(ADMIN_ID, pay_log, parse_mode="HTML")
    except: pass
    await log_to_group(pay_log)
    # Сообщение в чат для двустволки
    if item_type == "double_vote":
        import httpx as _hxdv, random as _rdv
        _dv_msgs = [
            f"🔫 {uname} зарядил Двустволку — кто-то получит двойной удар!",
            f"💥 {uname} взял Двустволку. Два выстрела в одну цель — кому-то не поздоровится.",
            f"🔫 Двустволка заряжена. {uname} готовит двойной удар.",
            f"💣 {uname} достал Двустволку. Два голоса по одному — кто цель?",
            f"🔫 {uname} вооружился. Двустволка в руках — кто попадёт под раздачу?",
        ]
        try:
            async with _hxdv.AsyncClient(timeout=8) as _cldv:
                await _cldv.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={"chat_id": CHAT_ID, "text": _rdv.choice(_dv_msgs), "parse_mode": "HTML"})
        except: pass


@dp.message(Command("friends"))
async def cmd_friends(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    users = get_all_referrals()
    if not users:
        await message.answer("Пока нет юзеров.")
        return
    lines = ["👥 <b>Юзеры и рефералы:</b>\n"]
    for u in users[:50]:
        uname = f"@{u['username']}" if u['username'] else (u['first_name'] or f"ID{u['user_id']}")
        inv = f"@{u['inviter_username']}" if u['inviter_username'] else (u['inviter_name'] or "—")
        lines.append(f"• {uname} ← {inv} | рефов: {u['ref_count']}")
    await message.answer("\n".join(lines), parse_mode="HTML")



@dp.message(F.chat.username == "shrimpgames_chat", ~F.text.startswith("/"))
async def count_chat_messages(message: Message):
    """Считаем сообщения участников в чате"""
    if not message.from_user or message.from_user.is_bot:
        return
    try:
        from database import get_conn
        conn = get_conn()
        c = conn.cursor()
        c.execute("UPDATE users SET message_count = message_count + 1 WHERE user_id = ?", (message.from_user.id,))
        conn.commit()
        conn.close()
    except Exception:
        pass





from quiz import QUIZ_QUESTIONS

RANK_NAMES = ['Шестёрка','Фраер','Мужик','Бродяга','Пехота','Торпеда','Хулиган','Блатной','Смотрящий','Положенец','Авторитет','Вор','Законник','Положенец в законе','Пахан']
RANK_NAMES_FEMALE = ['Шестёрка','Найк Про','Пацанка','Бэсти','Наташа Пехота','Торпеда','Хулиганка','Блатная','Смотрящая','Положенка','Авторитетная','Воровка','Законница','Положенка в законе','Графиня']

def calc_rank(wins, kills, votes_cast, items_used, games_played, gender=None):
    score = wins*50 + kills*20 + votes_cast*5 + items_used*15 + games_played*10
    lvl = 1
    while round(200 * (lvl ** 1.75)) <= score:
        lvl += 1
    lvl = min(lvl, 15)
    names = RANK_NAMES_FEMALE if gender == 'female' else RANK_NAMES
    return names[lvl - 1]




async def send_bet_post(chat_id):
    """Отправляет актуальный пост аукциона с топ-3 и кнопками"""
    from database import get_auction_top
    rows = get_auction_top()
    medals = ["🥇", "🥈", "🥉"]

    if rows:
        top_lines = "\n".join([
            f"{medals[i]} {('@'+row['username']) if row['username'] else (row['first_name'] or 'ID'+str(row['user_id']))} — {row['total']} ⭐"
            for i, row in enumerate(rows)
        ])
    else:
        top_lines = "Пока никто не задонатил"

    _deadline_str = AUCTION_DEADLINE()
    _deadline_line = f"\n⏰ <b>DEAD LINE: {_deadline_str}</b>" if _deadline_str else ""
    # Считаем сколько времени осталось до дедлайна
    _time_left_line = ""
    try:
        from datetime import datetime as _dtnow, timezone as _tz, timedelta as _tdelta
        _msk = _tz(_tdelta(hours=3))
        _now_msk = _dtnow.now(_msk)
        _deadline_dt = _dtnow(2026, 5, 27, 19, 0, 0, tzinfo=_msk)
        _left = _deadline_dt - _now_msk
        _total_sec = int(_left.total_seconds())
        if 0 < _total_sec < 86400 * 7:
            _h = _total_sec // 3600
            _m = (_total_sec % 3600) // 60
            if _h > 0 and _m > 0:
                _time_left_line = f"\n⏳ Осталось: <b>{_h}ч {_m}мин</b>"
            elif _h > 0:
                _time_left_line = f"\n⏳ Осталось: <b>{_h}ч</b>"
            else:
                _time_left_line = f"\n⏳ Осталось: <b>{_m}мин</b>"
    except: pass
    caption = (
        f"🎁 <b>Розыгрыш {AUCTION_TITLE()}</b>\n"
        + (f"🔗 {AUCTION_LINK()}\n" if AUCTION_LINK() else "") +
        f"{_deadline_line}"
        f"{_time_left_line}\n"
        f"\nЗабирает топ-1 донатер\n\n"
        f"📊 <b>Топ 3 донатера:</b>\n{top_lines}"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="10 ⭐", callback_data="auc:10"),
            InlineKeyboardButton(text="50 ⭐", callback_data="auc:50"),
            InlineKeyboardButton(text="100 ⭐", callback_data="auc:100"),
        ],
        [InlineKeyboardButton(text="✍️ Своя сумма", callback_data="auc:custom")],
    ])

    import os
    photo_path = "/root/shrimp/static/icons/auction1.jpg"
    if os.path.exists(photo_path):
        from aiogram.types import FSInputFile
        photo = FSInputFile(photo_path)
        await bot.send_photo(chat_id, photo=photo, caption=caption, parse_mode="HTML", reply_markup=kb)
    else:
        await bot.send_message(chat_id, caption, parse_mode="HTML", reply_markup=kb)


@dp.message(Command("bet"))
async def cmd_bet(message: Message):
    if not AUCTION_ACTIVE():
        await message.answer("⚠️ Сейчас аукциона нет")
        return
    await send_bet_post(message.chat.id)


@dp.message(Command("startauction"))
async def cmd_startauction(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    if AUCTION_ACTIVE():
        await message.answer("⚠️ Аукцион уже идёт. Сначала /endauction")
        return

    args = message.text.split(maxsplit=2)
    title = args[1] if len(args) > 1 else "NFT"
    link = args[2] if len(args) > 2 else ""

    from database import init_auction_table, clear_auction
    init_auction_table()
    clear_auction()

    from database import set_auction_state
    set_auction_state(True, title, link)

    await send_bet_post(CHAT_ID)
    await message.answer("✅ Аукцион запущен!")


async def auto_end_auction():
    """Автоматическое закрытие аукциона по дедлайну"""
    if not AUCTION_ACTIVE():
        return
    from database import set_auction_state, get_auction_top
    title_now = AUCTION_TITLE()
    set_auction_state(False)
    rows = get_auction_top()
    medals = ["🥇", "🥈", "🥉"]
    if not rows:
        await bot.send_message(CHAT_ID, f"🔨 Аукцион <b>{title_now}</b> завершён.\n\nНикто не задонатил.", parse_mode="HTML")
        return
    lines = [f"🏁 <b>Аукцион завершён!</b>\n\n🎁 {title_now}\n\n<b>Топ доноров:</b>\n"]
    for i, row in enumerate(rows[:3]):
        name = f"@{row['username']}" if row['username'] else (row['first_name'] or f"ID{row['user_id']}")
        lines.append(f"{medals[i]} {name} — {row['total']} ⭐")
    await bot.send_message(CHAT_ID, "\n".join(lines), parse_mode="HTML")


@dp.message(Command("endauction"))
async def cmd_endauction(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    if not AUCTION_ACTIVE():
        await message.answer("⚠️ Аукциона нет")
        return

    title_now = AUCTION_TITLE()
    from database import set_auction_state, get_auction_top
    set_auction_state(False)
    rows = get_auction_top()

    medals = ["🥇", "🥈", "🥉"]
    if not rows:
        await bot.send_message(CHAT_ID, f"🔨 Аукцион <b>{AUCTION_TITLE()}</b> завершён.\n\nНикто не задонатил.", parse_mode="HTML")
        return

    lines = [f"🏁 <b>Аукцион завершён!</b>\n\n🎁 {AUCTION_TITLE()}\n\n<b>Топ доноров:</b>\n"]
    for i, row in enumerate(rows):
        name = f"@{row['username']}" if row['username'] else (row['first_name'] or f"ID{row['user_id']}")
        lines.append(f"{medals[i]} {name} — {row['total']} ⭐")

    await bot.send_message(CHAT_ID, "\n".join(lines), parse_mode="HTML")
    await message.answer("✅ Аукцион закрыт")


@dp.message(Command("auctionpush"))
async def cmd_auctionpush(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    title = AUCTION_TITLE() or "тест"
    await bot.send_message(ADMIN_ID, f"💸 <b>Новая ставка в аукционе</b>\n👤 @testuser\n⭐ 100 звёзд\n🏆 {title}", parse_mode="HTML")
    await message.answer("✅ Тестовый пуш отправлен")


@dp.message(Command("auctiontop"))
async def cmd_auctiontop(message: Message):
    from database import get_auction_top
    rows = get_auction_top()
    if not rows:
        await message.answer("📊 Пока никто не задонатил")
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"📊 <b>Топ доноров — {AUCTION_TITLE()}:</b>\n"]
    for i, row in enumerate(rows):
        name = f"@{row['username']}" if row['username'] else (row['first_name'] or f"ID{row['user_id']}")
        lines.append(f"{medals[i]} {name} — {row['total']} ⭐")
    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.callback_query(F.data.startswith("auc:"))
async def auction_donate(call: CallbackQuery):
    if not AUCTION_ACTIVE():
        await call.answer("⚠️ Аукцион уже завершён", show_alert=True)
        return

    action = call.data.split(":")[1]
    uid = call.from_user.id
    uname = call.from_user.username
    fname = call.from_user.first_name

    if action == "custom":
        await call.answer("Напиши в чат: /bid <сумма>\nНапример: /bid 200", show_alert=True)
        return

    amount = int(action)
    prices = [{"label": f"Донат {AUCTION_TITLE()}", "amount": amount}]

    await call.answer("💸 Открываю инвойс...", show_alert=False)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"🔨 Сделать ставку {amount} ⭐", pay=True)
    ]])
    await bot.send_invoice(
        chat_id=call.message.chat.id,
        title=f"Аукцион — {AUCTION_TITLE()}",
        description=f"Твоя ставка {amount} ⭐ идёт в аукцион.",
        payload=f"auction:{uid}:{amount}",
        currency="XTR",
        prices=prices,
        reply_markup=kb,
    )


@dp.message(Command("bid"))
async def cmd_bid(message: Message):
    if not AUCTION_ACTIVE():
        await message.answer("⚠️ Аукциона нет")
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("Используй: /bid <сумма>\nНапример: /bid 200")
        return
    amount = int(args[1])
    if amount < 1:
        await message.answer("Минимум 1 ⭐")
        return

    uid = message.from_user.id
    prices = [{"label": f"Ставка — {AUCTION_TITLE()}", "amount": amount}]
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"🔨 Сделать ставку {amount} ⭐", pay=True)
    ]])
    await bot.send_invoice(
        chat_id=message.chat.id,
        title=f"Аукцион — {AUCTION_TITLE()}",
        description=f"Твоя ставка {amount} ⭐ идёт в аукцион.",
        payload=f"auction:{uid}:{amount}",
        currency="XTR",
        prices=prices,
        reply_markup=kb,
    )


_diss_pending = {}  # {key: phrase}
_diss_last = {}    # {user_id: last_phrase_index}

DISS_PHRASES = [
    "🩸 @{from} дал отмашку — @{target} вскрыли как консерву, внутри пусто, даже мозга нет",
    "💀 @{from} позвонил братве. @{target} нашли в трёх дворах сразу — ноги в одном, остальное ищут",
    "🪓 По приказу @{from} — @{target} разрубили на куски и скормили голубям. Голуби отказались",
    "🔫 @{from} нажал курок. @{target} получил в лоб — башка слетела и покатилась до соседнего района",
    "🧠 @{from} вскрыл голову @{target} — внутри нашли пустую банку Jaguar и один дохлый таракан",
    "🥩 По команде @{from} — @{target} пустили на шашлык. Мясо жёсткое, вонючее, выбросили",
    "💣 @{from} заминировал @{target} — взорвался от позора. Радиус поражения — пять подъездов",
    "🪚 @{from} взял пилу. @{target} теперь в 12 файлах на жёстком диске — папка называется мусор",
    "⚰️ @{from} заказал похороны @{target} — гроб не закрывается, воняет, всех попросили отойти",
    "🩻 После звонка @{from} — рентген @{target} показал вместо позвоночника — желе, вместо сердца — труха",
    "🔪 @{from} пырнул репутацию @{target} — вытекло что-то жёлтое и дурно пахнущее",
    "🫀 Медики @{from} вскрыли @{target} — сердце не нашли. Зато нашли долги, страхи и старый кроссовок",
    "🦷 @{from} выбил @{target} зубы. Зубы собрали в пакет, подписали биомусор, сдали на утилизацию",
    "🪤 @{from} поймал @{target} в капкан — визжал три часа, вырвался, но без достоинства",
    "🧟 @{from} поднял @{target} из мёртвых только чтобы убить ещё раз — второй раз было приятнее",
    "🗡 @{from} насадил @{target} на кол у третьего подъезда — для красоты и как предупреждение",
    "🫁 По распоряжению @{from} — лёгкие @{target} нашли на крыше, печень у метро, сам не объявился",
    "🩸 @{from} пустил @{target} на удобрение. Цветы не выросли — земля отказала",
    "💀 @{from} снял скальп с @{target} — под ним обнаружили пустоту и старый мем из 2014",
    "🔩 @{from} разобрал @{target} до болтиков — болтики выбросил, остальное сдал в металлолом за 40 рублей",
    "🪦 @{from} поставил крест над @{target} — написали был тут, никто не плакал, собака убежала",
    "🥀 @{from} похоронил репутацию @{target} — на могиле выросли только сорняки и один гриб",
    "🔥 @{from} поджёг @{target} — горел плохо, воняло сильно, тушить не стали",
    "🪖 @{from} объявил войну @{target} — тот сдался через 4 секунды, заплакал и попросил маму",
    "🎯 @{from} прицелился в башку @{target} — не промахнулся. Башка улетела в закат",
    "🛻 @{from} вывез @{target} за город в багажнике — выбросил в лесу, волки понюхали и ушли",
    "🧨 @{from} взорвал самооценку @{target} — громко, ярко, ничего не осталось",
    "⚡ @{from} пропустил ток через @{target} — задымился, завонял, выключился насовсем",
    "🪠 @{from} прочистил @{target} как засор в трубе — вышло много лишнего, стало не лучше",
    "🔨 @{from} расплющил @{target} кувалдой — был человек, стала лепёшка, убрали совком",
    "🧪 Лаборатория @{from} провела анализ @{target} — в составе: трусость 80%, ложь 15%, моча 5%",
    "🪳 @{from} нашёл @{target} под плинтусом — раздавил тапком, тапок выбросил",
    "🏹 @{from} выстрелил стрелой в @{target} — попал в то место где должна быть душа. Там было пусто",
    "🗑 @{from} выбросил @{target} с 9 этажа — летел долго, кричал громко, упал тихо",
    "🥊 @{from} бил @{target} по голове пока не отвалилась — оказалось декоративная, внутри поролон",
    "🌡 Вскрытие @{target} по заказу @{from}: мозг усох до размера изюма, совесть не обнаружена",
    "💉 @{from} сделал @{target} укол правды — тот задёргался, пустил пену и признался во всём",
    "🦴 @{from} обглодал репутацию @{target} — выплюнул. Собаки понюхали и отошли",
    "🫗 @{from} вылил @{target} в канализацию — трубы пожаловались, сантехник отказался лезть",
    "🪝 @{from} подвесил @{target} на крюк у входа — как предупреждение. Все поняли. Сработало",
    "🔱 @{from} вынес приговор: @{target} расчленить, упаковать, забыть. Исполнено",
    "🗡 По решению @{from} — @{target} насквозь проткнули шваброй. Уборщица недовольна",
    "🩸 @{from} выпустил кровь @{target} — кровь убежала сама, даже она не хотела с ним быть",
    "💀 @{from} закопал @{target} живым — через час выкопался, но лучше б не выкапывался",
    "🪓 @{from} отрубил @{target} голову — голова покатилась, попросила пощады, не помогло",
    "🔥 @{from} спалил дотла всё что было у @{target} — честь, репутацию, два зуба и старые найки",
    "🧲 @{from} притянул к @{target} всех врагов района — окружили, посмотрели, ушли брезгливо",
    "💣 @{from} взорвал @{target} изнутри — разлетелся на куски, собирали неделю, не всё нашли",
    "🪦 @{from} написал некролог @{target}: жил тихо, умер громко, не жалко",
    "⚰️ @{from} уложил @{target} в гроб — тот ещё дышит но это временно и никого не волнует",
]


@dp.message(Command("diss"))
async def cmd_diss(message: Message):
    import random
    from_user = message.from_user
    from_name = f"@{from_user.username}" if from_user.username else from_user.first_name

    target_username = None
    target_name = None

    # Способ 1: реплай на сообщение
    if message.reply_to_message:
        t = message.reply_to_message.from_user
        target_username = t.username
        target_name = f"@{t.username}" if t.username else t.first_name

    # Способ 2: /diss @username
    else:
        args = message.text.split()
        if len(args) >= 2:
            raw = args[1].lstrip("@")
            target_username = raw
            target_name = f"@{raw}"

    if not target_name:
        await message.answer("Реплайни на сообщение или напиши /diss @username")
        return

    if target_username and target_username.lower() == (from_user.username or "").lower():
        await message.answer("😂 Сам себя заказал? Уважаю, но нет")
        return

    last_idx = _diss_last.get(from_user.id, -1)
    available = [i for i in range(len(DISS_PHRASES)) if i != last_idx]
    idx = random.choice(available)
    _diss_last[from_user.id] = idx
    phrase = DISS_PHRASES[idx].format(**{"from": from_name.lstrip("@"), "target": target_name.lstrip("@")})

    # Для админа — бесплатно
    if from_user.id == ADMIN_ID:
        await bot.send_message(CHAT_ID, phrase)
        return

    import time
    diss_key = f"{from_user.id}_{int(time.time())}"
    _diss_pending[diss_key] = phrase

    await bot.send_invoice(
        chat_id=message.chat.id,
        title="💀 Опустить на районе",
        description=f"Опустить {target_name} в чате за 1 звезду",
        payload=f"diss:{diss_key}",
        currency="XTR",
        prices=[{"label": "Diss", "amount": 1}],
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="💀 Слить за 1 ⭐", pay=True)
        ]])
    )


@dp.message(Command("game"))
async def cmd_game(message: Message):
    import random
    q = random.choice(QUIZ_QUESTIONS)
    options = [q["a"]] + q["w"]
    random.shuffle(options)
    buttons = [[InlineKeyboardButton(text=opt, callback_data=f"quiz:{opt}:{q['a']}")] for opt in options]
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer(f"🌍 <b>{q['q']}</b>", reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data.startswith("quiz:"))
async def quiz_answer(call: CallbackQuery):
    from database import get_conn
    from aiogram.types import InaccessibleMessage
    parts = call.data.split(":", 2)
    chosen = parts[1]
    correct = parts[2]
    uid = call.from_user.id
    name = call.from_user.first_name or f"@{call.from_user.username}" or f"ID{uid}"

    if chosen == correct:
        conn = get_conn()
        conn.row_factory = __import__('sqlite3').Row
        c = conn.cursor()
        c.execute("UPDATE users SET quiz_correct = COALESCE(quiz_correct,0) + 1 WHERE user_id=?", (uid,))
        conn.commit()
        c.execute("SELECT COALESCE(quiz_correct,0) as quiz_correct FROM users WHERE user_id=?", (uid,))
        row = c.fetchone()
        conn.close()
        total = row['quiz_correct'] if row else 1
        await call.answer("✅ Верно!", show_alert=True)
        if not isinstance(call.message, InaccessibleMessage):
            try:
                await call.message.delete()
            except:
                pass
    else:
        await call.answer(f"❌ Неверно! Правильный: {correct}", show_alert=True)
        if not isinstance(call.message, InaccessibleMessage):
            try:
                await call.message.delete()
            except:
                pass


@dp.message(Command("me"))
async def cmd_me(message: Message):
    from database import get_conn
    uid = message.from_user.id
    conn = get_conn()
    conn.row_factory = __import__('sqlite3').Row
    c = conn.cursor()
    c.execute("""
        SELECT username, first_name, games_played, kills, wins,
               COALESCE(votes_cast,0) as votes_cast,
               COALESCE(votes_cast,0) as votes_cast,
               COALESCE(items_used,0) as items_used,
               COALESCE(items_won,0) as items_won,
               COALESCE(streak_days,0) as streak_days,
               COALESCE(quiz_correct,0) as quiz_correct,
               gender
        FROM users WHERE user_id=?
    """, (uid,))
    row = c.fetchone()

    # активные предметы
    c.execute("SELECT COUNT(*) as cnt FROM items WHERE user_id=? AND status='active'", (uid,))
    active_items_row = c.fetchone()
    # items_bought из purchases
    c.execute("SELECT COUNT(*) as cnt FROM purchases WHERE user_id=?", (uid,))
    bought_row = c.fetchone()
    # друзья (рефералы)
    c.execute("SELECT COUNT(*) as cnt FROM users WHERE ref_by=?", (uid,))
    ref_row = c.fetchone()
    # аирдропы юзера
    try:
        c.execute("CREATE TABLE IF NOT EXISTS airdrop_log (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, username TEXT, airdrop_type TEXT, amount INTEGER DEFAULT 0, item_type TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)")
        c.execute("SELECT COALESCE(SUM(amount),0) as gems FROM airdrop_log WHERE user_id=? AND airdrop_type='gems'", (uid,))
        airdrop_gems_row = c.fetchone()
        c.execute("SELECT COUNT(*) as cnt FROM airdrop_log WHERE user_id=? AND airdrop_type='item'", (uid,))
        airdrop_items_row = c.fetchone()
    except:
        airdrop_gems_row = None; airdrop_items_row = None
    conn.close()

    if not row:
        await message.answer(f"❓ Не найден. uid={uid}", parse_mode="HTML")
        return

    name = f"@{row['username']}" if row['username'] else (row['first_name'] or f"ID{uid}")
    games = row['games_played'] or 0
    kills = row['kills'] or 0
    wins = row['wins'] or 0
    rounds = row['votes_cast'] or 0
    items_used = row['items_used'] or 0
    items_total = active_items_row['cnt'] if active_items_row else 0
    friends = ref_row['cnt'] if ref_row else 0
    purchases = bought_row['cnt'] if bought_row else 0
    gender = row['gender']
    rank = calc_rank(wins, kills, rounds, items_used, games, gender)

    streak = row['streak_days'] or 0
    airdrop_gems = airdrop_gems_row["gems"] if airdrop_gems_row else 0
    airdrop_items = airdrop_items_row["cnt"] if airdrop_items_row else 0

    text = (
        f"👤 <b>{name}</b>\n"
        f"🏅 Звание — {rank}\n"
        f"🏙 Столицы — {games}\n"
        f"👥 Друзья — {friends}\n"
        f"⚔️ Убийства — {kills}\n"
        f"🗳 Раунды — {rounds}\n"
        f"🏆 Попал в топ 5 — {wins}\n"
        f"🔗 Связи — {items_total}\n"
        f"🛍 Покупки — {purchases}\n"
        f"🔥 Стрик — {streak} дней\n"
        f"🎮 Игра — {row['quiz_correct'] or 0}\n"
        f"🎁 Словил связей — {airdrop_items} шт"
    )
    if airdrop_gems > 0:
        text += f"\n💎 Словил гемов — {airdrop_gems}"
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("rules"))
async def cmd_rules(message: Message):
    await message.answer(RULES_TEXT, parse_mode="HTML")


@dp.message(Command("top"))
async def cmd_top(message: Message):
    """Топ-5 игроков по количеству голосов за все игры"""
    from database import get_conn
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT u.user_id, u.username, u.first_name, COUNT(v.rowid) as vote_count
        FROM votes v
        JOIN users u ON u.user_id = v.voter_id
        WHERE u.user_id != 7308147004
        GROUP BY v.voter_id
        ORDER BY vote_count DESC
        LIMIT 5
    """)
    rows = c.fetchall()
    conn.close()

    if not rows:
        await message.answer("🗳 Пока нет данных о голосованиях.", parse_mode="HTML")
        return

    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    lines = ["🗳 <b>Топ голосующих района:</b>\n"]
    for i, row in enumerate(rows):
        name = f"@{row['username']}" if row['username'] else (row['first_name'] or f"ID{row['user_id']}")
        cnt = row['vote_count']
        word = "голос" if cnt % 10 == 1 and cnt % 100 != 11 else "голоса" if cnt % 10 in [2,3,4] and cnt % 100 not in [12,13,14] else "голосов"
        lines.append(f"{medals[i]} {name} — {cnt} {word}")

    lines.append("")

    # Топ по сообщениям в чате
    c2_conn = get_conn()
    c2 = c2_conn.cursor()
    c2.execute("""
        SELECT user_id, username, first_name, message_count
        FROM users
        WHERE message_count > 0
        ORDER BY message_count DESC
        LIMIT 5
    """)
    msg_rows = c2.fetchall()
    c2_conn.close()

    if msg_rows:
        lines.append("💬 <b>Топ болтунов района:</b>\n")
        for i, row in enumerate(msg_rows):
            name = f"@{row['username']}" if row['username'] else (row['first_name'] or f"ID{row['user_id']}")
            cnt = row['message_count']
            word = "сообщение" if cnt % 10 == 1 and cnt % 100 != 11 else "сообщения" if cnt % 10 in [2,3,4] and cnt % 100 not in [12,13,14] else "сообщений"
            lines.append(f"{medals[i]} {name} — {cnt} {word}")
        lines.append("")

    lines.append(f"<i>Участвуй → @shrimpgamesbot</i>")
    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("ref"))
async def cmd_ref(message: Message):
    """Топ-5 рефоводов по количеству рефералов в текущей игре"""
    from database import get_conn, get_active_game
    game = get_active_game()
    if not game:
        await message.answer("🔗 Сейчас нет активной игры.", parse_mode="HTML")
        return
    game_id = game["id"]

    conn = get_conn()
    c = conn.cursor()
    # Считаем активных рефов за все игры (хотя бы раз проголосовали)
    c.execute("""
        SELECT u.user_id, u.username, u.first_name,
               COUNT(ref_u.user_id) as active_refs,
               (SELECT COUNT(*) FROM users r WHERE r.ref_by = u.user_id) as total_refs
        FROM users u
        JOIN users ref_u ON ref_u.ref_by = u.user_id
        WHERE u.user_id != 7308147004 AND (u.is_banned IS NULL OR u.is_banned = 0)
          AND (SELECT COUNT(*) FROM votes WHERE voter_id = ref_u.user_id) > 0
        GROUP BY u.user_id
        ORDER BY active_refs DESC
        LIMIT 5
    """)
    rows = c.fetchall()
    conn.close()

    if not rows:
        await message.answer("🔗 Пока никто не привёл активных друзей.", parse_mode="HTML")
        return

    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    lines = ["🔗 <b>Топ рефоводов:</b>\n"]
    for i, row in enumerate(rows):
        name = f"@{row['username']}" if row['username'] else (row['first_name'] or f"ID{row['user_id']}")
        active = row['active_refs']
        total = row['total_refs']
        lines.append(f"{medals[i]} {name} — {active} играли (всего рефов: {total})")

    lines.append(f"\n<i>Зови своих → @shrimpgamesbot</i>")
    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("resolve"))
async def cmd_resolve(message: Message):
    """Подвести итоги текущего голосования"""
    if message.from_user.id != ADMIN_ID:
        return
    import httpx as hx
    try:
        async with hx.AsyncClient() as cl:
            r = await cl.post(
                "http://localhost:8007/api/game/resolve_votes",
                json={"admin_key": str(ADMIN_ID)}
            )
            data = r.json()
            if data.get("ok"):
                if data.get("outcome") == "game_over":
                    await message.answer(f"🏆 Игра завершена! Победитель: {data.get('winner_name')}")
                elif data.get("tie"):
                    await message.answer(f"⚖️ Ничья! Переголосование между: {', '.join(data.get('tied_names', []))}")
                else:
                    out = data.get('outcome','?')
                    msg = f"✅ Раунд завершён\n👤 Выбыл: {data.get('victim_name')}\nИсход: {'съеден' if out=='eliminated' else 'вернулся — Постанова сработала'}\nОсталось: {data.get('alive')}"
                    await message.answer(msg)
            else:
                await message.answer(f"❌ {data.get('error', 'Ошибка')}")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command("addtime"))
async def cmd_addtime(message: Message):
    """Прибавить минуты к текущему voting_ends: /addtime 120"""
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Использование: /addtime 120 (минут)")
        return
    try:
        minutes = int(parts[1])
    except ValueError:
        await message.answer("Неверное значение. Пример: /addtime 120")
        return
    conn = db()
    c = conn.cursor()
    game = c.execute("SELECT * FROM games WHERE status='active' ORDER BY id DESC LIMIT 1").fetchone()
    if not game:
        conn.close()
        await message.answer("❌ Нет активной игры")
        return
    game = dict(game)
    from datetime import datetime, timedelta
    current_end = datetime.strptime(game["voting_ends"], "%Y-%m-%d %H:%M:%S")
    new_end = current_end + timedelta(minutes=minutes)
    new_end_str = new_end.strftime("%Y-%m-%d %H:%M:%S")
    c.execute("UPDATE games SET voting_ends=? WHERE id=?", (new_end_str, game["id"]))
    conn.commit()
    conn.close()
    tallinn_end = new_end + timedelta(hours=3)
    await message.answer(f"✅ +{minutes} мин. Голосование до {tallinn_end.strftime('%H:%M')} Таллин")


@dp.message(Command("settimer"))
async def cmd_settimer(message: Message):
    """Установить таймер: /settimer 24 (часов до старта)"""
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Использование: /settimer 24 (количество часов)")
        return
    try:
        hours = float(parts[1])
    except ValueError:
        await message.answer("Неверное значение. Пример: /settimer 24")
        return
    ms = int(hours * 3600 * 1000)
    # Сохраним в БД для синхронизации
    import sqlite3 as _sq
    conn = _sq.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('timer_target', ?)",
                  (str(int(__import__('time').time()*1000) + ms),))
        conn.commit()
    except: pass
    conn.close()
    await message.answer(f"⏱ Таймер установлен: {hours} ч до старта")


async def send_vote_reminder():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM games WHERE status=\'active\' ORDER BY id DESC LIMIT 1")
    game = c.fetchone()
    if not game:
        conn.close()
        return
    game_id = game["id"]
    day = game["current_day"] or 1
    c.execute(
        "SELECT u.user_id, u.first_name FROM players p "
        "JOIN users u ON p.user_id=u.user_id "
        "WHERE p.game_id=? AND p.is_alive=1 "
        "AND u.user_id NOT IN (SELECT voter_id FROM votes WHERE game_id=? AND day_number=?)",
        (game_id, game_id, day)
    )
    not_voted = c.fetchall()
    conn.close()
    if not not_voted:
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗳 Голосовать", web_app=WebAppInfo(url=WEBAPP_URL))
    ]])
    for p in not_voted:
        try:
            await bot.send_message(
                p["user_id"],
                f"⏰ <b>Осталось 2 часа!</b>\n\nТы ещё не проголосовал в раунде {day}.\n"
                f"Не забудь — кто не голосует, теряет влияние!",
                parse_mode="HTML", reply_markup=kb
            )
        except Exception:
            pass
    try:
        await bot.send_message(
            CHAT_ID,
            f"⏰ <b>Осталось 2 часа до конца голосования!</b>\n\n"
            f"{len(not_voted)} игроков ещё не сделали выбор. Успейте!",
            parse_mode="HTML"
        )
    except Exception:
        pass


@dp.callback_query(F.data.startswith("clan_accept:"))
async def clan_accept_cb(callback: CallbackQuery):
    parts = callback.data.split(":")
    clan_id = int(parts[1])
    from_id = int(parts[2])
    user_id = callback.from_user.id
    try:
        conn = db(); c = conn.cursor()
        c.execute("CREATE TABLE IF NOT EXISTS clan_members (id INTEGER PRIMARY KEY AUTOINCREMENT, clan_id INTEGER, user_id INTEGER, joined_at DATETIME DEFAULT CURRENT_TIMESTAMP, UNIQUE(clan_id, user_id))")
        # Проверяем размер
        c.execute("SELECT COUNT(*) as cnt FROM clan_members WHERE clan_id=?", (clan_id,))
        cnt = c.fetchone()["cnt"]
        if cnt >= 4:
            await callback.answer("В клане уже максимум игроков!", show_alert=True)
            conn.close(); return
        c.execute("INSERT OR IGNORE INTO clan_members (clan_id, user_id) VALUES (?,?)", (clan_id, user_id))
        c.execute("UPDATE clan_invites SET status='accepted' WHERE clan_id=? AND to_id=?", (clan_id, user_id))
        # Считаем вступление в клан для ачивки
        try:
            c.execute("ALTER TABLE users ADD COLUMN joined_clan INTEGER DEFAULT 0")
        except: pass
        c.execute("UPDATE users SET joined_clan=COALESCE(joined_clan,0)+1 WHERE user_id=?", (user_id,))
        conn.commit()
        # Имя вступившего и название клана
        c.execute("SELECT first_name, username FROM users WHERE user_id=?", (user_id,))
        ur = c.fetchone()
        uname = (ur["first_name"] or ur["username"] or "Игрок") if ur else "Игрок"
        c.execute("SELECT name FROM clans WHERE id=?", (clan_id,))
        cr = c.fetchone()
        clan_name_val = cr["name"] if cr else "клан"
        conn.close()
        await callback.message.edit_text(f"✅ Ты вступил в союз!")
        # Уведомить лидера
        try:
            await bot.send_message(from_id, f"✅ <b>{uname}</b> принял твоё предложение и вступил в клан!", parse_mode="HTML")
        except: pass
        # Сообщение в чат
        import httpx as _hxj, random as _rj
        _join_msgs = [
            f"🤝 Кто-то вступил в клан «{clan_name_val}». Союз крепнет.",
            f"👥 «{clan_name_val}» пополнился. Команда собирается.",
            f"⚔️ Новый боец в рядах «{clan_name_val}». Конкуренты нервничают.",
            f"🔒 Ещё один примкнул к «{clan_name_val}». Клан растёт.",
            f"🌑 «{clan_name_val}» стал сильнее. Кто-то сделал правильный выбор.",
            f"🏴 Клан «{clan_name_val}» принял нового участника. Игра меняется.",
            f"💀 Один игрок перестал быть одиночкой — вступил в «{clan_name_val}».",
            f"⚡ «{clan_name_val}» растёт. Следи за ними.",
            f"🎯 Кто-то выбрал сторону — клан «{clan_name_val}».",
            f"👁 «{clan_name_val}» пополнился новым союзником. Район меняется.",
        ]
        try:
            async with _hxj.AsyncClient() as _clj:
                await _clj.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={"chat_id": "@shrimpgames_chat", "text": _rj.choice(_join_msgs), "parse_mode": "HTML"})
        except: pass
    except Exception as e:
        await callback.answer(str(e), show_alert=True)
    await callback.answer()


@dp.callback_query(F.data.startswith("clan_decline:"))
async def clan_decline_cb(callback: CallbackQuery):
    parts = callback.data.split(":")
    clan_id = int(parts[1])
    from_id = int(parts[2])
    user_id = callback.from_user.id
    try:
        conn = db(); c = conn.cursor()
        c.execute("UPDATE clan_invites SET status='declined' WHERE clan_id=? AND to_id=?", (clan_id, user_id))
        conn.commit()
        c.execute("SELECT first_name, username FROM users WHERE user_id=?", (user_id,))
        ur = c.fetchone()
        uname = (ur["first_name"] or ur["username"] or "Игрок") if ur else "Игрок"
        conn.close()
        await callback.message.edit_text("❌ Ты отказался от союза.")
        try:
            await bot.send_message(from_id, f"❌ <b>{uname}</b> отказался от союза.", parse_mode="HTML")
        except: pass
    except: pass
    await callback.answer()


@dp.message(Command("ban"))
async def cmd_ban(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /ban @username")
        return
    from database import ban_user
    username = parts[1].strip()
    ok = ban_user(username)
    if ok:
        await message.answer(f"🚫 Пользователь {username} заблокирован.")
    else:
        await message.answer(f"❌ Пользователь {username} не найден в БД.")

@dp.message(Command("unban"))
async def cmd_unban(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /unban @username")
        return
    from database import unban_user
    username = parts[1].strip()
    ok = unban_user(username)
    if ok:
        await message.answer(f"✅ Пользователь {username} разблокирован.")
    else:
        await message.answer(f"❌ Пользователь {username} не найден в БД.")

@dp.message(Command("nftstat"))
async def cmd_nftstat(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    from database import get_nft_counter, NFT_DROP_THRESHOLD
    data = get_nft_counter()
    total = data["total_stars"]
    drops = data["drops_given"]
    next_drop = NFT_DROP_THRESHOLD * (drops + 1)
    remaining = next_drop - total
    await message.answer(
        f"🎁 <b>NFT DROP — статистика</b>\n\n"
        f"⭐ Всего вложено: <b>{total}</b> звёзд\n"
        f"🖼 Дропов выдано: <b>{drops}</b>\n"
        f"⚡ До следующего дропа: <b>{remaining}</b> ⭐",
        parse_mode="HTML"
    )

@dp.message(Command("addgems"))
async def cmd_addgems(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("⚠️ Используй /addgem (без s) — там есть логирование и показывает новый баланс.")

@dp.message(Command("remind"))
async def cmd_remind(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    await send_vote_reminder()
    await message.answer("✅ Напоминания отправлены")


@dp.message(Command("push30"))
async def cmd_push30(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    conn = db()
    c = conn.cursor()
    # Все юзеры кто НЕ в next_game_queue (не записались на следующую игру)
    try:
        c.execute("""
            SELECT user_id FROM users
            WHERE user_id NOT IN (SELECT user_id FROM next_game_queue)
            AND user_id != ?
        """, (ADMIN_ID,))
    except Exception:
        # Таблица может не существовать если ещё никто не записывался
        c.execute("SELECT user_id FROM users WHERE user_id != ?", (ADMIN_ID,))
    not_in_queue = [r["user_id"] for r in c.fetchall()]
    conn.close()
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔥 Записаться", web_app=WebAppInfo(url=WEBAPP_URL))
    ]])
    sent = 0
    blocked = []
    conn2 = db(); c2 = conn2.cursor()
    for uid in not_in_queue:
        try:
            await bot.send_message(
                uid,
                "⚔️ <b>Разборки на районе — стрелка через 30 минут!</b>\n\nРегистрация открыта! Скоро начнётся следующая битва.\n\nЗаходи, записывайся и выживи до конца 💪\n\n🏆 Главный приз — NFT\n🥈 2 место — 100 Telegram Stars\n🥉 3 место — 50 Telegram Stars\n🌹 4 место — живая роза\n🧸 5 место — мягкий мишка",
                parse_mode="HTML",
                reply_markup=kb
            )
            sent += 1
        except Exception:
            c2.execute("SELECT username, first_name FROM users WHERE user_id=?", (uid,))
            u = c2.fetchone()
            if u:
                blocked.append(f"@{u['username']}" if u['username'] else u['first_name'])
    conn2.close()
    blocked_text = "\n".join(blocked) if blocked else "нет"
    await message.answer(
        f"✅ Пуш отправлен {sent} юзерам (не в очереди)\n"
        f"🚫 Заблокировали ({len(blocked)}):\n{blocked_text}"
    )


@dp.message(Command("push"))
async def cmd_push(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    game = get_active_game()
    if not game:
        await message.answer("❌ Нет активной игры")
        return
    game_id = game["id"]
    conn = db()
    c = conn.cursor()
    # Все юзеры кто НЕ в текущей игре
    c.execute("""
        SELECT user_id FROM users
        WHERE user_id NOT IN (SELECT user_id FROM players WHERE game_id=?)
        AND user_id != ?
    """, (game_id, ADMIN_ID))
    not_in_game = [r["user_id"] for r in c.fetchall()]
    conn.close()
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="▶️ Участвовать", web_app=WebAppInfo(url=WEBAPP_URL))
    ]])
    sent = 0
    blocked = []
    conn2 = db(); c2 = conn2.cursor()
    for uid in not_in_game:
        try:
            await bot.send_message(
                uid,
                "⚔️ <b>Разборки на районе — Стрелка #6</b>\n\n🔥 Регистрация открыта! Новая битва начинается скоро.\nЗаходи, записывайся и выживи до конца 💪\n\n👇 Жми и участвуй\n@shrimpgamesbot",
                parse_mode="HTML",
                reply_markup=kb
            )
            sent += 1
        except:
            c2.execute("SELECT username, first_name FROM users WHERE user_id=?", (uid,))
            u = c2.fetchone()
            if u:
                blocked.append(f"@{u['username']}" if u['username'] else u['first_name'])
    conn2.close()
    blocked_text = "\n".join(blocked) if blocked else "нет"
    await message.answer(f"✅ Пуш отправлен {sent} юзерам из {len(not_in_game)}\n🚫 Заблокировали ({len(blocked)}):\n{blocked_text}")


@dp.message(Command("queue"))
async def queue_cmd(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    from database import get_conn
    conn = get_conn(); c = conn.cursor()
    try:
        c.execute("""SELECT u.user_id, u.username, u.first_name, q.registered_at
                     FROM next_game_queue q
                     JOIN users u ON u.user_id=q.user_id
                     ORDER BY q.registered_at ASC""")
        rows = c.fetchall()
        conn.close()
        if not rows:
            await message.answer("Никто не записался на следующую игру")
            return
        lines = [f"📋 <b>Записались на следующую игру ({len(rows)}):</b>"]
        for i, r in enumerate(rows, 1):
            name = f"@{r['username']}" if r['username'] else r['first_name']
            lines.append(f"{i}. {name} (ID: {r['user_id']})")
        await message.answer("\n".join(lines), parse_mode="HTML")
    except Exception as e:
        await message.answer(f"Ошибка: {e}")


@dp.message(Command("whoinvited"))
async def whoinvited_cmd(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.split()[1:]
    if not args:
        await message.answer("Использование: /whoinvited @username или ID")
        return
    from database import get_user_by_username, get_conn
    arg = args[0].lstrip("@")
    # Попробуем по username или ID
    if arg.isdigit():
        conn = get_conn(); c = conn.cursor()
        c.execute("SELECT * FROM users WHERE user_id=?", (int(arg),))
        user = c.fetchone(); conn.close()
    else:
        user = get_user_by_username(arg)
    if not user:
        await message.answer(f"❌ Не найден: {arg}")
        return
    uid = user["user_id"]
    uname = f"@{user['username']}" if user["username"] else user["first_name"]
    ref_by = user["ref_by"]
    if not ref_by:
        await message.answer(f"👤 {uname} (ID: {uid})\n\nПришёл сам, без реферала")
        return
    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT user_id, username, first_name FROM users WHERE user_id=?", (ref_by,))
    inv = c.fetchone(); conn.close()
    if inv:
        inv_name = f"@{inv['username']}" if inv["username"] else inv["first_name"]
        await message.answer(f"👤 {uname} (ID: {uid})\n\nПришёл по реферальной ссылке от:\n➡️ {inv_name} (ID: {ref_by})")
    else:
        await message.answer(f"👤 {uname} (ID: {uid})\n\nRef_by ID: {ref_by} (не найден в базе)")


@dp.message(Command("addref"))
async def addref_cmd(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.split()[1:]
    if not args:
        await message.answer("Использование: /addref @username")
        return
    from database import get_user_by_username, get_conn
    uname = args[0].lstrip("@")
    user = get_user_by_username(uname)
    if not user:
        await message.answer(f"❌ @{uname} — не найден в базе")
        return
    # Создаём фейкового реферала привязанного к этому юзеру
    conn = get_conn()
    c = conn.cursor()
    import time, random
    fake_id = int(time.time() * 1000) % 2000000000 + random.randint(1, 9999)
    c.execute("INSERT OR IGNORE INTO users (user_id, username, first_name, ref_by) VALUES (?,?,?,?)",
              (fake_id, f"ref_fake_{fake_id}", "Реферал", user["user_id"]))
    conn.commit()
    conn.close()
    await message.answer(f"✅ @{uname} — +1 реферал добавлен (итого проверь /friends)")


@dp.message(Command("sale"))
async def sale_cmd(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    import httpx, os
    admin_id = os.getenv("ADMIN_ID", str(message.from_user.id))
    async with httpx.AsyncClient() as cl:
        r = await cl.post("http://localhost:8007/api/sale/start",
                          json={"admin_key": admin_id})
        d = r.json()
    if d.get("ok"):
        sale_end = d['sale_end']
        await message.answer(f"🔥 Скидка 50% запущена на 24 часа!\nКиллер, Постанова, Ворюга\nДо: {sale_end} UTC")
    else:
        await message.answer("Ошибка запуска скидки")


@dp.message(Command("premium"))
async def grant_premium_cmd(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.split()[1:]
    if not args:
        await message.answer("Использование: /premium @username1 @username2 ...")
        return
    from database import set_premium_force, get_user_by_username
    results = []
    for arg in args:
        uname = arg.lstrip("@")
        user = get_user_by_username(uname)
        if user:
            set_premium_force(user["user_id"], True)
            results.append(f"✅ @{uname} — премиум выдан")
        else:
            results.append(f"❌ @{uname} — не найден в базе")
    await message.answer("\n".join(results))


@dp.message(Command("admin"))
async def admin_cmd(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer(
        "⚙️ <b>Админ</b>\n\n"
        "/open — открыть регистрацию\n"
        "/go — запустить игру\n"
        "/addbots 3 — добавить N ботов в игру\n"
        "/resolve — итоги голосования\n"
        "/remind — напоминание о голосовании\n"
        "/settimer 24 — таймер до старта\n"
        "/cancel — сбросить игру\n"
        "/friends — юзеры и рефералы\n"
        "/stats — статистика бота\n"
        "/premium @user — выдать премиум\n",
        parse_mode="HTML"
    )


FAKE_PLAYERS_POOL = [
    {"user_id": 9000001, "username": "test_alice",   "first_name": "Alice"},
    {"user_id": 9000002, "username": "test_bob",     "first_name": "Bob"},
    {"user_id": 9000003, "username": "test_charlie", "first_name": "Charlie"},
    {"user_id": 9000004, "username": "test_diana",   "first_name": "Diana"},
    {"user_id": 9000005, "username": "test_eve",     "first_name": "Eve"},
    {"user_id": 9000006, "username": "test_frank",   "first_name": "Frank"},
]

@dp.message(Command("addbots"))
async def cmd_addbots(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    # Парсим количество
    parts = message.text.split()
    try:
        count = int(parts[1]) if len(parts) > 1 else 2
        count = min(count, len(FAKE_PLAYERS_POOL))
    except:
        await message.answer("❌ Укажи количество: /addbots 3")
        return

    conn = db()
    c = conn.cursor()
    c.execute("SELECT * FROM games WHERE status IN ('waiting','active') ORDER BY id DESC LIMIT 1")
    game = c.fetchone()
    if not game:
        conn.close()
        await message.answer("❌ Нет активной игры. Сначала /open")
        return

    game_id = game["id"]
    added = 0
    for p in FAKE_PLAYERS_POOL[:count]:
        try:
            c.execute("SELECT id FROM players WHERE game_id=? AND user_id=?", (game_id, p["user_id"]))
            if c.fetchone():
                continue
            c.execute("INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?,?,?)",
                      (p["user_id"], p["username"], p["first_name"]))
            c.execute("INSERT OR IGNORE INTO players (game_id, user_id) VALUES (?,?)",
                      (game_id, p["user_id"]))
            added += 1
        except: pass

    conn.commit()
    conn.close()
    await message.answer(f"✅ Добавлено ботов: {added}\nИгра #{game['number']}, статус: {game['status']}")



# ── /open — создать новую игру и открыть регистрацию ──
@dp.message(Command("open"))
async def cmd_open(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    conn = db()
    c = conn.cursor()
    # Завершаем старые активные игры
    c.execute("UPDATE games SET status='finished' WHERE status IN ('active','waiting')")
    # Сбрасываем все активные анонимусы — ники возвращаются
    c.execute("UPDATE items SET status='used' WHERE item_type='anon_player' AND status='active'")
    # Создаём новую игру (игнорируем тестовые игры с номерами выше 90)
    c.execute("SELECT MAX(number) as mx FROM games WHERE number < 90")
    row = c.fetchone()
    next_num = (row["mx"] or 0) + 1
    c.execute(
        "INSERT INTO games (number, status, max_players, prize_desc, prize_link) VALUES (?,?,?,?,?)",
        (next_num, "waiting", 0, "NFT Giraffe Pool Float", "https://t.me/nft/PoolFloat-131965")
    )
    game_id = c.lastrowid

    # Переносим всех из очереди next_game_queue
    try:
        c.execute("CREATE TABLE IF NOT EXISTS next_game_queue (user_id INTEGER PRIMARY KEY, registered_at DATETIME DEFAULT CURRENT_TIMESTAMP)")
        c.execute("INSERT OR IGNORE INTO players (game_id, user_id) SELECT ?, user_id FROM next_game_queue", (game_id,))
        transferred = c.rowcount
        c.execute("DELETE FROM next_game_queue")
        conn.commit()
    except: transferred = 0
    conn.commit()
    conn.close()

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="▶️ Участвовать", web_app=WebAppInfo(url=WEBAPP_URL))
    ]])
    try:
        await bot.send_message(
            CHAT_ID,
            f"🦐 <b>Открыта регистрация на Игру #{next_num}!</b>\n\n"
            f"Нажми кнопку ниже чтобы записаться.\n"
            f"Когда наберётся достаточно игроков — админ запустит игру командой /go",
            parse_mode="HTML",
            reply_markup=kb
        )
    except: pass

    # Пуш всем юзерам в бота
    conn2 = db()
    c2 = conn2.cursor()
    c2.execute("SELECT user_id FROM users WHERE user_id != ?", (ADMIN_ID,))
    all_users = [r["user_id"] for r in c2.fetchall()]
    conn2.close()
    sent = 0
    for uid in all_users:
        try:
            await bot.send_message(
                uid,
                f"🦐 <b>Открыта регистрация на Игру #{next_num}!</b>\n\n"
                f"Запишись и выживи дольше всех — победитель получит приз 🏆",
                parse_mode="HTML",
                reply_markup=kb
            )
            sent += 1
        except: pass

    await message.answer(f"✅ Стрелка #{next_num} создана! Уведомлено юзеров: {sent}")


# ── /go — запустить игру с записавшимися игроками ──
@dp.message(Command("go"))
async def cmd_go(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    conn = db()
    c = conn.cursor()
    c.execute("SELECT * FROM games WHERE status='waiting' ORDER BY id DESC LIMIT 1")
    game = c.fetchone()
    if not game:
        conn.close()
        await message.answer("❌ Нет игры в статусе ожидания. Сначала создай /open")
        return

    game_id = game["id"]
    c.execute("SELECT COUNT(*) as cnt FROM players WHERE game_id=?", (game_id,))
    cnt = c.fetchone()["cnt"]
    if cnt < 2:
        conn.close()
        await message.answer(f"❌ Записалось только {cnt} игроков. Нужно минимум 2.")
        return

    # Запускаем игру — голосование каждый час, ночной перерыв 20:00-08:00 Tallinn (UTC+3)
    from datetime import timezone
    now_utc = datetime.utcnow()
    voting_ends = (now_utc + timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
    c.execute("""UPDATE games SET status='active', current_day=1,
              started_at=CURRENT_TIMESTAMP, voting_ends=? WHERE id=?""",
              (voting_ends, game_id))
    c.execute("UPDATE users SET games_played=games_played+1 WHERE user_id IN (SELECT user_id FROM players WHERE game_id=?)",
              (game_id,))
    conn.commit()

    # Список игроков
    c.execute("""SELECT u.user_id, u.first_name, u.username
                 FROM players p JOIN users u ON p.user_id=u.user_id
                 WHERE p.game_id=?""", (game_id,))
    players = [dict(r) for r in c.fetchall()]
    conn.close()

    num = game["number"] or next_num

    # Уведомить каждого игрока
    vote_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗳 Голосовать", web_app=WebAppInfo(url=WEBAPP_URL))
    ]])
    for p in players:
        try:
            await bot.send_message(
                p["user_id"],
                f"🗡 <b>Стрелка #{num} началась!</b>\n\n{RULES_TEXT}",
                parse_mode="HTML",
                reply_markup=vote_kb
            )
        except: pass

    # Уведомить чат
    names = ", ".join([f"@{p['username']}" if p['username'] else p['first_name'] for p in players])
    try:
        await bot.send_message(
            CHAT_ID,
            f"🗡 <b>Стрелка #{num} началась!</b>\n\n"
            f"👥 Игроков: {cnt}\n\n"
            f"Удачи всем! Кто выживет? 💀",
            parse_mode="HTML",
            reply_markup=vote_kb
        )
    except: pass

    await message.answer(f"✅ Стрелка #{num} запущена! {cnt} игроков. Удачи!")



# ── /cancel — сбросить текущую игру ──
@dp.message(Command("cancel"))
async def cmd_cancel(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    conn = db()
    c = conn.cursor()
    c.execute("UPDATE games SET status='finished' WHERE status IN ('active','waiting')")
    affected = c.rowcount
    conn.commit()
    conn.close()
    if affected:
        try:
            await bot.send_message(
                CHAT_ID,
                "❌ <b>Игра отменена администратором.</b>\n\nСледите за новыми играми!",
                parse_mode="HTML"
            )
        except: pass
        await message.answer("✅ Игра отменена.")
    else:
        await message.answer("Нет активных игр.")



@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    conn = db()
    c = conn.cursor()

    # Всего юзеров
    c.execute("SELECT COUNT(*) as cnt FROM users")
    total_users = c.fetchone()["cnt"]

    # Присоединились сегодня (по Таллину UTC+3)
    c.execute("SELECT COUNT(*) as cnt FROM users WHERE created_at >= datetime('now', '-3 hours', 'start of day', '+3 hours')")
    today_users = c.fetchone()["cnt"]

    # Покупки предметов за звёзды (без меня)
    c.execute("""CREATE TABLE IF NOT EXISTS purchases
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
                  item_type TEXT, stars INTEGER, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("SELECT COUNT(*) as cnt, COALESCE(SUM(stars),0) as total FROM purchases WHERE user_id != ?", (ADMIN_ID,))
    prow = c.fetchone()
    items_purchases = prow["cnt"]
    items_stars = prow["total"]

    # Покупки гемов за звёзды (без меня)
    c.execute("SELECT COALESCE(SUM(gems_bought_total),0) as total, COUNT(*) as cnt FROM users WHERE gems_bought_total > 0 AND user_id != ?", (ADMIN_ID,))
    grow = c.fetchone()
    gems_stars = grow["total"]
    gems_buyers = grow["cnt"]

    total_purchases = items_purchases + gems_buyers
    total_stars = items_stars + gems_stars

    # Топ-5 донаторов (звёзды за предметы + звёзды за гемы)
    c.execute("""
        SELECT u.user_id, u.username, u.first_name,
               COALESCE(p.stars,0) + COALESCE(u.gems_bought_total,0) as total_stars
        FROM users u
        LEFT JOIN (SELECT user_id, SUM(stars) as stars FROM purchases GROUP BY user_id) p ON p.user_id=u.user_id
        WHERE (COALESCE(p.stars,0) + COALESCE(u.gems_bought_total,0)) > 0 AND u.user_id != ?
        ORDER BY total_stars DESC LIMIT 5
    """, (ADMIN_ID,))
    top_donors = c.fetchall()

    # Активная игра
    c.execute("SELECT * FROM games WHERE status IN ('waiting','active') ORDER BY id DESC LIMIT 1")
    game = c.fetchone()

    conn.close()

    game_info = ""
    if game:
        game_info = f"\n\n🎮 Стрелка #{game['number']} — {game['status']}, разборки {game['current_day'] or 0}"

    donors_text = ""
    if top_donors:
        lines = []
        medals = ["🥇","🥈","🥉","4️⃣","5️⃣"]
        for i, d in enumerate(top_donors):
            name = f"@{d['username']}" if d['username'] else (d['first_name'] or f"ID{d['user_id']}")
            lines.append(f"  {medals[i]} {name} — {d['total_stars']} ⭐")
        donors_text = "\n\n💎 <b>Топ-5 донаторов:</b>\n" + "\n".join(lines)

    await message.answer(
        f"📊 <b>Статистика бота</b>\n\n"
        f"👥 Всего юзеров: <b>{total_users}</b>\n"
        f"🆕 Пришли сегодня: <b>{today_users}</b>\n\n"
        f"🛒 Покупок за ⭐ (кроме меня): <b>{total_purchases}</b>\n"
        f"  └ предметов: {items_purchases} шт. ({items_stars} ⭐)\n"
        f"  └ гемов: {gems_buyers} чел. ({gems_stars} ⭐)\n"
        f"⭐ Всего звёзд потрачено: <b>{total_stars}</b>"
        f"{donors_text}"
        f"{game_info}",
        parse_mode="HTML"
    )


@dp.message(Command("give"))
async def give_cmd(message: Message):
    """/give @username item_type [count] — выдать связь игроку"""
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split()
    if len(parts) < 3:
        await message.answer("Использование: /give @username item_type [count]\nПример: /give @b800y resurrect")
        return
    username_raw = parts[1].lstrip("@")
    item_type = parts[2]
    count = int(parts[3]) if len(parts) > 3 else 1

    valid_items = ["shield","double_vote","resurrect","killer","spy","anon_msg","tiebreaker","anon_player","hacker","black_mark"]
    if item_type not in valid_items:
        await message.answer(f"❌ Неизвестный предмет: {item_type}\nДоступные: {', '.join(valid_items)}")
        return

    conn = db()
    c = conn.cursor()
    c.execute("SELECT user_id, first_name FROM users WHERE username=?", (username_raw,))
    user = c.fetchone()
    if not user:
        conn.close()
        await message.answer(f"❌ Юзер @{username_raw} не найден в базе")
        return

    target_id = user["user_id"]
    c.execute("SELECT id FROM games WHERE status IN ('waiting','active') ORDER BY id DESC LIMIT 1")
    game = c.fetchone()
    game_id = game["id"] if game else None

    for _ in range(count):
        c.execute("INSERT INTO items (user_id, item_type, game_id, status) VALUES (?,?,?,'active')",
                  (target_id, item_type, game_id))
    conn.commit()
    conn.close()

    item_display = {"shield":"🛡️ Крышануться","double_vote":"💎 Двустволка","resurrect":"✨ Постанова",
                    "killer":"💀 Киллер","spy":"🐭 Стукач","anon_msg":"📩 Малява","tiebreaker":"⚖️ Решала",
                    "anon_player":"👻 Анонимус","hacker":"💰 Ворюга","black_mark":"🚔 Мусорнуться"}
    name = item_display.get(item_type, item_type)
    await message.answer(f"✅ Выдано {count}x {name} → @{username_raw} (ID {target_id})")
    try:
        await bot.send_message(target_id,
            f"🎁 <b>Тебе выдан предмет от администратора!</b>\n\n{name} добавлен в твой инвентарь.",
            parse_mode="HTML")
    except: pass


@dp.message(Command("notify"))
async def notify_cmd(message: Message):
    user_id = message.from_user.id
    conn = db()
    c = conn.cursor()
    try:
        c.execute("ALTER TABLE users ADD COLUMN notifications_enabled INTEGER DEFAULT 1")
        conn.commit()
    except: pass
    c.execute("SELECT notifications_enabled FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    current = row["notifications_enabled"] if row and row["notifications_enabled"] is not None else 1
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔔 Включить" if not current else "✅ Включены", callback_data="notify_on"),
        InlineKeyboardButton(text="🔕 Выключить" if current else "❌ Выключены", callback_data="notify_off"),
    ]])
    status = "включены 🔔" if current else "выключены 🔕"
    await message.answer(f"Уведомления сейчас {status}\nВыбери:", reply_markup=kb)


@dp.callback_query(F.data.in_({"notify_on", "notify_off"}))
async def cb_notify(callback: CallbackQuery):
    user_id = callback.from_user.id
    enabled = 1 if callback.data == "notify_on" else 0
    conn = db()
    c = conn.cursor()
    c.execute("UPDATE users SET notifications_enabled=? WHERE user_id=?", (enabled, user_id))
    conn.commit()
    conn.close()
    status = "включены 🔔" if enabled else "выключены 🔕"
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔔 Включить" if not enabled else "✅ Включены", callback_data="notify_on"),
        InlineKeyboardButton(text="🔕 Выключить" if enabled else "❌ Выключены", callback_data="notify_off"),
    ]])
    await callback.message.edit_text(f"Уведомления {status}", reply_markup=kb)
    await callback.answer()


async def reminder_loop():
    """Каждую минуту проверяем — осталось ли 2 минуты до конца голосования"""
    reminded_rounds = set()  # game_id + day чтобы не слать дважды
    while True:
        await asyncio.sleep(60)
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            game = c.execute("SELECT * FROM games WHERE status='active' ORDER BY id DESC LIMIT 1").fetchone()
            if not game or not game["voting_ends"]:
                conn.close()
                continue
            from datetime import datetime, timedelta
            voting_ends = datetime.strptime(game["voting_ends"], "%Y-%m-%d %H:%M:%S")
            now_utc = datetime.utcnow()
            mins_left = (voting_ends - now_utc).total_seconds() / 60
            game_id = game["id"]
            day = game["current_day"] or 1
            key = f"{game_id}_{day}"
            # Шлём если осталось от 1 до 3 минут и ещё не слали
            if 1 <= mins_left <= 3 and key not in reminded_rounds:
                reminded_rounds.add(key)
                # Кто не проголосовал
                not_voted = c.execute(
                    "SELECT u.user_id FROM players p "
                    "JOIN users u ON p.user_id=u.user_id "
                    "WHERE p.game_id=? AND p.is_alive=1 "
                    "AND u.user_id NOT IN (SELECT voter_id FROM votes WHERE game_id=? AND day_number=?)",
                    (game_id, game_id, day)
                ).fetchall()
                conn.close()
                if not not_voted:
                    continue
                kb = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="🗳 Голосовать", web_app=WebAppInfo(url=WEBAPP_URL))
                ]])
                for p in not_voted:
                    if p["user_id"] in [9000001,9000002,9000003,9000004]:
                        continue
                    try:
                        await bot.send_message(
                            p["user_id"],
                            f"⚠️ <b>Осталось 2 минуты!</b>\n\nТы ещё не проголосовал в раунде {day}.\nУспей — голосование скоро закрывается!",
                            parse_mode="HTML", reply_markup=kb
                        )
                    except: pass
            else:
                conn.close()
            # Чистим старые ключи
            if len(reminded_rounds) > 50:
                reminded_rounds = set(list(reminded_rounds)[-20:])
        except Exception:
            pass


async def commands_broadcast_loop():
    """Постим список команд в чат в 06:00 и 18:00 по Таллину (UTC+3)"""
    from datetime import datetime, timezone, timedelta
    TALLINN = timezone(timedelta(hours=3))
    # Сразу помечаем текущий час как уже отправленный — чтобы рестарт не триггерил повтор
    now0 = datetime.now(timezone(timedelta(hours=3)))
    sent_today = set()
    if now0.hour in (6, 18):
        sent_today.add(f"{now0.strftime('%Y-%m-%d')}-{now0.hour}")

    COMMANDS_MSG = (
        "📋 <b>Команды чата:</b>\n\n"
        "🎮 <b>Игра:</b>\n"
        "🗡 /rules — Правила игры\n"
        "👤 /me — Моя статистика\n"
        "🗳 /top — Топ 5 игроков\n"
        "🔗 /ref — Топ 5 рефоводов\n"
        "🌍 /game — Викторина\n"
        "⚔️ /duel @username 100 — Дуэль\n"
        "💀 /diss @username — Опустить чела за 1 ⭐\n\n"
        "🎰 <b>Казино:</b>\n"
        "💰 /bank — Баланс Гемов\n"
        "🛒 /buy — Купить Гемы за Stars\n"
        "🎰 /spin &lt;ставка&gt; — Слоты\n"
        "🃏 /redblack &lt;ставка&gt; — Red &amp; Black\n"
        "✈️ /crash &lt;ставка&gt; — Краш\n"
        "🎲 /dice &lt;ставка&gt; — Кубик vs казино\n"
        "🎲 /dice &lt;ставка&gt; (реплай) — Кубик vs игрок\n"
        "🎡 /roul &lt;ставка&gt; — Рулетка (x35)\n"
        "🏆 /jackpot — Недельный джекпот\n"
        "🖼 /nft — Текущие розыгрыши NFT\n\n"
        "<i>Играй и зови друзей → @shrimpgamesbot</i>"
    )

    while True:
        await asyncio.sleep(30)
        try:
            now = datetime.now(TALLINN)
            hour = now.hour
            key = None
            if hour == 6:
                key = f"{now.strftime('%Y-%m-%d')}-6"
            elif hour == 18:
                key = f"{now.strftime('%Y-%m-%d')}-18"

            if key and key not in sent_today:
                sent_today.add(key)
                await bot.send_message("@shrimpgames_chat", COMMANDS_MSG, parse_mode="HTML")
                # Чистим старые ключи
                if len(sent_today) > 10:
                    sent_today = set(list(sent_today)[-4:])
        except Exception:
            pass


async def auto_push_unreg():
    """Авто-пуш незарегавшимся — каждые 2 дня в 12:00"""
    conn = db()
    c = conn.cursor()
    game = c.execute(
        "SELECT id, number FROM games WHERE status='waiting' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not game:
        conn.close()
        return
    game_id = game["id"]
    game_num = game["number"]
    rows = c.execute("""
        SELECT u.user_id FROM users u
        WHERE u.is_banned = 0
          AND u.user_id NOT IN (
              SELECT p.user_id FROM players p WHERE p.game_id = ?
          )
    """, (game_id,)).fetchall()
    conn.close()

    text = "Ты еще не зареган в новой игре. Заходи, скоро начнём!"
    for row in rows:
        try:
            await bot.send_message(row["user_id"], text)
        except:
            pass
        await asyncio.sleep(0.05)


@dp.message(Command("push"))
async def cmd_push(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    conn = db(); c = conn.cursor()
    game = c.execute(
        "SELECT id, number FROM games WHERE status='waiting' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not game:
        conn.close()
        await message.answer("❌ Нет игры в статусе waiting")
        return
    game_id = game["id"]
    game_num = game["number"]
    rows = c.execute("""
        SELECT u.user_id FROM users u
        WHERE (u.is_banned IS NULL OR u.is_banned = 0)
          AND (u.bot_blocked IS NULL OR u.bot_blocked = 0)
          AND u.user_id != ?
          AND u.user_id NOT IN (
              SELECT p.user_id FROM players p WHERE p.game_id = ?
          )
    """, (ADMIN_ID, game_id)).fetchall()
    conn.close()

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗡 Записаться", web_app=WebAppInfo(url=WEBAPP_URL))
    ]])
    text = (
        f"🗡 <b>Стрелка #{game_num} скоро начинается!</b>\n\n"
        f"Район снова собирается. Ты ещё не записался — успей пока есть места.\n\n"
        f"Последние выжившие заберут призы 🏆"
    )

    sent = 0
    for row in rows:
        try:
            await bot.send_message(row["user_id"], text, parse_mode="HTML", reply_markup=kb)
            sent += 1
        except TelegramForbiddenError:
            from database import mark_bot_blocked
            mark_bot_blocked(row["user_id"])
        except: pass
        await asyncio.sleep(0.05)

    await message.answer(f"✅ Пуш отправлен {sent} игрокам")


async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.ensure_future(reminder_loop())
    asyncio.ensure_future(commands_broadcast_loop())

    async def mayor_daily_gems():
        """Мэр получает 25 гемов каждый день"""
        from database import get_authority, add_gems
        mayor = get_authority('mayor')
        if mayor and mayor.get('user_id'):
            add_gems(mayor['user_id'], 25)
            try:
                mname = f"@{mayor['username']}" if mayor.get('username') else mayor.get('first_name', 'Мэр')
                await bot.send_message(mayor['user_id'], "🏛 Мэр берёт взятки — <b>+25 Гемов</b> зачислено на баланс!", parse_mode="HTML")
            except: pass

    scheduler = AsyncIOScheduler(timezone="Europe/Tallinn")
    scheduler.add_job(auto_push_unreg, "cron", hour=12, minute=0, day="*/2")
    scheduler.add_job(mayor_daily_gems, "cron", hour=10, minute=0)
    # Автозакрытие аукциона 27 мая в 19:00 МСК
    from datetime import datetime as _sdt
    _auction_end = _sdt(2026, 5, 27, 19, 0, 0)
    scheduler.add_job(auto_end_auction, "date", run_date=_auction_end)
    # Напоминания об аукционе
    async def _auc_remind(text):
        if AUCTION_ACTIVE():
            await bot.send_message(CHAT_ID, text, parse_mode="HTML")
    async def _remind_12h(): await _auc_remind("🎁 <b>До конца аукциона осталось 12 часов!</b>\n\n⏰ Дедлайн: 27 мая, 19:00 МСК\n\nТоп-1 донатер заберёт NFT. Жми /bet")
    async def _remind_6h():  await _auc_remind("🎁 <b>До конца аукциона осталось 6 часов!</b>\n\n⏰ Дедлайн: 27 мая, 19:00 МСК\n\nТоп-1 донатер заберёт NFT. Жми /bet")
    async def _remind_1h():  await _auc_remind("🎁 <b>Остался 1 час!</b> Аукцион закрывается в 19:00 МСК\n\nПоследний шанс обогнать лидера. Жми /bet")
    async def _remind_15m(): await _auc_remind("⚡ <b>15 минут до конца аукциона!</b>\n\nКто успеет — тот победит. Жми /bet")
    async def _remind_1m():  await _auc_remind("🔥 <b>1 МИНУТА!</b> Аукцион закрывается через минуту!")
    scheduler.add_job(_remind_12h, "date", run_date=_sdt(2026, 5, 27,  7, 0, 0))
    scheduler.add_job(_remind_6h,  "date", run_date=_sdt(2026, 5, 27, 13, 0, 0))
    scheduler.add_job(_remind_1h,  "date", run_date=_sdt(2026, 5, 27, 18, 0, 0))
    scheduler.add_job(_remind_15m, "date", run_date=_sdt(2026, 5, 27, 18, 45, 0))
    scheduler.add_job(_remind_1m,  "date", run_date=_sdt(2026, 5, 27, 18, 59, 0))
    # Реферальная гонка — конец 31 мая 18:00 МСК (Tallinn = UTC+3 летом = МСК)
    async def _ref_remind(text):
        await bot.send_message(CHAT_ID, text, parse_mode="HTML")
    async def _ref_48h(): await _ref_remind(
        "🏁 <b>До конца реферальной гонки — 48 часов!</b>\n\n"
        "👥 Кто привёл больше друзей — получит приз\n"
        "⏰ Дедлайн: 31 мая, 18:00 МСК\n\n"
        "Зови друзей → @shrimpgamesbot\nТоп рефоводов: /ref"
    )
    async def _ref_24h(): await _ref_remind(
        "🏁 <b>До конца реферальной гонки — 24 часа!</b>\n\n"
        "👥 Ещё можно обогнать лидеров — зови всех\n"
        "⏰ Дедлайн: 31 мая, 18:00 МСК\n\n"
        "Зови друзей → @shrimpgamesbot\nТоп рефоводов: /ref"
    )
    async def _ref_12h(): await _ref_remind(
        "⚡ <b>До конца реферальной гонки — 12 часов!</b>\n\n"
        "🔥 Финальный рывок — последний шанс вырваться вперёд\n"
        "⏰ Конец сегодня в 18:00 МСК\n\n"
        "Зови друзей → @shrimpgamesbot\nТоп рефоводов: /ref"
    )
    async def _ref_2h(): await _ref_remind(
        "🚨 <b>ОСТАЛОСЬ 2 ЧАСА!</b> Реферальная гонка закрывается!\n\n"
        "⏰ Конец в 18:00 МСК — торопись!\n\n"
        "Зови друзей → @shrimpgamesbot\nТоп рефоводов: /ref"
    )
    scheduler.add_job(_ref_48h, "date", run_date=_sdt(2026, 5, 29, 18, 0, 0))
    scheduler.add_job(_ref_24h, "date", run_date=_sdt(2026, 5, 30, 18, 0, 0))
    scheduler.add_job(_ref_12h, "date", run_date=_sdt(2026, 5, 31,  6, 0, 0))
    scheduler.add_job(_ref_2h,  "date", run_date=_sdt(2026, 5, 31, 16, 0, 0))
    scheduler.start()

    await dp.start_polling(bot, handle_signals=False)




# ── АИРДРОП ──
import random as _random

AIRDROP_ITEMS = [
    ("killer",     "💀 Киллер",     "killer.png"),
    ("resurrect",  "🎭 Постанова",  "resurection.png"),
    ("shield",     "🤵 Крыша",      "shield.png"),
    ("hacker",     "💰 Ворюга",     "hacker.png"),
    ("spy",        "🐭 Стукач",     "spy.png"),
    ("tiebreaker", "⚖️ Решала",     "bit.png"),
    ("double_vote","🔫 Двустволка", "shotgun.png"),
    ("anon_msg",   "📩 Малява",     "chats.png"),
    ("black_mark", "🚔 Розыск",     "police.png"),
]

_airdrop_active = {}  # msg_id -> {item_type, claimed: False}

def _airdrop_db_set(msg_id, item_type, item_name):
    try:
        from database import get_conn
        conn = get_conn(); c = conn.cursor()
        c.execute("CREATE TABLE IF NOT EXISTS airdrops (msg_id INTEGER PRIMARY KEY, item_type TEXT, item_name TEXT, claimed INTEGER DEFAULT 0)")
        c.execute("INSERT OR REPLACE INTO airdrops (msg_id, item_type, item_name, claimed) VALUES (?,?,?,0)", (msg_id, item_type, item_name))
        conn.commit(); conn.close()
    except: pass

def _airdrop_db_get(msg_id):
    try:
        from database import get_conn
        conn = get_conn(); c = conn.cursor()
        c.execute("CREATE TABLE IF NOT EXISTS airdrops (msg_id INTEGER PRIMARY KEY, item_type TEXT, item_name TEXT, claimed INTEGER DEFAULT 0)")
        r = c.execute("SELECT item_type, item_name, claimed FROM airdrops WHERE msg_id=?", (msg_id,)).fetchone()
        conn.close()
        if r: return {"item_type": r["item_type"], "item_name": r["item_name"], "claimed": bool(r["claimed"])}
    except: pass
    return None

def _airdrop_db_claim(msg_id):
    try:
        from database import get_conn
        conn = get_conn(); c = conn.cursor()
        c.execute("UPDATE airdrops SET claimed=1 WHERE msg_id=?", (msg_id,))
        conn.commit(); conn.close()
    except: pass

def _airdrop_db_unclaim(msg_id):
    try:
        from database import get_conn
        conn = get_conn(); c = conn.cursor()
        c.execute("UPDATE airdrops SET claimed=0 WHERE msg_id=?", (msg_id,))
        conn.commit(); conn.close()
    except: pass

@dp.message(Command("airdrop"))
async def airdrop_cmd(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.split()[1:]
    forced = args[0].lower() if args else None
    await launch_airdrop(forced)


@dp.message(Command("airdropgem"))
async def airdropstars_cmd(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.split()[1:]
    amount = 25
    if args:
        try: amount = int(args[0])
        except: pass
    await launch_gems_airdrop(amount)


_gems_airdrop_active = {}  # msg_id -> {amount, claimed}

def _log_airdrop(user_id: int, username: str, airdrop_type: str, amount: int = 0, item_type: str = None):
    """Логируем аирдроп (gems или item)"""
    conn = db(); c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS airdrop_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, username TEXT,
        airdrop_type TEXT, amount INTEGER DEFAULT 0, item_type TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("INSERT INTO airdrop_log (user_id, username, airdrop_type, amount, item_type) VALUES (?,?,?,?,?)",
              (user_id, username, airdrop_type, amount, item_type))
    conn.commit(); conn.close()

async def launch_gems_airdrop(amount: int = 25):
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"💎 Забрать {amount} Гемов", callback_data=f"gems_airdrop:{amount}")
    ]])
    msg = await bot.send_message(
        CHAT_ID,
        f"💎 <b>Аирдроп Гемов!</b>\n\n{amount} 💎 упали в чат\nПервый кто нажмёт — получит на баланс!",
        parse_mode="HTML",
        reply_markup=kb
    )
    _gems_airdrop_active[msg.message_id] = {"amount": amount, "claimed": False}


@dp.callback_query(F.data.startswith("gems_airdrop:"))
async def gems_airdrop_callback(callback: CallbackQuery):
    parts = callback.data.split(":")
    amount = int(parts[1])
    msg_id = callback.message.message_id
    user = callback.from_user

    state = _gems_airdrop_active.get(msg_id)
    if state is None or state["claimed"]:
        await callback.answer("⏰ Опоздал! Уже разобрали", show_alert=True)
        return

    state["claimed"] = True

    from database import get_or_create_user, add_gems
    try:
        get_or_create_user(user.id, user.username, user.first_name)
        add_gems(user.id, amount)
    except Exception as e:
        state["claimed"] = False
        await callback.answer(f"Ошибка: {e}", show_alert=True)
        return

    uname = user.first_name or f"@{user.username}"
    _log_airdrop(user.id, user.username or "", "gems", amount)
    await callback.message.edit_text(
        f"💎 <b>Аирдроп Гемов!</b>\n\n{amount} 💎 упали в чат\nПервый кто нажмёт — получит на баланс!\n\n✅ {uname} подобрал!",
        parse_mode="HTML"
    )
    await callback.answer(f"🔥 {amount} Гемов на балансе!", show_alert=True)

async def launch_airdrop(forced_type: str = None):
    if forced_type and forced_type != "random":
        match = [(t, n, i) for t, n, i in AIRDROP_ITEMS if t == forced_type]
        if not match:
            await bot.send_message(ADMIN_ID,
                f"❌ Неизвестная связь: {forced_type}\nДоступные:\n" +
                "random\n" + "\n".join(f"{t} — {n}" for t,n,_ in AIRDROP_ITEMS))
            return
        item_type, item_name, item_icon = match[0]
    else:
        item_type, item_name, item_icon = _random.choice(AIRDROP_ITEMS)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"👜 Забрать", callback_data=f"airdrop:{item_type}")
    ]])
    msg = await bot.send_message(
        CHAT_ID,
        f"👜 Аирдроп {item_name} в чат\nПервый кто подберёт — получит в инвентарь!",
        parse_mode="HTML",
        reply_markup=kb
    )
    _airdrop_active[msg.message_id] = {"item_type": item_type, "item_name": item_name, "claimed": False}
    _airdrop_db_set(msg.message_id, item_type, item_name)


@dp.callback_query(F.data.startswith("airdrop:"))
async def airdrop_callback(callback: CallbackQuery):
    item_type = callback.data.split(":")[1]
    msg_id = callback.message.message_id
    user = callback.from_user

    airdrop = _airdrop_active.get(msg_id) or _airdrop_db_get(msg_id)
    if not airdrop or airdrop["claimed"]:
        await callback.answer("⏰ Опоздал! Уже разобрали", show_alert=True)
        return

    if airdrop["item_type"] != item_type:
        await callback.answer("Ошибка", show_alert=True)
        return

    # Помечаем как забранный
    airdrop["claimed"] = True
    _airdrop_db_claim(msg_id)

    # Выдаём связь
    import httpx as _hx
    try:
        from database import get_or_create_user, add_item, get_active_game
        get_or_create_user(user.id, user.username, user.first_name)
        game = get_active_game()
        game_id = game["id"] if game else None
        add_item(user.id, item_type, game_id)
    except Exception as e:
        airdrop["claimed"] = False
        _airdrop_db_unclaim(msg_id)
        await callback.answer(f"Ошибка: {e}", show_alert=True)
        return

    uname = user.first_name or f"@{user.username}"
    item_name = airdrop["item_name"]

    # Убираем кнопку, дописываем кто поймал — оригинал остаётся
    original = callback.message.text or callback.message.caption or ""
    _log_airdrop(user.id, user.username or "", "item", 1, item_type)
    await callback.message.edit_text(
        f"👜 Аирдроп {item_name} в чат\nПервый кто подберёт — получит в инвентарь!\n\n✅ {uname} подобрал!",
        parse_mode="HTML"
    )
    await callback.answer(f"🔥 Твоя! {item_name} в инвентаре", show_alert=True)





















@dp.callback_query(F.data == "bear_airdrop")
async def bear_airdrop_callback(callback: CallbackQuery):
    global _bear_claimed
    user = callback.from_user
    if _bear_claimed:
        await callback.answer("⏰ Опоздал! Уже разобрали", show_alert=True)
        return
    _bear_claimed = True
    uname = f"@{user.username}" if user.username else user.first_name
    text = f"🪂 <b>АИРДРОП GIFT!</b>\n\n🐻 В чат упал подарок <b>Мишка</b>\nПервый кто нажмёт кнопку — получит его!\n\n✅ Схватил {uname}!"
    await callback.answer("🎉 Поймал! Мишка летит к тебе в личку.", show_alert=True)
    await callback.message.edit_text(text, parse_mode="HTML")
    try:
        await bot.send_message(
            ADMIN_ID,
            f"🐻 <b>Аирдроп Мишка поймал:</b>\n{uname} (ID: <code>{user.id}</code>) — отправь подарок!",
            parse_mode="HTML"
        )
    except:
        pass


# ══════════════════════════════════════════════════════════
#  КРАШ — /crash
# ══════════════════════════════════════════════════════════

import random as _random_crash
import math as _math_crash

_crash_games = {}  # msg_id -> game state

def _crash_generate(rtp=0.90):
    h = _random_crash.random()
    point = rtp / (1.0 - h)
    return max(1.01, min(round(point, 2), 50.0))

def _crash_mult(tick):
    return round(1.00 * (1.15 ** tick), 2)

async def _crash_loop(chat_id, msg_id, bet, crash_point, uname):
    tick = 0
    while True:
        await asyncio.sleep(3)
        tick += 1
        game = _crash_games.get(msg_id)
        if not game or game.get("ended"):
            break
        mult = _crash_mult(tick)
        game["current_mult"] = mult
        if mult >= crash_point:
            game["ended"] = True
            try:
                from database import log_game as _lg
                _lg(game["user_id"], "crash", bet, "lose", 0)
            except: pass
            for attempt in range(3):
                try:
                    await bot.edit_message_text(
                        f"✈️ <b>@{uname}</b> — ставка {bet} 💎\n\n💥 <b>КРАШ на x{crash_point:.2f}!</b>\n❌ Проиграл {bet} 💎",
                        chat_id=chat_id, message_id=msg_id, parse_mode="HTML"
                    )
                    break
                except Exception as e:
                    if "retry" in str(e).lower():
                        await asyncio.sleep(5)
                    else:
                        break
            break
        else:
            earn = int(bet * mult)
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=f"💸 Забрать x{mult} = {earn} 💎", callback_data=f"crash_out:{msg_id}")
            ]])
            try:
                await bot.edit_message_text(
                    f"✈️ <b>@{uname}</b> — ставка {bet} 💎\n\n📈 Множитель: <b>x{mult}</b>\n💰 Заберёшь: {earn} 💎",
                    chat_id=chat_id, message_id=msg_id, parse_mode="HTML", reply_markup=kb
                )
            except: pass


@dp.message(Command("crash"))
async def crash_cmd(message: Message):
    user = message.from_user
    args = message.text.split()[1:]
    if not args:
        bet = 10
    else:
        try:
            bet = int(args[0])
        except:
            await message.reply("❌ Ставка должна быть числом")
            return
    if bet < 10:
        await message.reply("❌ Минимальная ставка 10 💎")
        return

    from database import spend_gems, get_or_create_user
    get_or_create_user(user.id, user.username, user.first_name)
    if not spend_gems(user.id, bet):
        await message.reply("❌ Недостаточно Гемов. /bank чтобы пополнить")
        return

    crash_point = _crash_generate()
    uname = user.username or user.first_name

    msg = await message.answer(
        f"✈️ <b>@{uname}</b> — ставка {bet} 💎\n\n📈 Множитель: <b>x1.00</b>\n💰 Заберёшь: {bet} 💎",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"💸 Забрать x1.00 = {bet} 💎", callback_data=f"crash_out:{0}")
        ]])
    )
    mid = msg.message_id
    _crash_games[mid] = {
        "user_id": user.id, "username": uname, "bet": bet,
        "crash_point": crash_point, "current_mult": 1.00,
        "cashed_out": False, "ended": False, "chat_id": message.chat.id,
    }
    await bot.edit_message_reply_markup(
        chat_id=message.chat.id, message_id=mid,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"💸 Забрать x1.00 = {bet} 💎", callback_data=f"crash_out:{mid}")
        ]])
    )
    asyncio.create_task(_crash_loop(message.chat.id, mid, bet, crash_point, uname))


@dp.callback_query(F.data.startswith("crash_out:"))
async def crash_cashout(callback: CallbackQuery):
    msg_id = int(callback.data.split(":")[1])
    user = callback.from_user
    game = _crash_games.get(msg_id)
    if not game:
        await callback.answer("Игра не найдена", show_alert=True)
        return
    if game["user_id"] != user.id:
        await callback.answer("❌ Это не твоя игра!", show_alert=True)
        return
    if game.get("ended") or game.get("cashed_out"):
        await callback.answer("💥 Уже завершено!", show_alert=True)
        return
    game["cashed_out"] = True
    game["ended"] = True
    mult = game["current_mult"]
    bet = game["bet"]
    winnings = int(bet * mult)
    from database import add_gems, log_game
    add_gems(user.id, winnings)
    log_game(user.id, "crash", bet, "win", winnings)
    profit = winnings - bet
    try:
        await callback.message.edit_text(
            f"✈️ <b>@{game['username']}</b> — ставка {bet} 💎\n\n✅ Забрал на <b>x{mult}</b>!\n💰 +{winnings} 💎 (+{profit} профит)",
            parse_mode="HTML"
        )
    except: pass
    await callback.answer(f"✅ +{winnings} Гемов на балансе!", show_alert=True)


# ══════════════════════════════════════════════════════════
#  ГЕМЫ — /bank /buy /redblack + вывод
# ══════════════════════════════════════════════════════════


@dp.message(Command("stars"))
async def cmd_stars(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⭐ Купить Звёзды", url="https://split.tg/?ref=UQDngkmwbJxausCBgrbXcS_LmQYtGLG0-qfsaCYijyczQVap")
    ]])
    await message.answer(
        "⭐ <b>Купить Звёзды Telegram</b>\n\nЗвёзды нужны для покупки предметов, участия в аукционе и пополнения Гемов.",
        parse_mode="HTML",
        reply_markup=kb
    )


@dp.message(Command("bank"))
async def cmd_bank(message: Message):
    from database import get_gems
    uid = message.from_user.id
    gems = get_gems(uid)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💎 Пополнить", callback_data="gems:buy_menu"),
            InlineKeyboardButton(text="💸 Вывести", callback_data=f"gems:wd_menu:{gems}"),
        ]
    ])
    await message.answer(
        f"💎 <b>Твой баланс:</b> {gems} Гемов\n\n"
        f"Минимум для вывода: 300 Гемов\n"
        f"Комиссия при выводе: 5%",
        parse_mode="HTML",
        reply_markup=kb
    )


@dp.callback_query(F.data == "gems:buy_menu")
async def gems_buy_menu(call: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="10 💎", callback_data="gems:buy:10"),
            InlineKeyboardButton(text="25 💎", callback_data="gems:buy:25"),
            InlineKeyboardButton(text="100 💎", callback_data="gems:buy:100"),
        ],
        [
            InlineKeyboardButton(text="✏️ Другая сумма", callback_data="gems:buy_custom"),
        ],
    ])
    await call.answer()
    await call.message.answer(
        "💎 <b>Выбери сколько Гемов купить:</b>\n\n1 Гем = 1 ⭐ Telegram Star",
        parse_mode="HTML",
        reply_markup=kb
    )


@dp.callback_query(F.data == "gems:buy_custom")
async def gems_buy_custom(call: CallbackQuery):
    await call.answer()
    await call.message.answer(
        "✏️ Напиши команду с нужной суммой:\n<code>/buy 500</code>",
        parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("gems:buy:"))
async def gems_buy_invoice(call: CallbackQuery):
    amount = int(call.data.split(":")[2])
    uid = call.from_user.id
    await call.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"💎 Купить {amount} Гемов за {amount} ⭐", pay=True)
    ]])
    await bot.send_invoice(
        chat_id=call.message.chat.id,
        title="Покупка Гемов",
        description=f"{amount} Гемов для игр в чате Разборки на районе",
        payload=f"gems:{uid}:{amount}",
        currency="XTR",
        prices=[{"label": "Гемы", "amount": amount}],
        reply_markup=kb,
    )


@dp.message(Command("buy"))
async def cmd_buy(message: Message):
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        # Показываем меню кнопок
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="10 💎", callback_data="gems:buy:10"),
                InlineKeyboardButton(text="25 💎", callback_data="gems:buy:25"),
                InlineKeyboardButton(text="100 💎", callback_data="gems:buy:100"),
            ],
            [
                InlineKeyboardButton(text="✏️ Другая сумма", callback_data="gems:buy_custom"),
            ],
        ])
        await message.answer(
            "💎 <b>Выбери сколько Гемов купить:</b>\n\n1 Гем = 1 ⭐ Telegram Star",
            parse_mode="HTML",
            reply_markup=kb
        )
        return

    amount = int(args[1])
    if amount < 1:
        await message.answer("❌ Минимум 1 Гем")
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"💎 Купить {amount} Гемов за {amount} ⭐", pay=True)
    ]])
    await bot.send_invoice(
        chat_id=message.chat.id,
        title="Покупка Гемов",
        description=f"{amount} Гемов для игр в чате Разборки на районе",
        payload=f"gems:{message.from_user.id}:{amount}",
        currency="XTR",
        prices=[{"label": "Гемы", "amount": amount}],
        reply_markup=kb,
    )


@dp.callback_query(F.data.startswith("gems:wd_menu:"))
async def gems_wd_menu(call: CallbackQuery):
    from database import get_gems
    uid = call.from_user.id
    gems = get_gems(uid)
    await call.answer()

    if gems < 300:
        await call.message.answer(f"❌ Минимум для вывода 300 Гемов.\nТвой баланс: {gems} Гемов.")
        return

    # Кнопки: фиксированные суммы + весь баланс (только те что <= баланса)
    options = [300, 500, 1000]
    buttons = []
    row = []
    for opt in options:
        if opt <= gems:
            row.append(InlineKeyboardButton(text=f"{opt} 💎", callback_data=f"gems:wd:{opt}"))
            if len(row) == 3:
                buttons.append(row)
                row = []
    if row:
        buttons.append(row)
    if gems not in options and gems >= 50:
        buttons.append([InlineKeyboardButton(text=f"Всё ({gems} 💎)", callback_data=f"gems:wd:{gems}")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await call.message.answer(
        f"💸 <b>Сколько Гемов вывести?</b>\n\n"
        f"Твой баланс: {gems} 💎\n"
        f"Комиссия: 5%",
        parse_mode="HTML",
        reply_markup=kb
    )


@dp.callback_query(F.data.startswith("gems:wd:"))
async def gems_wd_confirm(call: CallbackQuery):
    from database import get_gems, spend_gems, create_withdraw_request
    uid = call.from_user.id
    amount = int(call.data.split(":")[2])
    gems = get_gems(uid)

    if gems < amount:
        await call.answer(f"❌ Недостаточно Гемов! Баланс: {gems}", show_alert=True)
        return
    if amount < 100:
        await call.answer("❌ Минимум 100 Гемов", show_alert=True)
        return

    spend_gems(uid, amount)
    # Банкир — 0% комиссии
    from database import get_user_authority as _gua
    is_banker = _gua(uid) == 'banker'
    stars_out = amount if is_banker else int(amount * 0.95)
    commission_text = "комиссия 0% — ты Банкир 🏦" if is_banker else "после 5% комиссии"

    uname = call.from_user.username or ""
    fname = call.from_user.first_name or ""
    user_msg = await call.message.answer(
        f"✅ Запрос на вывод {amount} Гемов отправлен.\n"
        f"Получишь: {stars_out} ⭐ ({commission_text})\n\n"
        f"Ожидай подтверждения."
    )
    wid = create_withdraw_request(uid, uname, fname, amount, user_msg.message_id)

    name_str = f"@{uname}" if uname else fname
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Оплатить", callback_data=f"wd:pay:{wid}:{uid}"),
        InlineKeyboardButton(text="❌ Отменить", callback_data=f"wd:cancel:{wid}:{uid}"),
    ]])
    await bot.send_message(
        ADMIN_ID,
        f"💸 <b>Запрос на вывод</b>\n\n"
        f"👤 {name_str} (ID: {uid})\n"
        f"💎 {amount} Гемов → {stars_out} ⭐",
        parse_mode="HTML",
        reply_markup=kb
    )
    await call.answer()


@dp.callback_query(F.data.startswith("wd:"))
async def withdraw_action(call: CallbackQuery):
    from database import get_withdraw_request, set_withdraw_status, add_gems
    if call.from_user.id != ADMIN_ID:
        await call.answer("Нет доступа", show_alert=True)
        return

    parts = call.data.split(":")
    action = parts[1]
    wid = int(parts[2])
    uid = int(parts[3])

    req = get_withdraw_request(wid)
    if not req or req["status"] != "pending":
        await call.answer("Запрос уже обработан", show_alert=True)
        return

    if action == "pay":
        set_withdraw_status(wid, "paid")
        await call.message.edit_reply_markup(reply_markup=None)
        await call.message.edit_text(
            call.message.text + "\n\n✅ <b>Оплачено</b>",
            parse_mode="HTML"
        )
        if req.get("user_msg_id"):
            try:
                await bot.edit_message_text(
                    f"✅ Запрос на вывод {req['gems']} Гемов отправлен.\n"
                    f"Получишь: {req['stars']} ⭐ (после 5% комиссии)\n\n"
                    f"Ожидай подтверждения.\n\n"
                    f"✅ <b>Оплачено!</b>",
                    chat_id=uid,
                    message_id=req["user_msg_id"],
                    parse_mode="HTML"
                )
            except Exception:
                await bot.send_message(uid, f"✅ Твой запрос на {req['gems']} Гемов обработан!\nСтарс уже отправлены.")
        else:
            await bot.send_message(uid, f"✅ Твой запрос на {req['gems']} Гемов обработан!\nСтарс уже отправлены.")
        await call.answer("Оплачено!")

    elif action == "cancel":
        set_withdraw_status(wid, "cancelled")
        add_gems(uid, req["gems"])
        await call.message.edit_reply_markup(reply_markup=None)
        await call.message.edit_text(
            call.message.text + "\n\n❌ <b>Отменено</b>",
            parse_mode="HTML"
        )
        await bot.send_message(uid, f"❌ Запрос на вывод {req['gems']} Гемов отменён. Гемы возвращены на баланс.")
        await call.answer("Отменено")

# ── NOTIFY UNREGISTERED ──────────────────────────────────

@dp.message(Command("push"))
async def cmd_notify_unreg(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    conn = db()
    c = conn.cursor()

    # Берём активную игру с наибольшим числом игроков (Стрелка #6 = id 179)
    game = c.execute(
        "SELECT id, number FROM games WHERE status='waiting' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not game:
        conn.close()
        await message.answer("❌ Нет активной игры")
        return

    game_id = game["id"]
    game_num = game["number"]

    # Все юзеры кто НЕ зарегистрирован в эту игру
    rows = c.execute("""
        SELECT u.user_id FROM users u
        WHERE u.is_banned = 0
          AND u.user_id NOT IN (
              SELECT p.user_id FROM players p WHERE p.game_id = ?
          )
    """, (game_id,)).fetchall()
    conn.close()

    user_ids = [r["user_id"] for r in rows]
    total = len(user_ids)

    status_msg = await message.answer(
        f"📤 Отправляю {total} юзерам (не в Стрелка #{game_num})...",
        parse_mode="HTML"
    )

    text = "Ты еще не зареган в новой игре. Заходи, скоро начнём!"
    kb = None

    sent = 0
    failed = 0
    for uid in user_ids:
        try:
            await bot.send_message(uid, text, reply_markup=kb)
            sent += 1
        except:
            failed += 1
        await asyncio.sleep(0.05)  # не флудим в Telegram API

    await status_msg.edit_text(
        f"✅ Готово!\n\n"
        f"📨 Отправлено: {sent}\n"
        f"❌ Не доставлено (заблокировали бота): {failed}"
    )


# ── ADDGEM ───────────────────────────────────────────────

@dp.message(Command("addgem"))
async def cmd_addgem(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.split()
    if len(args) < 3:
        await message.answer("❌ Использование: <code>/addgem @username 50</code>", parse_mode="HTML")
        return
    username = args[1].lstrip("@")
    try:
        amount = int(args[2])
    except:
        await message.answer("❌ Сумма должна быть числом")
        return
    conn = db(); c = conn.cursor()
    row = c.execute("SELECT user_id, first_name FROM users WHERE username=?", (username,)).fetchone()
    if not row:
        conn.close()
        await message.answer(f"❌ Юзер @{username} не найден в БД")
        return
    c.execute("UPDATE users SET gems=COALESCE(gems,0)+? WHERE user_id=?", (amount, row["user_id"]))
    # Логируем выдачу
    c.execute("""CREATE TABLE IF NOT EXISTS admin_gems_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, username TEXT, amount INTEGER,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("INSERT INTO admin_gems_log (user_id, username, amount) VALUES (?,?,?)",
              (row["user_id"], username, amount))
    conn.commit()
    new_gems = c.execute("SELECT gems FROM users WHERE user_id=?", (row["user_id"],)).fetchone()["gems"]
    conn.close()
    try:
        await bot.send_message(
            row["user_id"],
            f"💎 <b>+{amount} Гемов!</b>\n\nТебе начислили гемы. Баланс: <b>{new_gems} 💎</b>",
            parse_mode="HTML"
        )
    except: pass
    await message.answer(
        f"✅ <b>@{username}</b> ({row['first_name']}) — начислено <b>{amount} 💎</b>\n"
        f"Баланс: {new_gems} 💎",
        parse_mode="HTML"
    )


# ── CASINOSTATS ──────────────────────────────────────────

@dp.message(Command("casinostats"))
async def cmd_casinostats(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    conn = db(); c = conn.cursor()
    today = "DATE('now')"

    # Создаём таблицы если нет
    c.execute("CREATE TABLE IF NOT EXISTS game_log (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, game_type TEXT, bet INTEGER, result TEXT, payout INTEGER, profit INTEGER, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)")
    c.execute("CREATE TABLE IF NOT EXISTS admin_gems_log (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, username TEXT, amount INTEGER, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)")

    # Гемы куплены за всё время (через Stars)
    bought = c.execute("""
        SELECT COALESCE(SUM(gems),0) as total, COUNT(*) as cnt
        FROM gem_purchases
    """).fetchone() if c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='gem_purchases'").fetchone() else None
    # Fallback: из колонки users если таблицы нет
    if not bought or bought["total"] == 0:
        fb = c.execute("SELECT COALESCE(SUM(gems_bought_total),0) as total, COUNT(*) as cnt FROM users WHERE gems_bought_total > 0").fetchone()
        bought_today = fb["total"]
        bought_cnt = fb["cnt"]
    else:
        bought_today = bought["total"]
        bought_cnt = bought["cnt"]

    # Выдано вручную за всё время (addgem)
    given = c.execute("""
        SELECT COALESCE(SUM(amount),0) as total, COUNT(*) as cnt
        FROM admin_gems_log
    """).fetchone()

    # Продано за Stars за всё время (выводы)
    sold = c.execute("""
        SELECT COALESCE(SUM(gems),0) as gems, COALESCE(SUM(stars),0) as stars, COUNT(*) as cnt
        FROM gem_withdrawals WHERE status='paid'
    """).fetchone() if c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='gem_withdrawals'").fetchone() else None
    sold_gems = sold["gems"] if sold else 0
    sold_stars = sold["stars"] if sold else 0
    sold_cnt = sold["cnt"] if sold else 0

    # Игры за всё время — общее
    total = c.execute("""
        SELECT COUNT(*) as cnt,
               COALESCE(SUM(bet),0) as bets,
               COALESCE(SUM(profit),0) as profit
        FROM game_log
    """).fetchone()

    # По играм за всё время
    by_game = c.execute("""
        SELECT game_type,
               COUNT(*) as games,
               COALESCE(SUM(profit),0) as profit
        FROM game_log
        GROUP BY game_type
    """).fetchall()

    conn.close()

    names = {"redblack": "🃏 Red&Black", "crash": "✈️ Краш", "dice_solo": "🎲 Кубик соло", "dice_pvp": "🎲 Кубик PvP"}
    games_text = ""
    for row in by_game:
        name = names.get(row["game_type"], row["game_type"])
        games_text += f"  {name}: {row['games']} игр | казино +{row['profit']} 💎\n"

    games_block = games_text if games_text else "  Игр ещё не было\n"

    await message.answer(
        f"🎰 <b>Статистика казино (всё время)</b>\n\n"
        f"💎 <b>Куплено Гемов:</b> {bought_today} 💎 ({bought_cnt} чел.)\n"
        f"🎁 <b>Выдано вручную:</b> {given['total']} 💎 ({given['cnt']} раз)\n"
        f"💸 <b>Продано за ⭐:</b> {sold_gems} 💎 → {sold_stars} ⭐ ({sold_cnt} выводов)\n\n"
        f"🎮 <b>Игр сыграно:</b> {total['cnt']}\n"
        f"{games_block}\n"
        f"🏦 <b>Казино заработало:</b> {total['profit']} 💎",
        parse_mode="HTML"
    )


# ── GEMSTATS ─────────────────────────────────────────────

@dp.message(Command("gemstats"))
async def cmd_gemstats(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    conn = db()
    c = conn.cursor()
    # Куплено за звёзды
    row = c.execute("SELECT COALESCE(SUM(gems_bought_total),0) as total, COUNT(*) as buyers FROM users WHERE gems_bought_total > 0").fetchone()
    bought_gems = row["total"]
    buyers_count = row["buyers"]
    # Продано (выведено) за звёзды — только оплаченные
    row2 = c.execute("SELECT COALESCE(SUM(gems),0) as total_gems, COALESCE(SUM(stars),0) as total_stars, COUNT(*) as cnt FROM gem_withdrawals WHERE status='paid'").fetchone()
    sold_gems = row2["total_gems"]
    sold_stars = row2["total_stars"]
    # Всего гемов в обращении
    row3 = c.execute("SELECT COALESCE(SUM(gems),0) as total FROM users").fetchone()
    total_gems = row3["total"]
    conn.close()

    await message.answer(
        f"💎 <b>Статистика Гемов</b>\n\n"
        f"🛒 <b>Куплено за ⭐:</b> {bought_gems} Гемов\n"
        f"   └ покупателей: {buyers_count}\n\n"
        f"💸 <b>Продано за ⭐:</b> {sold_gems} Гемов\n"
        f"   └ выплачено: {sold_stars} ⭐\n\n"
        f"👛 <b>На руках у юзеров:</b> {total_gems} Гемов",
        parse_mode="HTML"
    )


# ── RED & BLACK ──────────────────────────────────────────

@dp.message(Command("redblack"))
async def cmd_redblack(message: Message):
    from database import get_gems
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("❌ Укажи ставку. Пример: <code>/redblack 50</code>", parse_mode="HTML")
        return
    bet = int(args[1])
    if bet < 10:
        await message.answer("❌ Минимальная ставка 10 Гемов")
        return
    uid = message.from_user.id
    gems = get_gems(uid)
    if gems < bet:
        await message.answer(f"❌ Недостаточно Гемов. Твой баланс: {gems} 💎")
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔴 Red", callback_data=f"rb:red:{bet}"),
        InlineKeyboardButton(text="⚫ Black", callback_data=f"rb:black:{bet}"),
    ]])
    await message.answer(
        f"🎰 <b>Red & Black</b>\n\n"
        f"Ставка: {bet} 💎\n"
        f"🔴 Red                    ⚫ Black\n\n"
        f"Выбирай:",
        parse_mode="HTML",
        reply_markup=kb
    )


@dp.callback_query(F.data.startswith("rb:"))
async def redblack_result(call: CallbackQuery):
    import random
    from database import get_gems, spend_gems, add_gems
    parts = call.data.split(":")
    choice = parts[1]  # red / black
    bet = int(parts[2])
    uid = call.from_user.id

    # Проверяем баланс ещё раз
    gems = get_gems(uid)
    if gems < bet:
        await call.answer("❌ Недостаточно Гемов!", show_alert=True)
        return

    # Снимаем ставку
    if not spend_gems(uid, bet):
        await call.answer("❌ Недостаточно Гемов!", show_alert=True)
        return

    # Рандом из 100: 0-44 = red (45%), 45-89 = black (45%), 90-99 = zero (10%) → RTP 90%
    roll = random.randint(0, 99)
    if roll <= 44:
        result = "red"
        result_emoji = "🔴 Red"
    elif roll <= 89:
        result = "black"
        result_emoji = "⚫ Black"
    else:
        result = "zero"
        result_emoji = "🟢 Zero"

    choice_emoji = "🔴 Red" if choice == "red" else "⚫ Black"

    from database import log_game
    if result == "zero":
        new_gems = get_gems(uid)
        log_game(uid, "redblack", bet, "zero", 0)
        text = (
            f"🟢 <b>ZERO!</b> Казино забирает всё!\n\n"
            f"Твой выбор: {choice_emoji}\n"
            f"Результат: {result_emoji}\n"
            f"Проиграл: -{bet} 💎"
        )
    elif result == choice:
        winnings = bet * 2
        add_gems(uid, winnings)
        new_gems = get_gems(uid)
        log_game(uid, "redblack", bet, "win", winnings)
        text = (
            f"🎉 <b>Победа!</b>\n\n"
            f"Твой выбор: {choice_emoji}\n"
            f"Результат: {result_emoji}\n"
            f"Выиграл: +{bet} 💎"
        )
    else:
        new_gems = get_gems(uid)
        log_game(uid, "redblack", bet, "lose", 0)
        text = (
            f"😔 <b>Проигрыш</b>\n\n"
            f"Твой выбор: {choice_emoji}\n"
            f"Результат: {result_emoji}\n"
            f"Проиграл: -{bet} 💎"
        )

    await call.message.edit_text(text, parse_mode="HTML")
    await call.answer()
    asyncio.create_task(_check_jackpot_overtake())


# ── КУБИК /dice ──────────────────────────────────────────

_dice_games = {}  # duel_id -> state

async def _dice_accept_timer(duel_id: str, challenger_id: int, bet: int, msg_id: int, chat_id: int):
    await asyncio.sleep(15)
    from database import add_gems
    game = _dice_games.get(duel_id)
    if not game or game["status"] != "pending":
        return
    game["status"] = "expired"
    add_gems(challenger_id, bet)
    try:
        await bot.edit_message_text(
            f"⏱ Время вышло! {game['opponent_name']} не принял вызов.\n💎 Ставка возвращена {game['challenger_name']}.",
            chat_id=chat_id, message_id=msg_id, parse_mode="HTML"
        )
    except: pass


async def _dice_solo(message, bet: int):
    """Соло кубик против казино — RTP 90%, комиссия 10%, ничья = переигровка"""
    from database import spend_gems, add_gems, get_gems, get_or_create_user
    get_or_create_user(message.from_user.id, message.from_user.username, message.from_user.first_name)

    if not spend_gems(message.from_user.id, bet):
        await message.reply(f"❌ Недостаточно Гемов! Нужно {bet} 💎")
        return

    uname = message.from_user.first_name or f"@{message.from_user.username}"
    winnings = int(bet * 2 * 0.90)  # 10% комиссия вшита в выигрыш

    round_num = 1
    while True:
        prefix = f"🔄 Раунд {round_num}\n" if round_num > 1 else ""
        await message.answer(f"{prefix}🎲 <b>{uname}</b> vs 🏦 Казино...\n💎 Ставка: {bet}", parse_mode="HTML")

        msg_p = await bot.send_dice(message.chat.id)
        await asyncio.sleep(1)
        msg_b = await bot.send_dice(message.chat.id)
        await asyncio.sleep(3)

        p = msg_p.dice.value
        b = msg_b.dice.value

        if p > b:
            from database import log_game
            add_gems(message.from_user.id, winnings)
            log_game(message.from_user.id, "dice_solo", bet, "win", winnings)
            asyncio.create_task(_check_jackpot_overtake())
            await message.answer(
                f"🎲 <b>{uname}</b>: {p}  vs  🏦 Казино: {b}\n\n"
                f"🏆 <b>Победа!</b>\n"
                f"💎 +{winnings} Гемов",
                parse_mode="HTML"
            )
            break
        elif p < b:
            from database import log_game
            log_game(message.from_user.id, "dice_solo", bet, "lose", 0)
            asyncio.create_task(_check_jackpot_overtake())
            await message.answer(
                f"🎲 <b>{uname}</b>: {p}  vs  🏦 Казино: {b}\n\n"
                f"😔 <b>Казино выиграло!</b>\n"
                f"💎 -{bet} Гемов",
                parse_mode="HTML"
            )
            break
        else:
            await message.answer(
                f"🎲 <b>{uname}</b>: {p}  vs  🏦 Казино: {b}\n\n"
                f"🤝 <b>Ничья!</b> Переигровка...",
                parse_mode="HTML"
            )
            round_num += 1
            await asyncio.sleep(1)


@dp.message(Command("dice"))
async def cmd_dice(message: Message):
    from database import get_or_create_user, spend_gems
    args = message.text.split()
    bet = 10
    if len(args) > 1:
        try: bet = int(args[1])
        except: pass
    if bet < 10:
        await message.reply("❌ Минимальная ставка 10 💎")
        return

    # Соло режим — без реплая
    if not message.reply_to_message:
        await _dice_solo(message, bet)
        return

    # PvP режим — реплай на сообщение игрока
    opponent = message.reply_to_message.from_user
    challenger = message.from_user

    if opponent.id == challenger.id:
        await message.reply("❌ Нельзя играть с собой")
        return
    if opponent.is_bot:
        await message.reply("❌ Нельзя играть с ботом")
        return

    get_or_create_user(challenger.id, challenger.username, challenger.first_name)
    if not spend_gems(challenger.id, bet):
        await message.reply("❌ Недостаточно Гемов!")
        return

    cname = challenger.first_name or f"@{challenger.username}"
    oname = opponent.first_name or f"@{opponent.username}"

    duel_id = f"dice_{challenger.id}_{opponent.id}_{message.message_id}"
    _dice_games[duel_id] = {
        "challenger_id": challenger.id, "challenger_name": cname,
        "opponent_id": opponent.id, "opponent_name": oname,
        "bet": bet, "chat_id": message.chat.id,
        "status": "pending"
    }

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎲 Принять!", callback_data=f"dice:accept:{duel_id}"),
        InlineKeyboardButton(text="❌ Отказать", callback_data=f"dice:decline:{duel_id}"),
    ]])
    sent = await message.answer(
        f"🎲 <b>{cname}</b> вызывает <b>{oname}</b> на кубик!\n\n"
        f"💎 Ставка: {bet} Гемов каждый\n"
        f"🏆 Банк: {bet * 2} Гемов (минус 5%)\n\n"
        f"{oname}, принимаешь? (15 сек ⏱)",
        parse_mode="HTML", reply_markup=kb
    )
    asyncio.ensure_future(_dice_accept_timer(duel_id, challenger.id, bet, sent.message_id, sent.chat.id))


@dp.callback_query(F.data.startswith("dice:accept:"))
async def dice_accept(call: CallbackQuery):
    from database import get_gems, spend_gems, add_gems
    duel_id = call.data.split("dice:accept:")[1]
    game = _dice_games.get(duel_id)

    if not game:
        await call.answer("Вызов не найден", show_alert=True); return
    if game["status"] != "pending":
        await call.answer("Вызов уже обработан", show_alert=True); return
    if call.from_user.id != game["opponent_id"]:
        await call.answer("Это не твой вызов!", show_alert=True); return

    if not spend_gems(call.from_user.id, game["bet"]):
        from database import add_gems
        add_gems(game["challenger_id"], game["bet"])
        game["status"] = "declined"
        await call.answer("❌ Недостаточно Гемов!", show_alert=True)
        await call.message.edit_text(
            f"❌ У <b>{game['opponent_name']}</b> недостаточно Гемов для игры!\n"
            f"💎 Ставка возвращена <b>{game['challenger_name']}</b>.",
            parse_mode="HTML"
        )
        return

    game["status"] = "rolling"
    await call.message.edit_text(
        f"🎲 <b>{game['challenger_name']}</b> vs <b>{game['opponent_name']}</b>\n"
        f"💎 Банк: {game['bet'] * 2} Гемов\n\n"
        f"Кидаем кубики... 🎲",
        parse_mode="HTML"
    )
    await call.answer()

    await _dice_roll_round(call.message.chat.id, duel_id, call.message)


async def _dice_roll_round(chat_id: int, duel_id: str, orig_msg=None):
    from database import add_gems
    game = _dice_games.get(duel_id)
    if not game:
        return

    cname = game["challenger_name"]
    oname = game["opponent_name"]
    bet = game["bet"]

    MAX_DRAWS = 5
    draw_count = game.get("draw_count", 0)

    try:
        msg1 = await bot.send_dice(chat_id)
        await asyncio.sleep(3)
        msg2 = await bot.send_dice(chat_id)
        await asyncio.sleep(3)
    except Exception:
        # Если Telegram не ответил — возвращаем ставки обоим
        add_gems(game["challenger_id"], bet)
        add_gems(game["opponent_id"], bet)
        game["status"] = "finished"
        _dice_games.pop(duel_id, None)
        try:
            await bot.send_message(chat_id, "⚠️ Ошибка при броске кубиков — ставки возвращены.", parse_mode="HTML")
        except Exception:
            pass
        return

    r1 = msg1.dice.value
    r2 = msg2.dice.value

    result_text = f"🎲 <b>{cname}</b>: {r1}  vs  <b>{oname}</b>: {r2}\n\n"

    if r1 == r2:
        draw_count += 1
        game["draw_count"] = draw_count
        if draw_count >= MAX_DRAWS:
            # Слишком много ничьих — возвращаем ставки
            add_gems(game["challenger_id"], bet)
            add_gems(game["opponent_id"], bet)
            game["status"] = "finished"
            _dice_games.pop(duel_id, None)
            result_text += "🤝 <b>Снова ничья!</b> Слишком много ничьих — ставки возвращены."
            await bot.send_message(chat_id, result_text, parse_mode="HTML")
        else:
            result_text += "🤝 <b>Ничья!</b> Играем ещё раз...\n"
            await bot.send_message(chat_id, result_text, parse_mode="HTML")
            await asyncio.sleep(2)
            await _dice_roll_round(chat_id, duel_id)
    else:
        pot = bet * 2
        commission = max(1, int(pot * 0.05))
        winnings = pot - commission
        if r1 > r2:
            winner_id = game["challenger_id"]
            winner_name = cname
        else:
            winner_id = game["opponent_id"]
            winner_name = oname
        add_gems(winner_id, winnings)
        game["status"] = "finished"
        loser_id = game["opponent_id"] if r1 > r2 else game["challenger_id"]
        from database import log_game
        log_game(winner_id, "dice_pvp", bet, "win", winnings)
        log_game(loser_id, "dice_pvp", bet, "lose", 0)
        result_text += f"🏆 Победитель: <b>{winner_name}</b>\n💎 +{winnings} Гемов (5% комиссия)"
        await bot.send_message(chat_id, result_text, parse_mode="HTML")
        _dice_games.pop(duel_id, None)


@dp.callback_query(F.data.startswith("dice:decline:"))
async def dice_decline(call: CallbackQuery):
    from database import add_gems
    duel_id = call.data.split("dice:decline:")[1]
    game = _dice_games.get(duel_id)
    if not game or game["status"] != "pending":
        await call.answer("Вызов уже обработан", show_alert=True); return
    if call.from_user.id != game["opponent_id"]:
        await call.answer("Это не твой вызов!", show_alert=True); return

    game["status"] = "declined"
    add_gems(game["challenger_id"], game["bet"])
    await call.message.edit_text(
        f"❌ <b>{game['opponent_name']}</b> отказался от вызова.\n"
        f"💎 Ставка возвращена {game['challenger_name']}.",
        parse_mode="HTML"
    )
    await call.answer()


@dp.message(Command("help"))
async def cmd_help(message: Message):
    # Считаем сколько гемов раздано через аирдроп
    try:
        conn = db(); c = conn.cursor()
        c.execute("CREATE TABLE IF NOT EXISTS airdrop_log (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, username TEXT, airdrop_type TEXT, amount INTEGER DEFAULT 0, item_type TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)")
        r = c.execute("SELECT COALESCE(SUM(amount),0) as total FROM airdrop_log WHERE airdrop_type='gems'").fetchone()
        r2 = c.execute("SELECT COALESCE(COUNT(*),0) as total FROM airdrop_log WHERE airdrop_type='item'").fetchone()
        conn.close()
        total_airdrop = 3150 + (r["total"] if r else 0)
        total_items = 125 + (r2["total"] if r2 else 0)
    except: total_airdrop = 3150; total_items = 125

    await message.answer(
        "📋 <b>Команды Разборки на районе:</b>\n\n"
        "🎮 <b>Игра:</b>\n"
        "🗡 /rules — Правила игры\n"
        "👤 /me — Моя статистика\n"
        "🗳 /top — Топ 5 игроков\n"
        "🔗 /ref — Топ 5 рефоводов\n"
        "🌍 /game — Викторина\n"
        "⚔️ /duel @username 100 — Дуэль\n"
        "💀 /diss @username — Опустить чела за 1 ⭐\n\n"
        "🎰 <b>Казино:</b>\n"
        "💰 /bank — Баланс Гемов\n"
        "🛒 /buy — Купить Гемы за Stars\n"
        "🎰 /spin &lt;ставка&gt; — Слоты\n"
        "🃏 /redblack &lt;ставка&gt; — Red &amp; Black\n"
        "✈️ /crash &lt;ставка&gt; — Краш\n"
        "🎲 /dice &lt;ставка&gt; — Кубик vs казино\n"
        "🎲 /dice &lt;ставка&gt; (реплай) — Кубик vs игрок\n"
        "🎡 /roul &lt;ставка&gt; — Рулетка (x35)\n"
        "🏆 /jackpot — Недельный джекпот\n\n"
        "🖼 /nft — Текущие розыгрыши NFT\n\n"
        f"🎁 <b>Аирдроп гемов:</b> 3800 💎\n"
        f"🗡 <b>Роздано связей:</b> {total_items}\n\n"
        "<i>@shrimpgamesbot</i>",
        parse_mode="HTML"
    )


# ══════════════════════════════════════════════════════════
#  ДУЭЛЬ — /duel
# ══════════════════════════════════════════════════════════

DUEL_BET = 10  # минимальная ставка гемов

async def _duel_lock_chat():
    """Запретить всем писать в чат во время дуэли"""
    try:
        from aiogram.types import ChatPermissions
        await bot.set_chat_permissions(CHAT_ID, ChatPermissions(
            can_send_messages=False
        ))
    except: pass

async def _duel_unlock_chat():
    """Открыть чат после дуэли"""
    try:
        from aiogram.types import ChatPermissions
        await bot.set_chat_permissions(CHAT_ID, ChatPermissions(
            can_send_messages=True,
            can_send_media_messages=True,
            can_send_polls=True,
            can_send_other_messages=True,
            can_add_web_page_previews=True,
        ))
    except: pass

async def duel_accept_timer(duel_id: int, challenger_id: int, msg_id: int = None, chat_id_int: int = None):
    """Через 15 секунд отменяем дуэль если не принята"""
    await asyncio.sleep(15)
    from database import get_duel, update_duel, add_gems
    duel = get_duel(duel_id)
    if not duel or duel["status"] != "pending":
        return
    # Возвращаем ставку вызывающему
    add_gems(challenger_id, duel["bet"])
    update_duel(duel_id, status="expired")
    try:
        # Удаляем сообщение с вызовом
        if msg_id and chat_id_int:
            await bot.delete_message(chat_id_int, msg_id)
    except:
        pass
    try:
        cid = chat_id_int or int(duel["chat_id"])
        await bot.send_message(
            cid,
            f"⏱ <b>{duel['opponent_name']}</b> не принял вызов. Ставка возвращена.",
            parse_mode="HTML"
        )
    except:
        pass



async def duel_timer(duel_id: int, question_turn: int, msg_chat_id: int):
    """Через 7 секунд проверяем — если ход не сменился, передаём ход сопернику"""
    import json, random
    await asyncio.sleep(7)
    from database import get_duel, update_duel, add_gems
    from quiz import QUIZ_QUESTIONS

    duel = get_duel(duel_id)
    if not duel or duel["status"] != "active":
        return
    # Если ход уже сменился — ничего не делаем
    if duel["current_turn"] != question_turn:
        return

    # Ход не был сделан — передаём сопернику
    is_challenger = question_turn == duel["challenger_id"]
    next_turn = duel["opponent_id"] if is_challenger else duel["challenger_id"]
    next_name = duel["opponent_name"] if is_challenger else duel["challenger_name"]
    cur_name = duel["challenger_name"] if is_challenger else duel["opponent_name"]
    sc = duel["score_challenger"]
    so = duel["score_opponent"]
    qc = duel.get("q_challenger") or 0
    qo = duel.get("q_opponent") or 0

    # Увеличиваем счётчик вопросов пропустившего игрока
    if is_challenger:
        qc += 1
    else:
        qo += 1

    # Новый вопрос
    q = random.choice(QUIZ_QUESTIONS)
    options = [q["a"]] + q["w"]
    random.shuffle(options)

    update_duel(duel_id,
        q_challenger=qc,
        q_opponent=qo,
        current_turn=next_turn,
        current_question=q["q"],
        current_answer=q["a"],
        current_options=json.dumps(options, ensure_ascii=False)
    )

    await bot.send_message(
        msg_chat_id,
        f"⏱ <b>{cur_name}</b> не успел! Ход переходит к <b>{next_name}</b>\n"
        f"📊 Счёт: {duel['challenger_name']} {sc} — {so} {duel['opponent_name']}",
        parse_mode="HTML"
    )

    buttons = [[InlineKeyboardButton(text=opt, callback_data=f"duelq:{duel_id}:{opt}")] for opt in options]
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await bot.send_message(
        msg_chat_id,
        f"🌍 <b>{q['q']}</b>\n\n<i>Отвечает {next_name}</i>",
        parse_mode="HTML",
        reply_markup=kb
    )
    # Запускаем таймер для следующего хода
    asyncio.ensure_future(duel_timer(duel_id, next_turn, msg_chat_id))



@dp.message(Command("duel"))
async def cmd_duel(message: Message):
    await message.answer("🔧 Дуэль временно отключена. Скоро вернём!")
    return

    from database import get_gems, spend_gems, create_duel, get_active_duel_for_user
    uid = message.from_user.id

    opponent = None
    bet = DUEL_BET  # дефолтная ставка

    # Парсим аргументы: ищем число в тексте (ставка)
    args = message.text.split()
    for arg in args[1:]:
        if arg.isdigit():
            bet = int(arg)
            break

    if bet < DUEL_BET:
        await message.answer(f"❌ Минимальная ставка {DUEL_BET} 💎")
        return

    # Способ 1: реплай на сообщение
    if message.reply_to_message and message.reply_to_message.from_user:
        opponent = message.reply_to_message.from_user

    # Способ 2: @username в тексте
    if not opponent and message.entities:
        for ent in message.entities:
            if ent.type == "mention":
                uname = message.text[ent.offset+1:ent.offset+ent.length]
                from database import get_conn
                conn = get_conn()
                row = conn.execute("SELECT user_id, first_name, username FROM users WHERE username=?", (uname,)).fetchone()
                conn.close()
                if row:
                    class FakeUser:
                        def __init__(self, uid, fname, uname):
                            self.id = uid
                            self.first_name = fname
                            self.username = uname
                            self.is_bot = False
                    opponent = FakeUser(row["user_id"], row["first_name"], row["username"])
                break

    if not opponent:
        await message.answer(
            "⚔️ Как вызвать на дуэль:\n"
            "1. Ответь реплаем на сообщение противника: /duel\n"
            "2. Или напиши: /duel @username 100\n\n"
            f"Минимальная ставка: {DUEL_BET} 💎 с каждого"
        )
        return

    if opponent.id == uid:
        await message.answer("❌ Нельзя вызвать себя на дуэль")
        return

    # Проверяем баланс
    gems = get_gems(uid)
    if gems < bet:
        await message.answer(f"❌ Недостаточно Гемов. Нужно {bet} 💎, у тебя {gems} 💎")
        return

    # Проверяем нет ли уже активной дуэли
    active = get_active_duel_for_user(uid)
    if active:
        await message.answer("❌ У тебя уже есть активная дуэль")
        return

    # Списываем ставку с вызывающего
    spend_gems(uid, bet)

    cname = message.from_user.first_name or f"@{message.from_user.username}" or f"ID{uid}"
    oname = opponent.first_name or f"@{opponent.username}" or f"ID{opponent.id}"

    duel_id = create_duel(uid, cname, opponent.id, oname, bet, message.chat.id)

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⚔️ Принять дуэль", callback_data=f"duel:accept:{duel_id}"),
        InlineKeyboardButton(text="❌ Отказать", callback_data=f"duel:decline:{duel_id}"),
    ]])

    sent = await message.answer(
        f"⚔️ <b>Вызов на дуэль! Игра Столицы.</b>\n\n"
        f"👊 {cname} вызывает {oname}\n"
        f"💎 Банк: {bet * 2} Гемов\n"
        f"🏆 Победитель: первый до 3 правильных ответов\n\n"
        f"{oname}, принимаешь вызов? (15 сек ⏱)",
        parse_mode="HTML",
        reply_markup=kb
    )
    asyncio.ensure_future(duel_accept_timer(duel_id, uid, sent.message_id, sent.chat.id))


@dp.callback_query(F.data.startswith("duel:accept:"))
async def duel_accept(call: CallbackQuery):
    import json, random
    from database import get_gems, spend_gems, get_duel, update_duel
    from quiz import QUIZ_QUESTIONS

    duel_id = int(call.data.split(":")[2])
    duel = get_duel(duel_id)

    if not duel:
        await call.answer("Дуэль не найдена", show_alert=True)
        return
    if duel["status"] != "pending":
        await call.answer("Дуэль уже началась или отменена", show_alert=True)
        return
    if call.from_user.id != duel["opponent_id"]:
        await call.answer("Это не твоя дуэль", show_alert=True)
        return

    # Проверяем баланс оппонента
    gems = get_gems(call.from_user.id)
    if gems < duel["bet"]:
        oname = call.from_user.first_name or f"@{call.from_user.username}" or "Игрок"
        await call.answer(f"❌ Недостаточно Гемов!", show_alert=True)
        await call.message.answer(f"😔 У <b>{oname}</b> нет Гемов для дуэли.", parse_mode="HTML")
        return

    spend_gems(call.from_user.id, duel["bet"])

    # Выбираем первый вопрос
    q = random.choice(QUIZ_QUESTIONS)
    options = [q["a"]] + q["w"]
    random.shuffle(options)

    update_duel(duel_id,
        status="active",
        current_question=q["q"],
        current_answer=q["a"],
        current_options=json.dumps(options, ensure_ascii=False),
        current_turn=duel["challenger_id"]
    )

    duel = get_duel(duel_id)
    await _duel_lock_chat()
    await call.message.edit_text(
        f"⚔️ <b>Дуэль началась!</b>\n\n"
        f"👊 {duel['challenger_name']} vs {duel['opponent_name']}\n"
        f"💎 Банк: {duel['bet'] * 2} Гемов\n"
        f"📊 Счёт: 0 — 0 (до 3 побед)\n\n"
        f"Ходит: <b>{duel['challenger_name']}</b>\n\n"
        f"🔇 <i>Чат заморожен на время дуэли</i>",
        parse_mode="HTML"
    )
    await call.answer()

    # Шлём вопрос
    buttons = [[InlineKeyboardButton(text=opt, callback_data=f"duelq:{duel_id}:{opt}")] for opt in options]
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await call.message.answer(
        f"🌍 <b>{q['q']}</b>\n\n<i>Отвечает {duel['challenger_name']}</i> (5 сек ⏱)",
        parse_mode="HTML",
        reply_markup=kb
    )
    asyncio.ensure_future(duel_timer(duel_id, duel["challenger_id"], call.message.chat.id))


@dp.callback_query(F.data.startswith("duel:decline:"))
async def duel_decline(call: CallbackQuery):
    from database import get_duel, update_duel, add_gems
    duel_id = int(call.data.split(":")[2])
    duel = get_duel(duel_id)

    if not duel or duel["status"] != "pending":
        await call.answer("Дуэль уже обработана", show_alert=True)
        return
    if call.from_user.id != duel["opponent_id"]:
        await call.answer("Это не твоя дуэль", show_alert=True)
        return

    # Возвращаем ставку вызывающему
    add_gems(duel["challenger_id"], duel["bet"])
    update_duel(duel_id, status="declined")

    await call.message.edit_text(
        f"❌ {duel['opponent_name']} отказал от дуэли.\n"
        f"Ставка возвращена {duel['challenger_name']}.",
        parse_mode="HTML"
    )
    await call.answer()


@dp.callback_query(F.data.startswith("duelq:"))
async def duel_question_answer(call: CallbackQuery):
    import json, random
    from database import get_duel, update_duel, add_gems
    from quiz import QUIZ_QUESTIONS

    parts = call.data.split(":", 2)
    duel_id = int(parts[1])
    chosen = parts[2]

    duel = get_duel(duel_id)
    if not duel or duel["status"] != "active":
        await call.answer("Дуэль завершена", show_alert=True)
        return

    # Проверяем чей ход
    if call.from_user.id != duel["current_turn"]:
        await call.answer("Сейчас не твой ход!", show_alert=True)
        return

    correct = duel["current_answer"]
    is_challenger = call.from_user.id == duel["challenger_id"]
    sc = duel["score_challenger"]
    so = duel["score_opponent"]
    qc = duel.get("q_challenger") or 0
    qo = duel.get("q_opponent") or 0

    if chosen == correct:
        if is_challenger:
            sc += 1
        else:
            so += 1
        result_text = f"✅ Верно! +1"
    else:
        result_text = f"❌ Неверно! Правильно: {correct}"

    await call.answer(result_text, show_alert=True)

    # Убираем кнопки
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except:
        pass

    # Обновляем счётчик вопросов
    if is_challenger:
        qc += 1
    else:
        qo += 1

    # Победитель определяется только когда оба ответили одинаковое кол-во вопросов (минимум 3 каждый)
    winner_id = None
    # Ничья если счёт 5:5 — возвращаем ставки
    if sc == 5 and so == 5:
        update_duel(duel_id, status="finished", score_challenger=sc, score_opponent=so, q_challenger=qc, q_opponent=qo)
        add_gems(duel["challenger_id"], duel["bet"])
        add_gems(duel["opponent_id"], duel["bet"])
        await _duel_unlock_chat()
        await call.message.answer(
            f"🤝 <b>Ничья!</b>\n\n"
            f"👊 {duel['challenger_name']} {sc} — {so} {duel['opponent_name']}\n\n"
            f"💎 Ставки возвращены обоим",
            parse_mode="HTML"
        )
        return
    if qc == qo and qc >= 3:
        if sc > so:
            winner_id = duel["challenger_id"]
            winner_name = duel["challenger_name"]
        elif so > sc:
            winner_id = duel["opponent_id"]
            winner_name = duel["opponent_name"]
        # Ничья — продолжаем

    if winner_id:
        pot = duel["bet"] * 2
        commission = max(1, int(pot * 0.05))
        winnings = pot - commission
        add_gems(winner_id, winnings)
        update_duel(duel_id, status="finished", score_challenger=sc, score_opponent=so, q_challenger=qc, q_opponent=qo)
        await _duel_unlock_chat()
        await call.message.answer(
            f"🏆 <b>Дуэль завершена!</b>\n\n"
            f"👊 {duel['challenger_name']} {sc} — {so} {duel['opponent_name']}\n\n"
            f"🎉 Победитель: <b>{winner_name}</b>\n"
            f"💎 +{winnings} Гемов (5% комиссия)",
            parse_mode="HTML"
        )
        return

    # Следующий ход — меняем очерёдность
    next_turn = duel["opponent_id"] if is_challenger else duel["challenger_id"]
    next_name = duel["opponent_name"] if is_challenger else duel["challenger_name"]

    # Новый вопрос
    q = random.choice(QUIZ_QUESTIONS)
    options = [q["a"]] + q["w"]
    random.shuffle(options)

    update_duel(duel_id,
        score_challenger=sc,
        score_opponent=so,
        q_challenger=qc,
        q_opponent=qo,
        current_turn=next_turn,
        current_question=q["q"],
        current_answer=q["a"],
        current_options=json.dumps(options, ensure_ascii=False)
    )

    # Показываем текущий счёт
    tie_note = " — ничья, продолжаем!" if (qc == qo and sc == so and qc >= 3) else ""
    await call.message.answer(
        f"📊 Счёт: {duel['challenger_name']} {sc} — {so} {duel['opponent_name']}{tie_note}\n\n"
        f"Ходит: <b>{next_name}</b>",
        parse_mode="HTML"
    )

    buttons = [[InlineKeyboardButton(text=opt, callback_data=f"duelq:{duel_id}:{opt}")] for opt in options]
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await call.message.answer(
        f"🌍 <b>{q['q']}</b>\n\n<i>Отвечает {next_name}</i> (5 сек ⏱)",
        parse_mode="HTML",
        reply_markup=kb
    )
    asyncio.ensure_future(duel_timer(duel_id, next_turn, call.message.chat.id))


@dp.message(Command("topgem"))
async def cmd_topgem(message: Message):
    from database import get_conn
    conn = get_conn()
    rows = conn.execute(
        f"SELECT first_name, username, COALESCE(gems,0) as gems FROM users WHERE user_id != {ADMIN_ID} ORDER BY gems DESC LIMIT 5"
    ).fetchall()
    conn.close()

    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    lines = ["💎 <b>Топ 5 по Гемам:</b>\n"]
    for i, row in enumerate(rows):
        name = f"@{row['username']}" if row['username'] else (row['first_name'] or f"ID{i+1}")
        lines.append(f"{medals[i]} {name} — {row['gems']} 💎")

    await message.answer("\n".join(lines), parse_mode="HTML")



@dp.message(Command("topdeposit"))
async def cmd_topdeposit(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    from database import get_conn
    conn = get_conn()
    rows = conn.execute(
        "SELECT first_name, username, COALESCE(gems_bought_total,0) as total FROM users ORDER BY total DESC LIMIT 10"
    ).fetchall()
    conn.close()
    lines = ["💎 <b>Топ 10 по депозиту Гемов:</b>\n"]
    medals = ["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
    for i, row in enumerate(rows):
        name = f"@{row['username']}" if row['username'] else (row['first_name'] or f"#{i+1}")
        lines.append(f"{medals[i]} {name} — {row['total']} 💎")
    await message.answer("\n".join(lines), parse_mode="HTML")

# ── СЛОТЫ /spin ──────────────────────────────────────────

_SLOT_SYMS = ["🍒", "🍋", "🍇", "🍉", "💰", "💎"]

# (символ, множитель, доля от всех выигрышей)
# RTP = WIN_CHANCE × Σ(доля × множитель) = 0.285 × 3.16 ≈ 90%
_SLOT_WINS = [
    ("🍒",  1, 0.30),
    ("🍋",  2, 0.25),
    ("🍇",  3, 0.20),
    ("🍉",  5, 0.12),
    ("💰",  7, 0.08),
    ("💎", 10, 0.05),
]
_SLOT_WIN_CHANCE = 0.285  # ~90% RTP

def _slot_roll(bet: int) -> tuple[list[str], int]:
    import random
    if random.random() < _SLOT_WIN_CHANCE:
        r = random.random()
        cumul = 0.0
        sym, mult = "🍒", 2
        for s, m, p in _SLOT_WINS:
            cumul += p
            if r <= cumul:
                sym, mult = s, m
                break
        return [sym, sym, sym], bet * mult
    else:
        while True:
            reels = [random.choice(_SLOT_SYMS) for _ in range(3)]
            if not (reels[0] == reels[1] == reels[2]):
                return reels, 0


@dp.message(Command("spin"))
async def cmd_spin(message: Message):
    import random
    from database import get_gems, spend_gems, add_gems, get_or_create_user

    args = message.text.split()
    try:
        bet = int(args[1]) if len(args) > 1 else 10
    except ValueError:
        await message.answer("❌ Ставка должна быть числом. Пример: /spin 50")
        return

    if bet < 10:
        await message.answer("❌ Минимальная ставка — 10 💎")
        return
    if bet > 10000:
        await message.answer("❌ Максимальная ставка — 10 000 💎")
        return

    uid = message.from_user.id
    get_or_create_user(uid, message.from_user.username or "", message.from_user.first_name or "")

    gems = get_gems(uid)
    if gems < bet:
        await message.answer(f"❌ Недостаточно Гемов. У тебя {gems} 💎\n💰 /bank чтобы пополнить")
        return

    spend_gems(uid, bet)

    name = message.from_user.first_name or f"@{message.from_user.username}" or "Игрок"

    def rnd_reel():
        return " │ ".join(random.choice(_SLOT_SYMS) for _ in range(3))

    msg = await message.answer(
        f"🎰 <b>Крутим...</b> (-{bet} 💎)\n┌─────────────┐\n│ ❓  │  ❓  │  ❓ │\n└─────────────┘",
        parse_mode="HTML"
    )
    await asyncio.sleep(2.0)

    reels, win = _slot_roll(bet)
    reel_str = " │ ".join(reels)
    sym_line = f"┌─────────────┐\n│ {reel_str} │\n└─────────────┘"

    if win > 0:
        add_gems(uid, win)
        mult = win // bet
        if reels[0] == "💎":
            result = (
                f"🎰 {sym_line}\n\n"
                f"💥 <b>ДЖЕКПОТ! {name} сорвал куш!</b>\n"
                f"💎 +{win} Гемов (×{mult})"
            )
        elif reels[0] == "💰":
            result = (
                f"🎰 {sym_line}\n\n"
                f"🤑 <b>Большой выигрыш!</b> {name} забирает <b>{win} 💎</b> (×{mult})"
            )
        elif reels[0] == "🍒":
            result = (
                f"🎰 {sym_line}\n\n"
                f"↩️ Возврат ставки. {name} забирает <b>{win} 💎</b>"
            )
        else:
            result = (
                f"🎰 {sym_line}\n\n"
                f"✅ <b>Выигрыш!</b> {name} забирает <b>{win} 💎</b> (×{mult})"
            )
    else:
        result = (
            f"🎰 {sym_line}\n\n"
            f"😞 Не повезло, {name}. Потеряно <b>{bet} 💎</b>"
        )

    from database import log_game as _lg
    _lg(uid, "slot", bet, "win" if win > 0 else "lose", win)
    asyncio.create_task(_check_jackpot_overtake())

    try:
        await msg.edit_text(result, parse_mode="HTML")
    except:
        await message.answer(result, parse_mode="HTML")


@dp.message(Command("turnover"))
async def cmd_turnover(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    conn = db(); c = conn.cursor()
    try:
        c.execute("""
            SELECT
                u.user_id,
                u.username,
                u.first_name,
                SUM(gl.bet) as total,
                SUM(CASE
                    WHEN date(gl.created_at) >= date('now', '-' || ((strftime('%w','now') + 6) % 7) || ' days')
                    THEN gl.bet ELSE 0 END) as week,
                MAX(gl.created_at) as last_at,
                COUNT(*) as rounds
            FROM game_log gl
            JOIN users u ON u.user_id = gl.user_id
            GROUP BY gl.user_id
            ORDER BY total DESC
            LIMIT 30
        """)
        rows = c.fetchall()
    except Exception as e:
        conn.close()
        await message.answer(f"❌ {e}")
        return
    conn.close()

    if not rows:
        await message.answer("Нет данных")
        return

    lines = ["📊 <b>Оборот игроков</b>\n<code>"]
    lines.append(f"{'Игрок':<18} {'Неделя':>8} {'Всего':>9} {'Игр':>5}")
    lines.append("─" * 44)
    for row in rows:
        name = (f"@{row['username']}" if row['username'] else (row['first_name'] or f"ID{row['user_id']}"))[:17]
        lines.append(f"{name:<18} {row['week']:>8} {row['total']:>9} {row['rounds']:>5}")
    lines.append("</code>")

    await message.answer("\n".join(lines), parse_mode="HTML")


# ── РУЛЕТКА /roul ────────────────────────────────────────────

_ROUL_RED = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
_roul_pending: dict = {}  # (user_id, chat_id) -> {bet, msg_id}

def _roul_color(n: int) -> str:
    if n == 0: return "🟢"
    return "🔴" if n in _ROUL_RED else "⚫"

def _roul_kb(bet: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="0", callback_data=f"roul:{bet}:0")]]
    for i in range(0, 36, 6):
        rows.append([
            InlineKeyboardButton(text=str(n), callback_data=f"roul:{bet}:{n}")
            for n in range(i+1, i+7)
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(Command("roul"))
async def cmd_roul(message: Message):
    from database import get_gems
    uid = message.from_user.id
    args = message.text.split()
    bet = 1
    if len(args) > 1:
        try:
            bet = int(args[1])
        except:
            await message.answer("❌ Ставка числом. Пример: /roul 50")
            return
    if bet < 1:
        await message.answer("❌ Минимальная ставка — 1 💎")
        return
    bal = get_gems(uid)
    if bal < bet:
        await message.answer(f"❌ Недостаточно Гемов. Баланс: {bal} 💎")
        return
    uname = message.from_user.first_name or f"@{message.from_user.username}"
    msg = await message.answer(
        f"🎡 <b>Рулетка — {uname}</b>\n"
        f"Ставка: <b>{bet} 💎</b>\n\n"
        f"Выигрыш при попадании: <b>x35</b>\n\n"
        f"Выбери число от 0 до 36:",
        parse_mode="HTML",
        reply_markup=_roul_kb(bet)
    )
    _roul_pending[(uid, message.chat.id)] = {"bet": bet, "msg_id": msg.message_id}


@dp.callback_query(F.data.startswith("roul:"))
async def roul_cb(callback: CallbackQuery):
    from database import get_gems, spend_gems, add_gems, log_game
    import random as _rnd
    parts = callback.data.split(":")
    bet = int(parts[1])
    chosen = int(parts[2])
    uid = callback.from_user.id
    key = (uid, callback.message.chat.id)

    pending = _roul_pending.get(key)
    if not pending or pending["msg_id"] != callback.message.message_id:
        await callback.answer("Это не твоя рулетка!", show_alert=True)
        return

    del _roul_pending[key]

    if get_gems(uid) < bet:
        await callback.answer("❌ Недостаточно Гемов!", show_alert=True)
        return

    spend_gems(uid, bet)
    uname = callback.from_user.first_name or f"@{callback.from_user.username}"

    await callback.message.edit_text(
        f"🎡 <b>Рулетка — {uname}</b>\n"
        f"Ставка {bet} 💎 на число <b>{chosen}</b>\n\n"
        f"⏳ Шарик крутится...",
        parse_mode="HTML"
    )
    await callback.answer()
    await asyncio.sleep(2)

    result = _rnd.randint(0, 36)
    color = _roul_color(result)
    new_bal = get_gems(uid)

    if result == chosen:
        win = bet * 35
        add_gems(uid, bet + win)
        log_game(uid, "roul", bet, "win", win)
        new_bal = get_gems(uid)
        text = (
            f"🎡 <b>Рулетка — {uname}</b>\n"
            f"Ставка {bet} 💎 на <b>{chosen}</b>\n\n"
            f"🎯 Выпало: {color} <b>{result}</b>\n\n"
            f"🎉 <b>ПОБЕДА! +{win} 💎 (x35)</b>"
        )
        asyncio.create_task(_check_jackpot_overtake())
    else:
        log_game(uid, "roul", bet, "lose", 0)
        text = (
            f"🎡 <b>Рулетка — {uname}</b>\n"
            f"Ставка {bet} 💎 на <b>{chosen}</b>\n\n"
            f"🎯 Выпало: {color} <b>{result}</b>\n\n"
            f"😔 Не угадал. -{bet} 💎"
        )
        asyncio.create_task(_check_jackpot_overtake())

    await callback.message.edit_text(text, parse_mode="HTML")


def _jackpot_time_left() -> str:
    from datetime import datetime, timedelta
    now = datetime.utcnow()
    # Следующий понедельник 00:00 UTC
    days_ahead = (7 - now.weekday()) % 7 or 7
    next_monday = (now + timedelta(days=days_ahead)).replace(hour=0, minute=0, second=0, microsecond=0)
    delta = next_monday - now
    total_sec = int(delta.total_seconds())
    days = total_sec // 86400
    hours = (total_sec % 86400) // 3600
    minutes = (total_sec % 3600) // 60
    if days >= 1:
        if days == 1:   word = "день"
        elif days <= 4: word = "дня"
        else:           word = "дней"
        return f"⏳ Осталось {days} {word}"
    elif hours >= 1:
        return f"⏳ Осталось {hours}ч {minutes}м"
    else:
        return f"⏳ Осталось {minutes} мин"


@dp.message(Command("topstars"))
async def cmd_topstars(message: Message):
    conn = db(); c = conn.cursor()
    c.execute("""
        SELECT u.user_id, u.username, u.first_name,
               COALESCE(p.stars,0) + COALESCE(g.stars,0) as total
        FROM users u
        LEFT JOIN (SELECT user_id, SUM(stars) as stars FROM purchases GROUP BY user_id) p ON p.user_id=u.user_id
        LEFT JOIN (SELECT user_id, SUM(stars) as stars FROM gem_purchases GROUP BY user_id) g ON g.user_id=u.user_id
        WHERE u.user_id != ? AND (COALESCE(p.stars,0) + COALESCE(g.stars,0)) > 0
        ORDER BY total DESC LIMIT 5
    """, (ADMIN_ID,))
    rows = c.fetchall(); conn.close()
    medals = ["🥇","🥈","🥉","4️⃣","5️⃣"]
    lines = ["⭐ <b>Топ по Telegram Stars:</b>\n"]
    for i, row in enumerate(rows):
        name = f"@{row['username']}" if row['username'] else (row['first_name'] or f"ID{row['user_id']}")
        lines.append(f"{medals[i]} {name} — {row['total']} ⭐")
    lines.append("\n<i>Спасибо что поддерживаете район 🙏</i>")
    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("topbuygem"))
async def cmd_topbuygem(message: Message):
    conn = db(); c = conn.cursor()
    c.execute("""
        SELECT gp.gems, gp.stars, gp.created_at,
               u.username, u.first_name
        FROM gem_purchases gp
        JOIN users u ON u.user_id = gp.user_id
        ORDER BY gp.gems DESC LIMIT 10
    """)
    rows = c.fetchall(); conn.close()
    medals = ["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
    lines = ["💎 <b>Топ 10 разовых покупок Гемов:</b>\n"]
    for i, row in enumerate(rows):
        name = f"@{row['username']}" if row['username'] else (row['first_name'] or "Аноним")
        date = row['created_at'][:10]
        lines.append(f"{medals[i]} {name} — {row['gems']} 💎 за {row['stars']} ⭐ <i>({date})</i>")
    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("jackpot"))
async def cmd_jackpot(message: Message):
    conn = db(); c = conn.cursor()
    try:
        # Оборот за текущую неделю (с пн 00:00 UTC) по всем играм, без админа
        c.execute("""
            SELECT gl.user_id,
                   u.username, u.first_name,
                   SUM(gl.bet) as turnover
            FROM game_log gl
            JOIN users u ON u.user_id = gl.user_id
            WHERE date(gl.created_at) >= date('now', '-' || ((strftime('%w','now') + 6) % 7) || ' days')
              AND gl.user_id != ?
            GROUP BY gl.user_id
            ORDER BY turnover DESC
            LIMIT 5
        """, (ADMIN_ID,))
        rows = c.fetchall()
    except Exception as e:
        conn.close()
        await message.answer(f"❌ Ошибка: {e}")
        return
    conn.close()

    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    lines = ["🎰 <b>Jackpot — недельный топ по обороту гемов</b>\n"]

    if rows:
        for i, row in enumerate(rows):
            name = row['first_name'] or f"@{row['username']}" or f"ID{row['user_id']}"
            lines.append(f"{medals[i]} {name} — {row['turnover']} 💎")
    else:
        lines.append("Пока никто не крутил слот на этой неделе")

    lines.append(
        f"\n🏆 <b>Приз:</b> NFT Pool Float\n"
        f"🔗 https://t.me/nft/PoolFloat-197571\n\n"
        f"{_jackpot_time_left()}"
    )

    await message.answer("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


# ── NFT РОЗЫГРЫШИ ────────────────────────────────────────────
@dp.message(Command("nft"))
async def cmd_nft(message: Message):
    conn = db(); c = conn.cursor()

    # Лидер реферальной гонки (активные рефы — хотя бы раз голосовали)
    try:
        c.execute("""
            SELECT u.user_id, u.username, u.first_name,
                   COUNT(ref_u.user_id) as active_refs,
                   (SELECT COUNT(*) FROM users r WHERE r.ref_by = u.user_id) as total_refs
            FROM users u
            JOIN users ref_u ON ref_u.ref_by = u.user_id
            WHERE u.user_id != ? AND (u.is_banned IS NULL OR u.is_banned = 0)
              AND (SELECT COUNT(*) FROM votes WHERE voter_id = ref_u.user_id) > 0
            GROUP BY u.user_id
            ORDER BY active_refs DESC
            LIMIT 1
        """, (ADMIN_ID,))
        ref_row = c.fetchone()
        if ref_row and ref_row['active_refs'] > 0:
            ref_name = f"@{ref_row['username']}" if ref_row['username'] else (ref_row['first_name'] or f"ID{ref_row['user_id']}")
            ref_leader = f"🥇 {ref_name} — {ref_row['active_refs']} в игре (всего рефов: {ref_row['total_refs']})"
        else:
            ref_leader = "пока нет участников"
    except:
        ref_leader = "данные недоступны"

    # Лидер по обороту гемов (jackpot — за текущую неделю)
    try:
        c.execute("""
            SELECT gl.user_id, u.username, u.first_name, SUM(gl.bet) as turnover
            FROM game_log gl
            JOIN users u ON u.user_id = gl.user_id
            WHERE date(gl.created_at) >= date('now', '-' || ((strftime('%w','now') + 6) % 7) || ' days')
              AND gl.user_id != ?
            GROUP BY gl.user_id
            ORDER BY turnover DESC
            LIMIT 1
        """, (ADMIN_ID,))
        jackpot_row = c.fetchone()
        if jackpot_row:
            j_name = f"@{jackpot_row['username']}" if jackpot_row['username'] else jackpot_row['first_name']
            jackpot_leader = f"{j_name} — {jackpot_row['turnover']} 💎"
        else:
            jackpot_leader = "пока нет участников"
    except:
        jackpot_leader = "данные недоступны"

    auction_active = AUCTION_ACTIVE()
    conn.close()

    # Считаем активные розыгрыши: 5 базовых + аукцион если активен
    total_nft = 5 + (1 if auction_active else 0)

    lines = [
        f"🖼 <b>NFT РОЗЫГРЫШИ — итоги 31 мая</b>",
        f"🏆 Разыгрывается NFT: <b>{total_nft} шт.</b>\n",
        "🔗 <b>Реферальная гонка: 3 NFT</b> /ref",
        f"Лидер: {ref_leader}\n",
        "💎 <b>Самый крупный оборот Гемов: 1 NFT</b> /jackpot",
        f"Лидер: {jackpot_leader}\n",
        "💸 <b>Самый крупный разовый бай Гемов: 1 NFT</b>",
        "Топ Бай: 500 звёзд\n",
        "🎁 <b>NFT DROP</b> — Падает случайным игрокам, которые играют в чате в казик\n",
        "🏺 <b>Аукцион</b>",
        "На данный момент неактивен",
    ]
    await message.answer("\n".join(lines), parse_mode="HTML")


# ── JACKPOT OVERTAKE NOTIFICATION ────────────────────────────
_jackpot_top_cache: list[dict] = []  # [{user_id, name, turnover}, ...]

def _get_jackpot_top() -> list[dict]:
    conn = db(); c = conn.cursor()
    try:
        c.execute("""
            SELECT gl.user_id, u.username, u.first_name, SUM(gl.bet) as turnover
            FROM game_log gl
            JOIN users u ON u.user_id = gl.user_id
            WHERE date(gl.created_at) >= date('now', '-' || ((strftime('%w','now') + 6) % 7) || ' days')
              AND gl.user_id != ?
            GROUP BY gl.user_id
            ORDER BY turnover DESC
            LIMIT 5
        """, (ADMIN_ID,))
        rows = c.fetchall()
    except:
        rows = []
    conn.close()
    result = []
    for i, row in enumerate(rows):
        name = row['first_name'] or f"@{row['username']}" or f"ID{row['user_id']}"
        result.append({"pos": i + 1, "user_id": row["user_id"], "name": name, "turnover": row["turnover"]})
    return result

async def _check_jackpot_overtake():
    global _jackpot_top_cache
    new_top = _get_jackpot_top()
    old_top = _jackpot_top_cache
    if not old_top:
        _jackpot_top_cache = new_top
        return
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    old_pos = {e["user_id"]: e["pos"] for e in old_top}
    for entry in new_top:
        uid = entry["user_id"]
        new_p = entry["pos"]
        old_p = old_pos.get(uid, 6)
        if new_p < old_p and new_p <= 5:
            # Кого обогнал — кто был на этом месте раньше
            overtaken = next((e for e in old_top if e["pos"] == new_p), None)
            if overtaken and overtaken["user_id"] != uid:
                medal = medals[new_p - 1]
                try:
                    await bot.send_message(
                        CHAT_ID,
                        f"🎰 <b>{entry['name']}</b> обогнал <b>{overtaken['name']}</b> "
                        f"и поднялся на {medal} {new_p}-е место в Джекпот топе!",
                        parse_mode="HTML"
                    )
                except: pass
    _jackpot_top_cache = new_top


if __name__ == "__main__":
    import fcntl, sys
    _lock_file = open("/tmp/shrimp_bot.lock", "w")
    try:
        fcntl.flock(_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("Bot already running, exiting.")
        sys.exit(0)
    asyncio.run(main())
