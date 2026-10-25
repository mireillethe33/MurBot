"""Мурчат: игровая логика и админка (без привязки к Telegram).
Все числа считает код. Бот только отправляет готовый текст."""
import json, math, os, random, sqlite3, time

C, R, E, L = "C", "R", "E", "L"
RAR_ICON = {C: "⚪", R: "🔵", E: "🟣", L: "🟡"}
RAR_NAME = {C: "Common", R: "Rare", E: "Epic", L: "Legendary"}

# ---------- Данные (меняй здесь баланс) ----------
BASE_ENERGY, BASE_REGEN, AUTO_PER_LVL, AUTO_CAP_H = 50, 30, 5, 8
EVENT_CHANCE = 0.05
LINES = ["мрр...", "мур", "фыр-фыр", "ещё!", "левее", "мур-мур"]

CATS = {
    "Мурзик": dict(r=C, base=True, desc="+0%"),
    "Рыжик": dict(r=C, mult=.02, desc="+2% к мурам"),
    "Пуша": dict(r=R, mult=.05, desc="+5% к мурам"),
    "Снежок": dict(r=R, mult=.07, desc="+7% к мурам"),
    "Багира": dict(r=E, mult=.10, crit=.03, desc="+10% к мурам и +3% крит"),
    "Компот": dict(r=E, auto=.15, desc="+15% к автогладилке"),
    "Король Тостов": dict(r=L, mult=.30, auto=.30, desc="+30% к мурам и автогладилке"),
}
ACCS = {"Бантик": (C, .02), "Очки": (R, .04), "Корона": (E, .08), "Золотая корона": (L, .15)}
DUP = {C: 50, R: 150, E: 500, L: 2000}  # дубликат -> мурры
MURR_DROP = {C: .4, R: .8, E: 1.5, L: 4.0}  # доля цены кейса
DROP_TYPES = [("cat", 35), ("acc", 25), ("boost", 20), ("murrs", 20)]
CASES = {
    1: ("📦", "Обычный", 200, [(C, 70), (R, 25), (E, 5)]),
    2: ("🎁", "Редкий", 1000, [(C, 40), (R, 40), (E, 17), (L, 3)]),
    3: ("👑", "Легендарный", 5000, [(R, 40), (E, 40), (L, 20)]),
}
UPG = {
    1: dict(k="hand", icon="🖐", name="Сила руки", max=20, base=50, g=1.5, start=1),
    2: dict(k="crit", icon="✨", name="Золотая лапка", max=15, base=100, g=1.6, start=0),
    3: dict(k="cap", icon="🔋", name="Запас сил", max=10, base=80, g=1.5, start=0),
    4: dict(k="regen", icon="⏱", name="Быстрое восстановление", max=10, base=120, g=1.6, start=0),
    5: dict(k="auto", icon="🤖", name="Авто-гладилка", max=20, base=200, g=1.5, start=0),
    6: dict(k="mult", icon="💎", name="Множитель", max=10, base=1000, g=2.0, start=0),
}
# Ранг «Создатель»: админ-права, неуязвимость и никаких ограничений.
# ID через запятую в переменной CREATORS (или ADMIN_ID).
CREATORS = {int(x) for x in os.getenv("CREATORS", os.getenv("ADMIN_ID", "7733792615")).split(",") if x.strip()}
def is_creator(uid): return uid in CREATORS
def can_admin(db, uid): return is_creator(uid) or db.is_admin(uid)
IMMUNE = "🛡 Создатель неуязвим"
LIMITS = dict(give=1_000_000, boost_mult=5, boost_min=120)


def n(x):
    return f"{int(x):,}".replace(",", " ")


def new_state():
    now = time.time()
    return {"murrs": 0, "pets": 0, "energy": 50.0, "energy_ts": now,
            "lv": {"hand": 1, "crit": 0, "cap": 0, "regen": 0, "auto": 0, "mult": 0},
            "cats": ["Мурзик"], "active": "Мурзик", "acc": [],
            "cases": {"1": 0, "2": 0, "3": 0}, "x2_until": 0, "auto_ts": now, "last_line": -1}


