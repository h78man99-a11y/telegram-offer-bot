import os
import json
import requests
from flask import Flask, request
from pymongo import MongoClient
from bson.objectid import ObjectId
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Initialize Flask app
app = Flask(__name__)

# Configuration from Environment Variables
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
MONGODB_URI = os.getenv("MONGODB_URI", "YOUR_MONGODB_URI")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# Multiple Channels Configuration ⭐
CHANNEL_1_NAME = os.getenv("CHANNEL_1_NAME", "@YOUR_CHANNEL_1")
CHANNEL_2_NAME = os.getenv("CHANNEL_2_NAME", "@YOUR_CHANNEL_2")

# List of all channels (for membership verification)
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
    print("✅ MongoDB connected successfully")
except Exception as e:
    print(f"❌ MongoDB Connection Error: {e}")
    raise

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

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
        # Notify admin of new user
        notify_admin_new_user(user_id, username, first_name)
        return users_collection.find_one({'_id': user_id}), is_new_user
    else:
        # Ensure current_mode field exists (for users created before this update)
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
        users_collection.update_one({'_id': user_id}, {'$set': {'is_active': False}})
        return True
    return False

def unban_user(user_id):
    """Unban a user"""
    if banned_users_collection.delete_one({'_id': user_id}).deleted_count > 0:
        users_collection.update_one({'_id': user_id}, {'$set': {'is_active': True}})
        return True
    return False

def get_total_users():
    """Get total active users"""
    return users_collection.count_documents({'is_active': True})

def get_banned_users_count():
    """Get total banned users"""
    return banned_users_collection.count_documents({})

def check_channel_membership(user_id):
    """Check if user is member of ALL required channels"""
    try:
        for channel in REQUIRED_CHANNELS:
            # Remove @ if present
            channel_name = channel.replace('@', '')
            url = f"{TELEGRAM_API}/getChatMember?chat_id=@{channel_name}&user_id={user_id}"
            response = requests.get(url, timeout=5)
            data = response.json()
            if data['ok']:
                status = data['result']['status']
                if status in ['left', 'kicked']:
                    return False, channel  # Return which channel user is not member of
            else:
                return False, channel
        return True, None  # User is member of all channels
    except Exception as e:
        print(f"Channel check error: {e}")
        return False, None

def can_send_help_request(user_id):
    """Check if user can send help request (max 2 per day)"""
    user = users_collection.find_one({'_id': user_id})
    if not user:
        return True, None
    
    today = datetime.utcnow().date()
    last_date = user.get('last_help_request_date')
    
    if last_date and last_date.date() == today:
        if user.get('help_requests_today', 0) >= 2:
            return False, "❌ You have reached your daily limit (2 messages/day). Try again tomorrow."
    return True, None

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
    
    # Insert help request with tracking ID for replies
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
        # Get the help request to find user_id
        help_req = help_requests_collection.find_one({'_id': request_id})
        if not help_req:
            return False, "Request not found"
        
        # Update the request with admin reply
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
        
        # Send reply to user
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

def get_recent_joined_users(limit=20):
    """Get list of recently joined users"""
    return list(users_collection.find({'is_active': True}).sort('created_at', -1).limit(limit))

# ==================== TELEGRAM FUNCTIONS ====================

def send_message(chat_id, text, reply_markup=None, parse_mode="HTML"):
    """Send a message to user"""
    url = f"{TELEGRAM_API}/sendMessage"
    data = {
        'chat_id': chat_id,
        'text': text,
        'parse_mode': parse_mode
    }
    if reply_markup:
        data['reply_markup'] = json.dumps(reply_markup)
    
    try:
        response = requests.post(url, json=data, timeout=10)
        return response.json()
    except Exception as e:
        print(f"Error sending message: {e}")
        return None

def notify_admin_new_user(user_id, username, first_name):
    """Notify admin when new user joins"""
    try:
        send_message(
            ADMIN_ID,
            f"🆕 <b>NEW USER JOINED!</b>\n\n"
            f"<b>Name:</b> {first_name}\n"
            f"<b>Username:</b> @{username or 'no_username'}\n"
            f"<b>User ID:</b> <code>{user_id}</code>\n"
            f"<b>Joined At:</b> {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC",
        )
    except:
        pass

