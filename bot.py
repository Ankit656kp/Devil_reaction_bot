# tg_broadcast_bot.py  — Full working with OWNER/ADMIN/USER commands
# Requirements:
#   pip install python-telegram-bot==21.10 motor==3.6 "pymongo[srv]"
#
# ENV VARS (recommended) or fill constants below:
#   BOT_TOKEN, MONGO_URI, OWNER_ID, SUPPORT_URL, PROMO_URL
#
# Notes:
# - Bot cannot auto-join groups/channels from invite links. You (or an admin) must add it.
# - Channels: channel posts (channel_post updates) tabhi milte hain jab bot ko channel me add kiya jata hai.
# - Groups: jab koi @BotUsername se bot ko mention kare, tab us message par auto-reaction lagega.
# - This build adds: full admin mgmt, ban/unban, mute/unmute (group restrict), warn/clearwarn,
#   backup to JSON, reload settings, update/restart, set/del reaction, set promotion, user settings, help/about.

import os
import sys
import re
import asyncio
import math
import tempfile
import json
from datetime import datetime, timezone, timedelta

from motor.motor_asyncio import AsyncIOMotorClient
from bson import json_util
from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, Chat, ChatMemberUpdated,
    ReactionTypeEmoji, ChatPermissions
)
from telegram.constants import ParseMode, ChatType
from telegram.ext import (
    Application, AIORateLimiter, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes, ChatMemberHandler
)
from telegram.error import Forbidden

# ===================== CONFIG =====================
BOT_TOKEN   = os.getenv("BOT_TOKEN",   "YOUR_BOT_TOKEN_HERE")
MONGO_URI   = os.getenv("MONGO_URI",   "mongodb://localhost:27017")
OWNER_ID    = int(os.getenv("OWNER_ID", "123456789"))
SUPPORT_URL = os.getenv("SUPPORT_URL", "https://t.me/YourSupportHandle")
PROMO_URL   = os.getenv("PROMO_URL",   "https://t.me/YourPromoHandle")

DB_NAME           = "broadcast_bot"
COL_CHATS         = "chats"
COL_ADMINS        = "admins"
COL_BCAST_LOGS    = "broadcast_logs"
COL_SETTINGS      = "settings"
COL_USERS         = "users"          # bans, personal settings, etc.
COL_WARNLOG       = "warn_logs"      # warn history

# Broadcast tuning
CONCURRENCY = 15
SLEEP_EVERY = 25
SLEEP_TIME  = 1.0

# Defaults
DEFAULT_REACTION_EMOJI = "👍"
DEFAULT_WARN_LIMIT = 3

# ===================== DB SETUP =====================
mongo = AsyncIOMotorClient(MONGO_URI)
db = mongo[DB_NAME]
chats_col   = db[COL_CHATS]
admins_col  = db[COL_ADMINS]
bclogs_col  = db[COL_BCAST_LOGS]
settings_col= db[COL_SETTINGS]
users_col   = db[COL_USERS]
warnlog_col = db[COL_WARNLOG]

# ===================== HELPERS =====================
def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID

async def is_admin(user_id: int) -> bool:
    if is_owner(user_id):
        return True
    doc = await admins_col.find_one({"_id": user_id})
    return doc is not None

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def fmt_chat(chat_doc) -> str:
    title = chat_doc.get("title") or chat_doc.get("username") or str(chat_doc["_id"])
    ctype = chat_doc.get("type", "?")
    blocked = "🚫" if chat_doc.get("blocked", False) else "✅"
    return f"{blocked} `{chat_doc['_id']}` • *{ctype}* • {title}"

async def upsert_chat(chat: Chat):
    doc = {
        "_id": chat.id,
        "type": chat.type,
        "title": chat.title if chat.title else chat.username,
        "username": chat.username,
        "blocked": False,
        "updated_at": now_iso(),
        "joined_at": now_iso(),
    }
    await chats_col.update_one(
        {"_id": chat.id},
        {
            "$setOnInsert": doc,
            "$set": {
                "type": chat.type,
                "title": doc["title"],
                "username": chat.username,
                "updated_at": now_iso(),
            },
        },
        upsert=True,
    )

async def mark_left(chat_id: int):
    await chats_col.update_one({"_id": chat_id}, {"$set": {"left_at": now_iso()}})

