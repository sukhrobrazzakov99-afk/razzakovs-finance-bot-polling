import os, re, sqlite3, time, logging, csv, io
from datetime import datetime, timedelta, time as dtime
from typing import Optional, Tuple, List
from zoneinfo import ZoneInfo
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler

import httpx
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# ---------------- Config ----------------
PORT = int(os.environ.get("PORT", "8080"))
DB_PATH = os.environ.get("DB_PATH", "finance.db")
TIMEZONE = ZoneInfo(os.environ.get("TZ", "Asia/Tashkent"))
ALLOWED_USER_IDS = {int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").replace(";", ",").split(",") if x.strip().isdigit()}
ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID")) if os.environ.get("ADMIN_USER_ID", "").isdigit() else None
DEFAULT_BOT_TOKEN = os.environ.get("BOT_TOKEN", "7611168200:AAH_NPSecM5hrqPKindVLiQy4zkPIauqmTc")

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s | %(message)s", level=logging.INFO)
log = logging.getLogger("bot")

# ---------------- Healthcheck (Railway Web) ----------------
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args): return

def start_healthcheck_server():
    HTTPServer(("0.0.0.0", PORT), HealthHandler).serve_forever()

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
    c.execute("""CREATE TABLE IF NOT EXISTS budgets(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        category TEXT NOT NULL,
        currency TEXT NOT NULL,
        limit_amount REAL NOT NULL,
        period TEXT NOT NULL DEFAULT 'month',
        created_ts INTEGER NOT NULL
    )""")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_budget ON budgets(user_id, category, currency, period)")
    c.execute("""CREATE TABLE IF NOT EXISTS reminders(
        user_id INTEGER PRIMARY KEY,
        hour INTEGER NOT NULL,
        minute INTEGER NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS recurring(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        ttype TEXT NOT NULL CHECK(ttype IN('income','expense')),
        amount REAL NOT NULL,
        currency TEXT NOT NULL,
        category TEXT NOT NULL,
        note TEXT,
        frequency TEXT NOT NULL CHECK(frequency IN('daily','weekly','monthly')),
        day_of_week INTEGER,
        day_of_month INTEGER,
        last_applied_date TEXT,
        created_ts INTEGER NOT NULL
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_recurring_user ON recurring(user_id)")
    c.execute("""CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY,
        first_name TEXT,
        username TEXT,
        last_seen_ts INTEGER NOT NULL
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS debts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        direction TEXT NOT NULL CHECK(direction IN('i_owe','they_owe')),
        counterparty TEXT NOT NULL,
        amount REAL NOT NULL,
        currency TEXT NOT NULL,
        note TEXT,
        status TEXT NOT NULL DEFAULT 'open' CHECK(status IN('open','closed')),
        created_ts INTEGER NOT NULL,
        updated_ts INTEGER NOT NULL
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_debts_user ON debts(user_id, status, direction)")
    con.commit(); con.close()
init_db()

# ---------------- Keyboards ----------------
BACK_BTN = "⬅️ Назад"

MAIN_KB = ReplyKeyboardMarkup(
    [
        [KeyboardButton("➕ Доход"), KeyboardButton("➖ Расход")],
        [KeyboardButton("💰 Баланс"), KeyboardButton("📜 История")],
        [KeyboardButton("📊 Отчёт (месяц)"), KeyboardButton("Экспорт 📂")],
        [KeyboardButton("↩️ Отменить"), KeyboardButton("✏️ Редактировать")],
        [KeyboardButton("Бюджет 💡"), KeyboardButton("Курс валют 💱")],
        [KeyboardButton("Долги")],
        [KeyboardButton("🔁 Повторы"), KeyboardButton("📈 Аналитика")],
        [KeyboardButton("📅 Автодаты"), KeyboardButton("🔔 Напоминания")],
        [KeyboardButton("PDF отчёт"), KeyboardButton("👥 Пользователи")],
    ],
    resize_keyboard=True
)

def debts_menu_kb() -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton("➕ Я должен"), KeyboardButton("➕ Мне должны")],
        [KeyboardButton("📜 Я должен"), KeyboardButton("📜 Мне должны")],
        [KeyboardButton("✖️ Закрыть долг"), KeyboardButton("➖ Уменьшить долг")],
        [KeyboardButton(BACK_BTN)]
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def _rows_keyboard(labels: List[str], per_row: int = 3) -> List[List[KeyboardButton]]:
    rows, row = [], []
    for i, lbl in enumerate(labels, 1):
        row.append(KeyboardButton(lbl))
        if i % per_row == 0:
            rows.append(row); row = []
    if row: rows.append(row)
    return rows

