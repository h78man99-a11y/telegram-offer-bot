import os
import json
import time
import asyncio
import urllib.parse
import requests
import logging
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)
from telegram.constants import ParseMode

load_dotenv()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("RewardBro")

BOT_TOKEN      = os.getenv("BOT_TOKEN")
BASE_URL       = os.getenv("BASE_URL", "https://app.rewardbro.in")
API_KEY        = os.getenv("API_KEY")
APP_NAME       = os.getenv("APP_NAME", "rewardbro")
COUNTRY        = os.getenv("COUNTRY", "IN")
ADMIN_TG_ID    = int(os.getenv("ADMIN_TG_ID", "0"))
WEBHOOK_URL    = os.getenv("WEBHOOK_URL", "")
PORT           = int(os.getenv("PORT", "8080"))
DATA_FILE      = os.getenv("DATA_FILE", "data.json")

RAW_WHITELIST  = os.getenv("ALLOWED_TELEGRAM_IDS", "")
ALLOWED_IDS    = set(
    int(x.strip()) for x in RAW_WHITELIST.split(",") if x.strip().isdigit()
)

PRE_CHECK_WAIT = int(os.getenv("PRE_CHECK_WAIT", "3"))
PLAY_PAUSE     = int(os.getenv("PLAY_PAUSE", "1"))
EVENT_PAUSE    = int(os.getenv("EVENT_PAUSE", "1"))
OFFER_PAUSE    = int(os.getenv("OFFER_PAUSE", "2"))

LOOPS = {
    "read":  "📰 Read-Earn",
    "watch": "📺 Watch-Earn",
    "games": "🎮 Games",
    "daily": "📋 Daily Task",
}

# ══════════════════════════════════════════════════════════════
#  DATA STORE (data.json — local file, lives only for as long as
#  the container runs. Fine if it's OK for data to reset whenever
#  the bot restarts/redeploys. Uses a relative path by default so
#  it needs no Railway volume — just the container's own disk.)
# ══════════════════════════════════════════════════════════════

def load_data() -> dict:
    try:
        if Path(DATA_FILE).exists():
            with open(DATA_FILE, "r") as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"load_data failed: {e}")
    return {"accounts": {}}


def save_data(data: dict) -> bool:
    try:
        parent = Path(DATA_FILE).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        with open(DATA_FILE, "w") as f:
            json.dump(data, f, indent=2)
        return True
    except Exception as e:
        logger.error(f"save_data failed: {e}")
        return False


def get_user_accounts(tg_id: int) -> list:
    data = load_data()
    return data["accounts"].get(str(tg_id), [])


def get_all_accounts() -> dict:
    """Returns {tg_id_str: [ {userId, email}, ... ]} for all users."""
    return load_data()["accounts"]


def set_user_accounts(tg_id: int, accounts: list) -> bool:
    data = load_data()
    data["accounts"][str(tg_id)] = accounts
    return save_data(data)


def add_account(tg_id: int, user_id_rb: str, email: str) -> bool:
    accs = get_user_accounts(tg_id)
    for a in accs:
        if a["userId"] == user_id_rb or a["email"] == email:
            return False
    accs.append({"userId": user_id_rb, "email": email})
    if not set_user_accounts(tg_id, accs):
        raise RuntimeError("Failed to save account data.")
    return True


def delete_account(tg_id: int, idx: int) -> bool:
    accs = get_user_accounts(tg_id)
    if 0 <= idx < len(accs):
        accs.pop(idx)
        if not set_user_accounts(tg_id, accs):
            raise RuntimeError("Failed to save account data.")
        return True
    return False


# ══════════════════════════════════════════════════════════════
#  USER STATE (in-memory, per session)
# ══════════════════════════════════════════════════════════════

user_state: dict[int, dict] = {}

AWAITING_NOTHING       = 0
AWAITING_ADD_USERID    = 1
AWAITING_ADD_EMAIL     = 2
AWAITING_DELETE_PICK   = 3


