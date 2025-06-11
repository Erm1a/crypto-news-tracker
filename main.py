import requests
import psycopg2
from datetime import datetime, timedelta
import time
from typing import List, Dict, Set
from flask import Flask, render_template, request, jsonify
import json
import os
from dotenv import load_dotenv
import re
from functools import lru_cache
import threading

# Load environment variables
load_dotenv(override=True)

# API Keys
COINDESK_API_KEY = os.getenv('COINDESK_API_KEY')
MESSARI_API_KEY = os.getenv('MESSARI_API_KEY', '')  # Optional for free tier

# Validate required API keys
if not COINDESK_API_KEY:
    raise ValueError("COINDESK_API_KEY environment variable is not set")

# News API Endpoints
NEWS_SOURCES = {
    "coindesk": {
        "url": "https://data-api.coindesk.com/news/v1/article/list",
        "headers": {'Authorization': f'Bearer {COINDESK_API_KEY}'},
        "parser": "coindesk"
    }
}

COINGECKO_API_URL = "https://api.coingecko.com/api/v3"
MESSARI_API_URL = "https://data.messari.io/api/v1"
COINDESK_PRICE_API_URL = "https://api.coindesk.com/v1"

# Map CoinGecko IDs to Messari IDs
COIN_SYMBOL_MAP = {
    "bitcoin": "bitcoin",
    "ethereum": "ethereum",
    "ripple": "ripple",
    "dogecoin": "dogecoin",
    "solana": "solana",
    "tron": "tron",
    "tether": "tether"
}

TRACKED_COINS = {
    "bitcoin": "BTC",
    "ethereum": "ETH",
    "ripple": "XRP",
    "dogecoin": "DOGE",
    "solana": "SOL",
    "tron": "TRX",
    "tether": "USDT"
}

app = Flask(__name__)

# Database Config
DB_CONFIG = {
    "dbname": os.getenv('DB_NAME'),
    "user": os.getenv('DB_USER'),
    "password": os.getenv('DB_PASSWORD'),
    "host": os.getenv('DB_HOST'),
    "port": os.getenv('DB_PORT')
}

# Validate required database configuration
required_db_vars = ['DB_NAME', 'DB_USER', 'DB_PASSWORD', 'DB_HOST', 'DB_PORT']
missing_vars = [var for var in required_db_vars if not os.getenv(var)]
if missing_vars:
    raise ValueError(f"Missing required database environment variables: {', '.join(missing_vars)}")

# Global price cache with thread safety
price_cache = {}
price_cache_lock = threading.Lock()
last_price_update = {}

def get_db_connection():
    conn = psycopg2.connect(**DB_CONFIG)
    conn.set_client_encoding('UTF8')
    conn.autocommit = True
    return conn

@lru_cache(maxsize=100)
def get_cached_price(coin_id: str) -> Dict:
    """Get cached price data with thread safety"""
    with price_cache_lock:
        if coin_id in price_cache and coin_id in last_price_update:
            time_diff = (datetime.now() - last_price_update[coin_id]).total_seconds()
            if time_diff < 300:  # 5 minutes cache
                return price_cache[coin_id]
    return None

def update_price_cache(coin_id: str, price_data: Dict):
    """Update price cache with thread safety"""
    with price_cache_lock:
        price_cache[coin_id] = price_data
        last_price_update[coin_id] = datetime.now()

