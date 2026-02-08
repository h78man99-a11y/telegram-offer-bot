import os
import json
import requests
import time
from flask import Flask, request
from pymongo import MongoClient
from bson.objectid import ObjectId
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs
from dotenv import load_dotenv
import asyncio
from concurrent.futures import ThreadPoolExecutor

# Load environment variables
load_dotenv()

# Initialize Flask app
app = Flask(__name__)

# Configuration from Environment Variables
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
MONGODB_URI = os.getenv("MONGODB_URI", "YOUR_MONGODB_URI")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# Multiple Channels Configuration
CHANNEL_1_NAME = os.getenv("CHANNEL_1_NAME", "@YOUR_CHANNEL_1")
CHANNEL_2_NAME = os.getenv("CHANNEL_2_NAME", "@YOUR_CHANNEL_2")

# List of all channels
REQUIRED_CHANNELS = [CHANNEL_1_NAME, CHANNEL_2_NAME]
CHANNEL_NAMES = {
    CHANNEL_1_NAME: "Channel 1",
    CHANNEL_2_NAME: "Channel 2"
}

OFFER18_URL = os.getenv("OFFER18_URL", "https://offer18.com")

# Validate configuration
if TELEGRAM_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
    raise ValueError("⚠️ TELEGRAM_TOKEN not configured. Check your .env file")
if MONGODB_URI == "YOUR_MONGODB_URI":
    raise ValueError("⚠️ MONGODB_URI not configured. Check your .env file")
if ADMIN_ID == 0:
    raise ValueError("⚠️ ADMIN_ID not configured. Check your .env file")

# MongoDB Setup
try:
    client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
    client.server_info()  # Test connection
    db = client['telegram_bot']
    users_collection = db['users']
    help_requests_collection = db['help_requests']
    banned_users_collection = db['banned_users']
    offers_collection = db['offers']
    submissions_collection = db['submissions']
    print("✅ MongoDB connected successfully")
except Exception as e:
    print(f"❌ MongoDB Connection Error: {e}")
    raise

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

executor = ThreadPoolExecutor(max_workers=5)

# ==================== DATABASE FUNCTIONS ====================

def get_or_create_user(user_id, username, first_name):
    user = users_collection.find_one({'_id': user_id})
    is_new_user = False
    
    if not user:
        is_new_user = True
        users_collection.insert_one({
            '_id': user_id,
            'username': username or f'user_{user_id}',
            'first_name': first_name or 'User',
            'joined_channels': [],
            'created_at': datetime.utcnow(),
            'help_requests_today': 0,
            'last_help_request_date': None,
            'is_active': True,
            'current_mode': None,
            'joined_bot_at': datetime.utcnow()
        })
        notify_admin_new_user(user_id, username, first_name)
        return users_collection.find_one({'_id': user_id}), is_new_user
    else:
        if 'current_mode' not in user:
            users_collection.update_one({'_id': user_id}, {'$set': {'current_mode': None}})
            user = users_collection.find_one({'_id': user_id})
    return user, is_new_user

def is_user_banned(user_id):
    return banned_users_collection.find_one({'_id': user_id}) is not None

def ban_user(user_id):
    if not is_user_banned(user_id):
        banned_users_collection.insert_one({'_id': user_id, 'banned_at': datetime.utcnow()})
        return True
    return False

def unban_user(user_id):
    result = banned_users_collection.delete_one({'_id': user_id})
    return result.deleted_count > 0

def get_total_users():
    return users_collection.count_documents({'is_active': True})

def get_banned_users_count():
    return banned_users_collection.count_documents({})

def can_send_help_request(user_id):
    user = users_collection.find_one({'_id': user_id})
    if not user: return False, "User not found"
    today = datetime.utcnow().date()
    last_date = user.get('last_help_request_date')
    if last_date and last_date.date() == today:
        if user.get('help_requests_today', 0) >= 2:
            return False, "⏳ You can send max 2 help messages per day. Try again tomorrow!"
    return True, ""

def add_help_request(user_id, username, message):
    today = datetime.utcnow().date()
    user = users_collection.find_one({'_id': user_id})
    if user:
        last_date = user.get('last_help_request_date')
        if last_date and last_date.date() != today:
            users_collection.update_one({'_id': user_id}, {'$set': {'help_requests_today': 1, 'last_help_request_date': datetime.utcnow()}})
        else:
            users_collection.update_one({'_id': user_id}, {'$inc': {'help_requests_today': 1}, '$set': {'last_help_request_date': datetime.utcnow()}})
    return help_requests_collection.insert_one({
        'user_id': user_id, 'username': username, 'message': message, 'created_at': datetime.utcnow(), 'status': 'pending'
    }).inserted_id

