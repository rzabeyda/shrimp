import asyncio
import os
import sqlite3
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, PreCheckoutQuery, CallbackQuery
from aiogram.filters import CommandStart, Command
from aiogram.utils.deep_linking import decode_payload
from dotenv import load_dotenv

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
    "🗡 <b>Правила игры</b>\n\n"
    "• Каждые 15 минут открывается голосование\n"
    "• Проголосуй за того кого хочешь выбить\n"
    "• Кто набрал больше всего голосов — <b>съеден</b> 🍳\n"
    "• Используй предметы из магазина чтобы выжить\n"
    "• Последняя выжившая креветка забирает приз 🏆\n\n"
    "Голосование начинается прямо сейчас!"
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


@dp.message(CommandStart())
async def start(message: Message):
    args = message.text.split(maxsplit=1)
    ref_by = None
    if len(args) > 1:
        try: ref_by = int(decode_payload(args[1]))
        except:
            try: ref_by = int(args[1])
            except: pass

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
            ref_info = f"\n👥 Реферал от {ref_label}"
        else:
            ref_info = ""
        notify = f"👤 <b>Новый юзер!</b>\n👤 {uname}{ref_info}"
        try:
            await bot.send_message(ADMIN_ID, notify, parse_mode="HTML")
        except: pass
        await log_to_group(f"👤 <b>Новый игрок</b>\n{notify}")

    webapp_url = WEBAPP_URL
    if ref_by and ref_by != message.from_user.id:
        webapp_url = f"{WEBAPP_URL}?ref={ref_by}"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Открыть", web_app=WebAppInfo(url=webapp_url))],
        [
            InlineKeyboardButton(text="💬 Чат", url=CHAT_URL),
            InlineKeyboardButton(text="📢 Канал", url="https://t.me/shrimpgames_channel")
        ]
    ])

    await message.answer_photo(
        photo="https://shrimpgames.zabeyda.lol/static/icons/main.png",
        caption=(
            "🗡 <b>Разборки на районе</b>\n\n"
            "Убирай конкурентов, строй союзы, используй связи.\n"
            "Кого завалят следующим — не тебя ли?\n\n"
            "Последние выжившие забирают призы"
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
        photo="https://shrimpgames.zabeyda.lol/static/icons/main.png",
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
    await query.answer(ok=True)


@dp.message(F.successful_payment)
async def successful_payment(message: Message):
    payload = message.successful_payment.invoice_payload
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
    # Анонимное уведомление в чат о покупке
    import httpx as _hx, random as _rb
    BUY_MSGS = {
        "shield":      ["🤵 Связь Крышануться куплена — кто-то залёг под крышу", "🤵 Один игрок крышанулся. В следующем раунде его не тронут"],
        "killer":      ["💀 Связь Киллер куплена — в игре появился наёмный убийца", "💀 Кто-то занёс киллеру. Берегитесь..."],
        "resurrect":   ["🎭 Связь Постанова куплена — кто-то готовит инсценировку", "🎭 Один игрок купил страховку. Смерть — не конец"],
        "hacker":      ["💰 Связь Ворюга куплена — чьи-то голоса скоро исчезнут", "💰 Ворюга нанят. Кто-то лишится своих голосов"],
        "spy":         ["🐭 Связь Стукач куплена — кто-то внедрил своего человека", "🐭 В игре завёлся стукач. Секреты под угрозой"],
        "tiebreaker":  ["⚖️ Связь Решала куплена — ничья больше не страшна", "⚖️ Решала на столе. При ничье всё решится мгновенно"],
        "double_vote": ["🔫 Связь Двустволка куплена — кто-то зарядил дуплет", "🔫 Двустволка взведена. Чей-то голос скоро удвоится"],
        "anon_player": ["👻 Связь Анонимус куплена — кто-то готовится раствориться", "🥷 Скоро один из игроков уйдёт в тень. Следи внимательно"],
        "anon_msg":    ["📩 Связь Малява куплена — анонимное письмо уже в пути...", "📩 Малява отправлена. Кто-то сговаривается из тени"],
        "black_mark":  ["🚔 Связь Мусорнуться куплена — кто-то готовится настучать на Анонимуса", "🚔 Заява куплена. Анонимус нервничает"],
    }
    _msgs = BUY_MSGS.get(item_type if item_type != "combo" else None)
    if _msgs:
        try:
            _buy_msg = _rb.choice(_msgs)
            # Добавляем имя покупателя кроме анонимуса
            if item_type != "anon_player":
                _buy_msg = f"{uname}: {_buy_msg}"
            async with _hx.AsyncClient(timeout=8) as _cl2:
                await _cl2.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={"chat_id": "@shrimpgames_chat", "text": _buy_msg, "parse_mode": "HTML"})
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
    """Топ-5 рефоводов — доступна в чате"""
    from database import get_conn
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT u.user_id, u.username, u.first_name, COUNT(r.user_id) as ref_count
        FROM users u
        JOIN users r ON r.ref_by = u.user_id
        WHERE u.user_id != 7308147004
        GROUP BY u.user_id
        ORDER BY ref_count DESC
        LIMIT 5
    """)
    rows = c.fetchall()
    conn.close()

    if not rows:
        await message.answer("🔗 Пока никто не привёл друзей на район.", parse_mode="HTML")
        return

    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    lines = ["🔗 <b>Топ рефоводов района:</b>\n"]
    for i, row in enumerate(rows):
        name = f"@{row['username']}" if row['username'] else (row['first_name'] or f"ID{row['user_id']}")
        cnt = row['ref_count']
        word = "друг" if cnt == 1 else "друга" if 2 <= cnt <= 4 else "друзей"
        lines.append(f"{medals[i]} {name} — {cnt} {word}")

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
    # Создаём новую игру
    c.execute("SELECT MAX(number) as mx FROM games")
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

    # Всего покупок и звёзд — только реальные платежи
    c.execute("""CREATE TABLE IF NOT EXISTS purchases
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
                  item_type TEXT, stars INTEGER, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("SELECT COUNT(*) as cnt, COALESCE(SUM(stars),0) as total FROM purchases")
    prow = c.fetchone()
    total_purchases = prow["cnt"]
    total_stars = prow["total"]

    # Активная игра
    c.execute("SELECT * FROM games WHERE status IN ('waiting','active') ORDER BY id DESC LIMIT 1")
    game = c.fetchone()

    conn.close()

    game_info = ""
    if game:
        game_info = f"\n\n🎮 Стрелка #{game['number']} — {game['status']}, разборки {game['current_day'] or 0}"

    await message.answer(
        f"📊 <b>Статистика бота</b>\n\n"
        f"👥 Всего юзеров: <b>{total_users}</b>\n"
        f"🆕 Пришли сегодня: <b>{today_users}</b>\n\n"
        f"🛒 Всего покупок: <b>{total_purchases}</b>\n"
        f"⭐ Потрачено звёзд: <b>{total_stars}</b>"
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
        "🔗 /ref — Топ 5 рефоводов\n"
        "🗳 /top — Топ 5 игроков по голосам\n\n"
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


async def main():
    asyncio.ensure_future(reminder_loop())
    asyncio.ensure_future(commands_broadcast_loop())
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


if __name__ == "__main__":
    asyncio.run(main())