def get_state(user_id: int) -> dict:
    if user_id not in user_state:
        user_state[user_id] = {
            "selected_account_idx": None,
            "selected_loops": set(),
            "running": False,
            "log": [],
            "last_result": None,
            "input_mode": AWAITING_NOTHING,
            "pending_userid": None,
        }
    return user_state[user_id]


def add_log(user_id: int, msg: str):
    st = get_state(user_id)
    st["log"].append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
    if len(st["log"]) > 50:
        st["log"] = st["log"][-50:]


def ts():
    return datetime.now().strftime("%H:%M:%S")


def encode_id(s):
    return urllib.parse.quote(str(s).replace(" ", "+"), safe="")


def is_allowed(user_id: int) -> bool:
    return user_id in ALLOWED_IDS or user_id == ADMIN_TG_ID


# ══════════════════════════════════════════════════════════════
#  KEYBOARDS
# ══════════════════════════════════════════════════════════════

def main_menu_keyboard(user_id: int):
    st = get_state(user_id)
    accs = get_user_accounts(user_id)
    kb = [
        [InlineKeyboardButton(
            f"👤 My Accounts ({len(accs)})",
            callback_data="menu_accounts"
        )],
        [InlineKeyboardButton(
            f"🔁 Select Loops ({len(st['selected_loops'])} selected)",
            callback_data="menu_loops"
        )],
        [
            InlineKeyboardButton(
                "▶️ Run" if not st["running"] else "⏳ Running...",
                callback_data="action_run"
            ),
            InlineKeyboardButton("📊 Status", callback_data="action_status"),
        ],
        [
            InlineKeyboardButton("📋 Last Result", callback_data="action_result"),
            InlineKeyboardButton("🗑 Clear Logs", callback_data="action_clearlogs"),
        ],
    ]
    if user_id == ADMIN_TG_ID:
        kb.append([InlineKeyboardButton("👑 Admin Panel", callback_data="admin_panel")])
    return InlineKeyboardMarkup(kb)


def accounts_manage_keyboard(user_id: int):
    accs = get_user_accounts(user_id)
    st   = get_state(user_id)
    sel  = st["selected_account_idx"]
    kb   = []
    for i, acc in enumerate(accs):
        icon = "✅" if sel == i else "⬜"
        kb.append([
            InlineKeyboardButton(f"{icon} {acc['email']}", callback_data=f"pick_acc_{i}"),
            InlineKeyboardButton("🗑", callback_data=f"del_acc_{i}"),
        ])
    kb.append([
        InlineKeyboardButton("➕ Add Account", callback_data="add_account"),
        InlineKeyboardButton("✅ All", callback_data="pick_acc_all"),
    ])
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="menu_main")])
    return InlineKeyboardMarkup(kb)


def loops_keyboard(user_id: int):
    st  = get_state(user_id)
    sel = st["selected_loops"]
    kb  = []
    for key, label in LOOPS.items():
        icon = "✅" if key in sel else "⬜"
        kb.append([InlineKeyboardButton(f"{icon} {label}", callback_data=f"toggle_loop_{key}")])
    kb.append([
        InlineKeyboardButton("✅ All", callback_data="loops_all"),
        InlineKeyboardButton("❌ Clear", callback_data="loops_clear"),
        InlineKeyboardButton("🔙 Back", callback_data="menu_main"),
    ])
    return InlineKeyboardMarkup(kb)


def admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📣 Broadcast", callback_data="admin_broadcast")],
        [InlineKeyboardButton("👥 List Allowed IDs", callback_data="admin_listids")],
        [InlineKeyboardButton("📂 All Users' Accounts", callback_data="admin_allaccs")],
        [InlineKeyboardButton("🔙 Back", callback_data="menu_main")],
    ])


def cancel_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_input")]
    ])