class CryptoNewsTracker:
    def __init__(self, db_config: Dict[str, str]):
        self.db_config = db_config
        self.conn = None
        self.cursor = None
        self.price_cache = {}
        self.last_price_update = {}

    def connect_db(self):
        """Establish database connection"""
        try:
            print(f"Attempting to connect to database with config: {self.db_config}")
            self.conn = psycopg2.connect(
                **self.db_config,
                client_encoding='utf8'  # Add this line to handle UTF-8 encoding
            )
            self.cursor = self.conn.cursor()
            print("Successfully connected to database")
        except Exception as e:
            print(f"Error connecting to database: {e}")
            raise

    def close_db(self):
        """Close database connection"""
        if self.cursor:
            self.cursor.close()
        if self.conn:
            self.conn.close()

    def fetch_news(self, start_date: datetime = None, end_date: datetime = None) -> List[Dict]:
        """Fetch latest news from CoinDesk within a specified date range"""
        all_news = []
        
        # Calculate date range if not provided
        if end_date is None:
            end_date = datetime.now()
        if start_date is None:
            start_date = end_date - timedelta(days=30)  # Default to last 30 days
        
        try:
            # Prepare URL with parameters
            url = f"{NEWS_SOURCES['coindesk']['url']}?lang=EN&limit=100"
            response = requests.get(url, headers=NEWS_SOURCES['coindesk']['headers'], timeout=10)
            response.raise_for_status()
            data = response.json()
            
            if 'Data' in data:
                news_items = data['Data']
                
                # Process news items in batches
                batch_size = 20  # Increased batch size for better performance
                for i in range(0, len(news_items), batch_size):
                    batch = news_items[i:i + batch_size]
                    batch_news = []
                    
                    for item in batch:
                        try:
                            published_at = datetime.fromtimestamp(item.get('PUBLISHED_ON', 0))
                            if start_date <= published_at <= end_date:
                                batch_news.append({
                                    'title': item.get('TITLE', ''),
                                    'body': item.get('BODY', ''),
                                    'url': item.get('URL', ''),
                                    'source': item.get('SOURCE_DATA', {}).get('NAME', 'CoinDesk'),
                                    'published_on': item.get('PUBLISHED_ON', 0)
                                })
                        except (ValueError, TypeError) as e:
                            continue
                    
                    # Process coins for this batch
                    for article in batch_news:
                        mentioned_coins = self.check_coin_mentions(article)
                        if mentioned_coins:
                            self.store_news(article, mentioned_coins)
                            all_news.append(article)
            
            return all_news
            
        except requests.exceptions.RequestException as e:
            print(f"Network error fetching news from coindesk: {e}")
            return []
        except Exception as e:
            print(f"Unexpected error fetching news from coindesk: {e}")
            return []

    def check_coin_mentions(self, article: Dict) -> Set[str]:
        """Check which tracked coins are mentioned in the article title and content"""
        mentioned_coins = set()
        
        # Get title and content text and clean them
        title = article['title'].lower()
        content = article.get('body', '').lower()
        
        # Replace common separators with spaces and normalize
        text = re.sub(r'[.,;:!?()\[\]{}"\'-]', ' ', f"{title} {content}")
        text = re.sub(r'\s+', ' ', text).strip()
        
        # Coin names and symbols (no substrings)
        coins = {
            "bitcoin": ["bitcoin", "btc"],
            "ethereum": ["ethereum", "eth"],
            "ripple": ["ripple", "xrp"],
            "dogecoin": ["dogecoin", "doge"],
            "solana": ["solana", "sol"],
            "tron": ["tron", "trx"],
            "tether": ["tether", "usdt"]
        }
        
        for coin, names in coins.items():
            for name in names:
                if f" {name} " in f" {text} ":  # Simple word boundary check
                    mentioned_coins.add(coin)
                    break
        
        return mentioned_coins

    def get_coin_price(self, coin_id: str) -> Dict:
        """Fetch current price and market cap from CoinGecko with thread-safe caching"""
        # Try to get from cache first
        cached_price = get_cached_price(coin_id)
        if cached_price:
            return cached_price

        # If not in cache, fetch from CoinGecko
        url = f"{COINGECKO_API_URL}/simple/price"
        params = {
            "ids": coin_id,
            "vs_currencies": "usd",
            "include_market_cap": "true",
            "include_24hr_vol": "true",
            "include_24hr_change": "true"
        }
        
        try:
            response = requests.get(url, params=params, timeout=5)  # Add timeout
            response.raise_for_status()
            data = response.json()
            
            if coin_id in data:
                price_data = {
                    "usd": data[coin_id]["usd"],
                    "usd_market_cap": data[coin_id]["usd_market_cap"],
                    "usd_24h_vol": data[coin_id]["usd_24h_vol"],
                    "usd_24h_change": data[coin_id]["usd_24h_change"]
                }
                
                # Update cache
                update_price_cache(coin_id, price_data)
                return price_data
            else:
                raise ValueError(f"Coin {coin_id} not found in CoinGecko response")
                
        except requests.exceptions.RequestException as e:
            print(f"Error fetching price from CoinGecko: {e}")
            raise

    def get_historical_price(self, coin_id: str, timestamp: datetime) -> float:
        """Fetch historical price from Messari for a specific timestamp"""
        try:
            # Convert to Messari ID
            messari_id = COIN_SYMBOL_MAP.get(coin_id)
            if not messari_id:
                raise ValueError(f"No Messari ID mapping found for {coin_id}")

            # Format timestamp for Messari API
            date_str = timestamp.strftime("%Y-%m-%d")
            
            # Get historical price data from Messari
            url = f"{MESSARI_API_URL}/assets/{messari_id}/metrics/price/time-series"
            params = {
                "start": date_str,
                "end": date_str,
                "interval": "1d"
            }
            headers = {'x-messari-api-key': MESSARI_API_KEY} if MESSARI_API_KEY else {}
            
            response = requests.get(url, params=params, headers=headers)
            response.raise_for_status()
            data = response.json()
            
            if 'data' in data and 'values' in data['data'] and data['data']['values']:
                # Get the closing price for the day
                price_data = data['data']['values'][0]
                return price_data[1]  # Messari returns [timestamp, price] pairs
            else:
                raise ValueError(f"No historical price data found for {coin_id} at {timestamp}")
                
        except requests.exceptions.RequestException as e:
            print(f"Error fetching historical price from Messari: {e}")
            raise

    def store_news(self, article: Dict, mentioned_coins: Set[str]):
        """Store news article and related coin data in database"""
        try:
            print(f"\nAttempting to store article: {article['title']}")
            print(f"Mentioned coins: {mentioned_coins}")
            
            # Clean and encode the text data
            title = article['title'].encode('utf-8', 'ignore').decode('utf-8')
            body = article['body'].encode('utf-8', 'ignore').decode('utf-8')
            url = article['url'].encode('utf-8', 'ignore').decode('utf-8')
            source = article['source'].encode('utf-8', 'ignore').decode('utf-8')
            published_date = datetime.fromtimestamp(article['published_on'])

            print(f"Checking if URL exists: {url}")
            # Check if URL already exists
            self.cursor.execute("""
                SELECT id, published_date 
                FROM crypto_news 
                WHERE url = %s
            """, (url,))
            existing_news = self.cursor.fetchone()
            
            if existing_news:
                news_id = existing_news[0]
                existing_date = existing_news[1]
                print(f"Article already exists in database: {url}")
                print(f"Existing date: {existing_date}, New date: {published_date}")
                
                # Update the article if the new one is more recent
                if published_date > existing_date:
                    print(f"Updating existing article with newer content")
                    self.cursor.execute("""
                        UPDATE crypto_news 
                        SET title = %s, content = %s, published_date = %s, source = %s
                        WHERE id = %s
                    """, (title, body, published_date, source, news_id))
                return news_id

            print("Inserting new article into database")
            # Insert new news article
            self.cursor.execute("""
                INSERT INTO crypto_news (title, content, published_date, source, url)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
            """, (title, body, published_date, source, url))
            news_id = self.cursor.fetchone()[0]
            print(f"Stored new news article with ID: {news_id}")

            # Store prices and create news-coin relationships
            for coin_id in mentioned_coins:
                print(f"Processing coin: {coin_id}")
                try:
                    # Get current price for market data
                    current_price_data = self.get_coin_price(coin_id)
                    print(f"Got current price data for {coin_id}: {current_price_data}")
                    
                    # Get historical price at news time
                    historical_price = self.get_historical_price(coin_id, published_date)
                    print(f"Got historical price for {coin_id} at {published_date}: {historical_price}")
                    
                    # Store current price data
                    print(f"Storing current price data for {coin_id}")
                    self.cursor.execute("""
                        INSERT INTO crypto_prices (coin_id, price, market_cap, timestamp)
                        VALUES (%s, %s, %s, %s)
                    """, (
                        coin_id,
                        current_price_data["usd"],
                        current_price_data["usd_market_cap"],
                        datetime.now()
                    ))

                    # Create news-coin relationship with historical price at news time
                    print(f"Creating news-coin relationship for {coin_id}")
                    self.cursor.execute("""
                        INSERT INTO news_coins (news_id, coin_id, price_at_news)
                        VALUES (%s, %s, %s)
                    """, (news_id, coin_id, historical_price))
                except Exception as e:
                    print(f"Error processing coin {coin_id}: {e}")

            self.conn.commit()
            print("Successfully committed all data to database")
            return news_id
            
        except Exception as e:
            self.conn.rollback()
            print(f"Error storing news: {e}")
            raise

    def run(self):
        """Main execution loop that runs continuously until manually stopped"""
        print("Starting news scraping...")

        try:
            self.connect_db()
            print("Connected to database successfully")
            
            # 1. SCRAPE: Fetch news for the last year
            end_date_continuous = datetime.now()
            start_date_continuous = end_date_continuous - timedelta(days=365)
            news_items = self.fetch_news(start_date=start_date_continuous, end_date=end_date_continuous)
            print(f"\nProcessing {len(news_items)} news items")
            
            articles_with_coins = 0
            for article in news_items:
                try:
                    # 2. CHECK: Detect coin mentions
                    mentioned_coins = self.check_coin_mentions(article)
                    print(f"\nArticle: {article['title']}")
                    print(f"Detected coins: {mentioned_coins}")
                    
                    if mentioned_coins:
                        articles_with_coins += 1
                        # 3. STORE: Save article and coin data
                        news_id = self.store_news(article, mentioned_coins)
                        print(f"Stored article with ID: {news_id}")
                        
                        # 4. VERIFY: Double-check the storage
                        self.cursor.execute("""
                            SELECT nc.coin_id, nc.price_at_news 
                            FROM news_coins nc 
                            WHERE nc.news_id = %s
                        """, (news_id,))
                        stored_coins = self.cursor.fetchall()
                        print(f"Verified stored coins: {stored_coins}")
                except Exception as e:
                    print(f"Error processing article {article.get('title', 'Unknown')}: {e}")
                    continue
            
            print(f"\nFound {articles_with_coins} articles mentioning tracked coins")
                
        except Exception as e:
            print(f"Error in main execution: {e}")
        finally:
            self.close_db()
            print("Database connection closed")

        # Wait for an hour before fetching news again
        time.sleep(3600)  # Wait for 1 hour (3600 seconds)

