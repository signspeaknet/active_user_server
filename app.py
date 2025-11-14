from flask import Flask, request, jsonify
from flask_socketio import SocketIO, emit
from flask_cors import CORS
import mysql.connector
from datetime import datetime, timedelta
import json
import os
import time
from threading import Lock

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'your-secret-key-here')
socketio = SocketIO(app, cors_allowed_origins="*")
CORS(app)

# Database configuration
db_config = {
    'host': os.environ.get('DB_HOST'),
    'user': os.environ.get('DB_USER'),
    'password': os.environ.get('DB_PASSWORD'),
    'database': os.environ.get('DB_NAME'),
    'port': int(os.environ.get('DB_PORT', 3306))
}

# Presence configuration
PRESENCE_INACTIVE_SECONDS = int(os.environ.get('PRESENCE_INACTIVE_SECONDS', 300))  # 5 minutes
PRESENCE_ROLLUP_SECONDS = int(os.environ.get('PRESENCE_ROLLUP_SECONDS', 60))       # 1 minute
PRESENCE_RETENTION_DAYS = int(os.environ.get('PRESENCE_RETENTION_DAYS', 90))       # retain 90 days of minute data

# Thread-safe storage for active users
active_users = {}
users_lock = Lock()

def get_db_connection():
    """Get database connection"""
    try:
        return mysql.connector.connect(**db_config)
    except mysql.connector.Error as err:
        print(f"Database connection error: {err}")
        return None

def check_if_admin(user_id):
    """Check if user is admin by querying database"""
    conn = get_db_connection()
    if conn:
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM admin_users WHERE admin_id = %s", (user_id,))
            is_admin = cursor.fetchone()[0] > 0
            cursor.close()
            return is_admin
        except mysql.connector.Error as err:
            print(f"Database error checking admin: {err}")
            return False
        finally:
            conn.close()
    return False

def serialize_user(user_data):
    """Convert user record to JSON-serializable dict"""
    return {
        'socket_id': user_data.get('socket_id'),
        'last_seen': user_data.get('last_seen').isoformat() if isinstance(user_data.get('last_seen'), datetime) else user_data.get('last_seen'),
        'user_info': user_data.get('user_info', {}),
        'user_id': user_data.get('user_id'),
        'is_admin': user_data.get('is_admin', False)
    }

def cleanup_inactive_users():
    """Remove users inactive for more than 5 minutes"""
    with users_lock:
        current_time = datetime.now()
        inactive_users = []
        
        for user_id, user_data in active_users.items():
            if current_time - user_data['last_seen'] > timedelta(seconds=PRESENCE_INACTIVE_SECONDS):
                inactive_users.append(user_id)
        
        for user_id in inactive_users:
            del active_users[user_id]
        
        # If users were removed, broadcast update
        if len(inactive_users) > 0:
            non_admin_count = sum(1 for user in active_users.values() if not user.get('is_admin', False))
            socketio.emit('active_users_update', {
                'count': non_admin_count,
                'users': [serialize_user(user) for user in active_users.values() if not user.get('is_admin', False)]
            })
        
        return len(inactive_users) > 0

def record_minute_presence():
    """Record the set of currently active users into user_presence_minutely for the current minute bucket."""
    bucket_minute = datetime.now().replace(second=0, microsecond=0)
    # Snapshot active user ids that are still within the inactive threshold
    with users_lock:
        now = datetime.now()
        active_user_ids = [
            user_id for user_id, user_data in active_users.items()
            if (now - user_data['last_seen']) <= timedelta(seconds=PRESENCE_INACTIVE_SECONDS)
        ]

    if not active_user_ids:
        return 0

    conn = get_db_connection()
    if not conn:
        return 0

    inserted = 0
    try:
        cursor = conn.cursor()
        for user_id in active_user_ids:
            # Use INSERT IGNORE to avoid duplicate key errors for (bucket_minute, user_id)
            cursor.execute(
                """
                INSERT IGNORE INTO user_presence_minutely (bucket_minute, user_id)
                VALUES (%s, %s)
                """,
                (bucket_minute, user_id)
            )
            inserted += cursor.rowcount if cursor.rowcount is not None else 0
        conn.commit()
        cursor.close()
    except mysql.connector.Error as err:
        print(f"Database error in record_minute_presence: {err}")
    finally:
        conn.close()

    return inserted