# ══════════════════════════════════════════════════════════════
#  COMMAND HANDLERS
# ══════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_allowed(user.id):
        await update.message.reply_text(
            "⛔ *Access Denied*\n\nYou are not authorized.\nContact the admin.",
            parse_mode=ParseMode.MARKDOWN
        )
        logger.warning(f"Unauthorized: {user.id} @{user.username}")
        return
    accs = get_user_accounts(user.id)
    await update.message.reply_text(
        f"🎭 *RewardBro Bot*\n\n"
        f"Welcome, {user.first_name}!\n"
        f"You have *{len(accs)} account(s)* saved.\n\n"
        f"Add your RewardBro accounts from the menu below.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_keyboard(user.id)
    )


async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_allowed(user.id):
        return
    await update.message.reply_text(
        "🎛 *Main Menu*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_keyboard(user.id)
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_allowed(user.id):
        return
    st   = get_state(user.id)
    logs = "\n".join(st["log"][-10:]) if st["log"] else "No logs yet."
    await update.message.reply_text(
        f"📊 *Status*\n\n"
        f"Running: {'✅ Yes' if st['running'] else '❌ No'}\n"
        f"Loops: {', '.join(LOOPS.get(l, l) for l in st['selected_loops']) or 'None'}\n\n"
        f"*Recent Logs:*\n```\n{logs}\n```",
        parse_mode=ParseMode.MARKDOWN
    )


async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TG_ID:
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    msg  = " ".join(ctx.args)
    sent = failed = 0
    for tid in ALLOWED_IDS:
        try:
            await ctx.bot.send_message(
                chat_id=tid,
                text=f"📣 *Broadcast:*\n{msg}",
                parse_mode=ParseMode.MARKDOWN
            )
            sent += 1
        except Exception:
            failed += 1
    await update.message.reply_text(f"📣 Done. Sent: {sent} | Failed: {failed}")


# ══════════════════════════════════════════════════════════════
#  TEXT INPUT HANDLER  (for add-account flow)
# ══════════════════════════════════════════════════════════════

async def text_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_allowed(user.id):
        return

    st   = get_state(user.id)
    text = update.message.text.strip()

    if st["input_mode"] == AWAITING_ADD_USERID:
        if not text:
            await update.message.reply_text("❌ Empty input. Try again or cancel.", reply_markup=cancel_keyboard())
            return
        st["pending_userid"] = text
        st["input_mode"]     = AWAITING_ADD_EMAIL
        await update.message.reply_text(
            f"✅ User ID saved: `{text}`\n\n"
            f"Now send your *RewardBro Email*:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=cancel_keyboard()
        )

    elif st["input_mode"] == AWAITING_ADD_EMAIL:
        if not text or "@" not in text:
            await update.message.reply_text("❌ Invalid email. Try again or cancel.", reply_markup=cancel_keyboard())
            return
        uid   = st["pending_userid"]
        try:
            ok = add_account(user.id, uid, text)
        except RuntimeError:
            st["input_mode"]   = AWAITING_NOTHING
            st["pending_userid"] = None
            await update.message.reply_text(
                "❌ *Couldn't save the account.*\n"
                "There's a storage problem on the server right now. "
                "Please contact the admin — just try again after it's fixed.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=main_menu_keyboard(user.id)
            )
            return
        st["input_mode"]   = AWAITING_NOTHING
        st["pending_userid"] = None
        if ok:
            accs = get_user_accounts(user.id)
            await update.message.reply_text(
                f"✅ *Account Added!*\n\n"
                f"📧 Email: `{text}`\n"
                f"🆔 User ID: `{uid}`\n\n"
                f"You now have *{len(accs)} account(s)*.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=main_menu_keyboard(user.id)
            )
        else:
            await update.message.reply_text(
                "⚠️ *Account already exists!*\nEmail or User ID is duplicate.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=main_menu_keyboard(user.id)
            )
    else:
        pass


# ══════════════════════════════════════════════════════════════
#  BUTTON HANDLER
# ══════════════════════════════════════════════════════════════

async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user

    if not is_allowed(user.id):
        await query.edit_message_text("⛔ Access Denied.")
        return

    data = query.data
    st   = get_state(user.id)

    # ── MAIN MENU ─────────────────────────────────────────────
    if data == "menu_main":
        st["input_mode"] = AWAITING_NOTHING
        await query.edit_message_text(
            "🎛 *Main Menu*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_menu_keyboard(user.id)
        )

    elif data == "cancel_input":
        st["input_mode"]    = AWAITING_NOTHING
        st["pending_userid"] = None
        await query.edit_message_text(
            "❌ *Cancelled.*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_menu_keyboard(user.id)
        )

    # ── ACCOUNTS MENU ─────────────────────────────────────────
    elif data == "menu_accounts":
        accs = get_user_accounts(user.id)
        await query.edit_message_text(
            f"👤 *My Accounts* ({len(accs)} total)\n\n"
            f"Tap ✅ to select for run | 🗑 to delete\n"
            f"Tap *➕ Add Account* to add a new one.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=accounts_manage_keyboard(user.id)
        )

    elif data == "add_account":
        st["input_mode"]    = AWAITING_ADD_USERID
        st["pending_userid"] = None
        await query.edit_message_text(
            "➕ *Add New Account*\n\n"
            "Step 1️⃣ — Send your *RewardBro User ID*\n\n"
            "_(Find it in the RewardBro app profile section)_",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=cancel_keyboard()
        )

    elif data.startswith("pick_acc_"):
        suffix = data.replace("pick_acc_", "")
        accs   = get_user_accounts(user.id)
        if suffix == "all":
            st["selected_account_idx"] = "all"
            await query.edit_message_text(
                "✅ *All accounts selected for run.*",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Back", callback_data="menu_accounts")
                ]])
            )
        else:
            idx = int(suffix)
            if 0 <= idx < len(accs):
                st["selected_account_idx"] = idx
                acc = accs[idx]
                await query.edit_message_text(
                    f"✅ *Selected:* `{acc['email']}`",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔙 Back", callback_data="menu_accounts")
                    ]])
                )

    elif data.startswith("del_acc_"):
        idx = int(data.replace("del_acc_", ""))
        accs = get_user_accounts(user.id)
        if 0 <= idx < len(accs):
            removed = accs[idx]["email"]
            delete_account(user.id, idx)
            if st["selected_account_idx"] == idx:
                st["selected_account_idx"] = None
            await query.edit_message_text(
                f"🗑 *Deleted:* `{removed}`\n\n"
                f"You now have *{len(get_user_accounts(user.id))} account(s)*.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=accounts_manage_keyboard(user.id)
            )

    # ── LOOPS ─────────────────────────────────────────────────
    elif data == "menu_loops":
        await query.edit_message_text(
            "🔁 *Select Loops*\n\nTap to toggle:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=loops_keyboard(user.id)
        )

    elif data.startswith("toggle_loop_"):
        key = data.replace("toggle_loop_", "")
        st["selected_loops"].discard(key) if key in st["selected_loops"] else st["selected_loops"].add(key)
        await query.edit_message_text(
            "🔁 *Select Loops*\n\nTap to toggle:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=loops_keyboard(user.id)
        )

    elif data == "loops_all":
        st["selected_loops"] = set(LOOPS.keys())
        await query.edit_message_text(
            "✅ *All loops selected.*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=loops_keyboard(user.id)
        )

    elif data == "loops_clear":
        st["selected_loops"].clear()
        await query.edit_message_text(
            "❌ *All loops cleared.*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=loops_keyboard(user.id)
        )

    # ── RUN ───────────────────────────────────────────────────
    elif data == "action_run":
        if st["running"]:
            await query.edit_message_text(
                "⏳ *Already running!* Wait for it to finish.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📊 Status", callback_data="action_status")
                ]])
            )
            return

        accs = get_user_accounts(user.id)
        if not accs:
            await query.edit_message_text(
                "⚠️ *No accounts added yet.*\nGo to 👤 My Accounts → ➕ Add Account first.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("👤 My Accounts", callback_data="menu_accounts")
                ]])
            )
            return

        if st["selected_account_idx"] is None:
            await query.edit_message_text(
                "⚠️ *No account selected.*\nGo to 👤 My Accounts and tap one.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("👤 My Accounts", callback_data="menu_accounts")
                ]])
            )
            return

        if not st["selected_loops"]:
            await query.edit_message_text(
                "⚠️ *No loops selected.*\nGo to 🔁 Select Loops first.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔁 Loops", callback_data="menu_loops")
                ]])
            )
            return

        await query.edit_message_text(
            "🚀 *Starting run...*\nYou'll get a full summary when done!",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📊 Live Status", callback_data="action_status")
            ]])
        )
        asyncio.create_task(run_task(user.id, ctx))

    # ── STATUS ────────────────────────────────────────────────
    elif data == "action_status":
        logs = "\n".join(st["log"][-12:]) if st["log"] else "No logs yet."
        await query.edit_message_text(
            f"📊 *Live Status*\n\n"
            f"Running: {'✅ Yes' if st['running'] else '❌ No'}\n"
            f"Loops: {', '.join(LOOPS.get(l, l) for l in st['selected_loops']) or 'None'}\n\n"
            f"*Recent Logs:*\n```\n{logs}\n```",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Refresh", callback_data="action_status"),
                InlineKeyboardButton("🔙 Menu", callback_data="menu_main"),
            ]])
        )

    elif data == "action_result":
        res = st.get("last_result")
        if not res:
            text = "📋 No results yet. Run a task first."
        else:
            lines = ["📋 *Last Run Result*\n"]
            for entry in res:
                if "error" in entry:
                    lines.append(f"❌ `{entry['email']}` — {entry['error']}")
                else:
                    detail = "  |  ".join(
                        f"{LOOPS.get(l, l)}: *{entry['coins'].get(l, 0)}c*"
                        for l in entry.get("loops", [])
                    )
                    lines.append(f"✅ `{entry['email']}`\n{detail}\nTotal: *{entry['total']}c*")
            text = "\n\n".join(lines)
        await query.edit_message_text(
            text, parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Menu", callback_data="menu_main")
            ]])
        )

    elif data == "action_clearlogs":
        st["log"].clear()
        await query.edit_message_text(
            "🗑 *Logs cleared.*", parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back", callback_data="menu_main")
            ]])
        )

    # ── ADMIN ─────────────────────────────────────────────────
    elif data == "admin_panel":
        if user.id != ADMIN_TG_ID:
            return
        await query.edit_message_text(
            "👑 *Admin Panel*", parse_mode=ParseMode.MARKDOWN,
            reply_markup=admin_keyboard()
        )

    elif data == "admin_listids":
        if user.id != ADMIN_TG_ID:
            return
        ids = "\n".join(str(x) for x in ALLOWED_IDS) or "None configured."
        await query.edit_message_text(
            f"👥 *Allowed Telegram IDs:*\n```\n{ids}\n```",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back", callback_data="admin_panel")
            ]])
        )

    elif data == "admin_allaccs":
        if user.id != ADMIN_TG_ID:
            return
        all_accounts = get_all_accounts()
        lines      = ["📂 *All Users' Accounts:*\n"]
        for tg_id, accs in all_accounts.items():
            lines.append(f"*TG {tg_id}* ({len(accs)} accounts):")
            for a in accs:
                lines.append(f"  • `{a['email']}`")
        text = "\n".join(lines) if len(lines) > 1 else "No accounts saved yet."
        await query.edit_message_text(
            text, parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back", callback_data="admin_panel")
            ]])
        )

    elif data == "admin_broadcast":
        if user.id != ADMIN_TG_ID:
            return
        await query.edit_message_text(
            "📣 *Broadcast*\n\nUse command:\n`/broadcast Your message`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back", callback_data="admin_panel")
            ]])
        )