def test_api_connection():
    """Test the API connection and print the response"""
    try:
        headers = {'authorization': f'Apikey {COINDESK_API_KEY}'}
        response = requests.get(NEWS_SOURCES['coindesk']['url'], headers=headers)
        print("API Status Code:", response.status_code)
        print("API Response Headers:", response.headers)
        print("API Response Content:", json.dumps(response.json(), indent=2))  # Print full JSON response
    except Exception as e:
        print(f"API Test Error: {e}")

def recreate_indexes():
    """Drop and recreate database indexes"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    try:
        # Drop existing indexes
        cur.execute("""
            DROP INDEX IF EXISTS idx_crypto_news_title_content;
            DROP INDEX IF EXISTS idx_crypto_news_title;
        """)
        
        # Create new indexes
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_crypto_news_published_date 
            ON crypto_news(published_date DESC);
            
            CREATE INDEX IF NOT EXISTS idx_crypto_news_title 
            ON crypto_news USING gin(to_tsvector('english', title));
        """)
        
        # Indexes for news_coins table
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_news_coins_news_id 
            ON news_coins(news_id);
            
            CREATE INDEX IF NOT EXISTS idx_news_coins_coin_id 
            ON news_coins(coin_id);
        """)
        
        # Indexes for crypto_prices table
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_crypto_prices_coin_timestamp 
            ON crypto_prices(coin_id, timestamp DESC);
        """)
        
        conn.commit()
        print("Successfully recreated database indexes")
    except Exception as e:
        conn.rollback()
        print(f"Error recreating indexes: {e}")
    finally:
        cur.close()
        conn.close()

