from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import os, hmac, hashlib, json, asyncio
from urllib.parse import parse_qsl
import httpx

from database import (
    update_streak,
    init_db, get_or_create_user, get_user, update_user_photo, set_chat_joined,
    get_referral_count, get_active_game, get_game_players,
    register_player, get_user_stats, get_user_items, add_item,
    get_user_vote, cast_vote, cast_double_vote, kill_player_by_killer, get_conn, has_bomzh_item,
    set_premium_force, get_user_by_username, get_gender, set_gender,
    add_gems
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# Женские окончания/имена для определения пола
FEMALE_NAMES = {
    'анна','мария','дарья','наталья','елена','ольга','татьяна','светлана','ирина',
    'юлия','екатерина','александра','валентина','людмила','нина','галина','надежда',
    'вера','зоя','лариса','тамара','алина','кристина','виктория','ксения','полина',
    'анастасия','марина','оксана','яна','диана','лилия','инна','жанна','алла',
    'регина','рита','соня','варвара','вика','катя','маша','даша','настя','лена',
    'саша','таня','юля','оля','наташа','света','ира','аня','поля','лиза',
    'elizabeth','anna','maria','kate','julia','sofia','alice','diana','victoria',
    'natalia','elena','olga','dasha','masha','katya','nastya','sonya','vika',
}

def detect_gender(first_name: str) -> str:
    if not first_name:
        return 'male'
    name = first_name.lower().strip().split()[0]
    if name in FEMALE_NAMES:
        return 'female'
    # По окончанию
    if name.endswith(('а','я','ия','ья','ка','на','ла','ра','та','ша','жа','га','да','ва','за')):
        return 'female'
    return 'male'


WEBAPP_URL = os.getenv("WEBAPP_URL", "https://shrimpgames.zabeyda.lol")
ADMIN_ID = 7308147004
FAKE_IDS = {9000001, 9000002, 9000003, 9000004}  # тестовые боты — без наград
LOG_GROUP_ID = int(os.getenv('LOG_GROUP_ID', '0'))
CHAT_USERNAME = "shrimpgames_chat"
BOT_LINK = '\n\n<a href="https://t.me/shrimpgamesbot">@shrimpgamesbot</a>'

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app):
    """Фоновый авторезолв для реальных игр — каждую минуту проверяет время"""
    _state = {"reminder_day": None}

    async def auto_resolve_loop():
        import httpx as _hx
        while True:
            try:
                await asyncio.sleep(60)
                game = get_active_game()
                if not game or game["status"] != "active":
                    continue
                if not game["voting_ends"]:
                    continue
                from datetime import datetime as _dt2, timedelta as _td2
                voting_ends = _dt2.strptime(game["voting_ends"], "%Y-%m-%d %H:%M:%S")
                now_utc = _dt2.utcnow()
                remaining = (voting_ends - now_utc).total_seconds()

                # Напоминалка за 2 минуты (120-180 сек до конца)
                current_day = game["current_day"]
                if 120 <= remaining <= 180 and _state["reminder_day"] != current_day:
                    _state["reminder_day"] = current_day
                    try:
                        async with _hx.AsyncClient() as _cl_r:
                            players = get_game_players(game["id"], alive_only=True)
                            voted_ids = []
                            try:
                                from database import get_conn as _gcr
                                _cr = _gcr(); _cur = _cr.cursor()
                                _cur.execute("SELECT DISTINCT voter_id FROM votes WHERE game_id=? AND day_number=?",
                                             (game["id"], current_day))
                                voted_ids = [r["voter_id"] for r in _cur.fetchall()]
                                _cr.close()
                            except: pass
                            for p in players:
                                if p["user_id"] in voted_ids:
                                    continue  # уже проголосовал
                                if p["user_id"] in [9000001,9000002,9000003,9000004]:
                                    continue
                                if not check_notifications(p["user_id"]):
                                    continue
                                try:
                                    await _cl_r.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                                        json={"chat_id": p["user_id"],
                                              "text": "⏰ <b>Осталось 2 минуты!</b> Ты ещё не проголосовал в этом раунде. Успей!",
                                              "parse_mode": "HTML",
                                              "reply_markup": {"inline_keyboard": [[{"text": "🗳 Голосовать", "web_app": {"url": WEBAPP_URL}}]]}})
                                except: pass
                    except: pass

                if now_utc < voting_ends:
                    continue  # Ещё не время
                # Время вышло — резолвим
                async with _hx.AsyncClient() as cl:
                    await cl.post(
                        f"http://localhost:{os.getenv('PORT', '8007')}/api/game/resolve_votes",
                        json={"admin_key": str(ADMIN_ID)},
                        timeout=30
                    )
            except Exception as e:
                pass

    task = asyncio.create_task(auto_resolve_loop())
    yield
    task.cancel()

app = FastAPI(lifespan=lifespan)
async def auto_create_next_game(finished_game_id: int):
    """Через 5 минут после финала — создать новую игру с очередью"""
    await asyncio.sleep(5 * 60)
    try:
        conn = get_conn(); c = conn.cursor()
        # Проверяем что нет уже новой активной/waiting игры
        c.execute("SELECT id FROM games WHERE status IN ('active','waiting') AND id != ?", (finished_game_id,))
        if c.fetchone():
            conn.close(); return
        # Берём номер новой игры (игнорируем тестовые номера >=90)
        c.execute("SELECT MAX(number) as mx FROM games WHERE number < 90")
        row = c.fetchone()
        next_num = (row["mx"] or 0) + 1
        # Создаём новую игру
        c.execute("INSERT INTO games (number, status) VALUES (?, 'waiting')", (next_num,))
        new_game_id = c.lastrowid
        # Переносим очередь в новую игру
        c.execute("SELECT user_id FROM next_game_queue")
        queued = [r["user_id"] for r in c.fetchall()]
        added = 0
        for uid in queued:
            try:
                c.execute("INSERT OR IGNORE INTO players (game_id, user_id) VALUES (?,?)", (new_game_id, uid))
                if c.rowcount > 0:
                    added += 1
            except: pass
        # Очищаем очередь
        c.execute("DELETE FROM next_game_queue")
        conn.commit(); conn.close()
        # Уведомляем в чат
        names_preview = ""
        async with httpx.AsyncClient(timeout=10) as cl:
            await cl.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": "@shrimpgames_chat",
                    "text": (
                        f"🗡 <b>Стрелка #{next_num} — Регистрация открыта!</b>\n\n"
                        f"👥 Уже записалось: {added}\n"
                        f"Заходи в бота и жми Участвовать!"
                    ),
                    "parse_mode": "HTML"
                })
    except Exception as e:
        pass




BUY_CHAT_MSGS = {
    "anon_msg":    [
        "🤫 Кто-то сговорился с нужным человеком...",
        "📩 Анонимное послание уже летит к адресату",
        "🤫 Тайная записка ушла по адресу. Кто-то плетёт интриги",
        "📩 Кто-то шепнул на ухо нужному игроку. Заговор в действии",
        "🤝 Сговор состоялся. Кто с кем — никто не знает",
        "🤫 Анонимка отправлена. Кто-то готовит ход втихую",
        "📨 Послание ушло в темноту. Получатель уже читает",
        "🗣 Кто-то нашёл союзника через анонимный канал",
        "🤫 Тихий сговор. Кто-то действует за спинами остальных",
        "📩 Конверт передан. Содержимое — только для своих",
    ],
    "spy":         [
        "🐭 В игре завёлся стукач. Кто-то узнает правду...",
        "🐭 Кто-то внедрил своего человека. Секреты под угрозой",
        "🕵️ Шпион на районе. Чужие голоса уже считают",
        "🐭 Кто-то купил информацию. Теперь знает больше всех",
        "👁 Стукач вышел на охоту. Чьи секреты раскроют?",
        "🐭 Крыса среди нас. Кто-то сольёт всю правду",
        "🕵️ Разведка куплена. Кто-то знает кто за него голосовал",
        "🐭 Стукач активирован. Информация уже утекает",
        "👁 Кто-то купил глаза на районе. Теперь ничего не скроешь",
        "🐭 Шпион внедрён. Тайны голосования больше нет",
    ],
    "black_mark":  [
        "🚔 Кто-то купил Мусорнуться. Анонимус нервничает",
        "🚔 Заява куплена. Кто-то готовится настучать на Анонимуса",
        "🚨 Кто-то стучит в мусарню. Анонимус скоро будет раскрыт",
        "🚔 Заява написана. Маска слетит с одного из игроков",
        "👮 Кто-то сдал Анонимуса ментам. Имя узнают",
        "🚔 Мусорнулся — настучал куда надо. Анонимус на прицеле",
        "🚨 Донос отправлен. Один игрок потеряет анонимность",
        "🚔 Кто-то пошёл в ментовку. Маска скоро слетит",
        "👮 Стукач пошёл по-крупному — сдаёт анонима с потрохами",
        "🚔 Заяву приняли. Анонимус скоро будет раскрыт",
    ],
    "anon_player": [
        "👻 Кто-то собирается раствориться в тени...",
        "🥷 Скоро один из игроков станет Анонимусом",
        "👻 Маска куплена. Один игрок скроет своё имя",
        "🥷 Кто-то уходит в тень. Имя исчезнет из списков",
        "👤 Один игрок стал невидимкой. Голосуй вслепую",
        "👻 Анонимус среди нас. Кто под маской — загадка",
        "🥷 Кто-то надел маску. Теперь его не вычислить",
        "👻 Один из игроков растворился в темноте района",
        "🎭 Маска надета. Анонимус смешался с толпой",
        "👤 Кто-то скрыл лицо. Голосуй за кота в мешке",
    ],
    "double_vote": [
        "🔫 Кто-то зарядил двустволку. Дуплет на подходе",
        "🔫 Двустволка куплена — чей-то голос скоро удвоится",
        "💥 Дуплет готов. Один голос ударит дважды",
        "🔫 Двустволка заряжена. Кто-то целится в конкурента",
        "💣 Двойной выстрел на подходе. Жертва уже выбрана",
        "🔫 Кто-то купил двойной патрон. Один голос — два удара",
        "💥 Двустволка взведена. Чьи-то шансы вылететь удвоились",
        "🔫 Заряжено и готово. Кто-то откроет дуплет по врагу",
        "💥 Двойной удар куплен. Цель уже на прицеле",
        "🔫 Кто-то не хочет промахнуться — взял двустволку",
    ],
    "hacker":      [
        "🦹 Ворюга вышел на дело. Чьи голоса украдут?",
        "💰 Кто-то занёс вору. Голоса исчезнут бесследно",
        "🦹 Ворюга на охоте. Голоса против кого-то сгорят",
        "💰 Кража голосов запланирована. Ворюга уже работает",
        "🦹 Кто-то нанял вора. Голоса против него исчезнут",
        "💸 Ворюга взломал систему. Чьи-то голоса обнулятся",
        "🦹 Дерзкая кража на районе. Голоса украдут прямо с раунда",
        "💰 Ворюга получил аванс. Работает чисто, без следов",
        "🦹 Кто-то решил не рисковать — нанял ворюгу на подчистку",
        "💸 Голоса утекут в никуда. Ворюга уже считает чужие карманы",
    ],
    "tiebreaker":  [
        "⚖️ Решала на столе. При ничье всё решится мгновенно",
        "⚖️ Кто-то купил Решалу — ничья ему не страшна",
        "⚖️ Решала в кармане. При равном счёте он выберет первый",
        "🎯 Ничья? Не для всех. Решала уже куплен",
        "⚖️ Кто-то подстраховался. Решала решит всё при ничье",
        "🃏 Козырь куплен. При равных голосах — победит первый",
        "⚖️ Решала активирован. Ничья больше не угрожает своему",
        "🎯 Кто-то не боится ничьей — у него Решала",
        "⚖️ Страховка от ничьей куплена. Первый голос решит всё",
        "🃏 Решала на руках. При спорной ситуации — он судья",
    ],
    "shield":      [
        "🤵 Кто-то крышанулся. В следующем раунде его не тронут",
        "🤵 Крыша куплена — один игрок вне досягаемости",
        "🛡 Кто-то купил защиту. В следующем раунде его нет в списках",
        "🤵 Крыша взята. Голосуй за кого угодно — этого не достать",
        "🛡 Один игрок ушёл под крышу. Следующий раунд он в безопасности",
        "🤵 Серьёзные связи куплены. Один игрок неприкосновенен",
        "🛡 Крыша работает. Следующий раунд без этого игрока в списке",
        "🤵 Кто-то вложился в безопасность. Следующий раунд — иммунитет",
        "🛡 Один из игроков недосягаем. Крыша куплена",
        "🤵 Серьёзный человек взял крышу. В следующем раунде его не тронут",
    ],
    "killer":      [
        "💀 Кто-то заказал киллера. Берегитесь...",
        "🔫 В игре появился наёмный убийца. Кто следующий?",
        "💀 Контракт подписан. Киллер ищет цель",
        "🔪 Кто-то нанял профессионала. Один из игроков в опасности",
        "💀 Заказ оформлен. Киллер уже вышел на охоту",
        "🩸 Наёмник получил аванс. Жертва ещё не знает",
        "💀 Кто-то решил не ждать голосований. Киллер куплен",
        "🔫 Профессионал нанят. Чья-то жизнь в игре висит на волоске",
        "💀 Тихая ликвидация запланирована. Киллер на позиции",
        "🩸 Заказ принят. Кто-то скоро вылетит без голосований",
    ],
    "resurrect":   [
        "🎭 Кто-то готовит постанову. Смерть — не конец",
        "🎭 Один игрок инсценирует смерть если что",
        "🎭 Постанова куплена. Кто-то сыграет смерть как актёр",
        "🎬 Один игрок готов инсценировать гибель. Подставного найдут",
        "🎭 Страховка от вылета куплена. Смерть — это ещё не конец",
        "🎬 Кто-то купил второй шанс. При вылете — подставят другого",
        "🎭 Постанова активна. Если выберут — вылетит кто-то другой",
        "🎬 Один игрок готов умереть понарошку. Настоящая жертва — сосед",
        "🎭 Кто-то застраховался по-крупному. Вылет отменяется",
        "🎬 Постанова в деле. Смерть сыграна — выживший вернётся",
    ],
}


ABILITY_USE_MSGS = {
    "anon_msg":    [
        "🤫 Анонимное послание отправлено...",
        "📩 Кто-то сговорился. Послание ушло",
        "🤫 Записка доставлена адресату. Сговор состоялся",
        "📨 Послание ушло в темноту. Кто-то строит планы",
        "🤝 Анонимный контакт установлен. Игра пошла в глубину",
        "🤫 Сговор оформлен. Кто-то уже знает больше всех",
        "📩 Тайная связь налажена. Послание дошло",
        "🗣 Слово передано. Кто получил — тот знает что делать",
        "🤫 Анонимка в руках адресата. Чей-то план приходит в действие",
        "📨 Сговорились. Остальные пока не догадываются",
    ],
    "spy":         [
        "🐭 Стукач активирован. Кто-то узнал правду о голосах",
        "🐭 Информация получена. Стукач сделал своё дело",
        "🕵️ Разведка отработала. Кто-то знает всё о голосах против",
        "🐭 Стукач слил данные. Теперь кто-то видит всю картину",
        "👁 Информация в руках. Шпион справился с задачей",
        "🐭 Крыса донесла. Один игрок знает кто против него голосовал",
        "🕵️ Разведка завершена. Кто-то вооружён информацией",
        "🐭 Стукач выполнил заказ. Тайное стало явным",
        "👁 Шпион сработал чисто. Голоса посчитаны и слиты",
        "🐭 Агент отчитался. Кто-то знает всё что нужно",
    ],
    "black_mark":  [
        "🚔 Кто-то мусорнулся и накатал заяву на Анонимуса — он узнал его настоящее имя!",
        "🚔 Один из кентов настучал в мусарню на Анонимуса. Теперь он знает кто под маской!",
        "🚨 Заява принята. Анонимус раскрыт — маска сорвана!",
        "🚔 Мусорнулся по полной. Анонимус больше не анонимус — имя известно!",
        "👮 Стукач донёс куда надо. Теперь один игрок знает кто прячется за маской",
        "🚔 Донос сработал. Анонимус раскрыт перед тем кто настучал",
        "🚨 Заява ушла в работу. Маска сорвана — имя Анонимуса раскрыто!",
        "🚔 Кто-то сходил в мусарню не зря. Анонимус вычислен",
        "👮 Настучали на Анонимуса. Теперь его знают в лицо",
        "🚔 Мусорнулся и не зря — маска слетела. Анонимус раскрыт!",
    ],
    "anon_player": [
        "🥷 Один из игроков растворился в тени...",
        "👻 Анонимус среди нас. Имя скрыто",
        "🎭 Маска надета. Один игрок стал Анонимусом",
        "👤 Кто-то исчез в тени. Имя больше не видно",
        "👻 Анонимус в игре. Голосуй вслепую",
        "🥷 Один из нас скрыл лицо. Кто — неизвестно",
        "🎭 Анонимус активирован. Имя игрока скрыто до конца раунда",
        "👤 Маска работает. Один игрок невидим для остальных",
        "👻 Тень среди нас. Анонимус растворился в списке",
        "🥷 Имя скрыто. Кто под маской — ваша проблема",
    ],
    "double_vote": [
        "🔫 Двустволка взведена. Один голос станет двумя",
        "🔫 Дуплет готов — чей-то голос удвоится",
        "💥 Двойной выстрел произведён. Один голос — два удара",
        "🔫 Дуплет выпущен. Кто-то получил двойную порцию",
        "💣 Двустволка сработала. Голос удвоен — жертва получила вдвойне",
        "🔫 Бах-бах. Двойной голос засчитан",
        "💥 Оба ствола выстрелили. Голос удвоен",
        "🔫 Дуплет в цель. Один игрок получил два голоса против",
        "💣 Двустволка отработала. Кто-то проголосовал с двойной силой",
        "🔫 Двойной удар нанесён. Цель под огнём",
    ],
    "hacker":      [
        "💰 Ворюга вышел на охоту. Голоса исчезнут бесследно",
        "🦹 Вор получил заказ. Чьи голоса украдут?",
        "🦹 Ворюга сработал. Голоса против кого-то обнулены",
        "💸 Кража состоялась. Голоса испарились — следов нет",
        "🦹 Вор взломал систему. Чьи-то голоса удалены",
        "💰 Ворюга зачистил. Голоса против одного игрока исчезли",
        "🦹 Чистая работа. Голоса украдены — никто не заметил",
        "💸 Ворюга отработал без следов. Голоса сгорели",
        "🦹 Взлом завершён. Кто-то избавился от голосов против",
        "💰 Деньги сделали дело. Голоса обнулены",
    ],
    "tiebreaker":  [
        "⚖️ Решала активирован — ничья больше не страшна",
        "⚖️ При ничье всё решится мгновенно",
        "🃏 Решала вступил в игру. Ничья разрулена",
        "⚖️ Первый голос решил всё. Решала сработал",
        "🎯 Ничья? Не в этот раз. Решала разобрался",
        "⚖️ Спорный вопрос закрыт. Решала принял решение",
        "🃏 Ничья разрешена. Первый голос стал решающим",
        "⚖️ Решала отработал. Ничья позади — есть победитель",
        "🎯 Козырная карта сыграна. Решала завершил спор",
        "⚖️ Вопрос решён. Ничья осталась в прошлом",
    ],
    "shield":      [
        "🤵 Крышануться активирована. Один игрок вне досягаемости",
        "🤵 Крыша работает — в следующем раунде его не тронут",
        "🛡 Крыша активна. В следующем раунде этого игрока нет в списках",
        "🤵 Защита включена. Один игрок недосягаем для голосования",
        "🛡 Крышануться сработала. Следующий раунд — иммунитет",
        "🤵 Один игрок под крышей. Голосовать против него бесполезно",
        "🛡 Крыша держит. В следующем раунде он в безопасности",
        "🤵 Иммунитет активирован. Один игрок выпал из списка на раунд",
        "🛡 Защита работает. Следующий раунд этого игрока не достать",
        "🤵 Крыша куплена и активна. Неприкосновенность гарантирована",
    ],
    "killer":      [
        "💀 Киллер получил заказ. Кто-то уже выбыл...",
        "🔫 В игре замечен киллер. Один участник устранён",
        "💀 Контракт выполнен. Один игрок покинул район навсегда",
        "🩸 Киллер отработал. Жертва выбыла без голосований",
        "💀 Чистая работа. Один игрок устранён по заказу",
        "🔫 Наёмник справился. Ещё один участник выбыл досрочно",
        "💀 Киллер закрыл контракт. На районе стало тише",
        "🩸 Профессионал выполнил заказ. Минус один игрок",
        "💀 Устранение завершено. Кто-то заплатил — кто-то вылетел",
        "🔫 Киллер сделал дело. Район не досчитался одного игрока",
    ],
    "resurrect":   [
        "🎭 Постанова готова. Смерть — не конец",
        "🎭 Один игрок инсценирует смерть — вернётся если что",
        "🎬 Постанова сработала. Один выжил — другой вылетел вместо него",
        "🎭 Инсценировка удалась. Настоящая жертва — не тот кого выбирали",
        "🎬 Смерть сыграна. Один игрок остался — другой ушёл вместо него",
        "🎭 Постанова в действии. Подставной нашёлся — хозяин выжил",
        "🎬 Актёр сыграл смерть убедительно. Вылетел совсем другой",
        "🎭 Второй шанс использован. Постанова спасла игрока",
        "🎬 Смерть обманута. Один остался — другой вылетел вместо",
        "🎭 Постанова отработала. Выбирали одного — вылетел другой",
    ],
}

async def notify_ability_activate(item_type: str, game_id: int = None, username: str = None):
    ITEM_NMS = {"anon_msg":"Сговориться","spy":"Стукач","black_mark":"Мусорнуться","anon_player":"Анонимус","double_vote":"Двустволка","hacker":"Ворюга","tiebreaker":"Решала","shield":"Крышануться","killer":"Заказать Киллера","resurrect":"Постанова"}
    ITEM_ICONS = {"anon_msg":"📩","spy":"🐭","black_mark":"🚔","anon_player":"👻","double_vote":"🔫","hacker":"💰","tiebreaker":"⚖️","shield":"🤵","killer":"💀","resurrect":"🎭"}
    nm = ITEM_NMS.get(item_type, item_type)
    ic = ITEM_ICONS.get(item_type, "🗡")
    if game_id:
        push_event(game_id, "use", f"{ic} Кто-то активировал связь {nm}", ic)
    msgs = ABILITY_USE_MSGS.get(item_type)
    if not msgs:
        return
    import random as _ra2
    msg = _ra2.choice(msgs)
    # Добавляем имя кроме анонимуса
    if item_type != "anon_player" and username:
        msg = f"{username}: {msg}"
    try:
        async with httpx.AsyncClient(timeout=8) as _cl3:
            await _cl3.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": "@shrimpgames_chat", "text": msg, "parse_mode": "HTML"})
    except: pass


async def log_event(cl, text: str):
    """Отправить лог в приватную группу — отключено"""
    return  # отключено

def get_display_name(user_id: int) -> str:
    """Возвращает имя юзера или 'Анонимус' если у него активен anon_player"""
    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT id FROM items WHERE user_id=? AND item_type='anon_player' AND status='active' LIMIT 1", (user_id,))
    is_anon = c.fetchone() is not None
    if is_anon:
        conn.close()
        return "Анонимус"
    c.execute("SELECT first_name, username FROM users WHERE user_id=?", (user_id,))
    u = c.fetchone()
    conn.close()
    return (u["first_name"] or u["username"] or "Игрок") if u else "Игрок"

def push_event(game_id: int, event_type: str, text: str, icon: str = "🗡"):
    """Записать событие в ленту игры"""
    try:
        conn = get_conn(); c = conn.cursor()
        c.execute("INSERT INTO game_events (game_id, event_type, text, icon) VALUES (?,?,?,?)",
                  (game_id, event_type, text, icon))
        # Оставляем только последние 50 событий на игру
        c.execute("DELETE FROM game_events WHERE game_id=? AND id NOT IN (SELECT id FROM game_events WHERE game_id=? ORDER BY id DESC LIMIT 50)",
                  (game_id, game_id))
        conn.commit(); conn.close()
    except: pass
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory="static"), name="static")
init_db()
# Добавляем колонки если нет
_conn_init = get_conn(); _c_init = _conn_init.cursor()
for _col, _def in [("votes_cast","INTEGER DEFAULT 0"),("items_used","INTEGER DEFAULT 0"),("items_won","INTEGER DEFAULT 0"),("top3","INTEGER DEFAULT 0")]:
    try: _c_init.execute(f"ALTER TABLE users ADD COLUMN {_col} {_def}")
    except: pass
_conn_init.commit(); _conn_init.close()


ITEMS = {
    "shield":      {"name": "Крышануться",          "stars": 2},
    "double_vote": {"name": "Двустволка",     "stars": 15},
    "resurrect":   {"name": "Постанова",       "stars": 40},
    "killer":      {"name": "Заказать Киллера",  "stars": 35},
    "spy":         {"name": "Стукач",             "stars": 3},
    "anon_msg":    {"name": "Сговориться",          "stars": 1},
    "tiebreaker":  {"name": "Решала",            "stars": 25},
    "anon_player": {"name": "Анонимус",           "stars": 10},
    "hacker":      {"name": "Ворюга",               "stars": 20},
    "black_mark":  {"name": "Мусорнуться",       "stars": 5},
}