async def get_reaction_emoji() -> str:
    s = await settings_col.find_one({"_id": "reaction"})
    return (s or {}).get("emoji", DEFAULT_REACTION_EMOJI)

async def set_reaction_emoji(emoji: str):
    await settings_col.update_one({"_id": "reaction"}, {"$set": {"emoji": emoji, "updated_at": now_iso()}}, upsert=True)

async def del_reaction_emoji():
    await settings_col.delete_one({"_id": "reaction"})

async def get_warn_limit() -> int:
    s = await settings_col.find_one({"_id": "warn_limit"})
    return int((s or {}).get("value", DEFAULT_WARN_LIMIT))

async def set_warn_limit(value: int):
    await settings_col.update_one({"_id": "warn_limit"}, {"$set": {"value": int(value), "updated_at": now_iso()}}, upsert=True)

async def get_promo_url() -> str:
    s = await settings_col.find_one({"_id": "promo"})
    return (s or {}).get("url", PROMO_URL)

async def set_promo_url(url: str):
    global PROMO_URL
    PROMO_URL = url  # update runtime
    await settings_col.update_one({"_id": "promo"}, {"$set": {"url": url, "updated_at": now_iso()}}, upsert=True)

def valid_emoji(s: str) -> bool:
    return bool(s)

async def ensure_user_doc(uid: int):
    await users_col.update_one(
        {"_id": uid},
        {"$setOnInsert": {"_id": uid, "banned": False, "muted_until": None, "reaction": None, "created_at": now_iso()}},
        upsert=True
    )

async def get_user(uid: int):
    await ensure_user_doc(uid)
    return await users_col.find_one({"_id": uid})

async def set_banned(uid: int, banned: bool, reason: str = None):
    await ensure_user_doc(uid)
    await users_col.update_one({"_id": uid}, {"$set": {"banned": banned, "ban_reason": reason, "updated_at": now_iso()}})

async def set_muted_until(chat_id: int, uid: int, until: datetime | None):
    # store global mute flag (for DMs) — for groups we also try to restrict
    await ensure_user_doc(uid)
    until_iso = until.astimezone(timezone.utc).isoformat() if until else None
    await users_col.update_one({"_id": uid}, {"$set": {"muted_until": until_iso, "updated_at": now_iso()}})

async def add_warn(uid: int, by_id: int, reason: str | None):
    await ensure_user_doc(uid)
    await warnlog_col.insert_one({"uid": uid, "by": by_id, "reason": reason, "created_at": now_iso()})

async def count_warns(uid: int) -> int:
    return await warnlog_col.count_documents({"uid": uid})

async def clear_warns(uid: int):
    await warnlog_col.delete_many({"uid": uid})
    
# ===================== UI MARKUPS =====================
def owner_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📣 Broadcast", callback_data="menu:broadcast_prompt"),
         InlineKeyboardButton("📊 Stats", callback_data="menu:stats")],
        [InlineKeyboardButton("📜 List Chats", callback_data="menu:list:1"),
         InlineKeyboardButton("🚫 Block/Unblock", callback_data="menu:block_help")],
        [InlineKeyboardButton("➕ Add Admin", callback_data="menu:addadmin_help"),
         InlineKeyboardButton("👥 Admins", callback_data="menu:admins")],
        [InlineKeyboardButton("🏳️ Leave Chat", callback_data="menu:leave_help"),
         InlineKeyboardButton("🏓 Ping", callback_data="menu:ping")],
        [InlineKeyboardButton("😊 Set Reaction", callback_data="menu:setreaction_help")]
    ])

def admin_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📣 Broadcast", callback_data="menu:broadcast_prompt"),
         InlineKeyboardButton("📊 Stats", callback_data="menu:stats")],
        [InlineKeyboardButton("🏓 Ping", callback_data="menu:ping")]
    ])

async def user_menu_kb_dynamic():
    promo = await get_promo_url()
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📮 Contact Owner", url=SUPPORT_URL)],
        [InlineKeyboardButton("📢 Promotion & Support", url=promo)]
    ])

# ===================== AUTH MESSAGE =====================
async def not_authorized(update: Update):
    await update.effective_message.reply_text("🚫 You are not authorized to use this command.")