class DB:
    def __init__(self, path="murchat.db"):
        self.c = sqlite3.connect(path)
        self.c.executescript("""
        PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;
        CREATE TABLE IF NOT EXISTS players(user_id INTEGER PRIMARY KEY, name TEXT,
            banned INTEGER DEFAULT 0, state TEXT);
        CREATE TABLE IF NOT EXISTS chat_members(chat_id INTEGER, user_id INTEGER,
            PRIMARY KEY(chat_id, user_id));
        CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS admins(user_id INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS admin_log(ts REAL, admin_id INTEGER, action TEXT);
        """)

    def ensure(self, uid, name):
        if self.c.execute("SELECT 1 FROM players WHERE user_id=?", (uid,)).fetchone():
            self.c.execute("UPDATE players SET name=? WHERE user_id=?", (name, uid))
        else:
            self.c.execute("INSERT INTO players(user_id,name,state) VALUES(?,?,?)",
                           (uid, name, json.dumps(new_state())))
        self.c.commit()

    def state(self, uid):
        r = self.c.execute("SELECT state FROM players WHERE user_id=?", (uid,)).fetchone()
        return json.loads(r[0]) if r else None

    def save(self, uid, s):
        self.c.execute("UPDATE players SET state=? WHERE user_id=?", (json.dumps(s), uid))
        self.c.commit()

    def banned(self, uid):
        if uid in CREATORS:
            return False
        r = self.c.execute("SELECT banned FROM players WHERE user_id=?", (uid,)).fetchone()
        return bool(r and r[0])

    def set_ban(self, uid, v):
        self.c.execute("UPDATE players SET banned=? WHERE user_id=?", (int(v), uid))
        self.c.commit()

    def players(self, chat_id=None):
        q = "SELECT p.user_id,p.name,p.banned,p.state FROM players p"
        a = ()
        if chat_id is not None:
            q += " JOIN chat_members m ON m.user_id=p.user_id WHERE m.chat_id=?"
            a = (chat_id,)
        return [(u, nm, b, json.loads(st)) for u, nm, b, st in self.c.execute(q, a)]

    def touch_chat(self, chat_id, uid):
        self.c.execute("INSERT OR IGNORE INTO chat_members VALUES(?,?)", (chat_id, uid))
        self.c.commit()

    def chat_ids(self):
        return [r[0] for r in self.c.execute("SELECT DISTINCT chat_id FROM chat_members")]

    def kv_get(self, k, d="0"):
        r = self.c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return r[0] if r else d

    def kv_set(self, k, v):
        self.c.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (k, str(v)))
        self.c.commit()

    # --- админы, выданные в чате ---
    def is_admin(self, uid):
        return self.c.execute("SELECT 1 FROM admins WHERE user_id=?", (uid,)).fetchone() is not None

    def add_admin(self, uid):
        self.c.execute("INSERT OR IGNORE INTO admins VALUES(?)", (uid,))
        self.c.commit()

    def remove_admin(self, uid):
        self.c.execute("DELETE FROM admins WHERE user_id=?", (uid,))
        self.c.commit()

    def admins(self):
        return [r[0] for r in self.c.execute("SELECT user_id FROM admins ORDER BY user_id")]

    # --- бэкап: весь прогресс одним JSON ---
    def export_json(self):
        return {"version": 1, "ts": time.time(),
                "players": [{"user_id": u, "name": nm, "banned": bool(b), "state": st}
                            for u, nm, b, st in self.players()],
                "chat_members": [list(r) for r in self.c.execute("SELECT chat_id,user_id FROM chat_members")],
                "kv": {k: v for k, v in self.c.execute("SELECT k,v FROM kv") if k != "backup_ts"},
                "admins": self.admins()}

    def import_json(self, d):
        if d.get("version") != 1 or not isinstance(d.get("players"), list):
            raise ValueError("не похоже на бэкап Мурчата")
        with self.c:
            for t in ("players", "chat_members", "kv", "admins"):
                self.c.execute(f"DELETE FROM {t}")
            self.c.executemany("INSERT INTO players VALUES(?,?,?,?)",
                               [(p["user_id"], p["name"], int(p["banned"]), json.dumps(p["state"])) for p in d["players"]])
            self.c.executemany("INSERT OR IGNORE INTO chat_members VALUES(?,?)", d.get("chat_members", []))
            self.c.executemany("INSERT OR REPLACE INTO kv VALUES(?,?)", [(k, str(v)) for k, v in d.get("kv", {}).items()])
            self.c.executemany("INSERT OR IGNORE INTO admins VALUES(?)", [(a,) for a in d.get("admins", [])])
            self.c.execute("INSERT OR REPLACE INTO kv VALUES('backup_ts',?)", (str(d.get("ts", 0)),))

    def log(self, admin_id, action):
        self.c.execute("INSERT INTO admin_log VALUES(?,?,?)", (time.time(), admin_id, action))
        self.c.commit()