def answer_callback_query(callback_query_id, text, show_alert=False):
    """Answer callback query"""
    url = f"{TELEGRAM_API}/answerCallbackQuery"
    data = {
        'callback_query_id': callback_query_id,
        'text': text,
        'show_alert': show_alert
    }
    try:
        requests.post(url, json=data, timeout=5)
    except:
        pass

def home_keyboard():
    """Return home keyboard"""
    return {
        'inline_keyboard': [
            [{'text': '🎁 Offers', 'callback_data': 'offers'}],
            [{'text': '💬 Help & Support', 'callback_data': 'help'}],
            [{'text': '📢 Join Channel', 'callback_data': 'join_channel'}]
        ]
    }

def home_keyboard_admin():
    """Return home keyboard for admin"""
    keyboard = home_keyboard()
    keyboard['inline_keyboard'].append([{'text': '👨‍💼 Admin Panel', 'callback_data': 'admin_panel'}])
    return keyboard

def offer_keyboard():
    """Return offer selection keyboard"""
    return {
        'inline_keyboard': [
            [{'text': 'Offer18', 'callback_data': 'offer_offer18'}],
            [{'text': 'Second Offer', 'callback_data': 'offer_second'}],
            [{'text': '⬅️ Back', 'callback_data': 'home'}]
        ]
    }

def join_channels_keyboard():
    """Return keyboard for joining channels"""
    channel_1_link = CHANNEL_1_NAME.replace("@", "")
    channel_2_link = CHANNEL_2_NAME.replace("@", "")
    return {
        'inline_keyboard': [
            [{'text': f'📱 Join {CHANNEL_1_NAME}', 'url': f'https://t.me/{channel_1_link}'}],
            [{'text': f'📱 Join {CHANNEL_2_NAME}', 'url': f'https://t.me/{channel_2_link}'}],
            [{'text': '✅ Check Membership', 'callback_data': 'check_membership'}],
            [{'text': '⬅️ Back', 'callback_data': 'home'}]
        ]
    }

def admin_keyboard():
    """Return admin panel keyboard"""
    return {
        'inline_keyboard': [
            [{'text': '📊 Stats', 'callback_data': 'admin_stats'}],
            [{'text': '👥 Recent Joins', 'callback_data': 'admin_recent_joins'}],
            [{'text': '📬 Help Requests', 'callback_data': 'admin_help_requests'}],
            [{'text': '💬 Reply to Support', 'callback_data': 'admin_reply_mode'}],
            [{'text': '📢 Broadcast', 'callback_data': 'admin_broadcast'}],
            [{'text': '🚫 Ban User', 'callback_data': 'admin_ban'}],
            [{'text': '✅ Unban User', 'callback_data': 'admin_unban'}],
            [{'text': '⬅️ Back', 'callback_data': 'home'}]
        ]
    }

# ==================== OFFER HANDLING ====================

def extract_clickid_from_url(url):
    """Extract clickid from URL"""
    try:
        parsed_url = urlparse(url)
        params = parse_qs(parsed_url.query)
        clickid = params.get('clickid', [None])[0]
        return clickid
    except:
        return None

def send_postback(clickid):
    """Send postback request to offer server and return response"""
    try:
        postback_url = f"{OFFER18_URL}?tid={clickid}"
        
        # Send postback request with timeout
        response = requests.get(postback_url, timeout=15)
        
        # Return the response text with status
        if response.status_code == 200:
            response_text = response.text
            # Limit response length for display
            if len(response_text) > 1000:
                response_text = response_text[:1000] + "\n\n[Response truncated...]"
            return True, response_text, response.status_code
        else:
            return False, f"Server error: {response.status_code}", response.status_code
    
    except requests.Timeout:
        return False, "⏱️ Request timeout. Server took more than 15 seconds to respond.", 0
    except requests.ConnectionError:
        return False, "❌ Connection error. Could not reach server.", 0
    except Exception as e:
        return False, f"❌ Error: {str(e)[:200]}", 0

# ==================== WEBHOOK HANDLER ====================

