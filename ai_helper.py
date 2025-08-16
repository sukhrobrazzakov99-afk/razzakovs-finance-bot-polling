# -*- coding: utf-8 -*-
import re
from datetime import datetime, timedelta

UZS_WORDS = ["uzs", "сум", "sum", "so'm", "сом", "soums", "сумы"]
USD_WORDS = ["usd", "$", "доллар", "бакс", "bucks", "dollar"]

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

def parse_free_text(text: str):
    amt = _find_amount(text)
    cur = _find_currency(text)
    mode = "income" if any(x in (text or "").lower() for x in ["доход", "+", "прибыл"]) else "expense"
    note = text
    return {"amount": amt, "currency": cur, "mode": mode, "note": note}

def parse_due(s: str):
    t = (s or "").strip().lower()
    if t in ("сегодня", "today"):
        d = datetime.now()
        return int(datetime(d.year, d.month, d.day).timestamp() * 1000)
    if t in ("завтра", "tomorrow"):
        d = datetime.now() + timedelta(days=1)
        return int(datetime(d.year, d.month, d.day).timestamp() * 1000)
    m = re.match(r"(\d{1,2})[./](\d{1,2})[./](\d{2,4})", t)
    if m:
        dd, mm, yy = map(int, m.groups())
        if yy < 100: yy += 2000
        try:
            return int(datetime(yy, mm, dd).timestamp() * 1000)
        except:
            return None
    return None