def presence_rollup_loop():
    """Background loop that records presence once per minute (or per configured cadence)."""
    while True:
        if PRESENCE_ROLLUP_SECONDS == 60:
            now = datetime.now()
            next_minute = (now.replace(second=0, microsecond=0) + timedelta(minutes=1))
            sleep_seconds = max(0.0, (next_minute - now).total_seconds())
            time.sleep(sleep_seconds)
        else:
            time.sleep(max(1, PRESENCE_ROLLUP_SECONDS))

        try:
            record_minute_presence()
        except Exception as e:
            # Guard the loop against any unexpected exception
            print(f"Error in presence_rollup_loop: {e}")

def cleanup_old_presence_data():
    """Delete old rows from user_presence_minutely beyond retention window."""
    if PRESENCE_RETENTION_DAYS <= 0:
        return 0
    conn = get_db_connection()
    if not conn:
        return 0
    deleted = 0
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            DELETE FROM user_presence_minutely
            WHERE bucket_minute < (NOW() - INTERVAL %s DAY)
            """,
            (PRESENCE_RETENTION_DAYS,)
        )
        deleted = cursor.rowcount if cursor.rowcount is not None else 0
        conn.commit()
        cursor.close()
    except mysql.connector.Error as err:
        print(f"Database error in cleanup_old_presence_data: {err}")
    finally:
        conn.close()
    return deleted

def presence_retention_loop():
    """Background loop to periodically prune old presence rows (hourly)."""
    while True:
        try:
            cleanup_old_presence_data()
        except Exception as e:
            print(f"Error in presence_retention_loop: {e}")
        # Run hourly
        time.sleep(3600)

@socketio.on('connect')
def handle_connect():
    print(f'User connected: {request.sid}')

@socketio.on('disconnect')
def handle_disconnect():
    print(f'User disconnected: {request.sid}')
    
    # Remove user from active users
    with users_lock:
        user_to_remove = None
        for user_id, user_data in active_users.items():
            if user_data['socket_id'] == request.sid:
                user_to_remove = user_id
                break
        
        if user_to_remove:
            del active_users[user_to_remove]
            # Count only non-admin users
            non_admin_count = sum(1 for user in active_users.values() if not user.get('is_admin', False))
            emit('active_users_update', {
            'count': non_admin_count,
            'users': [serialize_user(user) for user in active_users.values() if not user.get('is_admin', False)]
            }, broadcast=True)

@socketio.on('user_login')
def handle_user_login(data):
    """Handle user login event"""
    user_id = data.get('user_id')
    user_info = data.get('user_info', {})
    
    if user_id:
        # Check if user is admin (exclude from active count)
        is_admin = check_if_admin(user_id)
        
        with users_lock:
            active_users[user_id] = {
                'socket_id': request.sid,
                'last_seen': datetime.now(),
                'user_info': user_info,
                'user_id': user_id,
                'is_admin': is_admin
            }
        
        # Count only non-admin users
        non_admin_count = sum(1 for user in active_users.values() if not user.get('is_admin', False))
        
        # Broadcast updated user count (excluding admins)
        emit('active_users_update', {
            'count': non_admin_count,
            'users': [serialize_user(user) for user in active_users.values() if not user.get('is_admin', False)]
        }, broadcast=True)

@socketio.on('user_activity')
def handle_user_activity(data):
    """Handle user activity update"""
    user_id = data.get('user_id')
    
    if user_id and user_id in active_users:
        with users_lock:
            active_users[user_id]['last_seen'] = datetime.now()

# API Routes
@app.route('/api/active-users', methods=['GET'])
def get_active_users():
    """Get current active users (excluding admins)"""
    with users_lock:
        non_admin_users = [user for user in active_users.values() if not user.get('is_admin', False)]
        return jsonify({
            'count': len(non_admin_users),
            'users': [serialize_user(user) for user in non_admin_users]
        })

@app.route('/api/user-presence', methods=['POST'])
def update_user_presence():
    """Update user presence from PHP app"""
    data = request.get_json()
    user_id = data.get('user_id')
    user_info = data.get('user_info', {})
    page = data.get('page', 'unknown')
    action = data.get('action', 'browsing')
    
    if user_id:
        # Update database
        conn = get_db_connection()
        if conn:
            try:
                cursor = conn.cursor()
                session_data = json.dumps({
                    'page': page,
                    'action': action,
                    'timestamp': datetime.now().isoformat()
                })
                
                # Privacy-friendly: Only store user_id, timestamp, and minimal session data
                # No IP address or user agent collected
                query = """
                INSERT INTO user_sessions (user_id, last_activity, session_data) 
                VALUES (%s, NOW(), %s) 
                ON DUPLICATE KEY UPDATE 
                last_activity = NOW(), session_data = %s
                """
                
                cursor.execute(query, (
                    user_id, session_data, session_data
                ))
                conn.commit()
                cursor.close()
                
            except mysql.connector.Error as err:
                print(f"Database error: {err}")
            finally:
                conn.close()
        
        # Update active users
        with users_lock:
            is_admin = check_if_admin(user_id)
            if user_id in active_users:
                active_users[user_id]['last_seen'] = datetime.now()
            else:
                active_users[user_id] = {
                    'socket_id': None,  # No socket connection from API
                    'last_seen': datetime.now(),
                    'user_info': user_info,
                    'user_id': user_id,
                    'is_admin': is_admin
                }
        
        return jsonify({'success': True, 'user_id': user_id})
    
    return jsonify({'success': False, 'error': 'Invalid user_id'})

@app.route('/api/stats', methods=['GET'])
def get_stats():
    """Get server statistics"""
    conn = get_db_connection()
    # Count only non-admin users
    non_admin_count = sum(1 for user in active_users.values() if not user.get('is_admin', False))
    
    stats = {
        'active_users': non_admin_count,
        'server_time': datetime.now().isoformat(),
        'uptime': 'N/A'
    }
    
    if conn:
        try:
            cursor = conn.cursor()
            
            # Get total users
            cursor.execute("SELECT COUNT(*) FROM users")
            stats['total_users'] = cursor.fetchone()[0]
            
            # Get users active in last hour
            cursor.execute("""
                SELECT COUNT(DISTINCT user_id) 
                FROM user_sessions 
                WHERE last_activity >= DATE_SUB(NOW(), INTERVAL 1 HOUR)
            """)
            stats['users_last_hour'] = cursor.fetchone()[0]
            
            cursor.close()
        except mysql.connector.Error as err:
            print(f"Database error: {err}")
        finally:
            conn.close()
    
    return jsonify(stats)

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint for Render"""
    non_admin_count = sum(1 for user in active_users.values() if not user.get('is_admin', False))
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'active_users': non_admin_count
    })