# ===================== USER COMMANDS =====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    if is_owner(user.id):
        emoji = await get_reaction_emoji()
        await update.effective_message.reply_text(
            f"👑 *Owner Panel*\nAuto‑Reaction: `{emoji}`\nControls below.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=owner_menu_kb()
        )
    elif await is_admin(user.id):
        await update.effective_message.reply_text(
            "🛡️ *Admin Panel*\nYou can broadcast & view stats.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=admin_menu_kb()
        )
    else:
        await update.effective_message.reply_text(
            "🤖 Hi! This bot helps with broadcasts.\nFor info/promotion, reach out below.",
            reply_markup=await user_menu_kb_dynamic()
        )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "*OWNER COMMANDS*\n"
        "/stats, /broadcast <msg>, /addadmin <id>, /removeadmin <id>, /reload, /backup, /update, "
        "/ban <id>, /unban <id>, /setreaction <emoji>, /delreaction, /setpromotion <url>\n\n"
        "*ADMIN COMMANDS*\n"
        "/stats, /setreaction <emoji>, /delreaction, /mute <id> [mins], /unmute <id>, /warn <id> [reason], /clearwarn <id>\n\n"
        "*USER COMMANDS*\n"
        "/start, /help, /reaction <emoji>, /mysettings, /ping, /about"
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_ts = datetime.now(timezone.utc)
    msg = await update.effective_message.reply_text("Pong...")
    end_ts = datetime.now(timezone.utc)
    delta = (end_ts - start_ts).total_seconds() * 1000
    await msg.edit_text(f"🏓 Pong! `{int(delta)} ms`", parse_mode=ParseMode.MARKDOWN)

async def about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    promo = await get_promo_url()
    await update.effective_message.reply_text(
        "ℹ️ *Bot Info*\n"
        "• Developer: Ankit\n"
        "• Stack: Python, python-telegram-bot, Motor/MongoDB\n"
        f"• Promo: {promo}",
        parse_mode=ParseMode.MARKDOWN
    )

async def reaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await ensure_user_doc(uid)
    if not context.args:
        user = await get_user(uid)
        val = user.get("reaction")
        return await update.effective_message.reply_text(f"Your reaction: {val or 'not set'}. Use /reaction <emoji> to set.")
    emoji = context.args[0]
    if not valid_emoji(emoji):
        return await update.effective_message.reply_text("Please pass a valid emoji.")
    await users_col.update_one({"_id": uid}, {"$set": {"reaction": emoji, "updated_at": now_iso()}})
    await update.effective_message.reply_text(f"Saved your reaction: {emoji}")

async def mysettings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = await get_user(uid)
    promo = await get_promo_url()
    lines = [
        "⚙️ *Your Settings*",
        f"• Banned: *{u.get('banned', False)}*",
        f"• Muted until: `{u.get('muted_until')}`",
        f"• Personal reaction: {u.get('reaction') or '—'}",
        f"• Promo: {promo}"
    ]
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

# ===================== OWNER & ADMIN =====================
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    limited = False
    if is_owner(uid):
        pass
    elif await is_admin(uid):
        limited = True
    else:
        return await not_authorized(update)

    total = await chats_col.count_documents({"left_at": {"$exists": False}})
    blocked = await chats_col.count_documents({"blocked": True, "left_at": {"$exists": False}})
    groups = await chats_col.count_documents({"type": {"$in": ["group", "supergroup"]}, "left_at": {"$exists": False}})
    channels = await chats_col.count_documents({"type": "channel", "left_at": {"$exists": False}})
    last = await bclogs_col.find_one(sort=[("created_at", -1)])

    text = [
        "📊 *Stats*",
        f"Total chats: *{total}* (groups: *{groups}*, channels: *{channels}*)",
    ]
    if not limited:
        text.append(f"Blocked: *{blocked}*")
    if last:
        text += [
            "",
            "🧾 *Last Broadcast*",
            f"At: `{last.get('created_at')}`",
            f"MessageType: `{last.get('mode')}`",
            f"Success: *{last.get('success',0)}*",
            f"Failed: *{last.get('failed',0)}*"
        ]
    await update.effective_message.reply_text("\n".join(text), parse_mode=ParseMode.MARKDOWN)

async def add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id): return await not_authorized(update)
    if not context.args: return await update.effective_message.reply_text("Usage: /addadmin <user_id>")
    uid = int(context.args[0])
    await admins_col.update_one({"_id": uid}, {"$set": {"_id": uid, "added_at": now_iso()}}, upsert=True)
    await update.effective_message.reply_text(f"✅ Added admin: `{uid}`", parse_mode=ParseMode.MARKDOWN)

