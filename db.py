
import sqlite3
from pathlib import Path
import pandas as pd

DB_PATH = Path("data.sqlite3")

def get_conn():
    return sqlite3.connect(DB_PATH)

def init_db():
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS operations(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                kind TEXT CHECK(kind IN ('income','expense')),
                amount REAL,
                currency TEXT CHECK(currency IN ('UZS','USD')),
                description TEXT,
                ts TEXT
            )
        """ )
        conn.commit()

def add_operation(user_id: int, kind: str, amount: float, currency: str, description: str, ts: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO operations(user_id,kind,amount,currency,description,ts) VALUES(?,?,?,?,?,?)",
            (user_id, kind, float(amount), currency, description, ts),
        )
        conn.commit()

def get_recent(limit: int = 20):
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT * FROM operations ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in cur.fetchall()]

def get_balance():
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute("""
            SELECT
              currency,
              SUM(CASE WHEN kind='income' THEN amount ELSE -amount END) AS total
            FROM operations
            GROUP BY currency
        """ )
        res = {r["currency"]: r["total"] or 0 for r in cur.fetchall()}
        for cur in ("UZS","USD"):
            res.setdefault(cur, 0)
        return res

def get_dataframe(limit: int = 1000) -> pd.DataFrame:
    with get_conn() as conn:
        df = pd.read_sql_query(
            "SELECT id, ts, kind, amount, currency, description, user_id FROM operations ORDER BY id DESC LIMIT ?",
            conn,
            params=(limit,)
        )
    return df
