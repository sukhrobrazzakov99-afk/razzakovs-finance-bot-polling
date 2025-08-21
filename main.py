import os, re, sqlite3, time, logging
from datetime import datetime
from typing import Optional
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

PORT = int(os.environ.get("PORT", "8080"))
DB_PATH = os.environ.get("DB_PATH", "finance.db")

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
	con.commit(); con.close()
init_db()

MAIN_KB = ReplyKeyboardMarkup(
	[[KeyboardButton("➕ Доход"), KeyboardButton("➖ Расход")],
	 [KeyboardButton("💰 Баланс"), KeyboardButton("📜 История")],
	 [KeyboardButton("📊 Отчёт (месяц)")]],
	resize_keyboard=True
)

CATEGORY_KEYWORDS = {
	"Еда": ["еда", "продукт", "обед", "ужин", "завтрак", "кафе", "ресторан", "самса", "плов", "шаурма", "пицца"],
	"Транспорт": ["такси", "топливо", "бензин", "газ", "метро", "автобус", "аренда авто", "аренда машины"],
	"Зарплата": ["зарплата", "оклад", "премия", "бонус", "аванс"],
	"Здоровье": ["аптека", "врач", "стоматолог", "лекар", "витамин"],
	"Развлечения": ["кино", "игра", "cs2", "steam", "подписка", "spotify", "netflix"],
	"Дом": ["аренда", "квартира", "коммунал", "электр", "интернет", "ремонт"],
	"Детское": ["памперс", "подгуз", "коляска", "игруш", "детск", "дочка", "хадиджа"],
	"Спорт": ["зал", "спорт", "креатин", "протеин", "гейнер", "абонемент"],
	"Прочее": []
}

def detect_currency(t: str) -> str:
	tl = t.lower()
	if "$" in tl:
		return "usd"
	words = set(re.findall(r"[a-zа-яё]+", tl))
	if {"usd", "доллар", "доллара", "доллары", "долларов"} & words:
		return "usd"
	if {"uzs", "sum", "сум", "сумы", "сумов"} & words:
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
	if any(w in t for w in ["зарплата", "премия", "бонус", "получил", "пришло", "доход"]):
		return "income"
	if any(w in t for w in ["расход", "купил", "оплатил", "заплатил", "потратил", "снял"]):
		return "expense"
	return "expense"

def guess_category(t: str) -> str:
	t = t.lower()
	for cat, kws in CATEGORY_KEYWORDS.items():
		if any(k in t for k in kws):
			return cat
	if any(x in t for x in ["зарплат", "прем", "бонус"]):
		return "Зарплата"
	return "Прочее"

def ai_classify_finance(t: str):
	return guess_type(t), parse_amount(t), detect_currency(t), guess_category(t)

def add_tx(uid: int, ttype: str, amount: float, cur: str, cat: str, note: str):
	con = sqlite3.connect(DB_PATH); c = con.cursor()
	c.execute(
		"INSERT INTO tx(user_id,ttype,amount,currency,category,note,ts) VALUES(?,?,?,?,?,?,?)",
		(uid, ttype, amount, cur, cat, note, int(time.time()))
	)
	con.commit(); con.close()

def last_txs(uid: int, limit: int = 10):
	con = sqlite3.connect(DB_PATH); c = con.cursor()
	c.execute("""SELECT ttype,amount,currency,category,note,ts
	             FROM tx WHERE user_id=? ORDER BY ts DESC LIMIT ?""",
	          (uid, limit))
	rows = c.fetchall(); con.close(); return rows

def get_balance(uid: int):
	con = sqlite3.connect(DB_PATH); c = con.cursor()
	def s(t, cur):
		c.execute("SELECT COALESCE(SUM(amount),0) FROM tx WHERE user_id=? AND ttype=? AND currency=?",
		          (uid, t, cur))
		return c.fetchone()[0]
	bal_uzs = s("income", "uzs") - s("expense", "uzs")
	bal_usd = s("income", "usd") - s("expense", "usd")
	con.close()
	return bal_uzs, bal_usd

def month_bounds_now():
	now = datetime.now()
	start = datetime(now.year, now.month, 1, 0, 0, 0)
	return int(start.timestamp()), int(now.timestamp())

def fmt_amount(amount: float, cur: str) -> str:
	if cur == "uzs":
		return f"{int(round(amount)):,}".replace(",", " ")
	return f"{amount:.2f}"

def month_report_text(uid: int) -> str:
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

	inc_uzs = sums.get(("income", "uzs"), 0.0)
	inc_usd = sums.get(("income", "usd"), 0.0)
	exp_uzs = sums.get(("expense", "uzs"), 0.0)
	exp_usd = sums.get(("expense", "usd"), 0.0)
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

async def start(update: Update, _: ContextTypes.DEFAULT_TYPE):
	await update.message.reply_text(
		"Razzakov’s Finance 🤖\nПиши: «самса 18 000 сум», «такси 25 000», «зарплата 800$».",
		reply_markup=MAIN_KB
	)

async def text_router(update: Update, _: ContextTypes.DEFAULT_TYPE):
	uid = update.effective_user.id
	txt = (update.message.text or "").strip()
	low = txt.lower()

	if "баланс" in low:
		uzs, usd = get_balance(uid)
		await update.message.reply_text(
			"Баланс:\n• UZS: " + fmt_amount(uzs, "uzs") + f"\n• USD: {fmt_amount(usd,'usd')}",
			reply_markup=MAIN_KB
		)
		return

	if "история" in low:
		rows = last_txs(uid, 10)
		if not rows:
			await update.message.reply_text("История пуста.", reply_markup=MAIN_KB)
			return
		lines = []
		for ttype, amount, cur, cat, note, ts in rows:
			dt = datetime.fromtimestamp(ts).strftime("%d.%m %H:%M")
			sign = "➕" if ttype == "income" else "➖"
			lines.append(f"{dt} {sign} {fmt_amount(amount,cur)} {cur.upper()} • {cat} • {note or '-'}")
		await update.message.reply_text("Последние операции:\n" + "\n".join(lines), reply_markup=MAIN_KB)
		return

	if "отчёт" in low or "отчет" in low:
		await update.message.reply_text(month_report_text(uid), reply_markup=MAIN_KB)
		return

	ttype, amount, cur, cat = ai_classify_finance(txt)
	if amount is not None:
		add_tx(uid, ttype, amount, cur, cat, txt)
		await update.message.reply_text(
			f"{'Доход' if ttype=='income' else 'Расход'}: {fmt_amount(amount,cur)} {cur.upper()} • {cat}\n✓ Сохранено",
			reply_markup=MAIN_KB
		)
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

	log.info("Starting polling")
	app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
	main()

