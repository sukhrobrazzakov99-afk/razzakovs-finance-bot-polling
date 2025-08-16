# -*- coding: utf-8 -*-
import re
from datetime import datetime, timedelta

UZS_WORDS = ["uzs", "сум", "sum", "so'm", "сом", "soums", "сумы"]
USD_WORDS = ["usd", "$", "доллар", "бакс", "bucks", "dollar"]

# Синонимы для авто-категорий
EXPENSE_SYNONYMS = {
    "Еда": ["еда", "обед", "ужин", "завтрак", "food", "кафе", "ресторан", "продукт"],
    "Транспорт": ["такси", "транспорт", "авто", "бензин", "метро", "автобус", "трамвай", "троллейбус"],
    "Жильё": ["аренда", "квартира", "коммунал", "комуслуги", "жкх", "дом"],
    "Связь/интернет": ["связь", "интернет", "инет", "телефон", "мобайл", "скорость"],
    "Здоровье": ["аптека", "здоровье", "лекар", "врач", "стомат", "клиника"],
    "Одежда": ["одежда", "обувь", "шмот", "пальто", "куртка"],
    "Развлечения": ["кино", "фильм", "театр", "игры", "развлеч"],
    "Образование": ["курс", "обучение", "учёба", "образован", "школа"],
    "Подарки": ["подар", "сувенир"],
    "Другое": [],
}
INCOME_SYNONYMS = {
    "Зарплата": ["зарплат", "оклад", "зп", "salary", "payroll"],
    "Бонус": ["бонус", "премия", "преми"],
    "Подарок": ["подар", "gift"],
    "Другое": [],
}

def _find_amount(text: str):
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
        if w in t: return "USD"
    for w in UZS_WORDS:
        if w in t: return "UZS"
    return "UZS"

def _guess_category(text: str, mode: str):
    t = (text or "").lower()
    table = INCOME_SYNONYMS if mode == "income" else EXPENSE_SYNONYMS
    for cat, keys in table.items():
        for k in keys:
            if k and k in t:
                return cat
    return None

def parse_free_text(text: str):
    amt = _find_amount(text)
    cur = _find_currency(text)
    # если есть +/доход — считаем доходом, иначе расход
    mode = "income" if any(x in (text or "").lower() for x in ["доход", "+", "прибыл", "зарплат"]) else "expense"
    cat = _guess_category(text, mode)
    note = text
    return {"amount": amt, "currency": cur, "mode": mode, "category": cat, "note": note}

def parse_due(s: str):
    t = (s or "").strip().lower()
    if t in ("сегодня", "today"):
        d = datetime.now()
        return int(datetime(d.year, d.month, d.day).timestamp()*1000)
    if t in ("завтра", "tomorrow"):
        d = datetime.now() + timedelta(days=1)
        return int(datetime(d.year, d.month, d.day).timestamp()*1000)
    m = re.match(r"(\d{1,2})[./](\d{1,2})[./](\d{2,4})", t)
    if m:
        dd, mm, yy = map(int, m.groups())
        if yy < 100: yy += 2000
        try:
            return int(datetime(yy, mm, dd).timestamp()*1000)
        except:
            return None
    return None

    return None