# ══════════════════════════════════════════════════════════════
#  RUN TASK (async background)
# ══════════════════════════════════════════════════════════════

async def run_task(user_id: int, ctx: ContextTypes.DEFAULT_TYPE):
    st       = get_state(user_id)
    st["running"]     = True
    st["last_result"] = []
    accs     = get_user_accounts(user_id)
    loop_ids = list(st["selected_loops"])
    acc_sel  = st["selected_account_idx"]

    if acc_sel == "all":
        targets = accs
    elif isinstance(acc_sel, int) and 0 <= acc_sel < len(accs):
        targets = [accs[acc_sel]]
    else:
        targets = []

    try:
        await ctx.bot.send_message(
            chat_id=user_id,
            text=f"🚀 *Run started!*\n"
                 f"Accounts: *{len(targets)}*\n"
                 f"Loops: {', '.join(LOOPS.get(l, l) for l in loop_ids)}",
            parse_mode=ParseMode.MARKDOWN
        )

        for acc in targets:
            runner = AccountRunner(acc, user_id)
            result = await asyncio.get_event_loop().run_in_executor(
                None, runner.run_sync, loop_ids
            )
            st["last_result"].append(result)

            if "error" in result:
                summary = f"❌ `{acc['email']}` FAILED: {result['error']}"
            else:
                parts   = [f"  {LOOPS.get(l, l)}: *{result['coins'].get(l, 0)}c*" for l in loop_ids]
                summary = f"✅ `{acc['email']}`\n" + "\n".join(parts) + f"\n  Total: *{result['total']}c*"

            await ctx.bot.send_message(
                chat_id=user_id, text=summary,
                parse_mode=ParseMode.MARKDOWN
            )

        grand = sum(r.get("total", 0) for r in st["last_result"])
        await ctx.bot.send_message(
            chat_id=user_id,
            text=f"🏁 *All Done!*\n💰 Grand Total: *{grand}* coins",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_menu_keyboard(user_id)
        )

    except Exception as e:
        logger.exception(f"run_task error for {user_id}")
        await ctx.bot.send_message(
            chat_id=user_id,
            text=f"❌ *Run failed:* {e}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_menu_keyboard(user_id)
        )
    finally:
        st["running"] = False


