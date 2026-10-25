"""Мурчат: Telegram-бот (aiogram 3). Запуск: BOT_TOKEN=... python bot.py"""
import asyncio, hashlib, json, logging, os, time
from collections import defaultdict, deque

from aiogram import Bot, Dispatcher, F
from aiogram.types import BufferedInputFile, Message

import game

TOKEN = os.environ["BOT_TOKEN"]
UNKNOWN = "Напиши «погладить» 🐾"
PET_WORDS = {"погладить", "погладь"}
ALIASES = {"pet": "погладить", "shop": "магазин", "buy": "купить", "cases": "кейсы", "open": "открыть",
           "cats": "котики", "pick": "выбрать", "collect": "собрать", "top": "топ", "help": "помощь",
           "start": "помощь", "profile": "профиль"}
ADMIN_CMDS = {"admin", "give", "take", "setenergy", "givecat", "givecase", "ban", "unban",
              "stats", "user", "broadcast", "boost", "reset", "addadmin", "removeadmin", "admins", "backup"}
ID_CMDS = {"give", "take", "setenergy", "givecat", "givecase", "ban", "unban", "user", "reset",
           "addadmin", "removeadmin"}  # можно вызвать ответом на сообщение игрока
OWNER_ID = int(os.getenv("OWNER_ID") or os.getenv("ADMIN_ID", "7733792615"))  # сюда летят бэкапы
BACKUP_MINUTES = int(os.getenv("BACKUP_MINUTES", "10"))
BACKUP_NAME = "murchat_backup.json"
CONFIRM = {"reset", "ban", "broadcast"}

logging.basicConfig(level=logging.INFO)
db = game.DB(os.getenv("DB_PATH", "murchat.db"))
bot = Bot(TOKEN)
dp = Dispatcher()
backup_state = {"hash": None}
pending = {}  # admin_id -> (cmd, args, raw)
recent = defaultdict(lambda: deque(maxlen=3))  # антиспам: не более 3 поглаживаний в секунду


def parse(text):
    if not text.startswith("/"):
        word = text.lower().strip(" !.?,…")
        return ("погладить" if word in PET_WORDS else None), [], text
    head, _, rest = text.partition(" ")
    cmd = head[1:].split("@")[0].lower()
    return ALIASES.get(cmd, cmd), rest.split(), rest.strip()


# ---------- Бэкап в ЛС владельца (закреплённый JSON) ----------
def is_backup_msg(msg):
    return bool(msg and msg.document and msg.document.file_name == BACKUP_NAME)


