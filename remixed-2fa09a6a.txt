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
    offers_collection = db['offers']  # NEW
    submissions_collection = db['submissions']  # NEW
    print("✅ MongoDB connected successfully")
except Exception as e:
    print(f"❌ MongoDB Connection Error: {e}")
    raise

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# Thread pool for concurrent operations
executor = ThreadPoolExecutor(max_workers=5)

# ==================== DATABASE FUNCTIONS ====================

def get_or_create_user(user_id, username, first_name):
    """Get or create user in database"""
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
    """Check if user is banned"""
    return banned_users_collection.find_one({'_id': user_id}) is not None

def ban_user(user_id):
    """Ban a user"""
    if not is_user_banned(user_id):
        banned_users_collection.insert_one({'_id': user_id, 'banned_at': datetime.utcnow()})
        return True
    return False

def unban_user(user_id):
    """Unban a user"""
    result = banned_users_collection.delete_one({'_id': user_id})
    return result.deleted_count > 0

def get_total_users():
    """Get total active users"""
    return users_collection.count_documents({'is_active': True})

def get_banned_users_count():
    """Get count of banned users"""
    return banned_users_collection.count_documents({})

def can_send_help_request(user_id):
    """Check if user can send help request (max 2 per day)"""
    user = users_collection.find_one({'_id': user_id})
    if not user:
        return False, "User not found"
    
    today = datetime.utcnow().date()
    last_date = user.get('last_help_request_date')
    
    if last_date and last_date.date() == today:
        if user.get('help_requests_today', 0) >= 2:
            return False, "⏳ You can send max 2 help messages per day. Try again tomorrow!"
    
    return True, ""

def add_help_request(user_id, username, message):
    """Add help request to database"""
    today = datetime.utcnow().date()
    user = users_collection.find_one({'_id': user_id})
    
    if user:
        last_date = user.get('last_help_request_date')
        if last_date and last_date.date() != today:
            users_collection.update_one(
                {'_id': user_id},
                {'$set': {'help_requests_today': 1, 'last_help_request_date': datetime.utcnow()}}
            )
        else:
            users_collection.update_one(
                {'_id': user_id},
                {'$inc': {'help_requests_today': 1}, '$set': {'last_help_request_date': datetime.utcnow()}}
            )
    
    request_id = help_requests_collection.insert_one({
        'user_id': user_id,
        'username': username,
        'message': message,
        'created_at': datetime.utcnow(),
        'admin_reply': None,
        'admin_replied_at': None,
        'status': 'pending'
    }).inserted_id
    
    return request_id

def get_pending_help_requests():
    """Get all pending help requests"""
    return list(help_requests_collection.find({'status': 'pending'}).sort('created_at', -1))

def reply_to_help_request(request_id, reply_text):
    """Admin replies to a help request"""
    try:
        help_req = help_requests_collection.find_one({'_id': request_id})
        if not help_req:
            return False, "Request not found"
        
        help_requests_collection.update_one(
            {'_id': request_id},
            {
                '$set': {
                    'admin_reply': reply_text,
                    'admin_replied_at': datetime.utcnow(),
                    'status': 'resolved'
                }
            }
        )
        
        user_id = help_req['user_id']
        username = help_req['username']
        original_message = help_req['message']
        
        send_message(
            user_id,
            f"<b>📬 Support Reply</b>\n\n"
            f"<b>Your Question:</b> {original_message}\n\n"
            f"<b>Admin Response:</b>\n{reply_text}\n\n"
            f"<b>From:</b> Support Team"
        )
        
        return True, f"Reply sent to @{username}"
    except Exception as e:
        return False, str(e)

# ==================== OFFER FUNCTIONS ====================

def is_valid_objectid(id_string):
    """Check if a string is a valid MongoDB ObjectId"""
    try:
        if not isinstance(id_string, str) or len(id_string) != 24:
            return False
        ObjectId(id_string)
        return True
    except:
        return False

def create_offer(name, starting_link, postback_links, days):
    """Create a new offer"""
    try:
        offer = {
            'name': name,
            'starting_link': starting_link,
            'postback_links': postback_links,
            'postback_count': len([p for p in postback_links if p]),
            'days': days,
            'enabled': True,
            'created_at': datetime.utcnow(),
            'total_submissions': 0,
            'total_success': 0
        }
        result = offers_collection.insert_one(offer)
        return result.inserted_id, True
    except Exception as e:
        return str(e), False