def get_pending_help_requests():
    return list(help_requests_collection.find({'status': 'pending'}).sort('created_at', -1))

def reply_to_help_request(request_id, reply_text):
    try:
        help_req = help_requests_collection.find_one({'_id': request_id})
        if not help_req: return False, "Request not found"
        help_requests_collection.update_one({'_id': request_id}, {'$set': {'admin_reply': reply_text, 'admin_replied_at': datetime.utcnow(), 'status': 'resolved'}})
        send_message(help_req['user_id'], f"<b>📬 Support Reply</b>\n\n<b>Your Question:</b> {help_req['message']}\n\n<b>Admin Response:</b>\n{reply_text}")
        return True, f"Reply sent to @{help_req['username']}"
    except Exception as e: return False, str(e)

def get_recent_joined_users(limit=20):
    return list(users_collection.find({'is_active': True}).sort('created_at', -1).limit(limit))

# ==================== OFFER MANAGEMENT FUNCTIONS ====================

def create_offer(name, starting_link, postbacks, delays, admin_id):
    if len(postbacks) < 1 or len(postbacks) > 5: return False, "❌ Must have 1-5 postbacks"
    offer_id = offers_collection.insert_one({
        'name': name, 'starting_link': starting_link, 'postback_count': len(postbacks),
        'postbacks': postbacks, 'delays': delays, 'enabled': True, 'created_by': admin_id,
        'created_at': datetime.utcnow(), 'updated_at': datetime.utcnow(), 'total_submissions': 0, 'success_count': 0
    }).inserted_id
    return True, f"✅ Offer created! ID: {offer_id}"

def get_all_offers(): return list(offers_collection.find())
def get_enabled_offers(): return list(offers_collection.find({'enabled': True}))

def get_offer(offer_id):
    if not ObjectId.is_valid(offer_id): return None
    return offers_collection.find_one({'_id': ObjectId(offer_id)})

def edit_offer(offer_id, updates):
    try:
        if not ObjectId.is_valid(offer_id): return False, "Invalid ID"
        updates['updated_at'] = datetime.utcnow()
        offers_collection.update_one({'_id': ObjectId(offer_id)}, {'$set': updates})
        return True, "✅ Offer updated!"
    except Exception as e: return False, str(e)

def delete_offer(offer_id):
    try:
        if not ObjectId.is_valid(offer_id): return False, "Invalid ID"
        offers_collection.delete_one({'_id': ObjectId(offer_id)})
        return True, "✅ Offer deleted!"
    except Exception as e: return False, str(e)

def save_submission(user_id, username, offer_id, url, clickid, postback_responses, success, total_time):
    submissions_collection.insert_one({
        'user_id': user_id, 'username': username, 'offer_id': ObjectId(offer_id), 'submitted_url': url,
        'extracted_clickid': clickid, 'postback_responses': postback_responses, 'submitted_at': datetime.utcnow(),
        'total_execution_time_ms': total_time, 'success': success
    })
    offers_collection.update_one({'_id': ObjectId(offer_id)}, {'$inc': {'total_submissions': 1, 'success_count': 1 if success else 0}})

def get_offer_analytics(offer_id):
    if not ObjectId.is_valid(offer_id): return None
    subs = list(submissions_collection.find({'offer_id': ObjectId(offer_id)}).sort('submitted_at', -1))
    total = len(subs)
    success = sum(1 for s in subs if s['success'])
    return {
        'total': total, 'success': success, 'success_rate': (success / total * 100) if total > 0 else 0,
        'users': list(set([s['username'] for s in subs])), 'submissions': subs
    }

# ==================== POSTBACK FUNCTIONS ====================

def extract_clickid_from_url(url):
    try:
        params = parse_qs(urlparse(url).query)
        if 'clickid' in params: return params['clickid'][0]
        if params: return params[list(params.keys())[0]][0]
        return None
    except: return None

def validate_url_format(user_url, starting_link):
    try:
        res = urlparse(user_url)
        return bool(res.scheme and res.netloc)
    except: return False

def send_postback(postback_url):
    try:
        start = time.time()
        resp = requests.get(postback_url, timeout=15)
        elapsed = int((time.time() - start) * 1000)
        return True, resp.text[:500], resp.status_code, elapsed
    except Exception as e: return False, str(e)[:100], 0, 0

