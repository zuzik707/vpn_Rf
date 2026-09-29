"""
SQLite хранилище VFS аккаунтов.
Таблица accounts: email, password, proxy, enabled, added_at, last_used, fail_count
"""

import os
import sqlite3
import time
import logging

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "accounts.db")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            email       TEXT PRIMARY KEY,
            password    TEXT NOT NULL,
            proxy       TEXT DEFAULT '',
            enabled     INTEGER DEFAULT 1,
            added_at    REAL DEFAULT 0,
            last_used   REAL DEFAULT 0,
            fail_count  INTEGER DEFAULT 0,
            notes       TEXT DEFAULT '',
            mail_password TEXT DEFAULT ''
        )
    """)
    # Migration: add mail_password if table exists without it
    try:
        conn.execute("ALTER TABLE accounts ADD COLUMN mail_password TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    return conn


def add_account(email: str, password: str, proxy: str = "", notes: str = "",
                mail_password: str = "") -> bool:
    conn = _conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO accounts "
            "(email, password, proxy, enabled, added_at, notes, mail_password) "
            "VALUES (?, ?, ?, 1, ?, ?, ?)",
            (email.strip().lower(), password.strip(), proxy.strip(),
             time.time(), notes, mail_password),
        )
        conn.commit()
        logger.info("Account added: %s", email)
        return True
    except Exception as e:
        logger.error("add_account error: %s", e)
        return False
    finally:
        conn.close()


def remove_account(email: str) -> bool:
    conn = _conn()
    try:
        cur = conn.execute("DELETE FROM accounts WHERE email = ?", (email.strip().lower(),))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def toggle_account(email: str, enabled: bool) -> bool:
    conn = _conn()
    try:
        cur = conn.execute(
            "UPDATE accounts SET enabled = ? WHERE email = ?",
            (1 if enabled else 0, email.strip().lower()),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_enabled_accounts() -> list[dict]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT email, password, proxy, fail_count, last_used, added_at, mail_password "
            "FROM accounts WHERE enabled = 1 ORDER BY last_used ASC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_all_accounts() -> list[dict]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT email, password, proxy, enabled, fail_count, last_used, added_at, notes "
            "FROM accounts ORDER BY added_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_last_used(email: str) -> None:
    conn = _conn()
    try:
        conn.execute("UPDATE accounts SET last_used = ? WHERE email = ?", (time.time(), email.lower()))
        conn.commit()
    finally:
        conn.close()


def increment_fails(email: str) -> int:
    conn = _conn()
    try:
        conn.execute(
            "UPDATE accounts SET fail_count = fail_count + 1 WHERE email = ?",
            (email.lower(),),
        )
        conn.commit()
        row = conn.execute("SELECT fail_count FROM accounts WHERE email = ?", (email.lower(),)).fetchone()
        return row["fail_count"] if row else 0
    finally:
        conn.close()


def reset_fails(email: str) -> None:
    conn = _conn()
    try:
        conn.execute("UPDATE accounts SET fail_count = 0 WHERE email = ?", (email.lower(),))
        conn.commit()
    finally:
        conn.close()


def ban_account(email: str, reason: str = "") -> bool:
    """Отключает аккаунт и записывает причину бана в notes."""
    conn = _conn()
    try:
        ban_time = time.strftime("%Y-%m-%d %H:%M", time.localtime())
        note = f"BANNED {ban_time}: {reason[:100]}"
        cur = conn.execute(
            "UPDATE accounts SET enabled = 0, notes = ? WHERE email = ?",
            (note, email.strip().lower()),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_banned_accounts() -> list[dict]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT email, notes, added_at "
            "FROM accounts WHERE enabled = 0 AND notes LIKE 'BANNED%' "
            "ORDER BY added_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def count_accounts(enabled_only: bool = True) -> int:
    conn = _conn()
    try:
        if enabled_only:
            row = conn.execute("SELECT COUNT(*) as c FROM accounts WHERE enabled = 1").fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) as c FROM accounts").fetchone()
        return row["c"]
    finally:
        conn.close()


def import_from_env(accounts_list: list[dict]) -> int:
    """Импортирует аккаунты из Config.VFS_ACCOUNTS (из .env) в БД, если их там нет."""
    added = 0
    conn = _conn()
    try:
        for acct in accounts_list:
            email = acct["email"].strip().lower()
            existing = conn.execute("SELECT 1 FROM accounts WHERE email = ?", (email,)).fetchone()
            if not existing:
                conn.execute(
                    "INSERT INTO accounts (email, password, proxy, enabled, added_at, notes) "
                    "VALUES (?, ?, ?, 1, ?, ?)",
                    (email, acct["password"], acct.get("proxy", ""), time.time(), "imported from .env"),
                )
                added += 1
        conn.commit()
    finally:
        conn.close()
    return added