def create_tables():
    """Create necessary database tables if they don't exist"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    try:
        # Create crypto_news table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS crypto_news (
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                content TEXT,
                published_date TIMESTAMP NOT NULL,
                source TEXT,
                url TEXT UNIQUE NOT NULL
            );
        """)
        
        # Create crypto_prices table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS crypto_prices (
                id SERIAL PRIMARY KEY,
                coin_id TEXT NOT NULL,
                price DECIMAL NOT NULL,
                market_cap DECIMAL,
                timestamp TIMESTAMP NOT NULL,
                UNIQUE(coin_id, timestamp)
            );
        """)
        
        # Create news_coins table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS news_coins (
                news_id INTEGER REFERENCES crypto_news(id) ON DELETE CASCADE,
                coin_id TEXT NOT NULL,
                price_at_news DECIMAL,
                PRIMARY KEY (news_id, coin_id),
                UNIQUE(news_id, coin_id) -- Explicitly add unique constraint for ON CONFLICT
            );
        """)
        
        conn.commit()
        print("Successfully created database tables")
    except Exception as e:
        conn.rollback()
        print(f"Error creating tables: {e}")
    finally:
        cur.close()
        conn.close()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/news')
def get_news():
    import requests
    from datetime import datetime
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Get filter parameters
    coin = request.args.get('coin', '')
    search = request.args.get('search', '')
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')
    limit = int(request.args.get('limit', 10))
    offset = int(request.args.get('offset', 0))
    
    # Build query to get news and at-news-time prices
    # Using a CTE to first get the filtered news items
    query = """
        WITH filtered_news AS (
            SELECT n.id, n.title, n.content, n.published_date, n.source, n.url
            FROM crypto_news n
            LEFT JOIN news_coins nc ON n.id = nc.news_id
            WHERE 1=1
    """
    params = []
    
    if coin:
        query += " AND nc.coin_id = %s"
        params.append(coin)
    
    if search:
        query += " AND n.title ILIKE %s"
        params.append(f'%{search}%')
    
    if date_from:
        query += " AND n.published_date >= %s"
        params.append(date_from)
    
    if date_to:
        query += " AND n.published_date <= %s"
        params.append(date_to)
    
    query += """
            GROUP BY n.id, n.title, n.content, n.published_date, n.source, n.url
            ORDER BY n.published_date DESC
            LIMIT %s OFFSET %s
        )
        SELECT 
            fn.id, fn.title, fn.content, fn.published_date, fn.source, fn.url,
            COALESCE(array_agg(DISTINCT nc.coin_id) FILTER (WHERE nc.coin_id IS NOT NULL), ARRAY[]::text[]) as coins,
            COALESCE(json_object_agg(
                nc.coin_id,
                json_build_object(
                    'current', json_build_object('price', cp.price),
                    'historical', json_build_object('price', nc.price_at_news)
                )
            ) FILTER (WHERE nc.coin_id IS NOT NULL), '{}'::json) as prices
        FROM filtered_news fn
        LEFT JOIN news_coins nc ON fn.id = nc.news_id
        LEFT JOIN (
            SELECT DISTINCT ON (coin_id) coin_id, price
            FROM crypto_prices
            ORDER BY coin_id, timestamp DESC
        ) cp ON nc.coin_id = cp.coin_id
        GROUP BY fn.id, fn.title, fn.content, fn.published_date, fn.source, fn.url
        ORDER BY fn.published_date DESC
    """
    params.extend([limit, offset])
    
    cur.execute(query, params)
    news_items = cur.fetchall()
    
    # Format the results
    formatted_news = []
    for item in news_items:
        coins = item[6] if item[6] else []
        prices = item[7] if item[7] else {}
        
        # Debug print to verify price data
        print(f"Price data for news {item[0]}:", json.dumps(prices, indent=2))
        
        formatted_news.append({
            'id': item[0],
            'title': item[1],
            'content': item[2],
            'published_date': item[3].isoformat() if item[3] else None,
            'source': item[4],
            'url': item[5],
            'coins': coins,
            'prices': prices
        })
    
    cur.close()
    conn.close()
    
    print("Formatted news for API response:", json.dumps(formatted_news, indent=2))
    return jsonify(formatted_news)

