#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Рабочий бот-секретарь для Телеграма.

Что умеет:
  • Сидит в твоих рабочих чатах и ловит задачи/встречи (по упоминанию тебя,
    по ответам тебе, по списку "наблюдаемых" людей — например, Артура — или по всем,
    если включить SCAN_ALL).
  • Присылает пойманное тебе в ЛС структурно, с кнопками.
  • Напоминает о задаче снова и снова, пока не нажмёшь «✅ Выполнено».
  • Напоминает о встречах заранее (за MEETING_LEAD_MIN минут).
  • Каждый день в заданное время шлёт тебе в ЛС отчёт о проделанной работе.

Зависимости: см. requirements.txt
Настройка: см. README.md и .env.example
"""

import os
import re
import json
import sqlite3
import logging
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)

# ── Конфиг ────────────────────────────────────────────────────────
load_dotenv()
TOKEN            = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ANTHROPIC_KEY    = os.getenv("ANTHROPIC_API_KEY", "").strip()
MODEL            = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001").strip()
TZ               = ZoneInfo(os.getenv("TIMEZONE", "Asia/Makassar"))
REPORT_HH, REPORT_MM = [int(x) for x in os.getenv("DAILY_REPORT_TIME", "19:00").split(":")]
REMIND_EVERY_MIN = int(os.getenv("REMIND_EVERY_MIN", "30"))
QUIET_START      = int(os.getenv("QUIET_START", "22"))
QUIET_END        = int(os.getenv("QUIET_END", "8"))
MEETING_LEAD_MIN = int(os.getenv("MEETING_LEAD_MIN", "30"))
SCAN_ALL         = os.getenv("SCAN_ALL", "false").lower() in ("1", "true", "yes")
DB_PATH          = os.getenv("DB_PATH", "bot.db")

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("secretary")

# ── База данных ───────────────────────────────────────────────────
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS watch (username TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT DEFAULT 'task',        -- 'task' | 'event'
  title TEXT,
  src_chat TEXT,
  src_user TEXT,
  due TEXT,                        -- 'YYYY-MM-DD' | 'YYYY-MM-DDTHH:MM' | NULL
  status TEXT DEFAULT 'open',      -- 'open' | 'done'
  created TEXT,
  done_at TEXT,
  lead_done INTEGER DEFAULT 0      -- для встреч: отправлено ли предварительное напоминание
);
""")
db.commit()

def kv_get(k, default=None):
    r = db.execute("SELECT value FROM kv WHERE key=?", (k,)).fetchone()
    return r["value"] if r else default

def kv_set(k, v):
    db.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=?", (k, str(v), str(v)))
    db.commit()

def owner_id():
    v = kv_get("owner_id")
    return int(v) if v else None

def watch_set():
    return {r["username"].lower() for r in db.execute("SELECT username FROM watch").fetchall()}

# ── Время / тихие часы ────────────────────────────────────────────
def now():
    return datetime.now(TZ)

def in_quiet(dt=None):
    h = (dt or now()).hour
    if QUIET_START <= QUIET_END:
        return QUIET_START <= h < QUIET_END
    return h >= QUIET_START or h < QUIET_END  # окно через полночь

def parse_due(due):
    """'YYYY-MM-DDTHH:MM' или 'YYYY-MM-DD' -> aware datetime в TZ (или None)."""
    if not due:
        return None
    try:
        if "T" in due:
            dt = datetime.strptime(due, "%Y-%m-%dT%H:%M")
        else:
            dt = datetime.strptime(due, "%Y-%m-%d")
        return dt.replace(tzinfo=TZ)
    except ValueError:
        return None

def fmt_due(due):
    dt = parse_due(due)
    if not dt:
        return ""
    today = now().date()
    day = ("сегодня" if dt.date() == today else
           "завтра" if dt.date() == today + timedelta(days=1) else
           dt.strftime("%d.%m"))
    return f"{day}, {dt.strftime('%H:%M')}" if "T" in due else day

# ── Разбор сообщения в задачу/встречу ─────────────────────────────
MEETING_WORDS = ("встреч", "созвон", "звонок", "call", "зум", "zoom", "meet", "планёрк", "планерк")
TASK_CUES = ("сдела", "нужно", "надо", "прошу", "отправ", "подготов", "согласу", "напиши",
             "проверь", "дедлайн", "до завтра", "к пятниц", "скинь", "залей", "запусти")