def check_notifications(user_id: int) -> bool:
    """Проверить включены ли уведомления у юзера"""
    try:
        conn = get_conn()
        c = conn.cursor()
        c.execute("SELECT notifications_enabled FROM users WHERE user_id=?", (user_id,))
        row = c.fetchone()
        conn.close()
        if row and row["notifications_enabled"] == 0:
            return False
    except: pass
    return True


def verify_tg(init_data: str):
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        received = parsed.pop("hash", "")
        check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        expected = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
        return parsed if hmac.compare_digest(expected, received) else None
    except:
        return None


async def fetch_photo(user_id, bot_token):
    try:
        async with httpx.AsyncClient() as cl:
            r = await cl.get(f"https://api.telegram.org/bot{bot_token}/getUserProfilePhotos",
                             params={"user_id": user_id, "limit": 1})
            d = r.json()
            if d.get("ok") and d["result"]["total_count"] > 0:
                fid = d["result"]["photos"][0][0]["file_id"]
                r2 = await cl.get(f"https://api.telegram.org/bot{bot_token}/getFile", params={"file_id": fid})
                fp = r2.json()["result"]["file_path"]
                return f"https://api.telegram.org/file/bot{bot_token}/{fp}"
    except: pass
    return None


async def check_chat_member(user_id: int) -> bool:
    """Проверить подписан ли юзер на канал через Bot API"""
    try:
        async with httpx.AsyncClient() as cl:
            r = await cl.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getChatMember",
                params={"chat_id": f"@{CHAT_USERNAME}", "user_id": user_id}
            )
            d = r.json()
            if d.get("ok"):
                status = d["result"]["status"]
                return status in ("member", "administrator", "creator")
    except: pass
    return False


@app.get("/")
async def index():
    from fastapi.responses import Response
    import time
    with open("index.html", "rb") as f:
        data = f.read()
    return Response(content=data, media_type="text/html", headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"})


def _check_top10(user_id: int) -> bool:
    """Проверяет попадает ли юзер в топ-10 по любой категории за последние 3 игры"""
    try:
        conn_t = get_conn(); c = conn_t.cursor()
        c.execute("SELECT id FROM games WHERE status IN ('finished','active') ORDER BY id DESC LIMIT 3")
        last_games = [r["id"] for r in c.fetchall()]
        if not last_games:
            conn_t.close(); return False
        ph = ','.join('?'*len(last_games))
        # Топ по голосам
        c.execute(f"SELECT voter_id FROM votes WHERE game_id IN ({ph}) GROUP BY voter_id ORDER BY COUNT(*) DESC LIMIT 10", last_games)
        if user_id in [r["voter_id"] for r in c.fetchall()]:
            conn_t.close(); return True
        # Топ по убийствам
        c.execute(f"SELECT user_id FROM users ORDER BY kills DESC LIMIT 10")
        if user_id in [r["user_id"] for r in c.fetchall()]:
            conn_t.close(); return True
        # Топ по играм
        c.execute(f"SELECT user_id FROM players WHERE game_id IN ({ph}) GROUP BY user_id ORDER BY COUNT(*) DESC LIMIT 10", last_games)
        if user_id in [r["user_id"] for r in c.fetchall()]:
            conn_t.close(); return True
        conn_t.close()
    except: pass
    return False


@app.post("/api/auth")
async def auth(request: Request):
    body = await request.json()
    init_data = body.get("initData", "")
    ref_by = body.get("ref_by")

    if not init_data and os.getenv("DEV_MODE"):
        user_id, username, first_name = ADMIN_ID, "rzabeyda", "Roman"
    else:
        parsed = verify_tg(init_data)
        if not parsed:
            return JSONResponse({"ok": False, "error": "Invalid"}, status_code=403)
        ud = json.loads(parsed.get("user", "{}"))
        user_id = ud.get("id")
        username = ud.get("username", "")
        first_name = ud.get("first_name", "")

    get_or_create_user(user_id, username, first_name, ref_by)
    streak_days, is_new_day, streak_item = update_streak(user_id)
    photo = await fetch_photo(user_id, BOT_TOKEN)
    if photo:
        update_user_photo(user_id, photo)

    user = get_user(user_id)
    stats = get_user_stats(user_id)
    items = get_user_items(user_id)
    _bc = get_conn(); _bcc = _bc.cursor()
    _bcc.execute("SELECT COUNT(*) as cnt FROM bomzh_items WHERE user_id=?", (user_id,))
    bomzh_items_count = _bcc.fetchone()["cnt"]
    _bcc.execute("SELECT COUNT(*) as cnt FROM bomzh_donations WHERE user_id=?", (user_id,))
    bomzh_donations_count = _bcc.fetchone()["cnt"]
    try:
        _bcc.execute("CREATE TABLE IF NOT EXISTS bomzh_attacks (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, username TEXT, victim TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        _bcc.execute("SELECT COUNT(*) as cnt FROM bomzh_attacks WHERE user_id=?", (user_id,))
        bomzh_attacks_count = _bcc.fetchone()["cnt"]
    except: bomzh_attacks_count = 0
    _bc.close()
    game = get_active_game()

    in_game = False
    voted_today = False
    voted_target_name = None
    game_status = "none"
    game_number = 1
    current_day = 0
    player_count = 0

    if game:
        game_status = game["status"]
        game_number = game["number"] or 1
        current_day = game["current_day"] or 0
        players = get_game_players(game["id"])
        player_count = len(players)
        in_game = any(p["user_id"] == user_id for p in players)
        _my_player = next((p for p in players if p["user_id"] == user_id), None)
        is_alive = _my_player["is_alive"] if _my_player else 0
        if game_status == "active" and current_day > 0:
            v = get_user_vote(game["id"], current_day, user_id)
            voted_today = v is not None
            if v:
                try:
                    conn_vt = get_conn(); c_vt = conn_vt.cursor()
                    c_vt.execute("SELECT first_name, username FROM users WHERE user_id=?", (v["target_id"],))
                    vt_u = c_vt.fetchone()
                    # Проверяем анонимус у цели
                    anon_check = get_user_items(v["target_id"], game["id"])
                    is_target_anon = any(i["item_type"] == "anon_player" for i in anon_check)
                    if is_target_anon:
                        voted_target_name = "Анонимус"
                    else:
                        voted_target_name = (vt_u["first_name"] or vt_u["username"] or "Игрок") if vt_u else "Игрок"
                    conn_vt.close()
                except: voted_target_name = None
            else:
                voted_target_name = None

    # Реальное кол-во игр из таблицы players
    try:
        _gc = get_conn(); _gcc = _gc.cursor()
        _gcc.execute("SELECT COUNT(*) as cnt FROM players p JOIN games g ON p.game_id=g.id WHERE p.user_id=? AND g.status='finished' AND (SELECT COUNT(*) FROM players WHERE game_id=g.id) >= 6", (user_id,))
        _gr = _gcc.fetchone()
        db_games_played = _gr["cnt"] if _gr else 0
        _gc.close()
    except: db_games_played = stats["games_played"]

    return {
        "ok": True,
        "user": {
            "user_id": user["user_id"],
            "username": user["username"],
            "first_name": user["first_name"],
            "photo_url": user["photo_url"],
            "gender": get_gender(user_id),  # None если не задан — фронт покажет выбор
            "ref_count": get_referral_count(user_id),
            "games_played": db_games_played,
            "games_played_raw": stats["games_played"],
            "kills": stats["kills"],
            "wins": stats["wins"],
            "votes_cast": stats.get("votes_cast", 0),
            "items_used": stats.get("items_used", 0),
            "used_spy": stats.get("used_spy", 0),
            "used_killer": stats.get("used_killer", 0),
            "bought_shield": stats.get("bought_shield", 0),
            "used_double_vote": stats.get("used_double_vote", 0),
            "resurrected": stats.get("resurrected", 0),
            "losses": stats.get("losses", 0),
            "went_anon": stats.get("went_anon", 0),
            "won_as_anon": stats.get("won_as_anon", 0),
            "clean_wins": stats.get("clean_wins", 0),
            "first_joins": stats.get("first_joins", 0),
            "sent_anon": stats.get("sent_anon", 0),
            "times_voted_against": stats.get("times_voted_against", 0),
            "killed_by_killer": stats.get("killed_by_killer", 0),
            "total_purchases": stats.get("items_bought", 0),
            "items_bought": stats.get("items_bought", 0),
            "items_won": stats.get("items_won", 0),
            "items_used": stats.get("items_used", 0),
            "distinct_bought": stats.get("distinct_bought", 0),
            "is_premium": stats.get("is_premium", False),
            "premium_icon": stats.get("premium_icon", None),
            "first_eliminated": stats.get("first_eliminated", 0),
            "created_clan": stats.get("created_clan", 0),
            "joined_clan": stats.get("joined_clan", 0),
            "chat_joined": stats["chat_joined"],
            "items": [{"id": i["id"], "type": i["item_type"], "status": i.get("status","active")} for i in items],
            "in_game": in_game,
            "is_alive": is_alive if in_game else 1,
            "voted_today": voted_today,
            "voted_target_name": voted_target_name if voted_today else None,
            "is_admin": user_id == ADMIN_ID,
            "in_top10": _check_top10(user_id),
            "bomzh_items_count": bomzh_items_count,
            "bomzh_donations_count": bomzh_donations_count,
            "bomzh_attacks_count": bomzh_attacks_count,
            "streak_days": streak_days,
            "streak_new_day": is_new_day,
            "streak_item": streak_item,
            "gems_claimed": int(user["gems_claimed"]) if user["gems_claimed"] else 0,
            "gems_max_purchase": int(user["gems_max_purchase"]) if user["gems_max_purchase"] else 0,
        },
        "game": {
            "game_id": game["id"] if game else None,
            "status": game_status,
            "number": game_number,
            "current_day": current_day,
            "player_count": player_count,
            "voting_ends_ms": (lambda: __import__('calendar').timegm(__import__('datetime').datetime.strptime(game["voting_ends"], "%Y-%m-%d %H:%M:%S").timetuple()) * 1000 if game and game["voting_ends"] and game["status"]=="active" else None)(),
        }
    }


@app.get("/api/prize")
async def prize():
    game = get_active_game()
    if game:
        players = get_game_players(game["id"])
        return {
            "ok": True,
            "game_number": game["number"] or 1,
            "prize_desc": game["prize_desc"],
            "prize_link": game["prize_link"],
            "status": game["status"],
            "player_count": len(players),
        }
    return {"ok": True, "game_number": 1, "prize_desc": "NFT Giraffe Pool Float",
            "prize_link": "https://t.me/nft/PoolFloat-148562", "status": "waiting", "player_count": 0}


@app.get("/api/game/players")
async def game_players():
    game = get_active_game()
    if not game or game["status"] not in ("active","waiting"):
        return {"ok": False, "error": "Нет активной игры"}
    players = get_game_players(game["id"])
    # Подтягиваем premium_icon для каждого игрока
    conn = get_conn(); c = conn.cursor()
    try:
        c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    except: pass
    for p in players:
        try:
            r = c.execute("SELECT value FROM settings WHERE key=?", (f"premium_icon_{p['user_id']}",)).fetchone()
            p["premium_icon"] = r["value"] if r else None
        except: p["premium_icon"] = None
    conn.close()
    return {
        "ok": True, "game_id": game["id"],
        "game_number": game["number"] or 1,
        "status": game["status"],
        "winner_id": game["winner_id"] if "winner_id" in game.keys() else None,
        "max_players": game["max_players"] or 0,
        "current_day": game["current_day"] or 0,
        "players": players, "count": len(players),
    }


@app.get("/api/game/voted_ids")
async def game_voted_ids():
    """Кто уже проголосовал в текущем раунде"""
    game = get_active_game()
    if not game or game["status"] != "active":
        return {"ok": False, "voted_ids": []}
    day = game["current_day"] or 1
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT DISTINCT voter_id FROM votes WHERE game_id=? AND day_number=?", (game["id"], day))
    voted = [r["voter_id"] for r in c.fetchall()]
    conn.close()
    return {"ok": True, "voted_ids": voted, "day": day}


@app.get("/api/game/shielded_ids")
async def game_shielded_ids():
    """Игроки у которых активна крыша в текущем раунде — их нет в списке голосования"""
    game = get_active_game()
    if not game or game["status"] != "active":
        return {"ok": True, "shielded_ids": []}
    game_id = game["id"]
    day = game["current_day"] or 1
    conn = get_conn()
    c = conn.cursor()
    # Ручная активация через settings
    c.execute("SELECT key FROM settings WHERE key LIKE ?", (f"shield_active_{game_id}_{day}_%",))
    rows = c.fetchall()
    shielded = []
    for row in rows:
        parts = row["key"].split("_")
        # key: shield_active_{game_id}_{day}_{user_id}
        if len(parts) >= 5:
            try:
                shielded.append(int(parts[-1]))
            except ValueError:
                pass
    # Автоматическая — у кого есть shield active item (ещё не использован), игрок живой в этой игре
    c.execute(
        "SELECT DISTINCT user_id FROM items WHERE item_type='shield' AND status='active' AND user_id IN (SELECT user_id FROM players WHERE game_id=? AND is_alive=1)",
        (game_id,)
    )
    for row in c.fetchall():
        uid = row["user_id"]
        if uid not in shielded:
            shielded.append(uid)
    conn.close()
    return {"ok": True, "shielded_ids": shielded}


@app.get("/api/game/black_marks")
async def game_black_marks():
    game = get_active_game()
    if not game:
        return {"ok": False, "marked_ids": []}
    game_id = game["id"]
    day = game["current_day"] or 1
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT key FROM settings WHERE key LIKE ?", (f"black_mark_{game_id}_{day}_%",))
    rows = c.fetchall()
    conn.close()
    marked = []
    for row in rows:
        parts = row["key"].split("_")
        try: marked.append(int(parts[-1]))
        except: pass
    return {"ok": True, "marked_ids": marked}


@app.get("/api/game/alive")
async def game_alive(viewer_id: int = 0, next: int = 0):
    from database import get_conn as _gc
    conn = _gc(); c = conn.cursor()
    # Берём последнюю игру
    c.execute("SELECT * FROM games ORDER BY id DESC LIMIT 1")
    game = c.fetchone()
    # Если последняя игра finished — проверяем есть ли waiting
    if game and game["status"] == "finished":
        c.execute("SELECT * FROM games WHERE status='waiting' ORDER BY id DESC LIMIT 1")
        waiting_game = c.fetchone()
        if waiting_game:
            game = waiting_game
    conn.close()
    if not game:
        return {"ok": False, "error": "Нет игры"}
    # Для finished игры тоже возвращаем данные (чтобы показать таблицу результатов)
    if game["status"] not in ("active", "waiting", "finished"):
        return {"ok": False, "error": "Нет активной игры"}
    players = get_game_players(game["id"], alive_only=False)
    # Подтягиваем premium_icon
    conn2 = get_conn(); c2 = conn2.cursor()
    try: c2.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    except: pass
    for p in players:
        try:
            # Анонимус работает только во время активной игры
            if game["status"] == "active":
                anon_items = get_user_items(p["user_id"], game["id"])
                is_anon = any(i["item_type"] == "anon_player" for i in anon_items)
            else:
                is_anon = False
            p["is_anon"] = is_anon
            if is_anon and p["user_id"] != viewer_id:
                # Скрываем иконку только для чужих анонимусов
                p["premium_icon"] = None
            else:
                r = c2.execute("SELECT value FROM settings WHERE key=?", (f"premium_icon_{p['user_id']}",)).fetchone()
                p["premium_icon"] = r["value"] if r else None
        except:
            p["premium_icon"] = None
            p["is_anon"] = False
    conn2.close()
    winner_id = game["winner_id"] if "winner_id" in game.keys() else None

    # Активность голосования: сколько последних раундов подряд игрок не голосовал
    current_day = game["current_day"] or 0
    if game["status"] == "active" and current_day >= 1:
        conn_va = get_conn(); c_va = conn_va.cursor()
        try:
            # Проверяем есть ли вообще голоса в текущем раунде
            c_va.execute("SELECT COUNT(*) as cnt FROM votes WHERE game_id=? AND day_number=?", (game["id"], current_day))
            votes_in_current = c_va.fetchone()["cnt"]
            # Если в текущем раунде ещё никто не голосовал — смотрим с предыдущего
            start_day = current_day if votes_in_current > 0 else current_day - 1
            for p in players:
                uid = p["user_id"]
                if not p.get("is_alive"):
                    p["vote_missed"] = -1
                    continue
                if start_day < 1:
                    p["vote_missed"] = 0
                    continue
                missed = 0
                for d in range(start_day, max(start_day - 5, 0), -1):
                    c_va.execute("SELECT 1 FROM votes WHERE game_id=? AND day_number=? AND voter_id=?",
                                 (game["id"], d, uid))
                    if c_va.fetchone():
                        break
                    missed += 1
                p["vote_missed"] = missed
            # Считаем голоса: текущий раунд (для галочки) и всего за игру (для нуля)
            current_day = game["current_day"] or 1
            for p in players:
                uid = p["user_id"]
                # Проголосовал в текущем раунде?
                c_va.execute("SELECT COUNT(*) as cnt FROM votes WHERE game_id=? AND day_number=? AND voter_id=?", (game["id"], current_day, uid))
                row = c_va.fetchone()
                p["votes_in_game"] = row["cnt"] if row else 0
                # Голосовал ли хоть раз за всю игру?
                c_va.execute("SELECT COUNT(*) as cnt FROM votes WHERE game_id=? AND voter_id=?", (game["id"], uid))
                row2 = c_va.fetchone()
                p["votes_total"] = row2["cnt"] if row2 else 0
        except: pass
        conn_va.close()
    else:
        for p in players:
            p["vote_missed"] = 0
            p["votes_in_game"] = 0

    # Собираем voted_ids и shielded_ids чтобы фронт не делал лишние запросы
    voted_ids_list = []
    shielded_ids_list = []
    if game and game["status"] == "active":
        try:
            day = game["current_day"] or 1
            conn_extra = get_conn(); c_extra = conn_extra.cursor()
            c_extra.execute("SELECT DISTINCT voter_id FROM votes WHERE game_id=? AND day_number=?", (game["id"], day))
            voted_ids_list = [r["voter_id"] for r in c_extra.fetchall()]
            c_extra.execute("SELECT DISTINCT user_id FROM items WHERE item_type='shield' AND status='active' AND user_id IN (SELECT user_id FROM players WHERE game_id=? AND is_alive=1)", (game["id"],))
            shielded_ids_list = [r["user_id"] for r in c_extra.fetchall()]
            conn_extra.close()
        except: pass

    return {"ok": True, "players": players, "game_id": game["id"], "current_day": game["current_day"] or 0,
            "status": game["status"], "game_number": game["number"] or 1, "winner_id": winner_id,
            "voted_ids": voted_ids_list, "shielded_ids": shielded_ids_list}


@app.post("/api/game/register")
async def game_register(request: Request):
    body = await request.json()
    user_id = body.get("user_id")
    if not user_id:
        return JSONResponse({"ok": False}, status_code=400)

    game = get_active_game()
    if not game:
        return {"ok": False, "error": "Нет активной игры"}
    if game["status"] != "waiting":
        return {"ok": False, "error": "Регистрация закрыта"}

    # Проверяем подписку на чат
    is_member = await check_chat_member(user_id)
    if not is_member:
        return {"ok": False, "error": "not_subscribed"}
    set_chat_joined(user_id)

    # При регистрации оставляем только 1 анонимус — лишние удаляем
    try:
        conn_anr = get_conn(); c_anr = conn_anr.cursor()
        c_anr.execute("""
            UPDATE items SET status='used' WHERE user_id=? AND item_type='anon_player' AND status='active'
            AND id NOT IN (SELECT id FROM items WHERE user_id=? AND item_type='anon_player' AND status='active' LIMIT 1)
        """, (user_id, user_id))
        conn_anr.commit(); conn_anr.close()
    except: pass

    # Проверяем до регистрации — первый раз в любой игре?
    is_first_ever = False
    try:
        conn_chk = get_conn(); c_chk = conn_chk.cursor()
        c_chk.execute("SELECT COUNT(*) as cnt FROM players WHERE user_id=?", (user_id,))
        row_chk = c_chk.fetchone()
        is_first_ever = (row_chk["cnt"] == 0)
        conn_chk.close()
    except: pass

    ok, count, msg = register_player(game["id"], user_id)
    welcome_gems = 0
    if ok:
        # Первый в игре — ачивка Первопроходец
        if count == 1:
            try:
                conn_fj = get_conn(); c_fj = conn_fj.cursor()
                c_fj.execute("UPDATE users SET first_joins=first_joins+1 WHERE user_id=?", (user_id,))
                conn_fj.commit(); conn_fj.close()
            except: pass
        # Бонус новичка — 25 гемов при первой регистрации в игру
        if is_first_ever:
            add_gems(user_id, 25)
            welcome_gems = 25
    return {"ok": ok, "message": msg, "count": count, "welcome_gems": welcome_gems}


@app.post("/api/game/vote")
async def game_vote(request: Request):
    body = await request.json()
    voter_id = body.get("voter_id")
    target_id = body.get("target_id")
    game = get_active_game()
    if not game or game["status"] != "active":
        return {"ok": False, "error": "Голосование не активно"}
    if voter_id == target_id:
        return {"ok": False, "error": "Нельзя голосовать за себя"}
    # Проверяем что target не союзник voter
    conn_ally = get_conn(); c_ally = conn_ally.cursor()
    c_ally.execute("""
        SELECT 1 FROM clan_members cm1
        JOIN clan_members cm2 ON cm1.clan_id = cm2.clan_id
        JOIN clans cl ON cl.id = cm1.clan_id
        WHERE cl.game_id=? AND cm1.user_id=? AND cm2.user_id=?
        UNION
        SELECT 1 FROM clans cl
        JOIN clan_members cm ON cm.clan_id = cl.id
        WHERE cl.game_id=? AND cl.leader_id=? AND cm.user_id=?
        UNION
        SELECT 1 FROM clans cl
        JOIN clan_members cm ON cm.clan_id = cl.id
        WHERE cl.game_id=? AND cl.leader_id=? AND cm.user_id=?
    """, (game["id"], voter_id, target_id,
          game["id"], voter_id, target_id,
          game["id"], target_id, voter_id))
    is_ally = c_ally.fetchone()
    conn_ally.close()
    if is_ally:
        return {"ok": False, "error": "Нельзя голосовать против союзника"}
    # Проверяем что voter живой
    conn_chk = get_conn(); c_chk = conn_chk.cursor()
    c_chk.execute("SELECT is_alive FROM players WHERE game_id=? AND user_id=?", (game["id"], voter_id))
    voter_row = c_chk.fetchone(); conn_chk.close()
    if not voter_row or voter_row["is_alive"] == 0:
        return {"ok": False, "error": "Ты выбыл из игры"}
    # Проверяем активирована ли крышанулса на эту цель в этом раунде
    from database import get_alive_count as _gac_sh
    conn_sh2 = get_conn(); c_sh2 = conn_sh2.cursor()
    shield_active_key = f"shield_active_{game['id']}_{game['current_day']}_{target_id}"
    c_sh2.execute("SELECT value FROM settings WHERE key=?", (shield_active_key,))
    shield_active = c_sh2.fetchone()
    conn_sh2.close()
    if shield_active:
        return {"ok": False, "error": "У этого игрока иммунитет в этом раунде"}
    day = game["current_day"] or 1
    if has_bomzh_item(target_id, 'car_key') and day <= 25:
        return {"ok": False, "error": "У этого игрока иммунитет — уехал на тачке 🚗"}
    weight = cast_vote(game["id"], day, voter_id, target_id)

    # Лог в приватную группу
    def _n(uid):
        u = get_user(uid)
        if not u: return f"ID{uid}"
        anon_items = get_user_items(uid)
        if any(i["item_type"] == "anon_player" for i in anon_items):
            return "Анонимус"
        return f"@{u['username']}" if u["username"] else u["first_name"]
    double = " (x2 — Двустволка жахнула дуплетом)" if weight == 2 else ""
    async with httpx.AsyncClient() as _cl:
        await log_event(_cl, f"🗳 <b>Голос</b>\n{_n(voter_id)} → {_n(target_id)}{double}\nРаунд {day}")
        # Считаем голос для уровня
        try:
            _cv = get_conn(); _cc = _cv.cursor()
            _cc.execute("UPDATE users SET votes_cast=votes_cast+1 WHERE user_id=?", (voter_id,))
            _cv.commit(); _cv.close()
        except: pass

    # Проверяем — все ли живые проголосовали
    alive_players = get_game_players(game["id"])
    alive_ids = [p["user_id"] for p in alive_players]
    conn_chk = get_conn()
    c_chk = conn_chk.cursor()
    c_chk.execute("SELECT COUNT(DISTINCT voter_id) FROM votes WHERE game_id=? AND day_number=?", (game["id"], day))
    voted_count = c_chk.fetchone()[0]
    conn_chk.close()
    all_voted = voted_count >= len(alive_ids)

    # Счётчик раз проголосовали против цели
    try:
        conn_tva = get_conn(); c_tva = conn_tva.cursor()
        c_tva.execute("UPDATE users SET times_voted_against=times_voted_against+1 WHERE user_id=?", (target_id,))
        conn_tva.commit(); conn_tva.close()
    except: pass
    return {"ok": True, "weight": weight, "all_voted": all_voted, "voted": voted_count, "alive": len(alive_ids)}



@app.post("/api/game/double_vote")
async def use_double_vote(request: Request):
    """Активировать двустволку — +2 голоса за выбранного игрока"""
    body = await request.json()
    user_id = body.get("user_id")
    target_id = body.get("target_id")
    if not user_id or not target_id:
        return {"ok": False, "error": "Неверные данные"}
    game = get_active_game()
    if not game or game["status"] != "active":
        return {"ok": False, "error": "Игра не активна"}
    day = game["current_day"] or 1
    # Проверяем что игрок жив
    conn_chk = get_conn(); c_chk = conn_chk.cursor()
    c_chk.execute("SELECT is_alive FROM players WHERE game_id=? AND user_id=?", (game["id"], user_id))
    row = c_chk.fetchone(); conn_chk.close()
    if not row or not row["is_alive"]:
        return {"ok": False, "error": "Ты выбыл из игры"}
    ok, msg = cast_double_vote(game["id"], day, user_id, target_id)
    if not ok:
        return {"ok": False, "error": msg}
    # Уведомление в чат
    _dv_name = get_display_name(user_id)
    push_event(game["id"], "use", f"🔫 {_dv_name} активировал Двустволку — чей-то голос удвоился", "🔫")
    await notify_ability_activate("double_vote", username=_dv_name)
    return {"ok": True}


@app.post("/api/game/activate_shield")
async def activate_shield(request: Request):
    """Активировать крышанулсу вручную — защита на следующий раунд"""
    body = await request.json()
    user_id = body.get("user_id")
    game = get_active_game()
    if not game or game["status"] != "active":
        return {"ok": False, "error": "Игра не активна"}
    from database import get_alive_count
    alive_now = get_alive_count(game["id"])
    if alive_now < 3:
        return {"ok": False, "error": "Крышануться работает только при 3+ игроках"}
    items = get_user_items(user_id, game["id"])
    shield_item = next((i for i in items if i["item_type"] == "shield"), None)
    if not shield_item:
        return {"ok": False, "error": "Нет связи Крышануться"}
    game_id = game["id"]
    # Определяем на какой раунд — текущий +1
    next_day = (game["current_day"] or 1) + 1
    conn = get_conn()
    c = conn.cursor()
    # Сохраняем флаг что крышанулса активна на следующий раунд
    shield_key = f"shield_active_{game_id}_{next_day}_{user_id}"
    try:
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (shield_key, "1"))
    except:
        c.execute("UPDATE settings SET value='1' WHERE key=?", (shield_key,))
    # Тратим предмет
    c.execute("UPDATE items SET status='used' WHERE id=?", (shield_item["id"],))
    c.execute("UPDATE users SET items_used=items_used+1 WHERE user_id=?", (user_id,))
    conn.commit(); conn.close()
    _sh_name = get_display_name(user_id)
    push_event(game["id"], "use", f"🤵 {_sh_name} крышанулся — в следующем раунде его не достать", "🤵")
    await notify_ability_activate("shield", username=_sh_name)
    return {"ok": True, "protected_round": next_day}


@app.post("/api/game/killer")
async def use_killer(request: Request):
    body = await request.json()
    killer_id = body.get("killer_id")
    target_id = body.get("target_id")

    def _do_killer():
        game = get_active_game()
        if not game or game["status"] != "active":
            return None, False, "Игра не активна"
        if killer_id == target_id:
            return None, False, "Нельзя убить себя"
        target_items_k = get_user_items(target_id, game["id"])
        if any(i["item_type"] == "anon_player" for i in target_items_k):
            return None, False, "Этот игрок под защитой Анонимуса — киллер не работает"
        conn_ki = get_conn(); c_ki = conn_ki.cursor()
        shield_key_ki = f"shield_active_{game['id']}_{game['current_day']+1}_{target_id}"
        c_ki.execute("SELECT value FROM settings WHERE key=?", (shield_key_ki,))
        shield_ki = c_ki.fetchone(); conn_ki.close()
        if shield_ki:
            return None, False, "У этого игрока иммунитет — киллер не работает"
        from database import get_alive_count
        alive_now = get_alive_count(game["id"])
        if alive_now < 3:
            return None, False, "Киллер работает только при 3+ игроках"
        ok, msg = kill_player_by_killer(game["id"], killer_id, target_id)
        if ok:
            conn_kc = get_conn(); c_kc = conn_kc.cursor()
            c_kc.execute("UPDATE users SET used_killer=used_killer+1, items_used=items_used+1, kills=kills+1 WHERE user_id=?", (killer_id,))
            c_kc.execute("UPDATE users SET killed_by_killer=killed_by_killer+1 WHERE user_id=?", (target_id,))
            try:
                c_kc.execute("INSERT INTO kills_log (game_id, killer_id, victim_id) VALUES (?,?,?)",
                              (game["id"], killer_id, target_id))
            except: pass
            conn_kc.commit(); conn_kc.close()
        return game, ok, msg

    game, ok, msg = await asyncio.to_thread(_do_killer)
    if ok:
        def _uname(uid):
            u = get_user(uid)
            if not u: return f"ID{uid}"
            return f"@{u['username']}" if u["username"] else u["first_name"]
        import random as _rkill
        _target_name = _uname(target_id)
        _killer_name = _uname(killer_id)
        # Определяем имя киллера (анонимус или реальный)
        _killer_display = get_display_name(killer_id)
        current_day = game["current_day"] if game else "?"
        killer_phrases = [
            f"💀 <b>{_target_name} выбыл по-тихому.</b>\n\n{_killer_display} нанял киллера. Снайпер выследил жертву и снял её с крыши соседнего дома — один выстрел, никаких свидетелей.",
            f"🔫 <b>Заказ выполнен.</b>\n\n{_killer_display} потратил связи. {_target_name} шёл домой когда пуля догнала его в подворотне. Киллер растворился в темноте.",
            f"🩸 <b>{_target_name} больше не в игре.</b>\n\n{_killer_display} сделал звонок. Той же ночью {_target_name} нашли в багажнике — без следов борьбы, без свидетелей.",
            f"🗡 <b>Тихое устранение.</b>\n\n{_killer_display} заказал {_target_name}. Киллер ждал на крыше три часа — терпение окупилось. Один выстрел в голову, чистая работа.",
            f"💀 <b>{_target_name} исчез с района.</b>\n\n{_killer_display} вложил деньги в правильные руки. Профессионал сработал без лишнего шума — {_target_name} просто перестал выходить на связь.",
            f"🔪 <b>Контракт закрыт.</b>\n\n{_killer_display} нанял человека со связями. {_target_name} сидел в кафе когда незнакомец подсел рядом — это был последний кофе в его жизни.",
            f"🩸 <b>{_target_name} получил пулю.</b>\n\n{_killer_display} не захотел ждать голосований. Киллер выследил жертву у подъезда и выстрелил дважды — контрольный в висок.",
            f"💀 <b>Район стал чище.</b>\n\n{_killer_display} убрал {_target_name} руками профессионала. Тело нашли только утром — аккуратная дырка между глаз, никаких гильз.",
            f"🔫 <b>Снайпер отработал.</b>\n\n{_killer_display} дал добро. {_target_name} вышел покурить на балкон — больше он туда не выйдет. Выстрел с 400 метров, ветер не помешал.",
            f"🗡 <b>{_target_name} выбыл досрочно.</b>\n\n{_killer_display} не стал играть честно. Наёмник подкараулил жертву в лифте — тихо, быстро, профессионально. Камеры не работали.",
            f"🩸 <b>Заказ принят и исполнен.</b>\n\n{_killer_display} указал пальцем на {_target_name}. Этой ночью к нему пришли — не для разговора. Район потерял ещё одного игрока.",
            f"💀 <b>{_target_name} не доехал домой.</b>\n\n{_killer_display} перехватил его на трассе. Киллер подрезал машину на повороте, второй выстрел сделал работу. Дорога была пустой.",
            f"🔪 <b>Мокрое дело сделано.</b>\n\n{_killer_display} заплатил вперёд. Киллер дождался {_target_name} в подвале его же дома — там где нет камер и соседей. Быстро и без разговоров.",
            f"🩸 <b>Профессионал поработал.</b>\n\n{_killer_display} сделал звонок нужному человеку. {_target_name} шёл на встречу — но встреча уже ждала его за углом с глушителем наготове.",
            f"💀 <b>{_target_name} сложился.</b>\n\n{_killer_display} выбрал момент. Снайпер занял позицию на крыше ещё с утра — терпеливо ждал пока цель выйдет. Дождался. Один выстрел.",
            f"🔫 <b>Устранение прошло чисто.</b>\n\n{_killer_display} не хотел рисковать на голосовании. Нанятый стрелок подловил {_target_name} в гараже — пуля в затылок, машина осталась с открытой дверью.",
            f"🗡 <b>Район услышал один хлопок.</b>\n\n{_killer_display} дал команду. Снайпер работал с глушителем — но соседи всё равно слышали. Правда говорить об этом они не будут. {_target_name} выбыл.",
            f"🩸 <b>Наёмник не промахнулся.</b>\n\n{_killer_display} заказал {_target_name} ещё вчера. Киллер нашёл его в спортзале — дождался в раздевалке. Никто ничего не видел. Камера не писала.",
            f"💀 <b>Контракт выполнен точно в срок.</b>\n\n{_killer_display} вложил деньги в нужные руки. {_target_name} открыл входную дверь и получил три пули — профессионал не оставил шансов.",
            f"🔪 <b>{_target_name} получил то что заслужил.</b>\n\n{_killer_display} не стал ждать. Киллер выследил жертву два дня — изучил маршрут, выбрал место. Переулок за рынком. Чисто.",
            f"🔪 <b>{_target_name} не успел даже крикнуть.</b>\n\n{_killer_display} выбрал нож. Киллер подошёл сзади в тёмном переулке — быстро, бесшумно. Тело нашли только утром.",
            f"🚗 <b>{_target_name} попал под машину.</b>\n\n{_killer_display} организовал несчастный случай. {_target_name} переходил дорогу — чёрный джип даже не притормозил. Водителя не нашли.",
            f"☠️ <b>{_target_name} отравлен.</b>\n\n{_killer_display} добавил кое-что в напиток {_target_name}. К утру тот не проснулся. Яд не оставляет следов — вскрытие ничего не покажет.",
            f"🔪 <b>Нож решил всё.</b>\n\n{_killer_display} нанял человека с руками. {_target_name} открыл дверь незнакомцу — и это была его последняя ошибка. Три удара, всё кончено.",
            f"🧪 <b>{_target_name} выпил не то.</b>\n\n{_killer_display} заплатил бармену. {_target_name} заказал как обычно — но в этот раз в стакане было кое-что лишнее. Медленно, но верно.",
            f"🚗 <b>Несчастный случай на дороге.</b>\n\n{_killer_display} всё спланировал. {_target_name} выехал утром — и не доехал. Фура выехала на встречку точно в нужный момент. Случайность? Вряд ли.",
            f"💀 <b>{_target_name} задохнулся.</b>\n\n{_killer_display} нанял тихого профессионала. Тот дождался пока {_target_name} уснёт — подушка сделала своё дело. Никакого шума, никаких следов.",
            f"🔪 <b>Шею свернули быстро.</b>\n\n{_killer_display} позвонил нужному человеку. Киллер встретил {_target_name} в лифте — один резкий захват, одно движение. Профессионал старой закалки.",
            f"☠️ <b>{_target_name} съел не то.</b>\n\n{_killer_display} добрался до его кухни. Яд в еде — классика. {_target_name} почувствовал себя плохо за ужином и до утра не дотянул.",
            f"🚗 <b>{_target_name} улетел с моста.</b>\n\n{_killer_display} подрезал его на повороте над рекой. Машина пробила ограждение и ушла под воду. Водолазы нашли её только через сутки.",
            f"🔪 <b>Финка в бок на выходе из клуба.</b>\n\n{_killer_display} знал маршрут {_target_name}. Киллер ждал у чёрного входа — короткий удар, никто не видел. {_target_name} осел прямо у стены.",
            f"🧪 <b>Яд подмешали в кофе.</b>\n\n{_killer_display} знал где {_target_name} завтракает каждое утро. Официант был в теме. Один стакан — и {_target_name} покинул игру навсегда.",
        ]
        log_msg = f"💀 <b>Киллер использован!</b>\n👤 {_killer_name} убил {_target_name}\nРаунд {current_day}"
        chat_msg = _rkill.choice(killer_phrases)
        push_event(game["id"], "kill", f"💀 {_killer_display} нанял киллера — {_target_name} устранён", "💀")
        try:
            async with httpx.AsyncClient(timeout=10) as _cl:
                _r = await _cl.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={"chat_id": "@shrimpgames_chat", "text": chat_msg,
                          "parse_mode": "HTML", "disable_web_page_preview": True})
                print(f"[KILLER] chat msg sent, status={_r.status_code}, body={_r.text[:200]}")
        except Exception as _e:
            print(f"[KILLER] chat msg error: {_e}")
    return {"ok": ok, "error": msg if not ok else None}


