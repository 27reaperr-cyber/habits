#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram Task & Habit Tracker Bot
Full-featured: tasks, calendar, streaks, categories, automation, admin panel
"""

import asyncio
import calendar
import io
import logging
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime, date, timedelta
from typing import Optional, List, Dict

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from timezonefinder import TimezoneFinder
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    BotCommand
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)

load_dotenv()

# ──────────────────────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────────────────────
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
ADMIN_IDS: List[int] = [
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
]
DB_PATH: str = os.getenv("DB_PATH", "data/bot.db")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

tf = TimezoneFinder()
scheduler = AsyncIOScheduler(timezone="UTC")
_app: Optional[Application] = None  # global app ref for scheduler

# Conversation states stored in user_data["state"]
(
    ST_ADD_CATEGORY,
    ST_ADD_TASK,
    ST_RESET_REASON,
    ST_AUTO_TIME,
    ST_LINK_CHANNEL,
    ST_ADMIN_GRANT,
    ST_IMPORT_DB,
    ST_TZ_TEXT,
) = range(8)


# ──────────────────────────────────────────────────────────────
# DATABASE INIT & HELPERS
# ──────────────────────────────────────────────────────────────
def _ensure_dir():
    d = os.path.dirname(DB_PATH)
    if d:
        os.makedirs(d, exist_ok=True)


def get_db() -> sqlite3.Connection:
    _ensure_dir()
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    _ensure_dir()
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id    INTEGER PRIMARY KEY,
            username   TEXT    DEFAULT '',
            first_name TEXT    DEFAULT '',
            timezone   TEXT    DEFAULT 'UTC',
            has_access INTEGER DEFAULT 0,
            created_at TEXT    DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS categories (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id  INTEGER NOT NULL,
            name     TEXT    NOT NULL,
            position INTEGER DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS tasks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            category_id INTEGER NOT NULL,
            user_id     INTEGER NOT NULL,
            name        TEXT    NOT NULL,
            is_active   INTEGER DEFAULT 1,
            created_at  TEXT    DEFAULT (datetime('now')),
            FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id)     REFERENCES users(user_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS task_logs (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id   INTEGER NOT NULL,
            user_id   INTEGER NOT NULL,
            log_date  TEXT    NOT NULL,
            completed INTEGER DEFAULT 0,
            UNIQUE(task_id, log_date),
            FOREIGN KEY (task_id) REFERENCES tasks(id)      ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS resets (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            reason      TEXT    NOT NULL,
            tasks_count INTEGER DEFAULT 0,
            reset_at    TEXT    DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS automations (
            user_id           INTEGER PRIMARY KEY,
            is_enabled        INTEGER DEFAULT 0,
            send_time         TEXT    DEFAULT '09:00',
            linked_chat_id    INTEGER,
            linked_chat_title TEXT    DEFAULT '',
            FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
        );
    """)
    conn.commit()
    conn.close()
    logger.info("DB ready at %s", DB_PATH)


# ── User ──────────────────────────────────────────────────────
def db_ensure_user(uid: int, username: str, first_name: str):
    conn = get_db()
    conn.execute("""
        INSERT INTO users (user_id, username, first_name)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username   = excluded.username,
            first_name = excluded.first_name
    """, (uid, username or "", first_name or ""))
    conn.commit()
    conn.close()


def db_has_access(uid: int) -> bool:
    if uid in ADMIN_IDS:
        return True
    conn = get_db()
    row = conn.execute("SELECT has_access FROM users WHERE user_id=?", (uid,)).fetchone()
    conn.close()
    return bool(row and row["has_access"])


def db_get_tz(uid: int) -> pytz.BaseTzInfo:
    conn = get_db()
    row = conn.execute("SELECT timezone FROM users WHERE user_id=?", (uid,)).fetchone()
    conn.close()
    try:
        return pytz.timezone(row["timezone"] if row else "UTC")
    except Exception:
        return pytz.UTC


def db_today(uid: int) -> str:
    return datetime.now(db_get_tz(uid)).strftime("%Y-%m-%d")


def db_set_tz(uid: int, tz_str: str):
    conn = get_db()
    conn.execute("UPDATE users SET timezone=? WHERE user_id=?", (tz_str, uid))
    conn.commit()
    conn.close()


def db_all_users() -> List[sqlite3.Row]:
    conn = get_db()
    rows = conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
    conn.close()
    return rows


def db_grant_access(uid: int, grant: bool = True):
    conn = get_db()
    conn.execute("UPDATE users SET has_access=? WHERE user_id=?", (1 if grant else 0, uid))
    conn.commit()
    conn.close()


# ── Categories ────────────────────────────────────────────────
def db_categories(uid: int) -> List[sqlite3.Row]:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM categories WHERE user_id=? ORDER BY position, id", (uid,)
    ).fetchall()
    conn.close()
    return rows


def db_add_category(uid: int, name: str) -> int:
    conn = get_db()
    cur = conn.execute("INSERT INTO categories (user_id, name) VALUES (?,?)", (uid, name))
    cid = cur.lastrowid
    conn.commit()
    conn.close()
    return cid


def db_del_category(cid: int, uid: int):
    conn = get_db()
    conn.execute("DELETE FROM categories WHERE id=? AND user_id=?", (cid, uid))
    conn.commit()
    conn.close()


