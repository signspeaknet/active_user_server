from flask import Flask, request, jsonify
from flask_socketio import SocketIO, emit
from flask_cors import CORS
import mysql.connector
from datetime import datetime, timedelta
import json
import os
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

def cleanup_inactive_users():
    """Remove users inactive for more than 5 minutes"""
    with users_lock:
        current_time = datetime.now()
        inactive_users = []
        
        for user_id, user_data in active_users.items():
            if current_time - user_data['last_seen'] > timedelta(seconds=10):
                inactive_users.append(user_id)
        
        for user_id in inactive_users:
            del active_users[user_id]
        
        # If users were removed, broadcast update
        if len(inactive_users) > 0:
            non_admin_count = sum(1 for user in active_users.values() if not user.get('is_admin', False))
            socketio.emit('active_users_update', {
                'count': non_admin_count,
                'users': [user for user in active_users.values() if not user.get('is_admin', False)]
            })
        
        return len(inactive_users) > 0

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
                'users': [user for user in active_users.values() if not user.get('is_admin', False)]
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
            'users': [user for user in active_users.values() if not user.get('is_admin', False)]
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
            'users': non_admin_users
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
                
                query = """
                INSERT INTO user_sessions (user_id, last_activity, session_data, ip_address, user_agent) 
                VALUES (%s, NOW(), %s, %s, %s) 
                ON DUPLICATE KEY UPDATE 
                last_activity = NOW(), session_data = %s, ip_address = %s, user_agent = %s
                """
                
                cursor.execute(query, (
                    user_id, session_data, request.remote_addr, request.headers.get('User-Agent'),
                    session_data, request.remote_addr, request.headers.get('User-Agent')
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
        all_users = list(active_users.values())
        non_admin_users = [user for user in active_users.values() if not user.get('is_admin', False)]
        return jsonify({
            'total_users': len(all_users),
            'non_admin_users': len(non_admin_users),
            'all_users': all_users,
            'non_admin_users': non_admin_users
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
            import time
            time.sleep(3)
            cleanup_inactive_users()
            # Count only non-admin users
            non_admin_count = sum(1 for user in active_users.values() if not user.get('is_admin', False))
            socketio.emit('active_users_update', {
                'count': non_admin_count,
                'users': [user for user in active_users.values() if not user.get('is_admin', False)]
            })
    
    cleanup_thread = threading.Thread(target=cleanup_loop, daemon=True)
    cleanup_thread.start()
    
    # Run the app with production settings
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port, debug=False, allow_unsafe_werkzeug=True)
