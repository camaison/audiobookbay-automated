import os, re, requests, hashlib, time, sqlite3, threading, subprocess
from flask import Flask, request, render_template, jsonify, redirect, url_for, flash, session
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from bs4 import BeautifulSoup
from qbittorrentapi import Client
from transmission_rpc import Client as transmissionrpc
from deluge_web_client import DelugeWebClient as delugewebclient
from dotenv import load_dotenv
from urllib.parse import urlparse
import json
import bcrypt

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'your-secret-key-change-me')

# Initialize Flask-Login
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

#Load environment variables
load_dotenv()

ABB_HOSTNAME = os.getenv("ABB_HOSTNAME", "audiobookbay.lu")

PAGE_LIMIT = int(os.getenv("PAGE_LIMIT", 5))

DOWNLOAD_CLIENT = os.getenv("DOWNLOAD_CLIENT")
DL_URL = os.getenv("DL_URL")
if DL_URL:
    parsed_url = urlparse(DL_URL)
    DL_SCHEME = parsed_url.scheme
    DL_HOST = parsed_url.hostname
    DL_PORT = parsed_url.port
else:
    DL_SCHEME = os.getenv("DL_SCHEME", "http")
    DL_HOST = os.getenv("DL_HOST")
    DL_PORT = os.getenv("DL_PORT")

    # Make a DL_URL for Deluge if one was not specified
    if DL_HOST and DL_PORT:
        DL_URL = f"{DL_SCHEME}://{DL_HOST}:{DL_PORT}"

DL_USERNAME = os.getenv("DL_USERNAME")
DL_PASSWORD = os.getenv("DL_PASSWORD")
DL_CATEGORY = os.getenv("DL_CATEGORY", "Audiobookbay-Audiobooks")
SAVE_PATH_BASE = os.getenv("SAVE_PATH_BASE")

# Custom Nav Link Variables
NAV_LINK_NAME = os.getenv("NAV_LINK_NAME")
NAV_LINK_URL = os.getenv("NAV_LINK_URL")

# User management with AudiobookShelf database
ABS_DATABASE = os.getenv('ABS_DATABASE_PATH', 'absdatabase.sqlite')

class User(UserMixin):
    def __init__(self, username, user_type='user'):
        self.id = username
        self.username = username
        self.user_type = user_type
    
    def is_admin(self):
        return self.user_type == 'root'

def get_db_connection():
    """Get connection to AudiobookShelf database"""
    try:
        # Use direct path and disable problematic features
        db_path = os.path.abspath(ABS_DATABASE)
        # Use a more isolated connection approach
        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        
        # Disable all triggers, foreign keys, and schema validation
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("PRAGMA triggers = OFF") 
        conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute("PRAGMA writable_schema = OFF")
        
        return conn
    except Exception as e:
        print(f"[ERROR] Failed to connect to AudiobookShelf database: {e}")
        return None

def verify_user_credentials(username, password):
    """Verify user credentials against AudiobookShelf database"""
    print(f"[DEBUG] Attempting authentication for user: {username}")
    
    try:
        # Try using sqlite3 command line tool to bypass schema issues
        import subprocess
        import tempfile
        import json
        
        # Create a query to get user data using command line sqlite3
        query = f"SELECT username, pash, type, isActive FROM users WHERE username = '{username}' AND isActive = 1 LIMIT 1;"
        
        # Use sqlite3 command with -bail flag to stop on first error but continue with data
        result = subprocess.run([
            'sqlite3', 
            ABS_DATABASE, 
            '-json',
            query
        ], capture_output=True, text=True, timeout=10)
        
        if result.returncode == 0 and result.stdout.strip():
            # Parse JSON output from sqlite3
            data = json.loads(result.stdout.strip())
            if data and len(data) > 0:
                user_data = data[0]
                db_username = user_data.get('username')
                stored_hash = user_data.get('pash')
                user_type = user_data.get('type')
                is_active = user_data.get('isActive')
                
                if not stored_hash:
                    print(f"[DEBUG] No password hash for user {username}")
                    return None
                
                # AudiobookShelf uses bcrypt - verify password directly
                if bcrypt.checkpw(password.encode('utf-8'), stored_hash.encode('utf-8')):
                    print(f"[DEBUG] Authentication successful for {username}")
                    return {
                        'username': db_username,
                        'type': user_type,
                        'is_active': is_active
                    }
                else:
                    print(f"[DEBUG] Password verification failed for {username}")
                    return None
        
        # If subprocess approach fails, try direct connection one more time
        print("[DEBUG] Command line sqlite3 failed, trying direct connection...")
        
        # Last attempt with most minimal connection
        conn = sqlite3.connect(ABS_DATABASE, isolation_level=None)
        cursor = conn.cursor()
        cursor.execute("SELECT username, pash, type, isActive FROM users WHERE username = ? AND isActive = 1 LIMIT 1", (username,))
        result = cursor.fetchone()
        conn.close()
        
        if result:
            db_username, stored_hash, user_type, is_active = result
            if stored_hash and bcrypt.checkpw(password.encode('utf-8'), stored_hash.encode('utf-8')):
                print(f"[DEBUG] Authentication successful for {username}")
                return {
                    'username': db_username,
                    'type': user_type,
                    'is_active': is_active
                }
                
        print(f"[DEBUG] User {username} not found or authentication failed")
        return None
            
    except Exception as e:
        print(f"[ERROR] Authentication failed for {username}: {e}")
        return None

def verify_password_hash(password, stored_hash):
    """Verify bcrypt password hash used by AudiobookShelf"""
    try:
        # AudiobookShelf uses bcrypt hashing with $2a$ format
        # Convert $2a$ to $2b$ for Python bcrypt compatibility
        if stored_hash.startswith('$2a$'):
            converted_hash = '$2b$' + stored_hash[4:]
        else:
            converted_hash = stored_hash
        
        return bcrypt.checkpw(password.encode('utf-8'), converted_hash.encode('utf-8'))
    except Exception as e:
        print(f"[ERROR] Password verification failed with hash conversion: {e}")
        # Fallback: try original hash format
        try:
            return bcrypt.checkpw(password.encode('utf-8'), stored_hash.encode('utf-8'))
        except Exception as e2:
            print(f"[ERROR] Password verification failed with original hash: {e2}")
            return False

def get_user_by_username(username):
    """Get user details by username"""
    try:
        # Try using sqlite3 command line tool to bypass schema issues
        import subprocess
        import json
        
        # Create a query to get user data using command line sqlite3
        query = f"SELECT username, type, isActive FROM users WHERE username = '{username}' AND isActive = 1 LIMIT 1;"
        
        # Use sqlite3 command with JSON output
        result = subprocess.run([
            'sqlite3', 
            ABS_DATABASE, 
            '-json',
            query
        ], capture_output=True, text=True, timeout=10)
        
        if result.returncode == 0 and result.stdout.strip():
            # Parse JSON output from sqlite3
            data = json.loads(result.stdout.strip())
            if data and len(data) > 0:
                user_data = data[0]
                return {
                    'username': user_data.get('username'),
                    'type': user_data.get('type'),
                    'is_active': user_data.get('isActive')
                }
        
        print(f"[DEBUG] User {username} not found via command line sqlite3")
        return None
        
    except Exception as e:
        print(f"[ERROR] Failed to get user {username}: {e}")
        return None

# User-specific data management
USER_DATA_DIR = 'user_data'

def get_user_data_path(username, data_type):
    """Get user-specific data file path"""
    if not os.path.exists(USER_DATA_DIR):
        os.makedirs(USER_DATA_DIR)
    return os.path.join(USER_DATA_DIR, f'{username}_{data_type}.json')

def load_user_search_history(username):
    """Load search history for specific user"""
    file_path = get_user_data_path(username, 'search_history')
    if os.path.exists(file_path):
        with open(file_path, 'r') as f:
            return json.load(f)
    return []

def save_user_search_history(username, history):
    """Save search history for specific user"""
    file_path = get_user_data_path(username, 'search_history')
    with open(file_path, 'w') as f:
        json.dump(history, f)

def load_user_favorites(username):
    """Load favorites for specific user from database"""
    try:
        result = subprocess.run([
            'sqlite3', 
            ABS_DATABASE, 
            '-json',
            f"SELECT book_title, book_url, book_cover, book_author FROM user_favorites WHERE user_id = '{username}' ORDER BY created_at DESC"
        ], capture_output=True, text=True, timeout=10)
        
        if result.returncode == 0 and result.stdout.strip():
            db_favorites = json.loads(result.stdout.strip())
            # Map database fields to template expected fields
            favorites = []
            for fav in db_favorites:
                mapped_fav = {
                    'title': fav.get('book_title', ''),
                    'link': fav.get('book_url', ''),
                    'cover': fav.get('book_cover', '/static/images/default_cover.jpg'),
                    'author': fav.get('book_author', ''),
                    # Add empty fields for metadata that template expects
                    'category': '',
                    'language': '',
                    'file_format': '',
                    'bitrate': '',
                    'file_size': '',
                    'duration': ''
                }
                favorites.append(mapped_fav)
            return favorites
        return []
    except Exception as e:
        print(f"[ERROR] Failed to load user favorites: {e}")
        return []

def save_user_favorites(username, favorites):
    """Deprecated - use add_user_favorite and remove_user_favorite instead"""
    pass

def add_user_favorite(username, book_title, book_url, book_cover="", book_author=""):
    """Add favorite to user's favorites in database"""
    try:
        # Escape single quotes properly
        safe_title = book_title.replace("'", "''")
        safe_author = book_author.replace("'", "''")
        query = f"INSERT OR IGNORE INTO user_favorites (user_id, book_title, book_url, book_cover, book_author) VALUES ('{username}', '{safe_title}', '{book_url}', '{book_cover}', '{safe_author}');"
        
        subprocess.run([
            'sqlite3', 
            ABS_DATABASE, 
            query
        ], capture_output=True, text=True, timeout=10)
        return True
    except Exception as e:
        print(f"[ERROR] Failed to add favorite: {e}")
        return False

def remove_user_favorite(username, book_url):
    """Remove favorite from user's favorites in database"""
    try:
        subprocess.run([
            'sqlite3', 
            ABS_DATABASE, 
            f"DELETE FROM user_favorites WHERE user_id = '{username}' AND book_url = '{book_url}';"
        ], capture_output=True, text=True, timeout=10)
        return True
    except Exception as e:
        print(f"[ERROR] Failed to remove favorite: {e}")
        return False

def is_admin_user(username):
    """Check if user has admin/root permissions"""
    try:
        result = subprocess.run([
            'sqlite3', 
            ABS_DATABASE, 
            '-json',
            f"SELECT type FROM users WHERE username = '{username}'"
        ], capture_output=True, text=True, timeout=10)
        
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout.strip())
            return data[0]['type'] == 'root' if data else False
        return False
    except Exception as e:
        print(f"[ERROR] Failed to check admin status: {e}")
        return False

def get_all_users():
    """Get all users from database for admin dashboard"""
    try:
        result = subprocess.run([
            'sqlite3', 
            ABS_DATABASE, 
            '-json',
            "SELECT username, type, isActive, lastSeen FROM users ORDER BY username"
        ], capture_output=True, text=True, timeout=10)
        
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout.strip())
        return []
    except Exception as e:
        print(f"[ERROR] Failed to get users: {e}")
        return []

def get_all_user_downloads():
    """Get download summary for all users from database"""
    try:
        result = subprocess.run([
            'sqlite3', 
            ABS_DATABASE, 
            '-json',
            "SELECT user_id, COUNT(*) as download_count FROM user_downloads GROUP BY user_id"
        ], capture_output=True, text=True, timeout=10)
        
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout.strip())
            return {item['user_id']: item['download_count'] for item in data}
        return {}
    except Exception as e:
        print(f"[ERROR] Failed to get all user downloads: {e}")
        return {}

def get_detailed_user_downloads():
    """Get detailed download history for all users (admin only)"""
    try:
        result = subprocess.run([
            'sqlite3', 
            ABS_DATABASE, 
            '-json',
            "SELECT user_id, torrent_hash, book_title, book_url, created_at FROM user_downloads ORDER BY created_at DESC"
        ], capture_output=True, text=True, timeout=10)
        
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout.strip())
        return []
    except Exception as e:
        print(f"[ERROR] Failed to get detailed downloads: {e}")
        return []

def load_user_downloads(username):
    """Load download history for specific user from database"""
    try:
        result = subprocess.run([
            'sqlite3', 
            ABS_DATABASE, 
            '-json',
            f"SELECT torrent_hash, book_title, book_url, created_at FROM user_downloads WHERE user_id = '{username}' ORDER BY created_at DESC"
        ], capture_output=True, text=True, timeout=10)
        
        if result.returncode == 0 and result.stdout.strip():
            downloads = json.loads(result.stdout.strip())
            # Convert to expected format for backward compatibility
            return [{'hash': d['torrent_hash'], 'title': d['book_title'], 'url': d['book_url'], 'timestamp': d['created_at']} for d in downloads]
        return []
    except Exception as e:
        print(f"[ERROR] Failed to load user downloads: {e}")
        return []

def save_user_downloads(username, downloads):
    """Deprecated - use add_user_download instead"""
    pass