def get_offer(offer_id):
    """Get a specific offer by ID"""
    try:
        if not is_valid_objectid(offer_id):
            return None
        return offers_collection.find_one({'_id': ObjectId(offer_id)})
    except:
        return None

def get_all_offers():
    """Get all offers"""
    try:
        return list(offers_collection.find({}).sort('created_at', -1))
    except:
        return []

def update_offer(offer_id, name, starting_link, postback_links, days):
    """Update an existing offer"""
    try:
        if not is_valid_objectid(offer_id):
            return False, "Invalid offer ID format"
        
        offers_collection.update_one(
            {'_id': ObjectId(offer_id)},
            {
                '$set': {
                    'name': name,
                    'starting_link': starting_link,
                    'postback_links': postback_links,
                    'postback_count': len([p for p in postback_links if p]),
                    'days': days,
                    'updated_at': datetime.utcnow()
                }
            }
        )
        return True, "Offer updated successfully"
    except Exception as e:
        return False, str(e)

def delete_offer(offer_id):
    """Delete an offer"""
    try:
        if not is_valid_objectid(offer_id):
            return False, "Invalid offer ID format"
        
        result = offers_collection.delete_one({'_id': ObjectId(offer_id)})
        if result.deleted_count > 0:
            submissions_collection.delete_many({'offer_id': ObjectId(offer_id)})
            return True, "Offer deleted successfully"
        return False, "Offer not found"
    except Exception as e:
        return False, str(e)

def get_offer_analytics(offer_id):
    """Get analytics for a specific offer"""
    try:
        if not is_valid_objectid(offer_id):
            return {'total': 0, 'users': [], 'success_rate': 0}
        
        offer_id_obj = ObjectId(offer_id)
        submissions = list(submissions_collection.find({'offer_id': offer_id_obj}))
        
        total = len(submissions)
        users = list(set(sub['user_id'] for sub in submissions))
        
        success = sum(1 for sub in submissions if sub.get('status') == 'success')
        success_rate = (success / total * 100) if total > 0 else 0
        
        return {
            'total': total,
            'users': [str(u) for u in users],
            'success_rate': success_rate
        }
    except Exception as e:
        print(f"Analytics Error: {e}")
        return {'total': 0, 'users': [], 'success_rate': 0}

def add_submission(offer_id, user_id, tracking_data):
    """Add a submission for an offer"""
    try:
        if not is_valid_objectid(offer_id):
            return False
        
        submissions_collection.insert_one({
            'offer_id': ObjectId(offer_id),
            'user_id': user_id,
            'tracking_data': tracking_data,
            'created_at': datetime.utcnow(),
            'status': 'pending'
        })
        
        offers_collection.update_one(
            {'_id': ObjectId(offer_id)},
            {'$inc': {'total_submissions': 1}}
        )
        return True
    except Exception as e:
        print(f"Submission Error: {e}")
        return False

# ==================== TELEGRAM API FUNCTIONS ====================

def send_message(chat_id, text, reply_markup=None):
    """Send a message to a chat"""
    try:
        payload = {
            'chat_id': chat_id,
            'text': text,
            'parse_mode': 'HTML'
        }
        if reply_markup:
            payload['reply_markup'] = reply_markup
        
        response = requests.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=10)
        return response.status_code == 200
    except Exception as e:
        print(f"Send Message Error: {e}")
        return False

def answer_callback_query(callback_query_id, text, show_alert=False):
    """Answer a callback query"""
    try:
        payload = {
            'callback_query_id': callback_query_id,
            'text': text,
            'show_alert': show_alert
        }
        requests.post(f"{TELEGRAM_API}/answerCallbackQuery", json=payload, timeout=10)
    except Exception as e:
        print(f"Callback Error: {e}")

def notify_admin_new_user(user_id, username, first_name):
    """Notify admin of new user"""
    try:
        text = f"<b>👤 New User Joined</b>\n\nID: <code>{user_id}</code>\nUsername: @{username}\nName: {first_name}"
        send_message(ADMIN_ID, text)
    except Exception as e:
        print(f"Notify Error: {e}")

# ==================== KEYBOARD FUNCTIONS ====================

def main_keyboard():
    """Main menu keyboard"""
    return {
        'inline_keyboard': [
            [{'text': '🎁 Browse Offers', 'callback_data': 'browse_offers'}],
            [{'text': '❓ Help & Support', 'callback_data': 'help'}],
            [{'text': '📊 My Stats', 'callback_data': 'stats'}]
        ]
    }