@app.route('/debug', methods=['GET'])
def debug_info():
    """Debug endpoint to see all active users"""
    with users_lock:
        all_users = [serialize_user(user) for user in active_users.values()]
        non_admin_users = [serialize_user(user) for user in active_users.values() if not user.get('is_admin', False)]
        return jsonify({
            'total_users': len(all_users),
            'non_admin_users': len(non_admin_users),
            'all_users': all_users,
            'non_admin_users_list': non_admin_users
        })

@app.route('/', methods=['GET'])
def home():
    """Home endpoint with basic info"""
    return jsonify({
        'message': 'SignSpeak User Presence Server',
        'version': '1.0.0',
        'endpoints': {
            'health': '/health',
            'active_users': '/api/active-users',
            'user_presence': '/api/user-presence',
            'stats': '/api/stats',
            'debug': '/debug'
        }
    })

if __name__ == '__main__':
    # Cleanup inactive users every 2 minutes
    import threading
    def cleanup_loop():
        while True:
            time.sleep(3)
            cleanup_inactive_users()
            # Count only non-admin users
            non_admin_count = sum(1 for user in active_users.values() if not user.get('is_admin', False))
            socketio.emit('active_users_update', {
                'count': non_admin_count,
                'users': [serialize_user(user) for user in active_users.values() if not user.get('is_admin', False)]
            })
    
    cleanup_thread = threading.Thread(target=cleanup_loop, daemon=True)
    cleanup_thread.start()
    
    # Start presence minute-rollup thread
    presence_thread = threading.Thread(target=presence_rollup_loop, daemon=True)
    presence_thread.start()

    # Start presence retention cleanup thread
    retention_thread = threading.Thread(target=presence_retention_loop, daemon=True)
    retention_thread.start()
    
    # Run the app with production settings
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port, debug=False, allow_unsafe_werkzeug=True)