def db_get_category(cid: int, uid: int) -> Optional[sqlite3.Row]:
    conn = get_db()
    row = conn.execute("SELECT * FROM categories WHERE id=? AND user_id=?", (cid, uid)).fetchone()
    conn.close()
    return row


# ── Tasks ─────────────────────────────────────────────────────
def db_tasks(cid: int, uid: int) -> List[sqlite3.Row]:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM tasks WHERE category_id=? AND user_id=? ORDER BY id", (cid, uid)
    ).fetchall()
    conn.close()
    return rows


def db_all_active_tasks(uid: int) -> List[sqlite3.Row]:
    conn = get_db()
    rows = conn.execute("""
        SELECT t.*, c.name AS cat_name
        FROM tasks t JOIN categories c ON t.category_id=c.id
        WHERE t.user_id=? AND t.is_active=1
        ORDER BY c.name, t.id
    """, (uid,)).fetchall()
    conn.close()
    return rows


def db_add_task(cid: int, uid: int, name: str) -> int:
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO tasks (category_id, user_id, name) VALUES (?,?,?)", (cid, uid, name)
    )
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    return tid


def db_del_task(tid: int, uid: int):
    conn = get_db()
    conn.execute("DELETE FROM tasks WHERE id=? AND user_id=?", (tid, uid))
    conn.commit()
    conn.close()


def db_toggle_task_active(tid: int, uid: int):
    conn = get_db()
    conn.execute(
        "UPDATE tasks SET is_active=1-is_active WHERE id=? AND user_id=?", (tid, uid)
    )
    conn.commit()
    conn.close()


# ── Task Logs ─────────────────────────────────────────────────
def db_toggle_log(tid: int, uid: int, log_date: str) -> bool:
    """Toggle completion, return new state."""
    conn = get_db()
    row = conn.execute(
        "SELECT completed FROM task_logs WHERE task_id=? AND log_date=?", (tid, log_date)
    ).fetchone()
    if row:
        new = 1 - row["completed"]
        conn.execute(
            "UPDATE task_logs SET completed=? WHERE task_id=? AND log_date=?",
            (new, tid, log_date)
        )
    else:
        new = 1
        conn.execute(
            "INSERT INTO task_logs (task_id, user_id, log_date, completed) VALUES (?,?,?,1)",
            (tid, uid, log_date)
        )
    conn.commit()
    conn.close()
    return bool(new)


def db_day_logs(uid: int, log_date: str) -> Dict[int, int]:
    conn = get_db()
    rows = conn.execute(
        "SELECT task_id, completed FROM task_logs WHERE user_id=? AND log_date=?",
        (uid, log_date)
    ).fetchall()
    conn.close()
    return {r["task_id"]: r["completed"] for r in rows}


def db_streak(tid: int, uid: int, today_str: str) -> int:
    conn = get_db()
    rows = conn.execute("""
        SELECT log_date FROM task_logs
        WHERE task_id=? AND user_id=? AND completed=1
        ORDER BY log_date DESC
    """, (tid, uid)).fetchall()
    conn.close()
    if not rows:
        return 0
    today_dt = date.fromisoformat(today_str)
    dates = [date.fromisoformat(r["log_date"]) for r in rows]
    if dates[0] < today_dt - timedelta(days=1):
        return 0
    expected = today_dt if dates[0] == today_dt else today_dt - timedelta(days=1)
    streak = 0
    for d in dates:
        if d == expected:
            streak += 1
            expected -= timedelta(days=1)
        else:
            break
    return streak


# ── Resets ────────────────────────────────────────────────────
def db_reset_all(uid: int, reason: str):
    conn = get_db()
    cnt = conn.execute(
        "SELECT COUNT(*) AS c FROM tasks WHERE user_id=?", (uid,)
    ).fetchone()["c"]
    conn.execute(
        "INSERT INTO resets (user_id, reason, tasks_count) VALUES (?,?,?)",
        (uid, reason, cnt)
    )
    conn.execute("DELETE FROM tasks WHERE user_id=?", (uid,))
    conn.execute("DELETE FROM categories WHERE user_id=?", (uid,))
    conn.execute("DELETE FROM task_logs WHERE user_id=?", (uid,))
    conn.commit()
    conn.close()


def db_reset_history(uid: int) -> List[sqlite3.Row]:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM resets WHERE user_id=? ORDER BY reset_at DESC LIMIT 10", (uid,)
    ).fetchall()
    conn.close()
    return rows


# ── Automation ────────────────────────────────────────────────
def db_get_auto(uid: int) -> Optional[sqlite3.Row]:
    conn = get_db()
    row = conn.execute("SELECT * FROM automations WHERE user_id=?", (uid,)).fetchone()
    conn.close()
    return row


def db_set_auto(uid: int, **kw):
    conn = get_db()
    if conn.execute("SELECT user_id FROM automations WHERE user_id=?", (uid,)).fetchone():
        sets = ", ".join(f"{k}=?" for k in kw)
        conn.execute(f"UPDATE automations SET {sets} WHERE user_id=?", (*kw.values(), uid))
    else:
        cols = ", ".join(["user_id"] + list(kw.keys()))
        vals = ", ".join(["?"] * (1 + len(kw)))
        conn.execute(f"INSERT INTO automations ({cols}) VALUES ({vals})", (uid, *kw.values()))
    conn.commit()
    conn.close()


