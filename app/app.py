import os, re, requests, hashlib, time
from flask import Flask, request, render_template, jsonify, redirect, url_for, flash, session
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from bs4 import BeautifulSoup
from qbittorrentapi import Client
from transmission_rpc import Client as transmissionrpc
from deluge_web_client import DelugeWebClient as delugewebclient
from dotenv import load_dotenv
from urllib.parse import urlparse
import json

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

# User management
USERS_FILE = 'users.json'

class User(UserMixin):
    def __init__(self, username):
        self.id = username

def load_users():
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_users(users):
    with open(USERS_FILE, 'w') as f:
        json.dump(users, f)

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

# Search history and favorites management
SEARCH_HISTORY_FILE = 'search_history.json'
FAVORITES_FILE = 'favorites.json'

def load_search_history():
    if os.path.exists(SEARCH_HISTORY_FILE):
        with open(SEARCH_HISTORY_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_search_history(history):
    with open(SEARCH_HISTORY_FILE, 'w') as f:
        json.dump(history, f)

def load_favorites():
    if os.path.exists(FAVORITES_FILE):
        with open(FAVORITES_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_favorites(favorites):
    with open(FAVORITES_FILE, 'w') as f:
        json.dump(favorites, f)

def add_to_search_history(username, query):
    if not query or len(query.strip()) == 0:
        return
    
    history = load_search_history()
    if username not in history:
        history[username] = []
    
    # Remove query if it already exists
    history[username] = [h for h in history[username] if h.get('query', '').lower() != query.lower()]
    
    # Add to beginning of list
    history[username].insert(0, {
        'query': query,
        'timestamp': int(time.time())
    })
    
    # Keep only last 50 searches
    history[username] = history[username][:50]
    save_search_history(history)

@login_manager.user_loader
def load_user(user_id):
    users = load_users()
    if user_id in users:
        return User(user_id)
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
        posts = soup.select('.post')
        
        if not posts:
            print(f"[INFO] No more results found on page {page_num}")
            return results

        for post in posts:
            try:
                title_element = post.select_one('.postTitle > h2 > a')
                if not title_element:
                    continue
                
                title = title_element.text.strip()
                link = f"https://{ABB_HOSTNAME}{title_element['href']}"
                cover = post.select_one('img')['src'] if post.select_one('img') else "/static/images/default_cover.jpg"
                
                # Extract additional metadata for better display
                meta_info = post.select_one('.postContent')
                author = ""
                file_size = ""
                
                if meta_info:
                    meta_text = meta_info.get_text()
                    # Try to extract author
                    if "by " in meta_text.lower():
                        author_match = re.search(r'by\s+([^,\n]+)', meta_text, re.IGNORECASE)
                        if author_match:
                            author = author_match.group(1).strip()
                    
                    # Try to extract file size
                    size_match = re.search(r'(\d+(?:\.\d+)?)\s*(MB|GB)', meta_text, re.IGNORECASE)
                    if size_match:
                        file_size = f"{size_match.group(1)} {size_match.group(2).upper()}"

                results.append({
                    'title': title, 
                    'link': link, 
                    'cover': cover,
                    'author': author,
                    'file_size': file_size
                })
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
        # Try different URL schemes - audiobookbay.is might redirect
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
            posts = soup.select(selector)[:18]  # Limit to first 18 posts
            if posts:
                print(f"[INFO] Found {len(posts)} posts using selector '{selector}'")
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
                
                # Extract metadata
                meta_info = post.select_one('.postContent, .entry-content, .post-content')
                author = ""
                category = ""
                file_size = ""
                posted_date = ""
                
                if meta_info:
                    meta_text = meta_info.get_text()
                    # Try to extract author (usually after "by" or "Author:")
                    if "by " in meta_text.lower():
                        author_match = re.search(r'by\s+([^,\n]+)', meta_text, re.IGNORECASE)
                        if author_match:
                            author = author_match.group(1).strip()
                    
                    # Try to extract file size
                    size_match = re.search(r'(\d+(?:\.\d+)?)\s*(MB|GB)', meta_text, re.IGNORECASE)
                    if size_match:
                        file_size = f"{size_match.group(1)} {size_match.group(2).upper()}"

                results.append({
                    'title': title,
                    'link': link,
                    'cover': cover,
                    'author': author,
                    'category': category,
                    'file_size': file_size,
                    'posted_date': posted_date
                })
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
        # Try different URL schemes - audiobookbay.is might redirect
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
            posts = soup.select(selector)  # Get all posts on the page for infinite scroll
            if posts:
                print(f"[INFO] Found {len(posts)} posts using selector '{selector}' on page {page_num}")
                break
        
        if not posts:
            print(f"[WARNING] No posts found with any selector on page {page_num}")
            return []
        
        for post in posts:
            try:
                # Try multiple title selectors
                title_selectors = [
                    '.postTitle > h2 > a',
                    '.postTitle a', 
                    'h2 a',
                    '.entry-title a',
                    'a[title]'
                ]
                
                title_element = None
                for selector in title_selectors:
                    title_element = post.select_one(selector)
                    if title_element:
                        break
                
                if not title_element:
                    continue
                
                title = title_element.get('title', '').strip() or title_element.get_text().strip()
                link = title_element.get('href', '')
                
                if not title or not link:
                    continue
                
                # Make link absolute if needed
                if link.startswith('/'):
                    link = f"http://{ABB_HOSTNAME}{link}"
                elif not link.startswith('http'):
                    link = f"http://{ABB_HOSTNAME}/{link}"
                
                # Try to find cover image
                cover = "/static/images/default_cover.jpg"
                img_selectors = ['img.alignleft', '.postContent img', '.entry-content img', 'img']
                for selector in img_selectors:
                    img = post.select_one(selector)
                    if img and img.get('src'):
                        cover_url = img.get('src')
                        if cover_url.startswith('/'):
                            cover = f"http://{ABB_HOSTNAME}{cover_url}"
                        elif cover_url.startswith('http'):
                            cover = cover_url
                        break
                
                # Initialize metadata
                author = ""
                category = ""
                file_size = ""
                posted_date = ""
                
                # Try to extract metadata from post content
                meta_selectors = ['.postContent', '.entry-content', '.post-content']
                meta_info = None
                for selector in meta_selectors:
                    meta_info = post.select_one(selector)
                    if meta_info:
                        break
                
                if meta_info:
                    meta_text = meta_info.get_text()
                    # Try to extract author (usually after "by" or "Author:")
                    if "by " in meta_text.lower():
                        author_match = re.search(r'by\s+([^,\n]+)', meta_text, re.IGNORECASE)
                        if author_match:
                            author = author_match.group(1).strip()
                    
                    # Try to extract file size
                    size_match = re.search(r'(\d+(?:\.\d+)?)\s*(MB|GB)', meta_text, re.IGNORECASE)
                    if size_match:
                        file_size = f"{size_match.group(1)} {size_match.group(2).upper()}"

                results.append({
                    'title': title,
                    'link': link,
                    'cover': cover,
                    'author': author,
                    'category': category,
                    'file_size': file_size,
                    'posted_date': posted_date
                })
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
        posted_by = ""
        posted_date = ""
        categories = []
        download_links = []
        
        if content_element:
            content_text = content_element.get_text()
            content_html = str(content_element)
            
            # Extract author from title or content - AudiobookBay often has "Author - Title" format
            if " - " in title and not author:
                parts = title.split(" - ", 1)
                if len(parts) == 2:
                    potential_author = parts[0].strip()
                    if not re.search(r'\d', potential_author):  # Avoid numbers (like "B02")
                        author = potential_author
                        title = parts[1].strip()
            
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
                r'Narrator[:\s]+([^\n\r]+)',
                r'Narrated by[:\s]+([^\n\r]+)',
                r'Read by[:\s]+([^\n\r]+)',
                r'Voice[:\s]+([^\n\r]+)'
            ]
            for pattern in narrator_patterns:
                match = re.search(pattern, content_text, re.IGNORECASE)
                if match:
                    narrator = match.group(1).strip()
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
            
            # Posted by and date
            posted_by_match = re.search(r'Posted by[:\s]+([^\n\r]+)', content_text, re.IGNORECASE)
            if posted_by_match:
                posted_by = posted_by_match.group(1).strip()
                
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
                        description = cleaned[:300] + '...' if len(cleaned) > 300 else cleaned
                        break
        
        return {
            'title': title,
            'author': author,
            'narrator': narrator,
            'cover': cover,
            'description': description,
            'duration': duration,
            'file_format': file_format,
            'file_size': file_size,
            'bitrate': bitrate,
            'language': language,
            'posted_by': posted_by,
            'posted_date': posted_date,
            'categories': categories,
            'category': ', '.join(categories) if categories else '',
            'original_url': book_url
        }
        
    except Exception as e:
        print(f"[ERROR] Failed to extract book details: {e}")
        return None

# Helper function to sanitize titles
def sanitize_title(title):
    return re.sub(r'[<>:"/\\|?*]', '', title).strip()

# Authentication routes
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        users = load_users()
        
        if username in users and users[username] == hash_password(password):
            user = User(username)
            login_user(user)
            return redirect(url_for('search'))
        else:
            flash('Invalid username or password')
    
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        users = load_users()
        
        if username in users:
            flash('Username already exists')
        else:
            users[username] = hash_password(password)
            save_users(users)
            flash('Registration successful')
            return redirect(url_for('login'))
    
    return render_template('register.html')

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
    try:
        if request.method == 'POST':  # Form submitted
            query = request.form['query']
        elif request.method == 'GET' and request.args.get('q'):  # URL parameter from home page
            query = request.args.get('q')
        
        if query:
            #Convert to all lowercase
            query = query.lower()
            # Add to search history
            add_to_search_history(current_user.id, query)
            books = search_audiobookbay(query)
            
        return render_template('search.html', books=books, query=query)
    except Exception as e:
        print(f"[ERROR] Failed to search: {e}")
        return render_template('search.html', books=books, query=query, error=f"Failed to search. { str(e) }")

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
        if book_info:
            return render_template('book_details.html', book=book_info)
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
        
        if DOWNLOAD_CLIENT == 'qbittorrent':
            qb = Client(host=DL_HOST, port=DL_PORT, username=DL_USERNAME, password=DL_PASSWORD)
            qb.auth_log_in()
            qb.torrents_add(urls=magnet_link, save_path=save_path, category=DL_CATEGORY)
        elif DOWNLOAD_CLIENT == 'transmission':
            transmission = transmissionrpc(host=DL_HOST, port=DL_PORT, protocol=DL_SCHEME, username=DL_USERNAME, password=DL_PASSWORD)
            transmission.add_torrent(magnet_link, download_dir=save_path)
        elif DOWNLOAD_CLIENT == "delugeweb":
            delugeweb = delugewebclient(url=DL_URL, password=DL_PASSWORD)
            delugeweb.login()
            delugeweb.add_torrent_magnet(magnet_link, save_directory=save_path, label=DL_CATEGORY)
        else:
            return jsonify({'message': 'Unsupported download client'}), 400

        return jsonify({'message': f'Download added successfully! This may take some time, the download will show in Audiobookshelf when completed.'})
    except Exception as e:
        return jsonify({'message': str(e)}), 500
@app.route('/status')
@login_required
def status():
    try:
        if DOWNLOAD_CLIENT == 'transmission':
            transmission = transmissionrpc(host=DL_HOST, port=DL_PORT, username=DL_USERNAME, password=DL_PASSWORD)
            torrents = transmission.get_torrents()
            torrent_list = [
                {
                    'name': torrent.name,
                    'progress': round(torrent.progress, 2),
                    'state': torrent.status,
                    'size': f"{torrent.total_size / (1024 * 1024):.2f} MB"
                }
                for torrent in torrents
            ]
            return render_template('status.html', torrents=torrent_list)
        elif DOWNLOAD_CLIENT == 'qbittorrent':
            qb = Client(host=DL_HOST, port=DL_PORT, username=DL_USERNAME, password=DL_PASSWORD)
            qb.auth_log_in()
            torrents = qb.torrents_info(category=DL_CATEGORY)
            torrent_list = [
                {
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
                    "name": torrent["name"],
                    "progress": round(torrent["progress"], 2),
                    "state": torrent["state"],
                    "size": f"{torrent['total_size'] / (1024 * 1024):.2f} MB",
                }
                for k, torrent in torrents.result.items()
            ]
        else:
            return jsonify({'message': 'Unsupported download client'}), 400
        return render_template('status.html', torrents=torrent_list)
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
        posts = soup.select('.post')
        
        if not posts:
            print(f"[INFO] No more results found for category {category} on page {page_num}")
            return results

        for post in posts:
            try:
                title_element = post.select_one('.postTitle > h2 > a')
                if not title_element:
                    continue
                
                title = title_element.text.strip()
                link = f"https://{ABB_HOSTNAME}{title_element['href']}"
                cover = post.select_one('img')['src'] if post.select_one('img') else "/static/images/default_cover.jpg"
                
                # Extract additional metadata
                meta_info = post.select_one('.postContent')
                author = ""
                file_size = ""
                
                if meta_info:
                    meta_text = meta_info.get_text()
                    # Try to extract author
                    if "by " in meta_text.lower():
                        author_match = re.search(r'by\s+([^,\n]+)', meta_text, re.IGNORECASE)
                        if author_match:
                            author = author_match.group(1).strip()
                    
                    # Try to extract file size
                    size_match = re.search(r'(\d+(?:\.\d+)?)\s*(MB|GB)', meta_text, re.IGNORECASE)
                    if size_match:
                        file_size = f"{size_match.group(1)} {size_match.group(2).upper()}"

                results.append({
                    'title': title, 
                    'link': link, 
                    'cover': cover,
                    'author': author,
                    'file_size': file_size,
                    'category': category
                })
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
    try:
        books = browse_category(category)
        category_name = category.replace('-', ' ').title()
        return render_template('category.html', books=books, category=category, category_name=category_name)
    except Exception as e:
        print(f"[ERROR] Failed to browse category {category}: {e}")
        return render_template('category.html', books=[], category=category, error=f"Failed to load {category} books")

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

# Favorites management
@app.route('/api/favorites/add', methods=['POST'])
@login_required
def add_favorite():
    try:
        data = request.get_json()
        book_data = {
            'title': data.get('title'),
            'author': data.get('author', ''),
            'cover': data.get('cover', ''),
            'link': data.get('link'),
            'file_size': data.get('file_size', ''),
            'timestamp': int(time.time())
        }
        
        favorites = load_favorites()
        if current_user.id not in favorites:
            favorites[current_user.id] = []
        
        # Check if already in favorites
        existing = [f for f in favorites[current_user.id] if f.get('link') == book_data['link']]
        if existing:
            return jsonify({'message': 'Already in favorites', 'status': 'exists'})
        
        favorites[current_user.id].insert(0, book_data)
        # Keep only last 100 favorites
        favorites[current_user.id] = favorites[current_user.id][:100]
        save_favorites(favorites)
        
        return jsonify({'message': 'Added to favorites', 'status': 'added'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/favorites/remove', methods=['POST'])
@login_required
def remove_favorite():
    try:
        data = request.get_json()
        link = data.get('link')
        
        favorites = load_favorites()
        if current_user.id in favorites:
            favorites[current_user.id] = [f for f in favorites[current_user.id] if f.get('link') != link]
            save_favorites(favorites)
        
        return jsonify({'message': 'Removed from favorites', 'status': 'removed'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/favorites')
@login_required
def favorites_page():
    try:
        favorites = load_favorites()
        user_favorites = favorites.get(current_user.id, [])
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
        
        favorites = load_favorites()
        user_favorites = favorites.get(current_user.id, [])
        is_favorite = any(f.get('link') == link for f in user_favorites)
        
        return jsonify({'is_favorite': is_favorite})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5078)
