import os
import json
import requests
import time
from flask import Flask, request
from pymongo import MongoClient
from bson.objectid import ObjectId
from bson.errors import InvalidId
from datetime import datetime
from urllib.parse import urlparse, parse_qs
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
MONGODB_URI = os.getenv("MONGODB_URI")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

CHANNEL_1_NAME = os.getenv("CHANNEL_1_NAME")
CHANNEL_2_NAME = os.getenv("CHANNEL_2_NAME")
REQUIRED_CHANNELS = [CHANNEL_1_NAME, CHANNEL_2_NAME]

client = MongoClient(MONGODB_URI)
db = client["telegram_bot"]

users = db.users
offers = db.offers
submissions = db.submissions
banned = db.banned_users
help_requests = db.help_requests

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# ================= UTIL =================

def send_message(chat_id, text, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    requests.post(f"{TELEGRAM_API}/sendMessage", json=payload)

def safe_object_id(value):
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        return None

# ================= OFFERS =================

def create_offer(name, start, postbacks, delays, admin):
    offers.insert_one({
        "name": name,
        "starting_link": start,
        "postbacks": postbacks,
        "delays": delays,
        "enabled": True,
        "created_by": admin,
        "created_at": datetime.utcnow()
    })
    return "✅ Offer created successfully"

def get_offer(offer_id):
    oid = safe_object_id(offer_id)
    if not oid:
        return None
    return offers.find_one({"_id": oid})

def delete_offer(offer_id):
    oid = safe_object_id(offer_id)
    if not oid:
        return "❌ Invalid Offer ID"
    offers.delete_one({"_id": oid})
    return "✅ Offer deleted"

def edit_offer(offer_id, data):
    oid = safe_object_id(offer_id)
    if not oid:
        return "❌ Invalid Offer ID"
    offers.update_one({"_id": oid}, {"$set": data})
    return "✅ Offer updated"

# ================= KEYBOARDS =================

def manage_offers_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "➕ Create Offer", "callback_data": "offer_create"}],
            [{"text": "📋 View Offers", "callback_data": "offer_list"}],
            [{"text": "✏️ Edit Offer", "callback_data": "offer_edit"}],
            [{"text": "🗑️ Delete Offer", "callback_data": "offer_delete"}],
            [{"text": "⬅️ Back", "callback_data": "admin_panel"}],
        ]
    }

def offer_list_keyboard():
    kb = {"inline_keyboard": []}
    for o in offers.find():
        kb["inline_keyboard"].append([
            {"text": o["name"], "callback_data": f"offer_select_{o['_id']}"}
        ])
    kb["inline_keyboard"].append([{"text": "⬅️ Back", "callback_data": "admin_manage_offers"}])
    return kb

# ================= WEBHOOK =================

@app.route(f"/webhook/{TELEGRAM_TOKEN}", methods=["POST"])
def webhook():
    update = request.json

    # ================= CALLBACK =================
    if "callback_query" in update:
        cb = update["callback_query"]
        data = cb["data"]
        chat_id = cb["message"]["chat"]["id"]
        user_id = cb["from"]["id"]

        # ----- ADMIN ONLY -----
        if user_id != ADMIN_ID and data.startswith("offer"):
            send_message(chat_id, "❌ Admin only")
            return "ok"

        # ===== MENU ACTIONS FIRST =====
        if data == "offer_create":
            send_message(
                chat_id,
                "➕ <b>Create Offer</b>\n\n"
                "Format:\n"
                "<code>Name|StartLink|PB1|PB2|...|D1|D2</code>"
            )
            users.update_one({"_id": user_id}, {"$set": {"mode": "offer_create"}})

        elif data == "offer_edit":
            send_message(
                chat_id,
                "✏️ <b>Edit Offer</b>\n\n"
                "<code>OfferID|Name|StartLink|PB1|PB2|...|D1|D2</code>"
            )
            users.update_one({"_id": user_id}, {"$set": {"mode": "offer_edit"}})

        elif data == "offer_delete":
            send_message(chat_id, "🗑️ Send Offer ID to delete")
            users.update_one({"_id": user_id}, {"$set": {"mode": "offer_delete"}})

        elif data == "offer_list":
            send_message(chat_id, "📋 <b>All Offers</b>", reply_markup=offer_list_keyboard())

        # ===== REAL OFFER SELECT =====
        elif data.startswith("offer_select_"):
            offer_id = data.replace("offer_select_", "")
            offer = get_offer(offer_id)
            if not offer:
                send_message(chat_id, "❌ Offer not found")
            else:
                send_message(
                    chat_id,
                    f"🎁 <b>{offer['name']}</b>\n\n"
                    f"Postbacks: {len(offer['postbacks'])}\n"
                    f"Delays: {offer['delays']}\n"
                    f"ID: <code>{offer['_id']}</code>"
                )

        elif data == "admin_manage_offers":
            send_message(chat_id, "🎁 <b>Manage Offers</b>", reply_markup=manage_offers_keyboard())

        return "ok"

    # ================= MESSAGE =================
    if "message" in update:
        msg = update["message"]
        text = msg.get("text", "")
        chat_id = msg["chat"]["id"]
        user_id = msg["from"]["id"]

        user = users.find_one({"_id": user_id}) or {"mode": None}

        # ----- CREATE -----
        if user.get("mode") == "offer_create":
            parts = text.split("|")
            name = parts[0]
            start = parts[1]
            half = (len(parts) - 2) // 2
            pbs = parts[2:2+half]
            ds = list(map(int, parts[2+half:]))

            send_message(chat_id, create_offer(name, start, pbs, ds, user_id))
            users.update_one({"_id": user_id}, {"$set": {"mode": None}})

        # ----- EDIT -----
        elif user.get("mode") == "offer_edit":
            parts = text.split("|")
            oid = parts[0]
            name = parts[1]
            start = parts[2]
            half = (len(parts) - 3) // 2
            pbs = parts[3:3+half]
            ds = list(map(int, parts[3+half:]))

            send_message(chat_id, edit_offer(oid, {
                "name": name,
                "starting_link": start,
                "postbacks": pbs,
                "delays": ds
            }))
            users.update_one({"_id": user_id}, {"$set": {"mode": None}})

        # ----- DELETE -----
        elif user.get("mode") == "offer_delete":
            send_message(chat_id, delete_offer(text))
            users.update_one({"_id": user_id}, {"$set": {"mode": None}})

    return "ok"

@app.route("/health")
def health():
    return {"status": "ok"}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
            
