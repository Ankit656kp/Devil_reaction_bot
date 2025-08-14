# bot.py
# Requirements:
#   pip install python-telegram-bot==21.10 motor==3.6 pymongo[srv]
#
# ENV VARS (recommended) or fill constants below:
#   BOT_TOKEN, MONGO_URI, SUPPORT_URL, PROMO_URL
#
# NOTE: Owner ID is permanently set to 6135117014 in this file as requested.

import os
import asyncio
import math
import random
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
# Permanent owner id as requested (overrides env)
OWNER_ID    = 6135117014
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

# Backwards-compatible: single global reaction (legacy)
async def get_reaction_emoji() -> str:
    s = await settings_col.find_one({"_id": "reaction"})
    return (s or {}).get("emoji", DEFAULT_REACTION_EMOJI)

async def set_reaction_emoji_single(emoji: str):
    await settings_col.update_one({"_id": "reaction"}, {"$set": {"emoji": emoji, "updated_at": now_iso()}}, upsert=True)

# ===================== EMOJI DB HELPERS (multi + per-user) =====================
async def get_reaction_emojis() -> list:
    """Return owner/global emoji list (list). If none, return default single emoji in list."""
    s = await settings_col.find_one({"_id": "reaction_list"})
    if s and "emojis" in s and isinstance(s["emojis"], list) and len(s["emojis"]) > 0:
        return s["emojis"]
    # fallback to legacy single
    legacy = await get_reaction_emoji()
    return [legacy]

async def add_reaction_emoji_owner(emoji: str):
    emojis = await get_reaction_emojis()
    if emoji not in emojis:
        emojis.append(emoji)
    await settings_col.update_one(
        {"_id": "reaction_list"},
        {"$set": {"emojis": emojis, "updated_at": now_iso()}},
        upsert=True
    )

async def remove_reaction_emoji_owner(emoji: str):
    emojis = await get_reaction_emojis()
    if emoji in emojis:
        emojis.remove(emoji)
    await settings_col.update_one(
        {"_id": "reaction_list"},
        {"$set": {"emojis": emojis, "updated_at": now_iso()}},
        upsert=True
    )

# Per-user reaction list (users can manage their own reaction list)
async def get_user_reaction_emojis(user_id: int) -> list:
    key = f"user_reaction:{user_id}"
    s = await settings_col.find_one({"_id": key})
    if s and "emojis" in s and isinstance(s["emojis"], list) and len(s["emojis"]) > 0:
        return s["emojis"]
    return []

async def add_user_reaction_emoji(user_id: int, emoji: str):
    key = f"user_reaction:{user_id}"
    s = await settings_col.find_one({"_id": key})
    emojis = (s.get("emojis") if s else []) or []
    if emoji not in emojis:
        emojis.append(emoji)
    await settings_col.update_one(
        {"_id": key},
        {"$set": {"emojis": emojis, "updated_at": now_iso()}},
        upsert=True
    )

async def remove_user_reaction_emoji(user_id: int, emoji: str):
    key = f"user_reaction:{user_id}"
    s = await settings_col.find_one({"_id": key})
    emojis = (s.get("emojis") if s else []) or []
    if emoji in emojis:
        emojis.remove(emoji)
    await settings_col.update_one(
        {"_id": key},
        {"$set": {"emojis": emojis, "updated_at": now_iso()}},
        upsert=True
    )

# ===================== UI MARKUPS (UPDATED) =====================
def owner_menu_kb():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📣 Broadcast", callback_data="menu:broadcast_prompt"),
            InlineKeyboardButton("📊 Stats", callback_data="menu:stats")
        ],
        [
            InlineKeyboardButton("📜 List Chats", callback_data="menu:list:1"),
            InlineKeyboardButton("🚫 Block/Unblock", callback_data="menu:block_help")
        ],
        [
            InlineKeyboardButton("➕ Add Admin", callback_data="menu:addadmin_help"),
            InlineKeyboardButton("👥 Admins", callback_data="menu:admins")
        ],
        [
            InlineKeyboardButton("🏳️ Leave Chat", callback_data="menu:leave_help"),
            InlineKeyboardButton("🏓 Ping", callback_data="menu:ping")
        ],
        [
            InlineKeyboardButton("😊 Set Reaction (single)", callback_data="menu:setreaction_help"),
            InlineKeyboardButton("🎯 Reactions (list)", callback_data="menu:listreactions")
        ],
        [
            InlineKeyboardButton("📖 Help & Commands", callback_data="menu:help_owner")
        ],
        [
            InlineKeyboardButton("📮 Contact Owner", url=SUPPORT_URL),
            InlineKeyboardButton("📢 Promotion & Support", url=PROMO_URL)
        ]
    ])

