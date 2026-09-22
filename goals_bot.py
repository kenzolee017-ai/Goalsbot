#!/usr/bin/env python3
"""
Goal Tracker Telegram Bot
=========================

Anytime goal tracker (not holiday-specific). Each user:
  1. /start  -> onboarding: name, then CG
  2. /addgoal -> add a goal: what it is, timeline, and daily action plan
  3. Every day at their set time (default 10pm) the bot sends a check-in
     listing active goals + the running log, and asks what they did.
  4. After logging, the bot offers to attach a photo to that entry.
  5. /finishgoal marks a goal complete -> broadcasts the win to every user
     of the bot, then privately asks if they'd like to share what they
     gained (yes/no buttons) - if yes, that's broadcast too.
  6. /milestone lets anyone share a reflection/win with everyone, anytime,
     posted as a "MILESTONES" announcement signed with their name.

Commands:
  /start       set up, or show the welcome menu if already set up
  /help /menu  show the command menu
  /addgoal     add a new goal (goal, timeline, daily action)
  /finishgoal  mark one of your active goals complete
  /goals       show all your goals (active + completed)
  /log         show your full day-by-day log
  /checkin     trigger the check-in prompt right now
  /settime     change your daily reminder time, e.g. /settime 21:30
  /stop        pause your daily reminders
  /milestone   share a reflection/win with everyone, anytime

Setup:
  pip install "python-telegram-bot[job-queue]>=21.0"
  export BOT_TOKEN="123456:ABC-your-token-from-@BotFather"
  python goals_bot.py

Optional env vars:
  BOT_TIMEZONE   IANA timezone name (default: Asia/Singapore)
  BOT_DATA_FILE  path to the JSON data file (default: goals_data.json)
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
TIMEZONE = ZoneInfo(os.environ.get("BOT_TIMEZONE", "Asia/Singapore"))
DATA_FILE = Path(os.environ.get("BOT_DATA_FILE", "goals_data.json"))

DEFAULT_HOUR = 22          # 10pm
DEFAULT_MINUTE = 0
MAX_LOG_LINES_IN_PROMPT = 12

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("goals-bot")

# ---- conversation stages -------------------------------------------------
AWAITING_NAME = "awaiting_name"
AWAITING_CG = "awaiting_cg"
READY = "ready"
AWAITING_GOAL_TEXT = "awaiting_goal_text"
AWAITING_GOAL_TIMELINE = "awaiting_goal_timeline"
AWAITING_GOAL_DAILY = "awaiting_goal_daily"
AWAITING_PHOTO = "awaiting_photo"
AWAITING_GAIN_MESSAGE = "awaiting_gain_message"
AWAITING_MILESTONE = "awaiting_milestone"


# --------------------------------------------------------------------------
# Storage  (simple JSON file, one record per chat)
# --------------------------------------------------------------------------

def load_data() -> Dict[str, Any]:
    if not DATA_FILE.exists():
        return {}
    try:
        with DATA_FILE.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        log.error("Could not read %s (%s) - starting empty.", DATA_FILE, exc)
        return {}


def save_data(data: Dict[str, Any]) -> None:
    """Atomic write so a crash mid-save can't corrupt the file."""
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(DATA_FILE.parent or "."), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp_path, DATA_FILE)
    except OSError as exc:
        log.error("Failed to save data: %s", exc)
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def get_chat(data: Dict[str, Any], chat_id: int) -> Dict[str, Any]:
    """Fetch (or create) a chat record, backfilling any missing fields.

    Backfilling means old data files (from an earlier version of this bot)
    won't crash the bot - they'll just be treated as needing to re-onboard.
    """
    chat = data.setdefault(str(chat_id), {})
    chat.setdefault("stage", AWAITING_NAME)
    chat.setdefault("name", "")
    chat.setdefault("cg", "")
    chat.setdefault("onboarded", False)
    chat.setdefault("goals", [])
    chat.setdefault("next_goal_id", 1)
    chat.setdefault("entries", [])
    chat.setdefault("start_date", None)
    chat.setdefault("hour", DEFAULT_HOUR)
    chat.setdefault("minute", DEFAULT_MINUTE)
    chat.setdefault("reminders_on", True)
    chat.setdefault("temp_goal", {})
    chat.setdefault("pending_entry_index", None)
    chat.setdefault("pending_finish_goal_id", None)
    return chat


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def today() -> date:
    return datetime.now(TIMEZONE).date()


