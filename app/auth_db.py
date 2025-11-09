"""
Authentication database management for AudiobookBay
Handles user registration and authentication with SQLite
"""
import sqlite3
import os
import logging
from pathlib import Path
from werkzeug.security import generate_password_hash, check_password_hash

logger = logging.getLogger(__name__)

# Database path
AUTH_DB_PATH = os.path.join(os.path.dirname(__file__), 'users.db')

def init_auth_db():
    """Initialize the authentication database with users table"""
    try:
        conn = sqlite3.connect(AUTH_DB_PATH)
        cursor = conn.cursor()

        # Create users table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                user_type TEXT DEFAULT 'user',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_login TIMESTAMP
            )
        ''')

        # Create index on username for faster lookups
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_username ON users(username)
        ''')

        conn.commit()
        conn.close()
        logger.info(f"Authentication database initialized at {AUTH_DB_PATH}")
        return True
    except Exception as e:
        logger.error(f"Failed to initialize auth database: {e}")
        return False


def hash_password(password):
    """Hash a password using werkzeug"""
    return generate_password_hash(password)


def verify_password(password, password_hash):
    """Verify a password against its hash

    Args:
        password: Plain text password (str)
        password_hash: Hashed password (str)

    Returns:
        bool: True if password matches, False otherwise
    """
    try:
        return check_password_hash(password_hash, password)
    except Exception as e:
        logger.error(f"Password verification error: {e}")
        return False


def create_user(username, password, user_type='user'):
    """
    Create a new user in the database

    Args:
        username: Unique username
        password: Plain text password (will be hashed)
        user_type: 'user' or 'root' (admin)

    Returns:
        tuple: (success: bool, message: str)
    """
    if not username or not password:
        return False, "Username and password are required"

    if len(password) < 6:
        return False, "Password must be at least 6 characters"

    if len(username) < 3:
        return False, "Username must be at least 3 characters"

    try:
        conn = sqlite3.connect(AUTH_DB_PATH)
        cursor = conn.cursor()

        # Check if username already exists
        cursor.execute('SELECT id FROM users WHERE username = ?', (username,))
        if cursor.fetchone():
            conn.close()
            return False, "Username already exists"

        # Hash password and insert user
        password_hash = hash_password(password)
        cursor.execute(
            'INSERT INTO users (username, password_hash, user_type) VALUES (?, ?, ?)',
            (username, password_hash, user_type)
        )

        conn.commit()
        conn.close()
        logger.info(f"User created successfully: {username}")
        return True, "User created successfully"
    except sqlite3.IntegrityError:
        return False, "Username already exists"
    except Exception as e:
        logger.error(f"Error creating user: {e}")
        return False, "An error occurred during registration"


def authenticate_user(username, password):
    """
    Authenticate a user

    Args:
        username: Username
        password: Plain text password

    Returns:
        dict: User data if successful, None if authentication fails
        Example: {'id': 1, 'username': 'john', 'type': 'user'}
    """
    if not username or not password:
        return None

    try:
        conn = sqlite3.connect(AUTH_DB_PATH)
        cursor = conn.cursor()

        # Fetch user
        cursor.execute(
            'SELECT id, username, password_hash, user_type FROM users WHERE username = ?',
            (username,)
        )
        user = cursor.fetchone()

        if not user:
            conn.close()
            logger.debug(f"User not found: {username}")
            return None

        user_id, db_username, password_hash, user_type = user

        # Verify password
        if verify_password(password, password_hash):
            # Update last login
            cursor.execute(
                'UPDATE users SET last_login = CURRENT_TIMESTAMP WHERE id = ?',
                (user_id,)
            )
            conn.commit()
            conn.close()

            logger.info(f"User authenticated successfully: {username}")
            return {
                'id': user_id,
                'username': db_username,
                'type': user_type
            }

        conn.close()
        logger.debug(f"Invalid password for user: {username}")
        return None
    except Exception as e:
        logger.error(f"Authentication error: {e}")
        return None


def get_user_count():
    """Get total number of registered users"""
    try:
        conn = sqlite3.connect(AUTH_DB_PATH)
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM users')
        count = cursor.fetchone()[0]
        conn.close()
        return count
    except Exception as e:
        logger.error(f"Error getting user count: {e}")
        return 0


def user_exists(username):
    """Check if a username already exists"""
    try:
        conn = sqlite3.connect(AUTH_DB_PATH)
        cursor = conn.cursor()
        cursor.execute('SELECT id FROM users WHERE username = ?', (username,))
        exists = cursor.fetchone() is not None
        conn.close()
        return exists
    except Exception as e:
        logger.error(f"Error checking user existence: {e}")
        return False


def get_user_by_username(username):
    """
    Get user data by username for Flask-Login user_loader

    Args:
        username: Username to look up

    Returns:
        dict: User data if found, None otherwise
        Example: {'username': 'john', 'type': 'user'}
    """
    if not username:
        return None

    try:
        conn = sqlite3.connect(AUTH_DB_PATH)
        cursor = conn.cursor()

        cursor.execute(
            'SELECT id, username, user_type FROM users WHERE username = ?',
            (username,)
        )
        user = cursor.fetchone()
        conn.close()

        if user:
            user_id, db_username, user_type = user
            return {
                'id': user_id,
                'username': db_username,
                'type': user_type
            }

        return None
    except Exception as e:
        logger.error(f"Error getting user by username: {e}")
        return None


def is_admin_user(username):
    """
    Check if a user has admin/root permissions

    Args:
        username: Username to check

    Returns:
        bool: True if user is admin, False otherwise
    """
    try:
        user_data = get_user_by_username(username)
        if user_data:
            return user_data.get('type') == 'root'
        return False
    except Exception as e:
        logger.error(f"Error checking admin status: {e}")
        return False


def get_all_users():
    """
    Get all users from the database

    Returns:
        list: List of user dictionaries with username, type, and timestamps
    """
    try:
        conn = sqlite3.connect(AUTH_DB_PATH)
        cursor = conn.cursor()

        cursor.execute(
            'SELECT username, user_type, created_at, last_login FROM users ORDER BY username'
        )
        users = cursor.fetchall()
        conn.close()

        return [
            {
                'username': username,
                'type': user_type,
                'created_at': created_at,
                'lastSeen': last_login,
                'isActive': 1  # All users in new auth system are active
            }
            for username, user_type, created_at, last_login in users
        ]
    except Exception as e:
        logger.error(f"Error getting all users: {e}")
        return []
