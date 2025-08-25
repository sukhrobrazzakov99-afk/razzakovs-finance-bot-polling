import os, re, sqlite3, time, logging
from datetime import datetime
from typing import Optional, Tuple, List, Dict, Any
from zoneinfo import ZoneInfo
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, CallbackQueryHandler, filters

# ---------------- Config ----------------
PORT = int(os.environ.get("PORT", "8080"))
DB_PATH = os.environ.get("DB_PATH", "finance.db")
TIMEZONE = ZoneInfo(os.environ.get("TZ", "Asia/Tashkent"))
ALLOWED_USER_IDS = {int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").replace(";", ",").split(",") if x.strip().isdigit()}

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s | %(message)s", level=logging.INFO)
log = logging.getLogger("bot")

# ---------------- Keyboards ----------------
BACK_BTN = "◀️ Назад"
INCOME_BTN = "➕ Доход"
EXPENSE_BTN = "➖ Расход"
BALANCE_BTN = "💰 Баланс"
HISTORY_BTN = "📜 История"
REPORT_BTN = "📊 Отчёт (месяц)"
DEBTS_BTN = "💼 Долги"
SETTINGS_CMD = "/settings"

MAIN_KB = ReplyKeyboardMarkup(
    [
        [KeyboardButton(INCOME_BTN), KeyboardButton(EXPENSE_BTN)],
        [KeyboardButton(BALANCE_BTN), KeyboardButton(HISTORY_BTN)],
        [KeyboardButton(REPORT_BTN), KeyboardButton(DEBTS_BTN)],
    ],
    resize_keyboard=True
)

EXPENSE_CATS = ["Еда", "Транспорт", "Дом", "Детское", "Здоровье", "Развлечения", "Спорт", "Прочее"]
INCOME_CATS = ["Зарплата", "Подработка", "Подарок", "Прочее"]

def build_categories_kb(items: List[str]) -> ReplyKeyboardMarkup:
    rows, row = [], []
    for i, it in enumerate(items, 1):
        row.append(KeyboardButton(it))
        if i % 3 == 0:
            rows.append(row); row = []
    if row: rows.append(row)
    rows.append([KeyboardButton(BACK_BTN)])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def debts_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("➕ Я должен"), KeyboardButton("➕ Мне должны")],
            [KeyboardButton("📜 Я должен"), KeyboardButton("📜 Мне должны")],
            [KeyboardButton("✖️ Закрыть долг"), KeyboardButton("➖ Уменьшить долг")],
            [KeyboardButton(BACK_BTN)]
        ],
        resize_keyboard=True
    )