# ──────────────────────────────────────────────────────────────
# REPORT BUILDER
# ──────────────────────────────────────────────────────────────
def build_report(uid: int, report_date: Optional[str] = None) -> str:
    today = report_date or db_today(uid)
    cats = db_categories(uid)
    logs = db_day_logs(uid, today)

    if not cats:
        return f"📊 *Отчёт за {today}*\n\nЗадачи ещё не добавлены."

    lines = [f"📊 *Отчёт за {today}*\n"]
    total = done = 0

    for cat in cats:
        tasks = [t for t in db_tasks(cat["id"], uid) if t["is_active"]]
        if not tasks:
            continue
        lines.append(f"\n📁 *{cat['name']}*")
        for t in tasks:
            total += 1
            comp = logs.get(t["id"], 0)
            streak = db_streak(t["id"], uid, today)
            icon = "✅" if comp else "❌"
            suf = f" 🔥{streak}" if streak > 0 else ""
            lines.append(f"  {icon} {t['name']}{suf}")
            if comp:
                done += 1

    pct = int(done / total * 100) if total else 0
    bar = "▓" * (pct // 10) + "░" * (10 - pct // 10)
    lines.append(f"\n`[{bar}]` {done}/{total} ({pct}%)")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────
# KEYBOARD BUILDERS
# ──────────────────────────────────────────────────────────────
def kb_main(uid: int) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("📋 Задачи сегодня", callback_data="today"),
            InlineKeyboardButton("📅 Календарь",      callback_data="cal:0"),
        ],
        [
            InlineKeyboardButton("📊 Отчёты",    callback_data="reports"),
            InlineKeyboardButton("⚙️ Настройки", callback_data="settings"),
        ],
        [InlineKeyboardButton("🔄 Сбросить всё", callback_data="reset_ask")],
    ]
    if uid in ADMIN_IDS:
        rows.append([InlineKeyboardButton("👑 Админ панель", callback_data="admin")])
    return InlineKeyboardMarkup(rows)


def kb_back(to: str = "main") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data=to)]])


def kb_settings() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🌍 Часовой пояс",    callback_data="tz_ask"),
            InlineKeyboardButton("⏰ Автоматизация",   callback_data="auto"),
        ],
        [
            InlineKeyboardButton("📂 Категории",        callback_data="cats"),
            InlineKeyboardButton("📢 Привязать канал",  callback_data="link_ask"),
        ],
        [InlineKeyboardButton("🔙 Назад", callback_data="main")],
    ])


def kb_auto(uid: int) -> InlineKeyboardMarkup:
    a = db_get_auto(uid)
    on = a and a["is_enabled"]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔴 Выключить" if on else "🟢 Включить", callback_data="auto_toggle")],
        [InlineKeyboardButton("⏰ Изменить время", callback_data="auto_time")],
        [InlineKeyboardButton("🔙 Назад", callback_data="settings")],
    ])


def kb_cats(uid: int) -> InlineKeyboardMarkup:
    rows = []
    for c in db_categories(uid):
        rows.append([
            InlineKeyboardButton(f"📁 {c['name']}", callback_data=f"cat:{c['id']}"),
            InlineKeyboardButton("🗑️", callback_data=f"cat_del:{c['id']}"),
        ])
    rows.append([InlineKeyboardButton("➕ Новая категория", callback_data="cat_add")])
    rows.append([InlineKeyboardButton("🔙 Назад", callback_data="settings")])
    return InlineKeyboardMarkup(rows)


def kb_tasks_list(cid: int, uid: int) -> InlineKeyboardMarkup:
    rows = []
    for t in db_tasks(cid, uid):
        st = "✅" if t["is_active"] else "⏸️"
        rows.append([
            InlineKeyboardButton(f"{st} {t['name']}", callback_data=f"task_act:{t['id']}:{cid}"),
            InlineKeyboardButton("🗑️", callback_data=f"task_del:{t['id']}:{cid}"),
        ])
    rows.append([InlineKeyboardButton("➕ Новая задача", callback_data=f"task_add:{cid}")])
    rows.append([InlineKeyboardButton("🔙 Назад", callback_data="cats")])
    return InlineKeyboardMarkup(rows)


def kb_today(uid: int, log_date: str) -> InlineKeyboardMarkup:
    cats = db_categories(uid)
    logs = db_day_logs(uid, log_date)
    rows = []
    for cat in cats:
        tasks = [t for t in db_tasks(cat["id"], uid) if t["is_active"]]
        if not tasks:
            continue
        rows.append([InlineKeyboardButton(f"━━ 📁 {cat['name']} ━━", callback_data="noop")])
        for t in tasks:
            comp = logs.get(t["id"], 0)
            streak = db_streak(t["id"], uid, log_date)
            icon = "✅" if comp else "⬜"
            suf = f" 🔥{streak}" if streak else ""
            rows.append([InlineKeyboardButton(
                f"{icon} {t['name']}{suf}",
                callback_data=f"tlog:{t['id']}:{log_date}"
            )])
    if not rows:
        rows.append([InlineKeyboardButton("Задачи не добавлены", callback_data="cats")])
    rows.append([InlineKeyboardButton("🔙 Главное меню", callback_data="main")])
    return InlineKeyboardMarkup(rows)