# ---------- Расчёты ----------
def max_energy(s): return BASE_ENERGY + 10 * s["lv"]["cap"]
def regen_sec(s): return BASE_REGEN - 2 * s["lv"]["regen"]
def cat(s): return CATS[s["active"]]
def acc_bonus(s): return sum(ACCS[a][1] for a in s["acc"])
def crit_chance(s): return .02 * s["lv"]["crit"] + cat(s).get("crit", 0)
def auto_rate(s): return AUTO_PER_LVL * s["lv"]["auto"] * (1 + cat(s).get("auto", 0))


def refill(s, now):
    s["energy"] = min(max_energy(s), s["energy"] + (now - s["energy_ts"]) / regen_sec(s))
    s["energy_ts"] = now


def global_boost(db, now):
    if float(db.kv_get("boost_until")) > now:
        return float(db.kv_get("boost_mult", "1"))
    return 1.0


def multiplier(s, now, gboost):
    m = 1.1 ** s["lv"]["mult"] * (1 + cat(s).get("mult", 0) + acc_bonus(s)) * gboost
    return m * 2 if s["x2_until"] > now else m


def wchoice(table):
    return random.choices([t[0] for t in table], [t[1] for t in table])[0]


# ---------- Игровые действия ----------
def pet(db, uid, now=None):
    now = now or time.time()
    s = db.state(uid)
    refill(s, now)
    if is_creator(uid):
        s["energy"] = max_energy(s) + 1  # бесконечная энергия
    if s["energy"] < 1:
        wait = math.ceil((1 - s["energy"]) * regen_sec(s))
        db.save(uid, s)
        return f"Котик устал, подожди {wait} сек 😴"
    s["energy"] -= 1
    s["pets"] += 1
    crit = random.random() < crit_chance(s)
    ev = random.choice(["gift", "snort", "loud"]) if random.random() < EVENT_CHANCE else None
    gain = s["lv"]["hand"] * multiplier(s, now, global_boost(db, now)) * (2 if crit else 1)
    if ev == "loud":
        gain *= 3
    gain = 0 if ev == "snort" else max(1, round(gain))
    if ev == "gift":
        gain += 50
    s["murrs"] += gain
    i = random.choice([k for k in range(len(LINES)) if k != s["last_line"]])
    s["last_line"] = i
    head = f"🐱 {s['active']}: *{LINES[i]}* +{n(gain)} 🐾" + (" ✨ КРИТ ×2!" if crit else "")
    extra = {"gift": "🎁 Кот принёс подарок! +50 🐾", "snort": "😾 Котик фыркнул...",
             "loud": "🔊 Громкое мурчание! ×3"}.get(ev)
    out = [head] + ([extra] if extra else []) + [f"⚡ {int(s['energy'])}/{max_energy(s)} · 🐾 {n(s['murrs'])}"]
    db.save(uid, s)
    return "\n".join(out)


def upg_cost(u, lvl): return int(u["base"] * u["g"] ** (lvl - u["start"]))


def eff(k, lv):
    return {"hand": f"+{lv} за поглаживание", "crit": f"крит {2 * lv}%",
            "cap": f"макс. ⚡ {BASE_ENERGY + 10 * lv}", "regen": f"1 ⚡ за {BASE_REGEN - 2 * lv} сек",
            "auto": f"+{AUTO_PER_LVL * lv} 🐾/час", "mult": f"×{1.1 ** lv:.2f}"}[k]


def shop(db, uid):
    s = db.state(uid)
    rows = ["🛒 Магазин", f"🐾 {n(s['murrs'])}"]
    for i, u in UPG.items():
        lv = s["lv"][u["k"]]
        price = "МАКС 🏆" if lv >= u["max"] else f"{n(upg_cost(u, lv))} 🐾"
        rows.append(f"{i}. {u['icon']} {u['name']} · ур. {lv}/{u['max']} ({eff(u['k'], lv)}) · {price}")
    rows.append("Купить: /купить 1")
    return "\n".join(rows)