# ---------------- DB ----------------
def init_db():
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS tx(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        ttype TEXT NOT NULL CHECK(ttype IN('income','expense')),
        amount REAL NOT NULL,
        currency TEXT NOT NULL,
        category TEXT NOT NULL,
        note TEXT,
        ts INTEGER NOT NULL
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_tx_user_ts ON tx(user_id, ts)")
    c.execute("""CREATE TABLE IF NOT EXISTS debts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        direction TEXT NOT NULL CHECK(direction IN('owes','owed')),
        amount REAL NOT NULL,
        currency TEXT NOT NULL,
        counterparty TEXT NOT NULL,
        note TEXT,
        status TEXT NOT NULL CHECK(status IN('open','closed')) DEFAULT 'open',
        created_ts INTEGER NOT NULL,
        updated_ts INTEGER NOT NULL
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_debts_user ON debts(user_id, status, direction)")
    # settings per chat
    c.execute("""CREATE TABLE IF NOT EXISTS settings(
        chat_id INTEGER PRIMARY KEY,
        autopin INTEGER NOT NULL DEFAULT 1,
        autoclean INTEGER NOT NULL DEFAULT 1,
        group_silent INTEGER NOT NULL DEFAULT 1,
        lang TEXT NOT NULL DEFAULT 'ru',
        updated_ts INTEGER NOT NULL
    )""")
    # last pinned summary per chat
    c.execute("""CREATE TABLE IF NOT EXISTS pins(
        chat_id INTEGER PRIMARY KEY,
        message_id INTEGER NOT NULL
    )""")
    con.commit(); con.close()
init_db()

# ---------------- Utils ----------------
CURRENCY_SIGNS = {
    "usd": ["$", "usd", "дол", "долл", "доллар", "доллары", "долларов", "бакс", "баксы", "bak", "dollar"],
    "uzs": ["сум", "сумы", "сумов", "sum", "uzs"]
}
CURRENCY_WORDS = {"usd","uzs","sum","сум","сумы","сумов","дол","долл","доллар","доллары","долларов","бакс","баксы","dollar","$"}

def detect_currency(t: str) -> str:
    t = t.lower()
    for cur, words in CURRENCY_SIGNS.items():
        if any(w in t for w in words):
            return cur
    return "uzs"

def parse_amount(t: str) -> Optional[float]:
    m = re.findall(r"(?:(?<=\s)|^)(\d{1,3}(?:[ \u00A0,\.]\d{3})+|\d+)(?:[.,](\d{1,2}))?", t)
    if not m: return None
    raw, frac = m[-1]
    num = re.sub(r"[ \u00A0,\.]", "", raw)
    return float(f"{num}.{frac}") if frac else float(num)

def parse_debt_input(t: str) -> Tuple[Optional[float], Optional[str], str]:
    t = t.strip()
    m = re.match(r"^\s*(\d{1,3}(?:[ \u00A0,\.]\d{3})+|\d+)(?:[.,](\d{1,2}))?\s*([A-Za-zА-Яа-яЁё$]+)?\s*(.*)$", t)
    if not m:
        return None, None, ""
    raw, frac, cur_raw, rest = m.groups()
    num = re.sub(r"[ \u00A0,\.]", "", raw)
    amount = float(f"{num}.{frac}") if frac else float(num)
    currency = None
    if cur_raw:
        cur_low = cur_raw.lower()
        if cur_low in CURRENCY_WORDS or cur_low == "$":
            currency = "usd" if (cur_low in {"usd","$","дол","долл","доллар","доллары","долларов","бакс","баксы","dollar"}) else "uzs"
    if not currency:
        currency = detect_currency(t)
    name = (rest or "").strip()
    if name:
        toks = [w for w in re.findall(r"[@A-Za-zА-Яа-яЁё0-9\-_.]+", name)]
        toks = [w for w in toks if w.lower() not in CURRENCY_WORDS]
        name = " ".join(toks).strip()
    return amount, currency, name

def fmt_amount(amount: float, currency: str) -> str:
    if currency == "usd":
        s = f"{amount:,.2f}".replace(",", " ").replace(".00", "")
        return f"{s} USD"
    else:
        s = f"{int(round(amount)):,}".replace(",", " ")
        return f"{s} UZS"

def ts_now() -> int:
    return int(time.time())

def dt_fmt(ts: int) -> str:
    dt = datetime.fromtimestamp(ts, tz=TIMEZONE)
    return dt.strftime("%d.%m.%Y %H:%M")

# ---------------- Settings & Pins ----------------
def get_chat_settings(chat_id: int) -> Dict[str, Any]:
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("SELECT autopin, autoclean, group_silent, lang FROM settings WHERE chat_id=?", (chat_id,))
    row = c.fetchone(); con.close()
    if not row:
        return {"autopin": 1, "autoclean": 1, "group_silent": 1, "lang": "ru"}
    return {"autopin": int(row[0]), "autoclean": int(row[1]), "group_silent": int(row[2]), "lang": row[3]}

def set_chat_setting(chat_id: int, key: str, value: Any):
    now = ts_now()
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("""INSERT INTO settings(chat_id, autopin, autoclean, group_silent, lang, updated_ts)
                 VALUES(?,1,1,1,'ru',?) ON CONFLICT(chat_id) DO NOTHING""", (chat_id, now))
    c.execute(f"UPDATE settings SET {key}=?, updated_ts=? WHERE chat_id=?", (value, now, chat_id))
    con.commit(); con.close()

def get_pinned_msg_id(chat_id: int) -> Optional[int]:
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("SELECT message_id FROM pins WHERE chat_id=?", (chat_id,))
    row = c.fetchone(); con.close()
    return int(row[0]) if row else None

def set_pinned_msg_id(chat_id: int, message_id: int):
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("""INSERT INTO pins(chat_id, message_id) VALUES(?,?)
                 ON CONFLICT(chat_id) DO UPDATE SET message_id=excluded.message_id""", (chat_id, message_id))
    con.commit(); con.close()

# ---------------- DB Ops ----------------
def add_tx(uid: int, ttype: str, amount: float, currency: str, category: str, note: str = "") -> int:
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("INSERT INTO tx(user_id, ttype, amount, currency, category, note, ts) VALUES(?,?,?,?,?,?,?)",
              (uid, ttype, amount, currency, category, note, ts_now()))
    con.commit(); rowid = c.lastrowid; con.close()
    return rowid

def last_txs(uid: int, limit: int = 10) -> List[tuple]:
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("SELECT id, ttype, amount, currency, category, note, ts FROM tx WHERE user_id=? ORDER BY ts DESC LIMIT ?", (uid, limit))
    rows = c.fetchall(); con.close()
    return rows

def net_by_currency(uid: int) -> dict:
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("""SELECT currency, SUM(CASE WHEN ttype='income' THEN amount ELSE -amount END) as net
                 FROM tx WHERE user_id=? GROUP BY currency""", (uid,))
    res = {row[0]: row[1] or 0.0 for row in c.fetchall()}
    con.close()
    return res

def debt_add(uid: int, direction: str, amount: float, currency: str, counterparty: str, note: str = "") -> int:
    now = ts_now()
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("""INSERT INTO debts(user_id, direction, amount, currency, counterparty, note, status, created_ts, updated_ts)
                 VALUES(?,?,?,?,?,?, 'open', ?, ?)""",
              (uid, direction, amount, currency, counterparty or "", note, now, now))
    con.commit(); rowid = c.lastrowid; con.close()
    return rowid

def debts_open(uid: int, direction: str) -> List[tuple]:
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("""SELECT id, amount, currency, counterparty, created_ts
                 FROM debts WHERE user_id=? AND direction=? AND status='open'
                 ORDER BY created_ts DESC""", (uid, direction))
    rows = c.fetchall(); con.close()
    return rows

def debt_totals_by_currency(uid: int) -> dict:
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("""SELECT currency, direction, SUM(amount) FROM debts
                 WHERE user_id=? AND status='open'
                 GROUP BY currency, direction""", (uid,))
    res = {}
    for currency, direction, s in c.fetchall():
        if currency not in res: res[currency] = {"owes": 0.0, "owed": 0.0}
        res[currency][direction] = s or 0.0
    con.close()
    return res

def debt_reduce_or_close(uid: int, debt_id: int, reduce_amount: Optional[float] = None):
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("SELECT amount, currency FROM debts WHERE id=? AND user_id=? AND status='open'", (debt_id, uid))
    row = c.fetchone()
    if not row:
        con.close(); return False, "Долг не найден или уже закрыт."
    amount, currency = float(row[0]), row[1]
    if reduce_amount is None or reduce_amount >= amount:
        c.execute("UPDATE debts SET status='closed', amount=0, updated_ts=? WHERE id=?", (ts_now(), debt_id))
        con.commit(); con.close()
        return True, f"✅ Долг #{debt_id} закрыт."
    else:
        new_amount = amount - reduce_amount
        c.execute("UPDATE debts SET amount=?, updated_ts=? WHERE id=?", (new_amount, ts_now(), debt_id))
        con.commit(); con.close()
        return True, f"➖ Сумма долга #{debt_id} уменьшена: {fmt_amount(new_amount, currency)}"

# ---------------- Balance summary + pin ----------------
def build_balance_summary(uid: int) -> str:
    now = datetime.now(TIMEZONE)
    head = f"📌 Итог на {now.strftime('%d.%m')}, {now.strftime('%H:%M')}"
    net = net_by_currency(uid)
    debts = debt_totals_by_currency(uid)

    def fmt_multi(label: str, dd: dict, sign: int = +1) -> str:
        parts = []
        for cur in sorted(set(list(net.keys()) + list(dd.keys()))):
            owes = debts.get(cur, {}).get("owes", 0.0)
            owed = debts.get(cur, {}).get("owed", 0.0)
            if label == "Баланс":
                val = net.get(cur, 0.0)
            elif label == "Я должен":
                val = owes
            elif label == "Мне должны":
                val = owed
            else:
                val = net.get(cur, 0.0) - owes + owed
            if abs(val) > 0.0001:
                parts.append(fmt_amount(val, cur))
        if not parts:
            parts = [fmt_amount(0, "uzs")]
        return f"{label}: " + " | ".join(parts)

    lines = [
        head,
        "",
        fmt_multi("Баланс", net),
        fmt_multi("Я должен", debts),
        fmt_multi("Мне должны", debts),
        fmt_multi("Чистый баланс", debts),
    ]
    return "\n".join(lines)

async def send_and_pin_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    uid = update.effective_user.id
    text = build_balance_summary(uid)
    msg = await context.bot.send_message(chat_id=chat_id, text=text)
    st = get_chat_settings(chat_id)
    if st.get("autopin", 1):
        old = get_pinned_msg_id(chat_id)
        if old:
            try:
                await context.bot.unpin_chat_message(chat_id=chat_id, message_id=old)
            except Exception as e:
                log.debug(f"unpin old failed: {e}")
        try:
            await context.bot.pin_chat_message(chat_id=chat_id, message_id=msg.message_id, disable_notification=True)
            set_pinned_msg_id(chat_id, msg.message_id)
        except Exception as e:
            log.debug(f"pin failed: {e}")

# ---------------- Healthcheck HTTP (for Railway Web) ----------------
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type","text/plain")
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        return

def run_health_server():
    try:
        srv = HTTPServer(("0.0.0.0", PORT), HealthHandler)
        srv.serve_forever()
    except Exception as e:
        log.warning(f"Health server stopped: {e}")

# ---------------- Cleanup helpers ----------------
def should_autoclean(chat_id: int) -> bool:
    st = get_chat_settings(chat_id)
    return bool(st.get("autoclean", 1))

async def cleanup_prev_msgs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not should_autoclean(chat_id):
        return
    chat_data = context.chat_data
    last_user_id = chat_data.get("last_user_msg_id")
    last_bot_id = chat_data.get("last_bot_msg_id")
    # delete previous user msg
    if last_user_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=last_user_id)
        except Exception:
            pass
    # delete previous bot reply (never touch pinned summary)
    if last_bot_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=last_bot_id)
        except Exception:
            pass
    chat_data.pop("last_user_msg_id", None)
    chat_data.pop("last_bot_msg_id", None)

def remember_user_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.chat_data["last_user_msg_id"] = update.message.message_id

def remember_bot_msg(context: ContextTypes.DEFAULT_TYPE, message_id: int):
    context.chat_data["last_bot_msg_id"] = message_id

# ---------------- Handlers ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USER_IDS and update.effective_user.id not in ALLOWED_USER_IDS:
        await update.message.reply_text("Доступ запрещён.")
        return
    await update.message.reply_text("Главное меню:", reply_markup=MAIN_KB)

async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    await cleanup_prev_msgs(update, context)
    remember_user_msg(update, context)
    msg = await update.message.reply_text(build_balance_summary(uid))
    remember_bot_msg(context, msg.message_id)
    # Пин обновляем только при изменении данных (не здесь)

async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await cleanup_prev_msgs(update, context)
    remember_user_msg(update, context)
    rows = last_txs(uid, 10)
    if not rows:
        msg = await update.message.reply_text("История пуста.")
        remember_bot_msg(context, msg.message_id)
        return
    lines = ["Последние операции:"]
    for rid, ttype, amount, currency, category, note, ts in rows:
        when = dt_fmt(ts)
        lines.append(f"#{rid} {when} — {'+' if ttype=='income' else '-'} {fmt_amount(amount, currency)} [{category}]")
    msg = await update.message.reply_text("\n".join(lines))
    remember_bot_msg(context, msg.message_id)

# /settings
def settings_kb(chat_id: int) -> InlineKeyboardMarkup:
    st = get_chat_settings(chat_id)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Автопин: {'Вкл' if st['autopin'] else 'Выкл'}", callback_data="settings:toggle:autopin")],
        [InlineKeyboardButton(f"Автоочистка: {'Вкл' if st['autoclean'] else 'Выкл'}", callback_data="settings:toggle:autoclean")],
        [InlineKeyboardButton(f"Тихий режим (группы): {'Вкл' if st['group_silent'] else 'Выкл'}", callback_data="settings:toggle:group_silent")],
        [InlineKeyboardButton("Язык: RU", callback_data="settings:setlang:ru"),
         InlineKeyboardButton("UZ", callback_data="settings:setlang:uz")]
    ])

async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text("Настройки:", reply_markup=settings_kb(chat_id))

async def on_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    chat_id = update.effective_chat.id
    if data.startswith("settings:toggle:"):
        key = data.split(":")[2]
        st = get_chat_settings(chat_id)
        new_val = 0 if st.get(key, 1) else 1
        set_chat_setting(chat_id, key, new_val)
    elif data.startswith("settings:setlang:"):
        lang = data.split(":")[2]
        set_chat_setting(chat_id, "lang", lang)
    await q.edit_message_text("Настройки:", reply_markup=settings_kb(chat_id))

# Flow state helpers (existing)
def set_flow(context: ContextTypes.DEFAULT_TYPE, flow: dict):
    context.user_data["flow"] = flow

def get_flow(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.user_data.get("flow", {})

def clear_flow(context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("flow", None)

def set_debts_state(context: ContextTypes.DEFAULT_TYPE, state: dict):
    context.user_data["debts"] = state

def get_debts_state(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.user_data.get("debts", {})

def clear_debts_state(context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("debts", None)

async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Group silent mode gate
    chat = update.effective_chat
    is_group = chat.type in {"group", "supergroup"}
    if is_group:
        st = get_chat_settings(chat.id)
        if st.get("group_silent", 1):
            txt = (update.message.text or "").strip()
            low = txt.lower()
            is_command = txt.startswith("/")
            bot_username = (context.bot.username or "").lower()
            mentioned = bool(bot_username and f"@{bot_username}" in low)
            is_reply_to_bot = bool(update.message.reply_to_message and update.message.reply_to_message.from_user and update.message.reply_to_message.from_user.is_bot)
            if not (is_command or mentioned or is_reply_to_bot):
                return

    if ALLOWED_USER_IDS and update.effective_user.id not in ALLOWED_USER_IDS:
        await update.message.reply_text("Доступ запрещён.")
        return

    uid = update.effective_user.id
    txt = (update.message.text or "").strip()
    low = txt.lower()

    # ---------- Debts FSM first (don't lose stage) ----------
    debts = get_debts_state(context)
    stage = debts.get("stage")

    # Back in debts flow
    if txt == BACK_BTN and debts:
        clear_debts_state(context)
        await update.message.reply_text("Главное меню.", reply_markup=MAIN_KB)
        return

    # Awaiting amount+name for debt
    if stage == "await_amount":
        amount, currency, name = parse_debt_input(txt)
        if not amount:
            await update.message.reply_text("Введите сумму, например: 5000 usd Ahmed")
            return
        direction = debts.get("direction")
        if not name:
            set_debts_state(context, {"stage":"await_counterparty", "direction":direction, "amount":amount, "currency":currency})
            await update.message.reply_text("Кто контрагент? (Имя/комментарий)")
            return
        debt_add(uid, direction, amount, currency, name)
        when = dt_fmt(ts_now())
        party_line = f"• Должник: {name}" if direction == "owed" else f"• Кому: {name}"
        await update.message.reply_text(
            "✅ Долг добавлен:\n"
            f"• Сумма: {fmt_amount(amount, currency)}\n"
            f"{party_line}\n"
            f"• Дата: {when}"
        )
        clear_debts_state(context)
        await show_debts_list(update, context, direction)
        await send_and_pin_summary(update, context)
        return

    # Awaiting counterparty name after amount parsed
    if stage == "await_counterparty":
        direction = debts.get("direction")
        amount = debts.get("amount")
        currency = debts.get("currency")
        name = txt.strip()
        if not name:
            await update.message.reply_text("Введите имя/комментарий.")
            return
        debt_add(uid, direction, amount, currency, name)
        when = dt_fmt(ts_now())
        party_line = f"• Должник: {name}" if direction == "owed" else f"• Кому: {name}"
        await update.message.reply_text(
            "✅ Долг добавлен:\n"
            f"• Сумма: {fmt_amount(amount, currency)}\n"
            f"{party_line}\n"
            f"• Дата: {when}"
        )
        clear_debts_state(context)
        await show_debts_list(update, context, direction)
        await send_and_pin_summary(update, context)
        return

    # Reduce/close flows
    if stage == "reduce_ask_id":
        if not txt.lstrip("#").isdigit():
            await update.message.reply_text("Введите ID долга, например: 3")
            return
        set_debts_state(context, {"stage":"reduce_ask_amount", "debt_id": int(txt.lstrip('#'))})
        await update.message.reply_text("На сколько уменьшить? (например: 1000 или 1000 usd). Для полного закрытия введите 0.")
        return
    if stage == "reduce_ask_amount":
        if txt.strip() in {"0","0 uzs","0 usd","закрыть","close"}:
            ok, msg = debt_reduce_or_close(uid, get_debts_state(context)["debt_id"], None)
            await update.message.reply_text(msg)
            clear_debts_state(context)
            await update.message.reply_text("Выберите действие:", reply_markup=debts_menu_kb())
            await send_and_pin_summary(update, context)
            return
        amt = parse_amount(txt)
        if not amt:
            await update.message.reply_text("Введите число, например: 1500")
            return
        ok, msg = debt_reduce_or_close(uid, get_debts_state(context)["debt_id"], amt)
        await update.message.reply_text(msg)
        clear_debts_state(context)
        await update.message.reply_text("Выберите действие:", reply_markup=debts_menu_kb())
        await send_and_pin_summary(update, context)
        return

    # Debts menu actions
    if txt in {DEBTS_BTN, "Долги"} or ("долг" in low and not debts):
        await cleanup_prev_msgs(update, context)
        remember_user_msg(update, context)
        set_debts_state(context, {"stage":"menu"})
        msg = await update.message.reply_text("Раздел «Долги». Выберите действие:", reply_markup=debts_menu_kb())
        remember_bot_msg(context, msg.message_id)
        return

    if debts.get("stage") == "menu":
        if low.replace("+", "➕") in {"➕ я должен", "➕ мне должны"} or txt in {"➕ Я должен", "➕ Мне должны"}:
            direction = "owed" if "мне должны" in low else "owes"
            set_debts_state(context, {"stage":"await_amount", "direction":direction})
            await update.message.reply_text("Введите сумму и имя, например: 5000 usd Ahmed" if direction=="owed" else "Введите сумму и кому должны, например: 300 usd Rent")
            return
        if txt in {"📜 Мне должны", "📜 Я должен"}:
            direction = "owed" if "Мне" in txt else "owes"
            await show_debts_list(update, context, direction)
            return
        if txt == "✖️ Закрыть долг":
            set_debts_state(context, {"stage":"reduce_ask_id"})
            await update.message.reply_text("Введите ID долга для закрытия (например: 3). Введите 0 на следующем шаге для полного закрытия.")
            return
        if txt == "➖ Уменьшить долг":
            set_debts_state(context, {"stage":"reduce_ask_id"})
            await update.message.reply_text("Введите ID долга (например: 3)")
            return
        if txt == BACK_BTN:
            clear_debts_state(context)
            await update.message.reply_text("Главное меню.", reply_markup=MAIN_KB)
            return
        await update.message.reply_text("Выберите действие:", reply_markup=debts_menu_kb())
        return

    # ---------- Transaction flow (buttons) ----------
    flow = get_flow(context)
    if txt == BACK_BTN and flow:
        clear_flow(context)
        await update.message.reply_text("Главное меню.", reply_markup=MAIN_KB)
        return

    if txt == EXPENSE_BTN:
        set_flow(context, {"stage":"choose_expense"})
        await update.message.reply_text("Выберите категорию расхода:", reply_markup=build_categories_kb(EXPENSE_CATS))
        return

    if txt == INCOME_BTN:
        set_flow(context, {"stage":"choose_income"})
        await update.message.reply_text("Выберите категорию дохода:", reply_markup=build_categories_kb(INCOME_CATS))
        return

    if flow.get("stage") == "choose_expense" and txt in EXPENSE_CATS:
        set_flow(context, {"stage":"await_amount", "ttype":"expense", "category":txt})
        await update.message.reply_text(f"Введите сумму для «{txt}» (например: 25000 или 20 usd).", reply_markup=ReplyKeyboardMarkup([[KeyboardButton(BACK_BTN)]], resize_keyboard=True))
        return

    if flow.get("stage") == "choose_income" and txt in INCOME_CATS:
        set_flow(context, {"stage":"await_amount", "ttype":"income", "category":txt})
        await update.message.reply_text(f"Введите сумму для «{txt}» (например: 25000 или 20 usd).", reply_markup=ReplyKeyboardMarkup([[KeyboardButton(BACK_BTN)]], resize_keyboard=True))
        return

    if flow.get("stage") == "await_amount":
        amount = parse_amount(txt)
        if not amount:
            await update.message.reply_text("Введите корректную сумму, например: 25000 или 20 usd.")
            return
        currency = detect_currency(txt)
        ttype = flow.get("ttype")
        category = flow.get("category")
        add_tx(uid, ttype, amount, currency, category, "")
        await update.message.reply_text(f"✅ Сохранено: {('+' if ttype=='income' else '-')}{fmt_amount(amount, currency)} [{category}]")
        clear_flow(context)
        await send_and_pin_summary(update, context)
        await update.message.reply_text("Главное меню.", reply_markup=MAIN_KB)
        return

    # ---------- Simple buttons ----------
    if txt == BALANCE_BTN:
        await balance_cmd(update, context)
        return
    if txt == HISTORY_BTN:
        await history_cmd(update, context)
        return
    if txt == REPORT_BTN:
        await cleanup_prev_msgs(update, context)
        remember_user_msg(update, context)
        msg = await update.message.reply_text("Отчёт пока недоступен.")
        remember_bot_msg(context, msg.message_id)
        return
    if txt == SETTINGS_CMD:
        await settings_cmd(update, context)
        return

    # ---------- Free-form fallback ----------
    amount = parse_amount(txt)
    if amount:
        currency = detect_currency(txt)
        ttype = "expense"
        category = "Прочее"
        add_tx(uid, ttype, amount, currency, category, txt)
        await update.message.reply_text(f"✅ Сохранено: -{fmt_amount(amount, currency)} [{category}]")
        await send_and_pin_summary(update, context)
        return

    await update.message.reply_text("Не понял. Выберите действие.", reply_markup=MAIN_KB)

async def show_debts_list(update: Update, context: ContextTypes.DEFAULT_TYPE, direction: str):
    uid = update.effective_user.id
    await cleanup_prev_msgs(update, context)
    remember_user_msg(update, context)
    rows = debts_open(uid, direction)
    if not rows:
        msg = await update.message.reply_text("Список пуст.", reply_markup=debts_menu_kb())
        remember_bot_msg(context, msg.message_id)
        return
    title = "Список должников:" if direction == "owed" else "Список моих долгов:"
    lines = [title]
    for did, amount, currency, name, created_ts in rows:
        lines.append(f"#{did} {name or '-'} — {fmt_amount(amount, currency)} ({datetime.fromtimestamp(created_ts, tz=TIMEZONE).strftime('%d.%m.%Y')})")
    msg = await update.message.reply_text("\n".join(lines), reply_markup=debts_menu_kb())
    remember_bot_msg(context, msg.message_id)

# ---------------- Main ----------------
def build_app(token: str) -> Application:
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CallbackQueryHandler(on_settings_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    return app

def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN is not set in environment variables")
    Thread(target=run_health_server, daemon=True).start()
    app = build_app(token)
    log.info("Starting polling")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

if __name__ == "__main__":
    main()