def kb_calendar(uid: int, offset: int = 0) -> InlineKeyboardMarkup:
    tz = db_get_tz(uid)
    now = datetime.now(tz)
    # calculate target month
    month_offset = now.month - 1 + offset
    year = now.year + month_offset // 12
    month = month_offset % 12 + 1
    target = date(year, month, 1)

    rows = [
        [InlineKeyboardButton(f"📅 {target.strftime('%B %Y')}", callback_data="noop")],
        [InlineKeyboardButton(d, callback_data="noop") for d in ["Пн","Вт","Ср","Чт","Пт","Сб","Вс"]],
    ]

    today_str = now.strftime("%Y-%m-%d")
    conn = get_db()

    for week in calendar.monthcalendar(year, month):
        row = []
        for dn in week:
            if dn == 0:
                row.append(InlineKeyboardButton(" ", callback_data="noop"))
            else:
                d = date(year, month, dn)
                ds = d.isoformat()
                r = conn.execute("""
                    SELECT COUNT(*) AS total,
                           COALESCE(SUM(tl.completed),0) AS done
                    FROM tasks t
                    LEFT JOIN task_logs tl ON tl.task_id=t.id AND tl.log_date=?
                    WHERE t.user_id=? AND t.is_active=1
                """, (ds, uid)).fetchone()
                total, done = r["total"], r["done"]

                if ds == today_str:
                    pfx = "🔵"
                elif total > 0 and done == total:
                    pfx = "✅"
                elif done > 0:
                    pfx = "🟡"
                elif d < date.fromisoformat(today_str) and total > 0:
                    pfx = "❌"
                else:
                    pfx = "⬜"

                row.append(InlineKeyboardButton(f"{pfx}{dn}", callback_data=f"cday:{ds}"))
        rows.append(row)

    conn.close()

    nav = []
    nav.append(InlineKeyboardButton("◀️", callback_data=f"cal:{offset-1}"))
    nav.append(InlineKeyboardButton("Сегодня", callback_data="cal:0"))
    nav.append(InlineKeyboardButton("▶️", callback_data=f"cal:{offset+1}"))
    rows.append(nav)
    rows.append([InlineKeyboardButton("🔙 Главное меню", callback_data="main")])
    return InlineKeyboardMarkup(rows)


def kb_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👥 Пользователи",   callback_data="admin_users"),
            InlineKeyboardButton("📤 Экспорт БД",     callback_data="admin_export"),
        ],
        [InlineKeyboardButton("📥 Импорт БД", callback_data="admin_import")],
        [InlineKeyboardButton("🔙 Назад",     callback_data="main")],
    ])


# ──────────────────────────────────────────────────────────────
# ACCESS GUARD
# ──────────────────────────────────────────────────────────────
def guard(fn):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE, *a, **kw):
        user = update.effective_user
        if user:
            db_ensure_user(user.id, user.username or "", user.first_name or "")
        if not user or not db_has_access(user.id):
            msg = "⛔ Нет доступа. Обратитесь к администратору."
            if update.callback_query:
                await update.callback_query.answer(msg, show_alert=True)
            elif update.message:
                await update.message.reply_text(msg)
            return
        return await fn(update, ctx, *a, **kw)
    wrapper.__name__ = fn.__name__
    return wrapper


async def safe_edit(q, text: str, kb=None):
    try:
        await q.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.debug("edit failed: %s", e)