def admin_menu_kb():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📣 Broadcast", callback_data="menu:broadcast_prompt"),
            InlineKeyboardButton("📊 Stats", callback_data="menu:stats")
        ],
        [
            InlineKeyboardButton("🏓 Ping", callback_data="menu:ping"),
            InlineKeyboardButton("📖 Help & Commands", callback_data="menu:help_admin")
        ],
        [
            InlineKeyboardButton("📮 Contact Owner", url=SUPPORT_URL),
            InlineKeyboardButton("📢 Promotion & Support", url=PROMO_URL)
        ]
    ])

def user_menu_kb():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📖 Help & Commands", callback_data="menu:help_user")
        ],
        [
            InlineKeyboardButton("📮 Contact Owner", url=SUPPORT_URL)
        ],
        [
            InlineKeyboardButton("📢 Promotion & Support", url=PROMO_URL)
        ]
    ])

# ===================== COMMANDS =====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    fixed_owner_id = OWNER_ID
    user_id = user.id

    # Stylish headings with Unicode-ish look (works on mobile)
    if user_id == fixed_owner_id:
        emojis = await get_reaction_emojis()
        emoji_preview = " ".join(emojis[:5]) + ("…" if len(emojis) > 5 else "")
        await update.effective_message.reply_text(
            f"👑 𝗢𝘄𝗻𝗲𝗿 𝗣𝗮𝗻𝗲𝗹 👑\n\n"
            f"✨ Auto‑Reaction sample: {emoji_preview}\n"
            f"⚙ Controls below — tap a button to operate.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=owner_menu_kb()
        )
    elif await is_admin(user_id):
        await update.effective_message.reply_text(
            "🛡️ 𝗔𝗱𝗺𝗶𝗻 𝗣𝗮𝗻𝗲𝗹 🛡️\n\n"
            "📢 Use broadcast & stats buttons below.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=admin_menu_kb()
        )
    else:
        await update.effective_message.reply_text(
            "🤖 𝗪𝗲𝗹𝗰𝗼𝗺𝗲 𝘁𝗼 𝗧𝗵𝗲 𝗕𝗿𝗼𝗮𝗱𝗰𝗮𝘀𝘁 𝗕𝗼𝘁 🤖\n\n"
            "ℹ Tap buttons below for help, contact & promotions.",
            reply_markup=user_menu_kb()
        )

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_ts = datetime.now(timezone.utc)
    msg = await update.effective_message.reply_text("Pong...")
    end_ts = datetime.now(timezone.utc)
    delta = (end_ts - start_ts).total_seconds() * 1000
    await msg.edit_text(f"🏓 Pong! `{int(delta)} ms`", parse_mode=ParseMode.MARKDOWN)

# Admin management (unchanged)
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

# =============== EMOJI / REACTION COMMANDS ===============
async def list_reactions_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # show owner/global list (owner only)
    if not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("Owner only."); return
    emojis = await get_reaction_emojis()
    await update.effective_message.reply_text(
        f"🎯 Current Owner Reaction Emojis:\n{' '.join(emojis)}",
        parse_mode=ParseMode.MARKDOWN
    )

async def addreaction_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("Owner only."); return
    if not context.args:
        await update.effective_message.reply_text("Usage: /addreaction <emoji>"); return
    emoji = context.args[0]
    await add_reaction_emoji_owner(emoji)
    await update.effective_message.reply_text(f"✅ Added reaction emoji: {emoji}")

async def delreaction_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("Owner only."); return
    if not context.args:
        await update.effective_message.reply_text("Usage: /delreaction <emoji>"); return
    emoji = context.args[0]
    await remove_reaction_emoji_owner(emoji)
    await update.effective_message.reply_text(f"🗑 Removed reaction emoji: {emoji}")