def add_user_download(username, torrent_hash, book_title, download_url):
    """Add download to user's download history in database"""
    try:
        # Escape single quotes properly
        safe_title = book_title.replace("'", "''")
        query = f"INSERT OR IGNORE INTO user_downloads (user_id, torrent_hash, book_title, book_url) VALUES ('{username}', '{torrent_hash}', '{safe_title}', '{download_url}');"
        
        subprocess.run([
            'sqlite3', 
            ABS_DATABASE, 
            query
        ], capture_output=True, text=True, timeout=10)
        return True
    except Exception as e:
        print(f"[ERROR] Failed to add download: {e}")
        return False

# Legacy functions for backward compatibility (will migrate existing data)
def load_search_history():
    """Legacy function - migrate to user-specific storage"""
    if os.path.exists('search_history.json'):
        with open('search_history.json', 'r') as f:
            return json.load(f)
    return {}

def save_search_history(history):
    """Legacy function - will be removed after migration"""
    pass  # No longer save to global file

def load_favorites():
    """Legacy function - migrate to user-specific storage"""
    if os.path.exists('favorites.json'):
        with open('favorites.json', 'r') as f:
            return json.load(f)
    return {}

def save_favorites(favorites):
    """Legacy function - will be removed after migration"""
    pass  # No longer save to global file

def add_to_search_history(username, query):
    if not query or len(query.strip()) == 0:
        return
    
    history = load_user_search_history(username)
    
    # Remove query if it already exists
    history = [h for h in history if h.get('query', '').lower() != query.lower()]
    
    # Add to beginning of list
    history.insert(0, {
        'query': query,
        'timestamp': int(time.time())
    })
    
    # Keep only last 50 searches
    history = history[:50]
    save_user_search_history(username, history)

@login_manager.user_loader
def load_user(user_id):
    user_data = get_user_by_username(user_id)
    if user_data:
        return User(user_data['username'], user_data['type'])
    return None

#Print configuration
print(f"ABB_HOSTNAME: {ABB_HOSTNAME}")
print(f"DOWNLOAD_CLIENT: {DOWNLOAD_CLIENT}")
print(f"DL_HOST: {DL_HOST}")
print(f"DL_PORT: {DL_PORT}")
print(f"DL_URL: {DL_URL}")
print(f"DL_USERNAME: {DL_USERNAME}")
print(f"DL_CATEGORY: {DL_CATEGORY}")
print(f"SAVE_PATH_BASE: {SAVE_PATH_BASE}")
print(f"NAV_LINK_NAME: {NAV_LINK_NAME}")
print(f"NAV_LINK_URL: {NAV_LINK_URL}")
print(f"PAGE_LIMIT: {PAGE_LIMIT}")


@app.context_processor
def inject_nav_link():
    return {
        'nav_link_name': os.getenv('NAV_LINK_NAME'),
        'nav_link_url': os.getenv('NAV_LINK_URL')
    }



# Helper function to search AudiobookBay with pagination support
def search_audiobookbay(query, page_num=1):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36'
    }
    results = []
    url = f"https://{ABB_HOSTNAME}/page/{page_num}/?s={query.replace(' ', '+')}&cat=undefined%2Cundefined"
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code != 200:
            print(f"[ERROR] Failed to fetch page {page_num}. Status Code: {response.status_code}")
            return results

        soup = BeautifulSoup(response.text, 'html.parser')
        results = []
        
        # Extract posts from search results - use same approach as homepage
        post_selectors = ['.post', 'article.post', '.entry', '.postContent']
        posts = []
        
        for selector in post_selectors:
            all_posts = soup.select(selector)[:18]  # Limit to first 18 posts
            if all_posts:
                # Filter out hidden posts with display:none (SAME AS HOMEPAGE)
                posts = [p for p in all_posts if 'display:none' not in str(p)]
                print(f"[INFO] Found {len(all_posts)} posts, {len(posts)} visible using selector '{selector}' on page {page_num}")
                break
        
        if not posts:
            print(f"[WARNING] No posts found with any selector on search page {page_num}")
            return []

        for post in posts:
            try:
                # Try multiple title selectors (EXACT SAME AS HOMEPAGE)
                title_selectors = [
                    '.postTitle > h2 > a',
                    '.postTitle a', 
                    'h2 a',
                    'h3 a',
                    '.entry-title a',
                    'a[rel="bookmark"]'
                ]
                
                title_element = None
                for title_sel in title_selectors:
                    title_element = post.select_one(title_sel)
                    if title_element:
                        break
                
                if not title_element:
                    continue
                    
                title = title_element.text.strip()
                href = title_element.get('href', '')
                
                # Handle relative and absolute URLs (EXACT SAME AS HOMEPAGE)
                if href.startswith('http'):
                    link = href
                elif href.startswith('/'):
                    link = f"http://{ABB_HOSTNAME}{href}"
                else:
                    link = f"http://{ABB_HOSTNAME}/{href}"
                
                # Extract cover image with better selectors (EXACT SAME AS HOMEPAGE)
                cover_selectors = [
                    'img[src*="cover"]',
                    'img[alt*="cover"]', 
                    '.postContent img',
                    'img'
                ]
                
                cover = "/static/images/default_cover.jpg"
                for cover_sel in cover_selectors:
                    cover_element = post.select_one(cover_sel)
                    if cover_element and cover_element.get('src'):
                        cover_src = cover_element['src']
                        if cover_src.startswith('//'):
                            cover = 'http:' + cover_src
                        elif cover_src.startswith('/'):
                            cover = f"http://{ABB_HOSTNAME}{cover_src}"
                        elif cover_src.startswith('http'):
                            cover = cover_src
                        else:
                            cover = f"http://{ABB_HOSTNAME}/{cover_src}"
                        break
                
                # Extract comprehensive metadata using new helper functions (EXACT SAME AS HOMEPAGE)
                meta_info = post.select_one('.postContent, .entry-content, .post-content')
                meta_text = meta_info.get_text() if meta_info else ""
                
                # Extract file size (keep existing pattern for compatibility)
                file_size = ""
                size_match = re.search(r'(\d+(?:\.\d+)?)\s*(MB|GB)', meta_text, re.IGNORECASE)
                if size_match:
                    file_size = f"{size_match.group(1)} {size_match.group(2).upper()}"
                
                # Use new extraction functions for comprehensive metadata (EXACT SAME AS HOMEPAGE)
                book_data = {
                    'title': clean_title(title),
                    'link': link,
                    'cover': cover,
                    'author': extract_author(meta_text, title),
                    'category': extract_category(post, meta_text),
                    'keywords': extract_keywords(meta_text),
                    'language': extract_language(meta_text),
                    'file_format': extract_format(meta_text),
                    'bitrate': extract_bitrate(meta_text),
                    'file_size': file_size,
                    'upload_date': extract_upload_date(post),
                    'duration': extract_duration(meta_text),
                    'publisher': extract_publisher(meta_text),
                    'isbn': extract_isbn(meta_text),
                    'asin': extract_asin(meta_text),
                    'explicit': check_explicit_content(meta_text),
                    'abridged': check_abridged(meta_text),
                    'posted_date': extract_upload_date(post)
                }

                results.append(book_data)
            except Exception as e:
                print(f"[ERROR] Skipping post due to error: {e}")
                continue
                
        print(f"[INFO] Found {len(results)} results on page {page_num}")
        return results
        
    except Exception as e:
        print(f"[ERROR] Failed to search page {page_num}: {e}")
        return results

# Helper function to scrape AudiobookBay homepage
def scrape_homepage():
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36'
    }
    try:
        # Try different URL schemes - audiobookbay.lu might redirect
        urls_to_try = [
            f"http://{ABB_HOSTNAME}",
            f"https://{ABB_HOSTNAME}",
        ]
        
        response = None
        for url in urls_to_try:
            try:
                response = requests.get(url, headers=headers, timeout=10, allow_redirects=True)
                if response.status_code == 200:
                    print(f"[INFO] Successfully connected to {url}")
                    break
            except Exception as e:
                print(f"[INFO] Failed to connect to {url}: {e}")
                continue
        
        if not response or response.status_code != 200:
            print(f"[ERROR] Failed to fetch homepage from any URL. Last status: {response.status_code if response else 'None'}")
            return []

        soup = BeautifulSoup(response.text, 'html.parser')
        results = []
        
        # Extract posts from homepage - try multiple selectors
        post_selectors = ['.post', 'article.post', '.entry', '.postContent']
        posts = []
        
        for selector in post_selectors:
            all_posts = soup.select(selector)[:18]  # Limit to first 18 posts
            if all_posts:
                # Filter out hidden posts with display:none
                posts = [p for p in all_posts if 'display:none' not in str(p)]
                print(f"[INFO] Found {len(all_posts)} posts, {len(posts)} visible using selector '{selector}'")
                break
        
        if not posts:
            print("[WARNING] No posts found with any selector")
            return []
        
        for post in posts:
            try:
                # Try multiple title selectors
                title_selectors = [
                    '.postTitle > h2 > a',
                    '.postTitle a', 
                    'h2 a',
                    'h3 a',
                    '.entry-title a',
                    'a[rel="bookmark"]'
                ]
                
                title_element = None
                for title_sel in title_selectors:
                    title_element = post.select_one(title_sel)
                    if title_element:
                        break
                
                if not title_element:
                    continue
                    
                title = title_element.text.strip()
                href = title_element.get('href', '')
                
                # Handle relative and absolute URLs
                if href.startswith('http'):
                    link = href
                elif href.startswith('/'):
                    link = f"http://{ABB_HOSTNAME}{href}"
                else:
                    link = f"http://{ABB_HOSTNAME}/{href}"
                
                # Extract cover image with better selectors
                cover_selectors = [
                    'img[src*="cover"]',
                    'img[alt*="cover"]', 
                    '.postContent img',
                    'img'
                ]
                
                cover = "/static/images/default_cover.jpg"
                for cover_sel in cover_selectors:
                    cover_element = post.select_one(cover_sel)
                    if cover_element and cover_element.get('src'):
                        cover_src = cover_element['src']
                        if cover_src.startswith('//'):
                            cover = 'http:' + cover_src
                        elif cover_src.startswith('/'):
                            cover = f"http://{ABB_HOSTNAME}{cover_src}"
                        elif cover_src.startswith('http'):
                            cover = cover_src
                        else:
                            cover = f"http://{ABB_HOSTNAME}/{cover_src}"
                        break
                
                # Extract comprehensive metadata using new helper functions
                meta_info = post.select_one('.postContent, .entry-content, .post-content')
                meta_text = meta_info.get_text() if meta_info else ""
                
                # Extract file size (keep existing pattern for compatibility)
                file_size = ""
                size_match = re.search(r'(\d+(?:\.\d+)?)\s*(MB|GB)', meta_text, re.IGNORECASE)
                if size_match:
                    file_size = f"{size_match.group(1)} {size_match.group(2).upper()}"
                
                # Use new extraction functions for comprehensive metadata
                book_data = {
                    'title': clean_title(title),
                    'link': link,
                    'cover': cover,
                    'author': extract_author(meta_text, title),
                    'category': extract_category(post, meta_text),
                    'keywords': extract_keywords(meta_text),
                    'language': extract_language(meta_text),
                    'file_format': extract_format(meta_text),
                    'bitrate': extract_bitrate(meta_text),
                    'file_size': file_size,
                    'upload_date': extract_upload_date(post),
                    'duration': extract_duration(meta_text),
                    'publisher': extract_publisher(meta_text),
                    'isbn': extract_isbn(meta_text),
                    'asin': extract_asin(meta_text),
                    'explicit': check_explicit_content(meta_text),
                    'abridged': check_abridged(meta_text),
                    'posted_date': extract_upload_date(post)
                }

                results.append(book_data)
            except Exception as e:
                print(f"[ERROR] Skipping post due to error: {e}")
                continue
                
        return results
    except Exception as e:
        print(f"[ERROR] Failed to scrape homepage: {e}")
        return []