async def del_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id): return await not_authorized(update)
    if not context.args: return await update.effective_message.reply_text("Usage: /removeadmin <user_id>")
    uid = int(context.args[0])
    await admins_col.delete_one({"_id": uid})
    await update.effective_message.reply_text(f"🗑️ Removed admin: `{uid}`", parse_mode=ParseMode.MARKDOWN)

# alias for /removeadmin
remove_admin = del_admin

# ---- Reload / Backup / Update ----
async def reload_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id): return await not_authorized(update)
    # For this bot most settings are pulled on-demand from DB. We just acknowledge.
    wl = await get_warn_limit()
    emoji = await get_reaction_emoji()
    promo = await get_promo_url()
    await update.effective_message.reply_text(f"🔄 Reloaded settings.\nReaction: {emoji}\nWarn limit: {wl}\nPromo: {promo}")

async def backup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id): return await not_authorized(update)
    await update.effective_message.reply_text("💾 Creating backup…")
    data = {}
    for name, col in [
        (COL_CHATS, chats_col), (COL_ADMINS, admins_col), (COL_BCAST_LOGS, bclogs_col),
        (COL_SETTINGS, settings_col), (COL_USERS, users_col), (COL_WARNLOG, warnlog_col)
    ]:
        docs = [d async for d in col.find({})]
        data[name] = json.loads(json_util.dumps(docs))
    with tempfile.NamedTemporaryFile("w+", suffix=".json", delete=False) as tf:
        tf.write(json.dumps(data, indent=2))
        tf.flush()
        path = tf.name
    try:
        await update.effective_message.reply_document(path, filename="db_backup.json", caption="✅ Backup")
    finally:
        try: os.remove(path)
        except: pass

async def update_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id): return await not_authorized(update)
    await update.effective_message.reply_text("♻️ Restarting bot…")
    # Heroku/Koyeb/VPS: process exit triggers restart (via Procfile supervisor)
    os._exit(0)

# ---- Ban / Unban ----
async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id): return await not_authorized(update)
    if not context.args: return await update.effective_message.reply_text("Usage: /ban <user_id> [reason]")
    uid = int(context.args[0])
    reason = " ".join(context.args[1:]) if len(context.args) > 1 else None
    await set_banned(uid, True, reason)
    await update.effective_message.reply_text(f"🚫 Banned user `{uid}`. {('Reason: '+reason) if reason else ''}", parse_mode=ParseMode.MARKDOWN)

async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id): return await not_authorized(update)
    if not context.args: return await update.effective_message.reply_text("Usage: /unban <user_id>")
    uid = int(context.args[0])
    await set_banned(uid, False, None)
    await update.effective_message.reply_text(f"✅ Unbanned user `{uid}`.", parse_mode=ParseMode.MARKDOWN)

# ---- Set / Del Reaction (Owner/Admin) ----
async def setreaction_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not (is_owner(uid) or await is_admin(uid)): return await not_authorized(update)
    if not context.args:
        current = await get_reaction_emoji()
        return await update.effective_message.reply_text(f"Usage: /setreaction <emoji>\nCurrent: {current}")
    emoji = context.args[0]
    if not valid_emoji(emoji): return await update.effective_message.reply_text("Please pass a valid emoji.")
    await set_reaction_emoji(emoji)
    await update.effective_message.reply_text(f"😊 Reaction emoji set to: {emoji}")

async def delreaction_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not (is_owner(uid) or await is_admin(uid)): return await not_authorized(update)
    await del_reaction_emoji()
    await update.effective_message.reply_text("❌ Default reaction removed.")

# ---- Promotion link ----
async def setpromotion_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id): return await not_authorized(update)
    if not context.args: return await update.effective_message.reply_text("Usage: /setpromotion <t.me link | @username | url>")
    arg = context.args[0].strip()
    # Normalize to URL
    if re.match(r"^@[\w_]+$", arg):
        url = f"https://t.me/{arg[1:]}"
    elif re.match(r"^https?://", arg):
        url = arg
    elif re.match(r"^[\w_]+$", arg):
        url = f"https://t.me/{arg}"
    else:
        return await update.effective_message.reply_text("Invalid promo id/link.")
    await set_promo_url(url)
    await update.effective_message.reply_text(f"📢 Promotion link set to:\n{url}")

