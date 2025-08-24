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

DEFAULT_BOT_TOKEN = os.environ.get("BOT_TOKEN", "7611168200:AAH_NPSecM5hrqPKindVLiQy4zkPIauqmTc")

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
	c.execute("""CREATE TABLE IF NOT EXISTS budgets(
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		user_id INTEGER NOT NULL,
		category TEXT NOT NULL,
		currency TEXT NOT NULL,
		limit_amount REAL NOT NULL,
		period TEXT NOT NULL DEFAULT 'month',
		created_ts INTEGER NOT NULL
	)""")
	con.commit(); con.close()
init_db()

BACK_BTN = "⬅️ Назад"

MAIN_KB = ReplyKeyboardMarkup(
	[
		[KeyboardButton("➕ Доход"), KeyboardButton("➖ Расход")],
		[KeyboardButton("💰 Баланс"), KeyboardButton("📜 История")],
		[KeyboardButton("Долги")],
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

def is_authorized(user_id: int) -> bool:
	return True if not ALLOWED_USER_IDS else user_id in ALLOWED_USER_IDS

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

CURRENCY_WORDS = {"usd","uzs","sum","сум","сумы","сумов","доллар","доллара","доллары","долларов","бакс","баксы","дол"}
def extract_counterparty_from_text(t: str) -> str:
	words = re.findall(r"[A-Za-zА-Яа-яЁё]+", t)
	names = [w for w in words if w.lower() not in CURRENCY_WORDS]
	return " ".join(names[-2:]) if names else ""

def fmt_amount(amount: float, cur: str) -> str:
	if cur == "uzs":
		return f"{int(round(amount)):,}".replace(",", " ")
	return f"{amount:.2f}"

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
		return c.fetchone()[0]
	bal_uzs = s("income","uzs") - s("expense","uzs")
	bal_usd = s("income","usd") - s("expense","usd")
	con.close()
	return bal_uzs, bal_usd

# Debts
def add_debt(uid: int, direction: str, counterparty: str, amount: float, currency: str, note: str) -> int:
	now = int(time.time())
	con = sqlite3.connect(DB_PATH); c = con.cursor()
	c.execute("""INSERT INTO debts(user_id, direction, counterparty, amount, currency, note, status, created_ts, updated_ts)
	             VALUES(?,?,?,?,?,?, 'open', ?, ?)""",
	          (uid, direction, counterparty, amount, currency, note, now, now))
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
	c.execute("UPDATE debts SET status='closed', updated_ts=? WHERE id=? AND user_id=? AND status='open'",
	          (now, debt_id, uid))
	ok = c.rowcount > 0
	con.commit(); con.close()
	return ok

def reduce_debt(uid: int, debt_id: int, delta: float) -> Optional[Tuple[float,str,str]]:
	now = int(time.time())
	con = sqlite3.connect(DB_PATH); c = con.cursor()
	c.execute("SELECT amount, currency FROM debts WHERE id=? AND user_id=? AND status='open'", (debt_id, uid))
	row = c.fetchone()
	if not row:
		con.close(); return None
	amount, currency = row
	new_amount = max(0.0, amount - abs(delta))
	if new_amount <= 0.0:
		c.execute("UPDATE debts SET amount=0, status='closed', updated_ts=? WHERE id=?", (now, debt_id))
		status = "closed"
	else:
		c.execute("UPDATE debts SET amount=?, updated_ts=? WHERE id=?", (new_amount, now, debt_id))
		status = "open"
	con.commit(); con.close()
	return new_amount, currency, status

def debt_totals(uid: int) -> Tuple[float,float,float,float]:
	con = sqlite3.connect(DB_PATH); c = con.cursor()
	def s(direction: str, cur: str):
		c.execute("""SELECT COALESCE(SUM(amount),0)
		             FROM debts WHERE user_id=? AND status='open' AND direction=? AND currency=?""",
		          (uid, direction, cur))
		return c.fetchone()[0] or 0.0
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

async def start(update: Update, _: ContextTypes.DEFAULT_TYPE):
	if not is_authorized(update.effective_user.id):
		await update.message.reply_text("Доступ запрещён.")
		return
	await update.message.reply_text("Финансы 🤖\nКнопки: «➖ Расход / ➕ Доход / Долги».", reply_markup=MAIN_KB)

def tx_line(ttype: str, amount: float, cur: str, cat: str, note: Optional[str], ts: int) -> str:
	dt = datetime.fromtimestamp(ts, tz=TIMEZONE).strftime("%d.%m %H:%M")
	sign = "➕" if ttype == "income" else "➖"
	return f"{dt} {sign} {fmt_amount(amount,cur)} {cur.upper()} • {cat} • {note or '-'}"

async def send_history(update: Update, uid: int, limit: int = 10):
	rows = last_txs(uid, limit)
	if not rows:
		await update.message.reply_text("История пуста.", reply_markup=MAIN_KB); return
	lines = [f"Последние операции ({len(rows)}):"]
	for id_, ttype, amount, cur, cat, note, ts in rows:
		lines.append(f"#{id_} " + tx_line(ttype, amount, cur, cat, note, ts))
	await update.message.reply_text("\n".join(lines), reply_markup=MAIN_KB)

def debts_list_pretty(uid: int, direction: str) -> str:
	rows = list_debts(uid, direction)
	title = "Список должников:" if direction == "they_owe" else "Список моих долгов:"
	if not rows:
		return f"{title}\n— пусто —"
	lines = [title]
	for id_, who, amount, cur, note, created_ts in rows:
		d = datetime.fromtimestamp(created_ts, tz=TIMEZONE).strftime("%d.%m.%Y")
		lines.append(f"#{id_} {who} – {fmt_amount(amount,cur)} {cur.upper()} ({d})")
	return "\n".join(lines)

async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
	uid = update.effective_user.id
	txt = (update.message.text or "").strip()
	low = txt.lower()

	if not is_authorized(uid):
		await update.message.reply_text("Доступ запрещён.")
		return

	# Debts flow
	debts = context.user_data.get("debts")
	if debts:
		stage = debts.get("stage")
		if txt == BACK_BTN:
			context.user_data.pop("debts", None)
			await update.message.reply_text("Главное меню.", reply_markup=MAIN_KB)
			return
		if stage == "menu":
			await update.message.reply_text("Выберите действие:", reply_markup=debts_menu_kb())
			return
		if stage == "add_counterparty":
			amt = parse_amount(txt)
			if amt is not None:
				cur = detect_currency(txt)
				who = extract_counterparty_from_text(txt) or debts.get("counterparty") or "—"
				debt_id = add_debt(uid, debts["direction"], who, amt, cur, txt)
				now_s = datetime.now(TIMEZONE).strftime("%d.%m.%Y %H:%M")
				msg = "✅ Долг добавлен:\n" \
				      f"• Сумма: {fmt_amount(amt,cur)} {cur.upper()}\n" \
				      f"• Должник: {who}\n" \
				      f"• Дата: {now_s}"
				await update.message.reply_text(msg, reply_markup=debts_menu_kb())
				debts["stage"] = "menu"
			else:
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
			debt_id = add_debt(uid, debts["direction"], who, amt, cur, txt)
			now_s = datetime.now(TIMEZONE).strftime("%d.%m.%Y %H:%M")
			msg = "✅ Долг добавлен:\n" \
			      f"• Сумма: {fmt_amount(amt,cur)} {cur.upper()}\n" \
			      f"• Должник: {who}\n" \
			      f"• Дата: {now_s}"
			await update.message.reply_text(msg, reply_markup=debts_menu_kb())
			debts["stage"] = "menu"
			return
		if stage == "close_ask_id":
			m = re.search(r"(\d+)", txt)
			if not m:
				await update.message.reply_text("Отправьте номер долга (например: 12).", reply_markup=debts_menu_kb()); return
			ok = close_debt(uid, int(m.group(1)))
			await update.message.reply_text("Долг закрыт." if ok else "Не удалось закрыть. Проверьте id.", reply_markup=debts_menu_kb())
			debts["stage"] = "menu"; return
		if stage == "reduce_ask_id":
			m = re.search(r"(\d+)", txt)
			if not m:
				await update.message.reply_text("Отправьте номер долга (например: 12).", reply_markup=debts_menu_kb()); return
			debts["reduce_id"] = int(m.group(1))
			debts["stage"] = "reduce_ask_amount"
			await update.message.reply_text("На сколько уменьшить? (например: 50 000)",
			                                reply_markup=ReplyKeyboardMarkup([[KeyboardButton(BACK_BTN)]], resize_keyboard=True))
			return
		if stage == "reduce_ask_amount":
			amt = parse_amount(txt)
			if amt is None or amt <= 0:
				await update.message.reply_text("Не понял сумму. Пример: 50 000",
				                                reply_markup=ReplyKeyboardMarkup([[KeyboardButton(BACK_BTN)]], resize_keyboard=True)); return
			res = reduce_debt(uid, debts["reduce_id"], amt)
			if not res:
				await update.message.reply_text("Не удалось уменьшить. Проверьте id.", reply_markup=debts_menu_kb())
			else:
				new_amount, cur, status = res
				await update.message.reply_text(
					"Долг погашен полностью." if status=="closed" else f"Новый остаток: {fmt_amount(new_amount,cur)} {cur.upper()}",
					reply_markup=debts_menu_kb()
				)
			debts["stage"] = "menu"; debts.pop("reduce_id", None)
		 return

	# Enter debts menu
	if low == "долги":
		context.user_data["debts"] = {"stage":"menu"}
		await update.message.reply_text("Раздел «Долги».", reply_markup=debts_menu_kb()); return
	if low == "➕ я должен":
		context.user_data["debts"] = {"stage":"add_counterparty", "direction":"i_owe"}
		await update.message.reply_text("Кому вы должны? Или сразу: «5000 usd Иван».",
		                                reply_markup=ReplyKeyboardMarkup([[KeyboardButton(BACK_BTN)]], resize_keyboard=True)); return
	if low == "➕ мне должны":
		context.user_data["debts"] = {"stage":"add_counterparty", "direction":"they_owe"}
		await update.message.reply_text("Кто должен вам? Или сразу: «5000 usd Roni».",
		                                reply_markup=ReplyKeyboardMarkup([[KeyboardButton(BACK_BTN)]], resize_keyboard=True)); return
	if low == "📜 я должен":
		await update.message.reply_text(debts_list_pretty(uid, "i_owe"), reply_markup=debts_menu_kb()); return
	if low == "📜 мне должны":
		await update.message.reply_text(debts_list_pretty(uid, "they_owe"), reply_markup=debts_menu_kb()); return
	if low == "✖️ закрыть долг":
		context.user_data["debts"] = {"stage":"close_ask_id"}
		await update.message.reply_text("Отправьте номер долга для закрытия (например: 12).", reply_markup=debts_menu_kb()); return
	if low == "➖ уменьшить долг":
		context.user_data["debts"] = {"stage":"reduce_ask_id"}
		await update.message.reply_text("Отправьте номер долга для уменьшения (например: 12).", reply_markup=debts_menu_kb()); return

	# Basics
	if "баланс" in low:
		await update.message.reply_text(balance_with_debts_text(uid), reply_markup=MAIN_KB); return
	if "история" in low:
		await send_history(update, uid, 10); return

	# Free text tx
	ttype, amount, cur, cat = ai_classify_finance(txt)
	if amount is not None:
		tx_id = add_tx(uid, ttype, amount, cur, cat, txt)
		await update.message.reply_text(f"{'Доход' if ttype=='income' else 'Расход'}: {fmt_amount(amount,cur)} {cur.upper()} • {cat}\n✓ Сохранено (#{tx_id})", reply_markup=MAIN_KB)
		return

	await update.message.reply_text("Ок ✅ Напиши: «такси 25 000» или зайди в «Долги».", reply_markup=MAIN_KB)

async def unknown_cmd(update: Update, _: ContextTypes.DEFAULT_TYPE):
	await update.message.reply_text("Нажми кнопку или напиши траты/доход.", reply_markup=MAIN_KB)

def main():
	token = DEFAULT_BOT_TOKEN
	app = Application.builder().token(token).build()
	app.add_handler(CommandHandler("start", start))
	app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
	app.add_handler(MessageHandler(filters.COMMAND, unknown_cmd))
	log.info("Starting polling")
	app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
	main()