@app.post("/api/chat/joined")
async def chat_joined(request: Request):
    body = await request.json()
    user_id = body.get("user_id")
    if user_id:
        set_chat_joined(user_id)
    return {"ok": True}


@app.get("/api/friends/{user_id}")
async def friends(user_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT user_id, username, first_name, photo_url FROM users WHERE ref_by=? ORDER BY created_at DESC", (user_id,))
    rows = c.fetchall()
    conn.close()
    return {"ok": True, "friends": [dict(r) for r in rows]}


@app.post("/api/game/resurrect")
async def use_resurrect(request: Request):
    body = await request.json()
    user_id = body.get("user_id")
    game = get_active_game()
    if not game or game["status"] != "active":
        return JSONResponse({"ok": False, "error": "Игра не активна"})
    conn = get_conn(); c = conn.cursor()
    # Проверяем что игрок мёртв
    c.execute("SELECT is_alive FROM players WHERE game_id=? AND user_id=?", (game["id"], user_id))
    row = c.fetchone()
    if not row or row["is_alive"] != 0:
        conn.close()
        return JSONResponse({"ok": False, "error": "Ты жив — Постанова не нужна"})
    # Проверяем что в игре 10+ живых
    c.execute("SELECT COUNT(*) as cnt FROM players WHERE game_id=? AND is_alive=1", (game["id"],))
    alive_cnt = c.fetchone()["cnt"]
    if alive_cnt < 10:
        conn.close()
        return JSONResponse({"ok": False, "error": "Постанова работает только при 10+ живых игроках"})
    # Проверяем наличие предмета
    c.execute("SELECT id FROM items WHERE user_id=? AND item_type='resurrect' AND status='active' LIMIT 1", (user_id,))
    item = c.fetchone()
    if not item:
        conn.close()
        return JSONResponse({"ok": False, "error": "Постановы нет в инвентаре"})
    # Воскрешаем
    c.execute("UPDATE players SET is_alive=1 WHERE game_id=? AND user_id=?", (game["id"], user_id))
    c.execute("UPDATE items SET status='used' WHERE id=?", (item["id"],))
    try: c.execute("ALTER TABLE users ADD COLUMN resurrected INTEGER DEFAULT 0")
    except: pass
    c.execute("UPDATE users SET resurrected=COALESCE(resurrected,0)+1 WHERE user_id=?", (user_id,))
    conn.commit(); conn.close()
    # Пост в чат (только один раз, без push_event чтобы не дублировать)
    try:
        import httpx as _hxr
        conn_u = get_conn(); c_u = conn_u.cursor()
        c_u.execute("SELECT first_name, username FROM users WHERE user_id=?", (user_id,))
        u = c_u.fetchone(); conn_u.close()
        uname_r = f"@{u['username']}" if u and u["username"] else (u["first_name"] if u else f"ID{user_id}")
        async with _hxr.AsyncClient(timeout=8) as _cl:
            await _cl.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": "@shrimpgames_chat",
                      "text": f"🎭 {uname_r} замутил Постанову — инсценировал смерть и вернулся в игру!",
                      "parse_mode": "HTML"})
    except: pass
    return {"ok": True}


@app.post("/api/game/hacker")
async def use_hacker(request: Request):
    body = await request.json()
    user_id = body.get("user_id")
    game = get_active_game()
    if not game or game["status"] != "active":
        return {"ok": False, "error": "Игра не активна"}

    from database import get_alive_count, get_conn
    alive_now = get_alive_count(game["id"])
    if alive_now < 3:
        return {"ok": False, "error": "Ворюга работает только при 3+ игроках"}

    # Проверяем что связь есть
    items = get_user_items(user_id, game["id"])
    if not any(i["item_type"] == "hacker" for i in items):
        return {"ok": False, "error": "Нет связи Ворюга"}

    game_id = game["id"]
    day = game["current_day"] or 1

    # Обнуляем голоса против user_id в этом раунде
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM votes WHERE game_id=? AND day_number=? AND target_id=?", (game_id, day, user_id))
    deleted = c.rowcount
    # Удаляем связь
    c.execute("DELETE FROM items WHERE user_id=? AND item_type='hacker' AND status='active' LIMIT 1", (user_id,))
    conn.commit()
    conn.close()

    _hk_name = get_display_name(user_id)
    push_event(game["id"], "use", f"💰 {_hk_name} отправил Ворюгу на дело — чьи-то голоса украдены", "💰")
    await notify_ability_activate("hacker", username=_hk_name)
    return {"ok": True, "deleted_votes": deleted}


@app.post("/api/game/black_mark")
async def use_black_mark(request: Request):
    body = await request.json()
    user_id = body.get("user_id")
    target_id = body.get("target_id")
    game = get_active_game()
    if not game or game["status"] != "active":
        return {"ok": False, "error": "Игра не активна"}
    from database import get_alive_count
    if get_alive_count(game["id"]) < 3:
        return {"ok": False, "error": "Мусорнуться работает только при 3+ игроках"}
    items = get_user_items(user_id, game["id"])
    if not any(i["item_type"] == "black_mark" for i in items):
        # Проверяем без привязки к игре (куплен через Stars)
        items_all = get_user_items(user_id)
        if not any(i["item_type"] == "black_mark" for i in items_all):
            return {"ok": False, "error": "Нет связи Мусорнуться"}
        items = items_all
    game_id = game["id"]
    current_day = game["current_day"] or 1
    conn = get_conn()
    c = conn.cursor()
    # Сохраняем метку на ТЕКУЩИЙ раунд (работает сразу)
    mark_key = f"black_mark_{game_id}_{current_day}_{target_id}"
    try:
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (mark_key, str(user_id)))
    except:
        c.execute("UPDATE settings SET value=? WHERE key=?", (str(user_id), mark_key))
    # Тратим предмет
    c.execute("UPDATE items SET status='used' WHERE user_id=? AND item_type='black_mark' AND status='active' LIMIT 1", (user_id,))
    conn.commit(); conn.close()
    # Узнаём реальное имя цели
    conn_rt = get_conn(); c_rt = conn_rt.cursor()
    c_rt.execute("SELECT first_name, username FROM users WHERE user_id=?", (target_id,))
    _tu = c_rt.fetchone(); conn_rt.close()
    real_name = (_tu["first_name"] or _tu["username"] or "Неизвестный") if _tu else "Неизвестный"
    # Снимаем анонимус у жертвы — его раскрыли
    try:
        conn_ba = get_conn(); c_ba = conn_ba.cursor()
        c_ba.execute("UPDATE items SET status='used' WHERE user_id=? AND item_type='anon_player' AND status='active'", (target_id,))
        conn_ba.commit(); conn_ba.close()
    except: pass
    _bm_name = get_display_name(user_id)
    push_event(game["id"], "use", f"🚔 {_bm_name} мусорнулся и написал заяву на Анонимуса!", "🚔")
    await notify_ability_activate("black_mark", username=_bm_name)
    return {"ok": True, "real_name": real_name}


@app.post("/api/set_gender")
async def api_set_gender(request: Request):
    body = await request.json()
    user_id = body.get("user_id")
    gender = body.get("gender")
    if gender not in ("male", "female"):
        return {"ok": False, "error": "invalid gender"}
    set_gender(user_id, gender)
    return {"ok": True}


@app.post("/api/shop/buy")
async def shop_buy(request: Request):
    body = await request.json()
    user_id = body.get("user_id")
    item_type = body.get("item_type")
    if item_type not in ITEMS:
        return JSONResponse({"ok": False, "error": "Unknown item"}, status_code=400)

    # Админу бесплатно
    if user_id == ADMIN_ID:
        game = get_active_game()
        game_id = game["id"] if game else None
        add_item(user_id, item_type, game_id)
        # Счётчики ачивок
        try:
            conn_ac = get_conn(); c_ac = conn_ac.cursor()
            c_ac.execute("UPDATE users SET first_purchase=1 WHERE user_id=?", (user_id,))
            if item_type == "shield": c_ac.execute("UPDATE users SET bought_shield=bought_shield+1 WHERE user_id=?", (user_id,))
            if item_type == "anon_player": c_ac.execute("UPDATE users SET went_anon=went_anon+1 WHERE user_id=?", (user_id,))
            conn_ac.commit(); conn_ac.close()
        except: pass
        item_name = ITEMS[item_type]["name"]
        async with httpx.AsyncClient() as _cl:
            await log_event(_cl, f"🎁 <b>Бесплатная выдача (Админ)</b>\n📦 {item_name}\n👤 ID{user_id}")
        return {"ok": True, "free": True, "item_type": item_type}

    item = ITEMS[item_type]
    # Проверяем скидку
    final_stars = item["stars"]
    try:
        from datetime import datetime as _dts
        conn_sale = get_conn(); c_sale = conn_sale.cursor()
        c_sale.execute("SELECT value FROM settings WHERE key='sale_end'")
        sale_row = c_sale.fetchone(); conn_sale.close()
        if sale_row and item_type not in ["anon_msg"]:
            sale_end = _dts.fromisoformat(sale_row["value"])
            if _dts.utcnow() < sale_end:
                final_stars = item["stars"] // 2
    except: pass
    try:
        if has_bomzh_item(user_id, 'credit_card'):
            final_stars = max(1, final_stars // 2)
    except: pass
    try:
        async with httpx.AsyncClient() as cl:
            r = await cl.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/createInvoiceLink",
                json={
                    "title": item["name"],
                    "description": "Связь для Разборок на районе",
                    "payload": f"{user_id}:{item_type}",
                    "currency": "XTR",
                    "prices": [{"label": item["name"], "amount": final_stars}],
                }
            )
            d = r.json()
            if d.get("ok"):
                # Счётчики ачивок при покупке
                try:
                    conn_ac = get_conn(); c_ac = conn_ac.cursor()
                    c_ac.execute("UPDATE users SET first_purchase=1 WHERE user_id=?", (user_id,))
                    if item_type == "shield": c_ac.execute("UPDATE users SET bought_shield=bought_shield+1 WHERE user_id=?", (user_id,))
                    if item_type == "anon_player": c_ac.execute("UPDATE users SET went_anon=went_anon+1 WHERE user_id=?", (user_id,))
                    conn_ac.commit(); conn_ac.close()
                except: pass
                return {"ok": True, "invoice_url": d["result"]}
            return {"ok": False, "error": d.get("description", "Ошибка")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/shop/buy_combo")
async def shop_buy_combo(request: Request):
    """Комбо-набор: все связи по 1 штуке за 100 звёзд"""
    body = await request.json()
    user_id = body.get("user_id")
    if not user_id:
        return JSONResponse({"ok": False, "error": "no user"}, status_code=400)

    COMBO_ITEMS = ["killer", "resurrect", "shield", "hacker", "spy", "tiebreaker", "double_vote", "anon_msg", "anon_player", "black_mark"]
    COMBO_STARS = 100

    # Админу бесплатно
    if user_id == ADMIN_ID:
        game = get_active_game()
        game_id = game["id"] if game else None
        for it in COMBO_ITEMS:
            add_item(user_id, it, game_id)
        async with httpx.AsyncClient() as _cl:
            await log_event(_cl, f"🎁 <b>Комбо (Админ)</b>\n👤 ID{user_id}")
        return {"ok": True, "free": True}

    async with httpx.AsyncClient() as cl:
        r = await cl.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/createInvoiceLink",
            json={
                "title": "Олигарх — 100 ⭐",
                "description": "Все 10 связей по 1 штуке за 100 Stars — полный арсенал для победы!",
                "payload": f"{user_id}:combo",
                "currency": "XTR",
                "prices": [{"label": "Комбо-набор", "amount": COMBO_STARS}],
            }
        )
        d = r.json()
        if d.get("ok"):
            return {"ok": True, "invoice_url": d["result"]}
        return {"ok": False, "error": d.get("description", "Ошибка")}


@app.post("/api/task/reward")
async def task_reward(request: Request):
    """Бесплатная выдача стукача за выполнение задания (чат/канал)"""
    body = await request.json()
    user_id = body.get("user_id")
    task = body.get("task")  # "chat" or "channel"
    if not user_id or task not in ("chat", "channel"):
        return {"ok": False, "error": "bad request"}

    key = f"task_{task}_done_{user_id}"
    conn_t = get_conn(); c_t = conn_t.cursor()
    # Проверяем не получал ли уже
    c_t.execute("SELECT value FROM settings WHERE key=?", (key,))
    already = c_t.fetchone()
    if already:
        conn_t.close()
        return {"ok": False, "error": "already_claimed"}

    # Если задание чат — проверяем реальную подписку
    if task == "chat":
        is_member = await check_chat_member(user_id)
        if not is_member:
            conn_t.close()
            return {"ok": False, "error": "not_subscribed"}
        set_chat_joined(user_id)

    # Выдаём стукача
    game = get_active_game()
    game_id = game["id"] if game else None
    add_item(user_id, "spy", game_id)
    try:
        c_t.execute("UPDATE users SET items_won=items_won+1 WHERE user_id=?", (user_id,))
    except: pass
    # Помечаем что задание выполнено
    c_t.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, "1"))
    conn_t.commit(); conn_t.close()

    async with httpx.AsyncClient() as _cl:
        await log_event(_cl, f"🔍 <b>Стукач за задание ({task})</b>\n👤 ID{user_id}")
    return {"ok": True}