@app.route('/api/coins')
def get_coins():
    # Return all tracked coins directly from TRACKED_COINS
    return jsonify(list(TRACKED_COINS.keys()))

@app.route('/api/prices')
def get_prices():
    crypto_ids = list(TRACKED_COINS.keys())
    ids_param = ",".join(crypto_ids)
    
    try:
        # First try to get prices from database
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Get latest prices from database with a single query
        cur.execute("""
            WITH latest_prices AS (
                SELECT DISTINCT ON (coin_id) coin_id, price, timestamp
                FROM crypto_prices
                WHERE coin_id = ANY(%s)
                ORDER BY coin_id, timestamp DESC
            )
            SELECT coin_id, price, timestamp
            FROM latest_prices
            WHERE timestamp > NOW() - INTERVAL '5 minutes'
        """, (crypto_ids,))
        
        db_prices = {row[0]: row[1] for row in cur.fetchall()}
        
        # If we have fresh prices for all coins, return them
        if len(db_prices) == len(crypto_ids):
            sorted_prices = sorted([(k, v) for k, v in db_prices.items()], 
                                 key=lambda x: x[1] if x[1] is not None else float('-inf'), 
                                 reverse=True)
            return jsonify(sorted_prices)
        
        # Otherwise, fetch fresh prices from CoinGecko
        url = f"{COINGECKO_API_URL}/simple/price?ids={ids_param}&vs_currencies=usd"
        response = requests.get(url, timeout=5)  # Add timeout
        response.raise_for_status()
        data = response.json()
        
        # Store new prices in database
        for crypto_id, price_info in data.items():
            price = price_info.get('usd')
            if price:
                cur.execute("""
                    INSERT INTO crypto_prices (coin_id, price, timestamp)
                    VALUES (%s, %s, %s)
                """, (crypto_id, price, datetime.now()))
                db_prices[crypto_id] = price
        
        conn.commit()
        cur.close()
        conn.close()
        
        # Ensure we have prices for all tracked coins
        for coin_id in crypto_ids:
            if coin_id not in db_prices:
                db_prices[coin_id] = None
        
        # Sort coins by price (high to low)
        sorted_prices = sorted([(k, v) for k, v in db_prices.items()], 
                             key=lambda x: x[1] if x[1] is not None else float('-inf'), 
                             reverse=True)
        
        return jsonify(sorted_prices)
        
    except requests.exceptions.RequestException as e:
        print(f"Error fetching prices from CoinGecko: {e}")
        # Return database prices even if they're old
        if 'db_prices' in locals():
            sorted_prices = sorted([(k, v) for k, v in db_prices.items()], 
                                 key=lambda x: x[1] if x[1] is not None else float('-inf'), 
                                 reverse=True)
            return jsonify(sorted_prices)
        return jsonify([])
    except Exception as e:
        print(f"An unexpected error occurred in get_prices: {e}")
        return jsonify([])
    finally:
        if 'cur' in locals():
            cur.close()
        if 'conn' in locals():
            conn.close()

