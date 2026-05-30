import sqlite3
import os
from datetime import datetime, timedelta

DB_PATH = os.getenv("DB_PATH", "shrimp.db")

def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def init_db():
    conn = get_conn()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id      INTEGER PRIMARY KEY,
            username     TEXT,
            first_name   TEXT,
            photo_url    TEXT,
            ref_by       INTEGER,
            games_played INTEGER DEFAULT 0,
            kills        INTEGER DEFAULT 0,
            wins         INTEGER DEFAULT 0,
            chat_joined  INTEGER DEFAULT 0,
            streak_days  INTEGER DEFAULT 0,
            streak_last  TEXT DEFAULT NULL,
            created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    for col, coldef in [
        ("games_played","INTEGER DEFAULT 0"),("kills","INTEGER DEFAULT 0"),
        ("wins","INTEGER DEFAULT 0"),("chat_joined","INTEGER DEFAULT 0"),("streak_days","INTEGER DEFAULT 0"),("streak_last","TEXT DEFAULT NULL"),("losses","INTEGER DEFAULT 0"),("clean_wins","INTEGER DEFAULT 0"),("first_joins","INTEGER DEFAULT 0"),("sent_anon","INTEGER DEFAULT 0"),("times_voted_against","INTEGER DEFAULT 0"),("killed_by_killer","INTEGER DEFAULT 0"),("premium_force","INTEGER DEFAULT 0"),("gender","TEXT DEFAULT NULL"),("message_count","INTEGER DEFAULT 0"),("is_banned","INTEGER DEFAULT 0"),("quiz_correct","INTEGER DEFAULT 0"),("gems","INTEGER DEFAULT 0"),("gems_claimed","INTEGER DEFAULT 0"),("gems_purchased","INTEGER DEFAULT 0"),("gems_bought_total","INTEGER DEFAULT 0"),("bot_blocked","INTEGER DEFAULT 0")
    ]:
        try: c.execute(f"ALTER TABLE users ADD COLUMN {col} {coldef}")
        except: pass

    c.execute("""
        CREATE TABLE IF NOT EXISTS games (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            number      INTEGER DEFAULT 1,
            status      TEXT DEFAULT 'waiting',
            max_players INTEGER DEFAULT 0,
            current_day INTEGER DEFAULT 0,
            prize_desc  TEXT,
            prize_link  TEXT,
            created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
            started_at  DATETIME,
            voting_ends TEXT,
            finished_at DATETIME,
            winner_id   INTEGER
        )
    """)
    for col, coldef in [
        ("number","INTEGER DEFAULT 1"),("max_players","INTEGER DEFAULT 0"),
        ("current_day","INTEGER DEFAULT 0"),("voting_ends","TEXT")
    ]:
        try: c.execute(f"ALTER TABLE games ADD COLUMN {col} {coldef}")
        except: pass

    # Убрать лимит в существующей игре
    c.execute("UPDATE games SET max_players=0 WHERE max_players=10 AND status='waiting'")

    c.execute("""
        CREATE TABLE IF NOT EXISTS players (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id   INTEGER,
            user_id   INTEGER,
            is_alive  INTEGER DEFAULT 1,
            joined_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(game_id, user_id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS votes (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id    INTEGER,
            day_number INTEGER,
            voter_id   INTEGER,
            target_id  INTEGER,
            weight     INTEGER DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(game_id, day_number, voter_id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS items (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id    INTEGER,
            user_id    INTEGER,
            item_type  TEXT,
            status     TEXT DEFAULT 'active',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    try: c.execute("ALTER TABLE items ADD COLUMN status TEXT DEFAULT 'active'")
    except: pass
    try: c.execute("ALTER TABLE votes ADD COLUMN weight INTEGER DEFAULT 1")
    except: pass

    c.execute("""
        CREATE TABLE IF NOT EXISTS final_votes (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id   INTEGER,
            voter_id  INTEGER,
            target_id INTEGER,
            UNIQUE(game_id, voter_id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS kills_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id    INTEGER,
            killer_id  INTEGER,
            victim_id  INTEGER,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS game_events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id    INTEGER,
            event_type TEXT,
            text       TEXT,
            icon       TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    c.execute("SELECT COUNT(*) as cnt FROM games")
    if c.fetchone()["cnt"] == 0:
        c.execute("""INSERT INTO games (number, status, max_players, prize_desc, prize_link)
            VALUES (1, 'waiting', 0, 'NFT Giraffe Pool Float', 'https://t.me/nft/PoolFloat-148562')""")

    c.execute("""CREATE TABLE IF NOT EXISTS clans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        game_id INTEGER, leader_id INTEGER,
        name TEXT DEFAULT 'Клан',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(game_id, leader_id))""")
    c.execute("""CREATE TABLE IF NOT EXISTS clan_members (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        clan_id INTEGER, user_id INTEGER,
        joined_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(clan_id, user_id))""")
    c.execute("""CREATE TABLE IF NOT EXISTS clan_invites (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        clan_id INTEGER, from_id INTEGER, to_id INTEGER,
        status TEXT DEFAULT 'pending',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(clan_id, to_id))""")

    conn.commit()
    conn.close()


def get_or_create_user(user_id, username, first_name, ref_by=None):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    user = c.fetchone()
    if not user:
        c.execute("INSERT INTO users (user_id, username, first_name, ref_by) VALUES (?,?,?,?)",
                  (user_id, username, first_name, ref_by))
        # Новым юзерам даём Чёрную метку бесплатно
        c.execute("INSERT INTO items (user_id, item_type, game_id, status) VALUES (?,?,NULL,'active')",
                  (user_id, "black_mark"))
        conn.commit()
    conn.close()


def get_user(user_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    u = c.fetchone()
    conn.close()
    return u


def update_user_photo(user_id, photo_url):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE users SET photo_url=? WHERE user_id=?", (photo_url, user_id))
    conn.commit()
    conn.close()


def set_chat_joined(user_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE users SET chat_joined=1 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


def get_referral_count(user_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) as cnt FROM users WHERE ref_by=?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row["cnt"] if row else 0



def set_premium_force(user_id, value: bool):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE users SET premium_force=? WHERE user_id=?", (1 if value else 0, user_id))
    conn.commit()
    conn.close()
    return c.rowcount > 0


def set_gender(user_id, gender: str):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE users SET gender=? WHERE user_id=?", (gender, user_id))
    conn.commit()
    conn.close()

def get_gender(user_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT gender FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row["gender"] if row else None


def get_user_by_username(username: str):
    conn = get_conn()
    c = conn.cursor()
    username = username.lstrip("@")
    c.execute("SELECT * FROM users WHERE LOWER(username)=LOWER(?)", (username,))
    row = c.fetchone()
    conn.close()
    return row


def update_user_profile(user_id, username, first_name):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE users SET username=?, first_name=? WHERE user_id=?", (username, first_name, user_id))
    conn.commit()
    conn.close()

def get_user_stats(user_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT games_played, kills, wins, chat_joined, streak_days, streak_last, used_spy, used_killer, bought_shield, used_double_vote, resurrected, first_purchase, went_anon, won_as_anon, losses, clean_wins, first_joins, sent_anon, times_voted_against, killed_by_killer, items_used, items_won, premium_force, COALESCE(created_clan,0) as created_clan, COALESCE(votes_cast,0) as votes_cast FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    # Считаем покупки из таблицы purchases
    purchases_bought = 0
    distinct_bought = 0
    premium_icon = None
    try:
        c.execute("SELECT COUNT(*) as cnt FROM purchases WHERE user_id=?", (user_id,))
        r = c.fetchone(); purchases_bought = r["cnt"] if r else 0
        c.execute("SELECT COUNT(DISTINCT item_type) as cnt FROM purchases WHERE user_id=?", (user_id,))
        r = c.fetchone(); distinct_bought = r["cnt"] if r else 0
        # Премиум иконка
        c.execute("SELECT value FROM settings WHERE key=?", (f"premium_icon_{user_id}",))
        r = c.fetchone(); premium_icon = r["value"] if r else None
    except: pass
    conn.close()
    base = dict(row) if row else {"games_played":0,"kills":0,"wins":0,"chat_joined":0,"streak_days":0,"streak_last":None,"used_spy":0,"used_killer":0,"bought_shield":0,"used_double_vote":0,"resurrected":0,"first_purchase":0,"losses":0,"clean_wins":0,"first_joins":0,"sent_anon":0,"times_voted_against":0,"killed_by_killer":0,"items_used":0,"items_won":0}
    base["items_bought"] = purchases_bought
    # items_won берём из колонки users.items_won (инкрементируется при выигрыше в колесе)
    base["items_won"] = (base.get("items_won") or 0)
    base["distinct_bought"] = distinct_bought
    ref_count = 0
    try:
        c2 = get_conn().cursor()
        c2.execute("SELECT COUNT(*) as cnt FROM users WHERE ref_by=? AND (SELECT COUNT(*) FROM votes WHERE voter_id=users.user_id) > 0", (user_id,))
        r = c2.fetchone(); ref_count = r["cnt"] if r else 0
    except: pass
    base["ref_count"] = ref_count
    base["is_premium"] = distinct_bought >= 5 or ref_count >= 5 or bool(base.get("premium_force"))
    base["premium_icon"] = premium_icon
    return base


def get_active_game():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM games WHERE status IN ('waiting','active') ORDER BY id DESC LIMIT 1")
    g = c.fetchone()
    conn.close()
    return g


def get_current_prize():
    return get_active_game()


def get_game_players(game_id, alive_only=False):
    conn = get_conn()
    c = conn.cursor()
    q = """SELECT u.user_id, u.username, u.first_name, u.photo_url, u.gender, p.is_alive, p.joined_at,
           CASE WHEN u.premium_force=1
                     OR (SELECT COUNT(DISTINCT item_type) FROM items WHERE user_id=u.user_id AND status IN ('active','used')) >= 5
                     OR (SELECT COUNT(*) FROM users r WHERE r.ref_by=u.user_id AND (SELECT COUNT(*) FROM votes WHERE voter_id=r.user_id) > 0) >= 5
                THEN 1 ELSE 0 END as is_premium
           FROM players p JOIN users u ON p.user_id=u.user_id WHERE p.game_id=?"""
    if alive_only:
        q += " AND p.is_alive=1"
    q += " ORDER BY p.joined_at ASC"
    c.execute(q, (game_id,))
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def register_player(game_id, user_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT id FROM players WHERE game_id=? AND user_id=?", (game_id, user_id))
    if c.fetchone():
        conn.close()
        return False, 0, "Уже записан"
    c.execute("INSERT INTO players (game_id, user_id) VALUES (?,?)", (game_id, user_id))
    c.execute("SELECT COUNT(*) as cnt FROM players WHERE game_id=?", (game_id,))
    cnt = c.fetchone()["cnt"]
    conn.commit()
    conn.close()
    return True, cnt, "ok"


def get_user_items(user_id, game_id=None):
    conn = get_conn()
    c = conn.cursor()
    # Берём ВСЕ активные предметы юзера — абилка работает в любой игре
    c.execute("SELECT * FROM items WHERE user_id=? AND status='active'", (user_id,))
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_item(user_id, item_type, game_id=None):
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO items (user_id, item_type, game_id, status) VALUES (?,?,?,'active')",
              (user_id, item_type, game_id))
    conn.commit()
    conn.close()


def get_user_vote(game_id, day_number, voter_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM votes WHERE game_id=? AND day_number=? AND voter_id=?",
              (game_id, day_number, voter_id))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


BOMZH_EVENT_END = datetime(2026, 5, 26, 0, 0, 0)  # UTC, событие закончилось

def has_bomzh_item(user_id, item_id):
    try:
        conn = get_conn(); c = conn.cursor()
        c.execute("SELECT id, permanent FROM bomzh_items WHERE user_id=? AND item_id=? LIMIT 1", (user_id, item_id))
        row = c.fetchone(); conn.close()
        if not row:
            return False
        if row["permanent"]:
            return True
        return datetime.utcnow() <= BOMZH_EVENT_END
    except:
        return False


def cast_vote(game_id, day_number, voter_id, target_id):
    conn = get_conn()
    c = conn.cursor()
    weight = 1
    if has_bomzh_item(voter_id, 'pistol'):
        weight += 1
    if has_bomzh_item(target_id, 'drugs'):
        weight = max(0, weight - 1)
    try:
        c.execute("INSERT INTO votes (game_id, day_number, voter_id, target_id, weight) VALUES (?,?,?,?,?)",
                  (game_id, day_number, voter_id, target_id, weight))
    except:
        c.execute("UPDATE votes SET target_id=?, weight=? WHERE game_id=? AND day_number=? AND voter_id=?",
                  (target_id, weight, game_id, day_number, voter_id))
    conn.commit()
    conn.close()
    return weight


def cast_double_vote(game_id, day_number, voter_id, target_id):
    """Активировать двустволку — добавить +2 голоса за target_id (отдельно от основного голоса)"""
    conn = get_conn()
    c = conn.cursor()
    # Проверяем что у игрока есть двустволка
    c.execute("SELECT id FROM items WHERE user_id=? AND item_type='double_vote' AND status='active' LIMIT 1", (voter_id,))
    dv = c.fetchone()
    if not dv:
        conn.close()
        return False, "Нет предмета Двустволка"
    # Списываем абилку
    c.execute("UPDATE items SET status='used' WHERE id=?", (dv["id"],))
    try:
        c.execute("UPDATE users SET used_double_vote=used_double_vote+1, items_used=items_used+1 WHERE user_id=?", (voter_id,))
    except: pass
    # Добавляем отдельную запись с весом 2 за target_id
    # Используем специальный voter_id = voter_id + 9000000000 чтобы не конфликтовать с основным голосом
    fake_voter_id = voter_id + 9000000000
    try:
        c.execute("INSERT INTO votes (game_id, day_number, voter_id, target_id, weight) VALUES (?,?,?,?,?)",
                  (game_id, day_number, fake_voter_id, target_id, 2))
    except:
        conn.close()
        return False, "Ошибка записи голоса"
    conn.commit()
    conn.close()
    return True, "OK"


def kill_player_by_killer(game_id, killer_user_id, target_user_id):
    conn = get_conn()
    c = conn.cursor()
    # Ищем предмет с game_id или без (купленный через Stars может не иметь game_id)
    c.execute("SELECT id FROM items WHERE user_id=? AND game_id=? AND item_type='killer' AND status='active' LIMIT 1",
              (killer_user_id, game_id))
    item = c.fetchone()
    if not item:
        c.execute("SELECT id FROM items WHERE user_id=? AND item_type='killer' AND status='active' LIMIT 1",
                  (killer_user_id,))
        item = c.fetchone()
    if not item:
        conn.close()
        return False, "Нет предмета Киллер"
    c.execute("UPDATE items SET status='used' WHERE id=?", (item["id"],))
    c.execute("UPDATE players SET is_alive=0 WHERE game_id=? AND user_id=?", (game_id, target_user_id))
    c.execute("UPDATE users SET kills=kills+1 WHERE user_id=?", (killer_user_id,))
    conn.commit()
    conn.close()
    return True, "ok"


def get_vote_results(game_id, day_number):
    """Подсчёт голосов за раунд. Учитываем только живых — убитые киллером не считаются.
    При ничьей — выбывает тот за кого проголосовали раньше, или кто позже зарегистрировался."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT v.target_id,
               SUM(v.weight) as cnt,
               MIN(v.created_at) as first_vote_at,
               p.joined_at
        FROM votes v
        JOIN players p ON p.user_id=v.target_id AND p.game_id=v.game_id
        WHERE v.game_id=? AND v.day_number=? AND p.is_alive=1
        GROUP BY v.target_id
        ORDER BY cnt DESC, first_vote_at ASC, p.joined_at DESC
    """, (game_id, day_number))
    rows = c.fetchall()
    conn.close()
    return [(r["target_id"], r["cnt"]) for r in rows]


def eliminate_player(game_id, user_id):
    """Выбить игрока из игры"""
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE players SET is_alive=0 WHERE game_id=? AND user_id=?", (game_id, user_id))
    # Воскрешение: проверяем есть ли у игрока и 10+ живых
    c.execute("SELECT COUNT(*) as cnt FROM players WHERE game_id=? AND is_alive=1", (game_id,))
    _alive_for_rez = c.fetchone()["cnt"]
    c.execute("SELECT id FROM items WHERE user_id=? AND item_type='resurrect' AND status='active' LIMIT 1",
              (user_id,))
    rez = c.fetchone()
    if rez and _alive_for_rez > 5:
        # Воскрешаем и тратим предмет
        c.execute("UPDATE players SET is_alive=1 WHERE game_id=? AND user_id=?", (game_id, user_id))
        c.execute("UPDATE items SET status='used' WHERE id=?", (rez["id"],))
        # Анонимус тоже сгорает при воскрешении — заново надо покупать
        c.execute("UPDATE items SET status='used' WHERE user_id=? AND item_type='anon_player' AND status='active'", (user_id,))
        conn.commit()
        conn.close()
        return "resurrected"
    # Списываем анонимус если был
    c.execute("UPDATE items SET status='used' WHERE user_id=? AND item_type='anon_player' AND status='active'",
              (user_id,))
    # Счётчик проигрышей
    c.execute("UPDATE users SET losses=losses+1 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()
    return "eliminated"


def start_tiebreaker(game_id, tied_user_ids, day_number):
    """Сохранить список игроков для переголосования (в виде специального голосования)"""
    conn = get_conn()
    c = conn.cursor()
    # Помечаем день как ничья — просто увеличиваем день
    c.execute("UPDATE games SET current_day=current_day+1 WHERE id=?", (game_id,))
    conn.commit()
    conn.close()


def get_alive_count(game_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) as cnt FROM players WHERE game_id=? AND is_alive=1", (game_id,))
    row = c.fetchone()
    conn.close()
    return row["cnt"] if row else 0


def mark_bot_blocked(user_id):
    """Помечаем что юзер заблокировал бота"""
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE users SET bot_blocked=1 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


def unmark_bot_blocked(user_id):
    """Снимаем флаг — юзер снова доступен"""
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE users SET bot_blocked=0 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


def get_blocked_alive_players(game_id):
    """Возвращает список живых игроков в игре у которых bot_blocked=1"""
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT p.user_id, u.first_name, u.username
        FROM players p JOIN users u ON p.user_id=u.user_id
        WHERE p.game_id=? AND p.is_alive=1 AND u.bot_blocked=1
    """, (game_id,))
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def update_streak(user_id):
    """Обновить стрик при ежедневном входе. Возвращает (streak_days, is_new_day, item_reward)
    День 7  = связь на выбор (уже есть в фронте)
    День 14 = Постанова + Киллер
    День 21 = 250 игровых кредитов в казик
    День 30 = Премиум навсегда
    После 30 — стрик сбрасывается на 1."""
    from datetime import datetime, date
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT streak_days, streak_last FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return 0, False, None

    today = date.today().isoformat()
    streak_last = row["streak_last"]
    streak_days = row["streak_days"] or 0

    if streak_last == today:
        conn.close()
        return streak_days, False, None

    yesterday = (date.today() - __import__('datetime').timedelta(days=1)).isoformat()
    if streak_last == yesterday:
        # Продолжаем стрик — после 30 начинаем заново
        streak_days = 1 if streak_days >= 30 else streak_days + 1
    else:
        streak_days = 1

    # Награды
    item_reward = None
    FAKE_IDS_SET = {9000001, 9000002, 9000003, 9000004}

    c.execute("SELECT id FROM games WHERE status IN ('waiting','active') ORDER BY id DESC LIMIT 1")
    g = c.fetchone()
    game_id = g["id"] if g else None

    if user_id not in FAKE_IDS_SET:
        if streak_days == 3:
            c.execute("INSERT INTO items (user_id, item_type, game_id, status) VALUES (?,?,?,'active')",
                      (user_id, "anon_msg", game_id))
            item_reward = "anon_msg"
        elif streak_days == 5:
            c.execute("INSERT INTO items (user_id, item_type, game_id, status) VALUES (?,?,?,'active')",
                      (user_id, "spy", game_id))
            item_reward = "spy"
        elif streak_days == 7:
            c.execute("INSERT INTO items (user_id, item_type, game_id, status) VALUES (?,?,?,'active')",
                      (user_id, "black_mark", game_id))
            item_reward = "black_mark"
        elif streak_days == 14:
            # Постанова + Киллер
            for it in ["resurrect", "killer"]:
                c.execute("INSERT INTO items (user_id, item_type, game_id, status) VALUES (?,?,?,'active')",
                          (user_id, it, game_id))
            item_reward = "resurrect+killer"
        elif streak_days == 21:
            # 250 игровых кредитов в казик
            try:
                c.execute("CREATE TABLE IF NOT EXISTS casino_credits (user_id INTEGER PRIMARY KEY, credits INTEGER DEFAULT 0, last_free_spin TEXT)")
            except: pass
            c.execute("INSERT INTO casino_credits (user_id, credits) VALUES (?,250) ON CONFLICT(user_id) DO UPDATE SET credits=credits+250",
                      (user_id,))
            item_reward = "casino_250"
        elif streak_days == 30:
            # Премиум навсегда
            c.execute("UPDATE users SET premium_force=1 WHERE user_id=?", (user_id,))
            item_reward = "premium"

    c.execute("UPDATE users SET streak_days=?, streak_last=? WHERE user_id=?",
              (streak_days, today, user_id))
    conn.commit()
    conn.close()
    return streak_days, True, item_reward


def ban_user(username: str) -> bool:
    """Бан по username. Возвращает True если юзер найден."""
    username = username.lstrip("@")
    conn = get_conn(); c = conn.cursor()
    c.execute("UPDATE users SET is_banned=1 WHERE LOWER(username)=LOWER(?)", (username,))
    affected = c.rowcount
    conn.commit(); conn.close()
    return affected > 0

def unban_user(username: str) -> bool:
    username = username.lstrip("@")
    conn = get_conn(); c = conn.cursor()
    c.execute("UPDATE users SET is_banned=0 WHERE LOWER(username)=LOWER(?)", (username,))
    affected = c.rowcount
    conn.commit(); conn.close()
    return affected > 0

def is_user_banned(user_id: int) -> bool:
    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT is_banned FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone(); conn.close()
    return bool(row and row["is_banned"])


def init_auction_table():
    conn = get_conn(); c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS auction_donations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            first_name TEXT,
            amount INTEGER,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit(); conn.close()

def add_auction_donation(user_id, username, first_name, amount):
    conn = get_conn(); c = conn.cursor()
    c.execute("INSERT INTO auction_donations (user_id, username, first_name, amount) VALUES (?,?,?,?)",
              (user_id, username, first_name, amount))
    conn.commit(); conn.close()

def get_auction_top():
    conn = get_conn(); c = conn.cursor()
    c.execute("""
        SELECT user_id, username, first_name, SUM(amount) as total
        FROM auction_donations
        GROUP BY user_id
        ORDER BY total DESC
        LIMIT 3
    """)
    rows = c.fetchall(); conn.close()
    return rows

def clear_auction():
    conn = get_conn(); c = conn.cursor()
    c.execute("DELETE FROM auction_donations")
    conn.commit(); conn.close()


def get_auction_state():
    conn = get_conn(); c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS auction_state (
            id INTEGER PRIMARY KEY DEFAULT 1,
            active INTEGER DEFAULT 0,
            title TEXT DEFAULT '',
            link TEXT DEFAULT '',
            deadline TEXT DEFAULT ''
        )
    """)
    try: c.execute("ALTER TABLE auction_state ADD COLUMN deadline TEXT DEFAULT ''")
    except: pass
    conn.commit()
    c.execute("SELECT active, title, link, deadline FROM auction_state WHERE id=1")
    row = c.fetchone(); conn.close()
    if not row:
        return {"active": False, "title": "", "link": "", "deadline": ""}
    return {"active": bool(row["active"]), "title": row["title"] or "", "link": row["link"] or "", "deadline": row["deadline"] or ""}

def set_auction_state(active, title="", link="", deadline=""):
    conn = get_conn(); c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS auction_state (
            id INTEGER PRIMARY KEY DEFAULT 1,
            active INTEGER DEFAULT 0,
            title TEXT DEFAULT '',
            link TEXT DEFAULT '',
            deadline TEXT DEFAULT ''
        )
    """)
    try: c.execute("ALTER TABLE auction_state ADD COLUMN deadline TEXT DEFAULT ''")
    except: pass
    conn.commit()
    c.execute("INSERT OR REPLACE INTO auction_state (id, active, title, link, deadline) VALUES (1,?,?,?,?)",
              (1 if active else 0, title, link, deadline))
    conn.commit(); conn.close()


# ─── ГЕМЫ ───────────────────────────────────────────────────────────────

def get_gems(user_id: int) -> int:
    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT COALESCE(gems,0) as gems FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone(); conn.close()
    return row["gems"] if row else 0

def add_gems(user_id: int, amount: int):
    conn = get_conn(); c = conn.cursor()
    c.execute("UPDATE users SET gems=COALESCE(gems,0)+? WHERE user_id=?", (amount, user_id))
    conn.commit(); conn.close()

def spend_gems(user_id: int, amount: int) -> bool:
    """Снимает гемы. Возвращает True если успешно, False если не хватает."""
    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT COALESCE(gems,0) as gems FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    if not row or row["gems"] < amount:
        conn.close(); return False
    c.execute("UPDATE users SET gems=gems-? WHERE user_id=?", (amount, user_id))
    conn.commit(); conn.close()
    return True

def create_withdraw_request(user_id: int, username: str, first_name: str, gems: int, user_msg_id: int = None) -> int:
    """Создаёт запрос на вывод. Возвращает id запроса."""
    conn = get_conn(); c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS gem_withdrawals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            first_name TEXT,
            gems INTEGER,
            stars INTEGER,
            status TEXT DEFAULT 'pending',
            user_msg_id INTEGER,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    try:
        c.execute("ALTER TABLE gem_withdrawals ADD COLUMN user_msg_id INTEGER")
        conn.commit()
    except Exception:
        pass
    stars = int(gems * 0.95)
    c.execute(
        "INSERT INTO gem_withdrawals (user_id, username, first_name, gems, stars, user_msg_id) VALUES (?,?,?,?,?,?)",
        (user_id, username, first_name, gems, stars, user_msg_id)
    )
    wid = c.lastrowid
    conn.commit(); conn.close()
    return wid

def get_withdraw_request(wid: int):
    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT * FROM gem_withdrawals WHERE id=?", (wid,))
    row = c.fetchone(); conn.close()
    return dict(row) if row else None

def set_withdraw_status(wid: int, status: str):
    conn = get_conn(); c = conn.cursor()
    c.execute("UPDATE gem_withdrawals SET status=? WHERE id=?", (status, wid))
    conn.commit(); conn.close()

NFT_DROP_THRESHOLD = 2500

def init_nft_counter():
    conn = get_conn(); c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS nft_drop_counter (
            id INTEGER PRIMARY KEY CHECK (id=1),
            total_stars INTEGER DEFAULT 0,
            drops_given INTEGER DEFAULT 0
        )
    """)
    c.execute("INSERT OR IGNORE INTO nft_drop_counter (id, total_stars, drops_given) VALUES (1, 0, 0)")
    conn.commit(); conn.close()

def get_nft_counter():
    init_nft_counter()
    conn = get_conn(); c = conn.cursor()
    row = c.execute("SELECT total_stars, drops_given FROM nft_drop_counter WHERE id=1").fetchone()
    conn.close()
    return dict(row) if row else {"total_stars": 0, "drops_given": 0}

def add_nft_stars(stars: int) -> bool:
    """Добавляет звёзды в счётчик. Возвращает True если случился дроп."""
    init_nft_counter()
    conn = get_conn(); c = conn.cursor()
    c.execute("UPDATE nft_drop_counter SET total_stars=total_stars+? WHERE id=1", (stars,))
    conn.commit()
    row = c.execute("SELECT total_stars, drops_given FROM nft_drop_counter WHERE id=1").fetchone()
    total, drops = row["total_stars"], row["drops_given"]
    new_drops = total // NFT_DROP_THRESHOLD
    if new_drops > drops:
        c.execute("UPDATE nft_drop_counter SET drops_given=? WHERE id=1", (new_drops,))
        conn.commit()
        conn.close()
        return True
    conn.close()
    return False

# ── АВТОРИТЕТЫ ─────────────────────────────────────────────

AUTHORITY_TYPES = ['mayor', 'banker', 'crime_boss', 'cop', 'escort', 'dealer', 'dictator', 'krasotka', 'milf']

def init_authorities():
    conn = get_conn(); c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS authorities (
            authority_type TEXT PRIMARY KEY,
            user_id INTEGER,
            username TEXT,
            first_name TEXT,
            price INTEGER DEFAULT 1,
            bought_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            status_enabled INTEGER DEFAULT 0
        )
    """)
    try:
        c.execute("ALTER TABLE authorities ADD COLUMN status_enabled INTEGER DEFAULT 0")
    except: pass
    conn.commit(); conn.close()

def toggle_authority_status(user_id: int) -> bool:
    """Включить/выключить отображение статуса. Возвращает новое значение (True=вкл)."""
    init_authorities()
    conn = get_conn(); c = conn.cursor()
    row = c.execute("SELECT status_enabled FROM authorities WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        conn.close(); return False
    new_val = 0 if row['status_enabled'] else 1
    c.execute("UPDATE authorities SET status_enabled=? WHERE user_id=?", (new_val, user_id))
    conn.commit(); conn.close()
    return bool(new_val)

def get_all_authorities() -> dict:
    init_authorities()
    conn = get_conn(); c = conn.cursor()
    rows = c.execute("SELECT * FROM authorities").fetchall()
    conn.close()
    result = {}
    for row in rows:
        result[row['authority_type']] = dict(row)
    return result

def get_authority(authority_type: str) -> dict:
    init_authorities()
    conn = get_conn(); c = conn.cursor()
    row = c.execute("SELECT * FROM authorities WHERE authority_type=?", (authority_type,)).fetchone()
    conn.close()
    return dict(row) if row else None

def get_user_authority(user_id: int) -> str:
    """Возвращает тип должности юзера или None"""
    init_authorities()
    conn = get_conn(); c = conn.cursor()
    row = c.execute("SELECT authority_type FROM authorities WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row['authority_type'] if row else None

def set_authority(authority_type: str, user_id: int, username: str, first_name: str, price: int):
    init_authorities()
    conn = get_conn(); c = conn.cursor()
    # Снимаем предыдущую должность этого юзера — обнуляем владельца но цену сохраняем
    c.execute("UPDATE authorities SET user_id=NULL, username=NULL, first_name=NULL, status_enabled=0 WHERE user_id=? AND authority_type!=?", (user_id, authority_type))
    c.execute("""
        INSERT INTO authorities (authority_type, user_id, username, first_name, price, bought_at, status_enabled)
        VALUES (?,?,?,?,?, CURRENT_TIMESTAMP, 1)
        ON CONFLICT(authority_type) DO UPDATE SET
            user_id=excluded.user_id,
            username=excluded.username,
            first_name=excluded.first_name,
            price=excluded.price,
            bought_at=CURRENT_TIMESTAMP,
            status_enabled=1
    """, (authority_type, user_id, username, first_name, price))
    conn.commit(); conn.close()

def remove_authority(authority_type: str):
    init_authorities()
    conn = get_conn(); c = conn.cursor()
    c.execute("DELETE FROM authorities WHERE authority_type=?", (authority_type,))
    conn.commit(); conn.close()


# ─── ЛОГИ ИГР ───────────────────────────────────────────

def log_game(user_id: int, game_type: str, bet: int, result: str, payout: int):
    """
    game_type: 'redblack', 'crash', 'dice_solo', 'dice_pvp'
    result: 'win', 'lose', 'zero', 'draw'
    payout: сколько гемов вернули игроку
    profit = bet - payout (казино заработало)
    """
    try:
        conn = get_conn(); c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS game_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER,
                game_type  TEXT,
                bet        INTEGER,
                result     TEXT,
                payout     INTEGER,
                profit     INTEGER,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        profit = bet - payout
        c.execute(
            "INSERT INTO game_log (user_id, game_type, bet, result, payout, profit) VALUES (?,?,?,?,?,?)",
            (user_id, game_type, bet, result, payout, profit)
        )
        conn.commit(); conn.close()
    except: pass


# ─── ДУЭЛИ ──────────────────────────────────────────────

def init_duels_table():
    conn = get_conn(); c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS duels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            challenger_id INTEGER,
            challenger_name TEXT,
            opponent_id INTEGER,
            opponent_name TEXT,
            bet INTEGER,
            status TEXT DEFAULT 'pending',
            score_challenger INTEGER DEFAULT 0,
            score_opponent INTEGER DEFAULT 0,
            q_challenger INTEGER DEFAULT 0,
            q_opponent INTEGER DEFAULT 0,
            current_turn INTEGER,
            current_question TEXT,
            current_answer TEXT,
            current_options TEXT,
            chat_id TEXT,
            message_id INTEGER,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    for col in ("q_challenger", "q_opponent"):
        try: c.execute(f"ALTER TABLE duels ADD COLUMN {col} INTEGER DEFAULT 0")
        except: pass
    conn.commit(); conn.close()

def create_duel(challenger_id, challenger_name, opponent_id, opponent_name, bet, chat_id):
    init_duels_table()
    conn = get_conn(); c = conn.cursor()
    c.execute("""
        INSERT INTO duels (challenger_id, challenger_name, opponent_id, opponent_name, bet, current_turn, chat_id)
        VALUES (?,?,?,?,?,?,?)
    """, (challenger_id, challenger_name, opponent_id, opponent_name, bet, challenger_id, str(chat_id)))
    did = c.lastrowid
    conn.commit(); conn.close()
    return did

def get_duel(duel_id):
    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT * FROM duels WHERE id=?", (duel_id,))
    row = c.fetchone(); conn.close()
    return dict(row) if row else None

def update_duel(duel_id, **kwargs):
    conn = get_conn(); c = conn.cursor()
    sets = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [duel_id]
    c.execute(f"UPDATE duels SET {sets} WHERE id=?", vals)
    conn.commit(); conn.close()

def get_active_duel_for_user(user_id):
    conn = get_conn(); c = conn.cursor()
    c.execute("""
        SELECT * FROM duels 
        WHERE (challenger_id=? OR opponent_id=?) 
        AND status IN ('pending','active')
        ORDER BY id DESC LIMIT 1
    """, (user_id, user_id))
    row = c.fetchone(); conn.close()
    return dict(row) if row else None
