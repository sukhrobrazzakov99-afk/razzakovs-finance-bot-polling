
# -*- coding: utf-8 -*-
import sqlite3, time

class DB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init()

    def _init(self):
        cur = self.conn.cursor()
        cur.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS allowed_users(
            user_id INTEGER PRIMARY KEY
        );
        CREATE TABLE IF NOT EXISTS transactions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            type TEXT CHECK(type IN ('income','expense')) NOT NULL,
            amount REAL NOT NULL,
            currency TEXT NOT NULL,
            category TEXT,
            note TEXT,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS debts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('receivable','payable')),
            cp_name TEXT NOT NULL,
            amount REAL NOT NULL,
            currency TEXT NOT NULL,
            note TEXT,
            due_date INTEGER,
            status TEXT NOT NULL DEFAULT 'open',
            created_at INTEGER NOT NULL,
            paid_at INTEGER
        );
        """)
        self.conn.commit()

    # --- allowlist ---
    def allow(self, user_id: int):
        self.conn.execute("INSERT OR IGNORE INTO allowed_users(user_id) VALUES(?)", (user_id,))
        self.conn.commit()

    def is_allowed(self, user_id: int) -> bool:
        r = self.conn.execute("SELECT 1 FROM allowed_users WHERE user_id=?", (user_id,)).fetchone()
        return bool(r)

    def allow_count(self) -> int:
        r = self.conn.execute("SELECT COUNT(*) c FROM allowed_users").fetchone()
        return int(r["c"])

    def list_allowed_ids(self):
        return [row["user_id"] for row in self.conn.execute("SELECT user_id FROM allowed_users")]

    # --- transactions ---
    def add_tx(self, user_id, type_, amount, currency, category, note):
        self.conn.execute(
            "INSERT INTO transactions(user_id,type,amount,currency,category,note,created_at) VALUES(?,?,?,?,?,?,?)",
            (user_id, type_, amount, currency, category, note, int(time.time()*1000))
        )
        self.conn.commit()

    def last_tx(self, user_id, limit=20):
        return self.conn.execute(
            "SELECT * FROM transactions WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit)
        ).fetchall()

    def get_balance(self, user_id):
        inc = self.conn.execute(
            "SELECT COALESCE(SUM(amount),0) s FROM transactions WHERE user_id=? AND type='income'",
            (user_id,)
        ).fetchone()["s"]
        exp = self.conn.execute(
            "SELECT COALESCE(SUM(amount),0) s FROM transactions WHERE user_id=? AND type='expense'",
            (user_id,)
        ).fetchone()["s"]
        return float(inc or 0), float(exp or 0), float((inc or 0) - (exp or 0))

    # --- debts ---
    def add_debt(self, user_id, kind, cp_name, amount, currency, note, due_ts):
        self.conn.execute(
            "INSERT INTO debts(user_id,kind,cp_name,amount,currency,note,due_date,status,created_at) VALUES(?,?,?,?,?,?,?, 'open', ?)",
            (user_id, kind, cp_name, amount, currency, note, int(due_ts) if due_ts else None, int(time.time()*1000))
        )
        self.conn.commit()

    def open_debts(self, user_id, kind):
        return self.conn.execute(
            "SELECT * FROM debts WHERE user_id=? AND kind=? AND status='open' ORDER BY COALESCE(due_date, 9999999999999), id",
            (user_id, kind)
        ).fetchall()

    def close_debt(self, user_id, debt_id):
        cur = self.conn.execute(
            "UPDATE debts SET status='closed', paid_at=? WHERE user_id=? AND id=? AND status='open'",
            (int(time.time()*1000), user_id, debt_id)
        )
        self.conn.commit()
        return cur.rowcount > 0

    def debt_total(self, user_id, kind):
        r = self.conn.execute(
            "SELECT COALESCE(SUM(amount),0) s FROM debts WHERE user_id=? AND kind=? AND status='open'",
            (user_id, kind)
        ).fetchone()
        return float(r["s"] or 0)

    def get_debt_totals(self, user_id):
        rec = self.debt_total(user_id, 'receivable')
        pay = self.debt_total(user_id, 'payable')
        return rec, pay

    def overdue_debts(self, user_id):
        now = int(time.time()*1000)
        return self.conn.execute(
            "SELECT * FROM debts WHERE user_id=? AND status='open' AND due_date IS NOT NULL AND due_date < ? ORDER BY due_date",
            (user_id, now)
        ).fetchall()
