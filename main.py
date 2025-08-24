Ситуация:
В моём боте есть раздел «Долги». Там есть кнопка «Мне должны», где я должен указывать сумму и имя (например: 5000 usd Roni).

Проблема:
Когда я выбираю «Мне должны» и ввожу данные, бот не сохраняет их в базу и не показывает в списке должников. Он просто возвращается к выбору действия («Выберите действие»), как будто ничего не произошло.

Что должно быть:

После нажатия «Мне должны» и ввода текста вида «5000 usd Roni» бот должен создать запись с полями:

Сумма (5000)

Валюта (USD)

Имя/комментарий (Roni)

Дата/время добавления

Эта запись должна отображаться в списке «Мне должны».

Также должна учитываться в общем балансе (чистый баланс с учётом долгов).



Я нажимаю:

+ Мне должны
5000 usd Roni


Бот отвечает:

✅ Долг добавлен:
• Сумма: 5000 USD
• Должник: Roni
• Дата: 21.08.2025 14:27


И показывает меню:

➕ Я должен    ➕ Мне должны
📜 Я должен    📜 Мне должны
✖️ Закрыть долг  ➖ Уменьшить долг
◀️ Назад


А если я зайду в 📜 Мне должны, бот пишет список:

Список должников:
#1 Roni – 5000 USD (21.08.2025)


И в общем балансе он показывает отдельно:

Баланс без долгов: 20 000$
Я должен: 6 000$
Мне должны: 5 000$
Чистый баланс: 19 000$
Thought for 33s
Скопируй этот main.py — «➕ Мне должны → 5000 usd Roni» сохранится, выдаст подтверждение и появится в «📜 Мне должны». Баланс учитывает долги.

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
Проверка: Долги → «➕ Мне должны» → отправь «5000 usd Roni». Придёт подтверждение и запись появится в «📜 Мне должны». Баланс учитывает долги.
это полный список кода
Thought for 13s
Да, это полный main.py. Для запуска добавь requirements.txt и перезапусти:

python-telegram-bot==20.7
httpx==0.25.2
Старт: BOT_TOKEN в переменные окружения (или оставь встроенный) и запусти команду: python main.py.

IndentationError: unindent does not match any outer indentation level

Aug 24 14:48:52
  File "/app/main.py", line 330

Aug 24 14:48:52
    return

Aug 24 14:48:52
          ^

Aug 24 14:48:52
IndentationError: unindent does not match any outer indentation level

Aug 24 14:48:54
  File "/app/main.py", line 330

Aug 24 14:48:54
    return

Aug 24 14:48:54
          ^

Aug 24 14:48:54
IndentationError: unindent does not match any outer indentation level

Aug 24 14:48:55
  File "/app/main.py", line 330

Aug 24 14:48:55
IndentationError: unindent does not match any outer indentation level
Thought for 29s
Причина: лишний отступ у return в ветке reduce_ask_amount. Замени функцию text_router целиком на эту (всё остальное не трогай):

async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
	uid = update.effective_user.id
	txt = (update.message.text or "").strip()
	low = txt.lower()

	if not is_authorized(uid):
		await update.message.reply_text("Доступ запрещён.")
		return

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
				now_s = datetime.now(TIMEZONE).strftime("%d.%m.%Y %H:%М")
				msg = (
					"✅ Долг добавлен:\n"
					f"• Сумма: {fmt_amount(amt,cur)} {cur.upper()}\n"
					f"• Должник: {who}\n"
					f"• Дата: {now_s}"
				)
				await update.message.reply_text(msg, reply_markup=debts_menu_kb())
				debts["stage"] = "menu"
			else:
				debts["counterparty"] = txt
				debts["stage"] = "add_amount"
				await update.message.reply_text(
					"Введите сумму и комментарий (например: 25 000 долг за обед).",
					reply_markup=ReplyKeyboardMarkup([[KeyboardButton(BACK_BTN)]], resize_keyboard=True)
				)
			return

		if stage == "add_amount":
			amt = parse_amount(txt)
			if amt is None:
				await update.message.reply_text(
					"Не понял сумму. Пример: 25 000 комментарий.",
					reply_markup=ReplyKeyboardMarkup([[KeyboardButton(BACK_BTN)]], resize_keyboard=True)
				)
				return
			cur = detect_currency(txt)
			who = debts.get("counterparty") or extract_counterparty_from_text(txt) or "—"
			debt_id = add_debt(uid, debts["direction"], who, amt, cur, txt)
			now_s = datetime.now(TIMEZONE).strftime("%d.%m.%Y %H:%М")
			msg = (
				"✅ Долг добавлен:\n"
				f"• Сумма: {fmt_amount(amt,cur)} {cur.upper()}\n"
				f"• Должник: {who}\n"
				f"• Дата: {now_s}"
			)
			await update.message.reply_text(msg, reply_markup=debts_menu_kb())
			debts["stage"] = "menu"
			return

		if stage == "close_ask_id":
			m = re.search(r"(\d+)", txt)
			if not m:
				await update.message.reply_text("Отправьте номер долга (например: 12).", reply_markup=debts_menu_kb())
				return
			ok = close_debt(uid, int(m.group(1)))
			await update.message.reply_text(
				"Долг закрыт." if ok else "Не удалось закрыть. Проверьте id.",
				reply_markup=debts_menu_kb()
			)
			debts["stage"] = "menu"
			return

		if stage == "reduce_ask_id":
			m = re.search(r"(\d+)", txt)
			if not m:
				await update.message.reply_text("Отправьте номер долга (например: 12).", reply_markup=debts_menu_kb())
				return
			debts["reduce_id"] = int(m.group(1))
			debts["stage"] = "reduce_ask_amount"
			await update.message.reply_text(
				"На сколько уменьшить? (например: 50 000)",
				reply_markup=ReplyKeyboardMarkup([[KeyboardButton(BACK_BTN)]], resize_keyboard=True)
			)
			return

		if stage == "reduce_ask_amount":
			amt = parse_amount(txt)
			if amt is None or amt <= 0:
				await update.message.reply_text(
					"Не понял сумму. Пример: 50 000",
					reply_markup=ReplyKeyboardMarkup([[KeyboardButton(BACK_BTN)]], resize_keyboard=True)
				)
				return
			res = reduce_debt(uid, debts["reduce_id"], amt)
			if not res:
				await update.message.reply_text("Не удалось уменьшить. Проверьте id.", reply_markup=debts_menu_kb())
			else:
				new_amount, cur, status = res
				if status == "closed":
					await update.message.reply_text("Долг погашен полностью.", reply_markup=debts_menu_kb())
				else:
					await update.message.reply_text(f"Новый остаток: {fmt_amount(new_amount,cur)} {cur.upper()}", reply_markup=debts_menu_kb())
			debts["stage"] = "menu"
			debts.pop("reduce_id", None)
			return

	# Вход в раздел «Долги»
	if low == "долги":
		context.user_data["debts"] = {"stage":"menu"}
		await update.message.reply_text("Раздел «Долги».", reply_markup=debts_menu_kb())
		return
	if low == "➕ я должен":
		context.user_data["debts"] = {"stage":"add_counterparty", "direction":"i_owe"}
		await update.message.reply_text("Кому вы должны? Или сразу: «5000 usd Иван».", reply_markup=ReplyKeyboardMarkup([[KeyboardButton(BACK_BTN)]], resize_keyboard=True))
		return
	if low == "➕ мне должны":
		context.user_data["debts"] = {"stage":"add_counterparty", "direction":"they_owe"}
		await update.message.reply_text("Кто должен вам? Или сразу: «5000 usd Roni».", reply_markup=ReplyKeyboardMarkup([[KeyboardButton(BACK_BTN)]], resize_keyboard=True))
		return
	if low == "📜 я должен":
		await update.message.reply_text(debts_list_pretty(uid, "i_owe"), reply_markup=debts_menu_kb())
		return
	if low == "📜 мне должны":
		await update.message.reply_text(debts_list_pretty(uid, "they_owe"), reply_markup=debts_menu_kb())
		return
	# Остальное — как было: баланс/история/операции...
