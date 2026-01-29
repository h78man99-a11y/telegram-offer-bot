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

def get_recent_joined_users(limit=20):
    """Get list of recently joined users"""
    return list(users_collection.find({'is_active': True}).sort('created_at', -1).limit(limit))

# ==================== OFFER MANAGEMENT FUNCTIONS ====================

def create_offer(name, starting_link, postbacks, delays, admin_id):
    """Create a new offer with 1-5 postbacks"""
    if len(postbacks) < 1 or len(postbacks) > 5:
        return False, "❌ Must have 1-5 postbacks"
    
    if len(postbacks) != len(delays):
        return False, "❌ Number of postbacks must match delays"
    
    offer_id = offers_collection.insert_one({
        'name': name,
        'starting_link': starting_link,
        'postback_count': len(postbacks),
        'postbacks': postbacks,
        'delays': delays,
        'enabled': True,
        'created_by': admin_id,
        'created_at': datetime.utcnow(),
        'updated_at': datetime.utcnow(),
        'total_submissions': 0,
        'success_count': 0
    }).inserted_id
    
    return True, f"✅ Offer created! ID: {offer_id}"

def get_all_offers():
    """Get all offers"""
    return list(offers_collection.find())

def get_enabled_offers():
    """Get only enabled offers"""
    return list(offers_collection.find({'enabled': True}))

def get_offer(offer_id):
    """Get single offer"""
    return offers_collection.find_one({'_id': ObjectId(offer_id)})

def edit_offer(offer_id, updates):
    """Edit an offer"""
    try:
        updates['updated_at'] = datetime.utcnow()
        offers_collection.update_one(
            {'_id': ObjectId(offer_id)},
            {'$set': updates}
        )
        return True, "✅ Offer updated!"
    except Exception as e:
        return False, str(e)

def delete_offer(offer_id):
    """Delete an offer"""
    try:
        offers_collection.delete_one({'_id': ObjectId(offer_id)})
        return True, "✅ Offer deleted!"
    except Exception as e:
        return False, str(e)

def toggle_offer_status(offer_id):
    """Enable/disable an offer"""
    try:
        offer = offers_collection.find_one({'_id': ObjectId(offer_id)})
        if not offer:
            return False, "Offer not found"
        
        new_status = not offer.get('enabled', True)
        offers_collection.update_one(
            {'_id': ObjectId(offer_id)},
            {'$set': {'enabled': new_status}}
        )
        
        status_text = "enabled" if new_status else "disabled"
        return True, f"✅ Offer {status_text}!"
    except Exception as e:
        return False, str(e)

def save_submission(user_id, username, offer_id, url, clickid, postback_responses, success, total_time):
    """Save offer submission"""
    submission_id = submissions_collection.insert_one({
        'user_id': user_id,
        'username': username,
        'offer_id': ObjectId(offer_id),
        'submitted_url': url,
        'extracted_clickid': clickid,
        'postback_responses': postback_responses,
        'submitted_at': datetime.utcnow(),
        'completed_at': datetime.utcnow(),
        'total_execution_time_ms': total_time,
        'success': success,
        'postback_count': len(postback_responses)
    }).inserted_id
    
    # Update offer stats
    offers_collection.update_one(
        {'_id': ObjectId(offer_id)},
        {
            '$inc': {'total_submissions': 1, 'success_count': 1 if success else 0}
        }
    )
    
    return submission_id

def get_offer_submissions(offer_id):
    """Get all submissions for an offer"""
    return list(submissions_collection.find({'offer_id': ObjectId(offer_id)}).sort('submitted_at', -1).limit(100))

def get_offer_analytics(offer_id):
    """Get analytics for an offer"""
    submissions = get_offer_submissions(offer_id)
    total = len(submissions)
    success = sum(1 for s in submissions if s['success'])
    
    users = list(set([s['username'] for s in submissions]))
    first_submission = submissions[-1]['submitted_at'] if submissions else None
    last_submission = submissions[0]['submitted_at'] if submissions else None
    
    return {
        'total': total,
        'success': success,
        'success_rate': (success / total * 100) if total > 0 else 0,
        'users': users,
        'first_submission': first_submission,
        'last_submission': last_submission,
        'submissions': submissions
    }

# ==================== POSTBACK FUNCTIONS ====================

def extract_clickid_from_url(url):
    """Extract clickid from URL"""
    try:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        
        if 'clickid' in params:
            return params['clickid'][0]
        return None
    except:
        return None

def validate_url_format(user_url, starting_link):
    """Validate if user URL matches starting link pattern"""
    try:
        user_parsed = urlparse(user_url)
        pattern_parsed = urlparse(starting_link)
        
        return user_parsed.scheme == pattern_parsed.scheme and \
               user_parsed.netloc == pattern_parsed.netloc and \
               user_parsed.path == pattern_parsed.path
    except:
        return False