def buy(db, uid, num):
    u = UPG.get(num)
    if not u:
        return "Нет такого улучшения. Смотри /магазин 🛒"
    s = db.state(uid)
    lvl = s["lv"][u["k"]]
    if lvl >= u["max"]:
        return f"{u['icon']} {u['name']}: максимум 🏆"
    cost = 0 if is_creator(uid) else upg_cost(u, lvl)
    if s["murrs"] < cost:
        return f"Не хватает {n(cost - s['murrs'])} 🐾"
    now = time.time()
    refill(s, now)
    s["murrs"] -= cost
    s["lv"][u["k"]] = lvl + 1
    if u["k"] == "auto" and lvl == 0:
        s["auto_ts"] = now
    db.save(uid, s)
    return f"✅ {u['icon']} {u['name']} → ур. {lvl + 1} (−{n(cost)} 🐾)\n🐾 {n(s['murrs'])}"


def cases_menu(db, uid):
    s = db.state(uid)
    rows = ["🎰 Кейсы"]
    for i, (ic, nm, price, _) in CASES.items():
        rows.append(f"{i}. {ic} {nm} — {n(price)} 🐾 (в запасе: {s['cases'][str(i)]})")
    rows.append("Открыть: /открыть 1")
    return "\n".join(rows)


def give_cat(s, name):
    if name in s["cats"]:
        return False
    s["cats"].append(name)
    return True


def open_case(db, uid, num):
    if num not in CASES:
        return "Нет такого кейса. Смотри /кейсы 📦"
    ic, nm, price, table = CASES[num]
    s = db.state(uid)
    if s["cases"][str(num)] > 0:
        s["cases"][str(num)] -= 1
        paid = "из запасов 🎟"
    elif is_creator(uid):
        paid = "бесплатно 👑"
    elif s["murrs"] >= price:
        s["murrs"] -= price
        paid = f"−{n(price)} 🐾"
    else:
        return f"Не хватает {n(price - s['murrs'])} 🐾"
    rar, typ, now = wchoice(table), wchoice(DROP_TYPES), time.time()
    if typ == "cat":
        name = random.choice([k for k, v in CATS.items() if v["r"] == rar and not v.get("base")])
        if give_cat(s, name):
            txt = f"🐱 Новый котик: {name} ({CATS[name]['desc']})"
        else:
            s["murrs"] += DUP[rar]
            txt = f"🐱 {name} — дубликат → +{n(DUP[rar])} 🐾"
    elif typ == "acc":
        name = random.choice([k for k, v in ACCS.items() if v[0] == rar])
        if name not in s["acc"]:
            s["acc"].append(name)
            txt = f"🎀 Аксессуар: {name} (+{round(ACCS[name][1] * 100)}% к мурам)"
        else:
            s["murrs"] += DUP[rar]
            txt = f"🎀 {name} — дубликат → +{n(DUP[rar])} 🐾"
    elif typ == "boost":
        if random.random() < .5:
            refill(s, now)
            s["energy"] = max_energy(s)
            txt = "🧪 Бустер: полная энергия ⚡"
        else:
            s["x2_until"] = max(now, s["x2_until"]) + 600
            txt = "🧪 Бустер: ×2 мурра на 10 мин"
    else:
        amt = int(price * MURR_DROP[rar])
        s["murrs"] += amt
        txt = f"🐾 Мурры: +{n(amt)}"
    db.save(uid, s)
    return f"{ic} {nm} кейс ({paid})\n{RAR_ICON[rar]} {RAR_NAME[rar]}: {txt}\n🐾 {n(s['murrs'])}"


def cats_menu(db, uid):
    s = db.state(uid)
    rows = ["🐱 Твои котики"]
    for c in s["cats"]:
        rows.append(f"{'👉' if c == s['active'] else '  '} {RAR_ICON[CATS[c]['r']]} {c} · {CATS[c]['desc']}")
    rows.append("Выбрать: /выбрать <имя>")
    return "\n".join(rows)


def pick_cat(db, uid, name):
    s = db.state(uid)
    for c in s["cats"]:
        if c.lower() == name.strip().lower() and name.strip():
            s["active"] = c
            db.save(uid, s)
            return f"✅ Теперь с тобой {RAR_ICON[CATS[c]['r']]} {c} ({CATS[c]['desc']})"
    return "Такого котика нет. Смотри /котики 🐱"


