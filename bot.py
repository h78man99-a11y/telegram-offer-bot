import os
import asyncio
import sqlite3
import threading
import time
import urllib.parse
from typing import Dict, Set, List, Callable
import requests

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# Load .env file (only for local development)
load_dotenv()

# ══════════════════════════════════════════════════════════════
#  CONFIGURATION (from environment)
# ══════════════════════════════════════════════════════════════
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_KEY = os.getenv("API_KEY")
ALLOWED_USER_IDS = [int(uid.strip()) for uid in os.getenv("ALLOWED_USER_IDS", "").split(",") if uid.strip()]

BASE_URL = "https://app.rewardbro.in"
APP_NAME = "rewardbro"
COUNTRY = "IN"

PRE_CHECK_WAIT = 3
PLAY_PAUSE = 1
EVENT_PAUSE = 1
OFFER_PAUSE = 2

LOOPS = {1: "📖 Read-Earn", 2: "📺 Watch-Earn", 3: "🎮 Games", 4: "📅 Daily Task"}

# ══════════════════════════════════════════════════════════════
#  DATABASE (stored locally on Railway – will persist as long as container runs)
# ══════════════════════════════════════════════════════════════
DB_NAME = "rewardbro_users.db"

def init_db():
    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    conn.execute('''CREATE TABLE IF NOT EXISTS accounts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        chat_id INTEGER,
                        user_id TEXT NOT NULL,
                        email TEXT NOT NULL
                    )''')
    conn.commit()
    conn.close()

def get_db():
    return sqlite3.connect(DB_NAME, check_same_thread=False)

def add_account(chat_id: int, user_id: str, email: str):
    conn = get_db()
    conn.execute("INSERT INTO accounts (chat_id, user_id, email) VALUES (?,?,?)",
                 (chat_id, user_id, email))
    conn.commit()
    conn.close()

def get_accounts(chat_id: int) -> List[Dict]:
    conn = get_db()
    rows = conn.execute("SELECT id, user_id, email FROM accounts WHERE chat_id=?",
                        (chat_id,)).fetchall()
    conn.close()
    return [{"db_id": r[0], "userId": r[1], "email": r[2]} for r in rows]

def delete_account(chat_id: int, db_id: int):
    conn = get_db()
    conn.execute("DELETE FROM accounts WHERE chat_id=? AND id=?", (chat_id, db_id))
    conn.commit()
    conn.close()

