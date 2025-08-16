
import re

UZS_WORDS = ["uzs", "сум", "sum", "so'm", "сом", "soums", "сумы"]
USD_WORDS = ["usd", "$", "доллар", "бакс", "bucks", "dollar"]

def _find_amount(text: str):
    import re
    m = re.search(r"(\d+(?:[ \u00A0,]\d{3})*(?:[.,]\d+)?)", text or "")
    if not m:
        return None
    raw = m.group(1).replace("\u00A0", " ").replace(" ", "").replace(",", "")
    try:
        return float(raw)
    except:
        return None

def _find_currency(text: str):
    t = (text or "").lower()
    for w in USD_WORDS:
        if w in t:
            return "USD"
    for w in UZS_WORDS:
        if w in t:
            return "UZS"
    return None

def parse_free_text(text: str):
    amount = _find_amount(text or "")
    currency = _find_currency(text or "")
    desc = text
    return {"amount": amount, "currency": currency, "desc": desc} if amount and currency else None

def ai_answer(text: str) -> str:
    t = (text or "").lower()
    if any(w in t for w in ["еда", "продукт", "обед", "ужин", "завтрак", "кафе"]):
        return "Категория: еда/кафе. Совет: держите дневной лимит и сверяйте недельный расход."
    if any(w in t for w in ["такси", "топливо", "бензин", "авто", "транспорт"]):
        return "Категория: транспорт. Совет: фиксируйте маршруты и сравнивайте расходы по неделям."
    if any(w in t for w in ["зарплат", "выручк", "доход"]):
        return "Категория: доход. Совет: часть дохода сразу откладывайте (10–20%)."
    return "Готово. Записи фиксируйте через кнопки или вводите одной строкой: 'еда 150000 uzs'."