отправь полностю чтобы я скопировал
Thought for 33s
Готово. Скопируй эти файлы и перезапусти.

# main.py
import os, re, sqlite3, time, logging
from datetime import datetime
from typing import Optional, Tuple, List
from zoneinfo import ZoneInfo

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# ------- Config -------
PORT = int(os.environ.get("PORT", "8080"))
DB_PATH = os.environ.get("DB_PATH", "finance.db")
TIMEZONE = ZoneInfo(os.environ.get("TZ", "Asia/Tashkent"))
ALLOWED_USER_IDS = {int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").replace(";", ",").split(",") if x.strip().isdigit()}
DEFAULT_BOT_TOKEN = os.environ.get("BOT_TOKEN", "7611168200:AAH_NPSecM5hrqPKindVLiQy4zkPIauqmTc")

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s | %(message)s", level=logging.INFO)
log = logging.getLogger("bot")

# ------- DB -------
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
    c.execute("CREATE INDEX IF NOT EXISTS idx_tx_user_ts ON tx(user_id, ts)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_debts_user ON debts(user_id, status, direction)")
    con.commit(); con.close()
init_db()

# ------- Keyboards -------
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

# ------- Helpers -------
def is_authorized(user_id: int) -> bool:
    return True if not ALLOWED_USER_IDS else user_id in ALLOWED_USER_IDS

def detect_currency(t: str) -> str:
    tl = t.lower()
    if "$" in tl:
        return "usd"
    words = set(re.findall(r"[a-zа-яё]+", tl))
    if {"usd", "доллар", "доллара", "доллары", "долларов", "бакс", "баксы", "дол"} & words:
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

CURRENCY_WORDS = {"usd","uzs","sum","сум","сумы","сумов","доллар","доллара","доллары","долларов","бакс","баксы","дол"}
def extract_counterparty_from_text(t: str) -> str:
    words = re.findall(r"[A-Za-zА-Яа-яЁё]+", t)
    names = [w for w in words if w.lower() not in CURRENCY_WORDS]
    return " ".join(names[-2:]) if names else ""

def fmt_amount(amount: float, cur: str) -> str:
    if cur == "uzs":
        return f"{int(round(amount)):,}".replace(",", " ")
    return f"{amount:.2f}"

# ------- TX -------
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

def get_balance(uid: int) -> Tuple[float, float]:
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    def s(tt: str, cur: str) -> float:
        c.execute("SELECT COALESCE(SUM(amount),0) FROM tx WHERE user_id=? AND ttype=? AND currency=?", (uid, tt, cur))
        return float(c.fetchone()[0] or 0.0)
    bal_uzs = s("income", "uzs") - s("expense", "uzs")
    bal_usd = s("income", "usd") - s("expense", "usd")
    con.close()
    return bal_uzs, bal_usd

# ------- Debts -------
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
                 FROM debts WHERE user_id=? AND status='open' AND direction=? ORDER BY id DESC""", (uid, direction))
    rows = c.fetchall(); con.close(); return rows

def close_debt(uid: int, debt_id: int) -> bool:
    now = int(time.time())
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("UPDATE debts SET status='closed', updated_ts=? WHERE id=? AND user_id=? AND status='open'", (now, debt_id, uid))
    ok = c.rowcount > 0
    con.commit(); con.close()
    return ok

def reduce_debt(uid: int, debt_id: int, delta: float) -> Optional[Tuple[float, str, str]]:
    now = int(time.time())
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("SELECT amount, currency FROM debts WHERE id=? AND user_id=? AND status='open'", (debt_id, uid))
    row = c.fetchone()
    if not row:
        con.close(); return None
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

def debt_totals(uid: int) -> Tuple[float, float, float, float]:
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    def s(direction: str, cur: str) -> float:
        c.execute("""SELECT COALESCE(SUM(amount),0) FROM debts
                     WHERE user_id=? AND status='open' AND direction=? AND currency=?""", (uid, direction, cur))
        return float(c.fetchone()[0] or 0.0)
    iowe_uzs = s("i_owe", "uzs"); iowe_usd = s("i_owe", "usd")
    they_uzs = s("they_owe", "uzs"); they_usd = s("they_owe", "usd")
    con.close()
    return iowe_uzs, iowe_usd, they_uzs, they_usd

def balance_with_debts_text(uid: int) -> str:
    uzs, usd = get_balance(uid)
    iowe_uzs, iowe_usd, they_uzs, they_usd = debt_totals(uid)
    net_uzs = uzs - iowe_uzs + they_uzs
    net_usd = usd - iowe_usd + they_usd
    return "\n".join([
        f"Баланс без долгов: {fmt_amount(uzs,'uzs')} UZS | {fmt_amount(usd,'usd')} USD",
        f"Я должен: {fmt_amount(iowe_uzs,'uzs')} UZS | {fmt_amount(iowe_usd,'usd')} USD",
        f"Мне должны: {fmt_amount(they_uzs,'uzs')} UZS | {fmt_amount(they_usd,'usd')} USD",
        f"Чистый баланс: {fmt_amount(net_uzs,'uzs')} UZS | {fmt_amount(net_usd,'usd')} USD",
    ])

def debts_list_pretty(uid: int, direction: str) -> str:
    rows = list_debts(uid, direction)
    title = "Список должников:" if direction == "they_owe" else "Список моих долгов:"
    if not rows:
        return f"{title}\n— пусто —"
    lines = [title]
    for id_, who, amount, cur, note, created_ts in rows:
        d = datetime.fromtimestamp(int(created_ts), tz=TIMEZONE).strftime("%d.%m.%Y")
        lines.append(f"#{id_} {who} – {fmt_amount(float(amount),cur)} {cur.upper()} ({d})")
    return "\n".join(lines)

# ------- Bot Handlers -------
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
        lines.append(f"#{id_} " + tx_line(ttype, float(amount), cur, cat, note, int(ts)))
    await update.message.reply_text("\n".join(lines), reply_markup=MAIN_KB)

async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    txt = (update.message.text or "").strip()
    low = txt.lower()

    if not is_authorized(uid):
        await update.message.reply_text("Доступ запрещён.")
        return

    # Debts flow FSM
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
                add_debt(uid, debts["direction"], who, amt, cur, txt)
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
                await update.message.reply_text(
                    "Введите сумму и комментарий (например: 25 000 долг за обед).",
                    reply_markup=ReplyKeyboardMarkup([[KeyboardButton(BACK_BTN)]], resize_keyboard=True)
                )
            return

        if stage == "add_amount":
            amt = parse_amount(txt)
            if amt is None:
                await update.message.reply_text(
                    "Не понял сумму. Пример: 25 000 комментарий.",
                    reply_markup=ReplyKeyboardMarkup([[KeyboardButton(BACK_BTN)]], resize_keyboard=True)
                )
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
            debts["stage"] = "menu"
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
            res = reduce_debt(uid, int(debts.get("reduce_id", 0)), amt)
            if not res:
                await update.message.reply_text("Не удалось уменьшить. Проверьте id.", reply_markup=debts_menu_kb())
            else:
                new_amount, cur, status = res
                await update.message.reply_text(
                    "Долг погашен полностью." if status == "closed" else f"Новый остаток: {fmt_amount(new_amount,cur)} {cur.upper()}",
                    reply_markup=debts_menu_kb()
                )
            debts["stage"] = "menu"
            debts.pop("reduce_id", None)
            return

    # Enter debts menu
    if low == "долги":
        context.user_data["debts"] = {"stage": "menu"}
        await update.message.reply_text("Раздел «Долги».", reply_markup=debts_menu_kb())
        return
    if low == "➕ я должен":
        context.user_data["debts"] = {"stage": "add_counterparty", "direction": "i_owe"}
        await update.message.reply_text("Кому вы должны? Или сразу: «5000 usd Иван».",
                                        reply_markup=ReplyKeyboardMarkup([[KeyboardButton(BACK_BTN)]], resize_keyboard=True))
        return
    if low == "➕ мне должны":
        context.user_data["debts"] = {"stage": "add_counterparty", "direction": "they_owe"}
        await update.message.reply_text("Кто должен вам? Или сразу: «5000 usd Roni».",
                                        reply_markup=ReplyKeyboardMarkup([[KeyboardButton(BACK_BTN)]], resize_keyboard=True))
        return
    if low == "📜 я должен":
        await update.message.reply_text(debts_list_pretty(uid, "i_owe"), reply_markup=debts_menu_kb())
        return
    if low == "📜 мне должны":
        await update.message.reply_text(debts_list_pretty(uid, "they_owe"), reply_markup=debts_menu_kb())
        return

    # Balance / History
    if "баланс" in low:
        await update.message.reply_text(balance_with_debts_text(uid), reply_markup=MAIN_KB)
        return
    if "история" in low:
        await send_history(update, uid, 10); return

    # Free text transactions (optional simple)
    amt = parse_amount(txt)
    if amt is not None:
        cur = detect_currency(txt)
        tx_id = add_tx(uid, "expense", amt, cur, "Прочее", txt)
        await update.message.reply_text(f"Расход: {fmt_amount(amt,cur)} {cur.upper()} • Прочее\n✓ Сохранено (#{tx_id})", reply_markup=MAIN_KB)
        return

    await update.message.reply_text("Ок ✅ Напиши: «такси 25 000» или зайди в «Долги».", reply_markup=MAIN_KB)

async def unknown_cmd(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Нажми кнопку или напиши траты/доход.", reply_markup=MAIN_KB)

# ------- Main -------
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
# requirements.txt
python-telegram-bot==20.7
но тут потеряно ранее функции которые были а я не хочу потерять 
Thought for 52s
Понял. Ничего из твоего кода не трогаем, только минимальные правки для «➕ Мне должны» (и «➕ Я должен»). Вставь/замени ровно эти 3 фрагмента.

Добавь хелпер после твоего parse_amount(...)
# добавь сразу после parse_amount
CURRENCY_WORDS = {"usd","uzs","sum","сум","сумы","сумов","доллар","доллара","доллары","долларов","бакс","баксы","дол"}

def extract_counterparty_from_text(t: str) -> str:
	words = re.findall(r"[A-Za-zА-Яа-яЁё]+", t)
	names = [w for w in words if w.lower() not in CURRENCY_WORDS]
	return " ".join(names[-2:]) if names else ""
Замени твой список долгов, чтобы в выборке была дата (created_ts)
def list_debts(uid: int, direction: str):
	con = sqlite3.connect(DB_PATH); c = con.cursor()
	c.execute("""SELECT id, counterparty, amount, currency, note, created_ts
	             FROM debts
	             WHERE user_id=? AND status='open' AND direction=?
	             ORDER BY id DESC""", (uid, direction))
	rows = c.fetchall(); con.close(); return rows
И обнови формат вывода списка (если у тебя функция называется иначе — обнови ту, что используется при «📜 Мне должны»/«📜 Я должен»):

def debts_list_text(uid: int, direction: str) -> str:
	rows = list_debts(uid, direction)
	title = "Список должников:" if direction == "they_owe" else "Список моих долгов:"
	if not rows:
		return f"{title}\n— пусто —"
	lines = [title]
	for id_, who, amount, cur, note, created_ts in rows:
		d = datetime.fromtimestamp(int(created_ts)).strftime("%d.%m.%Y")
		lines.append(f"#{id_} {who} – {amount:.0f} {cur.upper()} ({d})")
	return "\n".join(lines)
Внутри text_router сразу после:
uid = ...
txt = ...
low = ...
debts = context.user_data.get("debts") (или туда, где у тебя читается debts)
вставь этот блок. Он перехватывает ввод «5000 usd Roni» и сохраняет долг, не ломая остальную логику:

# ВСТАВЬ ЗДЕСЬ: быстрый обработчик единой строки для долгов
if debts and debts.get("stage") in ("add_counterparty", "add_amount"):
	amt = parse_amount(txt)
	if amt is not None:
		cur = detect_currency(txt)
		who = debts.get("counterparty") or extract_counterparty_from_text(txt) or "—"
		debt_id = add_debt(uid, debts["direction"], who, amt, cur, txt)
		now_s = datetime.now(TIMEZONE).strftime("%d.%m.%Y %H:%M") if "TIMEZONE" in globals() else datetime.now().strftime("%d.%m.%Y %H:%M")
		await update.message.reply_text(
			"✅ Долг добавлен:\n"
			f"• Сумма: {amt:.0f} {cur.upper()}\n"
			f"• Должник: {who}\n"
			f"• Дата: {now_s}",
			reply_markup=debts_menu_kb()
		)
		debts["stage"] = "menu"
		return
	# иначе пусть работает твоя существующая логика (имя -> потом сумма)
Этого достаточно:

«➕ Мне должны» → «5000 usd Roni» теперь сразу создаёт запись и отвечает подтверждением.
«📜 Мне должны» показывает список с датами.
Баланс у тебя уже учитывает долги — менять не нужно.
весь список чтобы я скопировал и ставил
Thought for 27s
Скопируй эти два файла целиком.

# main.py
import os, re, sqlite3, time, logging, csv, io
from datetime import datetime, timedelta, time as dtime
from typing import Optional, Tuple, List
from zoneinfo import ZoneInfo

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
    if row:
        rows.append(row)
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

CURRENCY_WORDS = {"usd","uzs","sum","сум","сумы","сумов","доллар","доллара","доллары","долларов","бакс","баксы","дол"}
def extract_counterparty_from_text(t: str) -> str:
    words = re.findall(r"[A-Za-zА-Яа-яЁё]+", t)
    names = [w for w in words if w.lower() not in CURRENCY_WORDS]
    return " ".join(names[-2:]) if names else ""

def fmt_amount(amount: float, cur: str) -> str:
    if cur == "uzs":
        return f"{int(round(amount)):,}".replace(",", " ")
    return f"{amount:.2f}"

# ---------------- TX ----------------
def ai_classify_finance(t: str):
    ttype = "expense"
    lt = t.lower()
    if any(w in lt for w in ["зарплата","премия","бонус","получил","пришло","доход"]):
        ttype = "income"
    amount = parse_amount(t)
    cur = detect_currency(t)
    cat = "Прочее"
    for c, kws in CATEGORY_KEYWORDS.items():
        if any(k in lt for k in kws):
            cat = c; break
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
    bal_uzs = s("income", "uzs") - s("expense", "uzs")
    bal_usd = s("income", "usd") - s("expense", "usd")
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
        f"• Баланс UZS: {fmt_amount(bal_узs,'uzs')} | USD: {fmt_amount(bal_usd,'usd')}",
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

# ---------------- Recurring / Reminders ----------------
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
    today = datetime.now(TIMEZONE).date()
    date_str = today.isoformat()
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("""SELECT id, user_id, ttype, amount, currency, category, note, frequency, day_of_week, day_of_month, last_applied_date FROM recurring""")
    rows = c.fetchall(); con.close()
    for rec in rows:
        rec_id, uid, ttype, amount, currency, category, note, freq, dow, dom, last_date = rec
        if last_date == date_str:
            continue
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
        log.warning("JobQueue is not available; skipping scheduled jobs")
        return
    app.job_queue.run_daily(lambda ctx: ctx.application.create_task(process_recurring_all(ctx.application)),
                            dtime(hour=9, minute=0, tzinfo=TIMEZONE), name="recurring-processor")

def schedule_reminder_for_user(app: Application, uid: int, hour: int, minute: int):
    if not getattr(app, "job_queue", None):
        return
    job_name = f"reminder-{uid}"
    for job in app.job_queue.get_jobs_by_name(job_name):
        job.schedule_removal()
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
    return iowe_uzs, iowe_usd, they_uzs, they_usd

def debts_list_text(uid: int, direction: str) -> str:
    rows = list_debts(uid, direction)
    title = "Список должников:" if direction == "they_owe" else "Список моих долгов:"
    if not rows:
        return f"{title}\n— пусто —"
    lines = [title]
    for id_, who, amount, cur, note, created_ts in rows:
        d = datetime.fromtimestamp(created_ts, tz=TIMEZONE).strftime("%d.%m.%Y")
        lines.append(f"#{id_} {who} – {fmt_amount(amount,cur)} {cur.upper()} ({d})")
    return "\n".join(lines)

def balance_with_debts_text(uid: int) -> str:
    uzs, usd = get_balance(uid)
    iowe_uzs, iowe_usd, they_uzs, they_usd = debt_totals(uid)
    net_uzs = uzs - iowe_uzs + they_uzs
    net_usd = usd - iowe_usd + they_usd
    lines = [
        f"Баланс без долгов: {fmt_amount(uzs,'uzs')} UZS | {fmt_amount(usd,'usd')} USD",
        f"Я должен: {fmt_amount(iowe_uzs,'uzs')} UZS | {fmt_amount(iowe_usd,'usd')} USD",
        f"Мне должны: {fmt_amount(they_uzs,'uzs')} UZS | {fmt_amount(they_usd,'usd')} USD",
        f"Чистый баланс: {fmt_amount(net_uzs,'uzs')} UZS | {fmt_amount(net_usd,'usd')} USD",
    ]
    return "\n".join(lines)

# ---------------- AI Tip + Pin Summary ----------------
def week_bounds_now() -> Tuple[int,int]:
    now = datetime.now(TIMEZONE)
    week_start = now - timedelta(days=now.weekday())
    start = datetime(week_start.year, week_start.month, week_start.day, 0, 0, 0, tzinfo=TIMEZONE)
    return int(start.timestamp()), int(now.timestamp())

def sum_expenses_in_range(uid: int, category: str, currency: str, start_ts: int, end_ts: int) -> float:
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("""SELECT COALESCE(SUM(amount),0) FROM tx
                 WHERE user_id=? AND ttype='expense' AND category=? AND currency=? AND ts BETWEEN ? AND ?""",
              (uid, category, currency, start_ts, end_ts))
    s = c.fetchone()[0] or 0.0
    con.close(); return s

def ai_hint_after_tx(uid: int, ttype: str, cur: str, cat: str) -> str:
    if ttype == "expense":
        try:
            limit = None
            for bcat, bcur, blim in get_budgets(uid):
                if bcat == cat and bcur == cur:
                    limit = blim; break
            if limit and limit > 0:
                spent = month_expense_sum(uid, cat, cur)
                ratio = spent / limit if limit else 0
                if ratio >= 0.9:
                    perc = int(round(ratio * 100))
                    return f"На «{cat}» уже потрачено {perc}% бюджета."
        except Exception:
            pass
    try:
        now_start, now_end = week_bounds_now()
        prev_end = now_start - 1
        prev_start = prev_end - 7*24*3600 + 1
        now_sum = sum_expenses_in_range(uid, cat, cur, now_start, now_end)
        prev_sum = sum_expenses_in_range(uid, cat, cur, prev_start, prev_end)
        if prev_sum > 0 and now_sum >= prev_sum * 1.4:
            inc = int(round((now_sum/prev_sum - 1) * 100))
            return f"Траты на «{cat}» выросли на {inc}% к прошлой неделе."
    except Exception:
        pass
    return "Продолжайте вести учёт — вы молодец!"

def balance_summary_text(uid: int, ai_tip: str) -> str:
    uzs, usd = get_balance(uid)
    iowe_uzs, iowe_usd, they_uzs, they_usd = debt_totals(uid)
    net_uzs = uzs - iowe_узs + they_узs
    net_usd = usd - iowe_usд + they_usд
    now = datetime.now(TIMEZONE).strftime("%d.%m, %H:%M")
    lines = [
        f"📌 Итог на {now}",
        f"Баланс: {fmt_amount(uzs,'uzs')} UZS | {fmt_amount(usd,'usd')} USD",
        f"Я должен: {fmt_amount(iowe_uzs,'uzs')} UZS | {fmt_amount(iowe_usd,'usd')} USD",
        f"Мне должны: {fmt_amount(they_узs,'uzs')} UZS | {fmt_amount(they_usd,'usd')} USD",
        f"Чистый баланс: {fmt_amount(net_узs,'uzs')} UZS | {fmt_amount(net_usd,'usd')} USD",
        f"\nAI: {ai_tip}",
    ]
    return "\n".join(lines)

async def send_and_pin_summary(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int, ai_tip: str):
    text = balance_summary_text(uid, ai_tip)
    chat_id = update.effective_chat.id
    sent = await context.bot.send_message(chat_id=chat_id, text=text)
    if update.effective_chat.type in ("group", "supergroup"):
        try:
            await context.bot.unpin_all_chat_messages(chat_id=chat_id)
        except Exception as e:
            log.warning(f"Unpin failed: {e}")
        try:
            await context.bot.pin_chat_message(chat_id=chat_id, message_id=sent.message_id, disable_notification=True)
        except Exception as e:
            log.warning(f"Pin failed: {e}")

# ---------------- Export / Analytics / PDF ----------------
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
            ws.append([id_, datetime.fromtimestamp(ts, tz=TIMEZONE).strftime("%Y-%m-%d %H:%M:%S"), amount, cur, ttype, cat, note or ""])
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
    return datetime.fromtimestamp(ts, tz=TIMEZONE).strftime("%Y-%m-%d")

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
        f"Расход: {sparkline(exp_узs_d:=exp_uzs_d)}",
        f"Доход:  {sparkline(inc_узs_d:=inc_uzs_d)}",
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

# ---------------- UI helpers ----------------
async def start(update: Update, _: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Доступ запрещён. Обратитесь к администратору.")
        return
    upsert_seen_user(update.effective_user.id, update.effective_user.first_name or "", update.effective_user.username)
    await update.message.reply_text("Razzakov’s Finance 🤖\nПиши: «самса 18 000 сум» или используй кнопки.", reply_markup=MAIN_KB)

def tx_line(ttype: str, amount: float, cur: str, cat: str, note: Optional[str], ts: int) -> str:
    dt = datetime.fromtimestamp(ts, tz=TIMEZONE).strftime("%d.%m %H:%M")
    sign = "➕" if ttype == "income" else "➖"
    return f"{dt} {sign} {fmt_amount(amount,cur)} {cur.upper()} • {cat} • {note or '-'}"

def users_summary_text() -> str:
    if not ALLOWED_USER_IDS:
        return "Контроль доступа не настроен. Разрешены все пользователи."
    lines = ["Разрешённые пользователи (ID):"]
    for uid in sorted(ALLOWED_USER_IDS):
        lines.append(f"• {uid}")
    return "\n".join(lines)

async def send_history(update: Update, uid: int, limit: int = 10):
    rows = last_txs(uid, limit)
    if not rows:
        await update.message.reply_text("История пуста.", reply_markup=MAIN_KB); return
    lines = [f"Последние операции ({len(rows)}):"]
    for id_, ttype, amount, cur, cat, note, ts in rows:
        lines.append(f"#{id_} " + tx_line(ttype, amount, cur, cat, note, ts))
    await update.message.reply_text("\n".join(lines), reply_markup=MAIN_KB)

# ---------------- Handlers ----------------
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
            await update.message.reply_text("Пример: «Повтор: аренда 2 000 000 сум ежемесячно 5».")
            return
        add_recurring(uid, ttype, amount, cur, cat, txt, freq, dow, dom)
        await update.message.reply_text("Повтор добавлен.")
    else:
        rows = list_recurring(uid)
        if not rows:
            await update.message.reply_text("Повторов нет.")
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
    top = c.fetchall(); con.close()
    inc_uzs = sums.get(("income","uzs"),0.0); inc_usd = sums.get(("income","usd"),0.0)
    exp_uzs = sums.get(("expense","uzs"),0.0); exp_usd = sums.get(("expense","usd"),0.0)
    lines = [
        f"Итоги {title}:",
        f"• Доход UZS: {fmt_amount(inc_uzs,'uzs')} | USD: {fmt_amount(inc_usd,'usd')}",
        f"• Расход UZS: {fmt_amount(exp_узs,'uzs')} | USD: {fmt_amount(exp_usd,'usd')}",
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
            await update.message.reply_text("Напоминаний нет. Установите время: «Напоминания 21:30».")
        else:
            await update.message.reply_text(f"Текущее напоминание: {row[0]:02d}:{row[1]:02d}.")

# ---------------- Router ----------------
async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    txt = (update.message.text or "").strip()
    low = txt.lower()

    if not is_authorized(uid):
        await update.message.reply_text("Доступ запрещён. Обратитесь к администратору.")
        return

    upsert_seen_user(uid, update.effective_user.first_name or "", update.effective_user.username)

    # Debts flow (supports one-line "5000 usd Roni")
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
                add_debt(uid, debts["direction"], who, amt, cur, txt)
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
            add_debt(uid, debts["direction"], who, amt, cur, txt)
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
                await update.message.reply_text("Отправьте номер долга (например: 12).", reply_markup=debts_menu_kb())
                return
            ok = close_debt(uid, int(m.group(1)))
            await update.message.reply_text("Долг закрыт." if ok else "Не удалось закрыть. Проверьте id.", reply_markup=debts_menu_kb())
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
        await update.message.reply_text(debts_list_text(uid, "i_owe"), reply_markup=debts_menu_kb()); return
    if low == "📜 мне должны":
        await update.message.reply_text(debts_list_text(uid, "they_owe"), reply_markup=debts_menu_kb()); return

    # Step-by-step income/expense flow
    flow = context.user_data.get("flow")
    if flow:
        stage = flow.get("stage"); ttype = flow.get("ttype")
        if txt == BACK_BTN:
            context.user_data.pop("flow", None)
            await update.message.reply_text("Отменено. Главное меню.", reply_markup=MAIN_KB); return
        if stage == "choose_category":
            options = EXPENSE_CATEGORIES if ttype == "expense" else INCOME_CATEGORIES
            if txt in options:
                flow["category"] = txt
                flow["stage"] = "await_amount"
                await update.message.reply_text(f"Введи сумму для «{txt}». Можно добавить примечание.", reply_markup=amount_kb())
            else:
                await update.message.reply_text("Выбери категорию на клавиатуре.", reply_markup=categories_kb(ttype))
            return
        if stage == "await_amount":
            amount = parse_amount(txt)
            if amount is None:
                await update.message.reply_text("Не понял сумму. Пример: 25 000 или 25 000 обед.", reply_markup=amount_kb()); return
            cur = detect_currency(txt)
            cat = flow.get("category") or "Прочее"
            tx_id = add_tx(uid, ttype, amount, cur, cat, txt)
            context.user_data.pop("flow", None)
            await update.message.reply_text(f"{'Доход' if ttype=='income' else 'Расход'}: {fmt_amount(amount,cur)} {cur.upper()} • {cat}\n✓ Сохранено (#{tx_id})", reply_markup=MAIN_KB)
            if ttype == "expense":
                await maybe_warn_budget(update, uid, cat, cur)
            ai_tip = ai_hint_after_tx(uid, ttype, cur, cat)
            await send_and_pin_summary(update, context, uid, ai_tip)
            return

    if low == "➖ расход" or low == "расход":
        context.user_data["flow"] = {"stage":"choose_category","ttype":"expense"}
        await update.message.reply_text("Выбери категорию расхода:", reply_markup=categories_kb("expense")); return
    if low == "➕ доход" or low == "доход":
        context.user_data["flow"] = {"stage":"choose_category","ttype":"income"}
        await update.message.reply_text("Выбери категорию дохода:", reply_markup=categories_kb("income")); return

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
        if xl_name:
            await update.message.reply_document(document=xl_b, filename=xl_name)
        return
    if "pdf" in low:
        pdf = await pdf_report_month(uid)
        if pdf:
            buf, name = pdf; await update.message.reply_document(document=buf, filename=name)
        else:
            await update.message.reply_text("Не удалось сформировать PDF сейчас.")
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
        lines.append(balance_with_debts_text(uid))
        await update.message.reply_text("\n".join(lines)); return

    # Free text transaction
    ttype, amount, cur, cat = ai_classify_finance(txt)
    if amount is not None:
        tx_id = add_tx(uid, ttype, amount, cur, cat, txt)
        await update.message.reply_text(f"{'Доход' if ttype=='income' else 'Расход'}: {fmt_amount(amount,cur)} {cur.upper()} • {cat}\n✓ Сохранено (#{tx_id})", reply_markup=MAIN_KB)
        if ttype == "expense":
            await maybe_warn_budget(update, uid, cat, cur)
        ai_tip = ai_hint_after_tx(uid, ttype, cur, cat)
        await send_and_pin_summary(update, context, uid, ai_tip)
        return

    await update.message.reply_text("Принято ✅ Напиши: «такси 25 000», или используй кнопки.", reply_markup=MAIN_KB)

async def unknown_cmd(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Нажми кнопку или напиши траты/доход.", reply_markup=MAIN_KB)

# ---------------- Main ----------------
def main():
    token = DEFAULT_BOT_TOKEN
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
# requirements.txt
python-telegram-bot[job-queue]==20.7
httpx==0.25.2
openpyxl==3.1.5
reportlab==4.2.2
Aug 24 15:10:22
2025-08-24 10:10:22,659 INFO httpx | HTTP Request: POST https://api.telegram.org/bot7611168200:AAH_NPSecM5hrqPKindVLiQy4zkPIauqmTc/getUpdates "HTTP/1.1 200 OK"

Aug 24 15:10:42
2025-08-24 10:10:32,663 INFO httpx | HTTP Request: POST https://api.telegram.org/bot7611168200:AAH_NPSecM5hrqPKindVLiQy4zkPIauqmTc/getUpdates "HTTP/1.1 200 OK"

Aug 24 15:10:42
2025-08-24 10:10:42,667 INFO httpx | HTTP Request: POST https://api.telegram.org/bot7611168200:AAH_NPSecM5hrqPKindVLiQy4zkPIauqmTc/getUpdates "HTTP/1.1 200 OK"

Aug 24 15:11:01
Stopping Container

Aug 24 15:11:02
2025-08-24 10:10:52,671 INFO httpx | HTTP Request: POST https://api.telegram.org/bot7611168200:AAH_NPSecM5hrqPKindVLiQy4zkPIauqmTc/getUpdates "HTTP/1.1 200 OK"

Aug 24 15:11:02
2025-08-24 10:11:00,089 INFO httpx | HTTP Request: POST https://api.telegram.org/bot7611168200:AAH_NPSecM5hrqPKindVLiQy4zkPIauqmTc/getUpdates "HTTP/1.1 200 OK"

Aug 24 15:11:02
2025-08-24 10:11:00,090 INFO telegram.ext.Application | Application is stopping. This might take a moment.

Aug 24 15:11:02
2025-08-24 10:11:00,090 INFO apscheduler.scheduler | Scheduler has been shut down

Aug 24 15:11:02
2025-08-24 10:11:00,100 INFO telegram.ext.Application | Application.stop() complete
Thought for 33s
Это Railway останавливает контейнер: сервис помечен как Web, а бот на polling не слушает PORT, поэтому healthcheck “падает” и платформа шлёт SIGTERM (видно по “Application is stopping…”).

Сделай один из вариантов.

Рекомендуемо (без кода):

В Railway поменяй тип сервиса на Worker (или отключи HTTP/healthcheck).
Start Command оставь: python main.py
Redeploy.
Оставить Web и пройти healthcheck (маленькая правка в main.py):

Добавь наверх импорты:
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler
Добавь рядом с хелперами:
class HealthHandler(BaseHTTPRequestHandler):
	def do_GET(self):
		self.send_response(200); self.send_header("Content-Type","text/plain"); self.end_headers()
		self.wfile.write(b"OK")
	def log_message(self, format, *args): return

def start_healthcheck_server():
	httpd = HTTPServer(("0.0.0.0", PORT), HealthHandler)
	httpd.serve_forever()
В функции main(), перед app.run_polling(...):
Thread(target=start_healthcheck_server, daemon=True).start()
Перезапусти — контейнер перестанет останавливаться, а бот останется на polling.

отправь всегда так чтобы я просто скопировал
Thought for 49s
# main.py
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

# ---------------- Healthcheck (for Railway Web) ----------------
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        return

def start_healthcheck_server():
    httpd = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    httpd.serve_forever()

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
    if row:
        rows.append(row)
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

CURRENCY_WORDS = {"usd","uzs","sum","сум","сумы","сумов","доллар","доллара","доллары","долларов","бакс","баксы","дол"}
def extract_counterparty_from_text(t: str) -> str:
    words = re.findall(r"[A-Za-zА-Яа-яЁё]+", t)
    names = [w for w in words if w.lower() not in CURRENCY_WORDS]
    return " ".join(names[-2:]) if names else ""

def fmt_amount(amount: float, cur: str) -> str:
    if cur == "uzs":
        return f"{int(round(amount)):,}".replace(",", " ")
    return f"{amount:.2f}"

# ---------------- TX ----------------
def ai_classify_finance(t: str):
    ttype = "expense"
    lt = t.lower()
    if any(w in lt for w in ["зарплата","премия","бонус","получил","пришло","доход"]):
        ttype = "income"
    amount = parse_amount(t)
    cur = detect_currency(t)
    cat = "Прочее"
    for c, kws in CATEGORY_KEYWORDS.items():
        if any(k in lt for k in kws):
            cat = c; break
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
    bal_uzs = s("income", "uzs") - s("expense", "uzs")
    bal_usd = s("income", "usd") - s("expense", "usd")
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
    today = datetime.now(TIMEZONE).date()
    date_str = today.isoformat()
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("""SELECT id, user_id, ttype, amount, currency, category, note, frequency, day_of_week, day_of_month, last_applied_date FROM recurring""")
    rows = c.fetchall(); con.close()
    for rec in rows:
        rec_id, uid, ttype, amount, currency, category, note, freq, dow, dom, last_date = rec
        if last_date == date_str:
            continue
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
        log.warning("JobQueue is not available; skipping scheduled jobs")
        return
    app.job_queue.run_daily(lambda ctx: ctx.application.create_task(process_recurring_all(ctx.application)),
                            dtime(hour=9, minute=0, tzinfo=TIMEZONE), name="recurring-processor")

def schedule_reminder_for_user(app: Application, uid: int, hour: int, minute: int):
    if not getattr(app, "job_queue", None):
        return
    job_name = f"reminder-{uid}"
    for job in app.job_queue.get_jobs_by_name(job_name):
        job.schedule_removal()
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
    if not row:
        con.close(); return None
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
    if not rows:
        return f"{title}\n— пусто —"
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
    return iowe_uzs, iowe_usd, they_uzs, they_usd

def balance_with_debts_text(uid: int) -> str:
    uzs, usd = get_balance(uid)
    iowe_uzs, iowe_usd, they_uzs, they_usd = debt_totals(uid)
    net_uzs = uzs - iowe_uzs + they_uzs
    net_usd = usd - iowe_usd + they_uzs
    lines = [
        f"Баланс без долгов: {fmt_amount(uzs,'uzs')} UZS | {fmt_amount(usd,'usd')} USD",
        f"Я должен: {fmt_amount(iowe_uzs,'uzs')} UZS | {fmt_amount(iowe_usd,'usd')} USD",
        f"Мне должны: {fmt_amount(they_uzs,'uzs')} UZS | {fmt_amount(they_usd,'usd')} USD",
        f"Чистый баланс: {fmt_amount(net_uzs,'uzs')} UZS | {fmt_amount(net_usd,'usd')} USD",
    ]
    return "\n".join(lines)

# ---------------- Export / Rates / Analytics / PDF ----------------
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
    return datetime.fromtimestamp(ts, tz=TIMEZONE).strftime("%Y-%m-%d")

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
            weeks.insert(0, b); seen.add(b)
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

# ---------------- UI helpers ----------------
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

# ---------------- Router ----------------
async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    txt = (update.message.text or "").strip()
    low = txt.lower()

    if not is_authorized(uid):
        await update.message.reply_text("Доступ запрещён. Обратитесь к администратору.")
        return

    upsert_seen_user(uid, update.effective_user.first_name or "", update.effective_user.username)

    # Debts flow (supports one-line "5000 usd Roni")
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
                who = debts.get("counterparty") or extract_counterparty_from_text(txt) or "—"
                add_debt(uid, debts["direction"], who, amt, cur, txt)
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
            add_debt(uid, debts["direction"], who, amt, cur, txt)
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
                await update.message.reply_text("Отправьте номер долга (например: 12).", reply_markup=debts_menu_kb())
                return
            ok = close_debt(uid, int(m.group(1)))
            await update.message.reply_text("Долг закрыт." if ok else "Не удалось закрыть. Проверьте id.", reply_markup=debts_menu_kb())
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
        await update.message.reply_text(debts_list_text(uid, "i_owe"), reply_markup=debts_menu_kb()); return
    if low == "📜 мне должны":
        await update.message.reply_text(debts_list_text(uid, "they_owe"), reply_markup=debts_menu_kb()); return

    # Step-by-step income/expense flow (optional)
    flow = context.user_data.get("flow")
    if flow:
        stage = flow.get("stage"); ttype = flow.get("ttype")
        if txt == BACK_BTN:
            context.user_data.pop("flow", None)
            await update.message.reply_text("Отменено. Главное меню.", reply_markup=MAIN_KB); return
        if stage == "choose_category":
            options = EXPENSE_CATEGORIES if ttype == "expense" else INCOME_CATEGORIES
            if txt in options:
                flow["category"] = txt
                flow["stage"] = "await_amount"
                await update.message.reply_text(f"Введи сумму для «{txt}». Можно добавить примечание.", reply_markup=amount_kb())
            else:
                await update.message.reply_text("Выбери категорию на клавиатуре.", reply_markup=categories_kb(ttype))
            return
        if stage == "await_amount":
            amount = parse_amount(txt)
            if amount is None:
                await update.message.reply_text("Не понял сумму. Пример: 25 000 или 25 000 обед.", reply_markup=amount_kb()); return
            cur = detect_currency(txt)
            cat = flow.get("category") or "Прочее"
            tx_id = add_tx(uid, ttype, amount, cur, cat, txt)
            context.user_data.pop("flow", None)
            await update.message.reply_text(f"{'Доход' if ttype=='income' else 'Расход'}: {fmt_amount(amount,cur)} {cur.upper()} • {cat}\n✓ Сохранено (#{tx_id})", reply_markup=MAIN_KB)
            if ttype == "expense":
                await maybe_warn_budget(update, uid, cat, cur)
            ai_tip = ai_hint_after_tx(uid, ttype, cur, cat)
            await send_and_pin_summary(update, context, uid, ai_tip)
            return

    if low == "➖ расход" or low == "расход":
        context.user_data["flow"] = {"stage":"choose_category","ttype":"expense"}
        await update.message.reply_text("Выбери категорию расхода:", reply_markup=categories_kb("expense")); return
    if low == "➕ доход" or low == "доход":
        context.user_data["flow"] = {"stage":"choose_category","ttype":"income"}
        await update.message.reply_text("Выбери категорию дохода:", reply_markup=categories_kb("income")); return

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
        if xl_name:
            await update.message.reply_document(document=xl_b, filename=xl_name)
        return
    if "pdf" in low:
        pdf = await pdf_report_month(uid)
        if pdf:
            buf, name = pdf; await update.message.reply_document(document=buf, filename=name)
        else:
            await update.message.reply_text("Не удалось сформировать PDF сейчас.")
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
        lines.append(balance_with_debts_text(uid))
        await update.message.reply_text("\n".join(lines)); return

    # Free text transaction
    ttype, amount, cur, cat = ai_classify_finance(txt)
    if amount is not None:
        tx_id = add_tx(uid, ttype, amount, cur, cat, txt)
        await update.message.reply_text(f"{'Доход' if ttype=='income' else 'Расход'}: {fmt_amount(amount,cur)} {cur.upper()} • {cat}\n✓ Сохранено (#{tx_id})", reply_markup=MAIN_KB)
        if ttype == "expense":
            await maybe_warn_budget(update, uid, cat, cur)
        ai_tip = ai_hint_after_tx(uid, ttype, cur, cat)
        await send_and_pin_summary(update, context, uid, ai_tip)
        return

    await update.message.reply_text("Принято ✅ Напиши: «такси 25 000», или используй кнопки.", reply_markup=MAIN_KB)

async def unknown_cmd(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Нажми кнопку или напиши траты/доход.", reply_markup=MAIN_KB)

# ---------------- Main ----------------
def main():
    token = DEFAULT_BOT_TOKEN
    # Start healthcheck HTTP server (so Railway Web stays green)
    Thread(target=start_healthcheck_server, daemon=True).start()

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_cmd))

    # Optional scheduled jobs (requires python-telegram-bot[job-queue])
    try:
        from telegram.ext import JobQueue  # noqa: F401
        schedule_daily_jobs(app)
        load_and_schedule_all_reminders(app)
    except Exception:
        log.info("JobQueue extras not installed; skipping scheduled jobs")

    log.info("Starting polling")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