# ---- Mute / Unmute (Admin/Owner) ----
async def mute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not (is_owner(update.effective_user.id) or await is_admin(update.effective_user.id)):
        return await not_authorized(update)
    if not context.args:
        return await update.effective_message.reply_text("Usage: /mute <user_id> [minutes]")
    uid = int(context.args[0])
    minutes = int(context.args[1]) if len(context.args) > 1 and context.args[1].isdigit() else 30
    until_dt = datetime.now(timezone.utc) + timedelta(minutes=minutes)

    # Store global mute (for DMs)
    await set_muted_until(update.effective_chat.id, uid, until_dt)

    # If in a group/supergroup, attempt telegram restrict
    chat = update.effective_chat
    if chat and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        try:
            await context.bot.restrict_chat_member(
                chat_id=chat.id,
                user_id=uid,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until_dt
            )
        except Forbidden:
            pass  # bot may not be admin
    await update.effective_message.reply_text(f"🔇 User `{uid}` muted for {minutes} min.", parse_mode=ParseMode.MARKDOWN)

async def unmute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not (is_owner(update.effective_user.id) or await is_admin(update.effective_user.id)):
        return await not_authorized(update)
    if not context.args:
        return await update.effective_message.reply_text("Usage: /unmute <user_id>")
    uid = int(context.args[0])

    await set_muted_until(update.effective_chat.id, uid, None)

    chat = update.effective_chat
    if chat and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        try:
            await context.bot.restrict_chat_member(
                chat_id=chat.id,
                user_id=uid,
                permissions=ChatPermissions(can_send_messages=True)
            )
        except Forbidden:
            pass
    await update.effective_message.reply_text(f"🔊 User `{uid}` unmuted.", parse_mode=ParseMode.MARKDOWN)

# ---- Warn / ClearWarn (Admin/Owner) ----
async def warn_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not (is_owner(update.effective_user.id) or await is_admin(update.effective_user.id)):
        return await not_authorized(update)
    if not context.args:
        return await update.effective_message.reply_text("Usage: /warn <user_id> [reason]")
    uid = int(context.args[0])
    reason = " ".join(context.args[1:]) if len(context.args) > 1 else None
    await add_warn(uid, update.effective_user.id, reason)
    count = await count_warns(uid)
    limit = await get_warn_limit()
    txt = f"⚠️ Warned `{uid}`. Total warns: *{count}*."
    if reason: txt += f"\nReason: {reason}"
    # Auto-mute on limit
    if count >= limit:
        until_dt = datetime.now(timezone.utc) + timedelta(minutes=30)
        await set_muted_until(update.effective_chat.id, uid, until_dt)
        chat = update.effective_chat
        if chat and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):

   # ===================== CHAT MGMT/UTILITY =====================
PAGE_SIZE = 10

async def list_chats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id): return await not_authorized(update)
    await send_chat_page(update.effective_chat.id, context, 1)

async def send_chat_page(chat_id: int, context: ContextTypes.DEFAULT_TYPE, page: int):
    skip = (page - 1) * PAGE_SIZE
    cursor = chats_col.find({"left_at": {"$exists": False}}).sort("_id", 1).skip(skip).limit(PAGE_SIZE)
    items = [doc async for doc in cursor]
    total = await chats_col.count_documents({"left_at": {"$exists": False}})
    pages = max(1, math.ceil(total / PAGE_SIZE))
    if not items:
        await context.bot.send_message(chat_id, "No chats."); return
    text = "📜 *Chats (page {}/{})*\n\n".format(page, pages) + "\n".join(fmt_chat(d) for d in items)
    prev_btn = InlineKeyboardButton("⬅️ Prev", callback_data=f"menu:list:{page-1}") if page > 1 else InlineKeyboardButton(" ", callback_data="noop")
    next_btn = InlineKeyboardButton("Next ➡️", callback_data=f"menu:list:{page+1}") if page < pages else InlineKeyboardButton(" ", callback_data="noop")
    kb = InlineKeyboardMarkup([[prev_btn, next_btn]])
    await context.bot.send_message(chat_id, text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

async def block_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id): return await not_authorized(update)
    if not context.args: return await update.effective_message.reply_text("Usage: /block <chat_id>")
    cid = int(context.args[0])
    await chats_col.update_one({"_id": cid}, {"$set": {"blocked": True}})
    await update.effective_message.reply_text(f"🚫 Blocked chat `{cid}`", parse_mode=ParseMode.MARKDOWN)