# Helper function to scrape AudiobookBay homepage with pagination
def scrape_homepage_with_pagination(page_num=1):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36'
    }
    try:
        # Try different URL schemes - audiobookbay.lu might redirect
        if page_num == 1:
            urls_to_try = [
                f"http://{ABB_HOSTNAME}",
                f"https://{ABB_HOSTNAME}",
            ]
        else:
            urls_to_try = [
                f"http://{ABB_HOSTNAME}/page/{page_num}/",
                f"https://{ABB_HOSTNAME}/page/{page_num}/",
                f"http://{ABB_HOSTNAME}/page/{page_num}",
                f"https://{ABB_HOSTNAME}/page/{page_num}",
            ]
        
        response = None
        for url in urls_to_try:
            try:
                response = requests.get(url, headers=headers, timeout=10, allow_redirects=True)
                if response.status_code == 200:
                    print(f"[INFO] Successfully connected to {url}")
                    break
            except Exception as e:
                print(f"[INFO] Failed to connect to {url}: {e}")
                continue
        
        if not response or response.status_code != 200:
            print(f"[ERROR] Failed to fetch homepage page {page_num} from any URL. Last status: {response.status_code if response else 'None'}")
            return []

        soup = BeautifulSoup(response.text, 'html.parser')
        results = []
        
        # Extract posts from homepage - try multiple selectors
        post_selectors = ['.post', 'article.post', '.entry', '.postContent']
        posts = []
        
        for selector in post_selectors:
            all_posts = soup.select(selector)[:18]  # Limit to first 18 posts
            if all_posts:
                # Filter out hidden posts with display:none (SAME AS HOMEPAGE)
                posts = [p for p in all_posts if 'display:none' not in str(p)]
                print(f"[INFO] Found {len(all_posts)} posts, {len(posts)} visible using selector '{selector}' on page {page_num}")
                break
        
        if not posts:
            print(f"[WARNING] No posts found with any selector on page {page_num}")
            return []
        
        for post in posts:
            try:
                # Try multiple title selectors (EXACT SAME AS HOMEPAGE)
                title_selectors = [
                    '.postTitle > h2 > a',
                    '.postTitle a', 
                    'h2 a',
                    'h3 a',
                    '.entry-title a',
                    'a[rel="bookmark"]'
                ]
                
                title_element = None
                for title_sel in title_selectors:
                    title_element = post.select_one(title_sel)
                    if title_element:
                        break
                
                if not title_element:
                    continue
                    
                title = title_element.text.strip()
                href = title_element.get('href', '')
                
                # Handle relative and absolute URLs (EXACT SAME AS HOMEPAGE)
                if href.startswith('http'):
                    link = href
                elif href.startswith('/'):
                    link = f"http://{ABB_HOSTNAME}{href}"
                else:
                    link = f"http://{ABB_HOSTNAME}/{href}"
                
                # Extract cover image with better selectors (EXACT SAME AS HOMEPAGE)
                cover_selectors = [
                    'img[src*="cover"]',
                    'img[alt*="cover"]', 
                    '.postContent img',
                    'img'
                ]
                
                cover = "/static/images/default_cover.jpg"
                for cover_sel in cover_selectors:
                    cover_element = post.select_one(cover_sel)
                    if cover_element and cover_element.get('src'):
                        cover_src = cover_element['src']
                        if cover_src.startswith('//'):
                            cover = 'http:' + cover_src
                        elif cover_src.startswith('/'):
                            cover = f"http://{ABB_HOSTNAME}{cover_src}"
                        elif cover_src.startswith('http'):
                            cover = cover_src
                        else:
                            cover = f"http://{ABB_HOSTNAME}/{cover_src}"
                        break
                
                # Extract comprehensive metadata using new helper functions (EXACT SAME AS HOMEPAGE)
                meta_info = post.select_one('.postContent, .entry-content, .post-content')
                meta_text = meta_info.get_text() if meta_info else ""
                
                # Extract file size (keep existing pattern for compatibility)
                file_size = ""
                size_match = re.search(r'(\d+(?:\.\d+)?)\s*(MB|GB)', meta_text, re.IGNORECASE)
                if size_match:
                    file_size = f"{size_match.group(1)} {size_match.group(2).upper()}"
                
                # Use new extraction functions for comprehensive metadata (EXACT SAME AS HOMEPAGE)
                book_data = {
                    'title': clean_title(title),
                    'link': link,
                    'cover': cover,
                    'author': extract_author(meta_text, title),
                    'category': extract_category(post, meta_text),
                    'keywords': extract_keywords(meta_text),
                    'language': extract_language(meta_text),
                    'file_format': extract_format(meta_text),
                    'bitrate': extract_bitrate(meta_text),
                    'file_size': file_size,
                    'upload_date': extract_upload_date(post),
                    'duration': extract_duration(meta_text),
                    'publisher': extract_publisher(meta_text),
                    'isbn': extract_isbn(meta_text),
                    'asin': extract_asin(meta_text),
                    'explicit': check_explicit_content(meta_text),
                    'abridged': check_abridged(meta_text),
                    'posted_date': extract_upload_date(post)
                }

                results.append(book_data)
            except Exception as e:
                print(f"[ERROR] Skipping post due to error on page {page_num}: {e}")
                continue
                
        return results
    except Exception as e:
        print(f"[ERROR] Failed to scrape homepage page {page_num}: {e}")
        return []

# Helper function to extract magnet link from details page
def extract_magnet_link(details_url):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36'
    }
    try:
        response = requests.get(details_url, headers=headers)
        if response.status_code != 200:
            print(f"[ERROR] Failed to fetch details page. Status Code: {response.status_code}")
            return None

        soup = BeautifulSoup(response.text, 'html.parser')

        # Extract Info Hash
        info_hash_row = soup.find('td', string=re.compile(r'Info Hash', re.IGNORECASE))
        if not info_hash_row:
            print("[ERROR] Info Hash not found on the page.")
            return None
        info_hash = info_hash_row.find_next_sibling('td').text.strip()

        # Extract Trackers
        tracker_rows = soup.find_all('td', string=re.compile(r'udp://|http://', re.IGNORECASE))
        trackers = [row.text.strip() for row in tracker_rows]

        if not trackers:
            print("[WARNING] No trackers found on the page. Using default trackers.")
            trackers = [
                "udp://tracker.openbittorrent.com:80",
                "udp://opentor.org:2710",
                "udp://tracker.ccc.de:80",
                "udp://tracker.blackunicorn.xyz:6969",
                "udp://tracker.coppersurfer.tk:6969",
                "udp://tracker.leechers-paradise.org:6969"
            ]

        # Construct the magnet link
        trackers_query = "&".join(f"tr={requests.utils.quote(tracker)}" for tracker in trackers)
        magnet_link = f"magnet:?xt=urn:btih:{info_hash}&{trackers_query}"

        print(f"[DEBUG] Generated Magnet Link: {magnet_link}")
        return magnet_link

    except Exception as e:
        print(f"[ERROR] Failed to extract magnet link: {e}")
        return None

# Helper function to extract related books from AudiobookBay page
def get_related_books_from_page(soup, book_url):
    """Extract related books from the actual page content"""
    related_books = []
    try:
        # Look for related books sections with various selectors
        related_selectors = [
            '.related-posts',
            '.similar-posts', 
            '.more-posts',
            '#related',
            '.post-related',
            '.yarpp-related',
            '[class*="related"]'
        ]
        
        related_section = None
        for selector in related_selectors:
            section = soup.select_one(selector)
            if section:
                related_section = section
                break
        
        if related_section:
            # Extract links from the related section
            links = related_section.select('a[href*="/abss/"], a[href*="audiobookbay"]')
            for link in links[:8]:  # Limit to 8 related books
                try:
                    title = link.get_text().strip()
                    href = link.get('href', '')
                    
                    if not title or href == book_url:  # Skip empty titles or self-references
                        continue
                    
                    # Handle relative URLs
                    if href.startswith('/'):
                        href = f"https://{ABB_HOSTNAME}{href}"
                    elif not href.startswith('http'):
                        href = f"https://{ABB_HOSTNAME}/{href}"
                    
                    # Extract author from title (Title - Author format)
                    author = ""
                    if " - " in title:
                        parts = title.split(" - ", 1)
                        if len(parts) == 2:
                            title = parts[0].strip()
                            author = parts[1].strip()
                    
                    related_books.append({
                        'title': clean_title(title),
                        'author': author,
                        'link': href,
                        'cover': "/static/images/default_cover.jpg",  # Default cover
                        'category': ''
                    })
                except Exception as e:
                    print(f"[DEBUG] Error extracting related book: {e}")
                    continue
        
        # If no related section found, try to find links in the general content area
        if not related_books:
            # Look for links to other audiobooks in the page content
            content_area = soup.select_one('.postContent, .post-content, .entry-content')
            if content_area:
                book_links = content_area.select('a[href*="/abss/"]')
                for link in book_links[:6]:  # Limit to 6
                    try:
                        title = link.get_text().strip()
                        href = link.get('href', '')
                        
                        if not title or href == book_url or len(title) > 100:
                            continue
                        
                        # Handle relative URLs
                        if href.startswith('/'):
                            href = f"https://{ABB_HOSTNAME}{href}"
                        
                        # Extract author from title if present
                        author = ""
                        if " - " in title:
                            parts = title.split(" - ", 1)
                            if len(parts) == 2:
                                title = parts[0].strip()
                                author = parts[1].strip()
                        
                        related_books.append({
                            'title': clean_title(title),
                            'author': author,
                            'link': href,
                            'cover': "/static/images/default_cover.jpg",
                            'category': ''
                        })
                    except Exception as e:
                        print(f"[DEBUG] Error extracting content link: {e}")
                        continue
        
        print(f"[DEBUG] Found {len(related_books)} related books from page")
        return related_books
        
    except Exception as e:
        print(f"[ERROR] Failed to extract related books: {e}")
        return []

# Helper function to extract book details from AudiobookBay page
def get_book_details(book_url):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36'
    }
    try:
        response = requests.get(book_url, headers=headers, timeout=15)
        if response.status_code != 200:
            print(f"[ERROR] Failed to fetch book details. Status Code: {response.status_code}")
            return None

        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Extract basic information - AudiobookBay uses h1.postTitle
        title_element = soup.select_one('h1.postTitle, .postTitle h1, .postTitle a, .post h1')
        title = title_element.get_text().strip() if title_element else "Unknown Title"
        
        # Extract cover image - look for first image in post content or specific cover classes
        cover_element = soup.select_one('.postContent img, .post-content img, img[src*="cover"], img[alt*="cover"], .entry-content img')
        if cover_element and cover_element.get('src'):
            cover = cover_element['src']
            # Handle relative URLs
            if cover.startswith('//'):
                cover = 'https:' + cover
            elif cover.startswith('/'):
                cover = f"https://{ABB_HOSTNAME}" + cover
        else:
            cover = "/static/images/default_cover.jpg"
        
        # Extract content/description from post content
        content_element = soup.select_one('.postContent, .entry-content, .post-content, .post .postContent')
        description = ""
        author = ""
        narrator = ""
        duration = ""
        file_format = ""
        file_size = ""
        bitrate = ""
        language = "English"
        posted_date = ""
        categories = []
        download_links = []
        
        if content_element:
            content_text = content_element.get_text()
            content_html = str(content_element)
            
            # Extract author from title or content - AudiobookBay uses "Title - Author" format
            if " - " in title and not author:
                parts = title.split(" - ", 1)
                if len(parts) == 2:
                    potential_author = parts[1].strip()
                    if not re.search(r'\d', potential_author) and len(potential_author) < 50:  # Avoid numbers and overly long strings
                        author = potential_author
                        title = parts[0].strip()  # Keep the first part as title
            
            # Look for author in various formats
            author_patterns = [
                r'Author[:\s]+([^\n\r]+)',
                r'by\s+([^\n\r,]+)',
                r'Written by[:\s]+([^\n\r]+)',
                r'Book by[:\s]+([^\n\r]+)',
            ]
            if not author:
                for pattern in author_patterns:
                    match = re.search(pattern, content_text, re.IGNORECASE)
                    if match:
                        author = match.group(1).strip()
                        break
            
            # Extract narrator
            narrator_patterns = [
                r'Narrator[:\s]+([A-Za-z\s\.\-\']+?)(?:\s+Format|\s+Bitrate|\s+Unabridged|\s+M4B|$)',
                r'Narrated by[:\s]+([A-Za-z\s\.\-\']+?)(?:\s+Format|\s+Bitrate|\s+Unabridged|\s+M4B|$)',
                r'Read by[:\s]+([A-Za-z\s\.\-\']+?)(?:\s+Format|\s+Bitrate|\s+Unabridged|\s+M4B|$)',
                r'Voice[:\s]+([A-Za-z\s\.\-\']+?)(?:\s+Format|\s+Bitrate|\s+Unabridged|\s+M4B|$)'
            ]
            for pattern in narrator_patterns:
                match = re.search(pattern, content_text, re.IGNORECASE)
                if match:
                    narrator = match.group(1).strip()
                    # Clean up any remaining artifacts
                    narrator = re.sub(r'\s+(Format|Written|Read|Bitrate|M4B|Unabridged).*$', '', narrator, flags=re.IGNORECASE)
                    if len(narrator) > 2 and len(narrator) < 50:
                        break
            
            # Extract file information with improved patterns
            # File size - look for total size or individual file sizes
            size_patterns = [
                r'Total Size[:\s]+(\d+(?:\.\d+)?)\s*(MB|GB)',
                r'Size[:\s]+(\d+(?:\.\d+)?)\s*(MB|GB)', 
                r'(\d+(?:\.\d+)?)\s*(MB|GB)',
                r'(\d+(?:\.\d+)?)\s*MBs?',
                r'(\d+(?:\.\d+)?)\s*GBs?'
            ]
            for pattern in size_patterns:
                size_match = re.search(pattern, content_text, re.IGNORECASE)
                if size_match:
                    if len(size_match.groups()) >= 2:
                        file_size = f"{size_match.group(1)} {size_match.group(2).upper()}"
                    else:
                        file_size = size_match.group(0)
                    break
            
            # File format
            format_patterns = [
                r'Format[:\s]+(\w+)',
                r'File Format[:\s]+(\w+)',
                r'Audio Format[:\s]+(\w+)',
                r'\.(mp3|m4a|m4b|aac|flac|wav|ogg)\b'
            ]
            for pattern in format_patterns:
                format_match = re.search(pattern, content_text, re.IGNORECASE)
                if format_match:
                    file_format = format_match.group(1).upper()
                    break
            
            # Bitrate
            bitrate_patterns = [
                r'Bitrate[:\s]+(\d+)\s*kbps',
                r'(\d+)\s*kbps',
                r'(\d+)\s*Kbps'
            ]
            for pattern in bitrate_patterns:
                bitrate_match = re.search(pattern, content_text, re.IGNORECASE)
                if bitrate_match:
                    bitrate = f"{bitrate_match.group(1)} kbps"
                    break
            
            # Duration
            duration_patterns = [
                r'Duration[:\s]+([^\n\r]+)',
                r'Length[:\s]+([^\n\r]+)',
                r'Runtime[:\s]+([^\n\r]+)',
                r'(\d+)\s*hours?\s*(\d+)?\s*minutes?',
                r'(\d+)h\s*(\d+)?m'
            ]
            for pattern in duration_patterns:
                duration_match = re.search(pattern, content_text, re.IGNORECASE)
                if duration_match:
                    duration = duration_match.group(1).strip()
                    break
            
            # Extract categories from links or text
            category_links = soup.select('a[href*="/category/"], a[href*="cat="]')
            for link in category_links:
                cat_text = link.get_text().strip()
                if cat_text and cat_text not in categories:
                    categories.append(cat_text)
            
                
            # Extract a meaningful description
            # Look for paragraph breaks or description sections
            description_patterns = [
                r'Description[:\s]*\n([^\n]*(?:\n[^\n]*){0,5})',
                r'Synopsis[:\s]*\n([^\n]*(?:\n[^\n]*){0,5})',
                r'About[:\s]*\n([^\n]*(?:\n[^\n]*){0,5})'
            ]
            
            for pattern in description_patterns:
                desc_match = re.search(pattern, content_text, re.IGNORECASE)
                if desc_match:
                    description = desc_match.group(1).strip()
                    break
            
            # If no description found, take first meaningful paragraph
            if not description:
                # Split by double newlines to get paragraphs
                paragraphs = re.split(r'\n\s*\n', content_text)
                for para in paragraphs:
                    cleaned = para.strip()
                    # Skip short lines, metadata lines, or lines with mostly special chars
                    if (len(cleaned) > 50 and 
                        not re.match(r'^(format|size|bitrate|duration|author|narrator)', cleaned, re.IGNORECASE) and
                        len(re.sub(r'[^a-zA-Z\s]', '', cleaned)) > 30):
                        description = cleaned  # Show full description without truncation
                        break
        
        # Use enhanced extraction functions for additional metadata
        enhanced_data = {
            'title': clean_title(title),
            'author': author or extract_author(content_text, title),
            'narrator': narrator,
            'cover': cover,
            'description': description,
            'duration': duration or extract_duration(content_text),
            'file_format': file_format or extract_format(content_text),
            'file_size': file_size,
            'bitrate': bitrate or extract_bitrate(content_text),
            'language': extract_language(content_text),
            'upload_date': extract_upload_date(soup),
            'creation_date': extract_creation_date(soup, content_text),
            'categories': categories,
            'category': ', '.join(categories) if categories else '',
            'keywords': extract_keywords(content_text),
            'publisher': extract_publisher(content_text),
            'isbn': extract_isbn(content_text),
            'asin': extract_asin(content_text),
            'explicit': check_explicit_content(content_text),
            'abridged': check_abridged(content_text),
            'uploader': extract_uploader(soup),
            'torrent_files': extract_torrent_files(soup, content_text),
            'comments': extract_comments(soup),
            'original_url': book_url
        }
        
        return enhanced_data
        
    except Exception as e:
        print(f"[ERROR] Failed to extract book details: {e}")
        return None