def run_postbacks_sequence(clickid, postbacks, delays, user_id):
    responses, all_success, total_time = [], True, 0
    for i, (url, delay) in enumerate(zip(postbacks, delays)):
        final_url = url.replace('$clickid', clickid)
        success, text, status, elapsed = send_postback(final_url)
        total_time += elapsed + (delay * 1000)
        responses.append({'num': i+1, 'success': success, 'status': status})
        send_message(user_id, f"<b>{'✅' if success else '⚠️'} Postback {i+1}/{len(postbacks)}</b>\n<b>Status:</b> {status}\n<b>Time:</b> {elapsed}ms")
        if not success: all_success = False
        if i < len(postbacks) - 1:
            time.sleep(delay)
    return responses, all_success, total_time

# ==================== MESSAGE & KEYBOARDS ====================

def send_message(chat_id, text, reply_markup=None, parse_mode="HTML"):
    url = f"{TELEGRAM_API}/sendMessage"
    data = {'chat_id': chat_id, 'text': text, 'parse_mode': parse_mode}
    if reply_markup: data['reply_markup'] = json.dumps(reply_markup)
    try: return requests.post(url, json=data, timeout=10).json()
    except: return None

def notify_admin_new_user(user_id, username, first_name):
    send_message(ADMIN_ID, f"🆕 <b>NEW USER!</b>\nName: {first_name}\nUser ID: <code>{user_id}</code>")

def answer_callback_query(callback_query_id, text, show_alert=False):
    requests.post(f"{TELEGRAM_API}/answerCallbackQuery", json={'callback_query_id': callback_query_id, 'text': text, 'show_alert': show_alert})

def check_channel_membership(user_id):
    try:
        for channel in REQUIRED_CHANNELS:
            res = requests.get(f"{TELEGRAM_API}/getChatMember?chat_id={channel}&user_id={user_id}").json()
            if not res.get('ok') or res['result']['status'] in ['left', 'kicked']: return False, channel
        return True, None
    except: return False, None

def home_keyboard():
    return {'inline_keyboard': [[{'text': '🎁 Offers', 'callback_data': 'offers'}], [{'text': '💬 Help', 'callback_data': 'help'}], [{'text': '📱 Channels', 'callback_data': 'join_channel'}]]}

def home_keyboard_admin():
    kb = home_keyboard()
    kb['inline_keyboard'].append([{'text': '👨‍💼 Admin Panel', 'callback_data': 'admin_panel'}])
    return kb

def offer_keyboard():
    offers = get_enabled_offers()
    kb = {'inline_keyboard': [[{'text': f"🎁 {o['name']}", 'callback_data': f"select_offer_{o['_id']}"}] for o in offers[:10]]}
    kb['inline_keyboard'].append([{'text': '⬅️ Back', 'callback_data': 'home'}])
    return kb

def admin_keyboard():
    return {'inline_keyboard': [
        [{'text': '📊 Stats', 'callback_data': 'admin_stats'}, {'text': '📬 Help', 'callback_data': 'admin_help_requests'}],
        [{'text': '🎁 Manage Offers', 'callback_data': 'admin_manage_offers'}],
        [{'text': '📢 Broadcast', 'callback_data': 'admin_broadcast'}],
        [{'text': '🚫 Ban', 'callback_data': 'admin_ban'}, {'text': '✅ Unban', 'callback_data': 'admin_unban'}],
        [{'text': '⬅️ Back', 'callback_data': 'home'}]
    ]}

def manage_offers_keyboard():
    return {'inline_keyboard': [
        [{'text': '➕ Create', 'callback_data': 'offer_create'}, {'text': '✏️ Edit', 'callback_data': 'offer_edit'}],
        [{'text': '🗑️ Delete', 'callback_data': 'offer_delete'}, {'text': '📋 List', 'callback_data': 'offer_list'}],
        [{'text': '📊 Analytics', 'callback_data': 'admin_offer_analytics'}],
        [{'text': '⬅️ Back', 'callback_data': 'admin_panel'}]
    ]}

# ==================== WEBHOOK HANDLER ====================