async def unblock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id): return await not_authorized(update)
    if not context.args: return await update.effective_message.reply_text("Usage: /unblock <chat_id>")
    cid = int(context.args[0])
    await chats_col.update_one({"_id": cid}, {"$set": {"blocked": False}})
    await update.effective_message.reply_text(f"✅ Unblocked chat `{cid}`", parse_mode=ParseMode.MARKDOWN)

async def leave_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id): return await not_authorized(update)
    if not context.args: return await update.effective_message.reply_text("Usage: /leave <chat_id>")
    cid = int(context.args[0])
    try:
        await context.bot.leave_chat(cid)
        await mark_left(cid)
        await update.effective_message.reply_text(f"🏳️ Left chat `{cid}`", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.effective_message.reply_text(f"Error leaving chat: `{e}`", parse_mode=ParseMode.MARKDOWN)

# ===================== BROADCAST =====================
async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not (is_owner(uid) or await is_admin(uid)): return await not_authorized(update)

    msg = update.effective_message
    if msg.reply_to_message:
        await do_broadcast_copy(update, context, msg.reply_to_message)
    elif context.args:
        text = " ".join(context.args)
        await do_broadcast_text(update, context, text)
    else:
        await msg.reply_text("Usage:\n- Reply to a message with /broadcast\n- Or: /broadcast Your message text")

async def _iter_target_chats():
    cursor = chats_col.find({"left_at": {"$exists": False}, "blocked": {"$ne": True}})
    async for doc in cursor:
        yield int(doc["_id"])

async def do_broadcast_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    await update.effective_message.reply_text("🚀 Broadcasting text…")
    success, failed = 0, 0
    details = []
    sem = asyncio.Semaphore(CONCURRENCY)

    async def send_one(chat_id: int):
        nonlocal success, failed
        async with sem:
            try:
                await context.bot.send_message(chat_id, text, disable_web_page_preview=True)
                success += 1
                details.append({"chat_id": chat_id, "ok": True})
            except Exception as e:
                failed += 1
                details.append({"chat_id": chat_id, "ok": False, "error": str(e)})

    i = 0
    tasks = []
    async for cid in _iter_target_chats():
        tasks.append(asyncio.create_task(send_one(cid)))
        i += 1
        if i % SLEEP_EVERY == 0:
            await asyncio.sleep(SLEEP_TIME)
    await asyncio.gather(*tasks)

    log = {"mode": "text", "created_at": now_iso(), "success": success, "failed": failed, "details": details[-50:]}
    await bclogs_col.insert_one(log)
    await update.effective_message.reply_text(f"✅ Done. Sent: *{success}*, Failed: *{failed}*", parse_mode=ParseMode.MARKDOWN)

async def do_broadcast_copy(update: Update, context: ContextTypes.DEFAULT_TYPE, src_msg):
    await update.effective_message.reply_text("🚀 Broadcasting message copy…")
    success, failed = 0, 0
    details = []
    sem = asyncio.Semaphore(CONCURRENCY)

    async def copy_one(chat_id: int):
        nonlocal success, failed
        async with sem:
            try:
                await src_msg.copy(chat_id)
                success += 1
                details.append({"chat_id": chat_id, "ok": True})
            except Exception as e:
                failed += 1
                details.append({"chat_id": chat_id, "ok": False, "error": str(e)})

    i = 0
    tasks = []
    async for cid in _iter_target_chats():
        tasks.append(asyncio.create_task(copy_one(cid)))
        i += 1
        if i % SLEEP_EVERY == 0:
            await asyncio.sleep(SLEEP_TIME)
    await asyncio.gather(*tasks)

    log = {"mode": "copy", "created_at": now_iso(), "success": success, "failed": failed, "details": details[-50:]}
    await bclogs_col.insert_one(log)
    await update.effective_message.reply_text(f"✅ Done. Sent: *{success}*, Failed: *{failed}*", parse_mode=ParseMode.MARKDOWN)

# ===================== AUTO-REACTION =====================
async def auto_react_for_group_mentions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or msg.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    bot_username = (await context.bot.get_me()).username or ""
    if not bot_username:
        return
    text = (msg.text or "") + " " + (msg.caption or "")
    mentioned = False
    if f"@{bot_username}".lower() in text.lower():
        mentioned = True
    else:
        entities = (msg.entities or []) + (msg.caption_entities or [])
        for ent in entities:
            if ent.type == "mention":
                ent_text = text[ent.offset: ent.offset + ent.length]
                if ent_text.lower() == f"@{bot_username}".lower():
                    mentioned = True
                    break
    if not mentioned:
        return
    try:
        emoji = await get_reaction_emoji()
        await context.bot.set_message_reaction(
            chat_id=msg.chat_id,
            message_id=msg.id,
            reaction=[ReactionTypeEmoji(emoji)],
            is_big=False
        )
    except Exception:
        pass

async def auto_react_for_channel_posts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or msg.chat.type != ChatType.CHANNEL:
        return
    try:
        emoji = await get_reaction_emoji()
        await context.bot.set_message_reaction(
            chat_id=msg.chat_id,
            message_id=msg.id,
            reaction=[ReactionTypeEmoji(emoji)],
            is_big=False
        )
    except Exception:
        pass

# ===================== CHAT MEMBER UPDATES =====================
async def my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upd: ChatMemberUpdated = update.my_chat_member
    if not upd:
        return
    chat = upd.chat
    new_status = upd.new_chat_member.status
    if new_status in ("administrator", "member"):
        await upsert_chat(chat)
    elif new_status in ("left", "kicked"):
        await mark_left(chat.id)

async def chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return

# ===================== INLINE MENU CALLBACKS =====================
async def menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    uid = q.from_user.id

    async def ensure_auth(need_owner=False, need_admin=False):
        if need_owner and not is_owner(uid):
            await q.answer("Owner only.", show_alert=True)
            return False
        if need_admin and not (is_owner(uid) or await is_admin(uid)):
            await q.answer("Admins only.", show_alert=True)
            return False
        return True

    if data.startswith("menu:list:"):
        if not await ensure_auth(need_owner=True): return
        page = int(data.split(":")[-1])
        await q.answer()
        await send_chat_page(q.message.chat_id, context, max(1, page)); return

    if data == "menu:stats":
        if not await ensure_auth(need_admin=True): return
        await q.answer()
        fake_update = Update(update.update_id, message=q.message)
        await stats(fake_update, context); return

    if data == "menu:ping":
        if not await ensure_auth(need_admin=True): return
        await q.answer()
        fake_update = Update(update.update_id, message=q.message)
        await ping(fake_update, context); return

    if data == "menu:broadcast_prompt":
        if not await ensure_auth(need_admin=True): return
        await q.edit_message_text(
            "📣 *Broadcast*\nReply to any message with `/broadcast` OR send `/broadcast Your text here`",
            parse_mode=ParseMode.MARKDOWN
        ); return

    if data == "menu:block_help":
        if not await ensure_auth(need_owner=True): return
        await q.edit_message_text("Block/Unblock:\n`/block <chat_id>`\n`/unblock <chat_id>`", parse_mode=ParseMode.MARKDOWN); return

    if data == "menu:addadmin_help":
        if not await ensure_auth(need_owner=True): return
        await q.edit_message_text("Admins:\n`/addadmin <user_id>`\n`/removeadmin <user_id>`\n`/admins`", parse_mode=ParseMode.MARKDOWN); return

    if data == "menu:leave_help":
        if not await ensure_auth(need_owner=True): return
        await q.edit_message_text("Leave chat:\n`/leave <chat_id>`", parse_mode=ParseMode.MARKDOWN); return

    if data == "menu:setreaction_help":
        if not await ensure_auth(need_owner=True): return
        current = await get_reaction_emoji()
        await q.edit_message_text(f"Set reaction:\n`/setreaction <emoji>`\nCurrent: {current}", parse_mode=ParseMode.MARKDOWN); return

    await q.answer()
  
            try:
                await context.bot.restrict_chat_member(
                    chat_id=chat.id, user_id=uid,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=until_dt
                )
            except Forbidden:
                pass
        txt += f"\n🔇 Auto-muted for 30 minutes (limit {limit})."
    await update.effective_message.reply_text(txt, parse_mode=ParseMode.MARKDOWN)

async def clearwarn_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not (is_owner(update.effective_user.id) or await is_admin(update.effective_user.id)):
        return await not_authorized(update)
    if not context.args:
        return await update.effective_message.reply_text("Usage: /clearwarn <user_id>")
    uid = int(context.args[0])
    await clear_warns(uid)
    await update.effective_message.reply_text(f"✅ Cleared warnings for `{uid}`.", parse_mode=ParseMode.MARKDOWN)
    
# ===================== GATEKEEPER (ban/mute) =====================
async def gatekeeper(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Runs for all messages before handlers
    user = update.effective_user
    if not user:
        return
    # Owner/admin bypass
    if is_owner(user.id) or await is_admin(user.id):
        return
    udoc = await get_user(user.id)
    # Banned: block interactions
    if udoc.get("banned", False):
        # Reply only in private; in groups remain silent
        if update.effective_chat and update.effective_chat.type == ChatType.PRIVATE:
            try:
                await update.effective_message.reply_text("🚫 You are banned from using this bot.")
            except:
                pass
        raise asyncio.CancelledError  # stop processing further handlers
    # Muted until
    mu = udoc.get("muted_until")
    if mu:
        try:
            until_dt = datetime.fromisoformat(mu)
            if until_dt > datetime.now(timezone.utc):
                # In private chats, ignore; in groups we already restrict via API
                raise asyncio.CancelledError
        except:
            pass

# ===================== MISC =====================
async def save_on_new_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat:
        await upsert_chat(chat)

# ===================== MAIN =====================
def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        raise RuntimeError("Please set BOT_TOKEN (env or constant).")

    app = Application.builder()\
        .token(BOT_TOKEN)\
        .rate_limiter(AIORateLimiter())\
        .build()

    # --- Gatekeeper first (high priority) ---
    app.add_handler(MessageHandler(~filters.StatusUpdate.ALL & filters.ALL, gatekeeper), group=-1)

    # User commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("about", about))
    app.add_handler(CommandHandler("reaction", reaction))
    app.add_handler(CommandHandler("mysettings", mysettings))

    # Owner/Admin shared
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("setreaction", setreaction_cmd))
    app.add_handler(CommandHandler("delreaction", delreaction_cmd))

    # Owner only
    app.add_handler(CommandHandler("addadmin", add_admin))
    app.add_handler(CommandHandler(["deladmin","removeadmin"], del_admin))
    app.add_handler(CommandHandler("admins", lambda u,c: c.bot.send_message(u.effective_chat.id, "Use /addadmin, /removeadmin to manage admins.")))

    app.add_handler(CommandHandler("reload", reload_cmd))
    app.add_handler(CommandHandler("backup", backup_cmd))
    app.add_handler(CommandHandler("update", update_cmd))

    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(CommandHandler("unban", unban_cmd))
    app.add_handler(CommandHandler("setpromotion", setpromotion_cmd))

    app.add_handler(CommandHandler("mute", mute_cmd))
    app.add_handler(CommandHandler("unmute", unmute_cmd))
    app.add_handler(CommandHandler("warn", warn_cmd))
    app.add_handler(CommandHandler("clearwarn", clearwarn_cmd))

    # Chat mgmt
    app.add_handler(CommandHandler("list", list_chats))
    app.add_handler(CommandHandler("block", block_cmd))
    app.add_handler(CommandHandler("unblock", unblock_cmd))
    app.add_handler(CommandHandler("leave", leave_cmd))

    # Broadcast
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))

    # Auto-reaction handlers
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, auto_react_for_group_mentions))
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, auto_react_for_channel_posts))

    # Track add/remove
    app.add_handler(ChatMemberHandler(my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(ChatMemberHandler(chat_member, ChatMemberHandler.CHAT_MEMBER))

    # Menu callbacks
    app.add_handler(CallbackQueryHandler(menu_cb, pattern="^menu:"))
    app.add_handler(CallbackQueryHandler(lambda u,c: u.callback_query.answer(), pattern="^noop$"))

    # Track chats on any message bot can see
    app.add_handler(MessageHandler(filters.ALL, save_on_new_message))

    # Polling
    app.run_polling(
        close_loop=False,
        allowed_updates=[
            "message", "channel_post", "my_chat_member", "chat_member",
            "message_reaction", "message_reaction_count", "callback_query"
        ]
    )

if __name__ == "__main__":
    main()