def categories_kb(ttype: str) -> ReplyKeyboardMarkup:
    cats = EXPENSE_CATEGORIES if ttype == "expense" else INCOME_CATEGORIES
    rows = _rows_keyboard(cats, per_row=3)
    rows.append([KeyboardButton(BACK_BTN)])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def amount_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[KeyboardButton(BACK_BTN)]], resize_keyboard=True)

# ---------------- Categories ----------------
EXPENSE_CATEGORIES = ["Еда","Транспорт","Здоровье","Развлечения","Дом","Детское","Спорт","Прочее"]
INCOME_CATEGORIES  = ["Зарплата","Подработка","Подарок","Премия","Инвестиции","Прочее"]
CATEGORY_KEYWORDS = {
    "Еда": ["еда","продукт","обед","ужин","завтрак","кафе","ресторан","самса","плов","шаурма","пицца"],
    "Транспорт": ["такси","топливо","бензин","газ","метро","автобус","аренда авто","аренда машины"],
    "Зарплата": ["зарплата","оклад"],
    "Премия": ["премия","бонус","аванс"],
    "Здоровье": ["аптека","врач","стоматолог","лекар","витамин"],
    "Развлечения": ["кино","игра","cs2","steam","подписка","spotify","netflix"],
    "Дом": ["аренда","квартира","коммунал","электр","интернет","ремонт"],
    "Детское": ["памперс","подгуз","коляска","игруш","детск","дочка","хадиджа"],
    "Спорт": ["зал","спорт","креатин","протеин","гейнер","абонемент"],
    "Подарок": ["подарок","дарил","дарение"],
    "Подработка": ["подработка","фриланс","халтура"],
    "Инвестиции": ["акции","инвест","вклад"],
    "Прочее": []
}

# ---------------- Helpers ----------------
def is_authorized(user_id: int) -> bool:
    return True if not ALLOWED_USER_IDS else user_id in ALLOWED_USER_IDS

def upsert_seen_user(uid: int, first_name: str, username: Optional[str]):
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    now = int(time.time())
    c.execute("""INSERT INTO users(user_id, first_name, username, last_seen_ts)
                 VALUES(?,?,?,?)
                 ON CONFLICT(user_id) DO UPDATE SET
                   first_name=excluded.first_name,
                   username=excluded.username,
                   last_seen_ts=excluded.last_seen_ts
              """, (uid, first_name, username, now))
    con.commit(); con.close()

def detect_currency(t: str) -> str:
    tl = t.lower()
    if "$" in tl: return "usd"
    words = set(re.findall(r"[a-zа-яё]+", tl))
    if {"usd","доллар","доллара","доллары","долларов","бакс","баксы","дол"} & words: return "usd"
    if {"uzs","sum","сум","сумы","сумов"} & words: return "uzs"
    return "uzs"

def parse_amount(t: str) -> Optional[float]:
    s = t.replace("\u00A0", " ")
    m = re.findall(r"(?:(?<=\s)|^|(?<=[^\w]))(\d{1,3}(?:[ \u00A0\.,]\d{3})+|\d+)(?:[.,](\d{1,2}))?", s)
    if not m: return None
    raw, frac = m[-1]
    num = re.sub(r"[ \u00A0\.,]", "", raw)
    try: return float(f"{num}.{frac}") if frac else float(num)
    except ValueError: return None

CURRENCY_WORDS = {"usd","uzs","sum","сум","сумы","сумов","доллар","доллара","доллары","долларов","бакс","баксы","дол"}
def extract_counterparty_from_text(t: str) -> str:
    words = re.findall(r"[A-Za-zА-Яа-яЁё]+", t)
    names = [w for w in words if w.lower() not in CURRENCY_WORDS]
    return " ".join(names[-2:]) if names else ""

def fmt_amount(amount: float, cur: str) -> str:
    if cur == "uzs": return f"{int(round(amount)):,}".replace(",", " ")
    return f"{amount:.2f}"

# ---------------- TX ----------------
def ai_classify_finance(t: str):
    ttype = "expense"
    lt = t.lower()
    if any(w in lt for w in ["зарплата","премия","бонус","получил","пришло","доход"]): ttype = "income"
    amount = parse_amount(t); cur = detect_currency(t); cat = "Прочее"
    for c, kws in CATEGORY_KEYWORDS.items():
        if any(k in lt for k in kws): cat = c; break
    if ttype == "income" and cat == "Прочее":
        if any(x in lt for x in ["зарплат"]): cat = "Зарплата"
        elif any(x in lt for x in ["прем","бонус"]): cat = "Премия"
        elif any(x in lt for x in ["подар"]): cat = "Подарок"
        elif any(x in lt for x in ["подработ","фриланс","халтур"]): cat = "Подработка"
    return ttype, amount, cur, cat