def heuristic(text):
    low = text.lower()
    is_task = any(c in low for c in TASK_CUES) or any(w in low for w in MEETING_WORDS)
    kind = "event" if any(w in low for w in MEETING_WORDS) else "task"
    title = re.sub(r"\s+", " ", text).strip()[:140]
    return {"is_task": is_task, "kind": kind, "title": title, "due": None}

async def extract(text, chat_title, sender):
    if not ANTHROPIC_KEY:
        return heuristic(text)
    n = now()
    prompt = (
        f"Сегодня {n.strftime('%Y-%m-%dT%H:%M')}, часовой пояс пользователя — {TZ}.\n"
        f"Сообщение из рабочего чата «{chat_title}» от {sender}:\n\"{text}\"\n\n"
        "Это поручение/задача или встреча ДЛЯ пользователя? Верни СТРОГО JSON без markdown:\n"
        '{"is_task": bool, "kind": "task"|"event", "title": string, "due": string|null}\n'
        "Правила: is_task=false, если это просто болтовня/инфо, а не действие для пользователя. "
        "kind=event для встреч/созвонов, иначе task. "
        "title — короткая суть без слов о дате. "
        'due — локальное время "YYYY-MM-DDTHH:MM", либо "YYYY-MM-DD" без времени, либо null. '
        "Понимай «сегодня/завтра/в пятницу/через 2 дня/к 15:00» относительно сегодняшней даты."
    )
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": MODEL, "max_tokens": 400,
                      "messages": [{"role": "user", "content": prompt}]},
            )
        out = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
        m = re.search(r"\{.*\}", out, re.S)
        p = json.loads(m.group(0)) if m else {}
        return {
            "is_task": bool(p.get("is_task")),
            "kind": "event" if p.get("kind") == "event" else "task",
            "title": (p.get("title") or text).strip()[:140],
            "due": p.get("due") or None,
        }
    except Exception as e:
        log.warning("LLM extract failed: %s — фолбэк на эвристику", e)
        return heuristic(text)

# ── Планирование напоминаний ──────────────────────────────────────
def jobs_cancel(jq, item_id):
    for j in jq.get_jobs_by_name(f"nag:{item_id}") + jq.get_jobs_by_name(f"ev:{item_id}"):
        j.schedule_removal()

def schedule_item(jq, item):
    iid = item["id"]
    jobs_cancel(jq, iid)
    if item["status"] != "open":
        return
    due = parse_due(item["due"])
    if item["kind"] == "event":
        if not due:
            return
        when = due - timedelta(minutes=MEETING_LEAD_MIN)
        delay = max(1, (when - now()).total_seconds())
        if (due - now()).total_seconds() > -300:  # ещё не сильно прошла
            jq.run_once(meeting_cb, when=delay, data=iid, name=f"ev:{iid}")
    else:  # task — пилим напоминаниями, пока не выполнено
        first = max(1, (due - now()).total_seconds()) if due else 5
        jq.run_repeating(nag_cb, interval=REMIND_EVERY_MIN * 60, first=first,
                         data=iid, name=f"nag:{iid}")

async def nag_cb(ctx: ContextTypes.DEFAULT_TYPE):
    iid = ctx.job.data
    row = db.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
    if not row or row["status"] != "open":
        ctx.job.schedule_removal()
        return
    if in_quiet():
        return  # не дёргаем ночью, задача останется на утро
    oid = owner_id()
    if not oid:
        return
    due_txt = f"\n⏰ срок: {fmt_due(row['due'])}" if row["due"] else ""
    await ctx.bot.send_message(
        oid,
        f"🔔 *Напоминание — задача не закрыта:*\n«{row['title']}»{due_txt}",
        parse_mode=ParseMode.MARKDOWN, reply_markup=item_buttons(iid),
    )

async def meeting_cb(ctx: ContextTypes.DEFAULT_TYPE):
    iid = ctx.job.data
    row = db.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
    if not row or row["status"] != "open":
        return
    oid = owner_id()
    if not oid:
        return
    await ctx.bot.send_message(
        oid,
        f"📅 *Скоро встреча* (через ~{MEETING_LEAD_MIN} мин):\n«{row['title']}»\n🕒 {fmt_due(row['due'])}",
        parse_mode=ParseMode.MARKDOWN, reply_markup=item_buttons(iid),
    )