@app.route('/post/<int:news_id>')
def view_post(news_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, title, content, published_date, source, url FROM crypto_news WHERE id = %s", (news_id,))
    article = cur.fetchone()
    cur.close()
    conn.close()
    if not article:
        return "Post not found", 404
    return render_template('post.html',
        id=article[0],
        title=article[1],
        content=article[2],
        published_date=article[3].isoformat() if article[3] else None,
        source=article[4],
        url=article[5]
    )

def reprocess_post(post_id: int):
    """Re-run coin detection and storage for a specific post."""
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Fetch the post
        cur.execute("SELECT id, title, content, published_date FROM crypto_news WHERE id = %s", (post_id,))
        post = cur.fetchone()
        if not post:
            print(f"Post with id {post_id} not found.")
            return
        
        # Create article dict for coin detection
        article = {
            "title": post[1],
            "body": post[2],
            "published_on": post[3].timestamp()
        }
        
        # Run coin detection
        tracker = CryptoNewsTracker(DB_CONFIG)
        mentioned_coins = tracker.check_coin_mentions(article)
        print(f"Detected coins for post {post_id}: {mentioned_coins}")
        
        # First, remove existing coin relationships
        cur.execute("DELETE FROM news_coins WHERE news_id = %s", (post_id,))
        
        # Then insert new coin relationships with historical prices
        for coin_id in mentioned_coins:
            try:
                # Get historical price at news time
                historical_price = tracker.get_historical_price(coin_id, post[3])
                print(f"Got historical price for {coin_id} at {post[3]}: {historical_price}")
                
                # Insert new relationship with historical price
                cur.execute("""
                    INSERT INTO news_coins (news_id, coin_id, price_at_news)
                    VALUES (%s, %s, %s)
                """, (post_id, coin_id, historical_price))
            except Exception as e:
                print(f"Error processing coin {coin_id}: {e}")
                # Insert the coin relationship even if historical price fetch fails
                cur.execute("""
                    INSERT INTO news_coins (news_id, coin_id, price_at_news)
                    VALUES (%s, %s, NULL)
                """, (post_id, coin_id))
        
        conn.commit()
        print(f"Successfully reprocessed post {post_id} with coins: {mentioned_coins}")
    except Exception as e:
        conn.rollback()
        print(f"Error reprocessing post {post_id}: {e}")
    finally:
        cur.close()
        conn.close()

if __name__ == "__main__":
    # Create database tables and indexes
    create_tables()
    recreate_indexes()  # Use recreate_indexes instead of create_indexes
    
    # Test API connection first
    print("Testing API connection...")
    test_api_connection()
    
    # Create and start the tracker in a separate thread
    from threading import Thread
    tracker = CryptoNewsTracker(DB_CONFIG)
    tracker_thread = Thread(target=tracker.run)
    tracker_thread.daemon = True  # This ensures the thread will stop when the main program stops
    tracker_thread.start()

    # Run Flask app
    app.run(debug=True, host='0.0.0.0', port=5000)
