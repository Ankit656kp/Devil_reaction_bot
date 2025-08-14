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
# - Broadcast, stats, admin mgmt, blocklist, leave, etc. included.
# - /addreaction /delreaction multiple emojis manage karne ke liye (owner only).

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
SUPPORT_URL = os.getenv("SUPPORT_URL", "https://t.me/YourSupportHandle")
PROMO_URL   = os.getenv("PROMO_URL",   "https://t.me/YourPromoHandle")

# Permanent Owner ID (fixed)
OWNER_ID    = 6135117014

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

# ===================== UI MARKUPS =====================

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
            InlineKeyboardButton("😊 Set Reaction", callback_data="menu:setreaction_help"),
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

# ===================== COMMANDS: START & HELP =====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    user_id = user.id

    if is_owner(user_id):
        emojis = await get_reaction_emojis()
        await update.effective_message.reply_text(
            f"👑 𝗢𝘄𝗻𝗲𝗿 𝗣𝗮𝗻𝗲𝗹 👑\n"
            f"✨ Auto-Reaction Emojis: {' '.join(emojis)}\n"
            f"⚙ Controls below:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=owner_menu_kb()
        )
    elif await is_admin(user_id):
        await update.effective_message.reply_text(
            "🛡️ 𝗔𝗱𝗺𝗶𝗻 𝗣𝗮𝗻𝗲𝗹 🛡️\n"
            "📢 You can broadcast & view stats.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=admin_menu_kb()
        )
    else:
        await update.effective_message.reply_text(
            "🤖 𝗪𝗲𝗹𝗰𝗼𝗺𝗲 𝘁𝗼 𝗕𝗿𝗼𝗮𝗱𝗰𝗮𝘀𝘁 𝗕𝗼𝘁 🤖\n"
            "ℹ For info or promotions, use the buttons below.",
            reply_markup=user_menu_kb()
        )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE, role: str = None):
    """Show role-specific commands."""
    user = update.effective_user
    if not user:
        return

    user_id = user.id

    if role is None:
        if is_owner(user_id):
            role = "owner"
        elif await is_admin(user_id):
            role = "admin"
        else:
            role = "user"

    if role == "owner":
        text = (
            "👑 *Owner Commands*\n"
            "📣 `/broadcast` - Send a broadcast\n"
            "📊 `/stats` - View bot stats\n"
            "📜 `/list` - List all chats\n"
            "🚫 `/block <chat_id>` - Block a chat\n"
            "✅ `/unblock <chat_id>` - Unblock a chat\n"
            "➕ `/addadmin <id>` - Add admin\n"
            "➖ `/deladmin <id>` - Remove admin\n"
            "👥 `/admins` - List admins\n"
            "🏳 `/leave <chat_id>` - Leave a chat\n"
            "😊 `/addreaction <emoji>` - Add emoji to reaction list\n"
            "🗑 `/delreaction <emoji>` - Remove emoji from list\n"
            "🎯 `/reactions` - View current emoji list\n"
            "🏓 `/ping` - Test bot speed"
        )
    elif role == "admin":
        text = (
            "🛡️ *Admin Commands*\n"
            "📣 `/broadcast` - Send a broadcast\n"
            "📊 `/stats` - View bot stats\n"
            "🏓 `/ping` - Test bot speed"
        )
    else:
        text = (
            "🤖 *User Commands*\n"
            "ℹ No special commands — use menu buttons for contact & info."
        )

    await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

# ===================== HELP MENU CALLBACK =====================
async def help_menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    if data == "menu:help_owner":
        await q.answer()
        fake_update = Update(update.update_id, message=q.message)
        await help_command(fake_update, context, role="owner")
    elif data == "menu:help_admin":
        await q.answer()
        fake_update = Update(update.update_id, message=q.message)
        await help_command(fake_update, context, role="admin")
    elif data == "menu:help_user":
        await q.answer()
        fake_update = Update(update.update_id, message=q.message)
        await help_command(fake_update, context, role="user")

  # ===================== ADMIN/OWNER COMMANDS =====================

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

  # ===================== EMOJI DB HELPERS =====================
async def get_reaction_emojis() -> list:
    """Return list of default emojis from DB."""
    s = await settings_col.find_one({"_id": "reaction_list"})
    if s and "emojis" in s and isinstance(s["emojis"], list):
        return s["emojis"]
    return [DEFAULT_REACTION_EMOJI]