def collect(db, uid):
    s = db.state(uid)
    if s["lv"]["auto"] == 0:
        return "Авто-гладилка не куплена: /магазин 🤖"
    now = time.time()
    hours = min(now - s["auto_ts"], AUTO_CAP_H * 3600) / 3600
    gain = int(auto_rate(s) * hours)
    if gain < 1:
        return f"Пока нечего собирать 🤖 (+{n(auto_rate(s))}/ч)"
    s["murrs"] += gain
    s["auto_ts"] = now
    db.save(uid, s)
    return f"🤖 Авто-гладилка принесла +{n(gain)} 🐾\n🐾 {n(s['murrs'])}"


def profile(db, uid, name):
    s = db.state(uid)
    refill(s, time.time())
    db.save(uid, s)
    lv = s["lv"]
    rows = [f"👤 {name}" + (" 👑 Создатель" if is_creator(uid) else ""), f"🐾 {n(s['murrs'])} · погладил: {n(s['pets'])}",
            f"⚡ {int(s['energy'])}/{max_energy(s)} (1 за {regen_sec(s)} сек)",
            f"🐱 {s['active']} ({cat(s)['desc']})",
            f"🖐 {lv['hand']} · ✨ {round(crit_chance(s) * 100)}% · 🔋 {lv['cap']} · ⏱ {lv['regen']} · 🤖 {lv['auto']} · 💎 {lv['mult']}"]
    if s["acc"]:
        rows.append("🎀 " + ", ".join(s["acc"]))
    c = s["cases"]
    rows.append(f"🎟 📦{c['1']} 🎁{c['2']} 👑{c['3']}")
    if s["x2_until"] > time.time():
        rows.append(f"🧪 ×2 ещё {int(s['x2_until'] - time.time())} сек")
    return "\n".join(rows)


def top(db, chat_id, private):
    rows = sorted(db.players(None if private else chat_id), key=lambda r: -r[3]["murrs"])[:10]
    if not rows:
        return "Пока никого нет 🐾"
    medals = ["🥇", "🥈", "🥉"] + ["▫️"] * 7
    return "🏆 Топ по мурам\n" + "\n".join(
        f"{medals[i]} {nm}{' 👑' if is_creator(u) else ''} — {n(st['murrs'])} 🐾" for i, (u, nm, _, st) in enumerate(rows))


HELP = ("🐾 Мурчат: гладь котиков!\n"
        "Пиши «погладить» · /профиль · /магазин · /купить N\n"
        "/кейсы · /открыть N · /котики · /выбрать имя\n"
        "/собрать · /топ · /помощь")

# ---------- Админка ----------
USAGE = {"give": "/give <id> <N>", "take": "/take <id> <N>", "setenergy": "/setenergy <id> <N>",
         "givecat": "/givecat <id> <имя>", "givecase": "/givecase <id> <1-3> <N>", "ban": "/ban <id>",
         "unban": "/unban <id>", "addadmin": "/addadmin <id> (или ответом на сообщение)",
         "removeadmin": "/removeadmin <id> (или ответом)", "user": "/user <id>", "boost": "/boost <×N> <минут>", "reset": "/reset <id>"}
ADMIN_MENU = ("🛠 Админка\n/give /take /setenergy /givecat /givecase\n/ban /unban /stats /user\n"
              "/broadcast /boost /reset\n👑 Только Создатель: /addadmin /removeadmin /backup\n/admins — список админов\n"
              "Айди можно не писать: ответь командой на сообщение игрока.\n"
              "Подтверждение «да»: /reset, /ban, /broadcast")