def add_tx(uid: int, ttype: str, amount: float, cur: str, cat: str, note: str) -> int:
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("INSERT INTO tx(user_id,ttype,amount,currency,category,note,ts) VALUES(?,?,?,?,?,?,?)",
              (uid, ttype, amount, cur, cat, note, int(time.time())))
    tx_id = c.lastrowid
    con.commit(); con.close()
    return tx_id

def last_txs(uid: int, limit: int = 10):
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("""SELECT id, ttype, amount, currency, category, note, ts
                 FROM tx WHERE user_id=? ORDER BY ts DESC LIMIT ?""", (uid, limit))
    rows = c.fetchall(); con.close(); return rows

def get_balance(uid: int) -> Tuple[float,float]:
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    def s(t, cur):
        c.execute("SELECT COALESCE(SUM(amount),0) FROM tx WHERE user_id=? AND ttype=? AND currency=?",
                  (uid, t, cur))
        return c.fetchone()[0] or 0.0
    bal_uzs = s("income","uzs") - s("expense","uzs")
    bal_usd = s("income","usd") - s("expense","usd")
    con.close()
    return bal_uzs, bal_usd

def month_bounds_now():
    now = datetime.now(TIMEZONE)
    start = datetime(now.year, now.month, 1, 0, 0, 0, tzinfo=TIMEZONE)
    return int(start.timestamp()), int(now.timestamp())

def period_bounds(keyword: str) -> Tuple[int,int,str]:
    now = datetime.now(TIMEZONE); key = keyword.lower()
    if "сегодня" in key:
        start = datetime(now.year, now.month, now.day, 0, 0, 0, tzinfo=TIMEZONE)
        return int(start.timestamp()), int(now.timestamp()), "сегодня"
    if "вчера" in key:
        y = now - timedelta(days=1)
        start = datetime(y.year, y.month, y.day, 0, 0, 0, tzinfo=TIMEZONE)
        end = datetime(y.year, y.month, y.day, 23, 59, 59, tzinfo=TIMEZONE)
        return int(start.timestamp()), int(end.timestamp()), "вчера"
    week_start = now - timedelta(days=(now.weekday()))
    start = datetime(week_start.year, week_start.month, week_start.day, 0, 0, 0, tzinfo=TIMEZONE)
    return int(start.timestamp()), int(now.timestamp()), "на этой неделе"

async def month_report_text(uid: int) -> str:
    start_ts, end_ts = month_bounds_now()
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("""SELECT ttype, currency, COALESCE(SUM(amount),0)
                 FROM tx WHERE user_id=? AND ts BETWEEN ? AND ?
                 GROUP BY ttype, currency""", (uid, start_ts, end_ts))
    sums = {(tt, cur): total for tt, cur, total in c.fetchall()}
    c.execute("""SELECT category, currency, COALESCE(SUM(amount),0) AS s
                 FROM tx WHERE user_id=? AND ts BETWEEN ? AND ? AND ttype='expense'
                 GROUP BY category, currency ORDER BY s DESC LIMIT 5""", (uid, start_ts, end_ts))
    top = c.fetchall()
    con.close()
    inc_uzs = sums.get(("income","uzs"), 0.0); inc_usd = sums.get(("income","usd"), 0.0)
    exp_uzs = sums.get(("expense","uzs"), 0.0); exp_usd = sums.get(("expense","usd"), 0.0)
    bal_uzs = inc_uzs - exp_узs; bal_usd = inc_usd - exp_usd
    lines = [
        "Отчёт (месяц):",
        f"• Доход UZS: {fmt_amount(inc_uzs,'uzs')} | USD: {fmt_amount(inc_usd,'usd')}",
        f"• Расход UZS: {fmt_amount(exp_uzs,'uzs')} | USD: {fmt_amount(exp_usd,'usd')}",
        f"• Баланс UZS: {fmt_amount(bal_uzs,'uzs')} | USD: {fmt_amount(bal_usd,'usd')}",
    ]
    if top:
        lines.append("Топ расходов:")
        for cat, cur, s in top:
            lines.append(f"  - {cat}: {fmt_amount(s, cur)} {cur.upper()}")
    return "\n".join(lines)