# Helper function to sanitize titles
def sanitize_title(title):
    return re.sub(r'[<>:"/\\|?*]', '', title).strip()

def clean_title(title):
    """Extract just the title part from 'Title - Author' format"""
    if " - " in title:
        parts = title.split(" - ", 1)
        if len(parts) == 2 and not re.search(r'\d', parts[1]) and len(parts[1]) < 50:
            return parts[0].strip()  # Return the first part (title)
    return title

# Enhanced metadata extraction helper functions
def extract_author(meta_text, title):
    """Extract author with multiple patterns"""
    # Check title first (Title - Author format)
    if " - " in title:
        parts = title.split(" - ", 1)
        if len(parts) == 2 and not re.search(r'\d', parts[1]) and len(parts[1]) < 50:
            return parts[1].strip()  # Return the second part (author)
    
    # Extract from content using various patterns
    patterns = [
        r'Author[:\s]+([^\n\r]+)',
        r'by\s+([^\n\r,]+)',
        r'Written by[:\s]+([^\n\r]+)',
        r'Book by[:\s]+([^\n\r]+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, meta_text, re.IGNORECASE)
        if match:
            author = match.group(1).strip()
            # Clean up common suffixes
            author = re.sub(r'\s*(,.*|\..*|\(.*)', '', author)
            if len(author) > 2 and len(author) < 100:
                return author
    
    return ""

def extract_category(post, meta_text):
    """Extract category from post or content"""
    # Look for category links or tags in the post HTML
    category_selectors = ['a[href*="/category/"]', '.category', '.tag', '.genre']
    for selector in category_selectors:
        category_elements = post.select(selector)
        if category_elements:
            return category_elements[0].get_text().strip()
    
    # Extract from meta text
    category_patterns = [
        r'Category[:\s]+([^\n\r]+)',
        r'Genre[:\s]+([^\n\r]+)',
        r'Section[:\s]+([^\n\r]+)'
    ]
    for pattern in category_patterns:
        match = re.search(pattern, meta_text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return ""

def extract_keywords(meta_text):
    """Extract keywords/tags"""
    keyword_patterns = [
        r'Tags[:\s]+([^\n\r]+)',
        r'Keywords[:\s]+([^\n\r]+)',
        r'Genres?[:\s]+([^\n\r,]+)',
        r'Subject[:\s]+([^\n\r]+)'
    ]
    for pattern in keyword_patterns:
        match = re.search(pattern, meta_text, re.IGNORECASE)
        if match:
            keywords = match.group(1).strip()
            # Clean up and limit length
            keywords = re.sub(r'[,;]+', ', ', keywords)
            return keywords[:200] if len(keywords) > 200 else keywords
    return ""

def extract_language(meta_text):
    """Extract language information"""
    languages = {
        'english': 'English', 'spanish': 'Spanish', 'french': 'French', 
        'german': 'German', 'italian': 'Italian', 'portuguese': 'Portuguese',
        'russian': 'Russian', 'chinese': 'Chinese', 'japanese': 'Japanese',
        'dutch': 'Dutch', 'swedish': 'Swedish', 'norwegian': 'Norwegian'
    }
    
    # Look for explicit language mentions
    language_pattern = r'Language[:\s]+([^\n\r]+)'
    match = re.search(language_pattern, meta_text, re.IGNORECASE)
    if match:
        lang_text = match.group(1).strip().lower()
        for key, value in languages.items():
            if key in lang_text:
                return value
    
    # Check for language keywords in text
    for key, value in languages.items():
        if key in meta_text.lower():
            return value
    
    return "English"  # Default

def extract_format(meta_text):
    """Extract file format"""
    format_patterns = [
        r'Format[:\s]+([^\n\r\s,]+)',
        r'File Format[:\s]+([^\n\r\s,]+)',
        r'Audio Format[:\s]+([^\n\r\s,]+)',
        r'\b(M4B|MP3|M4A|AAC|FLAC|WAV|OGG)\b'
    ]
    
    for pattern in format_patterns:
        match = re.search(pattern, meta_text, re.IGNORECASE)
        if match:
            format_str = match.group(1).upper().strip()
            # Clean up common format variations
            if format_str in ['M4B', 'MP3', 'M4A', 'AAC', 'FLAC', 'WAV', 'OGG']:
                return format_str
    
    return "M4B"  # Default for audiobooks

def extract_bitrate(meta_text):
    """Extract bitrate information"""
    bitrate_patterns = [
        r'Bitrate[:\s]+(\d+)\s*kbps',
        r'(\d+)\s*kbps',
        r'(\d+)\s*Kbps',
        r'Quality[:\s]+(\d+)\s*k'
    ]
    
    for pattern in bitrate_patterns:
        match = re.search(pattern, meta_text, re.IGNORECASE)
        if match:
            bitrate = match.group(1)
            return f"{bitrate} kbps"
    return ""

def extract_upload_date(post):
    """Extract upload date from post"""
    date_selectors = [
        '.date', '.posted', '.upload-date', 'time', '.post-date',
        '.entry-date', '.published', '.created'
    ]
    
    for selector in date_selectors:
        date_element = post.select_one(selector)
        if date_element:
            date_text = date_element.get_text().strip()
            if date_text and len(date_text) < 50:
                return date_text
    
    # Try to extract from post attributes
    time_element = post.select_one('[datetime]')
    if time_element:
        return time_element.get('datetime', '')[:10]  # Get date part only
    
    return ""

def extract_uploader(post):
    """Extract uploader username"""
    # Look for actual uploader information, not book author
    uploader_selectors = [
        '.uploader', '.posted-by', '.user', '.username', '.sharer',
        '.entry-uploader', '.post-uploader'
    ]
    
    for selector in uploader_selectors:
        uploader_element = post.select_one(selector)
        if uploader_element:
            uploader = uploader_element.get_text().strip()
            # Clean up common prefixes
            uploader = re.sub(r'^(by|posted by|uploaded by|shared by)[:\s]*', '', uploader, flags=re.IGNORECASE)
            if uploader and len(uploader) < 50:
                return uploader
    
    # Look for "Shared by" or "Posted by" patterns in text
    full_text = post.get_text()
    uploader_patterns = [
        r'Shared by[:\s]*([^\n\r]+)',
        r'Posted by[:\s]*([^\n\r]+)',
        r'Uploaded by[:\s]*([^\n\r]+)'
    ]
    
    for pattern in uploader_patterns:
        match = re.search(pattern, full_text, re.IGNORECASE)
        if match:
            uploader = match.group(1).strip()
            if uploader and len(uploader) < 50 and not re.search(r'\d{4}', uploader):  # Avoid dates
                return uploader
    
    return ""

def extract_duration(meta_text):
    """Extract duration/length information"""
    # Only look for explicit duration labels first
    duration_patterns = [
        r'Duration[:\s]+([^\n\r]+?)(?:\s|$)',
        r'Length[:\s]+([^\n\r]+?)(?:\s|$)',  
        r'Runtime[:\s]+([^\n\r]+?)(?:\s|$)',
        r'Playing time[:\s]+([^\n\r]+?)(?:\s|$)'
    ]
    
    for pattern in duration_patterns:
        match = re.search(pattern, meta_text, re.IGNORECASE)
        if match:
            duration = match.group(1).strip()
            # Clean up any trailing punctuation or text
            duration = re.sub(r'[^\d:hm\s]+.*$', '', duration)
            if len(duration) > 2 and len(duration) < 20:
                return duration
    
    # Only look for time formats if explicitly labeled
    time_patterns = [
        r'(?:Duration|Length|Runtime)[:\s]+(\d+:\d+:\d+)',  # Must be labeled
        r'(?:Duration|Length|Runtime)[:\s]+(\d+)h?\s*(\d+)?m?'  # Must be labeled
    ]
    
    for pattern in time_patterns:
        match = re.search(pattern, meta_text, re.IGNORECASE)
        if match:
            if match.group(1) and ':' in match.group(1):
                return match.group(1)
            elif match.group(1) and match.group(2):
                return f"{match.group(1)}h {match.group(2)}m"
            elif match.group(1):
                return f"{match.group(1)}h"
    
    return ""

def extract_publisher(meta_text):
    """Extract publisher information"""
    publisher_patterns = [
        r'Publisher[:\s]+([^\n\r]+)',
        r'Published by[:\s]+([^\n\r]+)',
        r'Imprint[:\s]+([^\n\r]+)',
        r'Label[:\s]+([^\n\r]+)'
    ]
    
    for pattern in publisher_patterns:
        match = re.search(pattern, meta_text, re.IGNORECASE)
        if match:
            publisher = match.group(1).strip()
            # Clean up common suffixes
            publisher = re.sub(r'\s*(,.*|\(.*)', '', publisher)
            if len(publisher) < 100:
                return publisher
    
    return ""

def extract_isbn(meta_text):
    """Extract ISBN information"""
    isbn_patterns = [
        r'ISBN[:\s]+([0-9\-X]{10,17})',
        r'ISBN-?1[03][:\s]+([0-9\-]{10,17})',
        r'\b(97[89][0-9\-]{10,13})\b'
    ]
    
    for pattern in isbn_patterns:
        match = re.search(pattern, meta_text, re.IGNORECASE)
        if match:
            isbn = match.group(1).replace('-', '').replace(' ', '')
            if len(isbn) in [10, 13]:
                return isbn
    
    return ""

def extract_asin(meta_text):
    """Extract ASIN information"""
    asin_patterns = [
        r'ASIN[:\s]+([A-Z0-9]{10})',
        r'Amazon ASIN[:\s]+([A-Z0-9]{10})',
        r'Amazon[:\s]+([A-Z0-9]{10})'
    ]
    
    for pattern in asin_patterns:
        match = re.search(pattern, meta_text, re.IGNORECASE)
        if match:
            asin = match.group(1)
            if len(asin) == 10 and asin.isalnum() and not asin.isalpha():
                return asin
    
    return ""

def check_explicit_content(meta_text):
    """Check for explicit content warnings"""
    explicit_keywords = [
        'explicit', 'adult content', 'mature', '18+', 'adult only',
        'strong language', 'sexual content', 'graphic content'
    ]
    
    text_lower = meta_text.lower()
    for keyword in explicit_keywords:
        if keyword in text_lower:
            return True
    
    return False

def check_abridged(meta_text):
    """Check if content is abridged"""
    abridged_keywords = ['abridged', 'condensed', 'shortened', 'edited']
    unabridged_keywords = ['unabridged', 'complete', 'full version', 'uncut']
    
    text_lower = meta_text.lower()
    
    # Check for unabridged first (more definitive)
    for keyword in unabridged_keywords:
        if keyword in text_lower:
            return False
    
    # Check for abridged
    for keyword in abridged_keywords:
        if keyword in text_lower:
            return True
    
    return False  # Default to not abridged

def extract_narrator(meta_text):
    """Extract narrator information"""
    narrator_patterns = [
        r'(?:narrated by|narrator|read by|voice)[:\s]+([^\n\r,]+)',
        r'(?:reader|performer)[:\s]+([^\n\r,]+)',
        r'(?:voiced by)[:\s]+([^\n\r,]+)'
    ]
    
    for pattern in narrator_patterns:
        match = re.search(pattern, meta_text, re.IGNORECASE)
        if match:
            narrator = match.group(1).strip()
            # Clean up common artifacts
            narrator = re.sub(r'\s+', ' ', narrator)
            narrator = narrator.replace('|', '').strip()
            if len(narrator) > 3 and len(narrator) < 100:  # Reasonable length
                return narrator
    
    return ""

def extract_file_size(meta_text):
    """Extract file size with multiple patterns"""
    size_patterns = [
        r'size[:\s]*(\d+(?:\.\d+)?)\s*(MB|GB|KB)',
        r'(\d+(?:\.\d+)?)\s*(MB|GB|KB)(?:\s|$)',
        r'filesize[:\s]*(\d+(?:\.\d+)?)\s*(MB|GB|KB)'
    ]
    
    for pattern in size_patterns:
        match = re.search(pattern, meta_text, re.IGNORECASE)
        if match:
            size_value = match.group(1)
            size_unit = match.group(2).upper()
            return f"{size_value} {size_unit}"
    
    return ""

def extract_comments(soup):
    """Extract comments from AudiobookBay page"""
    comments = []
    try:
        # Common selectors for comments on AudiobookBay
        comment_selectors = [
            '#comments .comment',
            '.comments .comment',
            '.comment-item',
            '#respond .comment',
            '.comment-list .comment',
            'ol.commentlist li',
            '.wp-block-comments .wp-block-comment',
            'article.comment'
        ]
        
        comment_elements = []
        for selector in comment_selectors:
            found_comments = soup.select(selector)
            if found_comments:
                comment_elements = found_comments
                break
        
        for comment in comment_elements:
            try:
                # Extract comment author
                author_selectors = [
                    '.comment-author .fn',
                    '.comment-author cite',
                    '.comment-author',
                    '.comment-meta .author',
                    'cite.fn',
                    'b.fn',
                    '.author'
                ]
                
                author = ""
                for author_selector in author_selectors:
                    author_elem = comment.select_one(author_selector)
                    if author_elem:
                        author = author_elem.get_text().strip()
                        break
                
                # Extract comment date
                date_selectors = [
                    '.comment-date',
                    '.comment-meta .date',
                    '.comment-time',
                    'time',
                    '.published',
                    '.comment-meta time'
                ]
                
                date = ""
                for date_selector in date_selectors:
                    date_elem = comment.select_one(date_selector)
                    if date_elem:
                        date = date_elem.get_text().strip()
                        break
                
                # Extract comment text
                text_selectors = [
                    '.comment-content p',
                    '.comment-text p',
                    '.comment-body p',
                    '.comment p',
                    '.comment-content',
                    '.comment-text',
                    '.comment-body'
                ]
                
                text_content = ""
                for text_selector in text_selectors:
                    text_elem = comment.select_one(text_selector)
                    if text_elem:
                        text_content = text_elem.get_text().strip()
                        break
                
                # Only add comment if we have both author and content
                if author and text_content and len(text_content) > 10:
                    comments.append({
                        'author': author[:50],  # Limit author name length
                        'date': date[:20] if date else "",  # Limit date length
                        'content': text_content[:500]  # Limit comment length
                    })
                    
            except Exception as e:
                print(f"[DEBUG] Error extracting individual comment: {e}")
                continue
        
        # Limit to maximum 20 comments
        comments = comments[:20]
        print(f"[DEBUG] Extracted {len(comments)} comments")
        return comments
        
    except Exception as e:
        print(f"[DEBUG] Error extracting comments: {e}")
        return []

def extract_torrent_files(soup, content_text):
    """Extract list of files in the torrent"""
    files = []
    
    # Look for file listings
    file_patterns = [
        r'Files?[:\s]*\n((?:.*\.(?:mp3|m4b|m4a|aac|flac|wav|ogg)[^\n]*\n?)+)',
        r'Contents?[:\s]*\n((?:.*\.(?:mp3|m4b|m4a|aac|flac|wav|ogg)[^\n]*\n?)+)',
        r'Track(?:s|list)?[:\s]*\n((?:.*\.(?:mp3|m4b|m4a|aac|flac|wav|ogg)[^\n]*\n?)+)'
    ]
    
    for pattern in file_patterns:
        match = re.search(pattern, content_text, re.IGNORECASE | re.MULTILINE)
        if match:
            file_list = match.group(1).strip()
            # Split into individual files
            potential_files = file_list.split('\n')
            for file_line in potential_files:
                file_line = file_line.strip()
                if file_line and any(ext in file_line.lower() for ext in ['.mp3', '.m4b', '.m4a', '.aac', '.flac', '.wav', '.ogg']):
                    files.append(file_line)
            break
    
    # Also look for file tables in HTML
    file_tables = soup.select('table')
    for table in file_tables:
        rows = table.select('tr')
        for row in rows:
            cells = row.select('td')
            for cell in cells:
                cell_text = cell.get_text().strip()
                if any(ext in cell_text.lower() for ext in ['.mp3', '.m4b', '.m4a', '.aac', '.flac', '.wav', '.ogg']):
                    files.append(cell_text)
    
    return files[:20]  # Limit to first 20 files

def extract_creation_date(soup, content_text):
    """Extract creation/publication date"""
    date_patterns = [
        r'Published[:\s]+([^\n\r]+)',
        r'Created[:\s]+([^\n\r]+)',
        r'Release[:\s]+([^\n\r]+)',
        r'Date[:\s]+([^\n\r]+)',
        r'(\d{4}(?:\-\d{2}\-\d{2})?)',  # Year or full date
        r'©\s*(\d{4})',  # Copyright year
        r'\((\d{4})\)'   # Year in parentheses
    ]
    
    for pattern in date_patterns:
        match = re.search(pattern, content_text, re.IGNORECASE)
        if match:
            date_str = match.group(1).strip()
            # Clean up common suffixes
            date_str = re.sub(r'\s+(by|from|in).*$', '', date_str, flags=re.IGNORECASE)
            if len(date_str) >= 4 and len(date_str) <= 20:
                return date_str
    
    return ""


def scrape_available_categories():
    """Parse categories from the elements file"""
    try:
        # Try to read from elements file first
        with open('elements', 'r', encoding='utf-8') as f:
            html_content = f.read()
        
        soup = BeautifulSoup(html_content, 'html.parser')
        categories = []
        
        # Find Category section (not Category Modifiers)
        category_sections = soup.find_all('h2', string='Category')
        for category_section in category_sections:
            # Find the ul following the h2
            ul = category_section.find_next_sibling('ul')
            if ul:
                for li in ul.find_all('li'):
                    a = li.find('a')
                    if a:
                        category_name = a.get_text().strip()
                        href = a.get('href', '')
                        # Extract key from href
                        category_key = href.split('/')[-2] if href.endswith('/') else href.split('/')[-1]
                        
                        categories.append({
                            'name': category_name,
                            'key': category_key,
                            'url': href,
                            'count': None
                        })
        
        if categories:
            print(f"[INFO] Loaded {len(categories)} categories from elements file")
            # Return just the names for compatibility with existing code
            return [cat['name'] for cat in categories]
        else:
            print("[WARNING] No categories found in elements file, using fallback")
            return get_default_categories()
            
    except Exception as e:
        print(f"[ERROR] Failed to parse categories from elements file: {e}")
        return get_default_categories()

def get_default_categories():
    """AudiobookBay categories from actual website"""
    return [
        '(Post)apocalyptic', 'Action', 'Adventure', 'Art', 'Autobiography & Biographies',
        'Business', 'Computer', 'Contemporary', 'Crime', 'Detective', 'Doctor Who',
        'Education', 'Fantasy', 'General Fiction', 'Historical Fiction', 'History',
        'Horror', 'Humor', 'Lecture', 'LGBT', 'Light Novel', 'Literature', 'LitRPG',
        'Misc. Non-fiction', 'Mystery', 'Philosophy', 'Politics', 'Psychology',
        'Religion & Spirituality', 'Romance', 'Science', 'Sci-Fi', 'Self-Help',
        'Short Story', 'Thriller', 'True Crime', 'Western', 'Young Adult'
    ]

def scrape_available_languages():
    """Parse languages from the elements file"""
    try:
        # Try to read from elements file first
        with open('elements', 'r', encoding='utf-8') as f:
            html_content = f.read()
        
        soup = BeautifulSoup(html_content, 'html.parser')
        languages = []
        
        # Find Popular Language section
        language_sections = soup.find_all('h2', string='Popular Language')
        for language_section in language_sections:
            # Find the ul following the h2
            ul = language_section.find_next_sibling('ul')
            if ul:
                for li in ul.find_all('li'):
                    a = li.find('a')
                    if a:
                        language_name = a.get_text().strip()
                        href = a.get('href', '')
                        # Extract key from href
                        language_key = href.split('/')[-2] if href.endswith('/') else href.split('/')[-1]
                        
                        languages.append({
                            'name': language_name,
                            'key': language_key,
                            'url': href,
                            'count': None
                        })
        
        if languages:
            print(f"[INFO] Loaded {len(languages)} languages from elements file")
            # Return just the names for compatibility with existing code
            return [lang['name'] for lang in languages]
        else:
            print("[WARNING] No languages found in elements file, using fallback")
            return get_default_languages()
            
    except Exception as e:
        print(f"[ERROR] Failed to parse languages from elements file: {e}")
        return get_default_languages()

def get_default_languages():
    """AudiobookBay languages from actual website"""
    return [
        'English', 'Dutch', 'French', 'Spanish', 'German', 'Portuguese'
    ]

# Cache scraped data to avoid repeated requests
_cached_categories = None
_cached_languages = None
_cached_ages = None
_cached_modifiers = None
_cached_hot_searches = None
_cache_timestamp = 0

def get_categories():
    """Get categories with caching (refreshed every hour)"""
    global _cached_categories, _cache_timestamp
    current_time = time.time()
    
    if not _cached_categories or current_time - _cache_timestamp > 3600:  # 1 hour
        _cached_categories = scrape_available_categories()
        _cache_timestamp = current_time
    
    return _cached_categories

def get_languages():
    """Get languages with caching (refreshed every hour)"""
    global _cached_languages, _cache_timestamp
    current_time = time.time()
    
    if not _cached_languages or current_time - _cache_timestamp > 3600:  # 1 hour
        _cached_languages = scrape_available_languages()
        _cache_timestamp = current_time
    
    return _cached_languages

def get_ages():
    """Get age categories with caching (refreshed every hour)"""
    global _cached_ages, _cache_timestamp
    current_time = time.time()
    
    if not _cached_ages or current_time - _cache_timestamp > 3600:  # 1 hour
        _cached_ages = scrape_available_ages()
        _cache_timestamp = current_time
    
    return _cached_ages

def get_modifiers():
    """Get modifiers with caching (refreshed every hour)"""
    global _cached_modifiers, _cache_timestamp
    current_time = time.time()
    
    if not _cached_modifiers or current_time - _cache_timestamp > 3600:  # 1 hour
        _cached_modifiers = scrape_available_modifiers()
        _cache_timestamp = current_time
    
    return _cached_modifiers

def get_hot_searches():
    """Get hot searches with caching (refreshed every hour)"""
    global _cached_hot_searches, _cache_timestamp
    current_time = time.time()
    
    if not _cached_hot_searches or current_time - _cache_timestamp > 3600:  # 1 hour
        _cached_hot_searches = scrape_hot_searches()
        _cache_timestamp = current_time
    
    return _cached_hot_searches

def scrape_hot_searches():
    """Scrape real-time hot searches from AudiobookBay website"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36'
    }
    
    try:
        # Always scrape from live website for real-time data
        response = requests.get(f"http://{ABB_HOSTNAME}", headers=headers, timeout=10)
        if response.status_code != 200:
            print(f"[ERROR] Failed to fetch AudiobookBay homepage for hot searches. Status: {response.status_code}")
            return []  # Return empty list instead of fallback

        soup = BeautifulSoup(response.text, 'html.parser')
        searches = []
        
        # Look for Hot Search section in the sidebar or navigation
        # Try multiple potential selectors for hot search
        hot_search_selectors = [
            'h2:contains("Hot Search") + ul',
            '.hot-search ul',
            '.popular-searches ul',
            '.trending-searches ul',
            '[class*="hot"] ul',
            '[class*="popular"] ul'
        ]
        
        for selector in hot_search_selectors:
            try:
                if 'contains' in selector:
                    # Handle CSS :contains() manually with BeautifulSoup
                    h2_elements = soup.find_all('h2')
                    for h2 in h2_elements:
                        if h2.get_text() and 'hot search' in h2.get_text().lower():
                            ul = h2.find_next_sibling('ul')
                            if ul:
                                for li in ul.find_all('li'):
                                    a = li.find('a')
                                    if a:
                                        search_text = a.get_text().strip()
                                        href = a.get('href', '')
                                        
                                        if search_text and len(search_text) < 100:  # Reasonable search term length
                                            searches.append({
                                                'term': search_text,
                                                'url': href,
                                                'count': None
                                            })
                else:
                    elements = soup.select(selector)
                    for element in elements:
                        for li in element.find_all('li'):
                            a = li.find('a')
                            if a:
                                search_text = a.get_text().strip()
                                href = a.get('href', '')
                                
                                if search_text and len(search_text) < 100:
                                    searches.append({
                                        'term': search_text,
                                        'url': href,
                                        'count': None
                                    })
            except Exception as selector_error:
                continue  # Try next selector
        
        # If we found searches, limit to reasonable number and remove duplicates
        if searches:
            # Remove duplicates by term
            seen = set()
            unique_searches = []
            for search in searches:
                term_lower = search['term'].lower()
                if term_lower not in seen:
                    seen.add(term_lower)
                    unique_searches.append(search)
            
            # Limit to first 20 for UI performance
            unique_searches = unique_searches[:20]
            print(f"[INFO] Successfully scraped {len(unique_searches)} hot searches from live website")
            return unique_searches
        else:
            print("[INFO] No hot searches found on live website")
            return []  # Return empty list instead of fallback
        
    except Exception as e:
        print(f"[ERROR] Failed to scrape hot searches from website: {e}")
        return []  # Return empty list instead of fallback

def scrape_available_ages():
    """Parse age categories from the elements file"""
    try:
        # Try to read from elements file first
        with open('elements', 'r', encoding='utf-8') as f:
            html_content = f.read()
        
        soup = BeautifulSoup(html_content, 'html.parser')
        ages = []
        
        # Find Age section
        age_sections = soup.find_all('h2', string='Age')
        for age_section in age_sections:
            # Find the ul following the h2
            ul = age_section.find_next_sibling('ul')
            if ul:
                for li in ul.find_all('li'):
                    a = li.find('a')
                    if a:
                        age_name = a.get_text().strip()
                        href = a.get('href', '')
                        # Extract key from href
                        age_key = href.split('/')[-2] if href.endswith('/') else href.split('/')[-1]
                        
                        # Skip hidden items
                        if li.get('style') != 'display:none;':
                            ages.append({
                                'name': age_name,
                                'key': age_key,
                                'url': href,
                                'count': None
                            })
        
        if ages:
            print(f"[INFO] Loaded {len(ages)} age categories from elements file: {[a['name'] for a in ages]}")
            return ages
        else:
            print("[WARNING] No ages found in elements file, using fallback")
            return get_default_ages()
            
    except Exception as e:
        print(f"[ERROR] Failed to parse ages from elements file: {e}")
        return get_default_ages()

def get_default_ages():
    """Fallback ages if scraping fails"""
    print("[WARNING] Using fallback ages - scraping failed")
    return [
        {'name': 'Children', 'key': 'children', 'url': '/audio-books/type/children/', 'count': None},
        {'name': 'Teen & Young Adult', 'key': 'teen-young-adult', 'url': '/audio-books/type/teen-young-adult/', 'count': None},
        {'name': 'Adults', 'key': 'adults', 'url': '/audio-books/type/adults/', 'count': None}
    ]

def scrape_available_modifiers():
    """Parse category modifiers from the elements file"""
    try:
        # Try to read from elements file first
        with open('elements', 'r', encoding='utf-8') as f:
            html_content = f.read()
        
        soup = BeautifulSoup(html_content, 'html.parser')
        modifiers = []
        
        # Find Category Modifiers section
        modifier_sections = soup.find_all('h2', string='Category Modifiers')
        for modifier_section in modifier_sections:
            # Find the ul following the h2
            ul = modifier_section.find_next_sibling('ul')
            if ul:
                for li in ul.find_all('li'):
                    a = li.find('a')
                    if a:
                        modifier_name = a.get_text().strip()
                        href = a.get('href', '')
                        # Extract key from href
                        modifier_key = href.split('/')[-2] if href.endswith('/') else href.split('/')[-1]
                        
                        modifiers.append({
                            'name': modifier_name,
                            'key': modifier_key,
                            'url': href,
                            'count': None
                        })
        
        if modifiers:
            print(f"[INFO] Loaded {len(modifiers)} modifiers from elements file: {[m['name'] for m in modifiers]}")
            return modifiers
        else:
            print("[WARNING] No modifiers found in elements file, using fallback")
            return get_default_modifiers()
            
    except Exception as e:
        print(f"[ERROR] Failed to parse modifiers from elements file: {e}")
        return get_default_modifiers()

def get_default_modifiers():
    """Fallback modifiers if scraping fails"""
    print("[WARNING] Using fallback modifiers - scraping failed")
    return [
        {'name': 'Anthology', 'key': 'anthology', 'url': '/audio-books/type/anthology/', 'count': None},
        {'name': 'Bestsellers', 'key': 'bestsellers', 'url': '/audio-books/type/bestsellers/', 'count': None},
        {'name': 'Classic', 'key': 'classic', 'url': '/audio-books/type/classic/', 'count': None},
        {'name': 'Documentary', 'key': 'documentary', 'url': '/audio-books/type/documentary/', 'count': None},
        {'name': 'Full Cast', 'key': 'full-cast', 'url': '/audio-books/type/full-cast/', 'count': None},
        {'name': 'Military', 'key': 'military', 'url': '/audio-books/type/military/', 'count': None},
        {'name': 'Novel', 'key': 'novel', 'url': '/audio-books/type/novel/', 'count': None},
        {'name': 'Short Story', 'key': 'short-story', 'url': '/audio-books/type/short-story/', 'count': None}
    ]


# Authentication routes
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        
        user_data = verify_user_credentials(username, password)
        if user_data:
            user = User(user_data['username'], user_data['type'])
            login_user(user)
            return redirect(url_for('home'))
        else:
            flash('Invalid username or password')
    
    return render_template('login.html')

# Registration removed - users are managed through AudiobookShelf

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

# Home page endpoint
@app.route('/')
@login_required
def home():
    try:
        featured_books = scrape_homepage()
        return render_template('home.html', books=featured_books)
    except Exception as e:
        print(f"[ERROR] Failed to load homepage: {e}")
        return render_template('home.html', books=[], error="Failed to load featured books")

# Endpoint for search page
@app.route('/search', methods=['GET', 'POST'])
@login_required
def search():
    books = []
    query = ""
    category = ""
    try:
        if request.method == 'POST':  # Form submitted
            query = request.form['query']
        elif request.method == 'GET':
            # Check for URL parameters
            query = request.args.get('q', '')
            category = request.args.get('category', '')
        
        if query:
            # Convert to all lowercase
            query = query.lower()
            # Add to search history
            add_to_search_history(current_user.id, query)
            books = search_audiobookbay(query)
            
        return render_template('search.html', books=books, query=query, category=category)
    except Exception as e:
        print(f"[ERROR] Failed to search: {e}")
        return render_template('search.html', books=books, query=query, category=category, error=f"Failed to search. { str(e) }")

# API endpoint for infinite scroll search results
@app.route('/api/search')
@login_required
def api_search():
    query = request.args.get('q', '')
    page = int(request.args.get('page', 1))
    
    if not query:
        return jsonify({'books': [], 'has_more': False})
    
    try:
        books = search_audiobookbay(query.lower(), page)
        has_more = len(books) > 0  # If we got results, there might be more
        
        return jsonify({
            'books': books,
            'has_more': has_more,
            'page': page
        })
    except Exception as e:
        print(f"[ERROR] API search failed: {e}")
        return jsonify({'error': str(e)}), 500

# API endpoint for home page infinite scroll
@app.route('/api/home')
@login_required
def api_home():
    page = int(request.args.get('page', 1))
    
    try:
        books = scrape_homepage_with_pagination(page)
        has_more = len(books) > 0  # If we got results, there might be more
        
        return jsonify({
            'books': books,
            'has_more': has_more,
            'page': page
        })
    except Exception as e:
        print(f"[ERROR] API home failed: {e}")
        return jsonify({'error': str(e)}), 500

# Book details page
@app.route('/book/<path:book_url>')
@login_required
def book_details(book_url):
    try:
        # Decode the URL
        import urllib.parse
        decoded_url = urllib.parse.unquote(book_url)
        
        book_info = get_book_details(decoded_url)
        related_books = []
        if book_info:
            # Get related books from the actual page content
            try:
                # We need to fetch the page again to get the soup for related books
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36'
                }
                response = requests.get(decoded_url, headers=headers, timeout=15)
                if response.status_code == 200:
                    soup = BeautifulSoup(response.text, 'html.parser')
                    related_books = get_related_books_from_page(soup, decoded_url)
            except Exception as e:
                print(f"[ERROR] Failed to get related books: {e}")
                related_books = []
            
            return render_template('book_details.html', book=book_info, related_books=related_books)
        else:
            return render_template('book_details.html', book=None, error="Failed to load book details")
    except Exception as e:
        print(f"[ERROR] Failed to load book details: {e}")
        return render_template('book_details.html', book=None, error="Failed to load book details")




# Endpoint to send magnet link to qBittorrent
@app.route('/send', methods=['POST'])
@login_required
def send():
    data = request.json
    details_url = data.get('link')
    title = data.get('title')
    if not details_url or not title:
        return jsonify({'message': 'Invalid request'}), 400

    try:
        magnet_link = extract_magnet_link(details_url)
        if not magnet_link:
            return jsonify({'message': 'Failed to extract magnet link'}), 500

        save_path = f"{SAVE_PATH_BASE}/{sanitize_title(title)}"
        
        torrent_hash = None
        
        if DOWNLOAD_CLIENT == 'qbittorrent':
            qb = Client(host=DL_HOST, port=DL_PORT, username=DL_USERNAME, password=DL_PASSWORD)
            qb.auth_log_in()
            result = qb.torrents_add(urls=magnet_link, save_path=save_path, category=DL_CATEGORY)
            # Extract hash from magnet link for tracking
            if 'btih:' in magnet_link:
                torrent_hash = magnet_link.split('btih:')[1].split('&')[0]
        elif DOWNLOAD_CLIENT == 'transmission':
            transmission = transmissionrpc(host=DL_HOST, port=DL_PORT, protocol=DL_SCHEME, username=DL_USERNAME, password=DL_PASSWORD)
            result = transmission.add_torrent(magnet_link, download_dir=save_path)
            torrent_hash = result.hashString if hasattr(result, 'hashString') else None
        elif DOWNLOAD_CLIENT == "delugeweb":
            delugeweb = delugewebclient(url=DL_URL, password=DL_PASSWORD)
            delugeweb.login()
            result = delugeweb.add_torrent_magnet(magnet_link, save_directory=save_path, label=DL_CATEGORY)
            torrent_hash = result if isinstance(result, str) else None
        else:
            return jsonify({'message': 'Unsupported download client'}), 400

        # Track the download for the current user
        if torrent_hash:
            add_user_download(current_user.username, torrent_hash, title, details_url)
            print(f"[DEBUG] Tracked download for user {current_user.username}: {title}")

        return jsonify({'message': f'Download added successfully! This may take some time, the download will show in Audiobookshelf when completed.'})
    except Exception as e:
        return jsonify({'message': str(e)}), 500
@app.route('/status')
@login_required
def status():
    try:
        # Get user's download history to filter torrents
        user_downloads = load_user_downloads(current_user.username)
        user_torrent_hashes = {download['hash'] for download in user_downloads}
        
        if DOWNLOAD_CLIENT == 'transmission':
            transmission = transmissionrpc(host=DL_HOST, port=DL_PORT, username=DL_USERNAME, password=DL_PASSWORD)
            torrents = transmission.get_torrents()
            torrent_list = [
                {
                    'hash': torrent.hashString,
                    'name': torrent.name,
                    'progress': round(torrent.progress, 2),
                    'state': torrent.status,
                    'size': f"{torrent.total_size / (1024 * 1024):.2f} MB"
                }
                for torrent in torrents if torrent.hashString in user_torrent_hashes
            ]
            return render_template('status.html', torrents=torrent_list)
        elif DOWNLOAD_CLIENT == 'qbittorrent':
            qb = Client(host=DL_HOST, port=DL_PORT, username=DL_USERNAME, password=DL_PASSWORD)
            qb.auth_log_in()
            torrents = qb.torrents_info(category=DL_CATEGORY)
            torrent_list = [
                {
                    'hash': torrent.hash,
                    'name': torrent.name,
                    'progress': round(torrent.progress * 100, 2),
                    'state': torrent.state,
                    'size': f"{torrent.total_size / (1024 * 1024):.2f} MB"
                }
                for torrent in torrents if torrent.hash in user_torrent_hashes
            ]
            return render_template('status.html', torrents=torrent_list)
        elif DOWNLOAD_CLIENT == "delugeweb":
            delugeweb = delugewebclient(url=DL_URL, password=DL_PASSWORD)
            delugeweb.login()
            torrents = delugeweb.get_torrents_status(
                filter_dict={"label": DL_CATEGORY},
                keys=["name", "state", "progress", "total_size"],
            )
            torrent_list = [
                {
                    "hash": k,
                    "name": torrent["name"],
                    "progress": round(torrent["progress"], 2),
                    "state": torrent["state"],
                    "size": f"{torrent['total_size'] / (1024 * 1024):.2f} MB",
                }
                for k, torrent in torrents.result.items() if k in user_torrent_hashes
            ]
            return render_template('status.html', torrents=torrent_list)
        else:
            return jsonify({'message': 'Unsupported download client'}), 400
    except Exception as e:
        return jsonify({'message': f"Failed to fetch torrent status: {e}"}), 500

# Helper function to browse by category
def browse_category(category, page_num=1):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36'
    }
    results = []
    
    # Category mapping for AudiobookBay search URLs
    category_searches = {
        'fantasy': 'fantasy',
        'mystery': 'mystery', 
        'romance': 'romance',
        'science-fiction': 'science fiction',
        'thriller': 'thriller',
        'biography': 'biography',
        'history': 'history',
        'self-help': 'self help',
        'business': 'business',
        'classic': 'classic',
        'young-adult': 'young adult',
        'children': 'children'
    }
    
    search_term = category_searches.get(category.lower(), 'fantasy')
    url = f"http://{ABB_HOSTNAME}/page/{page_num}/?s={search_term.replace(' ', '+')}&cat=undefined%2Cundefined"
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code != 200:
            print(f"[ERROR] Failed to fetch category {category} page {page_num}. Status Code: {response.status_code}")
            return results

        soup = BeautifulSoup(response.text, 'html.parser')
        results = []
        
        # Extract posts from category results - use same approach as homepage
        post_selectors = ['.post', 'article.post', '.entry', '.postContent']
        posts = []
        
        for selector in post_selectors:
            all_posts = soup.select(selector)[:18]  # Limit to first 18 posts
            if all_posts:
                # Filter out hidden posts with display:none (SAME AS HOMEPAGE)
                posts = [p for p in all_posts if 'display:none' not in str(p)]
                print(f"[INFO] Found {len(all_posts)} posts, {len(posts)} visible using selector '{selector}' for category {category} on page {page_num}")
                break
        
        if not posts:
            print(f"[WARNING] No posts found with any selector for category {category} on page {page_num}")
            return []

        for post in posts:
            try:
                # Try multiple title selectors (EXACT SAME AS HOMEPAGE)
                title_selectors = [
                    '.postTitle > h2 > a',
                    '.postTitle a', 
                    'h2 a',
                    'h3 a',
                    '.entry-title a',
                    'a[rel="bookmark"]'
                ]
                
                title_element = None
                for title_sel in title_selectors:
                    title_element = post.select_one(title_sel)
                    if title_element:
                        break
                
                if not title_element:
                    continue
                    
                title = title_element.text.strip()
                href = title_element.get('href', '')
                
                # Handle relative and absolute URLs (EXACT SAME AS HOMEPAGE)
                if href.startswith('http'):
                    link = href
                elif href.startswith('/'):
                    link = f"http://{ABB_HOSTNAME}{href}"
                else:
                    link = f"http://{ABB_HOSTNAME}/{href}"
                
                # Extract cover image with better selectors (EXACT SAME AS HOMEPAGE)
                cover_selectors = [
                    'img[src*="cover"]',
                    'img[alt*="cover"]', 
                    '.postContent img',
                    'img'
                ]
                
                cover = "/static/images/default_cover.jpg"
                for cover_sel in cover_selectors:
                    cover_element = post.select_one(cover_sel)
                    if cover_element and cover_element.get('src'):
                        cover_src = cover_element['src']
                        if cover_src.startswith('//'):
                            cover = 'http:' + cover_src
                        elif cover_src.startswith('/'):
                            cover = f"http://{ABB_HOSTNAME}{cover_src}"
                        elif cover_src.startswith('http'):
                            cover = cover_src
                        else:
                            cover = f"http://{ABB_HOSTNAME}/{cover_src}"
                        break
                
                # Extract comprehensive metadata using new helper functions (EXACT SAME AS HOMEPAGE)
                meta_info = post.select_one('.postContent, .entry-content, .post-content')
                meta_text = meta_info.get_text() if meta_info else ""
                
                # Extract file size (keep existing pattern for compatibility)
                file_size = ""
                size_match = re.search(r'(\d+(?:\.\d+)?)\s*(MB|GB)', meta_text, re.IGNORECASE)
                if size_match:
                    file_size = f"{size_match.group(1)} {size_match.group(2).upper()}"
                
                # Use new extraction functions for comprehensive metadata (EXACT SAME AS HOMEPAGE)
                book_data = {
                    'title': clean_title(title),
                    'link': link,
                    'cover': cover,
                    'author': extract_author(meta_text, title),
                    'category': extract_category(post, meta_text),
                    'keywords': extract_keywords(meta_text),
                    'language': extract_language(meta_text),
                    'file_format': extract_format(meta_text),
                    'bitrate': extract_bitrate(meta_text),
                    'file_size': file_size,
                    'upload_date': extract_upload_date(post),
                    'duration': extract_duration(meta_text),
                    'publisher': extract_publisher(meta_text),
                    'isbn': extract_isbn(meta_text),
                    'asin': extract_asin(meta_text),
                    'explicit': check_explicit_content(meta_text),
                    'abridged': check_abridged(meta_text),
                    'posted_date': extract_upload_date(post)
                }

                results.append(book_data)
            except Exception as e:
                print(f"[ERROR] Skipping post due to error: {e}")
                continue
                
        print(f"[INFO] Found {len(results)} results for category {category} on page {page_num}")
        return results
        
    except Exception as e:
        print(f"[ERROR] Failed to browse category {category} page {page_num}: {e}")
        return results

# Category browsing page
@app.route('/browse/<category>')
@login_required
def browse_by_category(category):
    # Redirect to search page with category parameter
    return redirect(url_for('search', category=category))

# API endpoint for category browsing with pagination
@app.route('/api/browse/<category>')
@login_required
def api_browse_category(category):
    page = int(request.args.get('page', 1))
    
    try:
        books = browse_category(category, page)
        has_more = len(books) > 0  # If we got results, there might be more
        
        return jsonify({
            'books': books,
            'has_more': has_more,
            'page': page,
            'category': category
        })
    except Exception as e:
        print(f"[ERROR] API category browse failed: {e}")
        return jsonify({'error': str(e)}), 500

# New Browse Section APIs
@app.route('/api/browse/ages')
@login_required
def api_browse_ages():
    try:
        ages = get_ages()
        return jsonify({'ages': ages})
    except Exception as e:
        print(f"[ERROR] API ages failed: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/browse/categories')
@login_required
def api_browse_categories():
    try:
        categories = get_categories()
        # Convert to expected format
        formatted_categories = []
        for category in categories:
            formatted_categories.append({
                'name': category,
                'key': category.lower().replace(' ', '-'),
                'count': None
            })
        return jsonify({'categories': formatted_categories})
    except Exception as e:
        print(f"[ERROR] API categories failed: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/browse/modifiers')
@login_required
def api_browse_modifiers():
    try:
        modifiers = get_modifiers()
        return jsonify({'modifiers': modifiers})
    except Exception as e:
        print(f"[ERROR] API modifiers failed: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/browse/languages')
@login_required
def api_browse_languages():
    try:
        languages = get_languages()
        # Convert to expected format
        formatted_languages = []
        for language in languages:
            formatted_languages.append({
                'name': language,
                'key': language.lower().replace(' ', '-'),
                'count': None
            })
        return jsonify({'languages': formatted_languages})
    except Exception as e:
        print(f"[ERROR] API languages failed: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/browse/hot-search')
@login_required
def api_browse_hot_search():
    try:
        searches = get_hot_searches()
        return jsonify({'searches': searches})
    except Exception as e:
        print(f"[ERROR] API hot search failed: {e}")
        return jsonify({'error': str(e)}), 500

# Dynamic browse endpoints for each section type
@app.route('/api/browse/<section_type>/<item_key>')
@login_required
def api_browse_section_item(section_type, item_key):
    page = int(request.args.get('page', 1))
    
    try:
        books = []
        
        if section_type == 'category':
            # Use existing category browse functionality
            books = browse_category(item_key, page)
        elif section_type == 'age':
            # Search by age-related keywords
            books = search_audiobookbay(item_key, page)
        elif section_type == 'modifier':
            # Search by modifier keywords
            books = search_audiobookbay(item_key, page)
        elif section_type == 'language':
            # Search by language
            books = search_audiobookbay(f"language:{item_key}", page)
        elif section_type == 'search':
            # Direct search for hot search terms
            books = search_audiobookbay(item_key, page)
        else:
            return jsonify({'error': 'Invalid section type'}), 400
            
        has_more = len(books) > 0
        
        return jsonify({
            'books': books,
            'has_more': has_more,
            'page': page,
            'section_type': section_type,
            'item_key': item_key
        })
    except Exception as e:
        print(f"[ERROR] API browse {section_type} {item_key} failed: {e}")
        return jsonify({'error': str(e)}), 500

# Favorites management
@app.route('/api/favorites/add', methods=['POST'])
@login_required
def add_favorite():
    try:
        data = request.get_json()
        
        # Add to database using new function
        success = add_user_favorite(
            current_user.username,
            data.get('title', ''),
            data.get('link', ''),
            data.get('cover', ''),
            data.get('author', '')
        )
        
        if success:
            return jsonify({'message': 'Added to favorites', 'status': 'added'})
        else:
            return jsonify({'message': 'Already in favorites', 'status': 'exists'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/favorites/remove', methods=['POST'])
@login_required
def remove_favorite():
    try:
        data = request.get_json()
        link = data.get('link')
        
        success = remove_user_favorite(current_user.username, link)
        
        if success:
            return jsonify({'message': 'Removed from favorites', 'status': 'removed'})
        else:
            return jsonify({'message': 'Failed to remove from favorites', 'status': 'error'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/favorites')
@login_required
def favorites_page():
    try:
        user_favorites = load_user_favorites(current_user.username)
        return render_template('favorites.html', books=user_favorites)
    except Exception as e:
        print(f"[ERROR] Failed to load favorites: {e}")
        return render_template('favorites.html', books=[], error="Failed to load favorites")

@app.route('/api/search/history')
@login_required
def get_search_history():
    try:
        history = load_search_history()
        user_history = history.get(current_user.id, [])
        return jsonify({'history': user_history[:10]})  # Return last 10 searches
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/favorites/check', methods=['POST'])
@login_required
def check_favorite():
    try:
        data = request.get_json()
        link = data.get('link')
        
        user_favorites = load_user_favorites(current_user.username)
        is_favorite = any(f.get('link') == link for f in user_favorites)
        
        return jsonify({'is_favorite': is_favorite})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Enhanced torrent management endpoints
@app.route('/api/torrent/pause', methods=['POST'])
@login_required
def pause_torrent():
    try:
        data = request.get_json()
        torrent_hash = data.get('hash')
        if not torrent_hash:
            return jsonify({'error': 'Torrent hash required'}), 400

        if DOWNLOAD_CLIENT == 'qbittorrent':
            qb = Client(host=DL_HOST, port=DL_PORT, username=DL_USERNAME, password=DL_PASSWORD)
            qb.auth_log_in()
            qb.torrents_pause(torrent_hashes=torrent_hash)
            return jsonify({'message': 'Torrent paused successfully', 'status': 'paused'})
        
        elif DOWNLOAD_CLIENT == 'transmission':
            transmission = transmissionrpc(host=DL_HOST, port=DL_PORT, username=DL_USERNAME, password=DL_PASSWORD)
            transmission.stop_torrent(torrent_hash)
            return jsonify({'message': 'Torrent paused successfully', 'status': 'paused'})
        
        else:
            return jsonify({'error': 'Unsupported download client'}), 400
            
    except Exception as e:
        return jsonify({'error': f'Failed to pause torrent: {str(e)}'}), 500

@app.route('/api/torrent/resume', methods=['POST'])
@login_required
def resume_torrent():
    try:
        data = request.get_json()
        torrent_hash = data.get('hash')
        if not torrent_hash:
            return jsonify({'error': 'Torrent hash required'}), 400

        if DOWNLOAD_CLIENT == 'qbittorrent':
            qb = Client(host=DL_HOST, port=DL_PORT, username=DL_USERNAME, password=DL_PASSWORD)
            qb.auth_log_in()
            qb.torrents_resume(torrent_hashes=torrent_hash)
            return jsonify({'message': 'Torrent resumed successfully', 'status': 'downloading'})
        
        elif DOWNLOAD_CLIENT == 'transmission':
            transmission = transmissionrpc(host=DL_HOST, port=DL_PORT, username=DL_USERNAME, password=DL_PASSWORD)
            transmission.start_torrent(torrent_hash)
            return jsonify({'message': 'Torrent resumed successfully', 'status': 'downloading'})
        
        else:
            return jsonify({'error': 'Unsupported download client'}), 400
            
    except Exception as e:
        return jsonify({'error': f'Failed to resume torrent: {str(e)}'}), 500

@app.route('/api/torrent/delete', methods=['POST'])
@login_required
def delete_torrent():
    try:
        data = request.get_json()
        torrent_hash = data.get('hash')
        delete_files = data.get('delete_files', False)
        
        if not torrent_hash:
            return jsonify({'error': 'Torrent hash required'}), 400

        if DOWNLOAD_CLIENT == 'qbittorrent':
            qb = Client(host=DL_HOST, port=DL_PORT, username=DL_USERNAME, password=DL_PASSWORD)
            qb.auth_log_in()
            qb.torrents_delete(torrent_hashes=torrent_hash, delete_files=delete_files)
            return jsonify({'message': 'Torrent deleted successfully', 'status': 'deleted'})
        
        elif DOWNLOAD_CLIENT == 'transmission':
            transmission = transmissionrpc(host=DL_HOST, port=DL_PORT, username=DL_USERNAME, password=DL_PASSWORD)
            transmission.remove_torrent(torrent_hash, delete_data=delete_files)
            return jsonify({'message': 'Torrent deleted successfully', 'status': 'deleted'})
        
        else:
            return jsonify({'error': 'Unsupported download client'}), 400
            
    except Exception as e:
        return jsonify({'error': f'Failed to delete torrent: {str(e)}'}), 500

@app.route('/api/torrent/info', methods=['POST'])
@login_required
def get_torrent_info():
    try:
        data = request.get_json()
        torrent_hash = data.get('hash')
        if not torrent_hash:
            return jsonify({'error': 'Torrent hash required'}), 400

        if DOWNLOAD_CLIENT == 'qbittorrent':
            qb = Client(host=DL_HOST, port=DL_PORT, username=DL_USERNAME, password=DL_PASSWORD)
            qb.auth_log_in()
            torrent_info = qb.torrents_info(torrent_hashes=torrent_hash)
            if torrent_info:
                torrent = torrent_info[0]
                return jsonify({
                    'name': torrent.name,
                    'progress': round(torrent.progress * 100, 2),
                    'state': torrent.state,
                    'size': torrent.total_size,
                    'downloaded': torrent.downloaded,
                    'upload_speed': torrent.upspeed,
                    'download_speed': torrent.dlspeed,
                    'eta': torrent.eta,
                    'ratio': torrent.ratio
                })
            else:
                return jsonify({'error': 'Torrent not found'}), 404
        
        elif DOWNLOAD_CLIENT == 'transmission':
            transmission = transmissionrpc(host=DL_HOST, port=DL_PORT, username=DL_USERNAME, password=DL_PASSWORD)
            torrent = transmission.get_torrent(torrent_hash)
            return jsonify({
                'name': torrent.name,
                'progress': round(torrent.progress, 2),
                'state': torrent.status,
                'size': torrent.total_size,
                'downloaded': torrent.downloaded_ever,
                'upload_speed': torrent.rate_upload,
                'download_speed': torrent.rate_download,
                'eta': torrent.eta,
                'ratio': torrent.ratio
            })
        
        else:
            return jsonify({'error': 'Unsupported download client'}), 400
            
    except Exception as e:
        return jsonify({'error': f'Failed to get torrent info: {str(e)}'}), 500


@app.route('/api/torrent/status', methods=['GET'])
@login_required
def get_torrent_status():
    """Get status of all active torrents for real-time updates"""
    try:
        torrents_data = []
        
        if DOWNLOAD_CLIENT == 'qbittorrent':
            qb = Client(host=DL_HOST, port=DL_PORT, username=DL_USERNAME, password=DL_PASSWORD)
            torrents = qb.torrents_info()
            
            for torrent in torrents:
                # Format size properly
                size_bytes = torrent.get('size', 0)
                if size_bytes > 0:
                    size_formatted = format_bytes(size_bytes)
                else:
                    size_formatted = 'Unknown'
                
                torrents_data.append({
                    'hash': torrent['hash'],
                    'name': torrent['name'],
                    'progress': round(torrent['progress'] * 100, 1),
                    'state': torrent['state'].replace('_', ' ').title(),
                    'size': size_formatted
                })
        
        elif DOWNLOAD_CLIENT == 'transmission':
            transmission = transmissionrpc(host=DL_HOST, port=DL_PORT, username=DL_USERNAME, password=DL_PASSWORD)
            torrents = transmission.get_torrents()
            
            for torrent in torrents:
                # Format size properly  
                size_bytes = torrent.total_size if hasattr(torrent, 'total_size') else 0
                if size_bytes > 0:
                    size_formatted = format_bytes(size_bytes)
                else:
                    size_formatted = 'Unknown'
                
                torrents_data.append({
                    'hash': torrent.hashString,
                    'name': torrent.name,
                    'progress': round(torrent.progress * 100, 1),
                    'state': torrent.status.replace('_', ' ').title(),
                    'size': size_formatted
                })
        
        else:
            return jsonify({'error': 'Unsupported download client'}), 400
            
        return jsonify({'torrents': torrents_data})
        
    except Exception as e:
        return jsonify({'error': f'Failed to get torrent status: {str(e)}'}), 500


def format_bytes(bytes_value):
    """Convert bytes to human readable format"""
    if bytes_value == 0:
        return '0 B'
    
    sizes = ['B', 'KB', 'MB', 'GB', 'TB']
    i = 0
    while bytes_value >= 1024 and i < len(sizes) - 1:
        bytes_value /= 1024.0
        i += 1
    
    return f"{bytes_value:.1f} {sizes[i]}"


# Auto-stop seeding functionality
def auto_stop_completed_torrents():
    """Background service to automatically pause torrents when they complete"""
    while True:
        try:
            # Check if auto-stop is enabled
            if not AUTO_STOP_ENABLED:
                time.sleep(60)
                continue
                
            if DOWNLOAD_CLIENT == 'qbittorrent':
                qb = Client(host=DL_HOST, port=DL_PORT, username=DL_USERNAME, password=DL_PASSWORD)
                torrents = qb.torrents_info()
                
                for torrent in torrents:
                    # Check if torrent is completed (100% progress) and currently seeding
                    if torrent['progress'] >= 1.0 and torrent['state'].lower() in ['uploading', 'seeding', 'stalledup']:
                        try:
                            qb.torrents_pause(hashes=torrent['hash'])
                            print(f"[AUTO-STOP] Paused completed torrent: {torrent['name']}")
                        except Exception as e:
                            print(f"[ERROR] Failed to auto-pause torrent {torrent['name']}: {e}")
            
            elif DOWNLOAD_CLIENT == 'transmission':
                transmission = transmissionrpc(host=DL_HOST, port=DL_PORT, username=DL_USERNAME, password=DL_PASSWORD)
                torrents = transmission.get_torrents()
                
                for torrent in torrents:
                    # Check if torrent is completed and currently seeding
                    if torrent.progress >= 1.0 and torrent.status.lower() in ['seed', 'seed_wait']:
                        try:
                            transmission.stop_torrent(torrent.hashString)
                            print(f"[AUTO-STOP] Stopped completed torrent: {torrent.name}")
                        except Exception as e:
                            print(f"[ERROR] Failed to auto-stop torrent {torrent.name}: {e}")
            
        except Exception as e:
            print(f"[ERROR] Auto-stop service error: {e}")
        
        # Check every 60 seconds
        time.sleep(60)

# Global flag to control auto-stop service
AUTO_STOP_ENABLED = True

def start_auto_stop_service():
    """Start the auto-stop seeding service in a background thread"""
    if DOWNLOAD_CLIENT and DOWNLOAD_CLIENT != 'none':
        auto_stop_thread = threading.Thread(target=auto_stop_completed_torrents, daemon=True)
        auto_stop_thread.start()
        print("[INFO] Auto-stop seeding service started")
    else:
        print("[INFO] Auto-stop service disabled - no download client configured")

@app.route('/api/settings/auto-stop', methods=['GET', 'POST'])
@login_required
def auto_stop_settings():
    """Get or update auto-stop seeding settings"""
    global AUTO_STOP_ENABLED
    
    if request.method == 'POST':
        data = request.get_json()
        if 'enabled' in data:
            AUTO_STOP_ENABLED = data['enabled']
            status = "enabled" if AUTO_STOP_ENABLED else "disabled" 
            return jsonify({
                'success': True,
                'message': f'Auto-stop seeding {status}',
                'enabled': AUTO_STOP_ENABLED
            })
        else:
            return jsonify({'error': 'Missing enabled parameter'}), 400
    
    return jsonify({'enabled': AUTO_STOP_ENABLED})


# Context processor temporarily disabled to avoid performance issues
# @app.context_processor 
# def sidebar_context():
#     # Will be implemented properly later with actual scraped data
#     return {}

# Language browsing endpoint
@app.route('/browse/language/<language>')
@login_required
def browse_by_language(language):
    try:
        # For now, use search with language filter - can be enhanced later
        books = search_audiobookbay(f"language:{language}")
        language_name = language.replace('-', ' ').title()
        return render_template('category.html', books=books, category=language, category_name=f"{language_name} Audiobooks")
    except Exception as e:
        print(f"[ERROR] Failed to browse language {language}: {e}")
        return render_template('category.html', books=[], category=language, error=f"Failed to load {language} books")

# Popular books endpoint
@app.route('/popular')
@login_required
def popular_books():
    try:
        books = scrape_homepage()  # Use homepage as popular books proxy
        return render_template('category.html', books=books, category='popular', category_name="Popular Books")
    except Exception as e:
        print(f"[ERROR] Failed to load popular books: {e}")
        return render_template('category.html', books=[], category='popular', error="Failed to load popular books")

# Recent books endpoint
@app.route('/recent')
@login_required
def recent_books():
    try:
        books = scrape_homepage_with_pagination(1)  # Get most recent from homepage
        return render_template('category.html', books=books, category='recent', category_name="Recent Books")
    except Exception as e:
        print(f"[ERROR] Failed to load recent books: {e}")
        return render_template('category.html', books=[], category='recent', error="Failed to load recent books")

# Admin dashboard routes (root users only)
@app.route('/admin')
@login_required
def admin_dashboard():
    try:
        if not is_admin_user(current_user.username):
            return render_template('403.html'), 403
        
        # Get all users and their stats
        all_users = get_all_users()
        download_counts = get_all_user_downloads()
        
        # Get favorites count for each user
        favorites_counts = {}
        for user in all_users:
            user_favorites = load_user_favorites(user['username'])
            favorites_counts[user['username']] = len(user_favorites)
        
        # Merge data
        for user in all_users:
            username = user['username']
            user['download_count'] = download_counts.get(username, 0)
            user['favorites_count'] = favorites_counts.get(username, 0)
        
        return render_template('admin.html', users=all_users)
    except Exception as e:
        print(f"[ERROR] Failed to load admin dashboard: {e}")
        return render_template('admin.html', users=[], error="Failed to load admin dashboard")

@app.route('/admin/downloads')
@login_required 
def admin_downloads():
    try:
        if not is_admin_user(current_user.username):
            return render_template('403.html'), 403
        
        detailed_downloads = get_detailed_user_downloads()
        
        # Calculate today's downloads
        from datetime import datetime
        today = datetime.now().strftime('%Y-%m-%d')
        today_downloads = [d for d in detailed_downloads if d.get('created_at', '').startswith(today)]
        
        return render_template('admin_downloads.html', 
                             downloads=detailed_downloads, 
                             today_count=len(today_downloads))
    except Exception as e:
        print(f"[ERROR] Failed to load admin downloads: {e}")
        return render_template('admin_downloads.html', downloads=[], error="Failed to load download data", today_count=0)

@app.route('/admin/status')
@login_required
def admin_status():
    try:
        if not is_admin_user(current_user.username):
            return render_template('403.html'), 403
        
        # Get ALL torrents for admin view (no user filtering)
        if DOWNLOAD_CLIENT == 'transmission':
            transmission = transmissionrpc(host=DL_HOST, port=DL_PORT, username=DL_USERNAME, password=DL_PASSWORD)
            torrents = transmission.get_torrents()
            torrent_list = [
                {
                    'hash': torrent.hashString,
                    'name': torrent.name,
                    'progress': round(torrent.progress, 2),
                    'state': torrent.status,
                    'size': f"{torrent.total_size / (1024 * 1024):.2f} MB"
                }
                for torrent in torrents
            ]
        elif DOWNLOAD_CLIENT == 'qbittorrent':
            qb = Client(host=DL_HOST, port=DL_PORT, username=DL_USERNAME, password=DL_PASSWORD)
            qb.auth_log_in()
            torrents = qb.torrents_info(category=DL_CATEGORY)
            torrent_list = [
                {
                    'hash': torrent.hash,
                    'name': torrent.name,
                    'progress': round(torrent.progress * 100, 2),
                    'state': torrent.state,
                    'size': f"{torrent.total_size / (1024 * 1024):.2f} MB"
                }
                for torrent in torrents
            ]
        elif DOWNLOAD_CLIENT == "delugeweb":
            delugeweb = delugewebclient(url=DL_URL, password=DL_PASSWORD)
            delugeweb.login()
            torrents = delugeweb.get_torrents_status(
                filter_dict={"label": DL_CATEGORY},
                keys=["name", "state", "progress", "total_size"],
            )
            torrent_list = [
                {
                    "hash": k,
                    "name": torrent["name"],
                    "progress": round(torrent["progress"], 2),
                    "state": torrent["state"],
                    "size": f"{torrent['total_size'] / (1024 * 1024):.2f} MB",
                }
                for k, torrent in torrents.result.items()
            ]
        else:
            return jsonify({'message': 'Unsupported download client'}), 400
            
        # Add user ownership info to torrents
        detailed_downloads = get_detailed_user_downloads()
        hash_to_user = {d['torrent_hash']: d['user_id'] for d in detailed_downloads}
        
        for torrent in torrent_list:
            torrent['owner'] = hash_to_user.get(torrent['hash'], 'Unknown')
        
        return render_template('admin_status.html', torrents=torrent_list)
    except Exception as e:
        print(f"[ERROR] Failed to load admin status: {e}")
        return render_template('admin_status.html', torrents=[], error="Failed to load torrent status")

if __name__ == '__main__':
    # Start auto-stop seeding service
    start_auto_stop_service()
    
    app.run(host='0.0.0.0', port=5078)