def _int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def admin_exec(db, admin_id, cmd, args, raw=""):
    bad = f"❌ Формат: {USAGE.get(cmd, '/' + cmd)}"
    if cmd == "admin":
        return ADMIN_MENU
    if cmd == "stats":
        ps = db.players()
        return (f"📊 Игроков: {len(ps)} · поглаживаний: {n(sum(p[3]['pets'] for p in ps))}"
                f" · мурров в обороте: {n(sum(p[3]['murrs'] for p in ps))}")
    if cmd == "boost":
        m = _int((args[0] if args else "").lstrip("×xXх")) if len(args) == 2 else None
        mins = _int(args[1]) if len(args) == 2 else None
        if m is None or mins is None:
            return bad
        if m < 1 or mins < 1 or (not is_creator(admin_id) and (m > LIMITS["boost_mult"] or mins > LIMITS["boost_min"])):
            return f"❌ Лимит: ×1–{LIMITS['boost_mult']} и 1–{LIMITS['boost_min']} мин"
        db.kv_set("boost_mult", m)
        db.kv_set("boost_until", time.time() + mins * 60)
        db.log(admin_id, f"boost x{m} {mins}m")
        return f"✅ Глобальный ×{m} на {mins} мин"
    if cmd == "admins":
        names = {u: nm for u, nm, _, _ in db.players()}
        who = lambda u: f"{names.get(u, '?')} ({u})"
        return ("👑 Создатели: " + ", ".join(who(u) for u in sorted(CREATORS)) + "\n🛠 Админы: "
                + (", ".join(who(u) for u in db.admins()) or "нет"))
    if cmd not in USAGE or not args:
        return bad
    tid = _int(args[0])
    s = db.state(tid) if tid is not None else None
    if s is None:
        return "❌ Игрок не найден" if tid is not None else bad
    now = time.time()
    free = is_creator(admin_id)  # у Создателя нет лимитов
    if cmd in ("take", "setenergy", "ban", "reset"):
        if tid in CREATORS:
            return IMMUNE
        if db.is_admin(tid) and not free:
            return "🛡 Цель — админ, нужен Создатель"
    if cmd in ("addadmin", "removeadmin"):
        if not free:
            return "❌ Только Создатель"
        if cmd == "addadmin":
            if tid in CREATORS:
                return "ℹ️ Он и так Создатель 👑"
            db.add_admin(tid)
            res = f"✅ Игрок {tid} теперь админ 🛠"
        else:
            db.remove_admin(tid)
            res = f"✅ Игрок {tid} больше не админ"
        db.log(admin_id, res)
        return res
    if cmd in ("give", "take"):
        v = _int(args[1]) if len(args) == 2 else None
        if v is None:
            return bad
        if v < 1 or (not free and v > LIMITS["give"]):
            return f"❌ N от 1 до {n(LIMITS['give'])}"
        if cmd == "give":
            s["murrs"] += v
            res = f"✅ Выдано {n(v)} 🐾 игроку {tid}"
        else:
            v = min(v, s["murrs"])
            s["murrs"] -= v
            res = f"✅ Забрано {n(v)} 🐾 у игрока {tid}"
    elif cmd == "setenergy":
        v = _int(args[1]) if len(args) == 2 else None
        if v is None:
            return bad
        s["energy"], s["energy_ts"] = max(0, min(v, max_energy(s))), now
        res = f"✅ Энергия игрока {tid}: {int(s['energy'])}/{max_energy(s)}"
    elif cmd == "givecat":
        name = next((c for c in CATS if c.lower() == " ".join(args[1:]).strip().lower()), None)
        if not name:
            return "❌ Нет такого котика: " + ", ".join(CATS)
        res = f"✅ Котик {name} выдан игроку {tid}" if give_cat(s, name) else f"❌ У игрока {tid} уже есть {name}"
    elif cmd == "givecase":
        k, v = (_int(args[1]), _int(args[2])) if len(args) == 3 else (None, None)
        if k is None or v is None:
            return bad
        if k not in CASES or v < 1 or (not free and v > 1000):
            return "❌ Кейс 1–3, количество 1–1000"
        s["cases"][str(k)] += v
        res = f"✅ Выдано кейсов {CASES[k][0]} ×{v} игроку {tid}"
    elif cmd == "ban":
        db.set_ban(tid, True)
        res = f"✅ Игрок {tid} забанен"
    elif cmd == "unban":
        db.set_ban(tid, False)
        res = f"✅ Игрок {tid} разбанен"
    elif cmd == "reset":
        s = new_state()
        res = f"✅ Прогресс игрока {tid} обнулён"
    elif cmd == "user":
        row = next(p for p in db.players() if p[0] == tid)
        return json.dumps({"user_id": tid, "name": row[1], "banned": bool(row[2]), "state": s},
                          ensure_ascii=False, indent=1)
    db.save(tid, s)
    db.log(admin_id, res)
    return res