@app.post("/api/game/resolve_votes")
async def resolve_votes(request: Request):
    """
    Подвести итоги голосования. Вызывается вручную админом или автоматически.
    Пишет результаты в чат и уведомляет каждого игрока.
    Ночной перерыв: 20:00-08:00 по Таллину (UTC+3)
    """
    body = await request.json()
    if body.get("admin_key") != str(ADMIN_ID):
        return JSONResponse({"ok": False, "error": "Forbidden"}, status_code=403)

    from database import get_vote_results, eliminate_player, start_tiebreaker, get_alive_count, get_conn

    game = get_active_game()
    if not game or game["status"] != "active":
        return {"ok": False, "error": "Нет активной игры"}

    game_id = game["id"]
    day = game["current_day"] or 1
    alive_now = get_alive_count(game_id)
    results = get_vote_results(game_id, day)

    # ══════════════════════════════════════════
    # ФИНАЛ — осталось 2 игрока
    # ══════════════════════════════════════════
    if alive_now == 2:
        from database import get_conn as _gc2
        alive_players_final = get_game_players(game_id, alive_only=True)
        p1_id = alive_players_final[0]["user_id"]
        p2_id = alive_players_final[1]["user_id"]

        def uname_final(uid):
            u = get_user(uid)
            if not u: return "Игрок"
            anon = get_user_items(uid, game_id)
            if any(i["item_type"] == "anon_player" for i in anon):
                return "Анонимус"
            return u["first_name"] or u["username"] or "Игрок"

        # Кто проголосовал в этом раунде
        conn_f = _gc2(); c_f = conn_f.cursor()
        c_f.execute("SELECT voter_id, target_id, created_at FROM votes WHERE game_id=? AND day_number=? ORDER BY created_at ASC",
                    (game_id, day))
        final_votes = [dict(r) for r in c_f.fetchall()]
        conn_f.close()

        p1_voted = any(v["voter_id"] == p1_id for v in final_votes)
        p2_voted = any(v["voter_id"] == p2_id for v in final_votes)

        winner_id = None
        loser_id = None
        reason = ""

        if not p1_voted and not p2_voted:
            # Никто не проголосовал — ничья, ждём
            return {"ok": False, "error": "Финал: никто не проголосовал"}

        elif p1_voted and not p2_voted:
            # p2 не проголосовал — проигрывает
            winner_id = p1_id
            loser_id = p2_id
            reason = f"⚠️ {uname_final(p2_id)} не проголосовал — выбывает!"

        elif p2_voted and not p1_voted:
            winner_id = p2_id
            loser_id = p1_id
            reason = f"⚠️ {uname_final(p1_id)} не проголосовал — выбывает!"

        else:
            # Оба проголосовали — проверяем Решалу
            p1_items = get_user_items(p1_id, game_id)
            p2_items = get_user_items(p2_id, game_id)
            p1_has_tb = any(i["item_type"] == "tiebreaker" for i in p1_items)
            p2_has_tb = any(i["item_type"] == "tiebreaker" for i in p2_items)

            if p1_has_tb and not p2_has_tb:
                winner_id = p1_id; loser_id = p2_id
                reason = f"🔨 {uname_final(p1_id)} использовал Решалу!"
                conn_tb = _gc2(); c_tb = conn_tb.cursor()
                c_tb.execute("UPDATE items SET status='used' WHERE user_id=? AND item_type='tiebreaker' AND status='active' LIMIT 1", (p1_id,))
                conn_tb.commit(); conn_tb.close()

            elif p2_has_tb and not p1_has_tb:
                winner_id = p2_id; loser_id = p1_id
                reason = f"🔨 {uname_final(p2_id)} использовал Решалу!"
                conn_tb = _gc2(); c_tb = conn_tb.cursor()
                c_tb.execute("UPDATE items SET status='used' WHERE user_id=? AND item_type='tiebreaker' AND status='active' LIMIT 1", (p2_id,))
                conn_tb.commit(); conn_tb.close()

            else:
                # Проверяем премиум у самих финалистов
                p1_prem = get_user_stats(p1_id).get("is_premium", False)
                p2_prem = get_user_stats(p2_id).get("is_premium", False)
                if p1_prem and not p2_prem:
                    winner_id = p1_id; loser_id = p2_id
                    reason = "👑 Премиум-игрок победил в равном бою!"
                elif p2_prem and not p1_prem:
                    winner_id = p2_id; loser_id = p1_id
                    reason = "👑 Премиум-игрок победил в равном бою!"
                else:
                # Оба с Решалой или оба без — считаем голоса за всю игру
                    conn_hist = _gc2(); c_hist = conn_hist.cursor()
                c_hist.execute("SELECT target_id, COUNT(*) as cnt FROM votes WHERE game_id=? GROUP BY target_id",
                               (game_id, ))
                hist = {r["target_id"]: r["cnt"] for r in c_hist.fetchall()}
                conn_hist.close()
                p1_total = hist.get(p1_id, 0)
                p2_total = hist.get(p2_id, 0)

                if p1_total != p2_total:
                    # Больше голосов за всю игру — проигрывает
                    loser_id = p1_id if p1_total > p2_total else p2_id
                    winner_id = p2_id if loser_id == p1_id else p1_id
                    reason = f"📊 За {uname_final(loser_id)} проголосовали больше всего за игру ({max(p1_total,p2_total)} раз)"
                else:
                    # Поровну — побеждает тот против кого дали первый голос в финале
                    if final_votes:
                        first_target = final_votes[0]["target_id"]
                        # Первый голос был против него — он побеждает (значит другой проигрывает)
                        winner_id = first_target
                        loser_id = p1_id if winner_id == p2_id else p2_id
                        reason = f"⚡ Первый голос в финале решил исход!"
                    else:
                        winner_id = p1_id; loser_id = p2_id
                        reason = "🎲 Победитель определён случайно"

        # Завершаем игру
        eliminate_player(game_id, loser_id)
        conn_w = _gc2(); c_w = conn_w.cursor()
        c_w.execute("UPDATE games SET status='finished', finished_at=CURRENT_TIMESTAMP, winner_id=? WHERE id=?",
                    (winner_id, game_id))
        c_w.execute("UPDATE users SET wins=wins+1 WHERE user_id=?", (winner_id,))
        conn_w.commit(); conn_w.close()

        w_name = uname_final(winner_id)
        l_name = uname_final(loser_id)

        async with httpx.AsyncClient() as cl_f:
            win_msg = (
                f"⚔️ <b>ФИНАЛ завершён!</b>\n\n"
                f"{reason}\n\n"
                f"🏆 Победитель: <b>{w_name}</b>\n"
                f"💀 Выбывает: <b>{l_name}</b>\n\n"
                f"Поздравляем! Приз будет отправлен победителю." + BOT_LINK
            )
            try:
                await cl_f.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={"chat_id": "@shrimpgames_chat", "text": win_msg, "parse_mode": "HTML", "disable_web_page_preview": True})
            except: pass
            try:
                await cl_f.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={"chat_id": winner_id, "text": f"🏆 Ты победил в Разборках на районе! Приз скоро придёт.", "parse_mode": "HTML"})
            except: pass

        # Пуш администратору — итоги игры топ-5
        try:
            conn_top = _gc2(); c_top = conn_top.cursor()
            game_num_row = c_top.execute("SELECT number FROM games WHERE id=?", (game_id,)).fetchone()
            game_num = str(game_num_row["number"]) if game_num_row else "?"
            top_rows = c_top.execute("""
                SELECT p.user_id, p.is_alive, u.first_name, u.username,
                       COALESCE((SELECT MAX(v.day_number) FROM votes v WHERE v.game_id=p.game_id AND v.target_id=p.user_id), 0) AS last_vote_day
                FROM players p JOIN users u ON p.user_id = u.user_id
                WHERE p.game_id = ?
                ORDER BY p.is_alive DESC, last_vote_day DESC
                LIMIT 5
            """, (game_id,)).fetchall()
            conn_top.close()
            place_icons = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
            result_lines = []
            for i, r in enumerate(top_rows):
                fn = (r["first_name"] or "").strip()
                un = " (@" + r["username"] + ")" if r["username"] else ""
                result_lines.append(place_icons[i] + " " + fn + un)
            top_text = "\n".join(result_lines) if result_lines else "-"
            admin_text = (
                "\U0001f3c1 <b>\u0418\u0433\u0440\u0430 #" + game_num + " \u0437\u0430\u0432\u0435\u0440\u0448\u0435\u043d\u0430!</b>\n\n"
                + "<b>\u0422\u043e\u043f-5 \u0438\u0433\u0440\u043e\u043a\u043e\u0432:</b>\n"
                + top_text + "\n\n"
                + "\U0001f3c6 \u041f\u043e\u0431\u0435\u0434\u0438\u0442\u0435\u043b\u044c: <b>" + w_name + "</b>"
            )
            async with httpx.AsyncClient() as _cl_adm:
                await _cl_adm.post(
                    "https://api.telegram.org/bot" + BOT_TOKEN + "/sendMessage",
                    json={"chat_id": ADMIN_ID, "text": admin_text, "parse_mode": "HTML"}
                )
        except Exception as e_adm:
            print("[ADMIN NOTIFY] error:", e_adm)

        asyncio.create_task(auto_create_next_game(game_id))
        return {"ok": True, "outcome": "game_over", "winner_id": winner_id, "winner_name": w_name}

    # ══════════════════════════════════════════
    # ОБЫЧНЫЙ РАУНД
    # ══════════════════════════════════════════
    if not results:
        return {"ok": False, "error": "Голосов нет"}

    # Ворюга срабатывает только вручную через /api/game/hacker — не авто
    item_events = []

    # Собираем события предметов за этот раунд
    conn_ev = get_conn()
    c_ev = conn_ev.cursor()
    # Крышануться — кто был защищён
    try:
        c_ev.execute("SELECT key FROM settings WHERE key LIKE ?", (f"shield_active_{game_id}_{day}_%",))
        for row in c_ev.fetchall():
            parts = row["key"].split("_")
            uid = int(parts[-1])
            c_ev.execute("SELECT first_name FROM users WHERE user_id=?", (uid,))
            u = c_ev.fetchone()
            name = u["first_name"] if u else "Игрок"
            item_events.append(f"🤵 {name} крышанулся — в следующем раунде его не достать")
    except: pass
    # Киллер — кто был убит до голосования (is_alive=0 но голоса против них есть)
    try:
        c_ev.execute("""
            SELECT DISTINCT v.target_id FROM votes v
            JOIN players p ON p.user_id=v.target_id AND p.game_id=v.game_id
            WHERE v.game_id=? AND v.day_number=? AND p.is_alive=0
        """, (game_id, day))
        for row in c_ev.fetchall():
            uid = row["target_id"]
            c_ev.execute("SELECT first_name FROM users WHERE user_id=?", (uid,))
            u = c_ev.fetchone()
            name = u["first_name"] if u else "Игрок"
            item_events.insert(0, f"💀 {name} устранён — кто-то заказал на него киллера. Без голосований, без предупреждений")
            push_event(game["id"], "kill", f"💀 {_target_name} устранён — кто-то заказал киллера", "💀")
    except: pass
    # Мусорнуться — кто помечен в этом раунде
    try:
        c_ev.execute("SELECT key FROM settings WHERE key LIKE ?", (f"black_mark_{game_id}_{day}_%",))
        for row in c_ev.fetchall():
            parts = row["key"].split("_")
            uid = int(parts[-1])
            c_ev.execute("SELECT first_name FROM users WHERE user_id=?", (uid,))
            u = c_ev.fetchone()
            name = u["first_name"] if u else "Игрок"
            c_ev.execute("SELECT first_name, username FROM users WHERE user_id=?", (int(row["key"].split("_")[4]),))
            _bmu = c_ev.fetchone()
            _bm_actor = (_bmu["first_name"] or _bmu["username"] or "Игрок") if _bmu else "Игрок"
            item_events.append(f"🚔 {_bm_actor} мусорнулся и накатал заяву — теперь знает настоящий ник Анонимуса")
    except: pass
    conn_ev.close()

    # Собираем список помеченных чёрной меткой в этом раунде
    conn_bm = get_conn(); c_bm = conn_bm.cursor()
    c_bm.execute("SELECT key FROM settings WHERE key LIKE ?", (f"black_mark_{game_id}_{day}_%",))
    black_marked_ids = set()
    for row in c_bm.fetchall():
        parts = row["key"].split("_")
        try: black_marked_ids.add(int(parts[-1]))
        except: pass
    conn_bm.close()

    # Формируем счёт голосов для отчёта
    def uname(uid, force_real=False):
        u = get_user(uid)
        if not u: return "Игрок"
        if not force_real:
            # Если на игроке чёрная метка — показываем реальный ник
            if uid in black_marked_ids:
                return u["first_name"] or u["username"] or "Игрок"
            anon_items = get_user_items(uid, game_id)
            if any(i["item_type"] == "anon_player" for i in anon_items):
                return "Анонимус"
        return u["first_name"] or u["username"] or "Игрок"

    score_lines = []
    for uid, cnt in results:
        score_lines.append(f"  {uname(uid)} — {cnt} {'голос' if cnt==1 else 'голоса' if 2<=cnt<=4 else 'голосов'}")
    score_text = "\n".join(score_lines)

    # Детальный лог всех голосов для лог-группы
    conn_log = get_conn()
    c_log = conn_log.cursor()
    c_log.execute("SELECT voter_id, target_id FROM votes WHERE game_id=? AND day_number=?", (game_id, day))
    all_votes = c_log.fetchall()
    conn_log.close()
    vote_detail = "\n".join([f"  {uname(v['voter_id'])} → {uname(v['target_id'])}" for v in all_votes])
    detailed_log = f"📋 <b>Раунд {day} — все голоса:</b>\n{vote_detail}\n\n<b>Счёт:</b>\n{score_text}" 

    max_votes = results[0][1]
    leaders = [uid for uid, cnt in results if cnt == max_votes]

    # Проверяем крышанулсу — через items (автоматическая) или через shield_active_* (ручная активация)
    def has_shield(uid):
        # Сначала проверяем активный предмет в инвентаре
        conn_sh = get_conn(); c_sh = conn_sh.cursor()
        c_sh.execute("SELECT id FROM items WHERE user_id=? AND item_type='shield' AND status='active' LIMIT 1", (uid,))
        item = c_sh.fetchone()
        conn_sh.close()
        if item:
            return True
        # Фоллбек — старый способ через settings (ручная активация)
        shield_key = f"shield_active_{game_id}_{day}_{uid}"
        conn_sh2 = get_conn(); c_sh2 = conn_sh2.cursor()
        c_sh2.execute("SELECT value FROM settings WHERE key=?", (shield_key,))
        result = c_sh2.fetchone()
        conn_sh2.close()
        return result is not None

    def use_shield(uid):
        # Списываем предмет из items
        conn_s = get_conn(); c_s = conn_s.cursor()
        c_s.execute("UPDATE items SET status='used' WHERE user_id=? AND item_type='shield' AND status='active' LIMIT 1", (uid,))
        # Удаляем ключ из settings если был
        shield_key = f"shield_active_{game_id}_{day}_{uid}"
        c_s.execute("DELETE FROM settings WHERE key=?", (shield_key,))
        conn_s.commit(); conn_s.close()

    async with httpx.AsyncClient() as cl:

        if len(leaders) == 1:
            victim_id = leaders[0]

            # Крышануться — автоматически защищает, вылетает следующий по голосам
            if has_shield(victim_id):
                use_shield(victim_id)
                shield_name = uname(victim_id)
                # Ищем следующего по голосам
                next_candidates = [(uid, cnt) for uid, cnt in results if uid != victim_id]
                if next_candidates:
                    victim_id = next_candidates[0][0]
                    item_events.insert(0, f"🤵 {shield_name} крышанулся — за него нельзя было голосовать. Вылетает следующий!")
                else:
                    # Некого выбивать — раунд без выбывания
                    return {"ok": True, "outcome": "shielded", "message": "Крышануться сработала, некого выбивать"}

            # Берём имя ДО выбывания (пока анонимус ещё активен)
            v_name = uname(victim_id)
            outcome = eliminate_player(game_id, victim_id)

            # Если воскрешение сработало — вылетает следующий по голосам
            _resurrected_name = None
            _was_resurrected = False
            if outcome == "resurrected":
                _resurrected_name = uname(victim_id, force_real=True)  # реальное имя даже если был анонимус
                _was_resurrected = True
                # Сжигаем постанову
                try:
                    _conn_res = get_conn(); _c_res = _conn_res.cursor()
                    _c_res.execute("UPDATE items SET status='used' WHERE user_id=? AND item_type='resurrect' AND status='active' LIMIT 1", (victim_id,))
                    _c_res.execute("UPDATE users SET items_used=items_used+1 WHERE user_id=?", (victim_id,))
                    _conn_res.commit(); _conn_res.close()
                except: pass
                next_candidates = [(uid, cnt) for uid, cnt in results if uid != victim_id]
                if next_candidates:
                    second_victim_id = next_candidates[0][0]
                    second_outcome = eliminate_player(game_id, second_victim_id)
                    pass  # сообщение о Постанове уже в основном тексте раунда
                    # Меняем outcome на eliminated чтобы раунд завершился нормально
                    outcome = second_outcome
                    victim_id = second_victim_id
                    v_name = uname(second_victim_id, force_real=True)
                else:
                    # Некого выбивать — воскресший остаётся, раунд без выбывания
                    # Уведомляем чат
                    _no_victim_msg = (
                        f"🗡 <b>Раунд {day} завершён!</b>\n\n"
                        f"🎭 <b>{v_name}</b> замутил Постанову — инсценировал смерть и вернулся в игру!\n"
                        f"😮 Больше некого выбивать — раунд без выбывания!\n\n"
                        f"👥 Осталось: <b>{get_alive_count(game_id)}</b>"
                    )
                    async with httpx.AsyncClient(timeout=10) as _cl_nv:
                        try:
                            await _cl_nv.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                                json={"chat_id": "@shrimpgames_chat", "text": _no_victim_msg, "parse_mode": "HTML"})
                        except: pass
                    outcome = "no_victim"

            # Тратим анонимус у выбывшего если был активен
            try:
                conn_an = get_conn(); c_an = conn_an.cursor()
                c_an.execute("UPDATE items SET status='used' WHERE user_id=? AND item_type='anon_player' AND status='active'", (victim_id,))  # тратим все
                conn_an.commit(); conn_an.close()
            except: pass

            # +kills голосовавшим за жертву
            conn = get_conn()
            c = conn.cursor()
            c.execute("SELECT voter_id FROM votes WHERE game_id=? AND day_number=? AND target_id=?",
                      (game_id, day, victim_id))
            voters = [r["voter_id"] for r in c.fetchall()]
            if outcome == "eliminated":
                for v in voters:
                    if v not in FAKE_IDS:
                        c.execute("UPDATE users SET kills=kills+1 WHERE user_id=?", (v,))
                        try:
                            c.execute("INSERT INTO kills_log (game_id, killer_id, victim_id) VALUES (?,?,?)",
                                      (game_id, v, victim_id))
                        except: pass
                # Если вылетел в первом раунде — считаем first_eliminated
                try:
                    _fe_conn = get_conn(); _fe_c = _fe_conn.cursor()
                    try: _fe_c.execute("ALTER TABLE users ADD COLUMN first_eliminated INTEGER DEFAULT 0")
                    except: pass
                    if day == 1:
                        _fe_c.execute("UPDATE users SET first_eliminated=COALESCE(first_eliminated,0)+1 WHERE user_id=?", (victim_id,))
                    _fe_conn.commit(); _fe_conn.close()
                except: pass
            from datetime import timedelta as _td, datetime as _dt
            # Проверяем — все ли живые проголосовали
            _alive_players = get_game_players(game_id, alive_only=True)
            _alive_ids = [p["user_id"] for p in _alive_players]
            _conn_vc = get_conn(); _c_vc = _conn_vc.cursor()
            _c_vc.execute("SELECT COUNT(DISTINCT voter_id) FROM votes WHERE game_id=? AND day_number=?", (game_id, day))
            _voted_cnt = _c_vc.fetchone()[0]; _conn_vc.close()
            _all_voted = _voted_cnt >= len(_alive_ids)
            # тест: 60 сек, реал: 1 час (или до 08:00 если ночь)
            _is_test = any(str(uid) in ["9000001","9000002","9000003","9000004"] for uid in _alive_ids)
            if _is_test and _all_voted:
                _vote_delta = _td(seconds=60)
                new_voting_ends = (_dt.utcnow() + _vote_delta).strftime("%Y-%m-%d %H:%M:%S")
            else:
                _now_utc = _dt.utcnow()
                _tallinn_now = _now_utc + _td(hours=3)
                _tallinn_hour = _tallinn_now.hour
                _tallinn_min = _tallinn_now.minute
                _is_night = (_tallinn_hour >= 22) or (_tallinn_hour < 7)
                if _is_night:
                    # Ночь — следующее голосование в 07:00 Таллин = 04:00 UTC
                    if _tallinn_hour < 7:
                        _next_7am = _now_utc.replace(hour=4, minute=0, second=0, microsecond=0)
                    else:
                        _next_7am = (_now_utc + _td(days=1)).replace(hour=4, minute=0, second=0, microsecond=0)
                    new_voting_ends = _next_7am.strftime("%Y-%m-%d %H:%M:%S")
                else:
                    new_voting_ends = (_now_utc + _td(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
            c.execute("UPDATE games SET current_day=current_day+1, voting_ends=? WHERE id=?", (new_voting_ends, game_id,))
            conn.commit()
            conn.close()

            # Сжигаем по одной крыше у каждого живого игрока у кого она есть — раунд прошёл
            try:
                conn_sb = get_conn(); c_sb = conn_sb.cursor()
                # Берём всех с активной крышей (game_id может быть NULL если куплено через Stars)
                c_sb.execute(
                    "SELECT DISTINCT user_id FROM items WHERE item_type='shield' AND status='active' AND user_id IN (SELECT user_id FROM players WHERE game_id=? AND is_alive=1)",
                    (game_id,)
                )
                for _row in c_sb.fetchall():
                    _uid = _row["user_id"]
                    c_sb.execute(
                        "UPDATE items SET status='used' WHERE id=(SELECT id FROM items WHERE user_id=? AND item_type='shield' AND status='active' LIMIT 1)",
                        (_uid,)
                    )
                c_sb.execute("DELETE FROM settings WHERE key LIKE ?", (f"shield_active_{game_id}_{day}_%",))
                conn_sb.commit(); conn_sb.close()
            except Exception as _esb: print(f"[SHIELD BURN] {_esb}")

            alive = get_alive_count(game_id)

            # Сообщение в чат
            if _was_resurrected:
                # Счётчик воскрешения
                try:
                    conn_rs = get_conn(); c_rs = conn_rs.cursor()
                    c_rs.execute("UPDATE users SET resurrected=resurrected+1 WHERE user_id=?", (victim_id,))
                    conn_rs.commit(); conn_rs.close()
                except: pass
                # Имя второй жертвы (кто реально вылетел)
                items_line = ("\n\n" + "\n".join(item_events)) if item_events else ""
                _real_loser = v_name  # это уже второй (кто реально вылетел)
                chat_msg = (
                    f"🗡 <b>Раунд {day} завершён!</b>\n\n"
                    f"📊 Счёт голосов:\n{score_text}\n\n"
                    f"🎭 <b>{_resurrected_name}</b> замутил Постанову — инсценировал свою смерть чтобы всех обмануть! Возвращается в игру.\n"
                    f"🍳 Вылетает <b>{_real_loser}</b>\n\n"
                    f"👥 Осталось: <b>{alive}</b>"
                    f"{items_line}" + BOT_LINK
                )
            else:
                items_line = ("\n\n" + "\n".join(item_events)) if item_events else ""
                # Если жертва была анонимусом — раскрываем реальное имя
                _real_victim_name = uname(victim_id, force_real=True)
                _was_anon = v_name == "Анонимус"
                _anon_reveal = f"\n🎭 Его звали <b>{_real_victim_name}</b>" if _was_anon else ""
                elim_phrases = [
                    f"🍳 <b>{v_name}</b> ликвидирован!{_anon_reveal}",
                    f"💀 <b>{v_name}</b> выбывает. Голосование беспощадно.{_anon_reveal}",
                    f"🗡 <b>{v_name}</b> не выжил. Таков закон района.{_anon_reveal}",
                    f"⚡ <b>{v_name}</b> ликвидирован. Жестоко, но честно.{_anon_reveal}",
                ]
                import random as _rand
                # Выбиваем второго — следующего по голосам после первой жертвы
                _second_v_name = None
                _remaining_candidates = [(uid, cnt) for uid, cnt in results if uid != victim_id]
                if _remaining_candidates and get_alive_count(game_id) > 5:
                    _second_victim_id = _remaining_candidates[0][0]
                    _second_v_name_raw = uname(_second_victim_id)
                    _second_outcome = eliminate_player(game_id, _second_victim_id)
                    if _second_outcome == "resurrected":
                        # Постанова у второго — не считаем его выбывшим
                        _second_v_name = None
                    else:
                        _second_v_name = _second_v_name_raw
                        # +kills голосовавшим за второго
                        try:
                            conn_k2 = get_conn(); c_k2 = conn_k2.cursor()
                            c_k2.execute("SELECT voter_id FROM votes WHERE game_id=? AND day_number=? AND target_id=?",
                                         (game_id, day, _second_victim_id))
                            for _vr in c_k2.fetchall():
                                if _vr["voter_id"] not in FAKE_IDS:
                                    c_k2.execute("UPDATE users SET kills=kills+1 WHERE user_id=?", (_vr["voter_id"],))
                            conn_k2.commit(); conn_k2.close()
                        except: pass

                # Список только живых игроков после выбывания
                _alive_players_msg = get_game_players(game_id, alive_only=True)
                _players_lines = [f"  🗡 {uname(_p['user_id'])}" for _p in _alive_players_msg]
                _players_text = "\n".join(_players_lines)
                alive = get_alive_count(game_id)
                _second_line = f"\n🍳 <b>{_second_v_name}</b> тоже вылетает — второй по голосам!" if _second_v_name else ""
                chat_msg = (
                    f"🗡 <b>Раунд {day} завершён!</b>\n\n"
                    f"📊 Счёт голосов:\n{score_text}\n\n"
                    f"{_rand.choice(elim_phrases)}"
                    f"{_second_line}\n\n"
                    f"👥 Осталось: <b>{alive}</b>"
                    f"{items_line}" + BOT_LINK
                )

            # Сообщение "все проголосовали" убрано

            # Уведомление о старте нового раунда голосования
            try:
                _next_day = day + 1
                _alive_for_notify = get_game_players(game_id, alive_only=True)
                _vote_kb_new = {"inline_keyboard": [[
                    {"text": "\U0001f5f3 Голосовать", "web_app": {"url": WEBAPP_URL}}
                ]]}
                for _np in _alive_for_notify:
                    if _np["user_id"] in FAKE_IDS:
                        continue
                    if not check_notifications(_np["user_id"]):
                        continue
                    try:
                        await cl.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                            json={"chat_id": _np["user_id"],
                                  "text": f"\U0001f5f3 <b>Раунд {_next_day} \u2014 голосование открыто!</b>\n\nВыбирай кого убрать с района. У тебя 15 минут.",
                                  "parse_mode": "HTML",
                                  "reply_markup": _vote_kb_new})
                    except: pass
            except: pass

            # Пишем в чат
            try:
                await cl.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={"chat_id": "@shrimpgames_chat", "text": chat_msg, "parse_mode": "HTML", "disable_web_page_preview": True})
            except: pass
            # Детальный лог в приватную группу
            await log_event(cl, detailed_log + "\n\n" + chat_msg)

            # Личное уведомление выбывшему — всегда, независимо от тумблера пушей
            try:
                shop_url = WEBAPP_URL + "?buy=resurrect"
                killed_kb = {"inline_keyboard": [[
                    {"text": "🎭 Активировать Постанову", "web_app": {"url": shop_url}}
                ]]}
                await cl.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={"chat_id": victim_id,
                          "text": f"💀 <b>Тебя ликвидировали!</b>\n\nТы выбыл в раунде {day}.\n\n🎭 <b>Постанова</b> — это связь за 40 ⭐, которая позволяет инсценировать смерть и вернуться в игру. Купи и активируй прямо сейчас — пока не поздно!",
                          "parse_mode": "HTML",
                          "reply_markup": killed_kb})
            except: pass

            # Проверка победителя
            # При alive==2 — оба в топ-3, при alive==3 — все трое в топ-3
            if alive in (2, 3):
                try:
                    _top3_players = get_game_players(game_id, alive_only=True)
                    _conn_t3 = get_conn(); _c_t3 = _conn_t3.cursor()
                    for _tp in _top3_players:
                        _c_t3.execute("UPDATE users SET top3=top3+1 WHERE user_id=?", (_tp["user_id"],))
                    _conn_t3.commit(); _conn_t3.close()
                except: pass
            if alive == 1:
                winner_players = get_game_players(game_id, alive_only=True)
                winner_id = winner_players[0]["user_id"] if winner_players else None
                if winner_id:
                    w_name = uname(winner_id, force_real=True)
                    # Завершаем игру
                    conn = get_conn()
                    c = conn.cursor()
                    c.execute("UPDATE games SET status='finished', finished_at=CURRENT_TIMESTAMP, winner_id=? WHERE id=?",
                              (winner_id, game_id))
                    c.execute("UPDATE users SET wins=wins+1 WHERE user_id=?", (winner_id,))
                    # Неприкасаемый — победил без голосов против
                    conn_cw = get_conn(); c_cw = conn_cw.cursor()
                    c_cw.execute("SELECT times_voted_against FROM users WHERE user_id=?", (winner_id,))
                    _tva = c_cw.fetchone()
                    if _tva and (_tva["times_voted_against"] or 0) == 0:
                        c_cw.execute("UPDATE users SET clean_wins=clean_wins+1 WHERE user_id=?", (winner_id,))
                    conn_cw.commit(); conn_cw.close()
                    # Если победитель был Анонимусом
                    anon_check = get_user_items(winner_id)
                    if any(i["item_type"]=="anon_player" for i in anon_check):
                        c.execute("UPDATE users SET won_as_anon=won_as_anon+1 WHERE user_id=?", (winner_id,))
                        c.execute("UPDATE items SET status='used' WHERE user_id=? AND item_type='anon_player' AND status='active'", (winner_id,))
                    conn.commit()
                    conn.close()
                    win_msg = (
                        f"🏆 <b>ИГРА В КРЕВЕТКУ ЗАВЕРШЕНА!</b>\n\n"
                        f"👑 Последняя выжившая креветка: <b>{w_name}</b>\n\n"
                        f"🎁 NFT приз отправляется победителю. Поздравляем!" + BOT_LINK
                    )
                    try:
                        await cl.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                            json={"chat_id": "@shrimpgames_chat",
                                  "text": win_msg, "parse_mode": "HTML"})
                    except: pass
                    if check_notifications(winner_id):
                        try:
                            await cl.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                                json={"chat_id": winner_id,
                                      "text": f"✅ <b>Ты победил в Разборках на районе!</b> Ты последний выживший на районе 🗡",
                                      "parse_mode": "HTML"})
                        except: pass
                    # Ставим таймер следующей игры — 24 часа
                    from datetime import timedelta as _tdn, datetime as _dtn
                    _next_game = (_dtn.utcnow() + _tdn(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
                    try:
                        conn_ng = get_conn(); c_ng = conn_ng.cursor()
                        c_ng.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
                        c_ng.execute("INSERT OR REPLACE INTO settings (key,value) VALUES ('next_game_at',?)", (_next_game,))
                        conn_ng.commit(); conn_ng.close()
                    except: pass
                    asyncio.create_task(auto_create_next_game(game_id))
                return {"ok": True, "outcome": "game_over", "winner_id": winner_id, "winner_name": w_name}



            # Уведомить живых — новый раунд
            players = get_game_players(game_id, alive_only=True)
            vote_kb = {"inline_keyboard": [[{"text": "🗳 Голосовать", "web_app": {"url": WEBAPP_URL}}]]}
            # Объявление нового раунда в чат
            new_day_num = (game["current_day"] or 1) + 1
            round_phrases = [
                f"Кто следующий на выход?",
                f"Союзники или предатели — выбирай мудро.",
                f"Каждый голос решает судьбу.",
                f"Время голосовать. Не жди.",
            ]
            import random as _rand3
            round_msg = (
                f"⚔️ <b>Раунд {new_day_num} начался!</b>\n\n"
                f"👥 {alive} креветок борются за выживание.\n"
                f"{_rand3.choice(round_phrases)}\n\n"
                f"👇 Голосуй прямо сейчас:" + BOT_LINK
            )
            try:
                await cl.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={"chat_id": "@shrimpgames_chat",
                          "text": round_msg, "parse_mode": "HTML",
                          "reply_markup": {"inline_keyboard": [[{"text": "🗳 Голосовать", "web_app": {"url": WEBAPP_URL}}]]}})
            except: pass

            new_day_num2 = (game["current_day"] or 1) + 1
            short_msg = f"✅ <b>Раунд {new_day_num2} голосования начался!</b> Сделай свой выбор!"
            for p in players:
                if not check_notifications(p["user_id"]):
                    continue
                if p["user_id"] in FAKE_IDS:
                    continue
                try:
                    await cl.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                        json={"chat_id": p["user_id"], "text": short_msg,
                              "parse_mode": "HTML", "reply_markup": vote_kb})
                except: pass

            return {"ok": True, "outcome": outcome, "victim_id": victim_id,
                    "victim_name": v_name, "alive": alive, "tie": False}

        else:
            # При равных голосах — проверяем есть ли у кого-то Решала
            tiebreaker_winner = None
            for lid in leaders:
                titems = get_user_items(lid, game_id)
                if any(i["item_type"] == "tiebreaker" for i in titems):
                    tiebreaker_winner = lid
                    # Тратим предмет
                    conn_tb = get_conn()
                    c_tb = conn_tb.cursor()
                    c_tb.execute("UPDATE items SET status='used' WHERE user_id=? AND item_type='tiebreaker' AND status='active' LIMIT 1", (lid,))
                    conn_tb.commit()
                    conn_tb.close()
                    break

            if tiebreaker_winner:
                # Решала спасает — выбывает другой лидер
                victim_id = next(l for l in leaders if l != tiebreaker_winner)
                async with httpx.AsyncClient() as _cl_tb:
                    try:
                        await _cl_tb.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                            json={"chat_id": "@shrimpgames_chat",
                                  "text": f"🔨 <b>Решала сработал!</b> При ничье один из игроков использовал Решалу и остался в игре.",
                                  "parse_mode": "HTML"})
                    except: pass
            else:
                # Нет Решалы — проверяем премиум у проголосовавших за каждого лидера
                # Побеждает тот за кого проголосовал премиум-игрок
                premium_victim = None
                conn_pm = get_conn(); c_pm = conn_pm.cursor()
                for lid in leaders:
                    # Кто голосовал за этого лидера
                    c_pm.execute("SELECT voter_id FROM votes WHERE game_id=? AND day_number=? AND target_id=?",
                                 (game_id, day, lid))
                    voters_for_lid = [r["voter_id"] for r in c_pm.fetchall()]
                    # Есть ли среди них премиум-игрок с оставшимися использованиями (макс 3 за игру)
                    for voter in voters_for_lid:
                        voter_stats = get_user_stats(voter)
                        is_prem = voter_stats.get("is_premium", False)
                        if is_prem:
                            tb_key = f"premium_tiebreak_{game_id}_{voter}"
                            c_pm.execute("SELECT value FROM settings WHERE key=?", (tb_key,))
                            tb_row = c_pm.fetchone()
                            used_count = int(tb_row["value"]) if tb_row else 0
                            if used_count < 3:
                                c_pm.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)",
                                             (tb_key, str(used_count + 1)))
                                # За lid проголосовал премиум-игрок — lid выбывает
                                premium_victim = lid
                                break
                    if premium_victim:
                        break
                conn_pm.commit()
                conn_pm.close()

                if premium_victim:
                    victim_id = premium_victim
                    item_events.insert(0, "👑 При ничье голос Премиум-игрока стал решающим!")
                else:
                    # Нет ни Решалы ни Премиума — выбывает по времени первого голоса
                    victim_id = leaders[0]

            # Берём имя ДО выбывания (пока анонимус ещё активен)
            v_name = uname(victim_id)
            outcome = eliminate_player(game_id, victim_id)

            conn = get_conn()
            c = conn.cursor()
            c.execute("SELECT voter_id FROM votes WHERE game_id=? AND day_number=? AND target_id=?",
                      (game_id, day, victim_id))
            voters = [r["voter_id"] for r in c.fetchall()]
            if outcome == "eliminated":
                for v in voters:
                    if v not in FAKE_IDS:
                        c.execute("UPDATE users SET kills=kills+1 WHERE user_id=?", (v,))
                        try:
                            c.execute("INSERT INTO kills_log (game_id, killer_id, victim_id) VALUES (?,?,?)",
                                      (game_id, v, victim_id))
                        except: pass

            # Выбиваем второго — следующего по голосам после первой жертвы (ничья)
            _second_v_name_tie = None
            _tie_remaining = [(uid, cnt) for uid, cnt in results if uid != victim_id]
            if _tie_remaining and get_alive_count(game_id) > 5:
                _second_vic_tie = _tie_remaining[0][0]
                _second_name_tie_raw = uname(_second_vic_tie)
                _second_out_tie = eliminate_player(game_id, _second_vic_tie)
                if _second_out_tie != "resurrected":
                    _second_v_name_tie = _second_name_tie_raw
                    try:
                        conn_k3 = get_conn(); c_k3 = conn_k3.cursor()
                        c_k3.execute("SELECT voter_id FROM votes WHERE game_id=? AND day_number=? AND target_id=?",
                                     (game_id, day, _second_vic_tie))
                        for _vr3 in c_k3.fetchall():
                            if _vr3["voter_id"] not in FAKE_IDS:
                                c_k3.execute("UPDATE users SET kills=kills+1 WHERE user_id=?", (_vr3["voter_id"],))
                        conn_k3.commit(); conn_k3.close()
                    except: pass
            from datetime import timedelta as _td, datetime as _dt
            _alive_players2 = get_game_players(game_id, alive_only=True)
            _conn_vc2 = get_conn(); _c_vc2 = _conn_vc2.cursor()
            _c_vc2.execute("SELECT COUNT(DISTINCT voter_id) FROM votes WHERE game_id=? AND day_number=?", (game_id, day))
            _voted_cnt2 = _c_vc2.fetchone()[0]; _conn_vc2.close()
            _all_voted2 = _voted_cnt2 >= len(_alive_players2)
            _alive_ids2 = [p["user_id"] for p in _alive_players2]
            _is_test2 = any(uid in [9000001,9000002,9000003,9000004] for uid in _alive_ids2)
            if _is_test2 and _all_voted2:
                _vote_delta2 = _td(seconds=60)
                new_voting_ends = (_dt.utcnow() + _vote_delta2).strftime("%Y-%m-%d %H:%M:%S")
            else:
                _now_utc2 = _dt.utcnow()
                _tallinn_hour2 = (_now_utc2 + _td(hours=3)).hour
                if _tallinn_hour2 >= 22 or _tallinn_hour2 < 7:
                    if _tallinn_hour2 < 7:
                        _next_8am2 = _now_utc2.replace(hour=4, minute=0, second=0, microsecond=0)
                    else:
                        _next_8am2 = (_now_utc2 + _td(days=1)).replace(hour=4, minute=0, second=0, microsecond=0)
                    new_voting_ends = _next_8am2.strftime("%Y-%m-%d %H:%M:%S")
                else:
                    new_voting_ends = (_now_utc2 + _td(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
            c.execute("UPDATE games SET current_day=current_day+1, voting_ends=? WHERE id=?", (new_voting_ends, game_id,))
            conn.commit()
            conn.close()

            # Сжигаем по одной крыше у каждого живого игрока — раунд прошёл
            try:
                conn_sb2 = get_conn(); c_sb2 = conn_sb2.cursor()
                c_sb2.execute(
                    "SELECT DISTINCT user_id FROM items WHERE item_type='shield' AND status='active' AND user_id IN (SELECT user_id FROM players WHERE game_id=? AND is_alive=1)",
                    (game_id,)
                )
                for _row2 in c_sb2.fetchall():
                    _uid2 = _row2["user_id"]
                    c_sb2.execute(
                        "UPDATE items SET status='used' WHERE id=(SELECT id FROM items WHERE user_id=? AND item_type='shield' AND status='active' LIMIT 1)",
                        (_uid2,)
                    )
                c_sb2.execute("DELETE FROM settings WHERE key LIKE ?", (f"shield_active_{game_id}_{day}_%",))
                conn_sb2.commit(); conn_sb2.close()
            except Exception as _esb2: print(f"[SHIELD BURN2] {_esb2}")

            alive = get_alive_count(game_id)
            _alive_msg2 = get_game_players(game_id, alive_only=True)
            _players_text2 = "\n".join([f"  🗡 {uname(_p['user_id'])}" for _p in _alive_msg2])
            _items_line2 = ("\n\n" + "\n".join(item_events)) if item_events else ""
            _second_tie_line = f"\n🍳 <b>{_second_v_name_tie}</b> тоже вылетает — второй по голосам!" if _second_v_name_tie else ""
            chat_msg = (
                f"🗡 <b>Раунд {day} завершён!</b>\n\n"
                f"📊 Счёт голосов:\n{score_text}\n\n"
                f"🍳 <b>{v_name}</b> ликвидирован (при равном счёте решил первый голос)!"
                f"{_second_tie_line}\n\n"
                f"👥 Осталось: <b>{alive}</b>"
                f"{_items_line2}" + BOT_LINK
            )
            try:
                await cl.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={"chat_id": "@shrimpgames_chat", "text": chat_msg, "parse_mode": "HTML"})
            except: pass
            await log_event(cl, detailed_log + "\n\n" + chat_msg)

            pass  # уведомление при ничье убрано

            if alive in (2, 3):
                try:
                    _top3_players2 = get_game_players(game_id, alive_only=True)
                    _conn_t3b = get_conn(); _c_t3b = _conn_t3b.cursor()
                    for _tp2 in _top3_players2:
                        _c_t3b.execute("UPDATE users SET top3=top3+1 WHERE user_id=?", (_tp2["user_id"],))
                    _conn_t3b.commit(); _conn_t3b.close()
                except: pass
            if alive == 1:
                winner_players = get_game_players(game_id, alive_only=True)
                winner_id = winner_players[0]["user_id"] if winner_players else None
                if winner_id:
                    w_name = uname(winner_id, force_real=True)
                    conn = get_conn()
                    c = conn.cursor()
                    c.execute("UPDATE games SET status='finished', finished_at=CURRENT_TIMESTAMP, winner_id=? WHERE id=?",
                              (winner_id, game_id))
                    c.execute("UPDATE users SET wins=wins+1 WHERE user_id=?", (winner_id,))
                    # Неприкасаемый — победил без голосов против
                    conn_cw = get_conn(); c_cw = conn_cw.cursor()
                    c_cw.execute("SELECT times_voted_against FROM users WHERE user_id=?", (winner_id,))
                    _tva = c_cw.fetchone()
                    if _tva and (_tva["times_voted_against"] or 0) == 0:
                        c_cw.execute("UPDATE users SET clean_wins=clean_wins+1 WHERE user_id=?", (winner_id,))
                    conn_cw.commit(); conn_cw.close()
                    conn.commit()
                    conn.close()
                    win_msg2 = (
                        f"🏆 <b>ИГРА ЗАВЕРШЕНА!</b>\n\n"
                        f"🗡 Победитель: <b>{w_name}</b>\n\n"
                        f"Поздравляем! Приз будет отправлен победителю." + BOT_LINK
                    )
                    try:
                        await cl.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                            json={"chat_id": "@shrimpgames_chat",
                                  "text": win_msg2, "parse_mode": "HTML"})
                    except: pass
                    try:
                        await cl.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                            json={"chat_id": winner_id,
                                  "text": f"✅ <b>Ты победил в Разборках на районе!</b> Ты последний выживший на районе 🗡",
                                  "parse_mode": "HTML"})
                    except: pass
                    # Ставим таймер следующей игры — 24 часа
                    from datetime import timedelta as _tdn2, datetime as _dtn2
                    _next_game2 = (_dtn2.utcnow() + _tdn2(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
                    try:
                        conn_ng2 = get_conn(); c_ng2 = conn_ng2.cursor()
                        c_ng2.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
                        c_ng2.execute("INSERT OR REPLACE INTO settings (key,value) VALUES ('next_game_at',?)", (_next_game2,))
                        conn_ng2.commit(); conn_ng2.close()
                    except: pass
                    asyncio.create_task(auto_create_next_game(game_id))
                return {"ok": True, "outcome": "game_over", "winner_id": winner_id, "winner_name": w_name}

            return {"ok": True, "outcome": outcome, "victim_id": victim_id,
                    "victim_name": v_name, "alive": alive, "tie": False}


@app.get("/api/game/tiebreaker")
async def get_tiebreaker(game_id: int = None, day: int = None):
    """Получить список игроков для переголосования если ничья"""
    game = get_active_game()
    if not game:
        return {"ok": False}

    from database import get_vote_results
    current_day = game["current_day"] or 1
    # Смотрим предыдущий день на ничью
    if current_day <= 1:
        return {"ok": True, "tiebreaker": False}

    prev_day = current_day - 1
    results = get_vote_results(game["id"], prev_day)
    if not results:
        return {"ok": True, "tiebreaker": False}

    max_votes = results[0][1]
    leaders = [uid for uid, cnt in results if cnt == max_votes]

    if len(leaders) > 1:
        players = get_game_players(game["id"], alive_only=True)
        tied = [p for p in players if p["user_id"] in leaders]
        return {"ok": True, "tiebreaker": True, "tied_players": tied}

    return {"ok": True, "tiebreaker": False}

@app.get("/api/game/spy")
async def use_spy(user_id: int):
    """Стукач — сколько голосов за всю игру, кто и сколько раз голосовал против, кто голосовал в текущем раунде"""
    game = get_active_game()
    if not game or game["status"] != "active":
        return {"ok": False, "error": "Игра не активна"}

    # Проверить что юзер живой
    _conn_spy = get_conn(); _c_spy = _conn_spy.cursor()
    _pr_spy = _c_spy.execute("SELECT is_alive FROM players WHERE game_id=? AND user_id=?", (game["id"], user_id)).fetchone()
    _conn_spy.close()
    if not _pr_spy or not _pr_spy["is_alive"]:
        return {"ok": False, "error": "Ты выбыл из игры"}

    # Проверить что у юзера есть стукач
    items = get_user_items(user_id)
    has_spy = any(i["item_type"] == "spy" for i in items)
    if not has_spy:
        return {"ok": False, "error": "У тебя нет Стукача"}

    game_id = game["id"]
    current_day = game["current_day"] or 1

    conn = get_conn()
    c = conn.cursor()

    # Кто голосовал против этого юзера за ВСЮ игру и сколько раз
    c.execute("""
        SELECT v.voter_id, u.username, u.first_name, COUNT(*) as times
        FROM votes v JOIN users u ON v.voter_id = u.user_id
        WHERE v.game_id=? AND v.target_id=?
        GROUP BY v.voter_id
        ORDER BY times DESC
    """, (game_id, user_id))
    all_voters = c.fetchall()

    # Кто голосовал против этого юзера в ТЕКУЩЕМ раунде
    c.execute("""
        SELECT v.voter_id, u.username, u.first_name
        FROM votes v JOIN users u ON v.voter_id = u.user_id
        WHERE v.game_id=? AND v.day_number=? AND v.target_id=?
    """, (game_id, current_day, user_id))
    current_voters = c.fetchall()

    # Итого голосов за всю игру
    c.execute("SELECT COUNT(*) as total FROM votes WHERE game_id=? AND target_id=?", (game_id, user_id))
    total_row = c.fetchone()
    total_votes = total_row["total"] if total_row else 0

    conn.close()


    # Стукач списывается через /api/game/spy/use (вызывается с фронта после показа)
    # Скрываем имена анонимусов
    def _voter_name(voter_id, username, first_name):
        anon_items = get_user_items(voter_id, game_id)
        if any(i["item_type"] == "anon_player" for i in anon_items):
            return "Анонимус"
        return first_name or username or f"ID{voter_id}"

    _spy_name = get_display_name(user_id)
    push_event(game_id, "use", f"🐭 {_spy_name} слил Стукача — узнал кто против него голосовал", "🐭")
    await notify_ability_activate("spy", username=_spy_name)
    return {
        "ok": True,
        "current_day": current_day,
        "total_votes_against": total_votes,
        "all_voters": [{
            "user_id": r["voter_id"],
            "name": _voter_name(r["voter_id"], r["username"], r["first_name"]),
            "times": r["times"]
        } for r in all_voters],
        "current_round_voters": [{
            "user_id": r["voter_id"],
            "name": _voter_name(r["voter_id"], r["username"], r["first_name"])
        } for r in current_voters]
    }


@app.post("/api/game/spy/use")
async def consume_spy(request: Request):
    """Израсходовать стукача после использования"""
    body = await request.json()
    user_id = body.get("user_id")
    game = get_active_game()
    if not game or not user_id:
        return {"ok": False}

    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        UPDATE items SET status='used'
        WHERE id=(
            SELECT id FROM items
            WHERE user_id=? AND item_type='spy' AND status='active'
            LIMIT 1
        )
    """, (user_id,))
    conn.commit()
    conn.close()
    # Счётчик стукача
    try:
        conn_sp = get_conn(); c_sp = conn_sp.cursor()
        c_sp.execute("UPDATE users SET used_spy=used_spy+1, items_used=items_used+1 WHERE user_id=?", (user_id,))
        conn_sp.commit(); conn_sp.close()
    except: pass
    return {"ok": True}

@app.post("/api/game/anon_message")
async def anon_message(request: Request):
    """Отправить анонимное сообщение игроку"""
    body = await request.json()
    sender_id = body.get("sender_id")
    target_id = body.get("target_id")
    text = body.get("text", "").strip()

    if not text or not target_id or not sender_id:
        return {"ok": False, "error": "Неверные данные"}
    if len(text) > 300:
        return {"ok": False, "error": "Максимум 300 символов"}

    # Проверяем что отправитель живой
    _conn_am_chk = get_conn(); _c_am_chk = _conn_am_chk.cursor()
    _game_am = _c_am_chk.execute("SELECT id FROM games WHERE status='active' LIMIT 1").fetchone()
    if _game_am:
        _pr_am = _c_am_chk.execute("SELECT is_alive FROM players WHERE game_id=? AND user_id=?", (_game_am["id"], sender_id)).fetchone()
        if _pr_am and not _pr_am["is_alive"]:
            _conn_am_chk.close()
            return {"ok": False, "error": "Ты выбыл из игры"}
    _conn_am_chk.close()

    _has_phone = has_bomzh_item(sender_id, 'phone')
    conn_am = get_conn(); c_am = conn_am.cursor()
    c_am.execute("SELECT id FROM items WHERE user_id=? AND item_type='anon_msg' AND status='active' LIMIT 1", (sender_id,))
    item_am = c_am.fetchone()
    conn_am.close()
    if not item_am and not _has_phone:
        return {"ok": False, "error": "Нет предмета Сговориться"}
    item_am_id = int(item_am["id"]) if item_am else None

    # Сначала отправляем — потом списываем
    try:
        async with httpx.AsyncClient() as cl:
            tg_resp = await cl.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": target_id,
                    "text": f"🪄 <b>Анонимное сообщение от участника игры:</b>\n\n{text}",
                    "parse_mode": "HTML"
                }
            )
            tg_data = tg_resp.json()
            if not tg_data.get("ok"):
                tg_err = tg_data.get("description", "Telegram error")
                return {"ok": False, "error": f"Получатель не начал диалог с ботом ({tg_err})"}

            # Telegram принял — теперь списываем предмет
            conn_use = get_conn(); c_use = conn_use.cursor()
            if item_am_id and not _has_phone:
                c_use.execute("UPDATE items SET status='used' WHERE id=?", (item_am_id,))
            c_use.execute("UPDATE users SET sent_anon=sent_anon+1, items_used=items_used+1 WHERE user_id=?", (sender_id,))
            conn_use.commit(); conn_use.close()

            # Определяем имя отправителя — анонимус или реальный ник
            conn_sn = get_conn(); c_sn = conn_sn.cursor()
            c_sn.execute("SELECT id FROM items WHERE user_id=? AND item_type='anon_player' AND status='active' LIMIT 1", (sender_id,))
            is_sender_anon = c_sn.fetchone() is not None
            if not is_sender_anon:
                c_sn.execute("SELECT first_name, username FROM users WHERE user_id=?", (sender_id,))
                _su = c_sn.fetchone()
                sender_label = (_su["first_name"] or _su["username"] or "Игрок") if _su else "Игрок"
            else:
                sender_label = "Анонимус"
            conn_sn.close()

            # Интрига в чат
            import random as _r2
            anon_msgs = [
                f"💌 {sender_label} отправил тайное послание одному из игроков...",
                f"💌 {sender_label} вышел на связь из тени. Интриги нарастают.",
                f"💌 Тайный переговорщик {sender_label} вышел на связь...",
            ]
            try:
                await cl.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={"chat_id": "@shrimpgames_chat",
                          "text": _r2.choice(anon_msgs), "parse_mode": "HTML"})
            except: pass

        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/game/final_vote")
async def final_vote(request: Request):
    """Голосование выбывших за финалиста"""
    body = await request.json()
    voter_id = body.get("voter_id")
    target_id = body.get("target_id")
    game_id_body = body.get("game_id")

    conn = get_conn()
    c = conn.cursor()
    # Проверить что voter_id уже выбыл
    c.execute("SELECT is_alive FROM players WHERE game_id=? AND user_id=?", (game_id_body, voter_id))
    row = c.fetchone()
    if not row or row["is_alive"] == 1:
        conn.close()
        return {"ok": False, "error": "Голосовать могут только выбывшие"}

    # Записать в отдельную таблицу final_votes
    try:
        c.execute("""
            INSERT OR REPLACE INTO final_votes (game_id, voter_id, target_id)
            VALUES (?,?,?)
        """, (game_id_body, voter_id, target_id))
        conn.commit()
    except:
        conn.rollback()
    conn.close()
    return {"ok": True}




@app.post("/api/user/notifications")
async def set_notifications(request: Request):
    body = await request.json()
    user_id = body.get("user_id")
    enabled = body.get("enabled", True)
    try:
        conn = get_conn()
        c = conn.cursor()
        c.execute("ALTER TABLE users ADD COLUMN notifications_enabled INTEGER DEFAULT 1")
        conn.commit()
    except: pass
    try:
        conn = get_conn()
        c = conn.cursor()
        c.execute("UPDATE users SET notifications_enabled=? WHERE user_id=?", (1 if enabled else 0, user_id))
        conn.commit()
        conn.close()
    except: pass
    return {"ok": True}


@app.get("/api/game/next_timer")
async def next_game_timer():
    """Таймер до следующей игры"""
    import calendar
    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute("SELECT value FROM settings WHERE key='next_game_at'")
        row = c.fetchone()
        conn.close()
        if row:
            from datetime import datetime as _dt2
            t = _dt2.strptime(row["value"], "%Y-%m-%d %H:%M:%S")
            ms = int(calendar.timegm(t.timetuple()) * 1000)
            return {"ok": True, "target": ms}
    except:
        conn.close()
    # По умолчанию — 24 часа от сейчас
    return {"ok": True, "target": int((__import__('time').time() + 24*3600)*1000)}


@app.post("/api/test/launch")
async def test_launch(request: Request):
    """Одна кнопка — сброс + создать игру + добавить ботов + старт + авто-цикл"""
    body = await request.json()
    if body.get("user_id") != ADMIN_ID:
        return JSONResponse({"ok": False, "error": "Forbidden"}, status_code=403)

    import asyncio
    from datetime import datetime as _dt, timedelta as _td

    FAKE_PLAYERS = [
        {"user_id": 9000001, "username": "test_alice",   "first_name": "Alice"},
        {"user_id": 9000002, "username": "test_bob",     "first_name": "Bob"},
        {"user_id": 9000003, "username": "test_charlie", "first_name": "Charlie"},
        {"user_id": 9000004, "username": "test_diana",   "first_name": "Diana"},
    ]
    FAKE_IDS_LIST = [p["user_id"] for p in FAKE_PLAYERS]
    ph = ",".join("?" * len(FAKE_IDS_LIST))

    conn = get_conn()
    c = conn.cursor()

    # --- RESET ---
    c.execute("DELETE FROM votes")
    c.execute(f"DELETE FROM players WHERE user_id IN ({ph})", FAKE_IDS_LIST)
    c.execute(f"DELETE FROM items WHERE user_id IN ({ph})", FAKE_IDS_LIST)
    c.execute(f"DELETE FROM users WHERE user_id IN ({ph})", FAKE_IDS_LIST)
    c.execute("DELETE FROM games WHERE number=99")
    try: c.execute("DELETE FROM settings")
    except: pass
    conn.commit()

    # --- СОЗДАТЬ ИГРУ ---
    c.execute("SELECT MAX(number) as mx FROM games WHERE number < 90")
    row = c.fetchone()
    next_num = (row["mx"] or 0) + 1
    c.execute("INSERT INTO games (number, status, max_players, prize_desc) VALUES (?,?,?,?)",
              (next_num, "waiting", 0, "NFT Giraffe Pool Float"))
    game_id = c.lastrowid
    conn.commit()

    # --- ДОБАВИТЬ БОТОВ ---
    for p in FAKE_PLAYERS:
        c.execute("INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?,?,?)",
                  (p["user_id"], p["username"], p["first_name"]))
    conn.commit()
    for p in FAKE_PLAYERS:
        c.execute("INSERT OR IGNORE INTO players (game_id, user_id) VALUES (?,?)", (game_id, p["user_id"]))
    # Добавить реального юзера
    c.execute("INSERT OR IGNORE INTO players (game_id, user_id) VALUES (?,?)", (game_id, ADMIN_ID))
    conn.commit()

    # --- СТАРТ ---
    voting_ends = (_dt.utcnow() + _td(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
    c.execute("UPDATE games SET status='active', current_day=1, started_at=CURRENT_TIMESTAMP, voting_ends=? WHERE id=?",
              (voting_ends, game_id))
    c.execute("UPDATE users SET games_played=games_played+1 WHERE user_id IN (SELECT user_id FROM players WHERE game_id=?)",
              (game_id,))
    conn.commit()
    conn.close()

    # --- СООБЩЕНИЕ В ЧАТ О СТАРТЕ ---
    async def _send_start_msg():
        import httpx as _hx2
        try:
            players_list = get_game_players(game_id)
            def _start_name(p):
                anon = get_user_items(p['user_id'], game_id)
                if any(i['item_type'] == 'anon_player' for i in anon):
                    return 'Анонимус'
                return p['first_name'] or p['username'] or 'Игрок'
            names = "\n".join([f"  🗡 {_start_name(p)}" for p in players_list])
            start_text = (
                f"🗡 <b>Игра #{next_num} началась!</b>\n\n"
                f"👥 Участники ({len(players_list)}):\n{names}\n\n"
                f"🗳 Голосование открыто — выбирай кого выбить!" + BOT_LINK
            )
            async with _hx2.AsyncClient() as _cl2:
                await _cl2.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={"chat_id": "@shrimpgames_chat", "text": start_text, "parse_mode": "HTML"})
        except: pass
    import asyncio as _asyncio2
    _asyncio2.create_task(_send_start_msg())

    # --- ФОНОВЫЙ АВТО-ЦИКЛ ---
    async def _run_loop():
        import httpx as _hx
        import random as _r

        async def fake_votes():
            conn2 = get_conn()
            c2 = conn2.cursor()
            c2.execute("SELECT p.user_id, u.first_name FROM players p JOIN users u ON p.user_id=u.user_id WHERE p.game_id=? AND p.is_alive=1", (game_id,))
            alive = [dict(r) for r in c2.fetchall()]
            conn2.close()
            if len(alive) < 2:
                return
            async with _hx.AsyncClient() as cl:
                for voter in alive:
                    if voter["user_id"] not in FAKE_IDS_LIST:
                        continue
                    others = [p for p in alive if p["user_id"] != voter["user_id"]]
                    if not others:
                        continue
                    target = _r.choice(others)
                    try:
                        await cl.post(f"http://localhost:8007/api/game/vote",
                            json={"voter_id": voter["user_id"], "target_id": target["user_id"]}, timeout=5)
                    except:
                        pass

        async def all_voted():
            conn3 = get_conn()
            c3 = conn3.cursor()
            c3.execute("SELECT * FROM games WHERE id=?", (game_id,))
            g = c3.fetchone()
            if not g or g["status"] != "active":
                conn3.close()
                return True
            day = g["current_day"] or 1
            c3.execute("SELECT COUNT(*) FROM players WHERE game_id=? AND is_alive=1", (game_id,))
            alive_cnt = c3.fetchone()[0]
            c3.execute("SELECT COUNT(DISTINCT voter_id) FROM votes WHERE game_id=? AND day_number=?", (game_id, day))
            voted_cnt = c3.fetchone()[0]
            conn3.close()
            return voted_cnt >= alive_cnt

        while True:
            await asyncio.sleep(5)
            conn4 = get_conn()
            c4 = conn4.cursor()
            c4.execute("SELECT * FROM games WHERE id=?", (game_id,))
            g = c4.fetchone()
            conn4.close()
            if not g or g["status"] != "active":
                break

            await fake_votes()

            # Ждём до 60 сек пока все проголосуют
            for _ in range(55):
                if await all_voted():
                    break
                await asyncio.sleep(1)

            # Resolve
            try:
                async with _hx.AsyncClient() as cl:
                    r = await cl.post(f"http://localhost:8007/api/game/resolve_votes",
                        json={"admin_key": str(ADMIN_ID)}, timeout=15)
                    data = r.json()
                    if data.get("outcome") == "game_over":
                        break
            except:
                pass

            await asyncio.sleep(3)

    # # asyncio.create_task(_run_loop())  # отключено  # отключено — test_game.py управляет сам

    return {"ok": True, "game_id": game_id, "game_number": next_num}


@app.get("/api/game/timer")
async def game_timer():
    """Получить таймер голосования из активной игры"""
    game = get_active_game()
    if game and game["status"] == "active" and game["voting_ends"]:
        import calendar
        from datetime import datetime as _dt
        try:
            # voting_ends хранится как UTC строка
            vend = _dt.strptime(game["voting_ends"], "%Y-%m-%d %H:%M:%S")
            ts_ms = int(calendar.timegm(vend.timetuple()) * 1000)
            return {"ok": True, "target": ts_ms}
        except:
            pass
    # Фоллбек — настройки
    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        c.execute("SELECT value FROM settings WHERE key='timer_target'")
        row = c.fetchone()
        conn.close()
        if row:
            return {"ok": True, "target": int(row["value"])}
    except:
        conn.close()
    return {"ok": False}

@app.post("/api/shop/use_item")
async def use_item_from_inventory(request: Request):
    """Списать предмет из инвентаря (использован бесплатно т.к. уже куплен)"""
    body = await request.json()
    user_id = body.get("user_id")
    item_type = body.get("item_type")
    if not user_id or not item_type:
        return {"ok": False, "error": "no data"}
    conn = get_conn(); c = conn.cursor()
    # Предметы которые требуют быть живым в активной игре
    REQUIRES_ALIVE = {"killer", "hacker", "spy", "black_mark", "double_vote",
                      "anon_player", "tiebreaker", "shield", "anon_msg"}
    if item_type in REQUIRES_ALIVE:
        _game = c.execute("SELECT id FROM games WHERE status='active' LIMIT 1").fetchone()
        if not _game:
            conn.close()
            return {"ok": False, "error": "Активной игры нет"}
        _player = c.execute(
            "SELECT is_alive FROM players WHERE game_id=? AND user_id=?",
            (_game["id"], user_id)
        ).fetchone()
        if not _player or not _player["is_alive"]:
            conn.close()
            return {"ok": False, "error": "Ты выбыл из игры — предмет недоступен"}
    row = c.execute(
        "SELECT id FROM items WHERE user_id=? AND item_type=? AND status='active' LIMIT 1",
        (user_id, item_type)
    ).fetchone()
    if not row:
        conn.close()
        return {"ok": False, "error": "Предмет не найден"}
    c.execute("UPDATE items SET status='used' WHERE id=?", (row["id"],))
    c.execute("UPDATE users SET items_used=items_used+1 WHERE user_id=?", (user_id,))
    conn.commit(); conn.close()
    await notify_ability_activate(item_type)
    return {"ok": True}


# ══════════════════════════════════════════
# КОЛЕСО ФОРТУНЫ
# ══════════════════════════════════════════
WHEEL_PRIZES = [
    {"type": "nothing",    "name": "Ничего",        "icon": "/static/icons/rip.png",             "weight": 35},
    {"type": "anon_msg",   "name": "Сговориться",   "icon": "/static/icons/conspire.png",        "weight": 18},
    {"type": "spy",        "name": "Стукач",        "icon": "/static/icons/rat.png",             "weight": 14},
    {"type": "black_mark", "name": "Мусорнуться",   "icon": "/static/icons/police.png",          "weight": 10},
    {"type": "anon_player","name": "Анонимус",       "icon": "/static/icons/anonymous.png",       "weight": 8},
    {"type": "double_vote","name": "Двустволка",     "icon": "/static/icons/double-barreled.png", "weight": 6},
    {"type": "tiebreaker", "name": "Решала",         "icon": "/static/icons/fixer.png",           "weight": 4},
    {"type": "shield",     "name": "Крышануться",    "icon": "/static/icons/criminal_roof.png",   "weight": 2.5},
    {"type": "hacker",     "name": "Ворюга",         "icon": "/static/icons/thief.png",           "weight": 1.5},
    {"type": "resurrect",  "name": "Постанова",      "icon": "/static/icons/fakeout.png",         "weight": 0.7},
    {"type": "killer",     "name": "Киллер",         "icon": "/static/icons/killer.png",          "weight": 0.3},
]

@app.get("/api/wheel/status")
async def wheel_status(request: Request):
    user_id = int(request.headers.get("X-User-Id", 0))
    if not user_id:
        return {"ok": False, "error": "no user"}
    conn = get_conn(); c = conn.cursor()
    try:
        c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    except: pass
    row = c.execute("SELECT value FROM settings WHERE key=?", (f"wheel_{user_id}",)).fetchone()
    conn.close()
    if not row:
        return {"ok": True, "can_spin": True, "next_spin_ms": None}
    from datetime import datetime as _dt, timedelta as _td
    last = _dt.fromisoformat(row["value"])
    next_spin = last + _td(hours=24)
    now = _dt.utcnow()
    if now >= next_spin:
        return {"ok": True, "can_spin": True, "next_spin_ms": None}
    ms = int((next_spin - now).total_seconds() * 1000)
    return {"ok": True, "can_spin": False, "next_spin_ms": ms}

@app.post("/api/wheel/spin")
async def wheel_spin(request: Request):
    user_id = int(request.headers.get("X-User-Id", 0))
    if not user_id:
        return {"ok": False, "error": "no user"}
    conn = get_conn(); c = conn.cursor()
    try:
        c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    except: pass
    row = c.execute("SELECT value FROM settings WHERE key=?", (f"wheel_{user_id}",)).fetchone()
    from datetime import datetime as _dt, timedelta as _td
    now = _dt.utcnow()
    if row and int(user_id) != int(ADMIN_ID):
        last = _dt.fromisoformat(row["value"])
        if now < last + _td(hours=24):
            ms = int((last + _td(hours=24) - now).total_seconds() * 1000)
            conn.close()
            return {"ok": False, "error": "cooldown", "next_spin_ms": ms}
    # Крутим
    import random
    total = sum(p["weight"] for p in WHEEL_PRIZES)
    r = random.uniform(0, total)
    acc = 0
    prize = WHEEL_PRIZES[0]
    for p in WHEEL_PRIZES:
        acc += p["weight"]
        if r <= acc:
            prize = p
            break
    # Сохраняем время ДО выдачи приза — защита от race condition
    if int(user_id) != int(ADMIN_ID):
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (f"wheel_{user_id}", now.isoformat()))
        conn.commit()
    # Выдаём предмет если не ничего
    game_id = None
    if prize["type"] != "nothing":
        g = c.execute("SELECT id FROM games WHERE status IN ('waiting','active') ORDER BY id DESC LIMIT 1").fetchone()
        game_id = g["id"] if g else None
        c.execute("INSERT INTO items (user_id, item_type, game_id, status) VALUES (?,?,?,'active')",
                  (user_id, prize["type"], game_id))
        # Считаем выигранные в колесе
        try:
            c.execute("UPDATE users SET items_won=items_won+1 WHERE user_id=?", (user_id,))
        except: pass
    conn.commit()
    conn.close()
    return {"ok": True, "prize": prize}


@app.post("/api/game/pre_register")
async def pre_register_next_game(request: Request):
    """Регистрация на следующую игру пока текущая идёт"""
    body = await request.json()
    user_id = body.get("user_id")
    if not user_id:
        return {"ok": False}
    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute("CREATE TABLE IF NOT EXISTS next_game_queue (user_id INTEGER PRIMARY KEY, registered_at DATETIME DEFAULT CURRENT_TIMESTAMP)")
        c.execute("INSERT OR IGNORE INTO next_game_queue (user_id) VALUES (?)", (user_id,))
        conn.commit()
        already = c.rowcount == 0
        conn.close()
        return {"ok": True, "already": already}
    except Exception as e:
        conn.close()
        return {"ok": False, "error": str(e)}


@app.get("/api/game/pre_register_status/{user_id}")
async def pre_register_status(user_id: int):
    """Проверить зарегистрирован ли юзер на следующую игру"""
    conn = get_conn(); c = conn.cursor()
    try:
        c.execute("CREATE TABLE IF NOT EXISTS next_game_queue (user_id INTEGER PRIMARY KEY, registered_at DATETIME DEFAULT CURRENT_TIMESTAMP)")
        c.execute("SELECT 1 FROM next_game_queue WHERE user_id=?", (user_id,))
        found = c.fetchone()
        conn.close()
        return {"ok": True, "registered": bool(found)}
    except:
        conn.close()
        return {"ok": True, "registered": False}


@app.get("/api/game/pre_register_count")
async def pre_register_count():
    """Сколько людей записалось на следующую игру"""
    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute("CREATE TABLE IF NOT EXISTS next_game_queue (user_id INTEGER PRIMARY KEY, registered_at DATETIME DEFAULT CURRENT_TIMESTAMP)")
        c.execute("SELECT COUNT(*) as cnt FROM next_game_queue")
        row = c.fetchone()
        conn.close()
        return {"ok": True, "count": row["cnt"] if row else 0}
    except:
        conn.close()
        return {"ok": True, "count": 0}


@app.post("/api/game/premium_tiebreak")
async def premium_tiebreak(request: Request):
    """Премиум-решала: голос премиум-игрока решает ничью (3 раза за игру)"""
    body = await request.json()
    user_id = body.get("user_id")
    target_id = body.get("target_id")
    game = get_active_game()
    if not game or not user_id or not target_id:
        return {"ok": False, "error": "Нет активной игры"}

    stats = get_user_stats(user_id)
    if not stats.get("is_premium"):
        return {"ok": False, "error": "Только для премиум-игроков"}

    conn = get_conn()
    c = conn.cursor()
    try:
        # Проверяем не использовал ли уже 3 раза в этой игре
        key = f"premium_tiebreak_{game['id']}_{user_id}"
        c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        c.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = c.fetchone()
        used_count = int(row["value"]) if row else 0
        if used_count >= 3:
            conn.close()
            return {"ok": False, "error": "Использовал все 3 раза в этой игре"}

        # Отмечаем использование
        c.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)", (key, str(used_count + 1)))

        # Проверяем что цель жива
        c.execute("SELECT is_alive FROM players WHERE game_id=? AND user_id=?", (game["id"], target_id))
        p = c.fetchone()
        if not p or not p["is_alive"]:
            conn.close()
            return {"ok": False, "error": "Цель не в игре"}

        # Добавляем решающий голос
        day = game["current_day"]
        c.execute("SELECT COUNT(*) as cnt FROM votes WHERE game_id=? AND day_number=? AND voter_id=?",
                  (game["id"], day, user_id))
        if c.fetchone()["cnt"] == 0:
            c.execute("INSERT INTO votes (game_id, day_number, voter_id, target_id) VALUES (?,?,?,?)",
                      (game["id"], day, user_id, target_id))

        # Ставим флаг что этот целевой игрок получил решающий голос
        tiebreak_key = f"tiebreak_target_{game['id']}_{day}"
        c.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)", (tiebreak_key, str(target_id)))

        conn.commit()
        conn.close()
        return {"ok": True}
    except Exception as e:
        conn.close()
        return {"ok": False, "error": str(e)}




@app.get("/api/sale/status")
async def sale_status():
    """Возвращает текущие скидки"""
    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        c.execute("SELECT value FROM settings WHERE key='sale_end'")
        row = c.fetchone()
        conn.close()
        if not row:
            return {"ok": True, "active": False}
        from datetime import datetime as _dts
        sale_end = _dts.fromisoformat(row["value"])
        now = _dts.utcnow()
        if now >= sale_end:
            return {"ok": True, "active": False}
        remaining_ms = int((sale_end - now).total_seconds() * 1000)
        return {
            "ok": True, "active": True,
            "ends_at": row["value"],
            "remaining_ms": remaining_ms,
            "items": ["killer", "resurrect", "hacker"],
            "discount": 50
        }
    except Exception as e:
        try: conn.close()
        except: pass
        return {"ok": True, "active": False}


@app.post("/api/sale/start")
async def sale_start(request: Request):
    """Запустить скидку на 24 часа (только админ)"""
    body = await request.json()
    if body.get("admin_key") != str(ADMIN_ID):
        return {"ok": False, "error": "forbidden"}
    from datetime import datetime as _dts, timedelta as _tds
    sale_end = (_dts.utcnow() + _tds(hours=24)).strftime("%Y-%m-%dT%H:%M:%S")
    conn = get_conn()
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    c.execute("INSERT OR REPLACE INTO settings (key,value) VALUES ('sale_end',?)", (sale_end,))
    conn.commit()
    conn.close()
    return {"ok": True, "sale_end": sale_end}



@app.post("/api/game/early_finish_vote")
async def early_finish_vote(request: Request):
    """Голос за досрочное завершение раунда"""
    body = await request.json()
    user_id = body.get("user_id")
    game = get_active_game()
    if not game or not user_id:
        return {"ok": False}

    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        day = game["current_day"] or 1
        key_vote = f"early_finish_{game['id']}_{day}_{user_id}"
        key_all = f"early_finish_voters_{game['id']}_{day}"

        # Уже голосовал?
        c.execute("SELECT value FROM settings WHERE key=?", (key_vote,))
        if c.fetchone():
            # Вернём текущий счёт
            c.execute("SELECT value FROM settings WHERE key=?", (key_all,))
            row = c.fetchone()
            voters = json.loads(row["value"]) if row else []
            conn.close()
            return {"ok": True, "already": True, "count": len(voters)}

        # Добавляем голос
        c.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)", (key_vote, "1"))

        # Обновляем список голосующих
        c.execute("SELECT value FROM settings WHERE key=?", (key_all,))
        row = c.fetchone()
        voters = json.loads(row["value"]) if row else []
        if user_id not in voters:
            voters.append(user_id)
        c.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)", (key_all, json.dumps(voters)))
        conn.commit()

        # Проверяем — все ли живые проголосовали за досрочку
        from database import get_game_players, get_alive_count
        alive_players = get_game_players(game["id"], alive_only=True)
        alive_ids = [p["user_id"] for p in alive_players]
        all_voted = all(uid in voters for uid in alive_ids)

        if all_voted:
            # Все согласны — запускаем resolve досрочно
            import asyncio as _asyncio
            async def _do_resolve():
                import httpx as _hx
                try:
                    async with _hx.AsyncClient(timeout=30) as _cl:
                        await _cl.post(f"http://localhost:{os.getenv('PORT','8007')}/api/game/resolve_votes",
                                       json={"game_id": game["id"], "admin_key": str(ADMIN_ID)})
                except: pass
            _asyncio.create_task(_do_resolve())

        conn.close()
        return {"ok": True, "already": False, "count": len(voters), "total": len(alive_ids), "all_voted": all_voted}
    except Exception as e:
        try: conn.close()
        except: pass
        return {"ok": False, "error": str(e)}


@app.get("/api/game/early_finish_status")
async def early_finish_status(game_id: int = None, day: int = None):
    """Сколько проголосовало за досрочку"""
    game = get_active_game()
    if not game:
        return {"ok": False, "count": 0, "total": 0}
    day = day or game["current_day"] or 1
    key_all = f"early_finish_voters_{game['id']}_{day}"
    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute("SELECT value FROM settings WHERE key=?", (key_all,))
        row = c.fetchone()
        voters = json.loads(row["value"]) if row else []
        from database import get_alive_count
        total = get_alive_count(game["id"])
        conn.close()
        return {"ok": True, "count": len(voters), "total": total}
    except:
        conn.close()
        return {"ok": True, "count": 0, "total": 0}

@app.get("/api/last_buyer")
async def get_last_buyer():
    conn = get_conn(); c = conn.cursor()
    # Исключаем админа
    ADMIN_ID = 7308147004
    r = c.execute("""SELECT p.user_id, p.item_type, p.stars, p.created_at,
        u.first_name, u.username, u.photo_url
        FROM purchases p LEFT JOIN users u ON p.user_id=u.user_id
        WHERE p.user_id != ?
        ORDER BY p.created_at DESC LIMIT 1""", (ADMIN_ID,)).fetchone()
    conn.close()
    if not r:
        return {"ok": True, "buyer": None}
    return {"ok": True, "buyer": {
        "user_id": r["user_id"],
        "first_name": r["first_name"],
        "username": r["username"],
        "photo_url": r["photo_url"],
        "item_type": r["item_type"],
        "stars": r["stars"],
    }}


@app.get("/api/events")
async def get_events():
    """Лента событий текущей игры"""
    conn = get_conn(); c = conn.cursor()
    g = c.execute("SELECT id FROM games WHERE status='active' ORDER BY id DESC LIMIT 1").fetchone()
    if not g:
        g = c.execute("SELECT id FROM games WHERE status='finished' ORDER BY id DESC LIMIT 1").fetchone()
    if not g:
        conn.close()
        return {"ok": True, "events": []}
    game_id = g["id"]
    rows = c.execute("SELECT text, icon, created_at FROM game_events WHERE game_id=? ORDER BY id DESC LIMIT 50", (game_id,)).fetchall()
    conn.close()
    return {"ok": True, "events": [{"text": r["text"], "icon": r["icon"], "time": r["created_at"]} for r in rows]}


@app.get("/api/top")
async def get_top():
    conn = get_conn(); c = conn.cursor()
    EXCL = ADMIN_ID  # исключаем админа из всех топов
    # Последние 3 игры (включая текущую активную)
    c.execute("SELECT id FROM games WHERE status IN ('finished','active') ORDER BY id DESC LIMIT 3")
    last_games = [r["id"] for r in c.fetchall()]
    if not last_games:
        conn.close()
        return {"ok": True, "games": [], "friends": [], "purchases": [], "kills": []}

    def get_icon(uid):
        try:
            r = c.execute("SELECT value FROM settings WHERE key=?", (f"premium_icon_{uid}",)).fetchone()
            return r["value"] if r else None
        except: return None

    def get_gender(uid):
        try:
            r = c.execute("SELECT value FROM settings WHERE key=?", (f"gender_{uid}",)).fetchone()
            return r["value"] if r else None
        except: return None

    ph = ','.join('?'*len(last_games))

    # Топ по играм
    c.execute(f"""SELECT u.user_id, u.first_name, u.username, COUNT(*) as val
        FROM players p JOIN users u ON p.user_id=u.user_id
        WHERE p.game_id IN ({ph}) AND u.user_id != ?
        GROUP BY p.user_id ORDER BY val DESC LIMIT 10""", last_games + [EXCL])
    games = [{"user_id":r["user_id"],"first_name":r["first_name"],"username":r["username"],
              "val":r["val"],"premium_icon":get_icon(r["user_id"]),"gender":get_gender(r["user_id"])} for r in c.fetchall()]

    # Топ по раундам — считаем уникальные раунды в которых игрок голосовал
    c.execute("""SELECT u.user_id, u.first_name, u.username,
        (SELECT COUNT(DISTINCT v.game_id || '-' || v.day_number) FROM votes v WHERE v.voter_id = u.user_id) as val
        FROM users u
        WHERE u.user_id != ?
        ORDER BY val DESC LIMIT 10""", (EXCL,))
    kills = [{"user_id":r["user_id"],"first_name":r["first_name"],"username":r["username"],
              "val":r["val"],"premium_icon":get_icon(r["user_id"]),"gender":get_gender(r["user_id"])} for r in c.fetchall()]

    # Топ по друзьям (всего рефералов)
    c.execute("""SELECT u.user_id, u.first_name, u.username, COUNT(*) as val
        FROM users ref JOIN users u ON ref.ref_by=u.user_id
        WHERE u.user_id != ?
        GROUP BY u.user_id ORDER BY val DESC LIMIT 10""", (EXCL,))
    friends = [{"user_id":r["user_id"],"first_name":r["first_name"],"username":r["username"],
                "val":r["val"],"premium_icon":get_icon(r["user_id"]),"gender":get_gender(r["user_id"])} for r in c.fetchall()]

    # Топ по покупкам
    c.execute("""SELECT u.user_id, u.first_name, u.username, COUNT(*) as val
        FROM purchases p JOIN users u ON p.user_id=u.user_id
        WHERE u.user_id != ?
        GROUP BY p.user_id ORDER BY val DESC LIMIT 10""", (EXCL,))
    purchases = [{"user_id":r["user_id"],"first_name":r["first_name"],"username":r["username"],
                  "val":r["val"],"premium_icon":get_icon(r["user_id"]),"gender":get_gender(r["user_id"])} for r in c.fetchall()]

    conn.close()
    return {"ok": True, "games": games, "friends": friends, "purchases": purchases, "rounds": kills}


@app.post("/api/user/premium_icon")
async def set_premium_icon(request: Request):
    body = await request.json()
    user_id = body.get("user_id")
    icon = body.get("icon")
    if not user_id or not icon:
        return {"ok": False, "error": "missing params"}
    # Проверяем что у юзера есть премиум
    from database import get_user_stats as _gus
    stats = _gus(user_id)
    if not stats.get("is_premium"):
        return {"ok": False, "error": "no_premium"}
    conn = get_conn(); c = conn.cursor()
    try:
        c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    except: pass
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (f"premium_icon_{user_id}", icon))
    conn.commit(); conn.close()
    return {"ok": True, "icon": icon}

@app.get("/api/user/stats/{target_id}")
async def get_player_stats(target_id: int, request: Request):
    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT user_id, username, first_name, games_played, kills, wins, items_used FROM users WHERE user_id=?", (target_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return {"ok": False, "error": "not found"}
    row = dict(row)
    # Покупки
    try:
        c.execute("SELECT COUNT(DISTINCT item_type) as cnt FROM purchases WHERE user_id=?", (target_id,))
        r = c.fetchone(); distinct_bought = r["cnt"] if r else 0
    except: distinct_bought = 0
    # Премиум иконка
    try:
        c.execute("SELECT value FROM settings WHERE key=?", (f"premium_icon_{target_id}",))
        r = c.fetchone(); premium_icon = r["value"] if r else None
    except: premium_icon = None
    # Голоса в играх
    try:
        c.execute("SELECT COUNT(*) as cnt FROM votes WHERE voter_id=?", (target_id,))
        r = c.fetchone(); votes_cast = r["cnt"] if r else 0
    except: votes_cast = 0
    conn.close()
    return {
        "ok": True,
        "user_id": row["user_id"],
        "username": row["username"],
        "first_name": row["first_name"],
        "games_played": row["games_played"] or 0,
        "wins": row["wins"] or 0,
        "kills": row["kills"] or 0,
        "items_used": row["items_used"] or 0,
        "distinct_bought": distinct_bought,
        "is_premium": get_user_stats(target_id).get("is_premium", False),
        "premium_icon": premium_icon,
        "votes_cast": votes_cast,
    }


# ══════════════════════════════════════════
# КАЗИК — игровые кредиты и слот
# ══════════════════════════════════════════

@app.post("/api/casino/topup")
async def casino_topup(request: Request):
    """Пополнить игровые кредиты за реальные Stars"""
    body = await request.json()
    user_id = body.get("user_id")
    amount = body.get("amount", 0)
    if not user_id or amount < 1:
        return {"ok": False, "error": "Неверные данные"}
    try:
        async with httpx.AsyncClient() as cl:
            r = await cl.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/createInvoiceLink",
                json={
                    "title": "Игровые кредиты — Казик",
                    "description": f"Пополнение {amount} игровых кредитов для слота",
                    "payload": f"casino:{user_id}:{amount}",
                    "currency": "XTR",
                    "prices": [{"label": "Игровые кредиты", "amount": amount}],
                }
            )
            d = r.json()
            if d.get("ok"):
                return {"ok": True, "invoice_url": d["result"]}
            return {"ok": False, "error": d.get("description", "Ошибка")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/casino/balance")
async def casino_balance(user_id: int):
    """Получить баланс игровых кредитов"""
    conn = get_conn(); c = conn.cursor()
    try:
        c.execute("CREATE TABLE IF NOT EXISTS casino_credits (user_id INTEGER PRIMARY KEY, credits INTEGER DEFAULT 0, last_free_spin TEXT)")
        conn.commit()
    except: pass
    c.execute("SELECT credits, last_free_spin FROM casino_credits WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    credits = row["credits"] if row else 0
    last_free = row["last_free_spin"] if row else None
    # Проверяем доступность бесплатного кручения
    from datetime import datetime as _dt, timezone as _tz
    free_available = True
    if last_free:
        try:
            last = _dt.fromisoformat(last_free)
            now = _dt.now(_tz.utc).replace(tzinfo=None)
            free_available = (now - last).total_seconds() >= 86400
        except: pass
    return {"ok": True, "credits": credits, "free_available": free_available}


@app.post("/api/casino/add_credits")
async def casino_add_credits(request: Request):
    """Добавить кредиты после оплаты (вызывается из payment handler)"""
    body = await request.json()
    user_id = body.get("user_id")
    amount = body.get("amount", 0)
    conn = get_conn(); c = conn.cursor()
    try:
        c.execute("CREATE TABLE IF NOT EXISTS casino_credits (user_id INTEGER PRIMARY KEY, credits INTEGER DEFAULT 0, last_free_spin TEXT)")
        c.execute("INSERT INTO casino_credits (user_id, credits) VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET credits=credits+?",
                  (user_id, amount, amount))
        conn.commit()
    except Exception as e:
        conn.close()
        return {"ok": False, "error": str(e)}
    conn.close()
    return {"ok": True}


@app.post("/api/casino/spin")
async def casino_spin(request: Request):
    """Крутануть слот"""
    import random as _rnd
    body = await request.json()
    user_id = body.get("user_id")
    is_free = body.get("free", False)
    if not user_id:
        return {"ok": False, "error": "Нет user_id"}

    conn = get_conn(); c = conn.cursor()
    try:
        c.execute("CREATE TABLE IF NOT EXISTS casino_credits (user_id INTEGER PRIMARY KEY, credits INTEGER DEFAULT 0, last_free_spin TEXT)")
        conn.commit()
    except: pass

    c.execute("SELECT credits, last_free_spin FROM casino_credits WHERE user_id=?", (user_id,))
    row = c.fetchone()
    credits = row["credits"] if row else 0
    last_free = row["last_free_spin"] if row else None

    from datetime import datetime as _dt, timezone as _tz
    now = _dt.now(_tz.utc).replace(tzinfo=None)

    if is_free:
        # Проверяем прошли ли сутки
        if last_free:
            try:
                last = _dt.fromisoformat(last_free)
                if (now - last).total_seconds() < 86400:
                    conn.close()
                    return {"ok": False, "error": "Бесплатное кручение уже использовано сегодня"}
            except: pass
        # Обновляем время последнего бесплатного
        c.execute("INSERT INTO casino_credits (user_id, credits, last_free_spin) VALUES (?,?,?) ON CONFLICT(user_id) DO UPDATE SET last_free_spin=?",
                  (user_id, 0, now.isoformat(), now.isoformat()))
    else:
        # Платное — 5 кредитов
        if credits < 5:
            conn.close()
            return {"ok": False, "error": "Недостаточно кредитов. Пополни баланс!"}
        c.execute("UPDATE casino_credits SET credits=credits-5 WHERE user_id=?", (user_id,))

    conn.commit()

    # Таблица призов с шансами
    prizes = [
        {"type": "item", "item": "anon_msg",    "label": "Сговориться",  "weight": 45},
        {"type": "item", "item": "spy",          "label": "Стукач",       "weight": 25},
        {"type": "item", "item": "black_mark",   "label": "Мусорнуться",  "weight": 14},
        {"type": "item", "item": "anon_player",  "label": "Анонимус",     "weight": 8},
        {"type": "item", "item": "double_vote",  "label": "Двустволка",   "weight": 4},
        {"type": "item", "item": "hacker",       "label": "Ворюга",       "weight": 2},
        {"type": "item", "item": "tiebreaker",   "label": "Решала",       "weight": 1},
        {"type": "credits", "amount": 100,       "label": "100 кредитов", "weight": 0.6},
        {"type": "premium",                      "label": "Премиум",      "weight": 0.4},
        {"type": "nft", "nft_id": "bull",        "label": "NFT Bull Run", "weight": 0.05},
        {"type": "nft", "nft_id": "bear",        "label": "NFT Bear Market", "weight": 0.05},
    ]
    total = sum(p["weight"] for p in prizes)
    roll = _rnd.uniform(0, total)
    cumulative = 0
    prize = prizes[-1]
    for p in prizes:
        cumulative += p["weight"]
        if roll <= cumulative:
            prize = p
            break

    # Выдаём приз
    game = get_active_game()
    game_id = game["id"] if game else None
    stats = get_user_stats(user_id)
    is_premium = stats.get("is_premium") or stats.get("premium_force")

    result_label = prize["label"]

    if prize["type"] == "item":
        add_item(user_id, prize["item"], game_id)
    elif prize["type"] == "credits":
        c.execute("UPDATE casino_credits SET credits=credits+100 WHERE user_id=?", (user_id,))
        conn.commit()
    elif prize["type"] == "premium":
        if is_premium:
            c.execute("UPDATE casino_credits SET credits=credits+100 WHERE user_id=?", (user_id,))
            conn.commit()
            result_label = "100 кредитов (уже есть Премиум)"
        else:
            c2 = conn.cursor()
            c2.execute("UPDATE users SET premium_force=1 WHERE user_id=?", (user_id,))
            conn.commit()
    elif prize["type"] == "nft":
        # NFT — уведомляем тебя в лог, юзеру сообщение
        nft_name = prize.get("label", "NFT")
        result_label = nft_name
        async with httpx.AsyncClient() as _nft_cl:
            await log_event(_nft_cl, f"🏆 <b>NFT ВЫИГРАЛИ!</b>\n👤 ID{user_id}\n🎁 {nft_name}\n\n⚠️ Нужно вручную отправить NFT игроку!")
            try:
                await _nft_cl.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={"chat_id": user_id,
                          "text": f"🏆 <b>Поздравляем! Ты выиграл {nft_name}!</b>\n\nЭто редкий NFT приз. С тобой свяжется администратор для передачи.",
                          "parse_mode": "HTML"})
            except: pass

    # Получаем новый баланс
    c.execute("SELECT credits FROM casino_credits WHERE user_id=?", (user_id,))
    row2 = c.fetchone()
    new_credits = row2["credits"] if row2 else 0
    conn.close()

    async with httpx.AsyncClient() as _cl:
        await log_event(_cl, f"🎰 <b>Казик</b>\n👤 ID{user_id}\n🎁 Выпало: {result_label}\n{'Бесплатно' if is_free else '5 кредитов'}")

    return {"ok": True, "prize": prize, "prize_label": result_label, "credits": new_credits}


@app.post("/api/casino/shop_pay")
async def casino_shop_pay(request: Request):
    """Купить связь за игровые кредиты"""
    body = await request.json()
    user_id = body.get("user_id")
    item_type = body.get("item_type")
    if item_type not in ITEMS:
        return {"ok": False, "error": "Unknown item"}
    price = ITEMS[item_type]["stars"]  # цена в кредитах = цена в звёздах
    conn = get_conn(); c = conn.cursor()
    try:
        c.execute("CREATE TABLE IF NOT EXISTS casino_credits (user_id INTEGER PRIMARY KEY, credits INTEGER DEFAULT 0, last_free_spin TEXT)")
    except: pass
    c.execute("SELECT credits FROM casino_credits WHERE user_id=?", (user_id,))
    row = c.fetchone()
    credits = row["credits"] if row else 0
    if credits < price:
        conn.close()
        return {"ok": False, "error": f"Недостаточно кредитов. Нужно {price}, у тебя {credits}"}
    c.execute("UPDATE casino_credits SET credits=credits-? WHERE user_id=?", (price, user_id))
    conn.commit()
    conn.close()
    game = get_active_game()
    game_id = game["id"] if game else None
    add_item(user_id, item_type, game_id)
    return {"ok": True, "credits_left": credits - price}


# ============ КЛАНЫ ============

@app.post("/api/game/recruit")
async def game_recruit(request: Request):
    """Завербовать пассивного игрока — купить его голос за 4 Telegram Stars"""
    body = await request.json()
    user_id = body.get("user_id")
    target_id = body.get("target_id")
    if not user_id or not target_id:
        return {"ok": False, "error": "bad request"}

    game = get_active_game()
    if not game or game["status"] != "active":
        return {"ok": False, "error": "Игра не активна"}

    # Проверяем что target пассивен (vote_missed >= 5)
    conn = get_conn(); c = conn.cursor()
    day = game["current_day"] or 1
    missed = 0
    for d in range(day, max(day - 5, 0), -1):
        c.execute("SELECT 1 FROM votes WHERE game_id=? AND day_number=? AND voter_id=?", (game["id"], d, target_id))
        if c.fetchone():
            break
        missed += 1
    conn.close()
    if missed < 5:
        return {"ok": False, "error": "Игрок ещё активен — нельзя завербовать"}

    # Проверяем что у user_id есть предмет recruit или создаём инвойс
    RECRUIT_PRICE = 4
    async with httpx.AsyncClient() as cl:
        r = await cl.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/createInvoiceLink",
            json={
                "title": "Завербовать",
                "description": "Купить голос пассивного игрока в этом раунде за 4 Telegram Stars. Итого 2 голоса.",
                "payload": f"{user_id}:recruit:{target_id}",
                "currency": "XTR",
                "prices": [{"label": "Вербовка — 4 Telegram Stars", "amount": RECRUIT_PRICE}],
            }
        )
        d = r.json()
        if d.get("ok"):
            return {"ok": True, "invoice_url": d["result"]}
        return {"ok": False, "error": d.get("description", "Ошибка")}


@app.post("/api/clan/create")
async def clan_create(request: Request):
    """Создать клан — инвойс 99 Stars"""
    body = await request.json()
    user_id = body.get("user_id")
    clan_name = (body.get("clan_name") or "Клан").strip()[:20] or "Клан"
    if not user_id:
        return {"ok": False, "error": "Нет user_id"}
    game = get_active_game()
    if not game or game["status"] not in ("active", "waiting"):
        return {"ok": False, "error": "Нет активной игры"}
    # Проверяем что уже не в клане
    conn = get_conn(); c = conn.cursor()
    if game["status"] == "active":
        _alive_row = c.execute("SELECT is_alive FROM players WHERE game_id=? AND user_id=?", (game["id"], user_id)).fetchone()
        if _alive_row and not _alive_row["is_alive"]:
            conn.close()
            return {"ok": False, "error": "Ты выбыл из игры"}
    c.execute("CREATE TABLE IF NOT EXISTS clans (id INTEGER PRIMARY KEY AUTOINCREMENT, game_id INTEGER, leader_id INTEGER, name TEXT DEFAULT 'Клан', created_at DATETIME DEFAULT CURRENT_TIMESTAMP, UNIQUE(game_id, leader_id))")
    c.execute("CREATE TABLE IF NOT EXISTS clan_members (id INTEGER PRIMARY KEY AUTOINCREMENT, clan_id INTEGER, user_id INTEGER, joined_at DATETIME DEFAULT CURRENT_TIMESTAMP, UNIQUE(clan_id, user_id))")
    c.execute("CREATE TABLE IF NOT EXISTS clan_invites (id INTEGER PRIMARY KEY AUTOINCREMENT, clan_id INTEGER, from_id INTEGER, to_id INTEGER, status TEXT DEFAULT 'pending', created_at DATETIME DEFAULT CURRENT_TIMESTAMP, UNIQUE(clan_id, to_id))")
    # Уже лидер?
    c.execute("SELECT id FROM clans WHERE game_id=? AND leader_id=?", (game["id"], user_id))
    if c.fetchone():
        conn.close()
        return {"ok": False, "error": "Ты уже создал клан"}
    # Уже в клане?
    c.execute("SELECT cm.clan_id FROM clan_members cm JOIN clans cl ON cl.id=cm.clan_id WHERE cl.game_id=? AND cm.user_id=?", (game["id"], user_id))
    if c.fetchone():
        conn.close()
        return {"ok": False, "error": "Ты уже в клане"}
    conn.close()
    # Для админа — создаём клан бесплатно
    import urllib.parse as _up
    safe_name = _up.quote(clan_name, safe='')
    if user_id == 7308147004:
        conn2 = get_conn(); c2 = conn2.cursor()
        c2.execute("INSERT OR IGNORE INTO clans (game_id, leader_id, name) VALUES (?,?,?)", (game["id"], user_id, clan_name))
        try: c2.execute("ALTER TABLE users ADD COLUMN created_clan INTEGER DEFAULT 0")
        except: pass
        c2.execute("UPDATE users SET created_clan=COALESCE(created_clan,0)+1 WHERE user_id=?", (user_id,))
        conn2.commit(); conn2.close()
        return {"ok": True, "free": True, "clan_name": clan_name}
    # Создаём инвойс — имя клана передаём в payload
    try:
        async with httpx.AsyncClient() as cl:
            r = await cl.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/createInvoiceLink",
                json={
                    "title": f"⚔️ Клан «{clan_name}» — 99 ⭐",
                    "description": f"Создай клан «{clan_name}» и зови союзников. Максимум 5 человек.",
                    "payload": f"{user_id}:clan_create:{game['id']}:{safe_name}",
                    "currency": "XTR",
                    "prices": [{"label": "Создание клана", "amount": 99}],
                }
            )
            d = r.json()
            if d.get("ok"):
                return {"ok": True, "invoice_url": d["result"]}
            return {"ok": False, "error": d.get("description", "Ошибка")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/clan/invite")
async def clan_invite(request: Request):
    """Предложить союз игроку"""
    body = await request.json()
    from_id = body.get("from_id")
    to_id = body.get("to_id")
    if not from_id or not to_id:
        return {"ok": False, "error": "Нет данных"}
    game = get_active_game()
    if not game or game["status"] not in ("active", "waiting"):
        return {"ok": False, "error": "Нет активной игры"}
    conn = get_conn(); c = conn.cursor()
    if game["status"] == "active":
        _alive_row2 = c.execute("SELECT is_alive FROM players WHERE game_id=? AND user_id=?", (game["id"], from_id)).fetchone()
        if _alive_row2 and not _alive_row2["is_alive"]:
            conn.close()
            return {"ok": False, "error": "Ты выбыл из игры"}
    # Найти клан лидера
    c.execute("SELECT id FROM clans WHERE game_id=? AND leader_id=?", (game["id"], from_id))
    clan = c.fetchone()
    if not clan:
        conn.close()
        return {"ok": False, "error": "Сначала создай клан"}
    clan_id = clan["id"]
    # Проверить размер клана
    c.execute("SELECT COUNT(*) as cnt FROM clan_members WHERE clan_id=?", (clan_id,))
    members_count = c.fetchone()["cnt"]
    if members_count >= 4:  # лидер + 4 = 5 max
        conn.close()
        return {"ok": False, "error": "В клане максимум 5 человек"}
    # Уже в клане?
    c.execute("SELECT 1 FROM clan_members cm JOIN clans cl ON cl.id=cm.clan_id WHERE cl.game_id=? AND cm.user_id=?", (game["id"], to_id))
    if c.fetchone():
        conn.close()
        return {"ok": False, "error": "Этот игрок уже в клане"}
    # Уже есть инвайт?
    c.execute("SELECT 1 FROM clan_invites WHERE clan_id=? AND to_id=? AND status='pending'", (clan_id, to_id))
    if c.fetchone():
        conn.close()
        return {"ok": False, "error": "Приглашение уже отправлено"}
    c.execute("INSERT INTO clan_invites (clan_id, from_id, to_id) VALUES (?,?,?)", (clan_id, from_id, to_id))
    conn.commit()
    # Данные отправителя
    c.execute("SELECT first_name, username, gender FROM users WHERE user_id=?", (from_id,))
    sender = c.fetchone()
    conn.close()
    sender_name = (sender["first_name"] or sender["username"] or "Игрок") if sender else "Игрок"
    # Пуш в бот
    try:
        async with httpx.AsyncClient() as cl:
            await cl.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": to_id,
                    "text": f"⚔️ <b>{sender_name}</b> предлагает тебе вступить в союз!\n\nВ клане вы не будете видеть друг друга в списке голосования. Союз действует до конца игры или пока кто-то не выйдет.",
                    "parse_mode": "HTML",
                    "reply_markup": {
                        "inline_keyboard": [[
                            {"text": "✅ Принять", "callback_data": f"clan_accept:{clan_id}:{from_id}"},
                            {"text": "❌ Отказать", "callback_data": f"clan_decline:{clan_id}:{from_id}"}
                        ]]
                    }
                }
            )
    except: pass
    return {"ok": True}


@app.get("/api/clan/info/{user_id}")
async def clan_info(user_id: int):
    """Получить информацию о клане игрока"""
    game = get_active_game()
    if not game or game["status"] not in ("active", "waiting"):
        return {"ok": True, "clan": None}
    conn = get_conn(); c = conn.cursor()
    # Проверяем таблицы
    try:
        c.execute("SELECT 1 FROM clans LIMIT 1")
    except:
        conn.close()
        return {"ok": True, "clan": None}
    # Лидер?
    c.execute("SELECT id, name FROM clans WHERE game_id=? AND leader_id=?", (game["id"], user_id))
    clan = c.fetchone()
    is_leader = True
    if not clan:
        is_leader = False
        c.execute("SELECT cl.id, cl.name, cl.leader_id FROM clan_members cm JOIN clans cl ON cl.id=cm.clan_id WHERE cl.game_id=? AND cm.user_id=?", (game["id"], user_id))
        clan = c.fetchone()
    if not clan:
        conn.close()
        return {"ok": True, "clan": None}
    clan_id = clan["id"]
    leader_id = user_id if is_leader else clan["leader_id"]
    # Получаем лидера
    c.execute("SELECT first_name, username FROM users WHERE user_id=?", (leader_id,))
    lr = c.fetchone()
    leader_name = (lr["first_name"] or lr["username"] or "Лидер") if lr else "Лидер"
    # Получаем участников
    try:
        c.execute("ALTER TABLE users ADD COLUMN premium_icon TEXT DEFAULT NULL")
        conn.commit()
    except: pass
    c.execute("SELECT cm.user_id, u.first_name, u.username, u.premium_icon FROM clan_members cm JOIN users u ON u.user_id=cm.user_id WHERE cm.clan_id=?", (clan_id,))
    members = [{"user_id": r["user_id"], "name": r["first_name"] or r["username"] or "Игрок", "premium_icon": r["premium_icon"]} for r in c.fetchall()]
    conn.close()
    return {"ok": True, "clan": {
        "id": clan_id, "name": clan["name"],
        "leader_id": leader_id, "leader_name": leader_name,
        "is_leader": is_leader, "members": members
    }}


@app.post("/api/clan/kick")
async def clan_kick(request: Request):
    """Лидер выгоняет участника из клана"""
    body = await request.json()
    leader_id = body.get("leader_id")
    target_id = body.get("target_id")
    if not leader_id or not target_id:
        return {"ok": False, "error": "bad request"}
    game = get_active_game()
    if not game:
        return {"ok": False, "error": "Нет игры"}
    conn = get_conn(); c = conn.cursor()
    # Проверяем что leader_id реально лидер
    c.execute("SELECT id FROM clans WHERE game_id=? AND leader_id=?", (game["id"], leader_id))
    clan = c.fetchone()
    if not clan:
        conn.close()
        return {"ok": False, "error": "Ты не лидер"}
    # Нельзя выгнать себя
    if int(target_id) == int(leader_id):
        conn.close()
        return {"ok": False, "error": "Нельзя выгнать себя"}
    c.execute("DELETE FROM clan_members WHERE clan_id=? AND user_id=?", (clan["id"], target_id))
    conn.commit(); conn.close()
    return {"ok": True}


@app.post("/api/clan/leave")
async def clan_leave(request: Request):
    """Покинуть клан"""
    body = await request.json()
    user_id = body.get("user_id")
    if not user_id:
        return {"ok": False, "error": "Нет user_id"}
    game = get_active_game()
    if not game:
        return {"ok": False, "error": "Нет игры"}
    conn = get_conn(); c = conn.cursor()
    # Если лидер — распускаем клан
    c.execute("SELECT id, name FROM clans WHERE game_id=? AND leader_id=?", (game["id"], user_id))
    clan = c.fetchone()
    if clan:
        clan_name = clan["name"]
        c.execute("DELETE FROM clan_members WHERE clan_id=?", (clan["id"],))
        c.execute("DELETE FROM clan_invites WHERE clan_id=?", (clan["id"],))
        c.execute("DELETE FROM clans WHERE id=?", (clan["id"],))
        conn.commit(); conn.close()
        # Сообщение в чат
        import random as _rd
        _disband_msgs = [
            f"💔 Клан «{clan_name}» распался. Союз не выдержал.",
            f"🌑 «{clan_name}» больше нет. Район снова делится на одиночек.",
            f"⚰️ Клан «{clan_name}» прекратил существование. Все сами по себе.",
            f"🔥 «{clan_name}» сгорел изнутри. Союз распался.",
            f"💀 Клан «{clan_name}» пал. Бывшие союзники снова враги.",
            f"🏚 «{clan_name}» закрыл двери навсегда. Конец союза.",
            f"⚡ Всё хорошее кончается. Клан «{clan_name}» распущен.",
            f"🗡 «{clan_name}» разошлись. Теперь каждый за себя.",
        ]
        try:
            async with httpx.AsyncClient() as _cl:
                await _cl.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={"chat_id": "@shrimpgames_chat", "text": _rd.choice(_disband_msgs), "parse_mode": "HTML"})
        except: pass
        return {"ok": True, "disbanded": True}
    # Обычный участник
    c.execute("SELECT cm.clan_id FROM clan_members cm JOIN clans cl ON cl.id=cm.clan_id WHERE cl.game_id=? AND cm.user_id=?", (game["id"], user_id))
    row = c.fetchone()
    if row:
        c.execute("DELETE FROM clan_members WHERE clan_id=? AND user_id=?", (row["clan_id"], user_id))
        conn.commit()
    conn.close()
    return {"ok": True, "disbanded": False}


@app.get("/api/clan/allies/{user_id}")
async def clan_allies(user_id: int):
    """Получить список союзников для скрытия в голосовании"""
    game = get_active_game()
    if not game or game["status"] != "active":
        return {"ok": True, "ally_ids": []}
    conn = get_conn(); c = conn.cursor()
    try:
        # Найти клан
        c.execute("SELECT id FROM clans WHERE game_id=? AND leader_id=?", (game["id"], user_id))
        clan = c.fetchone()
        if not clan:
            c.execute("SELECT cl.id FROM clan_members cm JOIN clans cl ON cl.id=cm.clan_id WHERE cl.game_id=? AND cm.user_id=?", (game["id"], user_id))
            clan = c.fetchone()
        if not clan:
            conn.close()
            return {"ok": True, "ally_ids": []}
        clan_id = clan["id"]
        # Лидер + все участники кроме самого юзера
        c.execute("SELECT leader_id FROM clans WHERE id=?", (clan_id,))
        leader_row = c.fetchone()
        c.execute("SELECT user_id FROM clan_members WHERE clan_id=?", (clan_id,))
        member_ids = [r["user_id"] for r in c.fetchall()]
        all_ids = list(set([leader_row["leader_id"]] + member_ids) - {user_id})
        conn.close()
        return {"ok": True, "ally_ids": all_ids}
    except:
        conn.close()
        return {"ok": True, "ally_ids": []}


# ===== РАЙОН: БОМЖ =====

BOMZH_DONATE_OPTIONS = {
    "coffee":     {"name": "Кофе",          "stars": 3,    "label": "Кофе Чушпану ☕️"},
    "beer":       {"name": "Пиво",           "stars": 10,   "label": "Пиво Чушпану 🍺"},
    "cigs":       {"name": "Пачка сигарет", "stars": 25,   "label": "Сигареты Чушпану 🚬"},
    "bigmac":     {"name": "Бигмак Набор",  "stars": 50,   "label": "Бигмак Набор Чушпану 🍔"},
    "clothes":    {"name": "Тёплая одежда", "stars": 300,  "label": "Тёплая одежда Чушпану 🧥"},
    "premium300": {"name": "Общак",          "stars": 1000, "label": "Общак 💎"},
}

def ensure_bomzh_tables():
    conn = get_conn(); c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS bomzh_donations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        username TEXT,
        donate_type TEXT,
        stars INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS bomzh_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        username TEXT,
        item_id TEXT,
        item_name TEXT,
        permanent INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    try: c.execute("ALTER TABLE bomzh_items ADD COLUMN permanent INTEGER DEFAULT 0")
    except: pass
    conn.commit(); conn.close()

ensure_bomzh_tables()


@app.post("/api/bomzh/donate")
async def bomzh_donate(request: Request):
    body = await request.json()
    user_id = body.get("user_id")
    donate_type = body.get("donate_type")
    if not user_id or donate_type not in BOMZH_DONATE_OPTIONS:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)

    opt = BOMZH_DONATE_OPTIONS[donate_type]

    # Для админа — бесплатный тест
    if user_id == ADMIN_ID:
        import random as _rnd
        if donate_type in ('coffee', 'beer'):
            chat_msgs = ['Спасибо, друг... ты добрый человек. Я тут всех знаю, всех вижу. Попрошу кое-кого — прикроют тебя в следующих битвах. Я обещаю. 🤝','Эх, давно никто так не угощал... Я не забуду. Есть люди которые мне должны — скажу им чтоб за тобой присматривали в игре. 🤝','Хороший ты человек, видно сразу. Попрошу своих — в следующей битве тебя не тронут. Слово Чушпана. 🤝']
        elif donate_type in ('cigs', 'bigmac'):
            chat_msgs = ['Эх братан... спасибо. Серьёзно. Знаю одного чела — он в компах шарит, мне задолжал. Попрошу его. Сломает базу данных и тебя здесь никто не найдёт. Я тебе обещаю. 💻','Ты серьёзный человек, вижу. Есть у меня знакомый — хакер, старой закалки. Попрошу его чтоб твои следы в базе подчистил. Будешь невидимкой. 💻','Знаю одного умника, он с компами на ты. Должен мне с прошлого года. Скажу ему — сделает так что тебя в базе не найдут. Договорились? 💻']
        elif donate_type == 'clothes':
            chat_msgs = ['Брат... это серьёзно. Я знаю плохих людей на районе. Они мне кое-что должны. Попрошу их — разберутся с твоими конкурентами. Молча и без лишних вопросов. 🔪']
        elif donate_type == 'premium300':
            chat_msgs = ['Слушай... такого мне ещё никто не делал. Я поговорю с главным на районе. Лично. Попрошу чтобы тебя сделали Смотрящим района. 👑']
        else:
            chat_msgs = ['Спасибо, земляк... не забуду. 🤝']
        chat_text = _rnd.choice(chat_msgs)
        conn = get_conn(); c = conn.cursor()
        c.execute("INSERT INTO bomzh_donations (user_id, username, donate_type, stars) VALUES (?,?,?,?)", (user_id, "rzabeyda", donate_type, 0))
        c.execute("CREATE TABLE IF NOT EXISTS bomzh_chat (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, text TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        c.execute("INSERT INTO bomzh_chat (user_id, text) VALUES (?,?)", (user_id, chat_text))
        conn.commit(); conn.close()
        async with httpx.AsyncClient() as cl:
            await log_event(cl, f"🏚 [ТЕСТ] Донат Чушпану\n👤 rzabeyda\n🎁 {opt['name']} (бесплатно)")
        return {"ok": True, "invoice_url": None, "test": True, "chat_text": chat_text}

    # Создаём инвойс
    async with httpx.AsyncClient() as cl:
        r = await cl.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/createInvoiceLink",
            json={
                "title": opt["label"],
                "description": "Помочь Чушпану на районе 🏚",
                "payload": f"{user_id}:bomzh_{donate_type}",
                "currency": "XTR",
                "prices": [{"label": opt["name"], "amount": opt["stars"]}],
            }
        )
        d = r.json()
        if d.get("ok"):
            return {"ok": True, "invoice_url": d["result"]}
        return {"ok": False, "error": d.get("description", "Ошибка")}


@app.post("/api/bomzh/item_found")
async def bomzh_item_found(request: Request):
    """Записать найденный предмет в базу"""
    body = await request.json()
    user_id = body.get("user_id")
    item_id = body.get("item_id")
    item_name = body.get("item_name")
    username = body.get("username", "")
    if not user_id or not item_id:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)

    conn = get_conn(); c = conn.cursor()
    c.execute("INSERT INTO bomzh_items (user_id, username, item_id, item_name) VALUES (?,?,?,?)",
              (user_id, username, item_id, item_name))
    # Смартфон — сразу выдаём бесконечную анонимку в связи
    if item_id == 'phone':
        game = get_active_game()
        game_id = game["id"] if game else None
        c.execute("SELECT id FROM items WHERE user_id=? AND item_type='anon_msg' AND status='active' LIMIT 1", (user_id,))
        if not c.fetchone():
            c.execute("INSERT INTO items (user_id, item_type, game_id, status) VALUES (?,?,?,'active')",
                      (user_id, 'anon_msg', game_id))
    conn.commit(); conn.close()

    async with httpx.AsyncClient() as cl:
        await log_event(cl, f"🏚 <b>Находка у Бича!</b>\n👤 {username} (ID:{user_id})\n📦 {item_name}")

    return {"ok": True}


@app.get("/api/bomzh/my_items")
async def bomzh_my_items(request: Request):
    uid = request.query_params.get("user_id")
    if not uid:
        return JSONResponse({"ok": False, "error": "no user_id"}, status_code=400)
    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT item_id, item_name, COALESCE(permanent,0) as permanent FROM bomzh_items WHERE user_id=? ORDER BY created_at ASC", (int(uid),))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return {"ok": True, "items": rows}


@app.post("/api/bomzh/chat_save")
async def bomzh_chat_save(request: Request):
    body = await request.json()
    user_id = body.get("user_id")
    text = body.get("text", "")
    if not user_id or not text:
        return {"ok": False}
    conn = get_conn(); c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS bomzh_chat (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, text TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("INSERT INTO bomzh_chat (user_id, text) VALUES (?,?)", (user_id, text))
    conn.commit(); conn.close()
    return {"ok": True}


@app.get("/api/bomzh/chat_load")
async def bomzh_chat_load(request: Request):
    uid = request.query_params.get("user_id")
    if not uid:
        return {"ok": False, "messages": []}
    conn = get_conn(); c = conn.cursor()
    try:
        c.execute("SELECT text, created_at FROM bomzh_chat WHERE user_id=? ORDER BY created_at ASC", (int(uid),))
        msgs = [{"text": r["text"], "time": r["created_at"][:16]} for r in c.fetchall()]
    except:
        msgs = []
    conn.close()
    return {"ok": True, "messages": msgs}



@app.post("/api/bomzh/attack_pay")
async def bomzh_attack_pay(request: Request):
    body = await request.json()
    user_id = body.get("user_id")
    if not user_id:
        return JSONResponse({"ok": False}, status_code=400)
    async with httpx.AsyncClient() as cl:
        r = await cl.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/createInvoiceLink",
            json={
                "title": "Натравить Чушпана 🔪",
                "description": "Чушпан разберётся с твоим конкурентом на районе",
                "payload": f"{user_id}:bomzh_attack",
                "currency": "XTR",
                "prices": [{"label": "Натравить Чушпана", "amount": 10}],
            }
        )
        d = r.json()
        if d.get("ok"):
            return {"ok": True, "invoice_url": d["result"]}
        return {"ok": False, "error": d.get("description", "Ошибка")}


@app.post("/api/bomzh/attack_send")
async def bomzh_attack_send(request: Request):
    body = await request.json()
    user_id = body.get("user_id")
    username = body.get("username", "Неизвестный")
    victim = body.get("victim", "")
    if not user_id or not victim:
        return JSONResponse({"ok": False}, status_code=400)

    import random as _rnd
    phrases = [
        f"🔪 {username} заказал {victim}. Чушпан нашёл его за гаражами и въебал так что у соседей в квартирах картины покосились. {victim} лежит, картины висят криво — всем неудобно.",
        f"🔪 Привет {victim} от {username}. Чушпан поймал тебя в подъезде и так отхуярил об стену что штукатурка осыпалась. Управляющая компания уже выехала — думают землетрясение.",
        f"🔪 {username} отправил Чушпана разобраться с {victim}. Чушпан разобрался. Потом собрал обратно. Потом снова разобрал — для надёжности.",
        f"🔪 {victim} ты попал блядь. {username} нанял Чушпана и тот гнался за тобой через три двора. Догнал у четвёртого. Там и объяснил всё про жизнь.",
        f"🔪 Заказ {username} выполнен. Чушпан встретил {victim} у ларька и так отметелил что продавщица даже не вышла — видала и не такое. Район суровый.",
        f"🔪 {username} попросил передать привет {victim}. Чушпан передал. Лично. Кулаком. Дважды. Третий раз {victim} уже не просил повторить — дошло.",
        f"🔪 {victim} — тебя искал Чушпан по заданию {username}. Нашёл у мусорок. Ты пришёл выбросить пакет. Выбросил пакет и гордость — Чушпан помог.",
        f"🔪 По заказу {username}. Чушпан завёл {victim} за трансформаторную будку и так навалял что будка теперь работает лучше — от вибрации контакты восстановились.",
        f"🔪 {username} нанял профессионала. Чушпан нашёл {victim} во дворе ночью и объяснял ему про уважение ногами минут двадцать. {victim} очень внимательно слушал. Лёжа.",
        f"🔪 Привет от {username}. Чушпан поймал {victim} в лифте — двери закрылись, началось. Двери открылись — {victim} остался. Следующий жилец вызвал скорую. Добрый человек.",
        f"🔪 {username} объявил охоту. Чушпан нашёл {victim} на детской площадке и так отпиздил что качели до сих пор раскачиваются. Дети довольны — аттракцион бесплатный.",
        f"🔪 Заказ {username} принят и перевыполнен. Чушпан встретил {victim} в арке и работал так вдохновенно что бабка с пятого этажа аплодировала в окно. Ценитель.",
        f"🔪 {victim} — это тебе от {username}. Чушпан поджидал у подъезда четыре часа. Терпеливый сука. Когда ты вышел — четыре часа ожидания окупились с процентами.",
        f"🔪 По просьбе {username}. Чушпан загнал {victim} в подвал и там месил пока не устал. Устал через полчаса. {victim} устал раньше — но его никто не спрашивал.",
        f"🔪 {username} заплатил — Чушпан отработал. Нашёл {victim} между гаражами и так въебал что эхо между панельками гуляло ещё минут пять после.",
        f"🔪 Специальный привет {victim} от {username}. Чушпан поймал тебя на лестнице между этажами и объяснял об перила. Долго. Доходчиво. Перила погнулись — им тоже досталось.",
        f"🔪 {username} велел разобраться по-серьёзному. Чушпан разобрался. {victim} теперь выходит из дома только в сопровождении и только до ближайшего магазина. Смелый.",
        f"🔪 По наводке {username}. Чушпан нашёл {victim} у ларька в час ночи. Продавец закрыл окошко и сделал вид что не видит — опытный, работает в этом районе давно.",
        f"🔪 {victim} получи от {username}. Чушпан ждал тебя у машины. Ты пришёл заводить — завёл только Чушпана. Тот уже был заведён. Итог предсказуем.",
        f"🔪 Заказ {username} выполнен с огоньком. Чушпан поймал {victim} во дворе и так отхуярил что консьержка записала в журнал 'шумные работы'. Официально.",
        f"🔪 {username} попросил объяснить {victim} кто тут главный. Чушпан объяснял кулаками час. {victim} оказался тугодумом — но в итоге понял. Медленно, зато надолго.",
        f"🔪 По заявке {username}. Чушпан загнал {victim} в угол и так влупил что у того из кармана выпало всё — ключи, телефон, самооценка. Самооценку Чушпан забрал себе.",
        f"🔪 {victim} — финальный привет от {username}. Чушпан нашёл тебя в подворотне и работал пока соседская собака не перестала лаять — привыкла. Постоянный клиент.",
        f"🔪 Исполнено для {username}. Чушпан поймал {victim} между машинами и так отметелил что сигналки сработали на трёх соседних тачках. Солидарность.",
        f"🔪 Финал от {username} для {victim}. Чушпан работал вдохновенно и от души. Двор был пустой — Чушпан проверил заранее. Профессионал. Художник своего дела.",
    ]

    msg = _rnd.choice(phrases)

    async with httpx.AsyncClient() as cl:
        await cl.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": "@shrimpgames_chat", "text": msg, "parse_mode": "HTML"}
        )
        await log_event(cl, f"🔪 <b>Атака Чушпана!</b>\n👤 {username} (ID:{user_id})\n🎯 Жертва: {victim}")

    try:
        conn_atk = get_conn(); c_atk = conn_atk.cursor()
        c_atk.execute("CREATE TABLE IF NOT EXISTS bomzh_attacks (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, username TEXT, victim TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        c_atk.execute("INSERT INTO bomzh_attacks (user_id, username, victim) VALUES (?,?,?)", (user_id, username, victim))
        conn_atk.commit(); conn_atk.close()
    except: pass

    return {"ok": True}

@app.post("/api/bomzh/item_buy")
async def bomzh_item_buy(request: Request):
    body = await request.json()
    user_id = body.get("user_id")
    item_id = body.get("item_id")
    if not user_id or not item_id:
        return {"ok": False}
    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT id FROM bomzh_items WHERE user_id=? AND item_id=? LIMIT 1", (user_id, item_id))
    row = c.fetchone(); conn.close()
    if not row:
        return {"ok": False, "error": "Предмет не найден"}
    if user_id == ADMIN_ID:
        conn2 = get_conn(); c2 = conn2.cursor()
        c2.execute("UPDATE bomzh_items SET permanent=1 WHERE user_id=? AND item_id=?", (user_id, item_id))
        conn2.commit(); conn2.close()
        return {"ok": True, "free": True}
    item_names = {'phone':'Смартфон','pistol':'Пистолет','car_key':'Ключи от тачки','credit_card':'Кредитка','drugs':'Таблетки'}
    name = item_names.get(item_id, 'Предмет Чушпана')
    try:
        async with httpx.AsyncClient() as cl:
            r = await cl.post(f"https://api.telegram.org/bot{BOT_TOKEN}/createInvoiceLink",
                json={"title": f"Выкупить: {name}", "description": "Предмет останется навсегда и будет работать во всех играх",
                      "payload": f"{user_id}:bomzh_keep_{item_id}", "currency": "XTR",
                      "prices": [{"label": name, "amount": 999}]})
            d = r.json()
            if d.get("ok"): return {"ok": True, "invoice_url": d["result"]}
            return {"ok": False, "error": d.get("description", "Ошибка")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/bomzh/stats")
async def bomzh_stats():
    """Админ: статистика доната и находок"""
    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT user_id, username, donate_type, stars, created_at FROM bomzh_donations ORDER BY created_at DESC LIMIT 100")
    donations = [dict(r) for r in c.fetchall()]
    c.execute("SELECT user_id, username, item_name, created_at FROM bomzh_items ORDER BY created_at DESC LIMIT 100")
    items = [dict(r) for r in c.fetchall()]
    c.execute("SELECT COALESCE(SUM(stars),0) as total FROM bomzh_donations")
    total = c.fetchone()["total"]
    conn.close()
    return {"ok": True, "total_stars": total, "donations": donations, "items": items}


@app.post("/api/claim_gems")
async def claim_gems(request: Request):
    """Одноразовое получение 25 гемов"""
    try:
        data = await request.json()
        user_id = int(data.get("user_id", 0))
        if not user_id:
            return {"ok": False, "error": "no user"}
        conn = get_conn(); c = conn.cursor()
        # Проверяем не получал ли уже
        c.execute("SELECT COALESCE(gems_claimed,0) as gems_claimed, COALESCE(gems_max_purchase,0) as gems_max_purchase FROM users WHERE user_id=?", (user_id,))
        row = c.fetchone()
        if not row:
            conn.close()
            return {"ok": False, "error": "Пользователь не найден"}
        if row["gems_claimed"]:
            conn.close()
            return {"ok": False, "error": "Уже получено"}
        if row["gems_max_purchase"] < 100:
            conn.close()
            return {"ok": False, "error": "Нужно купить минимум 100 💎 за один раз"}
        # Начисляем 25 гемов
        c.execute("UPDATE users SET gems=COALESCE(gems,0)+25, gems_claimed=1 WHERE user_id=?", (user_id,))
        conn.commit(); conn.close()
        return {"ok": True, "gems": 25}
    except Exception as e:
        return {"ok": False, "error": str(e)}