# ══════════════════════════════════════════════════════════════
#  AUTHORIZATION DECORATOR / MIDDLEWARE
# ══════════════════════════════════════════════════════════════
def restricted(func):
    """Decorator to restrict access to allowed users only."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if user_id not in ALLOWED_USER_IDS:
            if update.message:
                await update.message.reply_text("⛔ You are not authorised to use this bot.")
            elif update.callback_query:
                await update.callback_query.answer("Unauthorized.", show_alert=True)
            return
        return await func(update, context, *args, **kwargs)
    return wrapper

# ══════════════════════════════════════════════════════════════
#  ACCOUNT RUNNER (unchanged logic)
# ══════════════════════════════════════════════════════════════
def encode_id(s):
    return urllib.parse.quote(str(s).replace(" ", "+"), safe="")

class AccountRunner:
    def __init__(self, account, log_cb: Callable, stop_event: threading.Event):
        self.user_id = account["userId"]
        self.email = account["email"]
        self.log_cb = log_cb
        self.stop_event = stop_event
        self.label = f"[{self.email}]"
        self.session = requests.Session()
        self.session.headers.update({
            "user-agent": "Dart/3.11 (dart:io)",
            "content-type": "application/json",
            "x-api-key": API_KEY,
            "accept-encoding": "gzip",
            "host": "app.rewardbro.in",
        })
        self.coins = {1:0,2:0,3:0,4:0}

    def _log(self, tag, msg):
        self.log_cb(f"{self.label} [{tag}] {msg}")

    def _games_headers(self):
        h = dict(self.session.headers)
        h["x-user-id"] = self.user_id
        return h

    def _sleep(self, secs):
        for _ in range(secs):
            if self.stop_event.is_set():
                raise InterruptedError("stopped")
            time.sleep(1)

    def run_read_earn(self):
        self._log("READ", "Started")
        earned = 0
        while not self.stop_event.is_set():
            try:
                r = self.session.get(f"{BASE_URL}/get-read-earn-url",
                    json={"appName":APP_NAME,"userId":self.user_id}, timeout=15)
                d = r.json()
                if not d["success"]: break
                oid, coins, tt, done, lim = d["offerId"], d["coins"], d["trackingTime"], d["completedCount"], d["limits"]
                if done >= lim: break
                self._log("READ", f"offerId={oid} coins={coins} ({done}/{lim})")
                self._sleep(tt)
                pb = self.session.get(f"{BASE_URL}/read-earn-postback",
                    params={"appName":APP_NAME,"userId":self.user_id,"offerId":oid}, timeout=15)
                if pb.json().get("success"):
                    earned += coins
                    self._log("READ", f"+{coins} total={earned}")
                else:
                    self._log("READ", "Postback rejected"); break
                time.sleep(2)
            except InterruptedError: raise
            except Exception as e: self._log("READ", str(e)); break
        self.coins[1] = earned

    def run_watch_earn(self):
        self._log("WATCH", "Started")
        try:
            r = self.session.post(f"{BASE_URL}/get-daily-task",
                json={"appName":APP_NAME,"userId":self.user_id,"email":self.email,
                      "countryCode":COUNTRY,"offerType":"WatchEarn"}, timeout=15)
            offers = r.json()["offers"]
        except Exception as e: self._log("WATCH", f"fetch err: {e}"); return
        earned = 0
        for idx,off in enumerate(offers):
            if self.stop_event.is_set(): raise InterruptedError()
            oid, coins = off["offerId"], off["coins"]
            if all(e.get("completed") for e in off.get("events",[])): continue
            self._log("WATCH", f"[{idx+1}/{len(offers)}] {str(oid)[:30]}...")
            self._sleep(PRE_CHECK_WAIT)
            try:
                url = f"{BASE_URL}/daily-task-postback?appName={APP_NAME}&userId={self.user_id}&email={urllib.parse.quote(self.email)}&offerId={encode_id(oid)}"
                pb = self.session.get(url, timeout=15)
                if pb.status_code==200:
                    c = pb.json().get("coins",coins); earned+=c; self._log("WATCH", f"+{c}")
                else: self._log("WATCH", f"HTTP {pb.status_code}")
            except Exception as e: self._log("WATCH", str(e))
            self._sleep(OFFER_PAUSE)
        self.coins[2] = earned

    def run_games(self):
        self._log("GAMES", "Started")
        try:
            r = self.session.get(f"{BASE_URL}/get-games", headers=self._games_headers(), timeout=15)
            games = r.json()["games"]
        except Exception as e: self._log("GAMES", str(e)); return
        earned = 0
        for g_idx,game in enumerate(games):
            if self.stop_event.is_set(): raise InterruptedError()
            oid, name, coins, tt, max_day = game["offerId"], game["offerName"], game["coins"], game["trackingTime"], game["maxPlaysPerDay"]
            self._log("GAMES", f"[{g_idx+1}] {name} x{max_day}")
            for p in range(max_day):
                if self.stop_event.is_set(): raise InterruptedError()
                self._sleep(tt)
                try:
                    pb = self.session.post(f"{BASE_URL}/record-game-play",
                        json={"offerId":oid}, headers=self._games_headers(), timeout=15)
                    if pb.status_code==200:
                        earned+=coins; self._log("GAMES", f"+{coins}")
                        if pb.json().get("remainingToday",1)==0: break
                    elif pb.status_code==400: break
                    else: self._log("GAMES", f"HTTP {pb.status_code}")
                except Exception as e: self._log("GAMES", str(e))
                self._sleep(PLAY_PAUSE)
            self._sleep(OFFER_PAUSE)
        self.coins[3] = earned

    def _claim_event(self, oid, event):
        coins = event["coins"]
        eid = event["eventId"]
        enc_o, enc_e = encode_id(oid), urllib.parse.quote(eid)
        enc_em = urllib.parse.quote(self.email)
        for url in (
            f"{BASE_URL}/daily-task-postback?appName={APP_NAME}&userId={self.user_id}&email={enc_em}&offerId={enc_o}&eventId={enc_e}",
            f"{BASE_URL}/redirect?offerId={enc_o}&appName={APP_NAME}&userId={self.user_id}&eventId={enc_e}"
        ):
            try:
                r = self.session.get(url, timeout=15)
                if r.status_code==200:
                    try: res = r.json()
                    except: return coins
                    if res.get("success"): return res.get("coins",coins)
            except: continue
        return 0

    def run_daily_task(self):
        self._log("DAILY", "Started")
        try:
            r = self.session.post(f"{BASE_URL}/get-daily-task",
                json={"appName":APP_NAME,"userId":self.user_id,"email":self.email,
                      "countryCode":COUNTRY,"offerType":"DailyTask"}, timeout=15)
            offers = r.json()["offers"]
        except Exception as e: self._log("DAILY", str(e)); return
        earned = 0
        for off in offers:
            if self.stop_event.is_set(): raise InterruptedError()
            oid, events = off["offerId"], off["events"]
            self._log("DAILY", f"{off['offerName']} ({len(events)} events)")
            for ev in events:
                if ev.get("completed") or ev.get("status")!="active": continue
                c = self._claim_event(oid, ev)
                if c: earned+=c; self._log("DAILY", f"+{c}")
                self._sleep(EVENT_PAUSE)
            self._sleep(OFFER_PAUSE)
        self.coins[4] = earned

    def run(self, loop_ids):
        self._log("SYSTEM", f"Loops: {[LOOPS[l] for l in loop_ids]}")
        for lid in loop_ids:
            if self.stop_event.is_set(): break
            try:
                {1:self.run_read_earn,2:self.run_watch_earn,
                 3:self.run_games,4:self.run_daily_task}[lid]()
            except InterruptedError: self._log("SYSTEM", "Stopped"); break
            except Exception as e: self._log("ERR", str(e))
        total = sum(self.coins[l] for l in loop_ids)
        self._log("SYSTEM", f"🏁 Done. Total={total}")
        return total

# ══════════════════════════════════════════════════════════════
#  BOT HANDLERS (all decorated with @restricted)
# ══════════════════════════════════════════════════════════════
pending_actions: Dict[int, str] = {}
run_wizard: Dict[int, dict] = {}
active_runs: Dict[int, tuple] = {}

async def send_msg(update: Update, text, reply_markup=None):
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")

@restricted
async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📋 Manage Accounts", callback_data="menu_accounts")],
        [InlineKeyboardButton("🚀 Start Run", callback_data="menu_startrun")],
        [InlineKeyboardButton("🛑 Stop Run", callback_data="menu_stoprun")],
    ]
    await send_msg(update, "**🤖 RewardBro Bot**\nSelect an option:", InlineKeyboardMarkup(keyboard))

@restricted
async def accounts_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("➕ Add Account", callback_data="acc_add")],
        [InlineKeyboardButton("🗑 Remove Account", callback_data="acc_remove")],
        [InlineKeyboardButton("🔙 Back", callback_data="menu_back")],
    ]
    await send_msg(update, "**📋 Manage Accounts**", InlineKeyboardMarkup(keyboard))

@restricted
async def acc_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat_id
    pending_actions[chat_id] = "add_account"
    await query.edit_message_text("✏️ Send the account details in one line:\n`userId email`", parse_mode="Markdown")

@restricted
async def handle_add_account_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    if pending_actions.get(chat_id) != "add_account":
        return
    text = update.message.text.strip()
    parts = text.split()
    if len(parts) < 2:
        await update.message.reply_text("❌ Please send both `userId` and `email` separated by a space.")
        return
    user_id, email = parts[0], parts[1]
    add_account(chat_id, user_id, email)
    del pending_actions[chat_id]
    await update.message.reply_text(f"✅ Account **{email}** added.")
    await show_main_menu(update, context)

@restricted
async def acc_remove_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat_id
    accounts = get_accounts(chat_id)
    if not accounts:
        await query.edit_message_text("No accounts to remove.", reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 Back", callback_data="menu_accounts")
        ]]))
        return
    keyboard = []
    for acc in accounts:
        keyboard.append([InlineKeyboardButton(f"❌ {acc['email']}", callback_data=f"del_{acc['db_id']}")])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="menu_accounts")])
    await query.edit_message_text("**🗑 Select account to remove:**", reply_markup=InlineKeyboardMarkup(keyboard))

@restricted
async def acc_remove_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    db_id = int(query.data.split("_")[1])
    delete_account(query.message.chat_id, db_id)
    await query.answer("Deleted ✅")
    await acc_remove_list(update, context)

@restricted
async def start_run_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat_id
    accounts = get_accounts(chat_id)
    if not accounts:
        await query.edit_message_text("❌ No accounts found. Add some first.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu_back")]]))
        return
    run_wizard[chat_id] = {
        "accounts": accounts,
        "selected": set(),
        "step": "accounts"
    }
    await show_account_selection(chat_id, query)

async def show_account_selection(chat_id, query):
    state = run_wizard[chat_id]
    keyboard = []
    for i, acc in enumerate(state["accounts"]):
        check = "✅" if i in state["selected"] else "⬜"
        keyboard.append([InlineKeyboardButton(f"{check} {acc['email']}", callback_data=f"run_toggle_acc_{i}")])
    keyboard.append([InlineKeyboardButton("➡ Next (Loops)", callback_data="run_loops")])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="menu_back")])
    text = "**📌 Select accounts to run:**\n(tap to toggle)"
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

@restricted
async def toggle_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat_id
    idx = int(query.data.split("_")[-1])
    state = run_wizard.get(chat_id)
    if not state: return
    if idx in state["selected"]:
        state["selected"].remove(idx)
    else:
        state["selected"].add(idx)
    await query.answer()
    await show_account_selection(chat_id, query)

@restricted
async def show_loop_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat_id
    state = run_wizard.get(chat_id)
    if not state or not state["selected"]:
        await query.answer("Select at least one account!", show_alert=True)
        return
    state["step"] = "loops"
    state["selected_loops"] = set()
    await query.edit_message_text("**🔄 Select loops:**", reply_markup=build_loop_keyboard(state))

def build_loop_keyboard(state):
    sel = state.get("selected_loops", set())
    keyboard = []
    for lid, name in LOOPS.items():
        check = "✅" if lid in sel else "⬜"
        keyboard.append([InlineKeyboardButton(f"{check} {name}", callback_data=f"run_toggle_loop_{lid}")])
    keyboard.append([InlineKeyboardButton("▶ Start Run", callback_data="run_confirm")])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="run_back_accounts")])
    return InlineKeyboardMarkup(keyboard)

@restricted
async def toggle_loop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat_id
    lid = int(query.data.split("_")[-1])
    state = run_wizard.get(chat_id)
    if not state: return
    sel = state["selected_loops"]
    if lid in sel: sel.remove(lid)
    else: sel.add(lid)
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=build_loop_keyboard(state))

@restricted
async def run_back_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    state = run_wizard.get(query.message.chat_id)
    if state:
        state["step"] = "accounts"
    await show_account_selection(query.message.chat_id, query)

@restricted
async def run_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat_id
    state = run_wizard.pop(chat_id, None)
    if not state or not state.get("selected_loops"):
        await query.answer("Select at least one loop!", show_alert=True)
        return
    if chat_id in active_runs and active_runs[chat_id][0].is_alive():
        await query.answer("Already running! Stop first.", show_alert=True)
        return

    selected_accs = [state["accounts"][i] for i in state["selected"]]
    loops = sorted(state["selected_loops"])

    stop_event = threading.Event()
    def log_cb(msg):
        asyncio.run_coroutine_threadsafe(
            context.bot.send_message(chat_id=chat_id, text=msg),
            context.application.loop
        )
    def job():
        grand = 0
        for acc in selected_accs:
            if stop_event.is_set(): break
            log_cb(f"▶ Account: {acc['email']}")
            runner = AccountRunner(acc, log_cb, stop_event)
            try:
                grand += runner.run(loops)
            except Exception as e:
                log_cb(f"❌ {acc['email']}: {e}")
        log_cb(f"🏁 **All done. Grand total = {grand} coins**")
        active_runs.pop(chat_id, None)

    thread = threading.Thread(target=job)
    active_runs[chat_id] = (thread, stop_event)
    thread.start()

    await query.edit_message_text(f"🚀 **Run started** for {len(selected_accs)} account(s).\nLoops: {', '.join(LOOPS[l] for l in loops)}")

@restricted
async def stop_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in active_runs:
        _, stop_event = active_runs[chat_id]
        stop_event.set()
        await update.message.reply_text("🛑 Stop signal sent. Finishing current step...")
    else:
        await update.message.reply_text("No active run.")

# ══════════════════════════════════════════════════════════════
#  MAIN APPLICATION
# ══════════════════════════════════════════════════════════════
def main():
    init_db()
    if not BOT_TOKEN or not API_KEY:
        raise ValueError("BOT_TOKEN and API_KEY must be set in environment variables.")

    app = Application.builder().token(BOT_TOKEN).build()

    # Command handlers – no need to decorate because the handler functions are already decorated
    app.add_handler(CommandHandler("start", show_main_menu))

    # Callback handlers
    app.add_handler(CallbackQueryHandler(accounts_menu, pattern="^menu_accounts$"))
    app.add_handler(CallbackQueryHandler(start_run_menu, pattern="^menu_startrun$"))
    app.add_handler(CallbackQueryHandler(stop_run, pattern="^menu_stoprun$"))
    app.add_handler(CallbackQueryHandler(show_main_menu, pattern="^menu_back$"))

    app.add_handler(CallbackQueryHandler(acc_add_start, pattern="^acc_add$"))
    app.add_handler(CallbackQueryHandler(acc_remove_list, pattern="^acc_remove$"))
    app.add_handler(CallbackQueryHandler(acc_remove_confirm, pattern="^del_"))

    app.add_handler(CallbackQueryHandler(toggle_account, pattern="^run_toggle_acc_"))
    app.add_handler(CallbackQueryHandler(show_loop_selection, pattern="^run_loops$"))
    app.add_handler(CallbackQueryHandler(toggle_loop, pattern="^run_toggle_loop_"))
    app.add_handler(CallbackQueryHandler(run_back_accounts, pattern="^run_back_accounts$"))
    app.add_handler(CallbackQueryHandler(run_confirm, pattern="^run_confirm$"))

    # Message handler for adding account text
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_add_account_text))

    print("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()