@app.route(f'/webhook/{TELEGRAM_TOKEN}', methods=['POST'])
def webhook():
    try:
        update = request.json
        if 'message' in update:
            msg = update['message']
            uid, chat_id, text = msg['from']['id'], msg['chat']['id'], msg.get('text', '').strip()
            if is_user_banned(uid): return 'ok', 200
            user, _ = get_or_create_user(uid, msg['from'].get('username'), msg['from'].get('first_name'))

            if text == '/start':
                kb = home_keyboard_admin() if uid == ADMIN_ID else home_keyboard()
                send_message(chat_id, "👋 Welcome! Select an option:", reply_markup=kb)
            
            elif text == '/cancel':
                users_collection.update_one({'_id': uid}, {'$set': {'current_mode': None}})
                send_message(chat_id, "❌ Action cancelled.", reply_markup=home_keyboard())

            # Mode Handlers
            mode = user.get('current_mode')
            if mode == 'offer_mode' and text:
                offer = get_offer(user.get('current_offer_id'))
                if not offer or not validate_url_format(text, ""):
                    send_message(chat_id, "❌ Invalid URL.")
                else:
                    cid = extract_clickid_from_url(text)
                    if not cid: send_message(chat_id, "❌ No parameter found in URL.")
                    else:
                        send_message(chat_id, "⏳ Processing...")
                        res, succ, total = run_postbacks_sequence(cid, offer['postbacks'], offer['delays'], chat_id)
                        save_submission(uid, user['username'], offer['_id'], text, cid, res, succ, total)
                        send_message(chat_id, f"✅ Done! Success: {succ}", reply_markup=home_keyboard())
                users_collection.update_one({'_id': uid}, {'$set': {'current_mode': None}})

            elif mode == 'help_mode' and text:
                can, err = can_send_help_request(uid)
                if not can: send_message(chat_id, err)
                else:
                    add_help_request(uid, user['username'], text)
                    send_message(ADMIN_ID, f"📬 <b>Help Request</b> from @{user['username']}:\n{text}")
                    send_message(chat_id, "✅ Sent to support.")
                users_collection.update_one({'_id': uid}, {'$set': {'current_mode': None}})

            elif uid == ADMIN_ID:
                if mode == 'broadcast_mode':
                    for u in users_collection.find({'is_active': True}):
                        try: send_message(u['_id'], f"📢 <b>Announcement</b>\n\n{text}")
                        except: pass
                    send_message(chat_id, "✅ Broadcast complete.", reply_markup=admin_keyboard())
                elif mode == 'ban_mode':
                    ban_user(int(text))
                    send_message(chat_id, "✅ Banned.", reply_markup=admin_keyboard())
                elif mode == 'offer_create_mode':
                    p = text.split('|')
                    if len(p) >= 4:
                        mid = len(p) // 2 + 1
                        create_offer(p[0], p[1], p[2:mid], [int(x) for x in p[mid:]], uid)
                        send_message(chat_id, "✅ Created.", reply_markup=manage_offers_keyboard())
                users_collection.update_one({'_id': uid}, {'$set': {'current_mode': None}})

        elif 'callback_query' in update:
            cb = update['callback_query']
            uid, data, qid = cb['from']['id'], cb['data'], cb['id']
            if is_user_banned(uid): return 'ok', 200
            
            # 1. Handle Specific Admin Actions First
            if data == 'admin_panel' and uid == ADMIN_ID:
                send_message(uid, "🔧 Admin Panel", reply_markup=admin_keyboard())
            elif data == 'admin_manage_offers' and uid == ADMIN_ID:
                send_message(uid, "🎁 Manage Offers", reply_markup=manage_offers_keyboard())
            elif data == 'offer_create' and uid == ADMIN_ID:
                send_message(uid, "➕ Send: Name|Link|PB1|PB2|D1|D2")
                users_collection.update_one({'_id': uid}, {'$set': {'current_mode': 'offer_create_mode'}})
            elif data == 'offer_list' and uid == ADMIN_ID:
                offers = get_all_offers()
                txt = "\n".join([f"ID: <code>{o['_id']}</code> | {o['name']}" for o in offers])
                send_message(uid, f"📋 Offers:\n{txt or 'None'}")
            
            # 2. Handle generic prefix selection
            elif data.startswith('select_offer_'):
                oid = data.replace('select_offer_', '')
                offer = get_offer(oid)
                if offer:
                    send_message(uid, f"🎁 <b>{offer['name']}</b>\nSend your URL:")
                    users_collection.update_one({'_id': uid}, {'$set': {'current_mode': 'offer_mode', 'current_offer_id': oid}})
            
            elif data == 'offers':
                is_m, _ = check_channel_membership(uid)
                if is_m: send_message(uid, "🎁 Select Offer:", reply_markup=offer_keyboard())
                else: answer_callback_query(qid, "❌ Join channels first!", True)
            
            elif data == 'home':
                kb = home_keyboard_admin() if uid == ADMIN_ID else home_keyboard()
                send_message(uid, "🏠 Home", reply_markup=kb)

            answer_callback_query(qid, "")
        return 'ok', 200
    except Exception as e:
        print(f"Error: {e}")
        return 'error', 500

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=int(os.getenv('PORT', 5000)))
                                 