def send_postback(postback_url):
    """Send postback request and return response"""
    try:
        start_time = time.time()
        response = requests.get(postback_url, timeout=15)
        elapsed = int((time.time() - start_time) * 1000)  # milliseconds
        
        response_text = response.text
        if len(response_text) > 500:
            response_text = response_text[:500] + "..."
        
        return True, response_text, response.status_code, elapsed
    
    except requests.Timeout:
        return False, "⏱️ Request timeout (15 sec limit)", 0, 15000
    except requests.ConnectionError:
        return False, "❌ Connection error", 0, 0
    except Exception as e:
        return False, f"❌ Error: {str(e)[:100]}", 0, 0

def run_postbacks_sequence(clickid, postbacks, delays, user_id):
    """Run postbacks sequentially with delays"""
    postback_responses = []
    all_success = True
    total_time = 0
    
    for i, (postback_url, delay) in enumerate(zip(postbacks, delays)):
        # Replace $clickid variable
        final_url = postback_url.replace('$clickid', clickid)
        
        # Send postback
        success, response_text, status_code, elapsed = send_postback(final_url)
        total_time += elapsed + (delay * 1000)
        
        postback_responses.append({
            'postback_num': i + 1,
            'postback_url': final_url,
            'response': response_text,
            'status_code': status_code,
            'success': success,
            'completed_at': datetime.utcnow(),
            'execution_time_ms': elapsed
        })
        
        # Show response to user
        status_emoji = "✅" if success else "⚠️"
        send_message(
            user_id,
            f"<b>{status_emoji} Postback {i+1}/{len(postbacks)}</b>\n\n"
            f"<b>URL:</b> <code>{final_url[:80]}...</code>\n"
            f"<b>Status:</b> {status_code}\n"
            f"<b>Response:</b> <code>{response_text[:200]}</code>\n"
            f"<b>Time:</b> {elapsed}ms"
        )
        
        if not success:
            all_success = False
        
        # Wait before next postback
        if i < len(postbacks) - 1:
            wait_seconds = delay
            send_message(user_id, f"⏱️ Waiting {wait_seconds} seconds before next postback...")
            time.sleep(wait_seconds)
    
    return postback_responses, all_success, total_time

# ==================== MESSAGE FUNCTIONS ====================

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

def check_channel_membership(user_id):
    """Check if user is member of ALL required channels"""
    try:
        for channel in REQUIRED_CHANNELS:
            channel_name = channel.replace('@', '')
            url = f"{TELEGRAM_API}/getChatMember?chat_id=@{channel_name}&user_id={user_id}"
            response = requests.get(url, timeout=5)
            data = response.json()
            if data['ok']:
                status = data['result']['status']
                if status in ['left', 'kicked']:
                    return False, channel
            else:
                return False, channel
        return True, None
    except Exception as e:
        print(f"Channel check error: {e}")
        return False, None

# ==================== KEYBOARD FUNCTIONS ====================

def home_keyboard():
    """Return home keyboard"""
    return {
        'inline_keyboard': [
            [{'text': '🎁 Offers', 'callback_data': 'offers'}],
            [{'text': '💬 Help & Support', 'callback_data': 'help'}],
            [{'text': '📱 Join Channels', 'callback_data': 'join_channel'}]
        ]
    }

def home_keyboard_admin():
    """Return home keyboard for admin"""
    keyboard = home_keyboard()
    keyboard['inline_keyboard'].append([{'text': '👨‍💼 Admin Panel', 'callback_data': 'admin_panel'}])
    return keyboard

def offer_keyboard():
    """Return offer selection keyboard"""
    offers = get_enabled_offers()
    
    keyboard = {'inline_keyboard': []}
    for i, offer in enumerate(offers[:10]):  # Max 10 offers
        keyboard['inline_keyboard'].append([
            {'text': f"{i+1}️⃣ {offer['name']}", 'callback_data': f"offer_{offer['_id']}"}
        ])
    
    keyboard['inline_keyboard'].append([{'text': '⬅️ Back', 'callback_data': 'home'}])
    return keyboard

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
            [{'text': '🎁 Manage Offers', 'callback_data': 'admin_manage_offers'}],
            [{'text': '📊 Offer Analytics', 'callback_data': 'admin_offer_analytics'}],
            [{'text': '🚫 Ban User', 'callback_data': 'admin_ban'}],
            [{'text': '✅ Unban User', 'callback_data': 'admin_unban'}],
            [{'text': '⬅️ Back', 'callback_data': 'home'}]
        ]
    }

def manage_offers_keyboard():
    """Return manage offers menu"""
    return {
        'inline_keyboard': [
            [{'text': '➕ Create Offer', 'callback_data': 'offer_create'}],
            [{'text': '✏️ Edit Offer', 'callback_data': 'offer_edit'}],
            [{'text': '🗑️ Delete Offer', 'callback_data': 'offer_delete'}],
            [{'text': '📋 List Offers', 'callback_data': 'offer_list'}],
            [{'text': '⬅️ Back', 'callback_data': 'admin_panel'}]
        ]
    }

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
            
            if is_user_banned(user_id):
