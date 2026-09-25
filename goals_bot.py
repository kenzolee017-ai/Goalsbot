#!/usr/bin/env python3
"""
Goal Tracker + Building Fund Telegram Bot
==========================================

Button-driven bot with two trackers:

  BUILDING FUND
    - one-time setup: "what sacrifice will you make every day?"
    - "Upload Log" asks: today's sacrifice, how much saved, optional photo
    - tracks a running total, celebrated with a rotating congrats message
    - "See what others logged" shows a shared feed (name, CG, date, photo,
      sacrifice, and that entry's amount - never anyone's running total)

  GOALS TRACKER
    - loop to add one or more goals (goal, target date/duration, daily plan)
    - "another goal?" + an Edit button on every goal-added confirmation
    - daily log entries, optional photo, milestone broadcasts, etc.

Onboarding: tap "Start" -> name -> CG -> main menu (Building Fund / Goals
Tracker). Every day at a set time (default 10pm) the bot pings "Update your
logs!" with buttons for both trackers.

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

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Update,
)
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
PHOTOS_PER_ALBUM = 10       # Telegram's media-group limit
MAX_OTHERS_FEED = 20

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("goals-bot")

# ---- conversation stages -------------------------------------------------
AWAITING_START = "awaiting_start"
AWAITING_NAME = "awaiting_name"
AWAITING_CG = "awaiting_cg"
READY = "ready"

# goals tracker
AWAITING_GOAL_TEXT = "awaiting_goal_text"
AWAITING_GOAL_TIMELINE = "awaiting_goal_timeline"
AWAITING_GOAL_DAILY = "awaiting_goal_daily"
AWAITING_PHOTO = "awaiting_photo"
AWAITING_GAIN_MESSAGE = "awaiting_gain_message"
AWAITING_MILESTONE = "awaiting_milestone"
AWAITING_EDIT_VALUE = "awaiting_edit_value"
GOALS_AWAITING_DAILY_LOG = "goals_awaiting_daily_log"

# building fund
BF_AWAITING_SACRIFICE_SETUP = "bf_awaiting_sacrifice_setup"
BF_AWAITING_TODAY_SACRIFICE = "bf_awaiting_today_sacrifice"
BF_AWAITING_AMOUNT = "bf_awaiting_amount"
BF_AWAITING_PHOTO = "bf_awaiting_photo"

BF_LOG_MESSAGES = [
    "🎉 LOGGED! You're awesome :) You've saved ${total:.2f} so far! Keep going 💪",
    "✅ Nice one! That's ${total:.2f} saved in total now. You're on fire 🔥",
    "🙌 Logged! Total saved so far: ${total:.2f}. Every bit counts 🌱",
]

NOT_ONBOARDED_MSG = "Let's finish setting you up first - send /start 🙂"


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
    """Fetch (or create) a chat record, backfilling any missing fields."""
    chat = data.setdefault(str(chat_id), {})
    chat.setdefault("stage", AWAITING_START)
    chat.setdefault("name", "")
    chat.setdefault("cg", "")
    chat.setdefault("onboarded", False)
    # goals tracker
    chat.setdefault("goals", [])
    chat.setdefault("next_goal_id", 1)
    chat.setdefault("entries", [])
    chat.setdefault("start_date", None)
    chat.setdefault("temp_goal", {})
    chat.setdefault("pending_entry_index", None)
    chat.setdefault("pending_finish_goal_id", None)
    chat.setdefault("pending_edit", {})
    # building fund
    chat.setdefault("bf_sacrifice", "")
    chat.setdefault("bf_setup", False)
    chat.setdefault("bf_entries", [])
    chat.setdefault("pending_bf_entry", {})
    # shared
    chat.setdefault("hour", DEFAULT_HOUR)
    chat.setdefault("minute", DEFAULT_MINUTE)
    chat.setdefault("reminders_on", True)
    return chat


def require_onboarded(chat: Dict[str, Any]) -> bool:
    return bool(chat.get("onboarded"))


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


def parse_target_date(text: str, start: date) -> Optional[str]:
    """Accepts either 'dd/mm/yy(yy)' or a duration like '30 days' / '3 months'."""
    text = text.strip()

    m = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})", text)
    if m:
        d, mo, y = (int(g) for g in m.groups())
        if y < 100:
            y += 2000
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            return None

    m2 = re.search(r"(\d+)\s*(day|week|month)s?", text.lower())
    if m2:
        n, unit = int(m2.group(1)), m2.group(2)
        if unit == "day":
            delta = timedelta(days=n)
        elif unit == "week":
            delta = timedelta(weeks=n)
        else:  # month (approximate)
            delta = timedelta(days=30 * n)
        return (start + delta).isoformat()

    return None


def parse_amount(text: str) -> Optional[float]:
    m = re.search(r"[\d,]+(\.\d+)?", text)
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def days_left_str(target_date_iso: Optional[str]) -> str:
    if not target_date_iso:
        return ""
    target = date.fromisoformat(target_date_iso)
    delta = (target - today()).days
    if delta > 1:
        return f"{delta} days left"
    if delta == 1:
        return "1 day left"
    if delta == 0:
        return "Last day! ⏰"
    return f"Overdue by {abs(delta)} day(s) ⚠️"


def active_goals(chat: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [g for g in chat.get("goals", []) if isinstance(g, dict) and g.get("status") == "active"]


def bf_total_saved(chat: Dict[str, Any]) -> float:
    return sum((e.get("amount") or 0) for e in chat.get("bf_entries", []))


def signature(chat: Dict[str, Any]) -> str:
    name = chat.get("name") or "Someone"
    cg = chat.get("cg") or ""
    return f"{name}, {cg}" if cg else name


def format_goals(chat: Dict[str, Any], only_active: bool = False) -> str:
    goals = [g for g in chat.get("goals", []) if isinstance(g, dict)]
    if only_active:
        goals = [g for g in goals if g.get("status") == "active"]
    if not goals:
        return "No active goals right now 🌱" if only_active else "No goals yet 🌱"

    lines = []
    for g in goals:
        icon = "✅" if g.get("status") == "completed" else "🔵"
        line = f"{icon} {g.get('text', '')}"
        extras = []
        if g.get("timeline"):
            dl = days_left_str(g.get("target_date")) if g.get("status") == "active" else ""
            tl = f"🗓️ {g['timeline']}"
            if dl:
                tl += f" ({dl})"
            extras.append(tl)
        if g.get("daily_action"):
            extras.append(f"🔁 {g['daily_action']}")
        if extras:
            line += "\n    " + "   |   ".join(extras)
        if g.get("status") == "completed" and g.get("completed"):
            line += f"\n    🏁 Completed {g['completed']}"
        lines.append(line)
    return "\n\n".join(lines)


def format_goals_checkin(chat: Dict[str, Any]) -> str:
    goals = active_goals(chat)
    if not goals:
        return "No active goals right now 🌱"
    lines = []
    for g in goals:
        dl = days_left_str(g.get("target_date"))
        suffix = f" — {dl}" if dl else ""
        lines.append(f"🔵 {g['text']}{suffix}")
    return "\n".join(lines)


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
        lines.insert(0, f"...({hidden} earlier {'entry' if hidden == 1 else 'entries'} - see full log)")
    return "\n".join(lines)


def format_bf_log(chat: Dict[str, Any], limit: Optional[int] = None) -> str:
    entries = chat.get("bf_entries", [])
    if not entries:
        return ""
    shown = entries if limit is None else entries[-limit:]
    lines = []
    for e in shown:
        camera = " 📸" if e.get("photo_file_id") else ""
        lines.append(f"📅 {e['date']} — {e.get('sacrifice_today', '')} — ${e.get('amount', 0):.2f}{camera}")
    if limit is not None and len(entries) > limit:
        hidden = len(entries) - limit
        lines.insert(0, f"...({hidden} earlier entr{'y' if hidden == 1 else 'ies'} - showing most recent {limit})")
    return "\n".join(lines)


def checkin_message(chat: Dict[str, Any]) -> str:
    parts = [
        f"📝 Day {day_number(chat)} Check-in",
        "",
        "🎯 Your goals:",
        format_goals_checkin(chat),
        "",
        "What have you done today to keep track of your goals?",
    ]
    history = format_log(chat, limit=MAX_LOG_LINES_IN_PROMPT)
    if history:
        parts += ["", "📖 Your log so far:", history]
    parts += ["", "Reply with what you did today - I'll then ask if you want to add a photo 📸"]
    return "\n".join(parts)


def start_button_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Start", callback_data="do_start")]])


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


async def send_photo_gallery(chat_id: int, context: ContextTypes.DEFAULT_TYPE, chat: Dict[str, Any]) -> None:
    photo_entries = [e for e in chat.get("entries", []) if e.get("photo_file_id")]
    if not photo_entries:
        await context.bot.send_message(chat_id=chat_id, text="No photos logged yet 📸 - add one next time you log your day!")
        return
    await context.bot.send_message(chat_id=chat_id, text=f"📸 Your photo log ({len(photo_entries)} photo(s)):")
    for i in range(0, len(photo_entries), PHOTOS_PER_ALBUM):
        chunk = photo_entries[i:i + PHOTOS_PER_ALBUM]
        media = [
            InputMediaPhoto(media=e["photo_file_id"], caption=f"Day {e['day']}: {e['text'][:60]}")
            for e in chunk
        ]
        await context.bot.send_media_group(chat_id=chat_id, media=media)


async def send_bf_photo_gallery(chat_id: int, context: ContextTypes.DEFAULT_TYPE, chat: Dict[str, Any]) -> None:
    photo_entries = [e for e in chat.get("bf_entries", []) if e.get("photo_file_id")]
    if not photo_entries:
        await context.bot.send_message(chat_id=chat_id, text="No Building Fund photos yet 📸")
        return
    await context.bot.send_message(chat_id=chat_id, text=f"📸 Your Building Fund photos ({len(photo_entries)}):")
    for i in range(0, len(photo_entries), PHOTOS_PER_ALBUM):
        chunk = photo_entries[i:i + PHOTOS_PER_ALBUM]
        media = [
            InputMediaPhoto(media=e["photo_file_id"], caption=f"{e['date']}: {e.get('sacrifice_today', '')[:60]} (${e.get('amount', 0):.2f})")
            for e in chunk
        ]
        await context.bot.send_media_group(chat_id=chat_id, media=media)


# --------------------------------------------------------------------------
# Action functions - the reusable core logic behind both buttons and
# slash commands. Each takes (chat_id, context) and does everything itself.
# --------------------------------------------------------------------------

async def action_show_main_menu(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    chat = get_chat(data, chat_id)
    save_data(data)
    name = chat.get("name") or ""
    text = f"What would you like to work on{', ' + name if name else ''}? 👇"
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🏗️ Building Fund", callback_data="menu_bf"),
        InlineKeyboardButton("🎯 Goals Tracker", callback_data="menu_goals"),
    ]])
    await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard)


# ---- Building Fund --------------------------------------------------------

async def action_enter_building_fund(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    chat = get_chat(data, chat_id)
    if not require_onboarded(chat):
        await context.bot.send_message(chat_id=chat_id, text=NOT_ONBOARDED_MSG)
        return
    if not chat.get("bf_setup"):
        chat["stage"] = BF_AWAITING_SACRIFICE_SETUP
        save_data(data)
        await context.bot.send_message(
            chat_id=chat_id,
            text="🏗️ Welcome to the Building Fund!\n\nWhat's a sacrifice you'll make every day to help you save? (e.g. 'no bubble tea') 💪",
        )
        return
    await action_show_bf_menu(chat_id, context)


async def action_show_bf_menu(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    chat = get_chat(data, chat_id)
    total = bf_total_saved(chat)
    text = (
        f"🏗️ Building Fund\n\n"
        f"💪 Your daily sacrifice: {chat.get('bf_sacrifice') or '-'}\n"
        f"💰 Total saved: ${total:.2f}\n\n"
        "What would you like to do?"
    )
    keyboard = [
        [InlineKeyboardButton("📝 Upload Log", callback_data="bf_upload"),
         InlineKeyboardButton("💰 Check Amount Saved", callback_data="bf_check_saved")],
        [InlineKeyboardButton("📖 See All Logs", callback_data="bf_see_logs"),
         InlineKeyboardButton("👀 See What Others Logged", callback_data="bf_see_others")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="menu_main")],
    ]
    await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=InlineKeyboardMarkup(keyboard))


async def action_start_bf_upload(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    chat = get_chat(data, chat_id)
    chat["stage"] = BF_AWAITING_TODAY_SACRIFICE
    chat["pending_bf_entry"] = {}
    save_data(data)
    await context.bot.send_message(chat_id=chat_id, text="💪 What sacrifice did you make today?")


async def action_reminder_bf(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    chat = get_chat(data, chat_id)
    if not chat.get("bf_setup"):
        await action_enter_building_fund(chat_id, context)
        return
    total = bf_total_saved(chat)
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"🏗️ Building Fund check-in\n💰 Saved so far: ${total:.2f}\n\nLet's log today's progress!",
    )
    await action_start_bf_upload(chat_id, context)


async def action_bf_check_saved(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    chat = get_chat(data, chat_id)
    total = bf_total_saved(chat)
    count = len(chat.get("bf_entries", []))
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"💰 You've saved ${total:.2f} so far across {count} log{'s' if count != 1 else ''}! Keep it up 🙌",
    )


async def action_bf_see_logs(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    chat = get_chat(data, chat_id)
    history = format_bf_log(chat, limit=MAX_LOG_LINES_IN_PROMPT)
    if not history:
        await context.bot.send_message(chat_id=chat_id, text="Nothing logged yet in the Building Fund 📖 - tap Upload Log to start!")
        return
    has_photos = any(e.get("photo_file_id") for e in chat.get("bf_entries", []))
    if has_photos:
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("📸 See photo log", callback_data="bf_show_photos")]])
        await context.bot.send_message(chat_id=chat_id, text=f"📖 Your Building Fund log:\n\n{history}", reply_markup=keyboard)
    else:
        await context.bot.send_message(chat_id=chat_id, text=f"📖 Your Building Fund log:\n\n{history}")


async def action_bf_see_others(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    my_id = str(chat_id)
    pairs = []
    for cid, c in data.items():
        if cid == my_id:
            continue
        for e in c.get("bf_entries", []):
            pairs.append((c, e))
    if not pairs:
        await context.bot.send_message(chat_id=chat_id, text="No one else has logged anything yet — be the first to inspire others! 🌱")
        return
    pairs.sort(key=lambda pair: pair[1].get("date", ""), reverse=True)
    pairs = pairs[:MAX_OTHERS_FEED]
    await context.bot.send_message(chat_id=chat_id, text="👀 Here's what everyone's been logging in the Building Fund:")
    for owner_chat, e in pairs:
        caption = (
            f"📅 {e['date']}\n💪 {e.get('sacrifice_today', '')}\n"
            f"💵 Saved: ${e.get('amount', 0):.2f}\n\n— {signature(owner_chat)}"
        )
        if e.get("photo_file_id"):
            try:
                await context.bot.send_photo(chat_id=chat_id, photo=e["photo_file_id"], caption=caption)
            except Exception as exc:  # noqa: BLE001
                log.warning("Failed sending bf photo: %s", exc)
                await context.bot.send_message(chat_id=chat_id, text=caption)
        else:
            await context.bot.send_message(chat_id=chat_id, text=caption)


# ---- Goals Tracker ---------------------------------------------------------

async def action_enter_goals_tracker(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    chat = get_chat(data, chat_id)
    if not require_onboarded(chat):
        await context.bot.send_message(chat_id=chat_id, text=NOT_ONBOARDED_MSG)
        return
    if not chat.get("goals"):
        chat["stage"] = AWAITING_GOAL_TEXT
        chat["temp_goal"] = {}
        save_data(data)
        await context.bot.send_message(
            chat_id=chat_id,
            text="🎯 Hi! I'm here to help you smash your goals!\n\nWhat's a goal you've set? ✨",
        )
        return
    await action_show_goals_menu(chat_id, context)


async def action_show_goals_menu(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [InlineKeyboardButton("📝 Log Today", callback_data="goals_log_today"),
         InlineKeyboardButton("➕ Add Goal", callback_data="goals_add")],
        [InlineKeyboardButton("✅ Finish Goal", callback_data="goals_finish"),
         InlineKeyboardButton("📋 My Goals", callback_data="goals_view")],
        [InlineKeyboardButton("📖 My Log", callback_data="goals_log_view"),
         InlineKeyboardButton("📸 My Photos", callback_data="goals_photos")],
        [InlineKeyboardButton("🌟 Share Milestone", callback_data="goals_milestone")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="menu_main")],
    ]
    await context.bot.send_message(
        chat_id=chat_id,
        text="🎯 Goals Tracker\n\nWhat would you like to do?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def action_start_add_goal(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    chat = get_chat(data, chat_id)
    if not require_onboarded(chat):
        await context.bot.send_message(chat_id=chat_id, text=NOT_ONBOARDED_MSG)
        return
    if chat["stage"] != READY:
        await context.bot.send_message(chat_id=chat_id, text="Let's finish what we're doing first! 🙂")
        return
    chat["stage"] = AWAITING_GOAL_TEXT
    chat["temp_goal"] = {}
    save_data(data)
    await context.bot.send_message(chat_id=chat_id, text="🎯 What's the goal you want to add?")


async def action_finish_goal_list(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    chat = get_chat(data, chat_id)
    if not require_onboarded(chat):
        await context.bot.send_message(chat_id=chat_id, text=NOT_ONBOARDED_MSG)
        return
    active = active_goals(chat)
    if not active:
        await context.bot.send_message(chat_id=chat_id, text="You don't have any active goals right now. Add one first! 🌱")
        return
    keyboard = [
        [InlineKeyboardButton(f"✅ {g['text'][:40]}", callback_data=f"finish_{g['id']}")]
        for g in active
    ]
    await context.bot.send_message(
        chat_id=chat_id,
        text="Which goal would you like to mark as complete? 🏁",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def action_show_goals(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    chat = get_chat(data, chat_id)
    if not require_onboarded(chat):
        await context.bot.send_message(chat_id=chat_id, text=NOT_ONBOARDED_MSG)
        return
    await context.bot.send_message(chat_id=chat_id, text=f"📋 Your goals:\n\n{format_goals(chat)}")


async def action_show_log(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    chat = get_chat(data, chat_id)
    if not require_onboarded(chat):
        await context.bot.send_message(chat_id=chat_id, text=NOT_ONBOARDED_MSG)
        return
    history = format_log(chat)
    if not history:
        await context.bot.send_message(chat_id=chat_id, text="Nothing logged yet - your first entry starts Day 1. 📖")
        return
    has_photos = any(e.get("photo_file_id") for e in chat.get("entries", []))
    if has_photos:
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("📸 See photo log", callback_data="show_photos")]])
        await context.bot.send_message(chat_id=chat_id, text=f"📖 Your log so far:\n\n{history}", reply_markup=keyboard)
    else:
        await context.bot.send_message(chat_id=chat_id, text=f"📖 Your log so far:\n\n{history}")


async def action_show_photos(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    chat = get_chat(data, chat_id)
    if not require_onboarded(chat):
        await context.bot.send_message(chat_id=chat_id, text=NOT_ONBOARDED_MSG)
        return
    await send_photo_gallery(chat_id, context, chat)


async def action_start_milestone(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    chat = get_chat(data, chat_id)
    if not require_onboarded(chat):
        await context.bot.send_message(chat_id=chat_id, text=NOT_ONBOARDED_MSG)
        return
    if chat["stage"] != READY:
        await context.bot.send_message(chat_id=chat_id, text="Let's finish what we're doing first! 🙂")
        return
    chat["stage"] = AWAITING_MILESTONE
    save_data(data)
    await context.bot.send_message(chat_id=chat_id, text="🌟 What would you like to share with everyone? Type it below.")


async def action_goals_log_prompt(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    chat = get_chat(data, chat_id)
    if not require_onboarded(chat):
        await context.bot.send_message(chat_id=chat_id, text=NOT_ONBOARDED_MSG)
        return
    if not chat.get("goals"):
        await action_enter_goals_tracker(chat_id, context)
        return
    await context.bot.send_message(chat_id=chat_id, text=checkin_message(chat))
    chat["stage"] = GOALS_AWAITING_DAILY_LOG
    save_data(data)


# --------------------------------------------------------------------------
# Daily job scheduling
# --------------------------------------------------------------------------

def job_name(chat_id: int) -> str:
    return f"daily-reminder-{chat_id}"


def schedule_daily(application: Application, chat_id: int, hour: int, minute: int) -> None:
    jq = application.job_queue
    if jq is None:
        log.error("JobQueue unavailable - install python-telegram-bot[job-queue].")
        return
    for job in jq.get_jobs_by_name(job_name(chat_id)):
        job.schedule_removal()
    jq.run_daily(
        daily_reminder,
        time=dtime(hour=hour, minute=minute, tzinfo=TIMEZONE),
        chat_id=chat_id,
        name=job_name(chat_id),
    )
    log.info("Scheduled daily reminder for chat %s at %02d:%02d", chat_id, hour, minute)


def unschedule_daily(application: Application, chat_id: int) -> None:
    jq = application.job_queue
    if jq is None:
        return
    for job in jq.get_jobs_by_name(job_name(chat_id)):
        job.schedule_removal()


async def daily_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = context.job.chat_id
    data = load_data()
    chat = data.get(str(chat_id))
    if not chat or not chat.get("onboarded") or not chat.get("reminders_on", True):
        return
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🏗️ Building Fund", callback_data="reminder_bf"),
        InlineKeyboardButton("🎯 Goals Tracker", callback_data="reminder_goals"),
    ]])
    await context.bot.send_message(
        chat_id=chat_id,
        text="🔔 Update your logs!\n\nWhat would you like to work on today?",
        reply_markup=keyboard,
    )


# --------------------------------------------------------------------------
# Command handlers (still work if typed; buttons are the primary UI)
# --------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    data = load_data()
    chat = get_chat(data, chat_id)

    if chat.get("onboarded"):
        save_data(data)
        await update.message.reply_text(f"👋 Welcome back, {chat.get('name') or 'there'}!")
        await action_show_main_menu(chat_id, context)
        return

    chat["stage"] = AWAITING_START
    save_data(data)
    await update.message.reply_text(
        "👋 Welcome to the Goal Tracker Bot!\n\nTap below to get started.",
        reply_markup=start_button_markup(),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    chat = get_chat(data, update.effective_chat.id)
    if not require_onboarded(chat):
        await update.message.reply_text(NOT_ONBOARDED_MSG)
        return
    await action_show_main_menu(update.effective_chat.id, context)


async def cmd_addgoal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await action_start_add_goal(update.effective_chat.id, context)


async def cmd_finishgoal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await action_finish_goal_list(update.effective_chat.id, context)


async def cmd_goals(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await action_show_goals(update.effective_chat.id, context)


async def cmd_photos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await action_show_photos(update.effective_chat.id, context)


async def cmd_log(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await action_show_log(update.effective_chat.id, context)


async def cmd_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await action_goals_log_prompt(update.effective_chat.id, context)


async def cmd_milestone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await action_start_milestone(update.effective_chat.id, context)


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


# --------------------------------------------------------------------------
# Callback (inline button) handlers
# --------------------------------------------------------------------------

async def callback_do_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    data = load_data()
    chat = get_chat(data, chat_id)
    if chat.get("onboarded"):
        await query.edit_message_text(f"👋 Welcome back, {chat.get('name') or 'there'}!")
        await action_show_main_menu(chat_id, context)
        return
    chat["stage"] = AWAITING_NAME
    save_data(data)
    await query.edit_message_text("Let's get you set up! 🙌\n\nWhat's your name?")


async def callback_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    if query.data == "menu_main":
        await action_show_main_menu(chat_id, context)
    elif query.data == "menu_bf":
        await action_enter_building_fund(chat_id, context)
    elif query.data == "menu_goals":
        await action_enter_goals_tracker(chat_id, context)


async def callback_bf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    d = query.data
    if d == "bf_upload":
        await action_start_bf_upload(chat_id, context)
    elif d == "bf_check_saved":
        await action_bf_check_saved(chat_id, context)
    elif d == "bf_see_logs":
        await action_bf_see_logs(chat_id, context)
    elif d == "bf_see_others":
        await action_bf_see_others(chat_id, context)


async def callback_bf_show_photos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    data = load_data()
    chat = get_chat(data, chat_id)
    await send_bf_photo_gallery(chat_id, context, chat)


async def callback_goals(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    data = load_data()
    chat = get_chat(data, chat_id)
    d = query.data

    if d == "goals_log_today":
        await action_goals_log_prompt(chat_id, context)
    elif d == "goals_add":
        await action_start_add_goal(chat_id, context)
    elif d == "goals_finish":
        await action_finish_goal_list(chat_id, context)
    elif d == "goals_view":
        await action_show_goals(chat_id, context)
    elif d == "goals_log_view":
        await action_show_log(chat_id, context)
    elif d == "goals_photos":
        await action_show_photos(chat_id, context)
    elif d == "goals_milestone":
        await action_start_milestone(chat_id, context)
    elif d == "goals_addanother":
        chat["stage"] = AWAITING_GOAL_TEXT
        chat["temp_goal"] = {}
        save_data(data)
        await context.bot.send_message(chat_id=chat_id, text="🎯 What's the next goal?")
    elif d == "goals_done":
        chat["stage"] = READY
        save_data(data)
        n = len(chat.get("goals", []))
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"🎉 You've got {n} goal{'s' if n != 1 else ''} set. Let's go!",
        )
        await action_show_goals_menu(chat_id, context)


async def callback_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    if query.data == "reminder_bf":
        await action_reminder_bf(chat_id, context)
    else:
        await action_goals_log_prompt(chat_id, context)


async def callback_editgoal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    goal_id = query.data.split("_", 1)[1]
    keyboard = [
        [InlineKeyboardButton("✏️ Goal text", callback_data=f"editfield_text_{goal_id}")],
        [InlineKeyboardButton("🗓️ Timeline", callback_data=f"editfield_timeline_{goal_id}")],
        [InlineKeyboardButton("🔁 Daily action", callback_data=f"editfield_daily_{goal_id}")],
    ]
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="What would you like to edit? ✏️",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def callback_editfield(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    _, field, goal_id_str = query.data.split("_", 2)

    data = load_data()
    chat = get_chat(data, chat_id)
    chat["pending_edit"] = {"goal_id": int(goal_id_str), "field": field}
    chat["stage"] = AWAITING_EDIT_VALUE
    save_data(data)

    prompts = {
        "text": "✏️ What should the goal be now?",
        "timeline": "🗓️ New target date (dd/mm/yy) or duration (e.g. '30 days')?",
        "daily": "🔁 What will you do daily now?",
    }
    await query.edit_message_text(prompts.get(field, "What's the new value?"))


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

    await broadcast(
        context.application, data,
        f"🎉🏆 Just completed a goal:\n\"{goal['text']}\"! 👏👏\n\n— {signature(chat)}",
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


async def callback_show_photos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    data = load_data()
    chat = get_chat(data, chat_id)
    await send_photo_gallery(chat_id, context, chat)


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

    # ---- brand new / not yet tapped Start ----------------------------------
    if stage == AWAITING_START:
        save_data(data)
        await update.message.reply_text(
            "👋 Welcome to the Goal Tracker Bot!\n\nTap below to get started.",
            reply_markup=start_button_markup(),
        )
        return

    # ---- onboarding: name ---------------------------------------------------
    if stage == AWAITING_NAME:
        chat["name"] = text
        chat["stage"] = AWAITING_CG
        save_data(data)
        await update.message.reply_text(f"Nice to meet you, {text}! 🙌\n\nWhat's your CG?")
        return

    # ---- onboarding: CG -------------------------------------------------------
    if stage == AWAITING_CG:
        chat["cg"] = text
        chat["onboarded"] = True
        chat["start_date"] = today().isoformat()
        chat["stage"] = READY
        save_data(data)
        schedule_daily(context.application, chat_id, chat["hour"], chat["minute"])
        await update.message.reply_text(f"Awesome, {chat['name']}! 🙌 You're all set.")
        await action_show_main_menu(chat_id, context)
        return

    # ---- add-goal flow: goal text ----------------------------------------
    if stage == AWAITING_GOAL_TEXT:
        chat["temp_goal"] = {"text": text}
        chat["stage"] = AWAITING_GOAL_TIMELINE
        save_data(data)
        await update.message.reply_text(
            f"Got it - \"{text}\" 🎯\n\n"
            "🗓️ When do you want to finish this by? Enter a date (dd/mm/yy) "
            "or a duration like '30 days' / '3 months'."
        )
        return

    # ---- add-goal flow: target date / duration -------------------------------
    if stage == AWAITING_GOAL_TIMELINE:
        target = parse_target_date(text, today())
        if not target:
            await update.message.reply_text(
                "Hmm, I couldn't read that 🤔 Try a date like 25/12/26, or a duration like '30 days' / '3 months'."
            )
            return
        chat["temp_goal"]["timeline"] = text
        chat["temp_goal"]["target_date"] = target
        chat["stage"] = AWAITING_GOAL_DAILY
        save_data(data)
        await update.message.reply_text("🔥 And what will you do every day to work toward it?")
        return

    # ---- add-goal flow: daily action, then save ---------------------------
    if stage == AWAITING_GOAL_DAILY:
        tg = chat["temp_goal"]
        goal_id = chat.get("next_goal_id", 1)
        goal = {
            "id": goal_id,
            "text": tg.get("text", ""),
            "timeline": tg.get("timeline", ""),
            "daily_action": text,
            "status": "active",
            "created": today().isoformat(),
            "completed": None,
            "target_date": tg.get("target_date"),
        }
        chat["goals"].append(goal)
        chat["next_goal_id"] = goal_id + 1
        chat["temp_goal"] = {}
        chat["stage"] = READY
        save_data(data)

        dl = days_left_str(goal["target_date"])
        confirm = f"✅ Goal logged!\n\n🔵 {goal['text']}\n🗓️ {goal['timeline']}"
        if dl:
            confirm += f" ({dl})"
        confirm += f"\n🔁 {goal['daily_action']}"

        keyboard = [
            [InlineKeyboardButton("➕ Set another goal", callback_data="goals_addanother"),
             InlineKeyboardButton("✅ Done", callback_data="goals_done")],
            [InlineKeyboardButton("✏️ Edit this goal", callback_data=f"editgoal_{goal['id']}")],
        ]
        await update.message.reply_text(confirm, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    # ---- editing an existing goal's field ------------------------------------
    if stage == AWAITING_EDIT_VALUE:
        pe = chat.get("pending_edit", {})
        goal = next((g for g in chat["goals"] if g.get("id") == pe.get("goal_id")), None)
        if not goal:
            chat["stage"] = READY
            chat["pending_edit"] = {}
            save_data(data)
            await update.message.reply_text("That goal isn't available anymore.")
            return

        field = pe.get("field")
        if field == "timeline":
            target = parse_target_date(text, today())
            if not target:
                await update.message.reply_text(
                    "Hmm, I couldn't read that 🤔 Try a date like 25/12/26, or a duration like '30 days'."
                )
                return
            goal["timeline"] = text
            goal["target_date"] = target
        elif field == "text":
            goal["text"] = text
        elif field == "daily":
            goal["daily_action"] = text

        chat["stage"] = READY
        chat["pending_edit"] = {}
        save_data(data)

        dl = days_left_str(goal.get("target_date"))
        confirm = f"✅ Updated!\n\n🔵 {goal['text']}\n🗓️ {goal['timeline']}"
        if dl:
            confirm += f" ({dl})"
        confirm += f"\n🔁 {goal['daily_action']}"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✏️ Edit again", callback_data=f"editgoal_{goal['id']}")]])
        await update.message.reply_text(confirm, reply_markup=keyboard)
        return

    # ---- milestone broadcast ----------------------------------------------
    if stage == AWAITING_MILESTONE:
        msg = f"🌟 MILESTONES 🌟\n\n{text}\n\n— {signature(chat)}"
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
        msg = f"💬 What I gained from completing \"{goal_text}\":\n\"{text}\"\n\n— {signature(chat)}"
        await broadcast(context.application, data, msg, exclude_chat_id=chat_id)
        chat["stage"] = READY
        chat["pending_finish_goal_id"] = None
        save_data(data)
        await update.message.reply_text("Shared with everyone! 🎉🙌")
        return

    # ---- expecting a goals-tracker photo, got text instead -> skip -------------
    if stage == AWAITING_PHOTO:
        chat["stage"] = READY
        chat["pending_entry_index"] = None
        save_data(data)
        await update.message.reply_text("No worries, logged without a photo 📝👍")
        return

    # ---- goals tracker: today's log entry (explicit, via button/reminder) -----
    if stage == GOALS_AWAITING_DAILY_LOG:
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
        return

    # ---- building fund: one-time sacrifice-commitment setup -------------------
    if stage == BF_AWAITING_SACRIFICE_SETUP:
        chat["bf_sacrifice"] = text
        chat["bf_setup"] = True
        chat["stage"] = READY
        save_data(data)
        await update.message.reply_text(f"💪 Love it! Your daily sacrifice: \"{text}\"")
        await action_show_bf_menu(chat_id, context)
        return

    # ---- building fund: today's sacrifice ---------------------------------
    if stage == BF_AWAITING_TODAY_SACRIFICE:
        chat["pending_bf_entry"] = {"sacrifice_today": text}
        chat["stage"] = BF_AWAITING_AMOUNT
        save_data(data)
        await update.message.reply_text("💵 How much did you save today? (just the number, e.g. 5 or 5.50)")
        return

    # ---- building fund: amount saved ---------------------------------------
    if stage == BF_AWAITING_AMOUNT:
        amount = parse_amount(text)
        if amount is None:
            await update.message.reply_text(
                "Hmm, I couldn't read a number there 🤔 How much did you save today? (e.g. 5 or 5.50)"
            )
            return
        chat["pending_bf_entry"]["amount"] = amount
        chat["stage"] = BF_AWAITING_PHOTO
        save_data(data)
        await update.message.reply_text("📸 Got a photo of today's sacrifice? Send it now, or type 'skip'.")
        return

    # ---- building fund: expecting a photo, got text -> finalize without one ---
    if stage == BF_AWAITING_PHOTO:
        entry = chat.get("pending_bf_entry", {})
        entry["date"] = today().isoformat()
        entry["photo_file_id"] = None
        chat.setdefault("bf_entries", []).append(entry)
        chat["pending_bf_entry"] = {}
        chat["stage"] = READY
        save_data(data)
        total = bf_total_saved(chat)
        msg = BF_LOG_MESSAGES[(len(chat["bf_entries"]) - 1) % len(BF_LOG_MESSAGES)].format(total=total)
        await update.message.reply_text(msg)
        return

    # ---- default: idle + unexpected text -> nudge toward the menu -------------
    await update.message.reply_text("Not sure what you mean! Here's the menu 👇")
    await action_show_main_menu(chat_id, context)


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    data = load_data()
    chat = get_chat(data, chat_id)
    stage = chat["stage"]

    if stage == AWAITING_PHOTO and chat.get("pending_entry_index") is not None:
        idx = chat["pending_entry_index"]
        file_id = update.message.photo[-1].file_id
        day = day_number(chat)
        if 0 <= idx < len(chat["entries"]):
            chat["entries"][idx]["photo_file_id"] = file_id
            day = chat["entries"][idx]["day"]
        chat["stage"] = READY
        chat["pending_entry_index"] = None
        save_data(data)
        await update.message.reply_text(f"📸 Photo added to Day {day}! Nice work today 💪")
        return

    if stage == BF_AWAITING_PHOTO:
        entry = chat.get("pending_bf_entry", {})
        entry["date"] = today().isoformat()
        entry["photo_file_id"] = update.message.photo[-1].file_id
        chat.setdefault("bf_entries", []).append(entry)
        chat["pending_bf_entry"] = {}
        chat["stage"] = READY
        save_data(data)
        total = bf_total_saved(chat)
        msg = BF_LOG_MESSAGES[(len(chat["bf_entries"]) - 1) % len(BF_LOG_MESSAGES)].format(total=total)
        await update.message.reply_text(msg)
        return

    await update.message.reply_text(
        "Thanks for the photo! 📸 Tell me what you did today first, and I'll ask if you want to attach one."
    )


# --------------------------------------------------------------------------
# Startup
# --------------------------------------------------------------------------

async def on_startup(application: Application) -> None:
    await application.bot.set_my_commands([
        BotCommand("start", "Get started / show menu"),
        BotCommand("menu", "Show the main menu"),
        BotCommand("help", "Show the main menu"),
    ])

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

    app = Application.builder().token(BOT_TOKEN).post_init(on_startup).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("menu", cmd_help))
    app.add_handler(CommandHandler("addgoal", cmd_addgoal))
    app.add_handler(CommandHandler("finishgoal", cmd_finishgoal))
    app.add_handler(CommandHandler("goals", cmd_goals))
    app.add_handler(CommandHandler("photos", cmd_photos))
    app.add_handler(CommandHandler("log", cmd_log))
    app.add_handler(CommandHandler("checkin", cmd_checkin))
    app.add_handler(CommandHandler("settime", cmd_settime))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("milestone", cmd_milestone))

    app.add_handler(CallbackQueryHandler(callback_do_start, pattern=r"^do_start$"))
    app.add_handler(CallbackQueryHandler(callback_menu, pattern=r"^menu_(main|bf|goals)$"))
    app.add_handler(CallbackQueryHandler(callback_bf, pattern=r"^bf_(upload|check_saved|see_logs|see_others)$"))
    app.add_handler(CallbackQueryHandler(callback_bf_show_photos, pattern=r"^bf_show_photos$"))
    app.add_handler(CallbackQueryHandler(
        callback_goals,
        pattern=r"^goals_(log_today|add|finish|view|log_view|photos|milestone|addanother|done)$",
    ))
    app.add_handler(CallbackQueryHandler(callback_reminder, pattern=r"^reminder_(bf|goals)$"))
    app.add_handler(CallbackQueryHandler(callback_editgoal, pattern=r"^editgoal_\d+$"))
    app.add_handler(CallbackQueryHandler(callback_editfield, pattern=r"^editfield_(text|timeline|daily)_\d+$"))
    app.add_handler(CallbackQueryHandler(callback_finish, pattern=r"^finish_\d+$"))
    app.add_handler(CallbackQueryHandler(callback_gain, pattern=r"^gain(yes|no)_\d+$"))
    app.add_handler(CallbackQueryHandler(callback_show_photos, pattern=r"^show_photos$"))

    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    log.info("Bot starting (timezone: %s)...", TIMEZONE)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
