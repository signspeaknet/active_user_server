# SignSpeak User Presence Server

A Python Flask server with Socket.IO for real-time user presence tracking in the SignSpeak learning platform.

## Features

- Real-time user presence tracking using WebSockets
- MySQL database integration for persistent user sessions
- RESTful API endpoints for user management
- Automatic cleanup of inactive users
- CORS support for cross-origin requests
- Health check endpoint for monitoring
- **Privacy-friendly**: No IP addresses or user agents collected

## API Endpoints

### WebSocket Events
- `user_login` - Register a user as active
- `user_activity` - Update user activity timestamp
- `active_users_update` - Broadcast active user count

### HTTP Endpoints
- `GET /` - Server information
- `GET /health` - Health check
- `GET /api/active-users` - Get current active users
- `POST /api/user-presence` - Update user presence
- `GET /api/stats` - Get server statistics

## Environment Variables

Set these in your Render dashboard:

```
FLASK_ENV=production
SECRET_KEY=your-secret-key
DB_HOST=your-database-host
DB_USER=your-database-username
DB_PASSWORD=your-database-password
DB_NAME=sslocal
DB_PORT=3306
```

## Database Setup

Run the SQL from `database_setup.sql` to create the required table.

## Deployment

1. Upload this folder to GitHub
2. Connect your GitHub repository to Render
3. Set the environment variables in Render dashboard
4. Deploy!

## Local Development

```bash
pip install -r requirements.txt
python app.py
```

The server will run on `http://localhost:5000`