def admin_keyboard():
    """Admin menu keyboard"""
    return {
        'inline_keyboard': [
            [{'text': '🎁 Manage Offers', 'callback_data': 'admin_manage_offers'}],
            [{'text': '📊 Analytics', 'callback_data': 'admin_offer_analytics'}],
            [{'text': '📬 Help Requests', 'callback_data': 'admin_help_requests'}],
            [{'text': '📢 Broadcast', 'callback_data': 'admin_broadcast'}],
            [{'text': '🚫 Ban User', 'callback_data': 'admin_ban'},
             {'text': '✅ Unban User', 'callback_data': 'admin_unban'}]
        ]
    }

def manage_offers_keyboard():
    """Manage offers keyboard"""
    return {
        'inline_keyboard': [
            [{'text': '➕ Create Offer', 'callback_data': 'offer_create'}],
            [{'text': '📋 List Offers', 'callback_data': 'offer_list'}],
            [{'text': '✏️ Edit Offer', 'callback_data': 'offer_edit'}],
            [{'text': '🗑️ Delete Offer', 'callback_data': 'offer_delete'}],
            [{'text': '🔙 Back', 'callback_data': 'back_to_admin'}]
        ]
    }

# ==================== WEBHOOK HANDLER ====================

@app.route('/webhook', methods=['POST'])
def webhook():
    """Handle incoming webhook updates from Telegram"""
    try:
        update = request.get_json()
        
        # Handle regular messages
        if 'message' in update:
            msg = update['message']
            user_id = msg['from']['id']
            username = msg['from'].get('username', 'Unknown')
            first_name = msg['from'].get('first_name', 'User')
            chat_id = msg['chat']['id']
            text = msg.get('text', '')
            
            # Check if user is banned
            if is_user_banned(user_id):
                return 'ok', 200
            
            # Get or create user
            user, is_new = get_or_create_user(user_id, username, first_name)
            current_mode = user.get('current_mode')
            
            # Handle /start command
            if text == '/start':
                send_message(chat_id, f"<b>Welcome to Offer Bot, {first_name}! 🎁</b>", reply_markup=main_keyboard())
                return 'ok', 200
            
            # Handle /help command
            if text == '/help':
                send_message(chat_id, "<b>❓ Help & Support</b>\n\nUse the menu buttons to browse offers and submit your data.", reply_markup=main_keyboard())
                return 'ok', 200
            
            # Handle /cancel command
            if text == '/cancel':
                users_collection.update_one({'_id': user_id}, {'$set': {'current_mode': None}})
                send_message(chat_id, "✅ Mode cancelled.", reply_markup=admin_keyboard() if user_id == ADMIN_ID else main_keyboard())
                return 'ok', 200
            
            # OFFER CREATE MODE
            if current_mode == 'offer_create_mode' and user_id == ADMIN_ID:
                try:
                    parts = text.split('|')
                    if len(parts) < 3:
                        send_message(chat_id, "❌ Invalid format. Use: Name|StartLink|PB1|PB2|...|D1|D2|...")
                        return 'ok', 200
                    
                    name = parts[0].strip()
                    starting_link = parts[1].strip()
                    
                    postback_count = min(5, len(parts) - 2)
                    postback_links = [parts[i+2].strip() if i+2 < len(parts) and parts[i+2].strip() else '' for i in range(5)]
                    days = [int(parts[i+7].strip()) if i+7 < len(parts) and parts[i+7].strip().isdigit() else 0 for i in range(4)]
                    
                    offer_id, success = create_offer(name, starting_link, postback_links, days)
                    
                    if success:
                        users_collection.update_one({'_id': user_id}, {'$set': {'current_mode': None}})
                        send_message(chat_id, f"✅ Offer created!\n\nID: <code>{offer_id}</code>", reply_markup=manage_offers_keyboard())
                    else:
                        send_message(chat_id, f"❌ Error: {offer_id}")
                except Exception as e:
                    send_message(chat_id, f"❌ Error: {str(e)}")
                
                return 'ok', 200
            
            # OFFER DELETE MODE
            if current_mode == 'offer_delete_mode' and user_id == ADMIN_ID:
                offer_id = text.strip()
                success, msg_text = delete_offer(offer_id)
                users_collection.update_one({'_id': user_id}, {'$set': {'current_mode': None}})
                send_message(chat_id, f"{'✅' if success else '❌'} {msg_text}", reply_markup=manage_offers_keyboard())
                return 'ok', 200
            
            # OFFER EDIT MODE
            if current_mode == 'offer_edit_mode' and user_id == ADMIN_ID:
                try:
                    parts = text.split('|')
                    if len(parts) < 3:
                        send_message(chat_id, "❌ Invalid format. Use: OfferID|Name|StartLink|PB1|PB2|...|D1|D2|...")
                        return 'ok', 200
                    
                    offer_id = parts[0].strip()
                    name = parts[1].strip()
                    starting_link = parts[2].strip()
                    
                    postback_links = [parts[i+3].strip() if i+3 < len(parts) and parts[i+3].strip() else '' for i in range(5)]
                    days = [int(parts[i+8].strip()) if i+8 < len(parts) and parts[i+8].strip().isdigit() else 0 for i in range(4)]
                    
                    success, msg_text = update_offer(offer_id, name, starting_link, postback_links, days)
                    users_collection.update_one({'_id': user_id}, {'$set': {'current_mode': None}})
                    send_message(chat_id, f"{'✅' if success else '❌'} {msg_text}", reply_markup=manage_offers_keyboard())
                except Exception as e:
                    send_message(chat_id, f"❌ Error: {str(e)}")
                
                return 'ok', 200
            
            # BAN MODE
            if current_mode == 'ban_mode' and user_id == ADMIN_ID:
                try:
                    ban_user_id = int(text.strip())
                    ban_success = ban_user(ban_user_id)
                    users_collection.update_one({'_id': user_id}, {'$set': {'current_mode': None}})
                    send_message(chat_id, f"{'✅ User banned' if ban_success else '❌ User already banned'}", reply_markup=admin_keyboard())
                except ValueError:
                    send_message(chat_id, "❌ Invalid user ID")
                
                return 'ok', 200
            
            # UNBAN MODE
            if current_mode == 'unban_mode' and user_id == ADMIN_ID:
                try:
                    unban_user_id = int(text.strip())
                    unban_success = unban_user(unban_user_id)
                    users_collection.update_one({'_id': user_id}, {'$set': {'current_mode': None}})
                    send_message(chat_id, f"{'✅ User unbanned' if unban_success else '❌ User not found'}", reply_markup=admin_keyboard())
                except ValueError:
                    send_message(chat_id, "❌ Invalid user ID")
                
                return 'ok', 200
            
            # BROADCAST MODE
            if current_mode == 'broadcast_mode' and user_id == ADMIN_ID:
                try:
                    all_users = users_collection.find({'is_active': True})
                    count = 0
                    for u in all_users:
                        send_message(u['_id'], text)
                        count += 1
                    
                    users_collection.update_one({'_id': user_id}, {'$set': {'current_mode': None}})
                    send_message(chat_id, f"✅ Message sent to {count} users", reply_markup=admin_keyboard())
                except Exception as e:
                    send_message(chat_id, f"❌ Error: {str(e)}")
                
                return 'ok', 200
            
            # HELP REQUEST MODE
            if current_mode == 'help_request_mode':
                can_send, restriction_msg = can_send_help_request(user_id)
                
                if not can_send:
                    send_message(chat_id, restriction_msg, reply_markup=main_keyboard())
                    return 'ok', 200
                
                request_id = add_help_request(user_id, username, text)
                users_collection.update_one({'_id': user_id}, {'$set': {'current_mode': None}})
                send_message(chat_id, f"✅ Support request sent! (ID: <code>{request_id}</code>)", reply_markup=main_keyboard())
                return 'ok', 200
        
        # Handle callback queries
        if 'callback_query' in update:
            callback_query = update['callback_query']
            callback_query_id = callback_query['id']
            user_id = callback_query['from']['id']
            chat_id = callback_query['message']['chat']['id']
            callback_data = callback_query['data']
            
            # Check if user is banned
            if is_user_banned(user_id):
                answer_callback_query(callback_query_id, "❌ You are banned!", show_alert=True)
                return 'ok', 200
            
            # Admin panel
            if callback_data == 'admin_panel':
                if user_id != ADMIN_ID:
                    answer_callback_query(callback_query_id, "❌ Admin only!", show_alert=True)
                    return 'ok', 200
                
                answer_callback_query(callback_query_id, "")
                send_message(chat_id, "<b>⚙️ Admin Panel</b>", reply_markup=admin_keyboard())
            
            # Help requests
            elif callback_data == 'admin_help_requests':
                if user_id != ADMIN_ID:
                    return 'ok', 200
                
                answer_callback_query(callback_query_id, "")
                requests_list = get_pending_help_requests()
                
                if requests_list:
                    text = "<b>📬 Pending Help Requests</b>\n\n"
                    for req in requests_list[:10]:
                        text += (f"<b>From:</b> @{req['username']}\n"
                                f"<b>Message:</b> {req['message'][:100]}...\n"
                                f"<b>ID:</b> <code>{str(req['_id'])}</code>\n\n")
                    send_message(user_id, text[:4000], reply_markup=admin_keyboard())
                else:
                    send_message(user_id, "📭 No pending help requests.", reply_markup=admin_keyboard())
            
            # Admin broadcast
            elif callback_data == 'admin_broadcast':
                if user_id != ADMIN_ID:
                    return 'ok', 200
                
                answer_callback_query(callback_query_id, "")
                send_message(
                    user_id,
                    "📢 <b>Broadcast Mode</b>\n\n"
                    "Send the message you want to broadcast to all users.\n\n"
                    "Type /cancel to exit this mode."
                )
                users_collection.update_one({'_id': user_id}, {'$set': {'current_mode': 'broadcast_mode'}})
            
            # Admin manage offers
            elif callback_data == 'admin_manage_offers':
                if user_id != ADMIN_ID:
                    answer_callback_query(callback_query_id, "❌ Admin only!", show_alert=True)
                    return 'ok', 200
                
                answer_callback_query(callback_query_id, "")
                send_message(
                    chat_id,
                    "🎁 <b>Manage Offers</b>\n\n"
                    "Select an option:",
                    reply_markup=manage_offers_keyboard()
                )
            
            # Offer list
            elif callback_data == 'offer_list':
                if user_id != ADMIN_ID:
                    answer_callback_query(callback_query_id, "❌ Admin only!", show_alert=True)
                    return 'ok', 200
                
                answer_callback_query(callback_query_id, "")
                offers = get_all_offers()
                
                if offers:
                    text = "<b>📋 All Offers</b>\n\n"
                    for i, offer in enumerate(offers, 1):
                        status = "✅" if offer['enabled'] else "❌"
                        text += (f"<b>{i}. {offer['name']}</b>\n"
                                f"   Link: {offer['starting_link']}\n"
                                f"   Postbacks: {offer['postback_count']}\n"
                                f"   Status: {status}\n"
                                f"   ID: <code>{str(offer['_id'])}</code>\n\n")
                    send_message(chat_id, text[:4000], reply_markup=manage_offers_keyboard())
                else:
                    send_message(chat_id, "📭 No offers created yet.", reply_markup=manage_offers_keyboard())
            
            # Offer delete
            elif callback_data == 'offer_delete':
                if user_id != ADMIN_ID:
                    answer_callback_query(callback_query_id, "❌ Admin only!", show_alert=True)
                    return 'ok', 200
                
                answer_callback_query(callback_query_id, "")
                send_message(
                    chat_id,
                    "🗑️ <b>Delete Offer</b>\n\n"
                    "Send the Offer ID you want to delete.\n\n"
                    "Get ID from: Manage Offers → List Offers\n\n"
                    "Type /cancel to exit this mode."
                )
                users_collection.update_one({'_id': user_id}, {'$set': {'current_mode': 'offer_delete_mode'}})
            
            # Offer edit
            elif callback_data == 'offer_edit':
                if user_id != ADMIN_ID:
                    answer_callback_query(callback_query_id, "❌ Admin only!", show_alert=True)
                    return 'ok', 200
                
                answer_callback_query(callback_query_id, "")
                send_message(
                    chat_id,
                    "✏️ <b>Edit Offer</b>\n\n"
                    "Send in format:\n"
                    "<code>OfferID|NewName|NewStartLink|NewPB1|NewPB2|...|NewD1|NewD2|...</code>\n\n"
                    "Get ID from: Manage Offers → List Offers\n\n"
                    "Type /cancel to exit this mode."
                )
                users_collection.update_one({'_id': user_id}, {'$set': {'current_mode': 'offer_edit_mode'}})
            
            # Admin offer analytics
            elif callback_data == 'admin_offer_analytics':
                if user_id != ADMIN_ID:
                    return 'ok', 200
                
                answer_callback_query(callback_query_id, "")
                offers = get_all_offers()
                
                if offers:
                    text = "<b>📊 OFFER ANALYTICS</b>\n\n"
                    text += f"<b>Total Offers:</b> {len(offers)}\n"
                    text += f"<b>Total Submissions:</b> {sum(o.get('total_submissions', 0) for o in offers)}\n\n"
                    
                    for i, offer in enumerate(offers, 1):
                        analytics = get_offer_analytics(str(offer['_id']))
                        text += (f"<b>{i}. {offer['name']}</b>\n"
                                f"   Starting Link: {offer['starting_link']}\n"
                                f"   Postbacks: {offer['postback_count']}\n"
                                f"   Status: {'✅ Enabled' if offer['enabled'] else '❌ Disabled'}\n"
                                f"   👥 Submissions: {analytics['total']}\n"
                                f"   👤 Users: {', '.join(analytics['users'][:5])}\n"
                                f"   📈 Success Rate: {analytics['success_rate']:.1f}%\n\n")
                    
                    send_message(user_id, text[:4000], reply_markup=admin_keyboard())
                else:
                    send_message(user_id, "📭 No offers yet.", reply_markup=admin_keyboard())
            
            # Admin ban
            elif callback_data == 'admin_ban':
                if user_id != ADMIN_ID:
                    return 'ok', 200
                
                answer_callback_query(callback_query_id, "")
                send_message(
                    user_id,
                    "🚫 <b>Ban User</b>\n\n"
                    "Send the user ID you want to ban.\n\n"
                    "Type /cancel to exit this mode."
                )
                users_collection.update_one({'_id': user_id}, {'$set': {'current_mode': 'ban_mode'}})
            
            # Admin unban
            elif callback_data == 'admin_unban':
                if user_id != ADMIN_ID:
                    return 'ok', 200
                
                answer_callback_query(callback_query_id, "")
                send_message(
                    user_id,
                    "✅ <b>Unban User</b>\n\n"
                    "Send the user ID you want to unban.\n\n"
                    "Type /cancel to exit this mode."
                )
                users_collection.update_one({'_id': user_id}, {'$set': {'current_mode': 'unban_mode'}})
            
            # Offer create
            elif callback_data == 'offer_create':
                if user_id != ADMIN_ID:
                    answer_callback_query(callback_query_id, "❌ Admin only!", show_alert=True)
                    return 'ok', 200
                
                answer_callback_query(callback_query_id, "")
                send_message(
                    chat_id,
                    "➕ <b>Create New Offer</b>\n\n"
                    "Send in format:\n"
                    "<code>Name|StartLink|PB1|PB2|PB3|PB4|PB5|D1|D2|D3|D4</code>\n\n"
                    "<b>Custom Variables:</b>\n"
                    "Use <code>$variable_name</code> in postback URLs\n"
                    "Example: <code>https://example.com?tid=$clickid</code>\n"
                    "or: <code>https://track.com?id=$myvar</code>\n\n"
                    "<b>Variable Extraction:</b>\n"
                    "User sends: <code>https://example.com?clickid=abc123</code>\n"
                    "Bot extracts: <code>abc123</code>\n"
                    "And replaces <code>$clickid</code> in postbacks\n\n"
                    "<b>Examples:</b>\n"
                    "1 postback: <code>Simple|https://example.com|https://example.com?tid=$clickid|0</code>\n\n"
                    "3 postbacks: <code>Premium|https://premium.com|https://premium.com?tid=$id|https://track.com?user=$id|https://log.com?data=$id|5|10|8</code>\n\n"
                    "<b>Use 1-5 postbacks, leave extras blank</b>"
                )
                users_collection.update_one({'_id': user_id}, {'$set': {'current_mode': 'offer_create_mode'}})
        
        return 'ok', 200
    
    except Exception as e:
        print(f"Webhook Error: {e}")
        return 'error', 500

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    try:
        client.server_info()
        return {'status': 'ok', 'database': 'connected'}, 200
    except:
        return {'status': 'error', 'database': 'disconnected'}, 500

@app.route('/', methods=['GET'])
def index():
    """Root endpoint"""
    return {
        'bot_name': 'Telegram Offer Bot v3',
        'version': '3.0.1',
        'status': 'running',
        'features': ['2-Channel Verification', '1-5 Postbacks', 'Analytics', 'Admin Controls'],
        'admin_id': ADMIN_ID,
        'channels': [CHANNEL_1_NAME, CHANNEL_2_NAME]
    }, 200

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)
