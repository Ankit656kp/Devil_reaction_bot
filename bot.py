# tg_broadcast_bot.py
# Requirements:
#   pip install python-telegram-bot==21.10 motor==3.6 pymongo[srv]
#
# ENV VARS (recommended) or fill constants below:
#   BOT_TOKEN, MONGO_URI, OWNER_ID, SUPPORT_URL, PROMO_URL
#
# Notes:
# - Bot cannot auto-join groups/channels from invite links. You (or an admin) must add it.
# - Channels: channel posts (channel_post updates) tabhi milte hain jab bot ko channel me add kiya jata hai
#   (aksar admin ke roop me). Wahan har post par auto-reaction lagega.
# - Groups: jab koi @BotUsername se bot ko mention/tag kare, tab us message par auto-reaction lagega.
# - Broadcast, stats, admin mgmt, blocklist, leave, etc. included (from earlier build).
# - /setreaction <emoji> se reaction emoji change hoga (default: 👍).

import os
import asyncio
import math
from datetime import datetime, timezone

from motor.motor_asyncio import AsyncIOMotorClient
from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, Chat, ChatMemberUpdated,
    ReactionTypeEmoji
)
from telegram.constants import ParseMode, ChatType
from telegram.ext import (
    Application, AIORateLimiter, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes, ChatMemberHandler
)

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

# Broadcast tuning
CONCURRENCY = 15
SLEEP_EVERY = 25
SLEEP_TIME  = 1.0

# Defaults
DEFAULT_REACTION_EMOJI = "👍"

# ===================== DB SETUP =====================
mongo = AsyncIOMotorClient(MONGO_URI)
db = mongo[DB_NAME]
chats_col = db[COL_CHATS]
admins_col = db[COL_ADMINS]
bclogs_col = db[COL_BCAST_LOGS]
settings_col = db[COL_SETTINGS]

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

def user_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📮 Contact Owner", url=SUPPORT_URL)],
        [InlineKeyboardButton("📢 Promotion & Support", url=PROMO_URL)]
    ])

# ===================== COMMANDS =====================
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
            reply_markup=user_menu_kb()
        )

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_ts = datetime.now(timezone.utc)
    msg = await update.effective_message.reply_text("Pong...")
    end_ts = datetime.now(timezone.utc)
    delta = (end_ts - start_ts).total_seconds() * 1000
    await msg.edit_text(f"🏓 Pong! `{int(delta)} ms`", parse_mode=ParseMode.MARKDOWN)

async def add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id): return
    if not context.args:
        await update.effective_message.reply_text("Usage: /addadmin <user_id>"); return
    try:
        uid = int(context.args[0])
    except:
        await update.effective_message.reply_text("Invalid user_id."); return
    await admins_col.update_one({"_id": uid}, {"$set": {"_id": uid, "added_at": now_iso()}}, upsert=True)
    await update.effective_message.reply_text(f"✅ Added admin: `{uid}`", parse_mode=ParseMode.MARKDOWN)

async def del_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id): return
    if not context.args:
        await update.effective_message.reply_text("Usage: /deladmin <user_id>"); return
    try:
        uid = int(context.args[0])
    except:
        await update.effective_message.reply_text("Invalid user_id."); return
    await admins_col.delete_one({"_id": uid})
    await update.effective_message.reply_text(f"🗑️ Removed admin: `{uid}`", parse_mode=ParseMode.MARKDOWN)

async def list_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id): return
    cursor = admins_col.find({})
    admins = [str(doc["_id"]) async for doc in cursor]
    if not admins:
        await update.effective_message.reply_text("No admins."); return
    await update.effective_message.reply_text("👥 Admins:\n" + "\n".join(f"- `{a}`" for a in admins), parse_mode=ParseMode.MARKDOWN)

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not (is_owner(uid) or await is_admin(uid)): return
    total = await chats_col.count_documents({"left_at": {"$exists": False}})
    blocked = await chats_col.count_documents({"blocked": True, "left_at": {"$exists": False}})
    groups = await chats_col.count_documents({"type": {"$in": ["group", "supergroup"]}, "left_at": {"$exists": False}})
    channels = await chats_col.count_documents({"type": "channel", "left_at": {"$exists": False}})
    last = await bclogs_col.find_one(sort=[("created_at", -1)])
    text = [
        "📊 *Stats*",
        f"Total chats: *{total}* (groups: *{groups}*, channels: *{channels}*)",
        f"Blocked: *{blocked}*",
    ]
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

PAGE_SIZE = 10

async def list_chats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id): return
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
    if not is_owner(update.effective_user.id): return
    if not context.args:
        await update.effective_message.reply_text("Usage: /block <chat_id>"); return
    cid = int(context.args[0])
    await chats_col.update_one({"_id": cid}, {"$set": {"blocked": True}})
    await update.effective_message.reply_text(f"🚫 Blocked chat `{cid}`", parse_mode=ParseMode.MARKDOWN)

async def unblock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id): return
    if not context.args:
        await update.effective_message.reply_text("Usage: /unblock <chat_id>"); return
    cid = int(context.args[0])
    await chats_col.update_one({"_id": cid}, {"$set": {"blocked": False}})
    await update.effective_message.reply_text(f"✅ Unblocked chat `{cid}`", parse_mode=ParseMode.MARKDOWN)

async def leave_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id): return
    if not context.args:
        await update.effective_message.reply_text("Usage: /leave <chat_id>"); return
    cid = int(context.args[0])
    try:
        await context.bot.leave_chat(cid)
        await mark_left(cid)
        await update.effective_message.reply_text(f"🏳️ Left chat `{cid}`", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.effective_message.reply_text(f"Error leaving chat: `{e}`", parse_mode=ParseMode.MARKDOWN)

# =============== BROADCAST ===============
async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not (is_owner(uid) or await is_admin(uid)): return

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

# =============== AUTO-REACTION ===============
async def setreaction_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id): return
    if not context.args:
        current = await get_reaction_emoji()
        await update.effective_message.reply_text(f"Usage: /setreaction <emoji>\nCurrent: {current}")
        return
    emoji = context.args[0]
    # Telegram supports many emoji; keep it simple
    if len(emoji) == 0:
        await update.effective_message.reply_text("Please pass a valid emoji."); return
    await set_reaction_emoji(emoji)
    await update.effective_message.reply_text(f"😊 Reaction emoji set to: {emoji}")

async def auto_react_for_group_mentions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """React when bot is mentioned (@username) in groups/supergroups."""
    msg = update.effective_message
    if not msg or msg.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    bot_username = (await context.bot.get_me()).username or ""
    if not bot_username:
        return

    text = (msg.text or "") + " " + (msg.caption or "")
    mentioned = False

    # Fast path: direct substring
    if f"@{bot_username}".lower() in text.lower():
        mentioned = True
    else:
        # Check entities/caption_entities
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
        # ignore failures silently (e.g., reactions disabled in chat)
        pass

async def auto_react_for_channel_posts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """React to every new channel post where the bot is present (usually as admin)."""
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

# =============== CHAT MEMBER UPDATES ===============
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