def undo_last(uid: int) -> Optional[Tuple]:
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("SELECT id, ttype, amount, currency, category, note FROM tx WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,))
    row = c.fetchone()
    if not row: con.close(); return None
    tx_id, ttype, amount, currency, category, note = row
    c.execute("DELETE FROM tx WHERE id=?", (tx_id,))
    con.commit(); con.close()
    return row

# ---------------- Budgets ----------------
def set_budget(uid: int, category: str, currency: str, limit_amount: float):
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    now = int(time.time())
    c.execute("""INSERT INTO budgets(user_id, category, currency, limit_amount, period, created_ts)
                 VALUES(?,?,?,?, 'month', ?)
                 ON CONFLICT(user_id, category, currency, period) DO UPDATE SET
                   limit_amount=excluded.limit_amount
              """, (uid, category, currency, limit_amount, now))
    con.commit(); con.close()

def get_budgets(uid: int) -> List[Tuple[str, str, float]]:
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("SELECT category, currency, limit_amount FROM budgets WHERE user_id=? AND period='month' ORDER BY category", (uid,))
    rows = c.fetchall(); con.close(); return rows

def month_expense_sum(uid: int, category: str, currency: str) -> float:
    start_ts, end_ts = month_bounds_now()
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("""SELECT COALESCE(SUM(amount),0) FROM tx
                 WHERE user_id=? AND ttype='expense' AND category=? AND currency=? AND ts BETWEEN ? AND ?""",
              (uid, category, currency, start_ts, end_ts))
    s = c.fetchone()[0] or 0.0
    con.close(); return s

async def maybe_warn_budget(update: Update, uid: int, category: str, currency: str):
    limit = None
    for cat, cur, lim in get_budgets(uid):
        if cat == category and cur == currency:
            limit = lim; break
    if limit is None: return
    spent = month_expense_sum(uid, category, currency)
    if spent >= limit:
        over = spent - limit
        await update.message.reply_text(
            f"Внимание: бюджет по «{category}» превышен.\n"
            f"Лимит: {fmt_amount(limit,currency)} {currency.upper()}, израсходовано: {fmt_amount(spent,currency)} ({fmt_amount(over,currency)} сверх).",
            reply_markup=MAIN_KB
        )

# ---------------- Recurring / Reminders (optional) ----------------
DOW_MAP = {"пн":0,"пон":0,"понедельник":0,"вт":1,"вторник":1,"ср":2,"среда":2,"чт":3,"чет":3,"четверг":3,"пт":4,"пятница":4,"птн":4,"сб":5,"суббота":5,"вс":6,"воскресенье":6}

def add_recurring(uid: int, ttype: str, amount: float, currency: str, category: str, note: str, frequency: str, day_of_week: Optional[int], day_of_month: Optional[int]):
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("""INSERT INTO recurring(user_id, ttype, amount, currency, category, note, frequency, day_of_week, day_of_month, last_applied_date, created_ts)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
              (uid, ttype, amount, currency, category, note, frequency, day_of_week, day_of_month, None, int(time.time())))
    con.commit(); con.close()

def list_recurring(uid: int) -> List[Tuple]:
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("""SELECT id, ttype, amount, currency, category, note, frequency, day_of_week, day_of_month
                 FROM recurring WHERE user_id=? ORDER BY id DESC""", (uid,))
    rows = c.fetchall(); con.close(); return rows

def mark_recurring_applied(rec_id: int, date_str: str):
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("UPDATE recurring SET last_applied_date=? WHERE id=?", (date_str, rec_id))
    con.commit(); con.close()

async def process_recurring_all(app: Application):
    today = datetime.now(TIMEZONE).date(); date_str = today.isoformat()
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("""SELECT id, user_id, ttype, amount, currency, category, note, frequency, day_of_week, day_of_month, last_applied_date FROM recurring""")
    rows = c.fetchall(); con.close()
    for rec in rows:
        rec_id, uid, ttype, amount, currency, category, note, freq, dow, dom, last_date = rec
        if last_date == date_str: continue
        do = (freq == "daily") or (freq == "weekly" and dow is not None and today.weekday() == int(dow)) or (freq == "monthly" and dom is not None and today.day == int(dom))
        if do:
            add_tx(uid, ttype, amount, currency, category, note or f"Recurring {freq}")
            mark_recurring_applied(rec_id, date_str)
            try:
                await app.bot.send_message(chat_id=uid, text=f"Добавлена регулярная операция: {category} {fmt_amount(amount, currency)} {currency.upper()} ({'Доход' if ttype=='income' else 'Расход'})")
            except Exception as e:
                log.warning(f"notify recurring failed for {uid}: {e}")

def schedule_daily_jobs(app: Application):
    if not getattr(app, "job_queue", None):
        log.info("JobQueue not available; skip schedules"); return
    app.job_queue.run_daily(lambda ctx: ctx.application.create_task(process_recurring_all(ctx.application)),
                            dtime(hour=9, minute=0, tzinfo=TIMEZONE), name="recurring-processor")

def schedule_reminder_for_user(app: Application, uid: int, hour: int, minute: int):
    if not getattr(app, "job_queue", None): return
    job_name = f"reminder-{uid}"
    for job in app.job_queue.get_jobs_by_name(job_name): job.schedule_removal()
    def _cb(context: ContextTypes.DEFAULT_TYPE):
        context.application.create_task(context.bot.send_message(chat_id=uid, text="🔔 Напоминание: Записать расходы за сегодня?"))
    app.job_queue.run_daily(_cb, dtime(hour=hour, minute=minute, tzinfo=TIMEZONE), name=job_name)

def load_and_schedule_all_reminders(app: Application):
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("SELECT user_id, hour, minute, enabled FROM reminders WHERE enabled=1")
    for uid, h, m, en in c.fetchall():
        schedule_reminder_for_user(app, uid, h, m)
    con.close()

# ---------------- Debts ----------------
def add_debt(uid: int, direction: str, counterparty: str, amount: float, currency: str, note: str) -> int:
    now = int(time.time())
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("""INSERT INTO debts(user_id, direction, counterparty, amount, currency, note, status, created_ts, updated_ts)
                 VALUES(?,?,?,?,?,?, 'open', ?, ?)""", (uid, direction, counterparty, amount, currency, note, now, now))
    debt_id = c.lastrowid
    con.commit(); con.close()
    return debt_id

def list_debts(uid: int, direction: str):
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("""SELECT id, counterparty, amount, currency, note, created_ts
                 FROM debts
                 WHERE user_id=? AND status='open' AND direction=?
                 ORDER BY id DESC""", (uid, direction))
    rows = c.fetchall(); con.close(); return rows

def close_debt(uid: int, debt_id: int) -> bool:
    now = int(time.time())
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("UPDATE debts SET status='closed', updated_ts=? WHERE id=? AND user_id=? AND status='open'", (now, debt_id, uid))
    ok = c.rowcount > 0
    con.commit(); con.close()
    return ok

def reduce_debt(uid: int, debt_id: int, delta: float) -> Optional[Tuple[float,str,str]]:
    now = int(time.time())
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("SELECT amount, currency FROM debts WHERE id=? AND user_id=? AND status='open'", (debt_id, uid))
    row = c.fetchone()
    if not row: con.close(); return None
    amount, currency = float(row[0]), str(row[1])
    new_amount = max(0.0, amount - abs(delta))
    if new_amount <= 0.0:
        c.execute("UPDATE debts SET amount=0, status='closed', updated_ts=? WHERE id=?", (now, debt_id))
        status = "closed"
    else:
        c.execute("UPDATE debts SET amount=?, updated_ts=? WHERE id=?", (new_amount, now, debt_id))
        status = "open"
    con.commit(); con.close()
    return new_amount, currency, status

def debts_list_text(uid: int, direction: str) -> str:
    rows = list_debts(uid, direction)
    title = "Список должников:" if direction == "they_owe" else "Список моих долгов:"
    if not rows: return f"{title}\n— пусто —"
    lines = [title]
    for id_, who, amount, cur, note, created_ts in rows:
        d = datetime.fromtimestamp(int(created_ts), tz=TIMEZONE).strftime("%d.%m.%Y")
        lines.append(f"#{id_} {who} – {fmt_amount(float(amount),cur)} {cur.upper()} ({d})")
    return "\n".join(lines)

def debt_totals(uid: int) -> Tuple[float,float,float,float]:
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    def s(direction: str, cur: str) -> float:
        c.execute("""SELECT COALESCE(SUM(amount),0)
                     FROM debts WHERE user_id=? AND status='open' AND direction=? AND currency=?""",
                  (uid, direction, cur))
        return float(c.fetchone()[0] or 0.0)
    iowe_uzs = s("i_owe","uzs"); iowe_usd = s("i_owe","usd")
    they_uzs = s("they_owe","uzs"); they_usd = s("they_owe","usd")
    con.close()
    return iowe_uzs, iowe_usd, they_узs, they_usd

def balance_with_debts_text(uid: int) -> str:
    uzs, usd = get_balance(uid)
    iowe_uzs, iowe_usd, they_узs, they_usd = debt_totals(uid)
    net_узs = uzs - iowe_узs + they_узs
    net_usd = usd - iowe_usд + they_usд
    lines = [
        f"Баланс без долгов: {fmt_amount(uzs,'uzs')} UZS | {fmt_amount(usd,'usd')} USD",
        f"Я должен: {fmt_amount(iowe_узs,'uzs')} UZS | {fmt_amount(iowe_usd,'usd')} USD",
        f"Мне должны: {fmt_amount(they_узs,'uzs')} UZS | {fmt_amount(they_usd,'usd')} USD",
        f"Чистый баланс: {fmt_amount(net_узs,'uzs')} UZS | {fmt_amount(net_usd,'usd')} USD",
    ]
    return "\n".join(lines)

# ---------------- UI ----------------
async def start(update: Update, _: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Доступ запрещён. Обратитесь к администратору.")
        return
    upsert_seen_user(update.effective_user.id, update.effective_user.first_name or "", update.effective_user.username)
    await update.message.reply_text("Razzakov’s Finance 🤖\nКнопки: «➖ Расход / ➕ Доход / Долги».", reply_markup=MAIN_KB)

def tx_line(ttype: str, amount: float, cur: str, cat: str, note: Optional[str], ts: int) -> str:
    dt = datetime.fromtimestamp(ts, tz=TIMEZONE).strftime("%d.%m %H:%M")
    sign = "➕" if ttype == "income" else "➖"
    return f"{dt} {sign} {fmt_amount(amount,cur)} {cur.upper()} • {cat} • {note or '-'}"

def users_summary_text() -> str:
    if not ALLOWED_USER_IDS: return "Контроль доступа не настроен. Разрешены все пользователи."
    lines = ["Разрешённые пользователи (ID):"]
    for uid in sorted(ALLOWED_USER_IDS): lines.append(f"• {uid}")
    return "\n".join(lines)

async def send_history(update: Update, uid: int, limit: int = 10):
    rows = last_txs(uid, limit)
    if not rows: await update.message.reply_text("История пуста.", reply_markup=MAIN_KB); return
    lines = [f"Последние операции ({len(rows)}):"]
    for id_, ttype, amount, cur, cat, note, ts in rows:
        lines.append(f"#{id_} " + tx_line(ttype, amount, cur, cat, note, ts))
    await update.message.reply_text("\n".join(lines), reply_markup=MAIN_KB)

# ---------------- Router ----------------
async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    txt = (update.message.text or "").strip()
    low = txt.lower()

    if not is_authorized(uid):
        await update.message.reply_text("Доступ запрещён. Обратитесь к администратору.")
        return

    upsert_seen_user(uid, update.effective_user.first_name or "", update.effective_user.username)

    # Debts FSM — handle input steps first, menu last
    debts = context.user_data.get("debts")
    if debts:
        stage = debts.get("stage") or "menu"
        log.info(f"debts stage={stage} txt={txt!r}")

        if txt == BACK_BTN:
            context.user_data.pop("debts", None)
            await update.message.reply_text("Главное меню.", reply_markup=MAIN_KB)
            return

        if stage == "add_counterparty":
            amt = parse_amount(txt)
            if amt is not None:
                cur = detect_currency(txt)
                who = debts.get("counterparty") or extract_counterparty_from_text(txt) or "—"
                add_debt(uid, debts["direction"], who, amt, cur, txt)
                now_s = datetime.now(TIMEZONE).strftime("%d.%m.%Y %H:%M")
                msg = "✅ Долг добавлен:\n" \
                      f"• Сумма: {fmt_amount(amt,cur)} {cur.upper()}\n" \
                      f"• Должник: {who}\n" \
                      f"• Дата: {now_s}"
                await update.message.reply_text(msg, reply_markup=debts_menu_kb())
                # показать обновлённый список соответствующего направления
                await update.message.reply_text(debts_list_text(uid, debts["direction"]), reply_markup=debts_menu_kb())
                debts["stage"] = "menu"
                return
            # если суммы нет — двигаемся к вводу суммы
            debts["counterparty"] = txt
            debts["stage"] = "add_amount"
            await update.message.reply_text("Введите сумму и комментарий (например: 25 000 долг за обед).",
                                            reply_markup=ReplyKeyboardMarkup([[KeyboardButton(BACK_BTN)]], resize_keyboard=True))
            return

        if stage == "add_amount":
            amt = parse_amount(txt)
            if amt is None:
                await update.message.reply_text("Не понял сумму. Пример: 25 000 комментарий.",
                                                reply_markup=ReplyKeyboardMarkup([[KeyboardButton(BACK_BTN)]], resize_keyboard=True))
                return
            cur = detect_currency(txt)
            who = debts.get("counterparty") or extract_counterparty_from_text(txt) or "—"
            add_debt(uid, debts["direction"], who, amt, cur, txt)
            now_s = datetime.now(TIMEZONE).strftime("%d.%m.%Y %H:%M")
            msg = "✅ Долг добавлен:\n" \
                  f"• Сумма: {fmt_amount(amt,cur)} {cur.upper()}\n" \
                  f"• Должник: {who}\n" \
                  f"• Дата: {now_s}"
            await update.message.reply_text(msg, reply_markup=debts_menu_kb())
            await update.message.reply_text(debts_list_text(uid, debts["direction"]), reply_markup=debts_menu_kb())
            debts["stage"] = "menu"
            return

        if stage == "reduce_ask_id":
            m = re.search(r"(\d+)", txt)
            if not m:
                await update.message.reply_text("Отправьте номер долга (например: 12).", reply_markup=debts_menu_kb())
                return
            debts["reduce_id"] = int(m.group(1))
            debts["stage"] = "reduce_ask_amount"
            await update.message.reply_text("На сколько уменьшить? (например: 50 000)",
                                            reply_markup=ReplyKeyboardMarkup([[KeyboardButton(BACK_BTN)]], resize_keyboard=True))
            return

        if stage == "reduce_ask_amount":
            amt = parse_amount(txt)
            if amt is None or amt <= 0:
                await update.message.reply_text("Не понял сумму. Пример: 50 000",
                                                reply_markup=ReplyKeyboardMarkup([[KeyboardButton(BACK_BTN)]], resize_keyboard=True))
                return
            res = reduce_debt(uid, debts.get("reduce_id", 0), amt)
            if not res:
                await update.message.reply_text("Не удалось уменьшить. Проверьте id.", reply_markup=debts_menu_kb())
            else:
                new_amount, cur, status = res
                if status == "closed":
                    await update.message.reply_text("Долг погашен полностью.", reply_markup=debts_menu_kb())
                else:
                    await update.message.reply_text(f"Новый остаток: {fmt_amount(new_amount,cur)} {cur.upper()}",
                                                    reply_markup=debts_menu_kb())
            debts["stage"] = "menu"
            debts.pop("reduce_id", None)
            return

        if stage == "close_ask_id":
            m = re.search(r"(\d+)", txt)
            if not m:
                await update.message.reply_text("Отправьте номер долга (например: 12).", reply_markup=debts_menu_kb())
                return
            ok = close_debt(uid, int(m.group(1)))
            await update.message.reply_text("Долг закрыт." if ok else "Не удалось закрыть. Проверьте id.", reply_markup=debts_menu_kb())
            debts["stage"] = "menu"
            return

        # menu — только в самом конце!
        if stage == "menu":
            await update.message.reply_text("Выберите действие:", reply_markup=debts_menu_kb())
            return

    # Enter debts menu/triggers — разрешаем разные варианты ввода
    if ("мне должны" in low) or (low == "➕ мне должны") or (low == "+ мне должны"):
        context.user_data["debts"] = {"stage":"add_counterparty", "direction":"they_owe"}
        await update.message.reply_text("Кто должен вам? Укажите имя/название. Или сразу: «5000 usd Ahmed».",
                                        reply_markup=ReplyKeyboardMarkup([[KeyboardButton(BACK_BTN)]], resize_keyboard=True))
        return
    if ("я должен" in low) or (low == "➕ я должен") or (low == "+ я должен"):
        context.user_data["debts"] = {"stage":"add_counterparty", "direction":"i_owe"}
        await update.message.reply_text("Кому вы должны? Укажите имя/название. Или сразу: «5000 usd Иван».",
                                        reply_markup=ReplyKeyboardMarkup([[KeyboardButton(BACK_BTN)]], resize_keyboard=True))
        return
    if low == "долги":
        context.user_data["debts"] = {"stage":"menu"}
        await update.message.reply_text("Раздел «Долги».", reply_markup=debts_menu_kb()); return
    if "закрыть долг" in low:
        context.user_data["debts"] = {"stage":"close_ask_id"}
        await update.message.reply_text("Отправьте номер долга для закрытия (например: 12).", reply_markup=debts_menu_kb()); return
    if "уменьшить долг" in low:
        context.user_data["debts"] = {"stage":"reduce_ask_id"}
        await update.message.reply_text("Отправьте номер долга для уменьшения (например: 12).", reply_markup=debts_menu_kb()); return
    if "📜 мне должны" in low or "список должников" in low:
        await update.message.reply_text(debts_list_text(uid, "they_owe"), reply_markup=debts_menu_kb()); return
    if "📜 я должен" in low:
        await update.message.reply_text(debts_list_text(uid, "i_owe"), reply_markup=debts_menu_kb()); return

    # Step-by-step income/expense (если используешь)
    flow = context.user_data.get("flow")
    if flow:
        stage = flow.get("stage"); ttype = flow.get("ttype")
        if txt == BACK_BTN:
            context.user_data.pop("flow", None)
            await update.message.reply_text("Отменено. Главное меню.", reply_markup=MAIN_KB); return
        if stage == "choose_category":
            options = EXPENSE_CATEGORIES if ttype == "expense" else INCOME_CATEGORIES
            if txt in options:
                flow["category"] = txt; flow["stage"] = "await_amount"
                await update.message.reply_text(f"Введи сумму для «{txt}». Можно добавить примечание.", reply_markup=amount_kb())
            else:
                await update.message.reply_text("Выбери категорию на клавиатуре.", reply_markup=categories_kb(ttype))
            return
        if stage == "await_amount":
            amount = parse_amount(txt)
            if amount is None:
                await update.message.reply_text("Не понял сумму. Пример: 25 000 или 25 000 обед.", reply_markup=amount_kb()); return
            cur = detect_currency(txt); cat = flow.get("category") or "Прочее"
            tx_id = add_tx(uid, ttype, amount, cur, cat, txt)
            context.user_data.pop("flow", None)
            await update.message.reply_text(f"{'Доход' if ttype=='income' else 'Расход'}: {fmt_amount(amount,cur)} {cur.upper()} • {cat}\n✓ Сохранено (#{tx_id})", reply_markup=MAIN_KB)
            if ttype == "expense": await maybe_warn_budget(update, uid, cat, cur)
            ai_tip = "Продолжайте вести учёт — вы молодец!"
            await send_and_pin_summary(update, context, uid, ai_tip)
            return

    # Other features
    if "баланс" in low:
        await update.message.reply_text(balance_with_debts_text(uid), reply_markup=MAIN_KB); return
    if "история" in low:
        await send_history(update, uid, 10); return
    if "отчёт" in low or "отчет" in low:
        msg = await month_report_text(uid)
        await update.message.reply_text(msg, reply_markup=MAIN_KB); return
    if "экспорт" in low:
        csv_b, csv_name, xl_b, xl_name = export_month(uid)
        await update.message.reply_document(document=csv_b, filename=csv_name)
        if xl_name: await update.message.reply_document(document=xl_b, filename=xl_name)
        return
    if "pdf" in low:
        pdf = await pdf_report_month(uid)
        if pdf: buf, name = pdf; await update.message.reply_document(document=buf, filename=name)
        else: await update.message.reply_text("Не удалось сформировать PDF сейчас.")
        return
    if "пользовател" in low:
        await update.message.reply_text(users_summary_text(), reply_markup=MAIN_KB); return

    # Free text transaction
    ttype, amount, cur, cat = ai_classify_finance(txt)
    if amount is not None:
        tx_id = add_tx(uid, ttype, amount, cur, cat, txt)
        await update.message.reply_text(f"{'Доход' if ttype=='income' else 'Расход'}: {fmt_amount(amount,cur)} {cur.upper()} • {cat}\n✓ Сохранено (#{tx_id})", reply_markup=MAIN_KB)
        if ttype == "expense": await maybe_warn_budget(update, uid, cat, cur)
        ai_tip = "Продолжайте вести учёт — вы молодец!"
        await send_and_pin_summary(update, context, uid, ai_tip)
        return

    await update.message.reply_text("Принято ✅ Напиши: «такси 25 000», или используй кнопки.", reply_markup=MAIN_KB)

async def unknown_cmd(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Нажми кнопку или напиши траты/доход.", reply_markup=MAIN_KB)

# ---------------- Main ----------------
def main():
    token = DEFAULT_BOT_TOKEN
    Thread(target=start_healthcheck_server, daemon=True).start()

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_cmd))

    try:
        from telegram.ext import JobQueue  # noqa: F401
        schedule_daily_jobs(app)
        load_and_schedule_all_reminders(app)
    except Exception:
        log.info("JobQueue extras not installed; skipping schedules")

    log.info("Starting polling")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
