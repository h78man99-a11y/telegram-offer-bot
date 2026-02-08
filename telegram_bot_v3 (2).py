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
    """Extract clickid from URL - looks for ?clickid= or any parameter after ?"""
    try:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        
        # First try clickid
        if 'clickid' in params:
            return params['clickid'][0]
        
        # If no clickid, get the first parameter value
        if params:
            first_key = list(params.keys())[0]
            return params[first_key][0]
        
        return None
    except:
        return None

def validate_url_format(user_url, starting_link):
    """Validate if user URL is a valid URL (removed strict format checking)"""
    try:
        result = urlparse(user_url)
        # Just check if it's a valid URL with scheme and netloc
        return bool(result.scheme and result.netloc)
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
    """Run postbacks sequentially with delays - supports any variable name"""
    postback_responses = []
    all_success = True
    total_time = 0
    
    for i, (postback_url, delay) in enumerate(zip(postbacks, delays)):
        # Replace $clickid or any $variable with the extracted value
        # This allows custom variables to be used
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
    """Send a message to user/chat"""
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
            chat_id = message['chat']['id']
            user_id = message['from']['id']
            username = message['from'].get('username', '')
            first_name = message['from'].get('first_name', 'User')
            text = message.get('text', '').strip()
            
            if is_user_banned(user_id):
                return 'ok', 200
            
            user, is_new_user = get_or_create_user(user_id, username, first_name)
            
            # Handle /start command
            if text == '/start':
                keyboard = home_keyboard_admin() if user_id == ADMIN_ID else home_keyboard()
                send_message(
                    chat_id,
                    f"👋 Welcome <b>{first_name}!</b>\n\n"
                    "Please join our channels to use all features.\n\n"
                    "Select an option below:",
                    reply_markup=keyboard
                )
            
            # Handle help mode
            elif user.get('current_mode') == 'help_mode' and text:
                can_send, error_msg = can_send_help_request(user_id)
                if not can_send:
                    send_message(chat_id, error_msg)
                else:
                    add_help_request(user_id, username, text)
                    send_message(
                        ADMIN_ID,
                        f"<b>📬 New Help Request</b>\n\n"
                        f"<b>From:</b> {first_name} (@{username or 'no_username'})\n"
                        f"<b>User ID:</b> <code>{user_id}</code>\n"
                        f"<b>Message:</b> {text}\n"
                        f"<b>Time:</b> {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC",
                    )
                    send_message(chat_id, "✅ Your message has been sent to support. We'll help you soon!")
                    users_collection.update_one({'_id': user_id}, {'$set': {'current_mode': None}})
            
            # Handle offer mode
            elif user.get('current_mode') == 'offer_mode' and text:
                offer_id = user.get('current_offer_id')
                offer = get_offer(offer_id)
                
                if not offer:
                    send_message(chat_id, "❌ Offer not found")
                    users_collection.update_one({'_id': user_id}, {'$set': {'current_mode': None}})
                    return 'ok', 200
                
                # Validate URL format (just check if it's a valid URL)
                if not validate_url_format(text, offer['starting_link']):
                    send_message(
                        chat_id,
                        f"❌ Invalid URL!\n\n"
                        f"Please send a valid URL starting with http:// or https://\n\n"
                        f"<b>Example:</b> <code>https://example.com?clickid=abc123</code>"
                    )
                    return 'ok', 200
                
                # Extract clickid or any parameter
                clickid = extract_clickid_from_url(text)
                if not clickid:
                    send_message(
                        chat_id,
                        f"❌ Could not extract variable from URL!\n\n"
                        f"Your URL must have at least one parameter.\n\n"
                        f"<b>Example:</b> <code>https://example.com?clickid=abc123</code>\n"
                        f"or: <code>https://example.com?tid=xyz789</code>"
                    )
                    return 'ok', 200
                
                # Show processing message
                send_message(chat_id, f"⏳ <b>Processing {len(offer['postbacks'])} postbacks...</b>")
                
                # Run postbacks
                postback_responses, all_success, total_time = run_postbacks_sequence(
                    clickid, 
                    offer['postbacks'], 
                    offer['delays'], 
                    chat_id
                )
                
                # Save submission
                save_submission(
                    user_id, username, offer_id, text, clickid,
                    postback_responses, all_success, total_time
                )
                
                # Show final summary
                send_message(
                    chat_id,
                    f"<b>✅ Complete!</b>\n\n"
                    f"<b>Offer:</b> {offer['name']}\n"
                    f"<b>Postbacks:</b> {len(postback_responses)}\n"
                    f"<b>Status:</b> {'✅ All Success' if all_success else '⚠️ Some Failed'}\n"
                    f"<b>Total Time:</b> {total_time // 1000} seconds"
                )
                
                users_collection.update_one({'_id': user_id}, {'$set': {'current_mode': None}})
                send_message(chat_id, "🏠 Select an option:", reply_markup=home_keyboard())
            
            # Handle broadcast mode
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
                    chat_id,
                    f"✅ <b>Broadcast Complete</b>\n\n"
                    f"<b>Sent to:</b> {success} users\n"
                    f"<b>Failed:</b> {failed} users",
                    reply_markup=admin_keyboard()
                )
                
                users_collection.update_one({'_id': user_id}, {'$set': {'current_mode': None}})
            
            # Handle ban mode
            elif user.get('current_mode') == 'ban_mode' and user_id == ADMIN_ID and text:
                try:
                    target_user_id = int(text)
                    if ban_user(target_user_id):
                        send_message(chat_id, f"✅ User <code>{target_user_id}</code> has been banned!", reply_markup=admin_keyboard())
                    else:
                        send_message(chat_id, f"⚠️ User <code>{target_user_id}</code> is already banned!", reply_markup=admin_keyboard())
                except ValueError:
                    send_message(chat_id, "❌ Invalid user ID. Please send only numbers.", reply_markup=admin_keyboard())
                
                users_collection.update_one({'_id': user_id}, {'$set': {'current_mode': None}})
            
            # Handle unban mode
            elif user.get('current_mode') == 'unban_mode' and user_id == ADMIN_ID and text:
                try:
                    target_user_id = int(text)
                    if unban_user(target_user_id):
                        send_message(chat_id, f"✅ User <code>{target_user_id}</code> has been unbanned!", reply_markup=admin_keyboard())
                    else:
                        send_message(chat_id, f"⚠️ User <code>{target_user_id}</code> is not banned!", reply_markup=admin_keyboard())
                except ValueError:
                    send_message(chat_id, "❌ Invalid user ID. Please send only numbers.", reply_markup=admin_keyboard())
                
                users_collection.update_one({'_id': user_id}, {'$set': {'current_mode': None}})
            
            # Handle admin reply mode
            elif user.get('current_mode') == 'admin_reply_mode' and user_id == ADMIN_ID and text:
                try:
                    if '|' in text:
                        request_id_str, reply_text = text.split('|', 1)
                        request_id_str = request_id_str.strip()
                        reply_text = reply_text.strip()
                        
                        try:
                            request_id = ObjectId(request_id_str)
                            success, message = reply_to_help_request(request_id, reply_text)
                            
                            if success:
                                send_message(chat_id, f"✅ {message}", reply_markup=admin_keyboard())
                            else:
                                send_message(chat_id, f"❌ Error: {message}", reply_markup=admin_keyboard())
                        except:
                            send_message(chat_id, f"❌ Invalid request ID format", reply_markup=admin_keyboard())
                    else:
                        send_message(chat_id, "❌ Invalid format. Use: <code>REQUEST_ID|Your Reply</code>", reply_markup=admin_keyboard())
                except Exception as e:
                    send_message(chat_id, f"❌ Error: {str(e)}", reply_markup=admin_keyboard())
                
                users_collection.update_one({'_id': user_id}, {'$set': {'current_mode': None}})
            
            # Handle offer delete mode
            elif user.get('current_mode') == 'offer_delete_mode' and user_id == ADMIN_ID and text:
                try:
                    offer_id = text.strip()
                    success, message = delete_offer(offer_id)
                    send_message(chat_id, message, reply_markup=admin_keyboard())
                except Exception as e:
                    send_message(chat_id, f"❌ Error: {str(e)}", reply_markup=admin_keyboard())
                
                users_collection.update_one({'_id': user_id}, {'$set': {'current_mode': None}})
            
            # Handle offer edit mode
            elif user.get('current_mode') == 'offer_edit_mode' and user_id == ADMIN_ID and text:
                try:
                    parts = text.split('|')
                    if len(parts) < 4:
                        send_message(chat_id, "❌ Invalid format. Use: OfferID|NewName|NewStartLink|NewPB1|...|NewD1|...")
                        return 'ok', 200
                    
                    offer_id = parts[0].strip()
                    name = parts[1].strip()
                    starting_link = parts[2].strip()
                    
                    # Find postbacks and delays
                    remaining = len(parts) - 3
                    pb_count = remaining // 2
                    
                    if pb_count < 1 or pb_count > 5:
                        send_message(chat_id, "❌ Must have 1-5 postbacks")
                        return 'ok', 200
                    
                    postbacks = [parts[i+3].strip() for i in range(pb_count)]
                    delays = [int(parts[i + pb_count + 3].strip()) for i in range(pb_count)]
                    
                    updates = {
                        'name': name,
                        'starting_link': starting_link,
                        'postback_count': pb_count,
                        'postbacks': postbacks,
                        'delays': delays
                    }
                    
                    success, message = edit_offer(offer_id, updates)
                    send_message(chat_id, message, reply_markup=admin_keyboard())
                    
                except Exception as e:
                    send_message(chat_id, f"❌ Error: {str(e)}", reply_markup=admin_keyboard())
                
                users_collection.update_one({'_id': user_id}, {'$set': {'current_mode': None}})
            
            # Handle offer creation mode
            elif user.get('current_mode') == 'offer_create_mode' and user_id == ADMIN_ID and text:
                try:
                    parts = text.split('|')
                    if len(parts) < 4:
                        send_message(chat_id, "❌ Invalid format. Use: Name|StartLink|PB1|PB2|...|D1|D2|...")
                        return 'ok', 200
                    
                    name = parts[0].strip()
                    starting_link = parts[1].strip()
                    
                    # Find postbacks and delays
                    pb_count = None
                    for i in range(1, 6):
                        if len(parts) > i + 1 and len(parts) > i + 5:  # Enough for postbacks and delays
                            if i == len(parts) - 5:  # Found the split point
                                pb_count = i
                                break
                    
                    if not pb_count:
                        # Try to determine automatically
                        remaining = len(parts) - 2
                        pb_count = remaining // 2
                    
                    if pb_count < 1 or pb_count > 5:
                        send_message(chat_id, "❌ Must have 1-5 postbacks")
                        return 'ok', 200
                    
                    postbacks = [parts[i+2].strip() for i in range(pb_count)]
                    delays = [int(parts[i + pb_count + 2].strip()) for i in range(pb_count)]
                    
                    success, message = create_offer(name, starting_link, postbacks, delays, user_id)
                    send_message(chat_id, message, reply_markup=admin_keyboard())
                    
                except Exception as e:
                    send_message(chat_id, f"❌ Error: {str(e)}", reply_markup=admin_keyboard())
                
                users_collection.update_one({'_id': user_id}, {'$set': {'current_mode': None}})
        
        # Handle callback queries
        elif 'callback_query' in update:
            callback = update['callback_query']
            user_id = callback['from']['id']
            username = callback['from'].get('username', '')
            first_name = callback['from'].get('first_name', 'User')
            callback_data = callback['data']
            callback_query_id = callback['id']
            chat_id = callback['message']['chat']['id']
            
            if is_user_banned(user_id):
                return 'ok', 200
            
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
            
            # Home
            if callback_data == 'home':
                answer_callback_query(callback_query_id, "")
                keyboard = home_keyboard_admin() if user_id == ADMIN_ID else home_keyboard()
                send_message(user_id, "🏠 <b>Home Menu</b>\n\nSelect an option:", reply_markup=keyboard)
            
            # Offers
            elif callback_data == 'offers':
                answer_callback_query(callback_query_id, "")
                send_message(user_id, "🎁 <b>Select an Offer</b>", reply_markup=offer_keyboard())
            
            # Specific offer selected
            elif callback_data.startswith('offer_'):
                answer_callback_query(callback_query_id, "")
                offer_id = callback_data.replace('offer_', '')
                offer = get_offer(offer_id)
                
                if not offer:
                    send_message(user_id, "❌ Offer not found")
                    return 'ok', 200
                
                send_message(
                    user_id,
                    f"🎁 <b>{offer['name']}</b>\n\n"
                    f"Send any URL with at least one parameter.\n\n"
                    f"<b>Example:</b> <code>https://example.com?clickid=YOUR_ID</code>\n"
                    f"or: <code>https://example.com?tid=abc123</code>\n\n"
                    f"<b>Postbacks:</b> {offer['postback_count']}\n"
                    f"<b>Delays:</b> {', '.join(str(d) + 's' for d in offer['delays'])}\n\n"
                    f"✅ The extracted variable will be sent to all postbacks."
                )
                users_collection.update_one({'_id': user_id}, {'$set': {'current_mode': 'offer_mode', 'current_offer_id': offer_id}})
            
            # Help
            elif callback_data == 'help':
                answer_callback_query(callback_query_id, "")
                can_send, error_msg = can_send_help_request(user_id)
                if not can_send:
                    send_message(user_id, f"⏳ {error_msg}")
                else:
                    send_message(
                        user_id,
                        f"💬 <b>Help & Support</b>\n\n"
                        f"Send your question or issue below:\n\n"
                        f"<b>Note:</b> Maximum 2 messages per day\n"
                        f"Your message will be sent directly to our support team."
                    )
                    users_collection.update_one({'_id': user_id}, {'$set': {'current_mode': 'help_mode'}})
            
            # Join channels
            elif callback_data == 'join_channel':
                answer_callback_query(callback_query_id, "")
                send_message(
                    user_id,
                    f"📢 <b>Join Our Channels</b>\n\n"
                    f"Please join <b>BOTH</b> channels to access all features:",
                    reply_markup=join_channels_keyboard()
                )
            
            # Check membership
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
            
            # Admin panel
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
            
            # Admin stats
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
            
            # Admin recent joins
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
            
            # Admin help requests
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
                    send_message(user_id, text[:4000], reply_markup=admin_keyboard())
                else:
                    send_message(user_id, "📭 No help requests yet.", reply_markup=admin_keyboard())
            
            # Admin reply mode
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
        'version': '3.0.0',
        'status': 'running',
        'features': ['2-Channel Verification', '1-5 Postbacks', 'Analytics', 'Admin Controls'],
        'admin_id': ADMIN_ID,
        'channels': [CHANNEL_1_NAME, CHANNEL_2_NAME]
    }, 200

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)