def day_number(chat: Dict[str, Any]) -> int:
    if not chat.get("start_date"):
        return 1
    start = date.fromisoformat(chat["start_date"])
    return (today() - start).days + 1


def parse_duration_to_date(text: str, start: date) -> Optional[str]:
    """Best-effort parse of '30 days' / '2 weeks' / '3 months' -> target date."""
    m = re.search(r"(\d+)\s*(day|week|month)s?", text.lower())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    if unit == "day":
        delta = timedelta(days=n)
    elif unit == "week":
        delta = timedelta(weeks=n)
    else:  # month (approximate)
        delta = timedelta(days=30 * n)
    return (start + delta).isoformat()


def active_goals(chat: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [g for g in chat.get("goals", []) if isinstance(g, dict) and g.get("status") == "active"]


def format_goals(chat: Dict[str, Any], only_active: bool = False) -> str:
    goals = [g for g in chat.get("goals", []) if isinstance(g, dict)]
    if only_active:
        goals = [g for g in goals if g.get("status") == "active"]
    if not goals:
        return "No active goals right now 🌱" if only_active else "No goals yet - add one with /addgoal 🌱"

    lines = []
    for g in goals:
        icon = "✅" if g.get("status") == "completed" else "🔵"
        line = f"{icon} {g.get('text', '')}"
        extras = []
        if g.get("timeline"):
            extras.append(f"⏳ {g['timeline']}")
        if g.get("daily_action"):
            extras.append(f"🔁 {g['daily_action']}")
        if extras:
            line += "\n    " + "   |   ".join(extras)
        if g.get("status") == "completed" and g.get("completed"):
            line += f"\n    🏁 Completed {g['completed']}"
        lines.append(line)
    return "\n\n".join(lines)


def format_log(chat: Dict[str, Any], limit: Optional[int] = None) -> str:
    entries = chat.get("entries", [])
    if not entries:
        return ""
    shown = entries if limit is None else entries[-limit:]
    lines = []
    for e in shown:
        camera = " 📸" if e.get("photo_file_id") else ""
        lines.append(f"Day {e['day']}, {e['text']}{camera}")
    if limit is not None and len(entries) > limit:
        hidden = len(entries) - limit
        lines.insert(0, f"...({hidden} earlier {'entry' if hidden == 1 else 'entries'} - see /log)")
    return "\n".join(lines)


def build_welcome(chat: Dict[str, Any]) -> str:
    name = chat.get("name") or "there"
    return (
        f"👋 Hey {name}! Here's what I can do:\n\n"
        "➕ /addgoal – add a new goal (with a timeline & daily plan)\n"
        "✅ /finishgoal – mark a goal complete 🎉\n"
        "📋 /goals – view all your goals\n"
        "📖 /log – see your full daily log\n"
        "🌙 /checkin – trigger today's check-in now\n"
        "⏰ /settime HH:MM – change your daily reminder time\n"
        "🔕 /stop – pause daily reminders\n"
        "🌟 /milestone – share a win or reflection with everyone, anytime\n"
        "❓ /help – show this menu again\n\n"
        "Just message me anytime to log what you've done today! 💪"
    )


def checkin_message(chat: Dict[str, Any]) -> str:
    parts = [
        f"🌙✨ Day {day_number(chat)} Check-in",
        "",
        "What have you done today to keep track of your goals? 📝",
        "",
        "🎯 Your active goals:",
        format_goals(chat, only_active=True),
    ]
    history = format_log(chat, limit=MAX_LOG_LINES_IN_PROMPT)
    if history:
        parts += ["", "📖 Your log so far:", history]
    parts += ["", "Reply with what you did today - I'll then ask if you want to add a photo 📸"]
    return "\n".join(parts)


async def broadcast(application: Application, data: Dict[str, Any], text: str,
                     exclude_chat_id: Optional[int] = None) -> None:
    exclude = str(exclude_chat_id) if exclude_chat_id is not None else None
    for chat_id_str in list(data.keys()):
        if chat_id_str == exclude:
            continue
        try:
            await application.bot.send_message(chat_id=int(chat_id_str), text=text)
        except Exception as exc:  # noqa: BLE001 - a blocked/deleted chat shouldn't stop the rest
            log.warning("Broadcast to %s failed: %s", chat_id_str, exc)


def require_onboarded(chat: Dict[str, Any]) -> bool:
    return bool(chat.get("onboarded"))


NOT_ONBOARDED_MSG = "Let's finish setting you up first - send /start 🙂"


# --------------------------------------------------------------------------
# Daily job scheduling
# --------------------------------------------------------------------------

def job_name(chat_id: int) -> str:
    return f"daily-checkin-{chat_id}"


def schedule_daily(application: Application, chat_id: int, hour: int, minute: int) -> None:
    jq = application.job_queue
    if jq is None:
        log.error("JobQueue unavailable - install python-telegram-bot[job-queue].")
        return
    for job in jq.get_jobs_by_name(job_name(chat_id)):
        job.schedule_removal()
    jq.run_daily(
        daily_checkin,
        time=dtime(hour=hour, minute=minute, tzinfo=TIMEZONE),
        chat_id=chat_id,
        name=job_name(chat_id),
    )
    log.info("Scheduled daily check-in for chat %s at %02d:%02d", chat_id, hour, minute)


def unschedule_daily(application: Application, chat_id: int) -> None:
    jq = application.job_queue
    if jq is None:
        return
    for job in jq.get_jobs_by_name(job_name(chat_id)):
        job.schedule_removal()


async def daily_checkin(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = context.job.chat_id
    data = load_data()
    chat = data.get(str(chat_id))
    if not chat or not chat.get("onboarded") or not chat.get("reminders_on", True):
        return
    await context.bot.send_message(chat_id=chat_id, text=checkin_message(chat))


# --------------------------------------------------------------------------
# Command handlers
# --------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    data = load_data()
    chat = get_chat(data, chat_id)

    if not chat.get("onboarded"):
        chat["stage"] = AWAITING_NAME
        save_data(data)
        await update.message.reply_text(
            "👋 Welcome to the Goal Tracker Bot!\n\nLet's get you set up.\n\nWhat's your name?"
        )
        return

    save_data(data)
    await update.message.reply_text(build_welcome(chat))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    chat = get_chat(data, update.effective_chat.id)
    if not require_onboarded(chat):
        await update.message.reply_text(NOT_ONBOARDED_MSG)
        return
    await update.message.reply_text(build_welcome(chat))


async def cmd_addgoal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    chat = get_chat(data, update.effective_chat.id)
    if not require_onboarded(chat):
        await update.message.reply_text(NOT_ONBOARDED_MSG)
        return
    if chat["stage"] != READY:
        await update.message.reply_text("Let's finish what we're doing first! 🙂")
        return
    chat["stage"] = AWAITING_GOAL_TEXT
    chat["temp_goal"] = {}
    save_data(data)
    await update.message.reply_text("🎯 What's the goal you want to add?")


async def cmd_finishgoal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    chat = get_chat(data, update.effective_chat.id)
    if not require_onboarded(chat):
        await update.message.reply_text(NOT_ONBOARDED_MSG)
        return
    active = active_goals(chat)
    if not active:
        await update.message.reply_text("You don't have any active goals right now. Add one with /addgoal 🌱")
        return
    keyboard = [
        [InlineKeyboardButton(f"✅ {g['text'][:40]}", callback_data=f"finish_{g['id']}")]
        for g in active
    ]
    await update.message.reply_text(
        "Which goal would you like to mark as complete? 🏁",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def cmd_goals(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    chat = get_chat(data, update.effective_chat.id)
    if not require_onboarded(chat):
        await update.message.reply_text(NOT_ONBOARDED_MSG)
        return
    await update.message.reply_text(f"📋 Your goals:\n\n{format_goals(chat)}")


async def cmd_log(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    chat = get_chat(data, update.effective_chat.id)
    if not require_onboarded(chat):
        await update.message.reply_text(NOT_ONBOARDED_MSG)
        return
    history = format_log(chat)
    if not history:
        await update.message.reply_text("Nothing logged yet - your first entry starts Day 1. 📖")
        return
    await update.message.reply_text(f"📖 Your log so far:\n\n{history}")


async def cmd_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    chat = get_chat(data, update.effective_chat.id)
    if not require_onboarded(chat):
        await update.message.reply_text(NOT_ONBOARDED_MSG)
        return
    await update.message.reply_text(checkin_message(chat))


async def cmd_settime(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or not re.fullmatch(r"\d{1,2}:\d{2}", context.args[0]):
        await update.message.reply_text("Usage: /settime 22:00")
        return
    hour, minute = (int(p) for p in context.args[0].split(":"))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        await update.message.reply_text("That's not a real time. Try /settime 22:00")
        return

    chat_id = update.effective_chat.id
    data = load_data()
    chat = get_chat(data, chat_id)
    if not require_onboarded(chat):
        await update.message.reply_text(NOT_ONBOARDED_MSG)
        return
    chat["hour"], chat["minute"], chat["reminders_on"] = hour, minute, True
    save_data(data)
    schedule_daily(context.application, chat_id, hour, minute)
    await update.message.reply_text(f"⏰ Done - I'll check in daily at {hour:02d}:{minute:02d}.")


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    data = load_data()
    chat = get_chat(data, chat_id)
    if not require_onboarded(chat):
        await update.message.reply_text(NOT_ONBOARDED_MSG)
        return
    chat["reminders_on"] = False
    save_data(data)
    unschedule_daily(context.application, chat_id)
    await update.message.reply_text(
        "🔕 Daily reminders are off. Your goals and log are safe - /settime turns them back on."
    )


async def cmd_milestone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    chat = get_chat(data, update.effective_chat.id)
    if not require_onboarded(chat):
        await update.message.reply_text(NOT_ONBOARDED_MSG)
        return
    if chat["stage"] != READY:
        await update.message.reply_text("Let's finish what we're doing first! 🙂")
        return
    chat["stage"] = AWAITING_MILESTONE
    save_data(data)
    await update.message.reply_text("🌟 What would you like to share with everyone? Type it below.")


# --------------------------------------------------------------------------
# Callback (inline button) handlers
# --------------------------------------------------------------------------

async def callback_finish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    goal_id = int(query.data.split("_", 1)[1])

    data = load_data()
    chat = get_chat(data, chat_id)
    goal = next((g for g in chat["goals"] if g.get("id") == goal_id and g.get("status") == "active"), None)
    if not goal:
        await query.edit_message_text("That goal isn't available anymore.")
        return

    goal["status"] = "completed"
    goal["completed"] = today().isoformat()
    save_data(data)

    await query.edit_message_text(f"🎉 Marked as complete:\n\"{goal['text']}\"")

    name = chat.get("name") or "Someone"
    await broadcast(
        context.application, data,
        f"🎉🏆 {name} just completed a goal:\n\"{goal['text']}\"! 👏👏",
        exclude_chat_id=chat_id,
    )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Yes 🙌", callback_data=f"gainyes_{goal_id}"),
        InlineKeyboardButton("No thanks", callback_data=f"gainno_{goal_id}"),
    ]])
    await context.bot.send_message(
        chat_id=chat_id,
        text="Amazing work! 🌟 Share what you have gained upon completing?",
        reply_markup=keyboard,
    )


async def callback_gain(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id

    if query.data.startswith("gainyes_"):
        goal_id = int(query.data.split("_", 1)[1])
        data = load_data()
        chat = get_chat(data, chat_id)
        chat["stage"] = AWAITING_GAIN_MESSAGE
        chat["pending_finish_goal_id"] = goal_id
        save_data(data)
        await query.edit_message_text("Go ahead - what did you gain from completing this? 💭 I'll share it with everyone.")
    else:
        await query.edit_message_text("All good - nice work either way! 👍")


# --------------------------------------------------------------------------
# Plain-text handler: the actual conversation
# --------------------------------------------------------------------------

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()
    if not text:
        return

    data = load_data()
    chat = get_chat(data, chat_id)
    stage = chat["stage"]

    # ---- onboarding: name ------------------------------------------------
    if stage == AWAITING_NAME:
        chat["name"] = text
        chat["stage"] = AWAITING_CG
        save_data(data)
        await update.message.reply_text(f"Nice to meet you, {text}! 🙌\n\nWhat's your CG?")
        return

    # ---- onboarding: CG ----------------------------------------------------
    if stage == AWAITING_CG:
        chat["cg"] = text
        chat["onboarded"] = True
        chat["start_date"] = today().isoformat()
        chat["stage"] = READY
        save_data(data)
        schedule_daily(context.application, chat_id, chat["hour"], chat["minute"])
        await update.message.reply_text(
            build_welcome(chat) + "\n\nLet's add your first goal - try /addgoal whenever you're ready! 🎯"
        )
        return

    # ---- add-goal flow: goal text ----------------------------------------
    if stage == AWAITING_GOAL_TEXT:
        chat["temp_goal"] = {"text": text}
        chat["stage"] = AWAITING_GOAL_TIMELINE
        save_data(data)
        await update.message.reply_text(
            f"Got it - \"{text}\" 🎯\n\n⏳ What's your timeline to complete this? "
            "(e.g. '30 days', '2 weeks', '3 months')"
        )
        return

    # ---- add-goal flow: timeline ------------------------------------------
    if stage == AWAITING_GOAL_TIMELINE:
        chat["temp_goal"]["timeline"] = text
        chat["stage"] = AWAITING_GOAL_DAILY
        save_data(data)
        await update.message.reply_text("🔥 And what will you do every day to work toward it?")
        return

    # ---- add-goal flow: daily action, then save ---------------------------
    if stage == AWAITING_GOAL_DAILY:
        tg = chat["temp_goal"]
        goal_id = chat.get("next_goal_id", 1)
        target = parse_duration_to_date(tg.get("timeline", ""), today())
        goal = {
            "id": goal_id,
            "text": tg.get("text", ""),
            "timeline": tg.get("timeline", ""),
            "daily_action": text,
            "status": "active",
            "created": today().isoformat(),
            "completed": None,
            "target_date": target,
        }
        chat["goals"].append(goal)
        chat["next_goal_id"] = goal_id + 1
        chat["temp_goal"] = {}
        chat["stage"] = READY
        save_data(data)

        confirm = f"✅ Goal added!\n\n🔵 {goal['text']}\n⏳ {goal['timeline']}\n🔁 {goal['daily_action']}"
        if target:
            confirm += f"\n🗓️ Target: {target}"
        confirm += "\n\nUse /addgoal to add another, or /finishgoal once you complete one 🎉"
        await update.message.reply_text(confirm)
        return

    # ---- milestone broadcast ----------------------------------------------
    if stage == AWAITING_MILESTONE:
        name = chat.get("name") or "Someone"
        msg = f"🌟 MILESTONES 🌟\n\n{text}\n\n— {name}"
        await broadcast(context.application, data, msg, exclude_chat_id=chat_id)
        chat["stage"] = READY
        save_data(data)
        await update.message.reply_text("Shared with everyone! 🌟🙌")
        return

    # ---- "what did you gain" broadcast -------------------------------------
    if stage == AWAITING_GAIN_MESSAGE:
        goal_id = chat.get("pending_finish_goal_id")
        goal = next((g for g in chat["goals"] if g.get("id") == goal_id), None)
        goal_text = goal["text"] if goal else "their goal"
        name = chat.get("name") or "Someone"
        msg = f"💬 {name} on completing \"{goal_text}\":\n\"{text}\""
        await broadcast(context.application, data, msg, exclude_chat_id=chat_id)
        chat["stage"] = READY
        chat["pending_finish_goal_id"] = None
        save_data(data)
        await update.message.reply_text("Shared with everyone! 🎉🙌")
        return

    # ---- expecting a photo, got text instead -> treat as skip -------------
    if stage == AWAITING_PHOTO:
        chat["stage"] = READY
        chat["pending_entry_index"] = None
        save_data(data)
        await update.message.reply_text("No worries, logged without a photo 📝👍")
        return

    # ---- default: this is a daily log entry --------------------------------
    day = day_number(chat)
    entry = {"day": day, "date": today().isoformat(), "text": text, "photo_file_id": None}
    chat["entries"].append(entry)
    chat["pending_entry_index"] = len(chat["entries"]) - 1
    chat["stage"] = AWAITING_PHOTO
    save_data(data)
    await update.message.reply_text(
        f"✅ Logged for Day {day}:\n\"{text}\"\n\n"
        "📸 Want to add a photo to today's log? Send one now, or type 'skip'."
    )


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    data = load_data()
    chat = get_chat(data, chat_id)

    if chat["stage"] == AWAITING_PHOTO and chat.get("pending_entry_index") is not None:
        idx = chat["pending_entry_index"]
        file_id = update.message.photo[-1].file_id  # largest size
        day = day_number(chat)
        if 0 <= idx < len(chat["entries"]):
            chat["entries"][idx]["photo_file_id"] = file_id
            day = chat["entries"][idx]["day"]
        chat["stage"] = READY
        chat["pending_entry_index"] = None
        save_data(data)
        await update.message.reply_text(f"📸 Photo added to Day {day}! Nice work today 💪")
    else:
        await update.message.reply_text(
            "Thanks for the photo! 📸 Tell me what you did today first, and I'll ask if you want to attach one."
        )


# --------------------------------------------------------------------------
# Startup
# --------------------------------------------------------------------------

async def restore_jobs(application: Application) -> None:
    """Re-create every chat's daily job after a restart."""
    data = load_data()
    restored = 0
    for chat_id_str, chat in data.items():
        if chat.get("onboarded") and chat.get("reminders_on", True):
            schedule_daily(
                application,
                int(chat_id_str),
                chat.get("hour", DEFAULT_HOUR),
                chat.get("minute", DEFAULT_MINUTE),
            )
            restored += 1
    log.info("Restored %d daily job(s).", restored)


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit(
            "BOT_TOKEN is not set.\n"
            "Get a token from @BotFather on Telegram, then:\n"
            '  export BOT_TOKEN="123456:ABC-your-token"'
        )

    app = Application.builder().token(BOT_TOKEN).post_init(restore_jobs).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("menu", cmd_help))
    app.add_handler(CommandHandler("addgoal", cmd_addgoal))
    app.add_handler(CommandHandler("finishgoal", cmd_finishgoal))
    app.add_handler(CommandHandler("goals", cmd_goals))
    app.add_handler(CommandHandler("log", cmd_log))
    app.add_handler(CommandHandler("checkin", cmd_checkin))
    app.add_handler(CommandHandler("settime", cmd_settime))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("milestone", cmd_milestone))
    app.add_handler(CallbackQueryHandler(callback_finish, pattern=r"^finish_\d+$"))
    app.add_handler(CallbackQueryHandler(callback_gain, pattern=r"^gain(yes|no)_\d+$"))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    log.info("Bot starting (timezone: %s)...", TIMEZONE)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