def item_buttons(iid):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Выполнено", callback_data=f"done:{iid}"),
        InlineKeyboardButton("⏰ +1 час",   callback_data=f"snooze:{iid}"),
        InlineKeyboardButton("🗑 Не задача", callback_data=f"del:{iid}"),
    ]])

# ── Хендлер сообщений из групп ────────────────────────────────────
async def on_group(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not (msg.text or msg.caption):
        return
    text = msg.text or msg.caption
    oid = owner_id()
    if not oid or (msg.from_user and msg.from_user.is_bot):
        return

    owner_un = (kv_get("owner_username") or "").lower()
    sender_un = (msg.from_user.username or "").lower() if msg.from_user else ""
    mentioned = owner_un and f"@{owner_un}" in text.lower()
    reply_to_owner = bool(msg.reply_to_message and msg.reply_to_message.from_user
                          and msg.reply_to_message.from_user.id == oid)
    watched = sender_un in watch_set()

    if not (SCAN_ALL or mentioned or reply_to_owner or watched):
        return

    chat_title = msg.chat.title or "личка"
    sender = f"@{sender_un}" if sender_un else (msg.from_user.full_name if msg.from_user else "кто-то")
    p = await extract(text, chat_title, sender)
    if not p["is_task"]:
        return

    cur = db.execute(
        "INSERT INTO items(kind,title,src_chat,src_user,due,created) VALUES(?,?,?,?,?,?)",
        (p["kind"], p["title"], chat_title, sender, p["due"], now().isoformat()),
    )
    db.commit()
    iid = cur.lastrowid
    row = db.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
    schedule_item(ctx.job_queue, row)

    icon = "📅 *Встреча*" if p["kind"] == "event" else "📌 *Новая задача*"
    due_txt = f"\n🕒 {fmt_due(p['due'])}" if p["due"] else ""
    await ctx.bot.send_message(
        oid,
        f"{icon} из «{chat_title}» (от {sender}):\n«{p['title']}»{due_txt}",
        parse_mode=ParseMode.MARKDOWN, reply_markup=item_buttons(iid),
    )

# ── Кнопки ────────────────────────────────────────────────────────
async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    action, iid = q.data.split(":")
    iid = int(iid)
    row = db.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
    if not row:
        await q.answer("Уже нет такой."); return

    if action == "done":
        db.execute("UPDATE items SET status='done', done_at=? WHERE id=?", (now().isoformat(), iid))
        db.commit(); jobs_cancel(ctx.job_queue, iid)
        await q.edit_message_text(f"✅ Выполнено: «{row['title']}»")
        await q.answer("Готово 💪")
    elif action == "del":
        db.execute("DELETE FROM items WHERE id=?", (iid,))
        db.commit(); jobs_cancel(ctx.job_queue, iid)
        await q.edit_message_text(f"🗑 Убрано: «{row['title']}»")
        await q.answer("Удалил")
    elif action == "snooze":
        jobs_cancel(ctx.job_queue, iid)
        ctx.job_queue.run_repeating(nag_cb, interval=REMIND_EVERY_MIN * 60,
                                    first=3600, data=iid, name=f"nag:{iid}")
        await q.answer("Напомню через час")

# ── Команды ───────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    kv_set("owner_id", u.id)
    if u.username:
        kv_set("owner_username", u.username)
    await update.message.reply_text(
        "Готов работать 🫡\n"
        "Добавь меня в рабочие чаты — я буду ловить задачи и встречи и слать их сюда.\n\n"
        "Команды:\n"
        "/tasks — открытые задачи\n"
        "/report — отчёт прямо сейчас\n"
        "/watch @user — всегда отслеживать этого человека (например, босса)\n"
        "/unwatch @user — перестать\n"
        "/repeat 30 — как часто напоминать (мин)\n"
        "/help — справка"
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, ctx)

async def cmd_tasks(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = db.execute("SELECT * FROM items WHERE status='open' ORDER BY due IS NULL, due").fetchall()
    if not rows:
        await update.message.reply_text("Открытых задач нет. Красавчик."); return
    for r in rows:
        icon = "📅" if r["kind"] == "event" else "📌"
        due = f" — {fmt_due(r['due'])}" if r["due"] else ""
        await update.message.reply_text(f"{icon} «{r['title']}»{due}", reply_markup=item_buttons(r["id"]))

async def cmd_watch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Пример: /watch @artur"); return
    un = ctx.args[0].lstrip("@").lower()
    db.execute("INSERT OR IGNORE INTO watch(username) VALUES(?)", (un,)); db.commit()
    await update.message.reply_text(f"Слежу за @{un} ✅")

async def cmd_unwatch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Пример: /unwatch @artur"); return
    un = ctx.args[0].lstrip("@").lower()
    db.execute("DELETE FROM watch WHERE username=?", (un,)); db.commit()
    await update.message.reply_text(f"Больше не слежу за @{un}")

async def cmd_repeat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global REMIND_EVERY_MIN
    if ctx.args and ctx.args[0].isdigit():
        REMIND_EVERY_MIN = int(ctx.args[0]); kv_set("repeat_min", REMIND_EVERY_MIN)
        await update.message.reply_text(f"Буду напоминать каждые {REMIND_EVERY_MIN} мин.")
    else:
        await update.message.reply_text(f"Сейчас: каждые {REMIND_EVERY_MIN} мин. Пример: /repeat 20")

def build_report():
    today = now().date().isoformat()
    tomorrow = (now().date() + timedelta(days=1)).isoformat()
    done = db.execute("SELECT * FROM items WHERE status='done' AND substr(done_at,1,10)=?", (today,)).fetchall()
    open_tasks = db.execute("SELECT * FROM items WHERE status='open' AND kind='task'").fetchall()
    overdue = [r for r in open_tasks if r["due"] and r["due"][:10] < today]
    ev = db.execute("SELECT * FROM items WHERE status='open' AND kind='event'").fetchall()
    ev_tom = [r for r in ev if r["due"] and r["due"][:10] == tomorrow]
    line = lambda r: f"• {r['title']}" + (f" ({fmt_due(r['due'])})" if r["due"] else "")
    s = f"📊 *Отчёт за {now().strftime('%d.%m')}*\n"
    s += f"\n✅ Сделано ({len(done)}):\n" + ("\n".join(line(r) for r in done) or "—")
    if overdue:
        s += f"\n\n⚠️ Просрочено ({len(overdue)}):\n" + "\n".join(line(r) for r in overdue)
    s += f"\n\n🔧 В работе ({len(open_tasks)}):\n" + ("\n".join(line(r) for r in open_tasks) or "—")
    s += f"\n\n📅 Встречи завтра ({len(ev_tom)}):\n" + ("\n".join(line(r) for r in ev_tom) or "—")
    return s

async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(build_report(), parse_mode=ParseMode.MARKDOWN)

async def daily_report_cb(ctx: ContextTypes.DEFAULT_TYPE):
    oid = owner_id()
    if oid:
        await ctx.bot.send_message(oid, build_report(), parse_mode=ParseMode.MARKDOWN)

# ── Запуск ────────────────────────────────────────────────────────
async def on_startup(app: Application):
    global REMIND_EVERY_MIN
    saved = kv_get("repeat_min")
    if saved:
        REMIND_EVERY_MIN = int(saved)
    # пересобираем напоминания по открытым делам после перезапуска
    for r in db.execute("SELECT * FROM items WHERE status='open'").fetchall():
        schedule_item(app.job_queue, r)
    # ежедневный отчёт
    app.job_queue.run_daily(daily_report_cb, time=dtime(REPORT_HH, REPORT_MM, tzinfo=TZ), name="daily")
    log.info("Бот запущен. Отчёт в %02d:%02d (%s).", REPORT_HH, REPORT_MM, TZ)

def main():
    if not TOKEN:
        raise SystemExit("Нет TELEGRAM_BOT_TOKEN — заполни .env (см. README.md)")
    app = Application.builder().token(TOKEN).post_init(on_startup).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("tasks", cmd_tasks))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("watch", cmd_watch))
    app.add_handler(CommandHandler("unwatch", cmd_unwatch))
    app.add_handler(CommandHandler("repeat", cmd_repeat))
    app.add_handler(CallbackQueryHandler(on_button))
    # сообщения из групп/супергрупп (личку с командами не трогаем)
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.CAPTION) & ~filters.COMMAND & filters.ChatType.GROUPS,
        on_group,
    ))
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