# ──────────────────────────────────────────────────────────────
# COMMAND HANDLERS
# ──────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_ensure_user(user.id, user.username or "", user.first_name or "")
    if not db_has_access(user.id):
        await update.message.reply_text(
            "👋 Привет!\n\nДля использования бота нужен доступ от администратора.\n"
            "Сообщи администратору свой Telegram ID: `" + str(user.id) + "`",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    await update.message.reply_text(
        f"👋 Привет, *{user.first_name}*!\n\n"
        "🗂 Трекер задач и привычек. Выбери действие:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb_main(user.id)
    )


async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not db_has_access(update.effective_user.id):
        return
    await update.message.reply_text(
        "🏠 *Главное меню*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb_main(update.effective_user.id)
    )


# ──────────────────────────────────────────────────────────────
# CALLBACK HANDLERS
# ──────────────────────────────────────────────────────────────
@guard
async def cb_main(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await safe_edit(q, "🏠 *Главное меню*\n\nВыбери действие:", kb_main(q.from_user.id))


@guard
async def cb_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    today = db_today(uid)
    await safe_edit(
        q,
        f"📋 *Задачи на {today}*\n\nОтметь выполненные задачи:",
        kb_today(uid, today)
    )


@guard
async def cb_tlog(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, tid_s, log_date = q.data.split(":")
    uid = q.from_user.id
    db_toggle_log(int(tid_s), uid, log_date)
    await safe_edit(
        q,
        f"📋 *Задачи на {log_date}*\n\nОтметь выполненные задачи:",
        kb_today(uid, log_date)
    )


@guard
async def cb_cal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    offset = int(q.data.split(":")[1])
    await safe_edit(q, "📅 *Календарь*\n\nВыбери день:", kb_calendar(q.from_user.id, offset))


@guard
async def cb_cday(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ds = q.data.split(":")[1]
    uid = q.from_user.id
    await safe_edit(q, f"📋 *Задачи на {ds}*\n\nОтметь выполненные задачи:", kb_today(uid, ds))


# ── Reports ───────────────────────────────────────────────────
@guard
async def cb_reports(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    text = build_report(uid)

    resets = db_reset_history(uid)
    if resets:
        text += "\n\n📜 *История сбросов:*"
        for r in resets[:5]:
            text += f"\n• `{r['reset_at'][:10]}` — {r['reason']} ({r['tasks_count']} задач)"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📈 Отчёт за 7 дней", callback_data="report7")],
        [InlineKeyboardButton("🔙 Назад",           callback_data="main")],
    ])
    await safe_edit(q, text, kb)


@guard
async def cb_report7(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    tz = db_get_tz(uid)
    today = db_today(uid)
    lines = ["📈 *Отчёт за последние 7 дней*\n"]

    for i in range(7):
        ds = (datetime.now(tz) - timedelta(days=i)).strftime("%Y-%m-%d")
        logs = db_day_logs(uid, ds)
        tasks = db_all_active_tasks(uid)
        total = len(tasks)
        done = sum(1 for t in tasks if logs.get(t["id"], 0))
        pct = int(done / total * 100) if total else 0
        bar = "▓" * (pct // 20) + "░" * (5 - pct // 20)
        day_label = "Сегодня" if i == 0 else ("Вчера" if i == 1 else ds)
        lines.append(f"`{day_label}` [{bar}] {done}/{total}")

    lines.append("\n🔥 *Текущие серии:*")
    for t in db_all_active_tasks(uid):
        s = db_streak(t["id"], uid, today)
        if s > 1:
            lines.append(f"  • {t['name']}: *{s} дн.* подряд")

    await safe_edit(q, "\n".join(lines), kb_back("reports"))


# ── Settings ──────────────────────────────────────────────────
@guard
async def cb_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    tz = db_get_tz(uid)
    a = db_get_auto(uid)
    auto_info = ""
    if a:
        st = "🟢 Вкл" if a["is_enabled"] else "🔴 Выкл"
        ch = f", канал: {a['linked_chat_title']}" if a["linked_chat_title"] else ""
        auto_info = f"\n⏰ Автоматизация: {st} в `{a['send_time']}`{ch}"
    await safe_edit(
        q,
        f"⚙️ *Настройки*\n\n🌍 Часовой пояс: `{tz.zone}`{auto_info}",
        kb_settings()
    )


# ── Timezone ──────────────────────────────────────────────────
@guard
async def cb_tz_ask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await safe_edit(
        q,
        "🌍 *Установка часового пояса*\n\n"
        "📍 Отправь геопозицию (📎 → Местоположение)\n\n"
        "*или* введи название часового пояса:\n"
        "`Europe/Moscow`, `Europe/Berlin`, `Asia/Tokyo`",
        kb_back("settings")
    )
    ctx.user_data["state"] = ST_TZ_TEXT


# ── Automation ────────────────────────────────────────────────
@guard
async def cb_auto(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    a = db_get_auto(uid)
    st = "🟢 Включена" if (a and a["is_enabled"]) else "🔴 Выключена"
    time_s = a["send_time"] if a else "09:00"
    ch = a["linked_chat_title"] if (a and a["linked_chat_title"]) else "не привязан"
    await safe_edit(
        q,
        f"⏰ *Автоматизация*\n\n"
        f"Статус: {st}\n"
        f"Время: `{time_s}`\n"
        f"Канал/группа: {ch}",
        kb_auto(uid)
    )


@guard
async def cb_auto_toggle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    a = db_get_auto(uid)
    new = 0 if (a and a["is_enabled"]) else 1
    db_set_auto(uid, is_enabled=new)
    if new:
        await reschedule(uid)
    else:
        jid = f"daily_{uid}"
        if scheduler.get_job(jid):
            scheduler.remove_job(jid)
    await cb_auto(update, ctx)


@guard
async def cb_auto_time(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await safe_edit(q, "⏰ Введи время в формате `ЧЧ:ММ`\nНапример: `08:30`", kb_back("auto"))
    ctx.user_data["state"] = ST_AUTO_TIME


# ── Categories ────────────────────────────────────────────────
@guard
async def cb_cats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await safe_edit(q, "📂 *Категории*\n\nВыбери или добавь категорию:", kb_cats(q.from_user.id))


@guard
async def cb_cat_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await safe_edit(q, "📁 Введи название новой категории:", kb_back("cats"))
    ctx.user_data["state"] = ST_ADD_CATEGORY


@guard
async def cb_cat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cid = int(q.data.split(":")[1])
    uid = q.from_user.id
    cat = db_get_category(cid, uid)
    if not cat:
        await q.answer("Не найдено", show_alert=True)
        return
    ctx.user_data["cur_cat"] = cid
    await safe_edit(
        q,
        f"📁 *{cat['name']}*\n\nЗадачи в этой категории:",
        kb_tasks_list(cid, uid)
    )


@guard
async def cb_cat_del(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cid = int(q.data.split(":")[1])
    db_del_category(cid, q.from_user.id)
    await safe_edit(q, "📂 *Категории*", kb_cats(q.from_user.id))


@guard
async def cb_task_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cid = int(q.data.split(":")[1])
    ctx.user_data["state"] = ST_ADD_TASK
    ctx.user_data["cur_cat"] = cid
    await safe_edit(q, "✏️ Введи название новой задачи:", kb_back(f"cat:{cid}"))


@guard
async def cb_task_del(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, tid_s, cid_s = q.data.split(":")
    db_del_task(int(tid_s), q.from_user.id)
    await safe_edit(q, "📁 Задачи:", kb_tasks_list(int(cid_s), q.from_user.id))


@guard
async def cb_task_act(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, tid_s, cid_s = q.data.split(":")
    uid = q.from_user.id
    db_toggle_task_active(int(tid_s), uid)
    await safe_edit(q, "📁 Задачи:", kb_tasks_list(int(cid_s), uid))


# ── Link Channel ──────────────────────────────────────────────
@guard
async def cb_link_ask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    a = db_get_auto(uid)
    cur = f"`{a['linked_chat_title']}`" if (a and a["linked_chat_title"]) else "нет"
    await safe_edit(
        q,
        f"📢 *Привязка канала/группы*\n\n"
        f"Текущий: {cur}\n\n"
        "1️⃣ Добавь бота в канал/группу как *администратора*\n"
        "2️⃣ Перешли любое сообщение оттуда сюда\n"
        "   *или* введи числовой ID чата (`-100...`)",
        kb_back("settings")
    )
    ctx.user_data["state"] = ST_LINK_CHANNEL


# ── Reset ─────────────────────────────────────────────────────
@guard
async def cb_reset_ask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await safe_edit(
        q,
        "⚠️ *Сброс всех задач*\n\n"
        "Это удалит *все* категории, задачи и историю выполнения.\n\n"
        "Продолжить?",
        InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Да, сбросить", callback_data="reset_yes"),
                InlineKeyboardButton("❌ Отмена",       callback_data="main"),
            ]
        ])
    )


@guard
async def cb_reset_yes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await safe_edit(
        q,
        "🔄 Введи *причину сброса* (сохранится в историю):",
        kb_back("main")
    )
    ctx.user_data["state"] = ST_RESET_REASON


# ── Admin ─────────────────────────────────────────────────────
@guard
async def cb_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.from_user.id not in ADMIN_IDS:
        await q.answer("Нет доступа", show_alert=True)
        return
    await safe_edit(q, "👑 *Админ панель*", kb_admin())


@guard
async def cb_admin_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.from_user.id not in ADMIN_IDS:
        return
    users = db_all_users()
    lines = ["👥 *Пользователи*\n"]
    btns = []
    for u in users:
        st = "✅" if u["has_access"] else "❌"
        name = u["first_name"] or u["username"] or str(u["user_id"])
        lines.append(f"{st} `{u['user_id']}` — {name}")
        action = "revoke" if u["has_access"] else "grant"
        txt = "🔴 Забрать" if u["has_access"] else "🟢 Выдать"
        btns.append([
            InlineKeyboardButton(f"{st} {name[:20]}", callback_data="noop"),
            InlineKeyboardButton(txt, callback_data=f"aaccess:{action}:{u['user_id']}"),
        ])
    btns.append([InlineKeyboardButton("➕ Выдать по ID", callback_data="admin_grant")])
    btns.append([InlineKeyboardButton("🔙 Назад", callback_data="admin")])
    await safe_edit(q, "\n".join(lines) or "Нет пользователей.", InlineKeyboardMarkup(btns))


@guard
async def cb_aaccess(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.from_user.id not in ADMIN_IDS:
        return
    _, action, uid_s = q.data.split(":")
    db_grant_access(int(uid_s), action == "grant")
    await cb_admin_users(update, ctx)


@guard
async def cb_admin_grant(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.from_user.id not in ADMIN_IDS:
        return
    await safe_edit(q, "Введи Telegram ID пользователя:", kb_back("admin_users"))
    ctx.user_data["state"] = ST_ADMIN_GRANT


@guard
async def cb_admin_export(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("Подготовка файла...")
    if q.from_user.id not in ADMIN_IDS:
        return
    try:
        with open(DB_PATH, "rb") as f:
            data = f.read()
        buf = io.BytesIO(data)
        fname = f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        await ctx.bot.send_document(
            chat_id=q.from_user.id,
            document=buf,
            filename=fname,
            caption="📤 Резервная копия базы данных"
        )
    except Exception as e:
        await q.answer(f"Ошибка: {e}", show_alert=True)


@guard
async def cb_admin_import(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.from_user.id not in ADMIN_IDS:
        return
    await safe_edit(
        q,
        "📥 *Импорт базы данных*\n\n"
        "⚠️ Текущая БД будет заменена!\n\n"
        "Отправь файл `.db` в этот чат.\n"
        "После импорта бот перезапустится.",
        kb_back("admin")
    )
    ctx.user_data["state"] = ST_IMPORT_DB


async def cb_noop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()


# ──────────────────────────────────────────────────────────────
# MESSAGE HANDLERS
# ──────────────────────────────────────────────────────────────
@guard
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state = ctx.user_data.get("state")
    uid = update.effective_user.id
    text = update.message.text.strip()

    if state == ST_ADD_CATEGORY:
        ctx.user_data.pop("state", None)
        db_add_category(uid, text)
        await update.message.reply_text(
            f"✅ Категория *{text}* добавлена!",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_cats(uid)
        )

    elif state == ST_ADD_TASK:
        ctx.user_data.pop("state", None)
        cid = ctx.user_data.get("cur_cat")
        if not cid:
            await update.message.reply_text("Ошибка: категория не выбрана.")
            return
        db_add_task(cid, uid, text)
        await update.message.reply_text(
            f"✅ Задача *{text}* добавлена!",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_tasks_list(cid, uid)
        )

    elif state == ST_RESET_REASON:
        ctx.user_data.pop("state", None)
        db_reset_all(uid, text)
        await update.message.reply_text(
            "🔄 *Всё сброшено.*\nПричина сохранена в истории.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_main(uid)
        )

    elif state == ST_AUTO_TIME:
        ctx.user_data.pop("state", None)
        if re.match(r"^\d{1,2}:\d{2}$", text):
            h, m = text.split(":")
            if 0 <= int(h) <= 23 and 0 <= int(m) <= 59:
                time_s = f"{int(h):02d}:{m}"
                db_set_auto(uid, send_time=time_s)
                await reschedule(uid)
                await update.message.reply_text(
                    f"✅ Время установлено: `{time_s}`",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=kb_auto(uid)
                )
                return
        await update.message.reply_text("❌ Неверный формат. Используй `ЧЧ:ММ`", parse_mode=ParseMode.MARKDOWN)

    elif state == ST_LINK_CHANNEL:
        ctx.user_data.pop("state", None)
        try:
            if text.lstrip("-").isdigit():
                chat_id = int(text)
            else:
                chat_id = text if text.startswith("@") else f"@{text}"
            chat = await ctx.bot.get_chat(chat_id)
            db_set_auto(uid, linked_chat_id=chat.id, linked_chat_title=chat.title or str(chat.id))
            await update.message.reply_text(
                f"✅ Привязан: *{chat.title}*",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb_settings()
            )
        except Exception as e:
            await update.message.reply_text(
                f"❌ Ошибка: {e}\n\nУбедись, что бот — администратор канала/группы."
            )

    elif state == ST_ADMIN_GRANT:
        ctx.user_data.pop("state", None)
        if uid not in ADMIN_IDS:
            return
        try:
            target = int(text)
            db_ensure_user(target, "", "")
            db_grant_access(target)
            await update.message.reply_text(
                f"✅ Доступ выдан: `{target}`",
                parse_mode=ParseMode.MARKDOWN
            )
            try:
                await ctx.bot.send_message(target, "✅ Вам выдан доступ к боту! Нажми /start")
            except Exception:
                pass
        except ValueError:
            await update.message.reply_text("❌ Введи числовой Telegram ID")

    elif state == ST_TZ_TEXT:
        # Try text-based timezone
        try:
            tz = pytz.timezone(text)
            db_set_tz(uid, tz.zone)
            await reschedule(uid)
            ctx.user_data.pop("state", None)
            await update.message.reply_text(
                f"✅ Часовой пояс: *{tz.zone}*",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb_settings()
            )
        except pytz.exceptions.UnknownTimeZoneError:
            await update.message.reply_text(
                "❌ Неизвестный пояс. Используй формат `Europe/Moscow` или отправь 📍 геопозицию.",
                parse_mode=ParseMode.MARKDOWN
            )

    else:
        await update.message.reply_text(
            "Используй кнопки меню 👇",
            reply_markup=kb_main(uid)
        )


@guard
async def on_location(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    loc = update.message.location
    uid = update.effective_user.id
    tz_str = tf.timezone_at(lng=loc.longitude, lat=loc.latitude)
    if tz_str:
        db_set_tz(uid, tz_str)
        await reschedule(uid)
        ctx.user_data.pop("state", None)
        await update.message.reply_text(
            f"✅ Часовой пояс определён: *{tz_str}*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_settings()
        )
    else:
        await update.message.reply_text("❌ Не удалось определить часовой пояс по геопозиции.")


@guard
async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ctx.user_data.get("state") != ST_IMPORT_DB:
        return
    if update.effective_user.id not in ADMIN_IDS:
        return
    doc = update.message.document
    if not doc.file_name.endswith(".db"):
        await update.message.reply_text("❌ Ожидается файл `.db`")
        return
    ctx.user_data.pop("state", None)
    bak = DB_PATH + ".bak"
    try:
        shutil.copy2(DB_PATH, bak)
        tfile = await ctx.bot.get_file(doc.file_id)
        tmp = DB_PATH + ".import"
        await tfile.download_to_drive(tmp)
        # Validate SQLite
        tc = sqlite3.connect(tmp)
        tc.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        tc.close()
        shutil.move(tmp, DB_PATH)
        await update.message.reply_text("✅ БД импортирована. Перезапуск...")
        logger.info("DB imported by admin %s", update.effective_user.id)
        await asyncio.sleep(1)
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as e:
        if os.path.exists(bak):
            shutil.move(bak, DB_PATH)
        await update.message.reply_text(f"❌ Ошибка импорта: {e}")


@guard
async def on_forward(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ctx.user_data.get("state") != ST_LINK_CHANNEL:
        return
    msg = update.message
    uid = msg.from_user.id
    if msg.forward_from_chat:
        chat_id = msg.forward_from_chat.id
        title = msg.forward_from_chat.title or str(chat_id)
        ctx.user_data.pop("state", None)
        db_set_auto(uid, linked_chat_id=chat_id, linked_chat_title=title)
        await msg.reply_text(
            f"✅ Привязан: *{title}*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_settings()
        )
    else:
        await msg.reply_text(
            "❌ Не удалось получить ID. Перешли сообщение именно из канала/группы."
        )


# ──────────────────────────────────────────────────────────────
# SCHEDULER / AUTOMATION
# ──────────────────────────────────────────────────────────────
async def send_report_job(uid: int):
    global _app
    if not _app:
        return
    a = db_get_auto(uid)
    if not a or not a["is_enabled"]:
        return
    report = build_report(uid)
    try:
        await _app.bot.send_message(uid, report, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.warning("Report to user %s failed: %s", uid, e)
    if a["linked_chat_id"]:
        try:
            await _app.bot.send_message(
                a["linked_chat_id"], report, parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.warning("Report to chat %s failed: %s", a["linked_chat_id"], e)


async def reschedule(uid: int):
    a = db_get_auto(uid)
    jid = f"daily_{uid}"
    if scheduler.get_job(jid):
        scheduler.remove_job(jid)
    if not a or not a["is_enabled"]:
        return
    h, m = map(int, (a["send_time"] or "09:00").split(":"))
    tz = db_get_tz(uid)
    scheduler.add_job(
        send_report_job,
        CronTrigger(hour=h, minute=m, timezone=tz),
        id=jid,
        args=[uid],
        replace_existing=True,
        misfire_grace_time=300,
    )
    logger.info("Scheduled daily report uid=%s at %02d:%02d %s", uid, h, m, tz.zone)


async def post_init(app: Application):
    global _app
    _app = app
    conn = get_db()
    rows = conn.execute("SELECT user_id FROM automations WHERE is_enabled=1").fetchall()
    conn.close()
    for r in rows:
        await reschedule(r["user_id"])
    if not scheduler.running:
        scheduler.start()
    await app.bot.set_my_commands([
        BotCommand("start", "Запустить бота / главное меню"),
        BotCommand("menu",  "Главное меню"),
    ])
    logger.info("Bot started. Scheduler jobs: %d", len(scheduler.get_jobs()))


# ──────────────────────────────────────────────────────────────
# APP SETUP & MAIN
# ──────────────────────────────────────────────────────────────
def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu",  cmd_menu))

    # Simple exact-match callbacks
    exact_cbs = {
        "main":         cb_main,
        "today":        cb_today,
        "reports":      cb_reports,
        "report7":      cb_report7,
        "settings":     cb_settings,
        "tz_ask":       cb_tz_ask,
        "auto":         cb_auto,
        "auto_toggle":  cb_auto_toggle,
        "auto_time":    cb_auto_time,
        "cats":         cb_cats,
        "cat_add":      cb_cat_add,
        "link_ask":     cb_link_ask,
        "reset_ask":    cb_reset_ask,
        "reset_yes":    cb_reset_yes,
        "admin":        cb_admin,
        "admin_users":  cb_admin_users,
        "admin_export": cb_admin_export,
        "admin_import": cb_admin_import,
        "admin_grant":  cb_admin_grant,
        "noop":         cb_noop,
    }
    for data, handler in exact_cbs.items():
        app.add_handler(CallbackQueryHandler(handler, pattern=f"^{data}$"))

    # Pattern callbacks
    app.add_handler(CallbackQueryHandler(cb_tlog,       pattern=r"^tlog:"))
    app.add_handler(CallbackQueryHandler(cb_cal,        pattern=r"^cal:-?\d+$"))
    app.add_handler(CallbackQueryHandler(cb_cday,       pattern=r"^cday:"))
    app.add_handler(CallbackQueryHandler(cb_cat,        pattern=r"^cat:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_cat_del,    pattern=r"^cat_del:"))
    app.add_handler(CallbackQueryHandler(cb_task_add,   pattern=r"^task_add:"))
    app.add_handler(CallbackQueryHandler(cb_task_del,   pattern=r"^task_del:"))
    app.add_handler(CallbackQueryHandler(cb_task_act,   pattern=r"^task_act:"))
    app.add_handler(CallbackQueryHandler(cb_aaccess,    pattern=r"^aaccess:"))

    # Message handlers
    app.add_handler(MessageHandler(filters.LOCATION, on_location))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(
        MessageHandler(
            filters.FORWARDED & filters.ChatType.PRIVATE & ~filters.COMMAND,
            on_forward
        )
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    return app


def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN is not set in .env!")
        sys.exit(1)

    init_db()

    # Ensure admins have access
    conn = get_db()
    for aid in ADMIN_IDS:
        conn.execute("""
            INSERT INTO users (user_id, username, first_name, has_access)
            VALUES (?, 'admin', 'Admin', 1)
            ON CONFLICT(user_id) DO UPDATE SET has_access=1
        """, (aid,))
    conn.commit()
    conn.close()

    app = build_app()
    logger.info("Starting bot polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
