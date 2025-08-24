import os, re, sqlite3, time, logging, csv, io, math
from datetime import datetime, timedelta
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
REPORT_BTN = "📊 Отчёт (период)"
DEBTS_BTN = "💼 Долги"
EXPORT_BTN = "Экспорт 📂"
BUDGET_BTN = "Бюджет 💡"
SETTINGS_BTN = "⚙️ Настройки"
CANCEL_BTN = "↩️ Отменить"

MAIN_KB = ReplyKeyboardMarkup(
    [
        [KeyboardButton(INCOME_BTN), KeyboardButton(EXPENSE_BTN)],
        [KeyboardButton(BALANCE_BTN), KeyboardButton(HISTORY_BTN)],
        [KeyboardButton(REPORT_BTN), KeyboardButton(DEBTS_BTN)],
        [KeyboardButton(BUDGET_BTN), KeyboardButton(EXPORT_BTN)],
        [KeyboardButton(SETTINGS_BTN), KeyboardButton(CANCEL_BTN)],
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
            [KeyboardButton("Экспорт долгов 📂")],
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
    c.execute("""CREATE TABLE IF NOT EXISTS budgets(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        category TEXT NOT NULL,
        currency TEXT NOT NULL,
        limit_amount REAL NOT NULL,
        period TEXT NOT NULL DEFAULT 'month',
        active INTEGER NOT NULL DEFAULT 1,
        created_ts INTEGER NOT NULL,
        updated_ts INTEGER NOT NULL
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS settings(
        chat_id INTEGER PRIMARY KEY,
        autopin INTEGER NOT NULL DEFAULT 1,
        aitips INTEGER NOT NULL DEFAULT 1,
        lang TEXT NOT NULL DEFAULT 'ru',
        updated_ts INTEGER NOT NULL
    )""")
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
    name = rest.strip()
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

def week_bounds_now() -> Tuple[int, int]:
    now = datetime.now(TIMEZONE)
    start = datetime(now.year, now.month, now.day, tzinfo=TIMEZONE) - timedelta(days=now.weekday())
    return int(start.timestamp()), int(now.timestamp())

def month_bounds_now() -> Tuple[int, int]:
    now = datetime.now(TIMEZONE)
    start = datetime(now.year, now.month, 1, 0, 0, 0, tzinfo=TIMEZONE)
    if now.month == 12:
        next_month = datetime(now.year + 1, 1, 1, tzinfo=TIMEZONE)
    else:
        next_month = datetime(now.year, now.month + 1, 1, tzinfo=TIMEZONE)
    return int(start.timestamp()), int(next_month.timestamp()) - 1

def quarter_bounds_now() -> Tuple[int, int]:
    now = datetime.now(TIMEZONE)
    q = (now.month - 1) // 3
    start_month = q * 3 + 1
    start = datetime(now.year, start_month, 1, tzinfo=TIMEZONE)
    return int(start.timestamp()), int(now.timestamp())

# ---------------- DB Ops ----------------
def add_tx(uid: int, ttype: str, amount: float, currency: str, category: str, note: str = "") -> int:
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("INSERT INTO tx(user_id, ttype, amount, currency, category, note, ts) VALUES(?,?,?,?,?,?,?)",
              (uid, ttype, amount, currency, category, note, ts_now()))
    con.commit(); rowid = c.lastrowid; con.close()
    return rowid

def delete_tx(uid: int, tx_id: int) -> bool:
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("DELETE FROM tx WHERE id=? AND user_id=?", (tx_id, uid))
    ok = c.rowcount > 0
    con.commit(); con.close()
    return ok

def last_txs(uid: int, limit: int = 10, offset: int = 0) -> List[tuple]:
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("""SELECT id, ttype, amount, currency, category, note, ts
                 FROM tx WHERE user_id=?
                 ORDER BY ts DESC, id DESC
                 LIMIT ? OFFSET ?""", (uid, limit, offset))
    rows = c.fetchall()
    con.close()
    return rows

def count_txs(uid: int) -> int:
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("SELECT COUNT(*) FROM tx WHERE user_id=?", (uid,))
    n = c.fetchone()[0]
    con.close()
    return int(n or 0)

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

def delete_debt(uid: int, debt_id: int) -> bool:
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("DELETE FROM debts WHERE id=? AND user_id=?", (debt_id, uid))
    ok = c.rowcount > 0
    con.commit(); con.close()
    return ok

def debts_open(uid: int, direction: str) -> List[tuple]:
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("""SELECT id, amount, currency, counterparty, created_ts
                 FROM debts WHERE user_id=? AND direction=? AND status='open'
                 ORDER BY created_ts DESC, id DESC""", (uid, direction))
    rows = c.fetchall(); con.close()
    return rows

def debt_get(uid: int, debt_id: int) -> Optional[tuple]:
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("""SELECT id, amount, currency, counterparty, status
                 FROM debts WHERE id=? AND user_id=?""", (debt_id, uid))
    row = c.fetchone(); con.close()
    return row

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

def debt_reduce_or_close(uid: int, debt_id: int, reduce_amount: Optional[float] = None) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("SELECT amount, currency, status FROM debts WHERE id=? AND user_id=?", (debt_id, uid))
    row = c.fetchone()
    if not row:
        con.close(); return False, "Долг не найден.", None
    amount, currency, status = float(row[0]), row[1], row[2]
    if status != "open":
        con.close(); return False, "Долг уже закрыт.", None
    undo = {"type":"debt_update", "debt_id": debt_id, "prev_amount": amount, "prev_status": status}
    if reduce_amount is None or reduce_amount >= amount:
        c.execute("UPDATE debts SET status='closed', amount=0, updated_ts=? WHERE id=?", (ts_now(), debt_id))
        con.commit(); con.close()
        return True, f"✅ Долг #{debt_id} закрыт.", undo
    else:
        new_amount = amount - reduce_amount
        c.execute("UPDATE debts SET amount=?, updated_ts=? WHERE id=?", (new_amount, ts_now(), debt_id))
        con.commit(); con.close()
        return True, f"➖ Сумма долга #{debt_id} уменьшена: {fmt_amount(new_amount, currency)}", undo

# Budgets
def budget_set(uid: int, category: str, currency: str, limit_amount: float, period: str = "month"):
    now = ts_now()
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("""INSERT INTO budgets(user_id, category, currency, limit_amount, period, active, created_ts, updated_ts)
                 VALUES(?,?,?,?,1,1,?,?)
                 ON CONFLICT(user_id, category, currency) DO UPDATE SET
                 limit_amount=excluded.limit_amount, period=excluded.period, active=1, updated_ts=excluded.updated_ts""",
              (uid, category, currency, limit_amount, period, now, now))
    # Note: ON CONFLICT requires unique index; create if missing:
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS uniq_budget ON budgets(user_id, category, currency)")
    con.commit(); con.close()

def budget_list(uid: int) -> List[tuple]:
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("""SELECT id, category, currency, limit_amount, period, active FROM budgets
                 WHERE user_id=? AND active=1 ORDER BY category""", (uid,))
    rows = c.fetchall(); con.close()
    return rows

def month_expenses_in_category(uid: int, category: str, currency: str) -> float:
    start, end = month_bounds_now()
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("""SELECT COALESCE(SUM(amount),0) FROM tx
                 WHERE user_id=? AND ttype='expense' AND category=? AND currency=? AND ts BETWEEN ? AND ?""",
              (uid, category, currency, start, end))
    s = c.fetchone()[0] or 0.0
    con.close()
    return float(s)

# Settings and pins
def get_chat_settings(chat_id: int) -> Dict[str, Any]:
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("SELECT autopin, aitips, lang FROM settings WHERE chat_id=?", (chat_id,))
    row = c.fetchone()
    if not row:
        con.close()
        return {"autopin": 1, "aitips": 1, "lang": "ru"}
    con.close()
    return {"autopin": int(row[0]), "aitips": int(row[1]), "lang": row[2]}

def set_chat_setting(chat_id: int, key: str, value: Any):
    now = ts_now()
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("""INSERT INTO settings(chat_id, autopin, aitips, lang, updated_ts)
                 VALUES(?,?,?,?,?)
                 ON CONFLICT(chat_id) DO UPDATE SET {}=excluded.{}, updated_ts=excluded.updated_ts""".format(key, key),
              (chat_id, 1, 1, "ru", now))
    # Update the specific field (second step for sqlite compatibility)
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
                 ON CONFLICT(chat_id) DO UPDATE SET message_id=excluded.message_id""",
              (chat_id, message_id))
    con.commit(); con.close()

# ---------------- Reports/AI helpers ----------------
def sum_range(uid: int, start_ts: int, end_ts: int) -> float:
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("""SELECT COALESCE(SUM(CASE WHEN ttype='expense' THEN amount ELSE 0 END),0)
                 FROM tx WHERE user_id=? AND ts BETWEEN ? AND ?""",
              (uid, start_ts, end_ts))
    s = c.fetchone()[0] or 0.0
    con.close()
    return float(s)

def month_expenses_by_category(uid: int) -> List[tuple]:
    start, end = month_bounds_now()
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("""SELECT category, currency, COALESCE(SUM(amount),0) as s
                 FROM tx
                 WHERE user_id=? AND ttype='expense' AND ts BETWEEN ? AND ?
                 GROUP BY category, currency
                 ORDER BY s DESC""", (uid, start, end))
    rows = c.fetchall(); con.close()
    return rows

def generate_ai_tip(uid: int) -> str:
    # Top category this month
    rows = month_expenses_by_category(uid)
    tip_parts = []
    if rows:
        rows_sorted = sorted(rows, key=lambda r: (r[1] != "uzs", -r[2]))
        top_cat, top_cur, top_sum = rows_sorted[0]
        tip_parts.append(f"Топ расход: «{top_cat}» — {fmt_amount(top_sum, top_cur)} в этом месяце.")
    # Weekly change
    w_start, now_ts = week_bounds_now()
    last_week_end = w_start - 1
    prev_week_start = last_week_end - 6*24*3600
    cur = sum_range(uid, w_start, now_ts)
    prev = sum_range(uid, prev_week_start, last_week_end)
    if prev > 0:
        diff = (cur - prev) / prev * 100.0
        if abs(diff) >= 20:
            tip_parts.append(("Расходы за неделю " + ("выросли" if diff > 0 else "снизились") + f" на {abs(diff):.0f}%."))

    # Budget utilization
    buds = budget_list(uid)
    if buds:
        best = None
        for _, cat, curcy, limit_amt, period, active in buds:
            if active != 1 or period != "month": continue
            spent = month_expenses_in_category(uid, cat, curcy)
            if limit_amt > 0:
                util = spent / limit_amt
                left = max(0.0, limit_amt - spent)
                if not best or util > best[0]:
                    best = (util, cat, curcy, left, limit_amt)
        if best:
            util, cat, curcy, left, lim = best
            tip_parts.append(f"По бюджету «{cat}»: осталось {fmt_amount(left, curcy)} ({min(100,int((1-util)*100))}% до лимита).")

    return " ".join(tip_parts) if tip_parts else "Нет заметных изменений расходов."

def report_text_for_period(uid: int, start: int, end: int, title: str) -> str:
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("""SELECT currency,
                        SUM(CASE WHEN ttype='income' THEN amount ELSE 0 END) as inc,
                        SUM(CASE WHEN ttype='expense' THEN amount ELSE 0 END) as exp
                 FROM tx WHERE user_id=? AND ts BETWEEN ? AND ?
                 GROUP BY currency""", (uid, start, end))
    rows = c.fetchall()
    lines = [f"📊 Отчёт: {title}"]
    if not rows:
        con.close(); return lines[0] + "\nНет операций."
    for cur, inc, exp in rows:
        inc = inc or 0.0; exp = exp or 0.0
        lines.append(f"• Доходы: {fmt_amount(inc, cur)}")
        lines.append(f"• Расходы: {fmt_amount(exp, cur)}")
        lines.append(f"• Итог: {fmt_amount(inc - exp, cur)}")
        lines.append("")
    c.execute("""SELECT category, currency, SUM(amount) as s
                 FROM tx WHERE user_id=? AND ttype='expense' AND ts BETWEEN ? AND ?
                 GROUP BY category, currency
                 ORDER BY s DESC LIMIT 10""", (uid, start, end))
    cats = c.fetchall(); con.close()
    if cats:
        lines.append("Топ расходов по категориям:")
        for cat, cur, s in cats:
            lines.append(f"- {cat}: {fmt_amount(s, cur)}")
    return "\n".join(lines)

async def export_month_csv(uid: int, context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    start, end = month_bounds_now()
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("""SELECT id, ts, ttype, amount, currency, category, note
                 FROM tx WHERE user_id=? AND ts BETWEEN ? AND ?
                 ORDER BY ts ASC""", (uid, start, end))
    rows = c.fetchall(); con.close()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id","datetime","type","amount","currency","category","note"])
    for rid, ts, ttype, amount, currency, category, note in rows:
        w.writerow([rid, datetime.fromtimestamp(ts, tz=TIMEZONE).strftime("%Y-%m-%d %H:%M:%S"), ttype, amount, currency, category, note or ""])
    data = buf.getvalue().encode("utf-8")
    await context.bot.send_document(chat_id=chat_id, document=io.BytesIO(data), filename="transactions_month.csv")

async def export_debts_csv(uid: int, context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("""SELECT id, direction, amount, currency, counterparty, status, created_ts, updated_ts
                 FROM debts WHERE user_id=? ORDER BY created_ts DESC""", (uid,))
    rows = c.fetchall(); con.close()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id","direction","amount","currency","counterparty","status","created_at","updated_at"])
    for rid, direction, amount, currency, cp, status, cts, uts in rows:
        w.writerow([rid, direction, amount, currency, cp, status,
                    datetime.fromtimestamp(cts, tz=TIMEZONE).strftime("%Y-%m-%d %H:%M:%S"),
                    datetime.fromtimestamp(uts, tz=TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")])
    data = buf.getvalue().encode("utf-8")
    await context.bot.send_document(chat_id=chat_id, document=io.BytesIO(data), filename="debts.csv")

# ---------------- Balance summary + pin ----------------
def build_balance_summary(uid: int) -> str:
    now = datetime.now(TIMEZONE)
    head = f"📌 Итог на {now.strftime('%d.%m')}, {now.strftime('%H:%M')}"
    net = net_by_currency(uid)
    debts = debt_totals_by_currency(uid)

    def fmt_multi(label: str) -> str:
        parts = []
        currencies = set(net.keys()) | set(debts.keys())
        for cur in sorted(currencies):
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
        fmt_multi("Баланс"),
        fmt_multi("Я должен"),
        fmt_multi("Мне должны"),
        fmt_multi("Чистый баланс"),
    ]
    return "\n".join(lines)

async def send_and_pin_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    uid = update.effective_user.id
    st = get_chat_settings(chat_id)
    text = build_balance_summary(uid)
    if st.get("aitips", 1):
        tip = generate_ai_tip(uid)
        text = text + "\n\n" + f"💡 {tip}"
    msg = await context.bot.send_message(chat_id=chat_id, text=text)
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

# ---------------- Undo last action (in-memory per user) ----------------
def set_last_action(context: ContextTypes.DEFAULT_TYPE, uid: int, payload: Dict[str, Any]):
    context.user_data["last_action"] = {"uid": uid, **payload}

def get_last_action(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Any]:
    return context.user_data.get("last_action", {})

def clear_last_action(context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("last_action", None)

async def undo_last(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    act = get_last_action(context)
    if not act or act.get("uid") != uid:
        await update.message.reply_text("Нет последней операции для отмены.")
        return
    t = act.get("type")
    if t == "tx_add":
        ok = delete_tx(uid, act["tx_id"])
        await update.message.reply_text("Отменено: последняя транзакция удалена." if ok else "Не удалось отменить транзакцию.")
    elif t == "debt_add":
        ok = delete_debt(uid, act["debt_id"])
        await update.message.reply_text("Отменено: последний долг удалён." if ok else "Не удалось отменить долг.")
    elif t == "debt_update":
        debt_id = act["debt_id"]
        prev_amount = act.get("prev_amount")
        prev_status = act.get("prev_status", "open")
        con = sqlite3.connect(DB_PATH); c = con.cursor()
        c.execute("UPDATE debts SET amount=?, status='open', updated_ts=? WHERE id=?",
                  (prev_amount, ts_now(), debt_id))
        con.commit(); con.close()
        await update.message.reply_text("Отмена применена: долг восстановлен.")
    else:
        await update.message.reply_text("Эту операцию отменить нельзя.")
        return
    clear_last_action(context)
    await send_and_pin_summary(update, context)

# ---------------- Handlers ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USER_IDS and update.effective_user.id not in ALLOWED_USER_IDS:
        await update.message.reply_text("Доступ запрещён.")
        return
    await update.message.reply_text("Главное меню:", reply_markup=MAIN_KB)

async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(build_balance_summary(uid))

def build_history_text(uid: int, page: int, page_size: int = 10) -> Tuple[str, int]:
    total = count_txs(uid)
    pages = max(1, math.ceil(total / page_size))
    page = max(1, min(page, pages))
    offset = (page - 1) * page_size
    rows = last_txs(uid, page_size, offset)
    if not rows:
        return "История пуста.", pages
    lines = [f"История (стр. {page}/{pages}):"]
    for rid, ttype, amount, currency, category, note, ts in rows:
        when = dt_fmt(ts)
        lines.append(f"#{rid} {when} — {'+' if ttype=='income' else '-'} {fmt_amount(amount, currency)} [{category}]")
    return "\n".join(lines), pages

def history_kb(uid: int, page: int, pages: int) -> InlineKeyboardMarkup:
    buttons = []
    if page > 1:
        buttons.append(InlineKeyboardButton("⟨ Пред", callback_data=f"hist:prev:{page-1}"))
    if page < pages:
        buttons.append(InlineKeyboardButton("След ⟩", callback_data=f"hist:next:{page+1}"))
    return InlineKeyboardMarkup([buttons] if buttons else [])

async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text, pages = build_history_text(uid, 1)
    await update.message.reply_text(text, reply_markup=history_kb(uid, 1, pages))

def report_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Неделя", callback_data="report:week"),
          InlineKeyboardButton("Месяц", callback_data="report:month"),
          InlineKeyboardButton("Квартал", callback_data="report:quarter")]]
    )

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    uid = update.effective_user.id
    chat_id = update.effective_chat.id

    if data.startswith("debt_close:"):
        debt_id = int(data.split(":")[1])
        ok, msg, undo = debt_reduce_or_close(uid, debt_id, None)
        if ok and undo: set_last_action(context, uid, undo)
        await context.bot.send_message(chat_id=chat_id, text=msg)
        await send_and_pin_summary(update, context)
        return

    if data.startswith("debt_reduce:"):
        debt_id = int(data.split(":")[1])
        context.user_data["debts"] = {"stage":"reduce_ask_amount", "debt_id": debt_id}
        await context.bot.send_message(chat_id=chat_id, text="Введите сумму уменьшения (например: 1000 или 10 usd). Для полного закрытия введите 0.")
        return

    if data.startswith("hist:"):
        parts = data.split(":")
        direction = parts[1]
        page = int(parts[2])
        text, pages = build_history_text(uid, page)
        try:
            await q.edit_message_text(text=text, reply_markup=history_kb(uid, page, pages))
        except Exception:
            await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=history_kb(uid, page, pages))
        return

    if data.startswith("report:"):
        if data == "report:week":
            s, e = week_bounds_now(); title = "неделя"
        elif data == "report:month":
            s, e = month_bounds_now(); title = "месяц"
        else:
            s, e = quarter_bounds_now(); title = "квартал"
        text = report_text_for_period(uid, s, e, title)
        await context.bot.send_message(chat_id=chat_id, text=text)
        return

    if data.startswith("settings:"):
        _, action, key = data.split(":")
        st = get_chat_settings(chat_id)
        if action == "toggle":
            new_val = 0 if st.get(key, 1) else 1
            set_chat_setting(chat_id, key, new_val)
        elif action == "setlang":
            set_chat_setting(chat_id, "lang", key)
        st = get_chat_settings(chat_id)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"Автопин: {'Вкл' if st['autopin'] else 'Выкл'}", callback_data="settings:toggle:autopin")],
            [InlineKeyboardButton(f"AI‑подсказки: {'Вкл' if st['aitips'] else 'Выкл'}", callback_data="settings:toggle:aitips")],
            [InlineKeyboardButton("Язык: RU", callback_data="settings:setlang:ru")]
        ])
        try:
            await q.edit_message_text(text="Настройки:", reply_markup=kb)
        except Exception:
            await context.bot.send_message(chat_id=chat_id, text="Настройки:", reply_markup=kb)
        return

# ------------- Flow helpers -------------
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

def set_budget_state(context: ContextTypes.DEFAULT_TYPE, state: dict):
    context.user_data["budget"] = state

def get_budget_state(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.user_data.get("budget", {})

def clear_budget_state(context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("budget", None)

# ---------------- Text router ----------------
async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USER_IDS and update.effective_user.id not in ALLOWED_USER_IDS:
        await update.message.reply_text("Доступ запрещён.")
        return

    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    txt = (update.message.text or "").strip()
    low = txt.lower()

    # Undo
    if txt == CANCEL_BTN:
        await undo_last(update, context)
        return

    # ---------- Debts FSM ----------
    debts = get_debts_state(context)
    stage = debts.get("stage")

    if txt == BACK_BTN and debts:
        clear_debts_state(context)
        await update.message.reply_text("Главное меню.", reply_markup=MAIN_KB)
        return

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
        debt_id = debt_add(uid, direction, amount, currency, name)
        set_last_action(context, uid, {"type":"debt_add", "debt_id":debt_id})
        when = dt_fmt(ts_now())
        party_line = f"• Должник: {name}" if direction == "owed" else f"• Кому: {name}"
        await update.message.reply_text(
            "✅ Долг добавлен:\n"
            f"• Сумма: {fmt_amount(amount, currency)}\n{party_line}\n• Дата: {when}"
        )
        clear_debts_state(context)
        await show_debts_list(update, context, direction)
        await send_and_pin_summary(update, context)
        return

    if stage == "await_counterparty":
        direction = debts.get("direction")
        amount = debts.get("amount")
        currency = debts.get("currency")
        name = txt.strip()
        if not name:
            await update.message.reply_text("Введите имя/комментарий.")
            return
        debt_id = debt_add(uid, direction, amount, currency, name)
        set_last_action(context, uid, {"type":"debt_add", "debt_id":debt_id})
        when = dt_fmt(ts_now())
        party_line = f"• Должник: {name}" if direction == "owed" else f"• Кому: {name}"
        await update.message.reply_text(
            "✅ Долг добавлен:\n"
            f"• Сумма: {fmt_amount(amount, currency)}\n{party_line}\n• Дата: {when}"
        )
        clear_debts_state(context)
        await show_debts_list(update, context, direction)
        await send_and_pin_summary(update, context)
        return

    if stage == "reduce_ask_id":
        if not txt.lstrip("#").isdigit():
            await update.message.reply_text("Введите ID долга, например: 3")
            return
        set_debts_state(context, {"stage":"reduce_ask_amount", "debt_id": int(txt.lstrip('#'))})
        await update.message.reply_text("На сколько уменьшить? (например: 1000 или 1000 usd). Для полного закрытия введите 0.")
        return

    if stage == "reduce_ask_amount":
        if txt.strip() in {"0","0 uzs","0 usd","закрыть","close"}:
            ok, msg, undo = debt_reduce_or_close(uid, get_debts_state(context)["debt_id"], None)
            if ok and undo: set_last_action(context, uid, undo)
            await update.message.reply_text(msg)
            clear_debts_state(context)
            await update.message.reply_text("Выберите действие:", reply_markup=debts_menu_kb())
            await send_and_pin_summary(update, context)
            return
        amt = parse_amount(txt)
        if not amt:
            await update.message.reply_text("Введите число, например: 1500")
            return
        # capture undo
        row = debt_get(uid, get_debts_state(context)["debt_id"])
        prev_amount = float(row[1]) if row else None
        ok, msg, undo = debt_reduce_or_close(uid, get_debts_state(context)["debt_id"], amt)
        if ok:
            if undo is None and prev_amount is not None:
                undo = {"type":"debt_update", "debt_id": get_debts_state(context)["debt_id"], "prev_amount": prev_amount, "prev_status":"open"}
            if undo: set_last_action(context, uid, undo)
        await update.message.reply_text(msg)
        clear_debts_state(context)
        await update.message.reply_text("Выберите действие:", reply_markup=debts_menu_kb())
        await send_and_pin_summary(update, context)
        return

    # Debts menu entry/actions
    if txt in {DEBTS_BTN, "Долги"} or ("долг" in low and not debts):
        set_debts_state(context, {"stage":"menu"})
        await update.message.reply_text("Раздел «Долги». Выберите действие:", reply_markup=debts_menu_kb())
        return

    if not debts and txt in {"📜 Мне должны", "📜 Я должен"}:
        set_debts_state(context, {"stage":"menu"})
        direction = "owed" if "Мне" in txt else "owes"
        await show_debts_list(update, context, direction)
        return

    if debts.get("stage") == "menu":
        if low.replace("+", "➕") in {"➕ я должен", "➕ мне должны"} or txt in {"➕ Я должен", "➕ Мне должны"}:
            direction = "owed" if "мне должны" in low else "owes"
            set_debts_state(context, {"stage":"await_amount", "direction":direction})
            prompt = "Введите сумму и имя, например: 5000 usd Ahmed" if direction=="owed" else "Введите сумму и кому должны, например: 300 usd Rent"
            await update.message.reply_text(prompt)
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
        if txt == "Экспорт долгов 📂":
            await export_debts_csv(uid, context, chat_id)
            return
        if txt == BACK_BTN:
            clear_debts_state(context)
            await update.message.reply_text("Главное меню.", reply_markup=MAIN_KB)
            return
        await update.message.reply_text("Выберите действие:", reply_markup=debts_menu_kb())
        return

    # ---------- Budget FSM ----------
    budget = get_budget_state(context)
    bstage = budget.get("stage")
    if txt == BUDGET_BTN and not bstage:
        set_budget_state(context, {"stage":"choose_cat"})
        await update.message.reply_text("Выберите категорию для бюджета:", reply_markup=build_categories_kb(EXPENSE_CATS))
        return
    if txt == BACK_BTN and bstage:
        clear_budget_state(context)
        await update.message.reply_text("Главное меню.", reply_markup=MAIN_KB)
        return
    if bstage == "choose_cat":
        if txt in EXPENSE_CATS:
            set_budget_state(context, {"stage":"await_amount", "category":txt})
            await update.message.reply_text(f"Введите лимит и валюту для «{txt}», например: 5 000 000 uzs или 300 usd.",
                                            reply_markup=ReplyKeyboardMarkup([[KeyboardButton(BACK_BTN)]], resize_keyboard=True))
            return
    if bstage == "await_amount":
        amount = parse_amount(txt)
        if not amount:
            await update.message.reply_text("Введите число, например: 5 000 000 uzs")
            return
        currency = detect_currency(txt)
        category = budget.get("category")
        budget_set(uid, category, currency, amount, "month")
        await update.message.reply_text(f"✅ Бюджет сохранён: {category} — {fmt_amount(amount, currency)} / месяц.")
        clear_budget_state(context)
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
        await update.message.reply_text(f"Введите сумму для «{txt}» (например: 25000 или 20 usd).",
                                        reply_markup=ReplyKeyboardMarkup([[KeyboardButton(BACK_BTN)]], resize_keyboard=True))
        return

    if flow.get("stage") == "choose_income" and txt in INCOME_CATS:
        set_flow(context, {"stage":"await_amount", "ttype":"income", "category":txt})
        await update.message.reply_text(f"Введите сумму для «{txt}» (например: 25000 или 20 usd).",
                                        reply_markup=ReplyKeyboardMarkup([[KeyboardButton(BACK_BTN)]], resize_keyboard=True))
        return

    if flow.get("stage") == "await_amount":
        amount = parse_amount(txt)
        if not amount:
            await update.message.reply_text("Введите корректную сумму, например: 25000 или 20 usd.")
            return
        currency = detect_currency(txt)
        ttype = flow.get("ttype")
        category = flow.get("category")
        tx_id = add_tx(uid, ttype, amount, currency, category, "")
        set_last_action(context, uid, {"type":"tx_add", "tx_id": tx_id})
        await update.message.reply_text(f"✅ Сохранено: {('+' if ttype=='income' else '-')}{fmt_amount(amount, currency)} [{category}]")
        # Budget notifications
        if ttype == "expense":
            for _, cat, curcy, limit_amt, period, active in budget_list(uid):
                if active == 1 and period == "month" and cat == category and curcy == currency and limit_amt > 0:
                    spent = month_expenses_in_category(uid, category, currency)
                    util = spent / limit_amt
                    if 0.8 <= util < 1.0:
                        await update.message.reply_text(f"⚠️ Достигнуто 80% бюджета по «{category}». Потрачено {fmt_amount(spent, currency)} из {fmt_amount(limit_amt, currency)}.")
                    if util >= 1.0:
                        await update.message.reply_text(f"⛔️ Бюджет по «{category}» исчерпан. Потрачено {fmt_amount(spent, currency)} из {fmt_amount(limit_amt, currency)}.")
                    break
        clear_flow(context)
        await send_and_pin_summary(update, context)
        await update.message.reply_text("Главное меню.", reply_markup=MAIN_KB)
        return

    # ---------- Simple buttons ----------
    if txt == BALANCE_BTN:
        await update.message.reply_text(build_balance_summary(uid))
        return
    if txt == HISTORY_BTN:
        await history_cmd(update, context)
        return
    if txt == REPORT_BTN:
        await update.message.reply_text("Выберите период отчёта:", reply_markup=report_kb())
        return
    if txt == EXPORT_BTN:
        await export_month_csv(uid, context, chat_id)
        return
    if txt == SETTINGS_BTN:
        st = get_chat_settings(chat_id)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"Автопин: {'Вкл' if st['autopin'] else 'Выкл'}", callback_data="settings:toggle:autopin")],
            [InlineKeyboardButton(f"AI‑подсказки: {'Вкл' if st['aitips'] else 'Выкл'}", callback_data="settings:toggle:aitips")],
            [InlineKeyboardButton("Язык: RU", callback_data="settings:setlang:ru")]
        ])
        await update.message.reply_text("Настройки:", reply_markup=kb)
        return

    # ---------- Free-form fallback ----------
    amount = parse_amount(txt)
    if amount:
        currency = detect_currency(txt)
        ttype = "expense"
        category = "Прочее"
        tx_id = add_tx(uid, ttype, amount, currency, category, txt)
        set_last_action(context, uid, {"type":"tx_add", "tx_id": tx_id})
        await update.message.reply_text(f"✅ Сохранено: -{fmt_amount(amount, currency)} [{category}]")
        await send_and_pin_summary(update, context)
        return

    await update.message.reply_text("Не понял. Выберите действие.", reply_markup=MAIN_KB)

# -------- Debts list + inline manage --------
def debts_inline_kb(rows: List[tuple]) -> InlineKeyboardMarkup:
    btn_rows = []
    for did, amount, currency, name, created_ts in rows[:10]:
        btn_rows.append([
            InlineKeyboardButton(f"Закрыть #{did}", callback_data=f"debt_close:{did}"),
            InlineKeyboardButton(f"➖ #{did}", callback_data=f"debt_reduce:{did}")
        ])
    return InlineKeyboardMarkup(btn_rows) if btn_rows else InlineKeyboardMarkup([])

async def show_debts_list(update: Update, context: ContextTypes.DEFAULT_TYPE, direction: str):
    uid = update.effective_user.id
    rows = debts_open(uid, direction)
    title = "Список должников:" if direction == "owed" else "Список моих долгов:"
    if not rows:
        await update.message.reply_text(title + "\nСписок пуст.", reply_markup=debts_menu_kb())
        return
    lines = [title]
    for did, amount, currency, name, created_ts in rows:
        lines.append(f"#{did} {name or '-'} — {fmt_amount(amount, currency)} ({datetime.fromtimestamp(created_ts, tz=TIMEZONE).strftime('%d.%m.%Y')})")
    await update.message.reply_text("\n".join(lines), reply_markup=debts_menu_kb())
    await update.message.reply_text("Управление долгами:", reply_markup=debts_inline_kb(rows))

# ---------------- Main ----------------
def build_app(token: str) -> Application:
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CallbackQueryHandler(on_callback))
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