# User-level reaction list commands
async def myreactions_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    emojis = await get_user_reaction_emojis(uid)
    if not emojis:
        await update.effective_message.reply_text("You have no personal reaction emojis set. Use /addmyreaction <emoji> to add.")
    else:
        await update.effective_message.reply_text(f"🎯 Your reaction emojis:\n{' '.join(emojis)}")

async def addmyreaction_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not context.args:
        await update.effective_message.reply_text("Usage: /addmyreaction <emoji>"); return
    emoji = context.args[0]
    await add_user_reaction_emoji(uid, emoji)
    await update.effective_message.reply_text(f"✅ Added to your reaction list: {emoji}")

async def delmyreaction_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not context.args:
        await update.effective_message.reply_text("Usage: /delmyreaction <emoji>"); return
    emoji = context.args[0]
    await remove_user_reaction_emoji(uid, emoji)
    await update.effective_message.reply_text(f"🗑 Removed from your reaction list: {emoji}")

# Legacy single setreaction (owner) - kept for compatibility
async def setreaction_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id): return
    if not context.args:
        current = await get_reaction_emoji()
        await update.effective_message.reply_text(f"Usage: /setreaction <emoji>\nCurrent: {current}")
        return
    emoji = context.args[0]
    await set_reaction_emoji_single(emoji)
    await update.effective_message.reply_text(f"😊 Reaction emoji (legacy single) set to: {emoji}")

# =============== AUTO-REACTION ===============
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
            try:
                if ent.type == "mention":
                    ent_text = text[ent.offset: ent.offset + ent.length]
                    if ent_text.lower() == f"@{bot_username}".lower():
                        mentioned = True
                        break
            except Exception:
                # defensive
                pass

    if not mentioned:
        return

    try:
        # Prefer per-user emojis if message author has set one
        user_list = await get_user_reaction_emojis(msg.from_user.id) if getattr(msg, "from_user", None) else []
        if user_list:
            chosen = random.choice(user_list)
        else:
            owner_list = await get_reaction_emojis()
            chosen = random.choice(owner_list)

        await context.bot.set_message_reaction(
            chat_id=msg.chat_id,
            message_id=msg.id,
            reaction=[ReactionTypeEmoji(chosen)],
            is_big=False
        )
    except Exception:
        # ignore reaction errors (permissions etc.)
        pass

async def auto_react_for_channel_posts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """React to every new channel post where the bot is present (usually as admin)."""
    msg = update.effective_message
    if not msg or msg.chat.type != ChatType.CHANNEL:
        return
    try:
        owner_list = await get_reaction_emojis()
        chosen = random.choice(owner_list)
        await context.bot.set_message_reaction(
            chat_id=msg.chat_id,
            message_id=msg.id,
            reaction=[ReactionTypeEmoji(chosen)],
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

# =============== HELP COMMANDS =====================
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE, role: str = None):
    """Show role-specific commands."""
    user = update.effective_user
    if not user:
        return

    user_id = user.id

    if role is None:
        # Detect role automatically
        if user_id == OWNER_ID:
            role = "owner"
        elif await is_admin(user_id):
            role = "admin"
        else:
            role = "user"

    # Role-based command lists
    if role == "owner":
        text = (
            "👑 *Owner Commands*\n\n"
            "📣 `/broadcast` - Send a broadcast (reply to message or text)\n"
            "📊 `/stats` - View bot stats\n"
            "📜 `/list` - List chats (paginated)\n"
            "🚫 `/block <chat_id>` - Block a chat\n"
            "✅ `/unblock <chat_id>` - Unblock a chat\n"
            "➕ `/addadmin <id>` - Add admin\n"
            "➖ `/deladmin <id>` - Remove admin\n"
            "👥 `/admins` - List admins\n"
            "🏳 `/leave <chat_id>` - Leave a chat\n"
            "😊 `/setreaction <emoji>` - (legacy) set single reaction\n"
            "🎯 `/reactions` - show owner/global emoji list\n"
            "➕ `/addreaction <emoji>` - add emoji to owner/global list\n"
            "➖ `/delreaction <emoji>` - remove emoji from owner/global list\n"
            "🏓 `/ping` - Test bot speed\n"
        )
    elif role == "admin

   