@app.route(f'/webhook/{TELEGRAM_TOKEN}', methods=['POST'])
def webhook():
    """Main webhook handler"""
    try:
        update = request.json
        
        # Handle messages
        if 'message' in update:
            message = update['message']
            user_id = message['from']['id']
            username = message['from'].get('username', '')
            first_name = message['from'].get('first_name', 'User')
            text = message.get('text', '').strip()
            
            # Check if user is banned
            if is_user_banned(user_id):
                return 'ok', 200
            
            # Get or create user
            user, is_new_user = get_or_create_user(user_id, username, first_name)
            
            # Check if user is banned
            if is_user_banned(user_id):
                return 'ok', 200
            
            # Handle /start command
            if text == '/start':
                keyboard = home_keyboard_admin() if user_id == ADMIN_ID else home_keyboard()
                send_message(
                    user_id,
                    f"👋 Welcome <b>{first_name}!</b>\n\n"
                    "Please join our channels to use all features.\n\n"
                    "Select an option below:",
                    reply_markup=keyboard
                )
            
            # Handle help mode - messages while in help mode
            elif user.get('current_mode') == 'help_mode' and text:
                can_send, error_msg = can_send_help_request(user_id)
                if not can_send:
                    send_message(user_id, error_msg)
                else:
                    add_help_request(user_id, username, text)
                    
                    # Send to admin
                    send_message(
                        ADMIN_ID,
                        f"<b>📬 New Help Request</b>\n\n"
                        f"<b>From:</b> {first_name} (@{username or 'no_username'})\n"
                        f"<b>User ID:</b> <code>{user_id}</code>\n"
                        f"<b>Message:</b> {text}\n"
                        f"<b>Time:</b> {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC",
                    )
                    
                    send_message(user_id, "✅ Your message has been sent to support. We'll help you soon!")
                    
                    # Reset mode
                    users_collection.update_one({'_id': user_id}, {'$set': {'current_mode': None}})
            
            # Handle broadcast mode (admin only)
            elif user.get('current_mode') == 'broadcast_mode' and user_id == ADMIN_ID and text:
                all_users = users_collection.find({'is_active': True})
                success = 0
                failed = 0
                
                for u in all_users:
                    try:
                        send_message(u['_id'], f"📢 <b>Announcement</b>\n\n{text}")
                        success += 1
                    except:
                        failed += 1
                
                send_message(
                    user_id,
                    f"✅ <b>Broadcast Complete</b>\n\n"
                    f"<b>Sent to:</b> {success} users\n"
                    f"<b>Failed:</b> {failed} users",
                    reply_markup=admin_keyboard()
                )
                
                users_collection.update_one({'_id': user_id}, {'$set': {'current_mode': None}})
                if OFFER18_URL in text or 'offer18.com' in text:
                    clickid = extract_clickid_from_url(text)
                    if clickid:
                        success, response_text, status_code = send_postback(clickid)
                        
                        if success:
                            send_message(
                                user_id,
                                f"<b>✅ Postback Successful!</b>\n\n"
                                f"<b>Clickid:</b> <code>{clickid}</code>\n"
                                f"<b>Status Code:</b> <code>{status_code}</code>\n\n"
                                f"<b>Server Response:</b>\n<code>{response_text}</code>",
                            )
                        else:
                            send_message(
                                user_id,
                                f"<b>❌ Postback Failed</b>\n\n"
                                f"<b>Clickid:</b> <code>{clickid}</code>\n"
                                f"<b>Error:</b> {response_text}",
                            )
                    else:
                        send_message(user_id, "❌ Could not extract <code>clickid</code> from URL. Make sure the URL contains: <code>?clickid=YOUR_ID</code>")
                else:
                    send_message(user_id, f"❌ Invalid offer URL. Should contain <code>{OFFER18_URL}</code>")
                
                # Reset mode
                users_collection.update_one({'_id': user_id}, {'$set': {'current_mode': None}})
                send_message(user_id, "🏠 Select an option:", reply_markup=home_keyboard())
            
            # Handle broadcast mode (admin only)
            elif user.get('current_mode') == 'broadcast_mode' and user_id == ADMIN_ID and text:
                all_users = users_collection.find({'is_active': True})
                success = 0
                failed = 0
                
                for u in all_users:
                    try:
                        send_message(u['_id'], f"📢 <b>Announcement</b>\n\n{text}")
                        success += 1
                    except:
                        failed += 1
                
                send_message(
                    user_id,
                    f"✅ <b>Broadcast Complete</b>\n\n"
                    f"<b>Sent to:</b> {success} users\n"
                    f"<b>Failed:</b> {failed} users",
                    reply_markup=admin_keyboard()
                )
                
                users_collection.update_one({'_id': user_id}, {'$set': {'current_mode': None}})
            
            # Handle ban mode (admin only)
            elif user.get('current_mode') == 'ban_mode' and user_id == ADMIN_ID and text:
                try:
                    target_user_id = int(text)
                    if ban_user(target_user_id):
                        send_message(user_id, f"✅ User <code>{target_user_id}</code> has been banned!", reply_markup=admin_keyboard())
                    else:
                        send_message(user_id, f"⚠️ User <code>{target_user_id}</code> is already banned!", reply_markup=admin_keyboard())
                except ValueError:
                    send_message(user_id, "❌ Invalid user ID. Please send only numbers.", reply_markup=admin_keyboard())
                
                users_collection.update_one({'_id': user_id}, {'$set': {'current_mode': None}})
            
            # Handle unban mode (admin only)
            elif user.get('current_mode') == 'unban_mode' and user_id == ADMIN_ID and text:
                try:
                    target_user_id = int(text)
                    if unban_user(target_user_id):
                        send_message(user_id, f"✅ User <code>{target_user_id}</code> has been unbanned!", reply_markup=admin_keyboard())
                    else:
                        send_message(user_id, f"⚠️ User <code>{target_user_id}</code> is not banned!", reply_markup=admin_keyboard())
                except ValueError:
                    send_message(user_id, "❌ Invalid user ID. Please send only numbers.", reply_markup=admin_keyboard())
                
                users_collection.update_one({'_id': user_id}, {'$set': {'current_mode': None}})
            
            # Handle admin reply mode
            elif user.get('current_mode') == 'admin_reply_mode' and user_id == ADMIN_ID and text:
                try:
                    # Format: request_id|reply_text
                    if '|' in text:
                        request_id_str, reply_text = text.split('|', 1)
                        request_id_str = request_id_str.strip()
                        reply_text = reply_text.strip()
                        
                        # Convert string ID to ObjectId
                        from bson.objectid import ObjectId
                        try:
                            request_id = ObjectId(request_id_str)
                            success, message = reply_to_help_request(request_id, reply_text)
                            
                            if success:
                                send_message(user_id, f"✅ {message}", reply_markup=admin_keyboard())
                            else:
                                send_message(user_id, f"❌ Error: {message}", reply_markup=admin_keyboard())
                        except:
                            send_message(user_id, f"❌ Invalid request ID format", reply_markup=admin_keyboard())
                    else:
                        send_message(user_id, "❌ Invalid format. Use: <code>REQUEST_ID|Your Reply</code>", reply_markup=admin_keyboard())
                except Exception as e:
                    send_message(user_id, f"❌ Error: {str(e)}", reply_markup=admin_keyboard())
                
                users_collection.update_one({'_id': user_id}, {'$set': {'current_mode': None}})
        
        # Handle callback queries (button clicks)
        elif 'callback_query' in update:
            callback = update['callback_query']
            user_id = callback['from']['id']
            username = callback['from'].get('username', '')
            first_name = callback['from'].get('first_name', 'User')
            callback_data = callback['data']
            callback_query_id = callback['id']
            chat_id = callback['message']['chat']['id']
            
            # Check if user is banned
            if is_user_banned(user_id):
                return 'ok', 200
            
            # Get or create user
            user, is_new_user = get_or_create_user(user_id, username, first_name)
            
            # Check channel membership for most features
            if callback_data in ['offers', 'help', 'offer_offer18', 'offer_second']:
                is_member, missing_channel = check_channel_membership(user_id)
                if not is_member:
                    answer_callback_query(callback_query_id, "❌ You must join all channels first!", show_alert=True)
                    send_message(
                        user_id,
                        f"❌ <b>Channel Membership Required</b>\n\n"
                        f"Please join <b>BOTH</b> channels to continue:\n\n"
                        f"1️⃣ {CHANNEL_1_NAME}\n"
                        f"2️⃣ {CHANNEL_2_NAME}\n\n"
                        f"After joining both, click the button below to verify.",
                        reply_markup={'inline_keyboard': [[{'text': '✅ Check Membership', 'callback_data': 'check_membership'}]]}
                    )
                    return 'ok', 200
            
            # Home menu
            if callback_data == 'home':
                answer_callback_query(callback_query_id, "")
                keyboard = home_keyboard_admin() if user_id == ADMIN_ID else home_keyboard()
                send_message(
                    user_id,
                    "🏠 <b>Home Menu</b>\n\nSelect an option:",
                    reply_markup=keyboard
                )
            
            # Offers menu
            elif callback_data == 'offers':
                answer_callback_query(callback_query_id, "")
                send_message(
                    user_id,
                    "🎁 <b>Select an Offer</b>",
                    reply_markup=offer_keyboard()
                )
            
            # Offer18
            elif callback_data == 'offer_offer18':
                answer_callback_query(callback_query_id, "")
                send_message(
                    user_id,
                    f"📎 <b>Offer18</b>\n\n"
                    f"Send your offer URL with clickid parameter:\n\n"
                    f"<code>https://offer18.com?clickid=YOUR_CLICKID</code>\n\n"
                    f"Example:\n<code>https://offer18.com?clickid=abc123def456</code>\n\n"
                    f"The bot will extract the clickid and send a postback request to the server."
                )
                users_collection.update_one({'_id': user_id}, {'$set': {'current_mode': 'offer_mode'}})
            
            # Second Offer
            elif callback_data == 'offer_second':
                answer_callback_query(callback_query_id, "")
                send_message(
                    user_id,
                    f"📎 <b>Second Offer</b>\n\n"
                    f"Send your offer URL with clickid parameter.\n\n"
                    f"(You can customize this offer in the code)"
                )
                users_collection.update_one({'_id': user_id}, {'$set': {'current_mode': 'offer_mode'}})
            
            # Help & Support
            elif callback_data == 'help':
                answer_callback_query(callback_query_id, "")
                can_send, error_msg = can_send_help_request(user_id)
                if not can_send:
                    send_message(user_id, f"⏳ {error_msg}\n\n{CHANNEL_ID}")
                else:
                    send_message(
                        user_id,
                        f"💬 <b>Help & Support</b>\n\n"
                        f"Send your question or issue below:\n\n"
                        f"<b>Note:</b> Maximum 2 messages per day\n"
                        f"Your message will be sent directly to our support team."
                    )
                    users_collection.update_one({'_id': user_id}, {'$set': {'current_mode': 'help_mode'}})
            
            # Join Channel
            elif callback_data == 'join_channel':
                answer_callback_query(callback_query_id, "")
                send_message(
                    user_id,
                    f"📢 <b>Join Our Channels</b>\n\n"
                    f"Please join <b>BOTH</b> channels to access all features:",
                    reply_markup=join_channels_keyboard()
                )
            
            # Check channel membership
            elif callback_data == 'check_membership':
                answer_callback_query(callback_query_id, "")
                is_member, _ = check_channel_membership(user_id)
                if is_member:
                    send_message(user_id, "✅ <b>Great!</b> You've joined both channels.\n\nNow you can access all features.")
                    keyboard = home_keyboard_admin() if user_id == ADMIN_ID else home_keyboard()
                    send_message(user_id, "🏠 Select an option:", reply_markup=keyboard)
                else:
                    send_message(
                        user_id,
                        f"❌ You need to join <b>BOTH</b> channels:\n\n"
                        f"1️⃣ {CHANNEL_1_NAME}\n"
                        f"2️⃣ {CHANNEL_2_NAME}\n\n"
                        f"After joining both, click Check Membership again.",
                        reply_markup=join_channels_keyboard()
                    )
            
            # Admin Panel
            elif callback_data == 'admin_panel':
                if user_id != ADMIN_ID:
                    answer_callback_query(callback_query_id, "❌ You don't have access!", show_alert=True)
                    return 'ok', 200
                
                answer_callback_query(callback_query_id, "")
                send_message(
                    user_id,
                    "🔧 <b>Admin Panel</b>\n\nSelect an option:",
                    reply_markup=admin_keyboard()
                )
            
            # Admin - Stats
            elif callback_data == 'admin_stats':
                if user_id != ADMIN_ID:
                    return 'ok', 200
                
                answer_callback_query(callback_query_id, "")
                total_users = get_total_users()
                banned_users = get_banned_users_count()
                
                send_message(
                    user_id,
                    f"📊 <b>Bot Statistics</b>\n\n"
                    f"👥 <b>Total Active Users:</b> <code>{total_users}</code>\n"
                    f"🚫 <b>Banned Users:</b> <code>{banned_users}</code>\n"
                    f"📅 <b>Total Users (All):</b> <code>{users_collection.count_documents({})}</code>\n"
                    f"⏰ <b>Checked At:</b> {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC",
                    reply_markup=admin_keyboard()
                )
            
            # Admin - Broadcast
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
            
            # Admin - Ban User
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
            
            # Admin - Unban User
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
            
            # Admin - Recent Joins
            elif callback_data == 'admin_recent_joins':
                if user_id != ADMIN_ID:
                    return 'ok', 200
                
                answer_callback_query(callback_query_id, "")
                recent_users = get_recent_joined_users(20)
                
                if recent_users:
                    text = "<b>👥 Recent Joined Users (Last 20)</b>\n\n"
                    for i, user_info in enumerate(recent_users, 1):
                        joined_time = user_info.get('joined_bot_at', user_info.get('created_at'))
                        text += (f"<b>{i}. {user_info['first_name']}</b>\n"
                                f"   <b>Username:</b> @{user_info['username']}\n"
                                f"   <b>User ID:</b> <code>{user_info['_id']}</code>\n"
                                f"   <b>Joined:</b> {joined_time.strftime('%Y-%m-%d %H:%M:%S')} UTC\n\n")
                    send_message(user_id, text[:4000], reply_markup=admin_keyboard())
                else:
                    send_message(user_id, "📭 No users yet.", reply_markup=admin_keyboard())
            
            # Admin - Reply to Help Requests
            elif callback_data == 'admin_reply_mode':
                if user_id != ADMIN_ID:
                    return 'ok', 200
                
                answer_callback_query(callback_query_id, "")
                pending = get_pending_help_requests()
                
                if pending:
                    text = "<b>📬 Pending Help Requests</b>\n\n"
                    text += "Copy the <b>ID</b> and send reply like:\n<code>ID|Your Reply</code>\n\n"
                    for i, req in enumerate(pending[:10], 1):
                        text += (f"<b>ID:</b> <code>{str(req['_id'])}</code>\n"
                                f"<b>From:</b> @{req['username']} (ID: {req['user_id']})\n"
                                f"<b>Message:</b> {req['message'][:80]}\n\n")
                    send_message(user_id, text[:4000])
                    users_collection.update_one({'_id': user_id}, {'$set': {'current_mode': 'admin_reply_mode'}})
                else:
                    send_message(user_id, "📭 No pending help requests.", reply_markup=admin_keyboard())
            
            # Admin - Help Requests
            elif callback_data == 'admin_help_requests':
                if user_id != ADMIN_ID:
                    return 'ok', 200
                
                answer_callback_query(callback_query_id, "")
                help_requests = list(help_requests_collection.find().sort('created_at', -1).limit(10))
                
                if help_requests:
                    text = "<b>📋 Recent Help Requests (Last 10)</b>\n\n"
                    for i, req in enumerate(help_requests, 1):
                        text += (f"<b>{i}. From:</b> {req['username']} (ID: <code>{req['user_id']}</code>)\n"
                                f"   <b>Message:</b> {req['message'][:100]}{'...' if len(req['message']) > 100 else ''}\n"
                                f"   <b>Time:</b> {req['created_at'].strftime('%Y-%m-%d %H:%M:%S')} UTC\n\n")
                    send_message(user_id, text[:4000], reply_markup=admin_keyboard())  # Telegram 4096 char limit
                else:
                    send_message(user_id, "📭 No help requests yet.", reply_markup=admin_keyboard())
        
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
        'bot_name': 'Telegram Offer Bot',
        'version': '1.0.0',
        'status': 'running',
        'admin_id': ADMIN_ID,
        'channel': CHANNEL_ID
    }, 200

if __name__ == '__main__':
    # For local testing
    port = int(os.getenv('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)