# ══════════════════════════════════════════════════════════════
#  ACCOUNT RUNNER
# ══════════════════════════════════════════════════════════════

class AccountRunner:
    def __init__(self, account: dict, tg_id: int):
        self.user_id_rb = account["userId"]
        self.email      = account["email"]
        self.tg_id      = tg_id
        self.session    = requests.Session()
        self.session.headers.update({
            "user-agent":      "Dart/3.11 (dart:io)",
            "content-type":    "application/json",
            "x-api-key":       API_KEY,
            "accept-encoding": "gzip",
            "host":            "app.rewardbro.in",
        })
        self.coins: dict[str, int] = {k: 0 for k in LOOPS}

    def log(self, tag, msg):
        entry = f"[{tag}] {msg}"
        add_log(self.tg_id, entry)
        logger.info(f"{self.email} {entry}")

    def games_headers(self):
        h = dict(self.session.headers)
        h["x-user-id"] = self.user_id_rb
        return h

    def run_sync(self, loop_ids: list) -> dict:
        dispatch = {
            "read":  self._read_earn,
            "watch": self._watch_earn,
            "games": self._games,
            "daily": self._daily_task,
        }
        try:
            for l in loop_ids:
                if l in dispatch:
                    dispatch[l]()
            total = sum(self.coins.get(l, 0) for l in loop_ids)
            return {"email": self.email, "coins": dict(self.coins), "total": total, "loops": loop_ids}
        except Exception as e:
            return {"email": self.email, "error": str(e), "total": 0}

    def _read_earn(self):
        self.log("READ", "Start")
        earned = reads = 0
        while True:
            try:
                r = self.session.get(
                    f"{BASE_URL}/get-read-earn-url",
                    json={"appName": APP_NAME, "userId": self.user_id_rb},
                    timeout=15
                )
                r.raise_for_status()
                d = r.json()
                if not d.get("success"):
                    break
                oid   = d["offerId"]
                coins = d["coins"]
                tt    = d["trackingTime"]
                done  = d["completedCount"]
                lim   = d["limits"]
                self.log("READ", f"offerId={oid} coins={coins} {done}/{lim}")
                if done >= lim:
                    break
                time.sleep(tt)
                pb = self.session.get(
                    f"{BASE_URL}/read-earn-postback",
                    params={"appName": APP_NAME, "userId": self.user_id_rb, "offerId": oid},
                    timeout=15
                )
                pb.raise_for_status()
                res = pb.json()
                if res.get("success"):
                    earned += coins
                    reads  += 1
                    self.log("READ", f"+{coins} total={earned}")
                else:
                    break
                time.sleep(2)
            except Exception as e:
                self.log("READ", f"Error: {e}")
                break
        self.coins["read"] = earned
        self.log("READ", f"Done reads={reads} coins={earned}")

    def _watch_earn(self):
        self.log("WATCH", "Start")
        try:
            r = self.session.post(
                f"{BASE_URL}/get-daily-task",
                json={"appName": APP_NAME, "userId": self.user_id_rb,
                      "email": self.email, "countryCode": COUNTRY, "offerType": "WatchEarn"},
                timeout=15
            )
            r.raise_for_status()
            offers = r.json().get("offers", [])
        except Exception as e:
            self.log("WATCH", f"Fetch failed: {e}")
            return
        earned = won = skipped = 0
        for idx, offer in enumerate(offers, 1):
            oid    = offer.get("offerId", "")
            coins  = offer.get("coins", 0)
            events = offer.get("events", [])
            if events and all(e.get("completed") for e in events):
                skipped += 1
                continue
            self.log("WATCH", f"[{idx}/{len(offers)}] {str(oid)[:28]} coins={coins}")
            time.sleep(PRE_CHECK_WAIT)
            try:
                url = (f"{BASE_URL}/daily-task-postback"
                       f"?appName={APP_NAME}&userId={self.user_id_rb}"
                       f"&email={urllib.parse.quote(self.email, safe='')}"
                       f"&offerId={encode_id(oid)}")
                pb = self.session.get(url, timeout=15)
                if pb.status_code == 200:
                    c = pb.json().get("coins", coins)
                    earned += c
                    won    += 1
                    self.log("WATCH", f"+{c}")
                else:
                    skipped += 1
            except Exception as e:
                self.log("WATCH", f"Error: {e}")
                skipped += 1
            time.sleep(OFFER_PAUSE)
        self.coins["watch"] = earned
        self.log("WATCH", f"Done won={won} skip={skipped} coins={earned}")

    def _games(self):
        self.log("GAMES", "Start")
        try:
            r = self.session.get(f"{BASE_URL}/get-games", headers=self.games_headers(), timeout=15)
            r.raise_for_status()
            games = r.json().get("games", [])
        except Exception as e:
            self.log("GAMES", f"Fetch failed: {e}")
            return
        earned = plays = 0
        for g_idx, game in enumerate(games, 1):
            oid     = game.get("offerId", "")
            name    = game.get("offerName", "?")
            coins   = game.get("coins", 5)
            tt      = game.get("trackingTime", 30)
            max_day = game.get("maxPlaysPerDay", 5)
            self.log("GAMES", f"[{g_idx}/{len(games)}] {name} {max_day}x{coins}c")
            for pnum in range(1, max_day + 1):
                time.sleep(tt)
                try:
                    pb = self.session.post(
                        f"{BASE_URL}/record-game-play",
                        json={"offerId": oid},
                        headers=self.games_headers(),
                        timeout=15
                    )
                    if pb.status_code == 200:
                        res = pb.json()
                        rem = res.get("remainingToday", -1)
                        earned += coins
                        plays  += 1
                        self.log("GAMES", f"+{coins} rem={rem}")
                        if rem == 0 or res.get("remainingLifetime", -1) == 0:
                            break
                    elif pb.status_code == 400:
                        break
                except Exception as e:
                    self.log("GAMES", f"Error: {e}")
                time.sleep(PLAY_PAUSE)
            time.sleep(OFFER_PAUSE)
        self.coins["games"] = earned
        self.log("GAMES", f"Done plays={plays} coins={earned}")

    def _claim_event(self, oid, event):
        coins    = event.get("coins", 0)
        event_id = event.get("eventId", "")
        enc_e    = urllib.parse.quote(event_id, safe="")
        enc_o    = encode_id(oid)
        enc_em   = urllib.parse.quote(self.email, safe="")
        try:
            url = (f"{BASE_URL}/daily-task-postback"
                   f"?appName={APP_NAME}&userId={self.user_id_rb}"
                   f"&email={enc_em}&offerId={enc_o}&eventId={enc_e}")
            r = self.session.get(url, timeout=15)
            if r.status_code == 200 and r.json().get("success"):
                return r.json().get("coins", coins)
        except Exception:
            pass
        try:
            url = (f"{BASE_URL}/redirect"
                   f"?offerId={enc_o}&appName={APP_NAME}"
                   f"&userId={self.user_id_rb}&eventId={enc_e}")
            r = self.session.get(url, timeout=15, allow_redirects=True)
            if r.status_code in (200, 201):
                try:
                    res = r.json()
                    if res.get("success"):
                        return res.get("coins", coins)
                except Exception:
                    return coins
        except Exception:
            pass
        return 0

    def _daily_task(self):
        self.log("DAILY", "Start")
        try:
            r = self.session.post(
                f"{BASE_URL}/get-daily-task",
                json={"appName": APP_NAME, "userId": self.user_id_rb,
                      "email": self.email, "countryCode": COUNTRY, "offerType": "DailyTask"},
                timeout=15
            )
            r.raise_for_status()
            offers = r.json().get("offers", [])
        except Exception as e:
            self.log("DAILY", f"Fetch failed: {e}")
            return
        earned = claimed = failed = 0
        for o_idx, offer in enumerate(offers, 1):
            oid    = offer.get("offerId", "")
            oname  = offer.get("offerName", "?")
            events = offer.get("events", [])
            self.log("DAILY", f"[{o_idx}/{len(offers)}] {oname} {len(events)} events")
            for ev in events:
                if ev.get("completed") or ev.get("status") != "active":
                    continue
                c = self._claim_event(oid, ev)
                if c > 0:
                    earned  += c
                    claimed += 1
                    self.log("DAILY", f"+{c} {ev.get('name','?')}")
                else:
                    failed += 1
                    self.log("DAILY", f"FAIL {ev.get('name','?')}")
                time.sleep(EVENT_PAUSE)
            time.sleep(OFFER_PAUSE)
        self.coins["daily"] = earned
        self.log("DAILY", f"Done claimed={claimed} failed={failed} coins={earned}")


# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════

async def global_error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Unhandled exception: {ctx.error}", exc_info=ctx.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Something went wrong on the server. "
                "Please try again in a moment."
            )
        except Exception:
            pass


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("menu",      cmd_menu))
    app.add_handler(CommandHandler("status",    cmd_status))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_error_handler(global_error_handler)

    if WEBHOOK_URL:
        logger.info(f"Webhook mode on port {PORT}")
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            webhook_url=f"{WEBHOOK_URL}/webhook",
            url_path="webhook",
        )
    else:
        logger.info("Polling mode")
        app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
