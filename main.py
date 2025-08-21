import os, re, sqlite3, time, logging, csv, io
from datetime import datetime, timedelta, time as dtime
from typing import Optional, Tuple, List
from zoneinfo import ZoneInfo

import httpx
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

PORT = int(os.environ.get("PORT", "8080"))
DB_PATH = os.environ.get("DB_PATH", "finance.db")
TIMEZONE = ZoneInfo(os.environ.get("TZ", "Asia/Tashkent"))
ALLOWED_USER_IDS = {int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").replace(";", ",").split(",") if x.strip().isdigit()}
ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID")) if os.environ.get("ADMIN_USER_ID", "").isdigit() else None

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s | %(message)s", level=logging.INFO)
log = logging.getLogger("bot")

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
    c.execute("CREATE INDEX IF NOT EXISTS idx_user_ts ON tx(user_id, ts)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_user_id ON tx(user_id, id)")
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
    con.commit(); con.close()
init_db()

MAIN_KB = ReplyKeyboardMarkup(
    [
        [KeyboardButton("➕ Доход"), KeyboardButton("➖ Расход")],
        [KeyboardButton("💰 Баланс"), KeyboardButton("📜 История")],
        [KeyboardButton("📊 Отчёт (месяц)"), KeyboardButton("Экспорт 📂")],
        [KeyboardButton("↩️ Отменить"), KeyboardButton("✏️ Редактировать")],
        [KeyboardButton("Бюджет 💡"), KeyboardButton("Курс валют 💱")],
        [KeyboardButton("🔁 Повторы"), KeyboardButton("📈 Аналитика")],
        [KeyboardButton("📅 Автодаты"), KeyboardButton("🔔 Напоминания")],
        [KeyboardButton("PDF отчёт"), KeyboardButton("👥 Пользователи")],
    ],
    resize_keyboard=True
)

EXPENSE_CATEGORIES = ["Еда","Транспорт","Здоровье","Развлечения","Дом","Детское","Спорт","Прочее"]
INCOME_CATEGORIES = ["Зарплата","Премии","Подарки","Инвестиции","Прочее"]
CATEGORY_KEYWORDS = {
    "Еда": ["еда","продукт","обед","ужин","завтрак","кафе","ресторан","самса","плов","шаурма","пицца"],
    "Транспорт": ["такси","топливо","бензин","газ","метро","автобус","аренда авто","аренда машины"],
    "Зарплата": ["зарплата","оклад","премия","бонус","аванс"],
    "Здоровье": ["аптека","врач","стоматолог","лекар","витамин"],
    "Развлечения": ["кино","игра","cs2","steam","подписка","spotify","netflix"],
    "Дом": ["аренда","квартира","коммунал","электр","интернет","ремонт"],
    "Детское": ["памперс","подгуз","коляска","игруш","детск","дочка","хадиджа"],
    "Спорт": ["зал","спорт","креатин","протеин","гейнер","абонемент"],
    "Подарки": ["подарок","дарил"],
    "Инвестиции": ["акции","инвест","вклад"],
    "Прочее": []
}

def is_authorized(user_id: int) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    return user_id in ALLOWED_USER_IDS

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
    if "$" in tl:
        return "usd"
    words = set(re.findall(r"[a-zа-яё]+", tl))
    if {"usd","доллар","доллара","доллары","долларов","бакс","баксы","дол"} & words:
        return "usd"
    if {"uzs","sum","сум","сумы","сумов"} & words:
        return "uzs"
    return "uzs"

def parse_amount(t: str) -> Optional[float]:
    s = t.replace("\u00A0", " ")
    m = re.findall(r"(?:(?<=\s)|^|(?<=[^\w]))(\d{1,3}(?:[ \u00A0\.,]\d{3})+|\d+)(?:[.,](\d{1,2}))?", s)
    if not m:
        return None
    raw, frac = m[-1]
    num = re.sub(r"[ \u00A0\.,]", "", raw)
    try:
        return float(f"{num}.{frac}") if frac else float(num)
    except ValueError:
        return None

def guess_type(t: str) -> str:
    t = t.lower()
    if any(w in t for w in ["зарплата","премия","бонус","получил","пришло","доход"]):
        return "income"
    if any(w in t for w in ["расход","купил","оплатил","заплатил","потратил","снял"]):
        return "expense"
    return "expense"

def guess_category(t: str, ttype: str) -> str:
    t = t.lower()
    for cat, kws in CATEGORY_KEYWORDS.items():
        if any(k in t for k in kws):
            return cat
    if ttype == "income":
        return "Зарплата" if any(x in t for x in ["зарплат","прем","бонус"]) else "Прочее"
    return "Прочее"

def ai_classify_finance(t: str):
    ttype = guess_type(t)
    amount = parse_amount(t)
    cur = detect_currency(t)
    cat = guess_category(t, ttype)
    return ttype, amount, cur, cat

def add_tx(uid: int, ttype: str, amount: float, cur: str, cat: str, note: str) -> int:
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute(
        "INSERT INTO tx(user_id,ttype,amount,currency,category,note,ts) VALUES(?,?,?,?,?,?,?)",
        (uid, ttype, amount, cur, cat, note, int(time.time()))
    )
    tx_id = c.lastrowid
    con.commit(); con.close()
    return tx_id

def last_txs(uid: int, limit: int = 10):
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("""SELECT id, ttype, amount, currency, category, note, ts
                 FROM tx WHERE user_id=? ORDER BY ts DESC LIMIT ?""",
              (uid, limit))
    rows = c.fetchall(); con.close(); return rows

def get_balance(uid: int) -> Tuple[float,float]:
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    def s(t, cur):
        c.execute("SELECT COALESCE(SUM(amount),0) FROM tx WHERE user_id=? AND ttype=? AND currency=?",
                  (uid, t, cur))
        return c.fetchone()[0]
    bal_uzs = s("income","uzs") - s("expense","uzs")
    bal_usd = s("income","usd") - s("expense","usd")
    con.close()
    return bal_uzs, bal_usd

def month_bounds_now():
    now = datetime.now(TIMEZONE)
    start = datetime(now.year, now.month, 1, 0, 0, 0, tzinfo=TIMEZONE)
    return int(start.timestamp()), int(now.timestamp())

def period_bounds(keyword: str) -> Tuple[int,int,str]:
    now = datetime.now(TIMEZONE)
    key = keyword.lower()
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

def fmt_amount(amount: float, cur: str) -> str:
    if cur == "uzs":
        return f"{int(round(amount)):,}".replace(",", " ")
    return f"{amount:.2f}"

async def month_report_text(uid: int) -> str:
    start_ts, end_ts = month_bounds_now()
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("""SELECT ttype, currency, COALESCE(SUM(amount),0)
                 FROM tx
                 WHERE user_id=? AND ts BETWEEN ? AND ?
                 GROUP BY ttype, currency""", (uid, start_ts, end_ts))
    sums = {(ttype, cur): total for ttype, cur, total in c.fetchall()}
    c.execute("""SELECT category, currency, COALESCE(SUM(amount),0) AS s
                 FROM tx
                 WHERE user_id=? AND ts BETWEEN ? AND ? AND ttype='expense'
                 GROUP BY category, currency
                 ORDER BY s DESC
                 LIMIT 5""", (uid, start_ts, end_ts))
    top = c.fetchall()
    con.close()
    inc_uzs = sums.get(("income","uzs"), 0.0)
    inc_usd = sums.get(("income","usd"), 0.0)
    exp_uzs = sums.get(("expense","uzs"), 0.0)
    exp_usd = sums.get(("expense","usd"), 0.0)
    bal_uzs = inc_uzs - exp_uzs
    bal_usd = inc_usd - exp_usd
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
    if not row:
        con.close(); return None
    tx_id, ttype, amount, currency, category, note = row
    c.execute("DELETE FROM tx WHERE id=?", (tx_id,))
    con.commit(); con.close()
    return row

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
    budgets = get_budgets(uid)
    limit = None
    for cat, cur, lim in budgets:
        if cat == category and cur == currency:
            limit = lim; break
    if limit is None:
        return
    spent = month_expense_sum(uid, category, currency)
    if spent >= limit:
        over = spent - limit
        await update.message.reply_text(
            f"Внимание: бюджет по «{category}» превышен.\n"
            f"Лимит: {fmt_amount(limit,currency)} {currency.upper()}, израсходовано: {fmt_amount(spent,currency)} ({fmt_amount(over,currency)} сверх).",
            reply_markup=MAIN_KB
        )

DOW_MAP = {
    "пн": 0, "пон": 0, "понедельник": 0,
    "вт": 1, "вторник": 1,
    "ср": 2, "среда": 2,
    "чт": 3, "чет": 3, "четверг": 3,
    "пт": 4, "пятница": 4, "птн": 4,
    "сб": 5, "суббота": 5,
    "вс": 6, "воскресенье": 6
}

def add_recurring(uid: int, ttype: str, amount: float, currency: str, category: str, note: str,
                  frequency: str, day_of_week: Optional[int], day_of_month: Optional[int]):
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
    today = datetime.now(TIMEZONE).date()
    date_str = today.isoformat()
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("""SELECT id, user_id, ttype, amount, currency, category, note, frequency, day_of_week, day_of_month, last_applied_date
                 FROM recurring""")
    rows = c.fetchall(); con.close()
    for rec in rows:
        rec_id, uid, ttype, amount, currency, category, note, freq, dow, dom, last_date = rec
        if last_date == date_str:
            continue
        do = False
        if freq == "daily":
            do = True
        elif freq == "weekly" and dow is not None and today.weekday() == int(dow):
            do = True
        elif freq == "monthly" and dom is not None and today.day == int(dom):
            do = True
        if do:
            add_tx(uid, ttype, amount, currency, category, note or f"Recurring {freq}")
            mark_recurring_applied(rec_id, date_str)
            try:
                await app.bot.send_message(chat_id=uid, text=f"Добавлена регулярная операция: {category} {fmt_amount(amount, currency)} {currency.upper()} ({'Доход' if ttype=='income' else 'Расход'})")
            except Exception as e:
                log.warning(f"notify recurring failed for {uid}: {e}")

def schedule_daily_jobs(app: Application):
    if not getattr(app, "job_queue", None):
        log.warning("JobQueue is not available; skipping scheduled jobs")
        return
    app.job_queue.run_daily(
        callback=lambda ctx: ctx.application.create_task(process_recurring_all(ctx.application)),
        time=dtime(hour=9, minute=0, tzinfo=TIMEZONE),
        name="recurring-processor"
    )

def schedule_reminder_for_user(app: Application, uid: int, hour: int, minute: int):
    if not getattr(app, "job_queue", None):
        return
    job_name = f"reminder-{uid}"
    for job in app.job_queue.get_jobs_by_name(job_name):
        job.schedule_removal()
    def _cb(context: ContextTypes.DEFAULT_TYPE):
        context.application.create_task(
            context.bot.send_message(chat_id=uid, text="🔔 Напоминание: Записать расходы за сегодня?")
        )
    app.job_queue.run_daily(_cb, dtime(hour=hour, minute=minute, tzinfo=TIMEZONE), name=job_name)

def load_and_schedule_all_reminders(app: Application):
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("SELECT user_id, hour, minute, enabled FROM reminders WHERE enabled=1")
    for uid, h, m, en in c.fetchall():
        schedule_reminder_for_user(app, uid, h, m)
    con.close()

async def start(update: Update, _: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Доступ запрещён. Обратитесь к администратору.")
        return
    upsert_seen_user(update.effective_user.id, update.effective_user.first_name or "", update.effective_user.username)
    await update.message.reply_text(
        "Razzakov’s Finance 🤖\nПиши: «самса 18 000 сум», «такси 25 000», «зарплата 800$».\nКнопки помогут с доп. функциями.",
        reply_markup=MAIN_KB
    )

def tx_line(ttype: str, amount: float, cur: str, cat: str, note: Optional[str], ts: int) -> str:
    dt = datetime.fromtimestamp(ts, tz=TIMEZONE).strftime("%d.%m %H:%M")
    sign = "➕" if ttype == "income" else "➖"
    return f"{dt} {sign} {fmt_amount(amount,cur)} {cur.upper()} • {cat} • {note or '-'}"

def users_summary_text() -> str:
    if not ALLOWED_USER_IDS:
        return "Контроль доступа не настроен. Разрешены все пользователи."
    lines = ["Разрешённые пользователи (ID):"]
    for uid in sorted(ALLOWED_USER_IDS):
        marker = " ← админ" if ADMIN_USER_ID and uid == ADMIN_USER_ID else ""
        lines.append(f"• {uid}{marker}")
    return "\n".join(lines)

async def send_history(update: Update, uid: int, limit: int = 10):
    rows = last_txs(uid, limit)
    if not rows:
        await update.message.reply_text("История пуста.", reply_markup=MAIN_KB); return
    lines = [f"Последние операции ({len(rows)}):"]
    for id_, ttype, amount, cur, cat, note, ts in rows:
        lines.append(f"#{id_} " + tx_line(ttype, amount, cur, cat, note, ts))
    await update.message.reply_text("\n".join(lines), reply_markup=MAIN_KB)

def export_month(uid: int) -> Tuple[io.BytesIO, str, io.BytesIO, str]:
    start_ts, end_ts = month_bounds_now()
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("""SELECT id, ts, ttype, amount, currency, category, note
                 FROM tx WHERE user_id=? AND ts BETWEEN ? AND ? ORDER BY ts ASC""",
              (uid, start_ts, end_ts))
    rows = c.fetchall(); con.close()
    year_month = datetime.now(TIMEZONE).strftime("%Y_%m")
    csv_buf = io.StringIO()
    writer = csv.writer(csv_buf)
    writer.writerow(["id","datetime","type","amount","currency","category","note"])
    for id_, ts, ttype, amount, cur, cat, note in rows:
        writer.writerow([id_, datetime.fromtimestamp(ts, tz=TIMEZONE).isoformat(sep=" "), ttype, f"{amount:.2f}", cur, cat, note or ""])
    csv_bytes = io.BytesIO(csv_buf.getvalue().encode("utf-8"))
    csv_name = f"transactions_{year_month}.csv"
    try:
        from openpyxl import Workbook
        wb = Workbook(); ws = wb.active; ws.title = "Transactions"
        ws.append(["id","datetime","type","amount","currency","category","note"])
        for id_, ts, ttype, amount, cur, cat, note in rows:
            ws.append([id_, datetime.fromtimestamp(ts, tz=TIMEZONE).strftime("%Y-%m-%d %H:%M:%S"), ttype, amount, cur, cat, note or ""])
        xl_bytes = io.BytesIO(); wb.save(xl_bytes); xl_bytes.seek(0)
        xl_name = f"transactions_{year_month}.xlsx"
    except Exception:
        xl_bytes = io.BytesIO(b""); xl_name = ""
    return csv_bytes, csv_name, xl_bytes, xl_name

async def fetch_usd_uzs_rate() -> Optional[float]:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get("https://api.exchangerate.host/latest?base=USD&symbols=UZS")
            data = r.json()
            return float(data["rates"]["UZS"])
    except Exception as e:
        log.warning(f"rate fetch failed: {e}")
        return None

def sparkline(values: List[float]) -> str:
    if not values:
        return ""
    min_v, max_v = min(values), max(values)
    blocks = "▁▂▃▄▅▆▇█"
    if max_v == min_v:
        return blocks[0] * len(values)
    res = []
    for v in values:
        idx = int((v - min_v) / (max_v - min_v) * (len(blocks) - 1))
        res.append(blocks[idx])
    return "".join(res)

def day_bucket(ts: int) -> str:
    dt = datetime.fromtimestamp(ts, tz=TIMEZONE)
    return dt.strftime("%Y-%m-%d")

def week_bucket(ts: int) -> str:
    dt = datetime.fromtimestamp(ts, tz=TIMEZONE)
    year, week, _ = dt.isocalendar()
    return f"{year}-W{week:02d}"

async def analytics_text(uid: int) -> str:
    now = datetime.now(TIMEZONE)
    start_14 = now - timedelta(days=13)
    start_14_ts = int(datetime(start_14.year, start_14.month, start_14.day, 0, 0, 0, tzinfo=TIMEZONE).timestamp())
    start_8w = now - timedelta(weeks=7)
    start_8w_ts = int(datetime(start_8w.year, start_8w.month, start_8w.day, 0, 0, 0, tzinfo=TIMEZONE).timestamp())
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("""SELECT ts, ttype, amount, currency FROM tx WHERE user_id=? AND ts>=?""", (uid, start_14_ts))
    rows14 = c.fetchall()
    c.execute("""SELECT ts, ttype, amount, currency FROM tx WHERE user_id=? AND ts>=?""", (uid, start_8w_ts))
    rows8w = c.fetchall()
    con.close()
    def series(rows, kind, cur, bucket_fn):
        buckets = {}
        for ts, ttype, amount, currency in rows:
            if ttype != kind or currency != cur:
                continue
            b = bucket_fn(ts)
            buckets[b] = buckets.get(b, 0) + amount
        return buckets
    days = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in reversed(range(14))]
    exp_uzs_d = [series(rows14, "expense", "uzs", day_bucket).get(d, 0.0) for d in days]
    inc_uzs_d = [series(rows14, "income", "uzs", day_bucket).get(d, 0.0) for d in days]
    exp_usd_d = [series(rows14, "expense", "usd", day_bucket).get(d, 0.0) for d in days]
    inc_usd_d = [series(rows14, "income", "usd", day_bucket).get(d, 0.0) for d in days]
    weeks = []
    tmp = now
    seen = set()
    while len(weeks) < 8:
        b = week_bucket(int(tmp.timestamp()))
        if b not in seen:
            weeks.insert(0, b)
            seen.add(b)
        tmp -= timedelta(days=1)
    exp_uzs_w = [series(rows8w, "expense", "uzs", week_bucket).get(w, 0.0) for w in weeks]
    inc_uzs_w = [series(rows8w, "income", "uzs", week_bucket).get(w, 0.0) for w in weeks]
    exp_usd_w = [series(rows8w, "expense", "usd", week_bucket).get(w, 0.0) for w in weeks]
    inc_usd_w = [series(rows8w, "income", "usd", week_bucket).get(w, 0.0) for w in weeks]
    lines = [
        "📈 Аналитика",
        "14 дней (UZS):",
        f"Расход: {sparkline(exp_uzs_d)}",
        f"Доход:  {sparkline(inc_uzs_d)}",
        "14 дней (USD):",
        f"Расход: {sparkline(exp_usd_d)}",
        f"Доход:  {sparkline(inc_usd_d)}",
        "8 недель (UZS):",
        f"Расход: {sparkline(exp_uzs_w)}",
        f"Доход:  {sparkline(inc_uzs_w)}",
        "8 недель (USD):",
        f"Расход: {sparkline(exp_usd_w)}",
        f"Доход:  {sparkline(inc_usd_w)}",
    ]
    return "\n".join(lines)

async def pdf_report_month(uid: int) -> Optional[Tuple[io.BytesIO, str]]:
    try:
        from reportlab.pdfgen import canvas
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        font_path = "/tmp/DejaVuSans.ttf"
        if not os.path.exists(font_path):
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get("https://github.com/dejavu-fonts/dejavu-fonts/raw/version_2_37/ttf/DejaVuSans.ttf")
                r.raise_for_status()
                with open(font_path, "wb") as f:
                    f.write(r.content)
        pdfmetrics.registerFont(TTFont("DejaVuSans", font_path))
        start_ts, end_ts = month_bounds_now()
        con = sqlite3.connect(DB_PATH); c = con.cursor()
        c.execute("""SELECT ttype, currency, COALESCE(SUM(amount),0)
                     FROM tx WHERE user_id=? AND ts BETWEEN ? AND ? GROUP BY ttype, currency""",
                  (uid, start_ts, end_ts))
        sums = {(t,c2): s for t,c2,s in c.fetchall()}
        c.execute("""SELECT id, ts, ttype, amount, currency, category, note
                     FROM tx WHERE user_id=? AND ts BETWEEN ? AND ? ORDER BY ts ASC""",
                  (uid, start_ts, end_ts))
        rows = c.fetchall(); con.close()
        inc_uzs = sums.get(("income","uzs"),0.0); inc_usd = sums.get(("income","usd"),0.0)
        exp_uzs = sums.get(("expense","uzs"),0.0); exp_usd = sums.get(("expense","usd"),0.0)
        buf = io.BytesIO()
        cnv = canvas.Canvas(buf, pagesize=A4)
        cnv.setFont("DejaVuSans", 12)
        w, h = A4
        y = h - 40
        cnv.drawString(40, y, "Отчёт за месяц"); y -= 20
        cnv.drawString(40, y, f"Доход: UZS {fmt_amount(inc_uzs,'uzs')} | USD {fmt_amount(inc_usd,'usd')}"); y -= 18
        cnv.drawString(40, y, f"Расход: UZS {fmt_amount(exp_uzs,'uzs')} | USD {fmt_amount(exp_usd,'usd')}"); y -= 18
        cnv.drawString(40, y, f"Баланс: UZS {fmt_amount(inc_uzs-exp_uzs,'uzs')} | USD {fmt_amount(inc_usd-exp_usd,'usd')}"); y -= 28
        cnv.drawString(40, y, "Операции:"); y -= 18
        cnv.setFont("DejaVuSans", 10)
        for id_, ts, ttype, amount, cur, cat, note in rows:
            line = f"#{id_} {datetime.fromtimestamp(ts, tz=TIMEZONE).strftime('%d.%m %H:%M')} • {'Доход' if ttype=='income' else 'Расход'} • {fmt_amount(amount,cur)} {cur.upper()} • {cat} • {note or ''}"
            cnv.drawString(40, y, line[:110])
            y -= 14
            if y < 60:
                cnv.showPage()
                cnv.setFont("DejaVuSans", 10)
                y = h - 40
        cnv.save()
        buf.seek(0)
        name = f"report_{datetime.now(TIMEZONE).strftime('%Y_%m')}.pdf"
        return buf, name
    except Exception as e:
        log.warning(f"pdf failed: {e}")
        return None

def parse_edit_command(txt: str) -> Optional[Tuple[int, Optional[float], Optional[str]]]:
    m_id = re.search(r"\b(id|#)\s*=?\s*(\d+)", txt, re.IGNORECASE)
    if not m_id:
        return None
    tx_id = int(m_id.group(2))
    new_amount = None
    new_category = None
    m_amt = re.search(r"(amount|сумма)\s*=?\s*([\d \u00A0\.,]+)", txt, re.IGNORECASE)
    if m_amt:
        new_amount = parse_amount(m_amt.group(0))
    m_cat = re.search(r"(category|категор(ия|ию|ии))\s*=?\s*([A-Za-zА-Яа-яЁё]+)", txt, re.IGNORECASE)
    if m_cat:
        new_category = m_cat.group(4).capitalize()
    return (tx_id, new_amount, new_category)

def update_tx(uid: int, tx_id: int, new_amount: Optional[float], new_category: Optional[str]) -> bool:
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("SELECT id FROM tx WHERE id=? AND user_id=?", (tx_id, uid))
    if not c.fetchone():
        con.close(); return False
    if new_amount is not None and new_category is not None:
        c.execute("UPDATE tx SET amount=?, category=? WHERE id=?", (new_amount, new_category, tx_id))
    elif new_amount is not None:
        c.execute("UPDATE tx SET amount=? WHERE id=?", (new_amount, tx_id))
    elif new_category is not None:
        c.execute("UPDATE tx SET category=? WHERE id=?", (new_category, tx_id))
    else:
        con.close(); return False
    con.commit(); con.close(); return True

async def handle_budgets(update: Update, uid: int, txt: str):
    m = re.search(r"бюджет\s+([A-Za-zА-Яа-яЁё]+)\s+([\d \u00A0\.,]+)\s*(\w+)?", txt, re.IGNORECASE)
    if m:
        category = m.group(1).capitalize()
        amount = parse_amount(m.group(0)) or 0.0
        cur = detect_currency(txt)
        set_budget(uid, category, cur, amount)
        await update.message.reply_text(f"Бюджет сохранён: {category} = {fmt_amount(amount, cur)} {cur.upper()} / месяц")
    else:
        buds = get_budgets(uid)
        if not buds:
            await update.message.reply_text("Бюджеты не заданы. Пример: «Бюджет Еда 1 500 000 сум»")
        else:
            lines = ["Текущие бюджеты (месяц):"]
            for cat, cur, lim in buds:
                spent = month_expense_sum(uid, cat, cur)
                lines.append(f"• {cat}: {fmt_amount(spent,cur)} / {fmt_amount(lim,cur)} {cur.upper()}")
            await update.message.reply_text("\n".join(lines))

async def handle_recurring(update: Update, uid: int, txt: str):
    low = txt.lower()
    if "добав" in low or "созда" in low or "повтор:" in low:
        ttype, amount, cur, cat = ai_classify_finance(txt)
        freq = None; dow = None; dom = None
        if "ежеднев" in low:
            freq = "daily"
        elif "еженед" in low:
            freq = "weekly"
            for k, v in DOW_MAP.items():
                if re.search(rf"\b{k}\b", low):
                    dow = v; break
            if dow is None:
                dow = 0
        elif "ежемес" in low:
            freq = "monthly"
            m = re.search(r"\b(\d{1,2})\b", low)
            dom = max(1, min(28, int(m.group(1)))) if m else 1
        if not (amount and freq):
            await update.message.reply_text("Пример: «Повтор: аренда 2 000 000 сум ежемесячно 5» или «Повтор: зарплата 800$ ежемесячно 1».")
            return
        add_recurring(uid, ttype, amount, cur, cat, txt, freq, dow, dom)
        await update.message.reply_text("Повтор добавлен.")
    else:
        rows = list_recurring(uid)
        if not rows:
            await update.message.reply_text("Повторов нет. Пример добавления: «Повтор: аренда 2 000 000 сум ежемесячно 5».")
            return
        lines = ["Текущие повторы:"]
        for id_, ttype, amount, cur, cat, note, freq, dow, dom in rows:
            extra = ""
            if freq == "weekly": extra = f" (день недели: {dow})"
            if freq == "monthly": extra = f" (день месяца: {dom})"
            lines.append(f"#{id_} {'Доход' if ttype=='income' else 'Расход'} • {fmt_amount(amount,cur)} {cur.upper()} • {cat} • {freq}{extra}")
        await update.message.reply_text("\n".join(lines))

async def handle_autodates(update: Update):
    kb = ReplyKeyboardMarkup([[KeyboardButton("Сегодня")],[KeyboardButton("Вчера")],[KeyboardButton("Неделя")]], resize_keyboard=True, one_time_keyboard=True, selective=True)
    await update.message.reply_text("Выберите период:", reply_markup=kb)

async def period_summary_text(uid: int, label: str) -> str:
    start_ts, end_ts, title = period_bounds(label)
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("""SELECT ttype, currency, COALESCE(SUM(amount),0) FROM tx
                 WHERE user_id=? AND ts BETWEEN ? AND ? GROUP BY ttype, currency""",
              (uid, start_ts, end_ts))
    sums = {(t,c2): s for t,c2,s in c.fetchall()}
    c.execute("""SELECT category, currency, COALESCE(SUM(amount),0) AS s FROM tx
                 WHERE user_id=? AND ts BETWEEN ? AND ? AND ttype='expense'
                 GROUP BY category, currency ORDER BY s DESC LIMIT 5""",
              (uid, start_ts, end_ts))
    top = c.fetchall()
    con.close()
    inc_uzs = sums.get(("income","uzs"),0.0); inc_usd = sums.get(("income","usd"),0.0)
    exp_uzs = sums.get(("expense","uzs"),0.0); exp_usd = sums.get(("expense","usd"),0.0)
    lines = [
        f"Итоги {title}:",
        f"• Доход UZS: {fmt_amount(inc_uzs,'uzs')} | USD: {fmt_amount(inc_usd,'usd')}",
        f"• Расход UZS: {fmt_amount(exp_uzs,'uzs')} | USD: {fmt_amount(exp_usd,'usd')}",
    ]
    if top:
        lines.append("Топ расходов:")
        for cat, cur, s in top:
            lines.append(f"  - {cat}: {fmt_amount(s,cur)} {cur.upper()}")
    return "\n".join(lines)

async def handle_reminders(update: Update, app: Application, uid: int, txt: str):
    low = txt.lower()
    if re.search(r"\b(\d{1,2}):(\d{2})\b", low):
        h, m = re.search(r"\b(\d{1,2}):(\d{2})\b", low).groups()
        h, m = max(0, min(23, int(h))), max(0, min(59, int(m)))
        con = sqlite3.connect(DB_PATH); c = con.cursor()
        c.execute("""INSERT INTO reminders(user_id, hour, minute, enabled)
                     VALUES(?,?,?,1)
                     ON CONFLICT(user_id) DO UPDATE SET hour=excluded.hour, minute=excluded.minute, enabled=1""",
                  (uid, h, m))
        con.commit(); con.close()
        schedule_reminder_for_user(app, uid, h, m)
        await update.message.reply_text(f"Напоминание включено: {h:02d}:{m:02d}")
    elif "выкл" in low or "off" in low:
        con = sqlite3.connect(DB_PATH); c = con.cursor()
        c.execute("""INSERT INTO reminders(user_id, hour, minute, enabled)
                     VALUES(?,21,0,0)
                     ON CONFLICT(user_id) DO UPDATE SET enabled=0""", (uid,))
        con.commit(); con.close()
        if getattr(app, "job_queue", None):
            for job in app.job_queue.get_jobs_by_name(f"reminder-{uid}"):
                job.schedule_removal()
        await update.message.reply_text("Напоминание выключено.")
    else:
        con = sqlite3.connect(DB_PATH); c = con.cursor()
        c.execute("SELECT hour, minute, enabled FROM reminders WHERE user_id=?", (uid,))
        row = c.fetchone(); con.close()
        if not row or row[2] == 0:
            await update.message.reply_text("Напоминаний нет. Установите время: «Напоминания 21:30», выключить: «Напоминания выкл».")
        else:
            await update.message.reply_text(f"Текущее напоминание: {row[0]:02d}:{row[1]:02d}. Измените сообщением «Напоминания HH:MM» или выключите «Напоминания выкл».")

async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    txt = (update.message.text or "").strip()
    low = txt.lower()
    if not is_authorized(uid):
        await update.message.reply_text("Доступ запрещён. Обратитесь к администратору.")
        return
    upsert_seen_user(uid, update.effective_user.first_name or "", update.effective_user.username)
    if "баланс" in low:
        uzs, usd = get_balance(uid)
        await update.message.reply_text(f"Баланс:\n• UZS: {fmt_amount(uzs,'uzs')}\n• USD: {fmt_amount(usd,'usd')}", reply_markup=MAIN_KB)
        return
    if "история" in low:
        await send_history(update, uid, 10); return
    if "отчёт" in low or "отчет" in low:
        msg = await month_report_text(uid)
        await update.message.reply_text(msg, reply_markup=MAIN_KB); return
    if "экспорт" in low:
        csv_b, csv_name, xl_b, xl_name = export_month(uid)
        await update.message.reply_document(document=csv_b, filename=csv_name)
        if xl_name:
            await update.message.reply_document(document=xl_b, filename=xl_name)
        return
    if "pdf" in low:
        pdf = await pdf_report_month(uid)
        if pdf:
            buf, name = pdf
            await update.message.reply_document(document=buf, filename=name)
        else:
            await update.message.reply_text("Не удалось сформировать PDF сейчас.")
        return
    if "отмен" in low:
        row = undo_last(uid)
        if not row:
            await update.message.reply_text("Нечего отменять.")
        else:
            _, ttype, amount, cur, cat, note = row
            await update.message.reply_text(f"Удалено: {fmt_amount(amount,cur)} {cur.upper()} • {cat}")
        return
    if "пользовател" in low:
        await update.message.reply_text(users_summary_text(), reply_markup=MAIN_KB); return
    if "курс" in low:
        rate = await fetch_usd_uzs_rate()
        uzs, usd = get_balance(uid)
        lines = []
        if rate:
            total_uzs = uzs + usd * rate
            total_usd = usd + (uzs / rate)
            lines.append(f"Курс: 1 USD = {rate:,.0f} UZS".replace(",", " "))
            lines.append(f"Сводный баланс: ≈ {fmt_amount(total_uzs,'uzs')} UZS | ≈ {total_usd:.2f} USD")
        else:
            lines.append("Не удалось получить курс. Показываю локальный баланс.")
        lines.append(f"Баланс: UZS {fmt_amount(uzs,'uzs')} | USD {fmt_amount(usd,'usd')}")
        await update.message.reply_text("\n".join(lines)); return
    if "бюджет" in low:
        await handle_budgets(update, uid, txt); return
    if "редакт" in low:
        await send_history(update, uid, 5)
        await update.message.reply_text("Отправьте команду вида: «id=123 сумма=25000» или «id 123 категория=Еда».")
        return
    if "id" in low or low.startswith("#"):
        cmd = parse_edit_command(txt)
        if cmd:
            tx_id, new_amount, new_cat = cmd
            ok = update_tx(uid, tx_id, new_amount, new_cat)
            await update.message.reply_text("Обновлено." if ok else "Не удалось обновить. Проверьте id.")
            return
    if "повтор" in low:
        await handle_recurring(update, uid, txt); return
    if "аналит" in low:
        msg = await analytics_text(uid)
        await update.message.reply_text(msg); return
    if "автодат" in low:
        await handle_autodates(update); return
    if low in ("сегодня","вчера","неделя","на этой неделе"):
        msg = await period_summary_text(uid, low)
        await update.message.reply_text(msg); return
    if "напомин" in low:
        await handle_reminders(update, context.application, uid, txt); return
    ttype, amount, cur, cat = ai_classify_finance(txt)
    if amount is not None:
        tx_id = add_tx(uid, ttype, amount, cur, cat, txt)
        await update.message.reply_text(f"{'Доход' if ttype=='income' else 'Расход'}: {fmt_amount(amount,cur)} {cur.upper()} • {cat}\n✓ Сохранено (#{tx_id})", reply_markup=MAIN_KB)
        if ttype == "expense":
            await maybe_warn_budget(update, uid, cat, cur)
        return
    await update.message.reply_text("Принято ✅ Напиши: «такси 25 000», «зарплата 800$».", reply_markup=MAIN_KB)

async def unknown_cmd(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Нажми кнопку или напиши траты/доход.", reply_markup=MAIN_KB)

def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN is not set in environment variables")
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_cmd))
    schedule_daily_jobs(app)
    load_and_schedule_all_reminders(app)
    log.info("Starting polling")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