async def add_reaction_emoji(emoji: str):
    """Add emoji to default list."""
    emojis = await get_reaction_emojis()
    if emoji not in emojis:
        emojis.append(emoji)
    await settings_col.update_one(
        {"_id": "reaction_list"},
        {"$set": {"emojis": emojis, "updated_at": now_iso()}},
        upsert=True
    )

async def remove_reaction_emoji(emoji: str):
    """Remove emoji from default list."""
    emojis = await get_reaction_emojis()
    if emoji in emojis:
        emojis.remove(emoji)
    await settings_col.update_one(
        {"_id": "reaction_list"},
        {"$set": {"emojis": emojis, "updated_at": now_iso()}},
        upsert=True
    )

# ===================== EMOJI COMMANDS =====================
async def list_reactions_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    emojis = await get_reaction_emojis()
    await update.effective_message.reply_text(
        f"🎯 Current Reaction Emojis:\n{' '.join(emojis)}",
        parse_mode=ParseMode.MARKDOWN
    )

async def addreaction_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /addreaction <emoji>")
        return
    emoji = context.args[0]
    await add_reaction_emoji(emoji)
    await update.effective_message.reply_text(f"✅ Added reaction emoji: {emoji}")

async def delreaction_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /delreaction <emoji>")
        return
    emoji = context.args[0]
    await remove_reaction_emoji(emoji)
    await update.effective_message.reply_text(f"🗑 Removed reaction emoji: {emoji}")

# ===================== AUTO-REACTIONS =====================
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
        emojis = await get_reaction_emojis()
        chosen = random.choice(emojis)
        await context.bot.set_message_reaction(
            chat_id=msg.chat_id,
            message_id=msg.id,
            reaction=[ReactionTypeEmoji(chosen)],
            is_big=False
        )
    except Exception:
        pass

async def auto_react_for_channel_posts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or msg.chat.type != ChatType.CHANNEL:
        return
    try:
        emojis = await get_reaction_emojis()
        chosen = random.choice(emojis)
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

# =============== INLINE MENU CALLBACKS ===============
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
        await q.edit_message_text("Admins:\n`/addadmin <user_id>`\n`/deladmin <user_id>`\n`/admins`", parse_mode=ParseMode.MARKDOWN); return

    if data == "menu:leave_help":
        if not await ensure_auth(need_owner=True): return
        await q.edit_message_text("Leave chat:\n`/leave <chat_id>`", parse_mode=ParseMode.MARKDOWN); return

    if data == "menu:setreaction_help":
        if not await ensure_auth(need_owner=True): return
        emojis = await get_reaction_emojis()
        await q.edit_message_text(f"Add/Delete reactions with:\n`/addreaction <emoji>`\n`/delreaction <emoji>`\nCurrent: {' '.join(emojis)}", parse_mode=ParseMode.MARKDOWN); return

    await q.answer()

# =============== MISC ===============
async def save_on_new_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat:
        await upsert_chat(chat)

# =============== MAIN ===============
def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        raise RuntimeError("Please set BOT_TOKEN (env or constant).")

    app = Application.builder()\
        .token(BOT_TOKEN)\
        .rate_limiter(AIORateLimiter())\
        .build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("addadmin", add_admin))
    app.add_handler(CommandHandler("deladmin", del_admin))
    app.add_handler(CommandHandler("admins", list_admins))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("list", list_chats))
    app.add_handler(CommandHandler("block", block_cmd))
    app.add_handler(CommandHandler("unblock", unblock_cmd))
    app.add_handler(CommandHandler("leave", leave_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(CommandHandler("reactions", list_reactions_cmd))
    app.add_handler(CommandHandler("addreaction", addreaction_cmd))
    app.add_handler(CommandHandler("delreaction", delreaction_cmd))

    # Auto-reaction handlers
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, auto_react_for_group_mentions))
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, auto_react_for_channel_posts))

    # Track add/remove
    app.add_handler(ChatMemberHandler(my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(ChatMemberHandler(chat_member, ChatMemberHandler.CHAT_MEMBER))

    # Menu callbacks
    app.add_handler(CallbackQueryHandler(menu_cb, pattern="^menu:"))
    app.add_handler(CallbackQueryHandler(help_menu_cb, pattern="^menu:help_"))
    app.add_handler(CallbackQueryHandler(lambda u,c: u.callback_query.answer(), pattern="^noop$"))

    # Track chats on any message bot can see
    app.add_handler(MessageHandler(filters.ALL, save_on_new_message))

    # Polling
    app.run_polling(
        close_loop=False,
        allowed_updates=[
            "message", "channel_post", "my_chat_member", "chat_member",
            "message_reaction", "message_reaction_count"
        ]
    )

if __name__ == "__main__":
    main()