def snapshot(data=None):
    data = data or db.export_json()
    body = {k: v for k, v in data.items() if k != "ts"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


async def do_backup(force=False):
    """Шлёт JSON владельцу в ЛС и закрепляет. Старый закреп снимается."""
    try:
        data = db.export_json()
        h = snapshot(data)
        if not force and h == backup_state["hash"]:
            return "ℹ️ Изменений нет"
        prev = (await bot.get_chat(OWNER_ID)).pinned_message
        raw = json.dumps(data, ensure_ascii=False).encode()
        msg = await bot.send_document(
            OWNER_ID, BufferedInputFile(raw, BACKUP_NAME),
            caption=f"💾 Бэкап Мурчата · игроков: {len(data['players'])} · {time.strftime('%d.%m %H:%M')}")
        await bot.pin_chat_message(OWNER_ID, msg.message_id, disable_notification=True)
        if is_backup_msg(prev):
            try:
                await bot.unpin_chat_message(OWNER_ID, prev.message_id)
            except Exception:
                pass
        db.kv_set("backup_ts", data["ts"])
        backup_state["hash"] = h
        return f"✅ Бэкап отправлен владельцу 💾 ({len(raw) // 1024} КБ)"
    except Exception as e:
        logging.exception("backup failed")
        return f"❌ Бэкап не удался: {e} (владелец должен написать боту /start)"


async def restore_from_pin():
    """Возвращает True, если в ЛС владельца есть закреплённый бэкап."""
    try:
        pm = (await bot.get_chat(OWNER_ID)).pinned_message
        if not is_backup_msg(pm):
            logging.info("закреплённого бэкапа нет")
            return False
        data = json.load(await bot.download(pm.document))
        local_ts = float(db.kv_get("backup_ts"))
        if not db.players() or data.get("ts", 0) > local_ts:
            db.import_json(data)
            logging.info("восстановлено из бэкапа: игроков %d", len(data["players"]))
        return True
    except Exception:
        logging.exception("восстановление не удалось")
        return False


async def backup_loop():
    while True:
        await asyncio.sleep(BACKUP_MINUTES * 60)
        await do_backup()


async def run_admin(uid, cmd, args, raw):
    if cmd == "backup":
        return await do_backup(force=True) if game.is_creator(uid) else "❌ Только Создатель"
    if cmd != "broadcast":
        return game.admin_exec(db, uid, cmd, args, raw)
    if not raw:
        return "❌ Формат: /broadcast <текст>"
    ids, ok = db.chat_ids(), 0
    for cid in ids:
        try:
            await bot.send_message(cid, raw)
            ok += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    db.log(uid, f"broadcast {ok}/{len(ids)}: {raw[:100]}")
    return f"✅ Рассылка: доставлено {ok}/{len(ids)}"


@dp.message(F.text)
async def on_text(m: Message):
    u = m.from_user
    if u is None or u.is_bot:
        return
    uid, text = u.id, m.text.strip()
    name = f"@{u.username}" if u.username else u.full_name
    db.ensure(uid, name)
    db.touch_chat(m.chat.id, uid)
    is_admin = game.can_admin(db, uid)  # права только по user_id (Создатель или выданный админ)
    if db.banned(uid):
        return
    if is_admin and uid in pending:
        act = pending.pop(uid)
        return await m.answer(await run_admin(uid, *act) if text.lower().lstrip("/") == "да" else "Отменено")

    cmd, args, raw = parse(text)
    if cmd in ADMIN_CMDS:
        if not is_admin:
            return await m.answer(UNKNOWN)
        ru = m.reply_to_message.from_user if m.reply_to_message else None
        if cmd in ID_CMDS and ru and not ru.is_bot and not (args and args[0].lstrip("-").isdigit()):
            db.ensure(ru.id, f"@{ru.username}" if ru.username else ru.full_name)
            args.insert(0, str(ru.id))  # цель = автор сообщения, на которое ответили
        if cmd in CONFIRM:
            pending[uid] = (cmd, args, raw)
            return await m.answer(f"⚠️ Выполнить /{cmd} {raw if cmd == 'broadcast' else ' '.join(args)}? Напиши «да»")
        return await m.answer(await run_admin(uid, cmd, args, raw))

    num = game._int(args[0]) if args else None
    if cmd == "погладить":
        dq, now = recent[uid], time.monotonic()
        if not game.is_creator(uid) and len(dq) == 3 and now - dq[0] < 1.0:
            return  # лишнее игнорируем
        dq.append(now)
        reply = game.pet(db, uid)
    elif cmd == "профиль":
        reply = game.profile(db, uid, name)
    elif cmd == "магазин":
        reply = game.shop(db, uid)
    elif cmd == "купить":
        reply = game.buy(db, uid, num) if num else "Купить: /купить 1"
    elif cmd == "кейсы":
        reply = game.cases_menu(db, uid)
    elif cmd == "открыть":
        reply = game.open_case(db, uid, num) if num else "Открыть: /открыть 1"
    elif cmd == "котики":
        reply = game.cats_menu(db, uid)
    elif cmd == "выбрать":
        reply = game.pick_cat(db, uid, raw)
    elif cmd == "собрать":
        reply = game.collect(db, uid)
    elif cmd == "топ":
        reply = game.top(db, m.chat.id, m.chat.type == "private")
    elif cmd == "помощь":
        reply = game.HELP
    elif cmd is not None or m.chat.type == "private":
        reply = UNKNOWN
    else:
        return  # в группах не отвечаем на обычную болтовню
    await m.answer(reply)  # одно действие = одно новое сообщение


async def main():
    if await restore_from_pin():
        backup_state["hash"] = snapshot()
    else:
        logging.info(await do_backup(force=True))  # первый запуск: создаём первый бэкап
    task = asyncio.create_task(backup_loop())
    try:
        await dp.start_polling(bot, close_bot_session=False)
    finally:
        task.cancel()
        await do_backup()  # последний бэкап при остановке
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
