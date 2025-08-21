import os, re, sqlite3, time, logging
from datetime import datetime
from typing import Optional
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

BOT_TOKEN   = "7611168200:AAHj7B6FelvvcoJMDBuKwKpveBHEo0NItnI"
WEBHOOK_URL = "https://beautiful-love.up.railway.app"
PORT = int(os.environ.get("PORT", "8080"))
DB_PATH = "finance.db"

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s | %(message)s", level=logging.INFO)
log = logging.getLogger("bot")

def init_db():
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS tx(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        ttype TEXT NOT NULL CHECK(ttype IN('income','expense')),
        amount REAL NOT NULL, currency TEXT NOT NULL, category TEXT NOT NULL,
        note TEXT, ts INTEGER NOT NULL)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_user_ts ON tx(user_id, ts)")
    con.commit(); con.close()
init_db()

MAIN_KB = ReplyKeyboardMarkup(
    [[KeyboardButton("➕ Доход"), KeyboardButton("➖ Расход")],
     [KeyboardButton("💰 Баланс"), KeyboardButton("📜 История")],
     [KeyboardButton("📊 Отчёт (месяц)")]],
    resize_keyboard=True
)

CURRENCY_SIGNS = {"usd": ["$", "usd", "дол", "доллар"], "uzs": ["сум", "sum", "uzs", "сумы", "сумов"]}
CATEGORY_KEYWORDS = {
    "Еда":["еда","продукт","обед","ужин","завтрак","кафе","ресторан","самса","плов","шаурма","пицца"],
    "Транспорт":["такси","топливо","бензин","газ","метро","автобус","аренда авто","аренда машины"],
    "Зарплата":["зарплата","оклад","премия","бонус","аванс"],
    "Здоровье":["аптека","врач","стоматолог","лекар","витамин"],
    "Развлечения":["кино","игра","cs2","steam","подписка","spotify","netflix"],
    "Дом":["аренда","квартира","коммунал","электр","интернет","ремонт"],
    "Детское":["памперс","подгуз","коляска","игруш","детск","дочка","хадиджа"],
    "Спорт":["зал","спорт","креатин","протеин","гейнер","абонемент"],
    "Прочее":[]
}
def detect_currency(t:str)->str:
    t=t.lower()
    for cur,s in CURRENCY_SIGNS.items():
        if any(x in t for x in s): return cur
    return "uzs"
def parse_amount(t:str)->Optional[float]:
    m=re.findall(r"(?:(?<=\s)|^)(\d{1,3}(?:[ \u00A0,\.]\d{3})+|\d+)(?:[.,](\d{1,2}))?",t)
    if not m: return None
    raw,frac=m[-1]; num=re.sub(r"[ \u00A0,\.]","",raw)
    return float(f"{num}.{frac}") if frac else float(num)
def guess_type(t:str)->str:
    t=t.lower()
    if any(w in t for w in ["зарплата","премия","бонус","получил","пришло","доход"]): return "income"
    if any(w in t for w in ["расход","купил","оплатил","заплатил","потратил","снял"]): return "expense"
    return "expense"
def guess_category(t:str)->str:
    t=t.lower()
    for cat,kws in CATEGORY_KEYWORDS.items():
        if any(k in t for k in kws): return cat
    if any(x in t for x in ["зарплат","прем","бонус"]): return "Зарплата"
    return "Прочее"
def ai_classify_finance(t:str):
    return guess_type(t), parse_amount(t), detect_currency(t), guess_category(t)

def add_tx(uid:int, ttype:str, amount:float, cur:str, cat:str, note:str):
    con=sqlite3.connect(DB_PATH); c=con.cursor()
    c.execute("INSERT INTO tx(user_id,ttype,amount,currency,category,note,ts) VALUES(?,?,?,?,?,?,?)",
              (uid,ttype,amount,cur,cat,note,int(time.time())))
    con.commit(); con.close()
def last_txs(uid:int, limit:int=10):
    con=sqlite3.connect(DB_PATH); c=con.cursor()
    c.execute("SELECT ttype,amount,currency,category,note,ts FROM tx WHERE user_id=? ORDER BY id DESC LIMIT ?",
              (uid,limit))
    rows=c.fetchall(); con.close(); return rows
def get_balance(uid:int):
    con=sqlite3.connect(DB_PATH); c=con.cursor()
    def s(t,cur):
        c.execute("SELECT COALESCE(SUM(amount),0) FROM tx WHERE user_id=? AND ttype=? AND currency=?",(uid,t,cur))
        return c.fetchone()[0]
    bal_uzs=s("income","uzs")-s("expense","uzs")
    bal_usd=s("income","usd")-s("expense","usd"); con.close()
    return bal_uzs, bal_usd

async def start(update:Update, _:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Razzakov’s Finance 🤖\nПиши: «самса 18 000 сум», «такси 25 000», «зарплата 800$».",
        reply_markup=MAIN_KB)

async def text_router(update:Update, _:ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    txt = (update.message.text or "").strip()
    low = txt.lower()
    if "баланс" in low:
        uzs, usd = get_balance(uid)
        await update.message.reply_text(f"Баланс:\n• UZS: {int(uzs):,}".replace(","," ") + f"\n• USD: {usd:.2f}", reply_markup=MAIN_KB); return
    if "история" in low:
        rows = last_txs(uid, 10)
        if not rows:
            await update.message.reply_text("История пуста.", reply_markup=MAIN_KB); return
        lines=[]
        for ttype,amount,cur,cat,note,ts in rows:
            dt=datetime.fromtimestamp(ts).strftime("%d.%m %H:%M")
            sign="➕" if ttype=="income" else "➖"
            lines.append(f"{dt} {sign} {amount:.2f} {cur.upper()} • {cat} • {note or '-'}")
        await update.message.reply_text("Последние операции:\n"+"\n".join(lines), reply_markup=MAIN_KB); return
    if "отчёт" in low or "отчет" in low:
        uzs, usd = get_balance(uid)
        await update.message.reply_text(f"Отчёт (кратко):\n• Баланс UZS: {int(uzs):,}".replace(","," ") + f"\n• Баланс USD: {usd:.2f}", reply_markup=MAIN_KB); return
    ttype, amount, cur, cat = ai_classify_finance(txt)
    if amount is not None:
        add_tx(uid, ttype, amount, cur, cat, txt)
        await update.message.reply_text(f"{'Доход' if ttype=='income' else 'Расход'}: {amount:.2f} {cur.upper()} • {cat}\n✓ Сохранено", reply_markup=MAIN_KB); return
    await update.message.reply_text("Принято ✅ Напиши: «такси 25 000», «зарплата 800$».", reply_markup=MAIN_KB)

async def unknown_cmd(update:Update, _:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Нажми кнопку или напиши траты/доход.", reply_markup=MAIN_KB)

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_cmd))
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=BOT_TOKEN,
        webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}",
        drop_pending_updates=True
    )

if __name__ == "__main__":
    main()

