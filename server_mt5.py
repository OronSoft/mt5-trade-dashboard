import MetaTrader5 as mt5
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
import threading
import time
from datetime import datetime
import json
import ssl
import base64
import sqlite3
import os
import sys
import ctypes
import gc
import logging
import urllib.request
import urllib.parse
import urllib.error

# Performance logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
perf_logger = logging.getLogger('performance')


def get_memory_usage_mb():
    """Get current memory usage in MB (cross-platform, no extra dependency)"""
    try:
        if sys.platform == 'win32':
            # Windows: use ctypes to call GetProcessMemoryInfo
            from ctypes import wintypes

            class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
            ctypes.windll.psapi.GetProcessMemoryInfo(
                ctypes.windll.kernel32.GetCurrentProcess(),
                ctypes.byref(counters),
                counters.cb
            )
            return counters.WorkingSetSize / (1024 * 1024)  # bytes -> MB
        else:
            # macOS / Linux: use resource module
            import resource
            usage = resource.getrusage(resource.RUSAGE_SELF)
            if sys.platform == 'darwin':
                return usage.ru_maxrss / (1024 * 1024)  # bytes -> MB on macOS
            else:
                return usage.ru_maxrss / 1024  # KB -> MB on Linux
    except Exception:
        return 0.0  # Unable to determine memory usage


def log_performance(label, start_time, extra_info=""):
    """Log elapsed time and memory for a labeled operation"""
    elapsed_ms = (time.time() - start_time) * 1000
    mem_mb = get_memory_usage_mb()
    msg = f"[PERF] {label}: {elapsed_ms:.1f}ms | Memory: {mem_mb:.1f}MB"
    if extra_info:
        msg += f" | {extra_info}"
    perf_logger.info(msg)
    return elapsed_ms


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """Handle requests in separate threads to prevent blocking"""
    daemon_threads = True


# Database configuration
DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'mt5_trades.db')

# Autotrail configuration
AUTOTRAIL_CHECK_INTERVAL = 5  # Check every 5 seconds
DEFAULT_TRAIL_PIPS = 50  # Fallback default for unknown symbols

# Supported symbols configuration
# pip_value: price movement per pip
# contract_size: standard lot size
# trail_multiplier: converts UI input to price movement (input * multiplier * pip_value = trail distance)
# default_trail: default trail value for this symbol
SUPPORTED_SYMBOLS = {
    'XAUUSD': {'pip_value': 0.01, 'contract_size': 100, 'trail_multiplier': 100, 'default_trail': 30, 'description': 'Gold'},
    'EURUSD': {'pip_value': 0.0001, 'contract_size': 100000, 'trail_multiplier': 1, 'default_trail': 75, 'description': 'EUR/USD'},
    'USDCHF': {'pip_value': 0.0001, 'contract_size': 100000, 'trail_multiplier': 1, 'default_trail': 50, 'description': 'USD/CHF'},
    'GBPUSD': {'pip_value': 0.0001, 'contract_size': 100000, 'trail_multiplier': 1, 'default_trail': 50, 'description': 'GBP/USD'},
    'USDJPY': {'pip_value': 0.01, 'contract_size': 100000, 'trail_multiplier': 1, 'default_trail': 50, 'description': 'USD/JPY'},
    'XAGUSD': {'pip_value': 0.001, 'contract_size': 5000, 'trail_multiplier': 1000, 'default_trail': 3, 'description': 'Silver'},
    'OILUSD': {'pip_value': 0.01, 'contract_size': 100, 'trail_multiplier': 100, 'default_trail': 6, 'description': 'Oil WTI'},
}

# Crypto symbols fetched via free public APIs (no API key, no geo restrictions)
# coingecko_id: for CoinGecko API, coincap_id: for CoinCap API (fallback)
CRYPTO_SYMBOLS = {
    'BTCUSD': {'coingecko_id': 'bitcoin', 'coincap_id': 'bitcoin', 'description': 'Bitcoin'},
    'SOLUSD': {'coingecko_id': 'solana', 'coincap_id': 'solana', 'description': 'Solana'},
    'ETHUSD': {'coingecko_id': 'ethereum', 'coincap_id': 'ethereum', 'description': 'Ethereum'},
}

# Cache crypto prices to avoid hammering the API (price, timestamp)
_crypto_price_cache = {}
_CRYPTO_CACHE_TTL = 300  # 5 minutes


def _fetch_coingecko(coingecko_id):
    """Fetch price from CoinGecko (free, no key, no geo block)"""
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={coingecko_id}&vs_currencies=usd"
    req = urllib.request.Request(url)
    req.add_header('User-Agent', 'MT5Dashboard/1.0')
    req.add_header('Accept', 'application/json')
    with urllib.request.urlopen(req, timeout=8) as resp:
        data = json.loads(resp.read().decode())
        return float(data.get(coingecko_id, {}).get('usd', 0))


def _fetch_coincap(coincap_id):
    """Fetch price from CoinCap (free fallback, no key, no geo block)"""
    url = f"https://api.coincap.io/v2/assets/{coincap_id}"
    req = urllib.request.Request(url)
    req.add_header('User-Agent', 'MT5Dashboard/1.0')
    with urllib.request.urlopen(req, timeout=8) as resp:
        data = json.loads(resp.read().decode())
        return float(data.get('data', {}).get('priceUsd', 0))


def get_crypto_price(symbol):
    """Get crypto price using CoinGecko (primary) + CoinCap (fallback)"""
    crypto_cfg = CRYPTO_SYMBOLS.get(symbol)
    if not crypto_cfg:
        return 0

    # Check cache
    cached = _crypto_price_cache.get(symbol)
    if cached and (time.time() - cached[1]) < _CRYPTO_CACHE_TTL:
        return cached[0]

    # Try CoinGecko first
    try:
        price = _fetch_coingecko(crypto_cfg['coingecko_id'])
        if price > 0:
            _crypto_price_cache[symbol] = (price, time.time())
            return price
    except Exception as e:
        print(f"⚠️ CoinGecko failed for {symbol}: {e}")

    # Fallback to CoinCap
    try:
        price = _fetch_coincap(crypto_cfg['coincap_id'])
        if price > 0:
            _crypto_price_cache[symbol] = (price, time.time())
            return price
    except Exception as e:
        print(f"⚠️ CoinCap also failed for {symbol}: {e}")

    # Return stale cache if available
    if cached:
        return cached[0]
    return 0


def get_symbol_config(symbol):
    """Get configuration for a symbol, with defaults if not found"""
    if symbol in SUPPORTED_SYMBOLS:
        return SUPPORTED_SYMBOLS[symbol]
    # Default config for unknown symbols (assumes forex pair)
    return {'pip_value': 0.0001, 'contract_size': 100000, 'trail_multiplier': 1, 'default_trail': DEFAULT_TRAIL_PIPS, 'description': symbol}


def get_default_trail_for_symbol(symbol):
    """Get the default trail value for a symbol"""
    config = get_symbol_config(symbol)
    return config.get('default_trail', DEFAULT_TRAIL_PIPS)

# Authentication configuration
# Change these credentials!
USERS = {
    "admin": "xxxxxx",
}


def check_auth(auth_header):
    """Check Basic Authentication header"""
    if auth_header is None:
        return False
    
    try:
        auth_type, credentials = auth_header.split(' ', 1)
        if auth_type.lower() != 'basic':
            return False
        
        decoded = base64.b64decode(credentials).decode('utf-8')
        username, password = decoded.split(':', 1)
        
        return USERS.get(username) == password
    except Exception:
        return False


# Database functions
def init_database():
    """Initialize SQLite database and create table if not exists"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS mt5_trades (
            ticket INTEGER PRIMARY KEY,
            symbol TEXT,
            type TEXT,
            volume REAL,
            price_open REAL,
            price_current REAL,
            profit REAL,
            swap REAL,
            sl REAL,
            tp REAL,
            risk_per_lot REAL,
            total_risk REAL,
            open_time TEXT,
            autotrail INTEGER DEFAULT 0,
            trail_pips INTEGER DEFAULT 20,
            status TEXT DEFAULT 'open',
            created_at TEXT,
            updated_at TEXT
        )
    ''')
    
    # Add trail_pips column if it doesn't exist (for existing databases)
    try:
        cursor.execute('ALTER TABLE mt5_trades ADD COLUMN trail_pips INTEGER DEFAULT 20')
    except sqlite3.OperationalError:
        pass  # Column already exists
    
    # Symbol settings table (per-symbol preferences like autotrail for new orders)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS symbol_settings (
            symbol TEXT PRIMARY KEY,
            autotrail_new_orders INTEGER DEFAULT 0,
            updated_at TEXT
        )
    ''')
    
    # Price alerts table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS price_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            condition TEXT NOT NULL,
            price REAL NOT NULL,
            alert_type TEXT DEFAULT 'price',
            active INTEGER DEFAULT 1,
            triggered INTEGER DEFAULT 0,
            triggered_at TEXT,
            created_at TEXT
        )
    ''')
    # Migration: add alert_type column if missing (existing DBs)
    try:
        cursor.execute("ALTER TABLE price_alerts ADD COLUMN alert_type TEXT DEFAULT 'price'")
    except Exception:
        pass  # Column already exists
    
    # Fix trail_pips for existing trades: update to symbol-specific defaults
    # Only update trades where autotrail is OFF (user hasn't customized the value)
    for symbol, config in SUPPORTED_SYMBOLS.items():
        default_trail = config.get('default_trail', DEFAULT_TRAIL_PIPS)
        cursor.execute(
            'UPDATE mt5_trades SET trail_pips = ? WHERE symbol = ? AND autotrail = 0',
            (default_trail, symbol)
        )
    # For unknown symbols, use DEFAULT_TRAIL_PIPS
    known_symbols = list(SUPPORTED_SYMBOLS.keys())
    if known_symbols:
        placeholders = ','.join('?' * len(known_symbols))
        cursor.execute(
            f'UPDATE mt5_trades SET trail_pips = ? WHERE symbol NOT IN ({placeholders}) AND autotrail = 0',
            [DEFAULT_TRAIL_PIPS] + known_symbols
        )
    
    conn.commit()
    conn.close()
    print(f"📁 Database initialized: {DB_FILE}")


def get_db_connection():
    """Get database connection"""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def upsert_trade(trade_data):
    """Insert or update a trade in the database (single trade, opens own connection)"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Check if trade exists
    cursor.execute('SELECT ticket, autotrail, status FROM mt5_trades WHERE ticket = ?', (trade_data['ticket'],))
    existing = cursor.fetchone()
    
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    if existing:
        # Update existing trade (preserve autotrail and status)
        cursor.execute('''
            UPDATE mt5_trades SET
                symbol = ?, type = ?, volume = ?, price_open = ?, price_current = ?,
                profit = ?, swap = ?, sl = ?, tp = ?, risk_per_lot = ?, total_risk = ?,
                open_time = ?, updated_at = ?
            WHERE ticket = ?
        ''', (
            trade_data['symbol'], trade_data['type'], trade_data['volume'],
            trade_data['price_open'], trade_data['price_current'], trade_data['profit'],
            trade_data['swap'], trade_data['sl'], trade_data['tp'],
            trade_data['risk_per_lot'], trade_data['total_risk'], trade_data['time'],
            now, trade_data['ticket']
        ))
    else:
        # Insert new trade with symbol-specific default trail
        symbol = trade_data['symbol']
        default_trail = get_default_trail_for_symbol(symbol)
        symbol_autotrail = get_symbol_autotrail_settings()
        auto_enabled = 1 if symbol_autotrail.get(symbol, False) else 0
        cursor.execute('''
            INSERT INTO mt5_trades (
                ticket, symbol, type, volume, price_open, price_current, profit, swap,
                sl, tp, risk_per_lot, total_risk, open_time, autotrail, trail_pips, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
        ''', (
            trade_data['ticket'], trade_data['symbol'], trade_data['type'],
            trade_data['volume'], trade_data['price_open'], trade_data['price_current'],
            trade_data['profit'], trade_data['swap'], trade_data['sl'], trade_data['tp'],
            trade_data['risk_per_lot'], trade_data['total_risk'], trade_data['time'],
            auto_enabled, default_trail,
            now, now
        ))
    
    conn.commit()
    conn.close()


def batch_upsert_trades(trades_list):
    """Insert or update multiple trades in a single DB connection (much faster for many trades)"""
    if not trades_list:
        return
    
    t0 = time.time()
    conn = get_db_connection()
    cursor = conn.cursor()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    # Fetch all existing tickets in one query
    tickets = [t['ticket'] for t in trades_list]
    placeholders = ','.join('?' * len(tickets))
    cursor.execute(f'SELECT ticket FROM mt5_trades WHERE ticket IN ({placeholders})', tickets)
    existing_tickets = set(row['ticket'] for row in cursor.fetchall())
    
    # Get per-symbol autotrail settings for new orders
    symbol_autotrail = get_symbol_autotrail_settings()
    
    new_count = 0
    for trade_data in trades_list:
        if trade_data['ticket'] in existing_tickets:
            cursor.execute('''
                UPDATE mt5_trades SET
                    symbol = ?, type = ?, volume = ?, price_open = ?, price_current = ?,
                    profit = ?, swap = ?, sl = ?, tp = ?, risk_per_lot = ?, total_risk = ?,
                    open_time = ?, updated_at = ?
                WHERE ticket = ?
            ''', (
                trade_data['symbol'], trade_data['type'], trade_data['volume'],
                trade_data['price_open'], trade_data['price_current'], trade_data['profit'],
                trade_data['swap'], trade_data['sl'], trade_data['tp'],
                trade_data['risk_per_lot'], trade_data['total_risk'], trade_data['time'],
                now, trade_data['ticket']
            ))
        else:
            # New order: use symbol-specific default_trail and check autotrail setting
            symbol = trade_data['symbol']
            default_trail = get_default_trail_for_symbol(symbol)
            auto_enabled = 1 if symbol_autotrail.get(symbol, False) else 0
            
            cursor.execute('''
                INSERT INTO mt5_trades (
                    ticket, symbol, type, volume, price_open, price_current, profit, swap,
                    sl, tp, risk_per_lot, total_risk, open_time, autotrail, trail_pips, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
            ''', (
                trade_data['ticket'], symbol, trade_data['type'],
                trade_data['volume'], trade_data['price_open'], trade_data['price_current'],
                trade_data['profit'], trade_data['swap'], trade_data['sl'], trade_data['tp'],
                trade_data['risk_per_lot'], trade_data['total_risk'], trade_data['time'],
                auto_enabled, default_trail,
                now, now
            ))
            new_count += 1
            if auto_enabled:
                print(f"🎯 New order {trade_data['ticket']} ({symbol}): autotrail ON, trail={default_trail}")
    
    conn.commit()
    conn.close()
    log_performance("batch_upsert_trades", t0, f"{len(trades_list)} trades ({new_count} new)")


def batch_get_trade_db_info(tickets):
    """Get autotrail, trail_pips and status from database for multiple trades in one query.
    Returns a dict keyed by ticket."""
    if not tickets:
        return {}
    
    t0 = time.time()
    conn = get_db_connection()
    cursor = conn.cursor()
    placeholders = ','.join('?' * len(tickets))
    cursor.execute(f'SELECT ticket, autotrail, trail_pips, status, symbol FROM mt5_trades WHERE ticket IN ({placeholders})', tickets)
    rows = cursor.fetchall()
    conn.close()
    
    result = {}
    for row in rows:
        symbol = row['symbol']
        default_trail = get_default_trail_for_symbol(symbol) if symbol else DEFAULT_TRAIL_PIPS
        result[row['ticket']] = {
            'autotrail': bool(row['autotrail']),
            'trail_pips': row['trail_pips'] if row['trail_pips'] else default_trail,
            'status': row['status']
        }
    
    log_performance("batch_get_trade_db_info", t0, f"{len(tickets)} tickets -> {len(result)} found")
    return result


def get_trade_db_info(ticket, symbol=None):
    """Get autotrail, trail_pips and status from database for a specific trade"""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT autotrail, trail_pips, status, symbol FROM mt5_trades WHERE ticket = ?', (ticket,))
    result = cursor.fetchone()
    conn.close()
    
    # Determine default trail based on symbol
    if result and result['symbol']:
        default_trail = get_default_trail_for_symbol(result['symbol'])
    elif symbol:
        default_trail = get_default_trail_for_symbol(symbol)
    else:
        default_trail = DEFAULT_TRAIL_PIPS
    
    if result:
        return {
            'autotrail': bool(result['autotrail']),
            'trail_pips': result['trail_pips'] if result['trail_pips'] else default_trail,
            'status': result['status']
        }
    return {'autotrail': False, 'trail_pips': default_trail, 'status': 'open'}


def update_trade_autotrail(ticket, autotrail, trail_pips=None):
    """Update autotrail status and optionally trail_pips for a trade"""
    conn = get_db_connection()
    cursor = conn.cursor()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    if trail_pips is not None:
        cursor.execute('UPDATE mt5_trades SET autotrail = ?, trail_pips = ?, updated_at = ? WHERE ticket = ?',
                       (1 if autotrail else 0, trail_pips, now, ticket))
    else:
        cursor.execute('UPDATE mt5_trades SET autotrail = ?, updated_at = ? WHERE ticket = ?',
                       (1 if autotrail else 0, now, ticket))
    conn.commit()
    conn.close()


def update_trade_trail_pips(ticket, trail_pips):
    """Update trail_pips for a trade"""
    conn = get_db_connection()
    cursor = conn.cursor()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    cursor.execute('UPDATE mt5_trades SET trail_pips = ?, updated_at = ? WHERE ticket = ?',
                   (trail_pips, now, ticket))
    conn.commit()
    conn.close()


def update_trade_status(ticket, status):
    """Update status for a trade (open/close/waiting)"""
    conn = get_db_connection()
    cursor = conn.cursor()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    cursor.execute('UPDATE mt5_trades SET status = ?, updated_at = ? WHERE ticket = ?',
                   (status, now, ticket))
    conn.commit()
    conn.close()


def mark_closed_trades(active_tickets):
    """Mark trades as closed if they're no longer in active positions"""
    conn = get_db_connection()
    cursor = conn.cursor()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    # Get all open trades from DB
    cursor.execute("SELECT ticket FROM mt5_trades WHERE status = 'open'")
    db_tickets = [row['ticket'] for row in cursor.fetchall()]
    
    # Mark as closed if not in active tickets
    for ticket in db_tickets:
        if ticket not in active_tickets:
            cursor.execute("UPDATE mt5_trades SET status = 'close', updated_at = ? WHERE ticket = ?",
                          (now, ticket))
    
    conn.commit()
    conn.close()


def get_all_trades_from_db():
    """Get all trades from database"""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM mt5_trades ORDER BY created_at DESC')
    trades = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return trades


def get_autotrail_trades():
    """Get all trades with autotrail enabled and status open"""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT ticket, symbol, type, trail_pips FROM mt5_trades WHERE autotrail = 1 AND status = 'open'")
    trades = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return trades


def get_symbol_autotrail_settings():
    """Get autotrail_new_orders setting for all symbols. Returns dict keyed by symbol."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT symbol, autotrail_new_orders FROM symbol_settings')
    result = {row['symbol']: bool(row['autotrail_new_orders']) for row in cursor.fetchall()}
    conn.close()
    return result


def set_symbol_autotrail_setting(symbol, enabled):
    """Set autotrail_new_orders for a symbol"""
    conn = get_db_connection()
    cursor = conn.cursor()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    cursor.execute('''
        INSERT INTO symbol_settings (symbol, autotrail_new_orders, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(symbol) DO UPDATE SET autotrail_new_orders = ?, updated_at = ?
    ''', (symbol, 1 if enabled else 0, now, 1 if enabled else 0, now))
    conn.commit()
    conn.close()
    print(f"🎯 Symbol {symbol} autotrail for new orders: {'ON' if enabled else 'OFF'}")


# Autotrail functions
SL_UPDATE_LOG = "stoploss_update.log"

def log_sl_update_failure(msg):
    """Append SL update failure to stoploss_update.log"""
    try:
        with open(SL_UPDATE_LOG, "a", encoding="utf-8") as f:
            f.write(datetime.now().strftime("%Y-%m-%d %H:%M:%S") + " " + msg + "\n")
    except Exception as e:
        print(f"⚠️ Failed to write to {SL_UPDATE_LOG}: {e}")


def update_stop_loss_mt5(ticket, new_sl):
    """Update stop loss for a position in MT5. Logs failures to stoploss_update.log."""
    try:
        position = mt5.positions_get(ticket=ticket)
        if not position or len(position) == 0:
            msg = f"Position {ticket} not found — cannot update SL"
            print(f"❌ {msg}")
            log_sl_update_failure(msg)
            return False

        position = position[0]
        symbol = position.symbol

        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "symbol": symbol,
            "sl": new_sl,
            "tp": position.tp,
        }

        result = mt5.order_send(request)

        if result is None:
            msg = f"{symbol} #{ticket}: order_send returned None — SL update to {new_sl:.5f} failed"
            print(f"❌ {msg}")
            log_sl_update_failure(msg)
            return False

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            msg = f"{symbol} #{ticket}: {result.comment} (code: {result.retcode}) — SL update to {new_sl:.5f} failed"
            print(f"❌ {msg}")
            log_sl_update_failure(msg)
            return False

        print(f"✅ Updated SL for {symbol} #{ticket} to {new_sl:.3f}")
        return True

    except Exception as e:
        msg = f"Exception updating SL for #{ticket}: {e}"
        print(f"❌ {msg}")
        log_sl_update_failure(msg)
        return False


def place_buy_stop_order(symbol, price, volume, sl=0.0, tp=0.0):
    """Place a BUY STOP pending order in MT5. Returns (success, message)."""
    try:
        symbol_info = mt5.symbol_info(symbol)
        if symbol_info is None:
            return False, f"Symbol {symbol} not found"
        if not symbol_info.visible:
            if not mt5.symbol_select(symbol, True):
                return False, f"Failed to select symbol {symbol}"

        request = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": symbol,
            "volume": volume,
            "type": mt5.ORDER_TYPE_BUY_STOP,
            "price": price,
            "sl": sl,
            "tp": tp,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)
        if result is None:
            return False, "order_send returned None"
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            return False, f"{result.comment} (code: {result.retcode})"

        print(f"✅ BUY STOP placed: {symbol} @ {price} vol={volume} ticket={result.order}")
        return True, f"Order #{result.order} placed"
    except Exception as e:
        return False, str(e)


def place_sell_stop_order(symbol, price, volume, sl=0.0, tp=0.0):
    """Place a SELL STOP pending order in MT5. Returns (success, message)."""
    try:
        symbol_info = mt5.symbol_info(symbol)
        if symbol_info is None:
            return False, f"Symbol {symbol} not found"
        if not symbol_info.visible:
            if not mt5.symbol_select(symbol, True):
                return False, f"Failed to select symbol {symbol}"

        request = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": symbol,
            "volume": volume,
            "type": mt5.ORDER_TYPE_SELL_STOP,
            "price": price,
            "sl": sl,
            "tp": tp,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)
        if result is None:
            return False, "order_send returned None"
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            return False, f"{result.comment} (code: {result.retcode})"

        print(f"✅ SELL STOP placed: {symbol} @ {price} vol={volume} ticket={result.order}")
        return True, f"Order #{result.order} placed"
    except Exception as e:
        return False, str(e)


def process_autotrail():
    """Process autotrail for all enabled trades across all symbols"""
    autotrail_trades = get_autotrail_trades()
    
    if not autotrail_trades:
        return
    
    for trade in autotrail_trades:
        ticket = trade['ticket']
        symbol = trade['symbol']
        trade_type = trade['type']
        
        # Get symbol-specific configuration
        config = get_symbol_config(symbol)
        
        # Use symbol-specific default if trail_pips not set
        default_trail = config.get('default_trail', DEFAULT_TRAIL_PIPS)
        trade_trail_pips = trade['trail_pips'] if trade['trail_pips'] else default_trail
        pip_value = config['pip_value']
        
        # Get current price for this symbol
        current_price = get_symbol_price(symbol)
        if current_price == 0:
            continue
        
        # Calculate trail distance using per-trade trail_pips and symbol-specific multiplier
        # XAUUSD: 20 * 100 * 0.01 = 20 ($20 trail)
        # EURUSD: 20 * 1 * 0.0001 = 0.002 (20 pips trail)
        trail_multiplier = config.get('trail_multiplier', 1)
        trail_distance = trade_trail_pips * trail_multiplier * pip_value
        
        # Get current position info from MT5
        position = mt5.positions_get(ticket=ticket)
        if not position or len(position) == 0:
            continue
        
        position = position[0]
        current_sl = position.sl
        
        # Determine price decimals for formatting
        price_decimals = 5 if pip_value == 0.0001 else 3
        
        if trade_type == "BUY":
            # For BUY: SL should be below current price
            # Trail SL up if price moves up
            ideal_sl = current_price - trail_distance
            
            # Only move SL up (never down for a BUY position)
            if current_sl == 0 or ideal_sl > current_sl:
                print(f"🔄 Autotrail {symbol} BUY {ticket} ({trade_trail_pips}): SL {current_sl:.{price_decimals}f} -> {ideal_sl:.{price_decimals}f} (price: {current_price:.{price_decimals}f})")
                update_stop_loss_mt5(ticket, round(ideal_sl, price_decimals))
        
        elif trade_type == "SELL":
            # For SELL: SL should be above current price
            # Trail SL down if price moves down
            ideal_sl = current_price + trail_distance
            
            # Only move SL down (never up for a SELL position)
            if current_sl == 0 or ideal_sl < current_sl:
                print(f"🔄 Autotrail {symbol} SELL {ticket} ({trade_trail_pips}): SL {current_sl:.{price_decimals}f} -> {ideal_sl:.{price_decimals}f} (price: {current_price:.{price_decimals}f})")
                update_stop_loss_mt5(ticket, round(ideal_sl, price_decimals))


def autotrail_loop():
    """Background thread to continuously process autotrail"""
    print(f"🎯 Autotrail started (checking every {AUTOTRAIL_CHECK_INTERVAL}s, default trail: varies by symbol)")
    while True:
        try:
            process_autotrail()
        except Exception as e:
            print(f"Autotrail error: {e}")
        time.sleep(AUTOTRAIL_CHECK_INTERVAL)


# ===== PRICE ALERTS =====
ALERT_CHECK_INTERVAL = 10  # Check alerts every 10 seconds


def create_alert(symbol, condition, price, alert_type='price'):
    """Create a new alert. alert_type is 'price' or 'potential_loss'. condition is 'above' or 'below'."""
    conn = get_db_connection()
    cursor = conn.cursor()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    cursor.execute(
        'INSERT INTO price_alerts (symbol, condition, price, alert_type, active, triggered, created_at) VALUES (?, ?, ?, ?, 1, 0, ?)',
        (symbol.upper(), condition, price, alert_type, now)
    )
    conn.commit()
    alert_id = cursor.lastrowid
    conn.close()
    type_label = "Potential Loss" if alert_type == 'potential_loss' else "Price"
    print(f"🔔 Alert #{alert_id} created: {symbol.upper()} {type_label} {condition} ${price}" if alert_type == 'potential_loss' else f"🔔 Alert #{alert_id} created: {symbol.upper()} {condition} {price}")
    return alert_id


def get_all_alerts():
    """Get all alerts (active and triggered)"""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM price_alerts ORDER BY active DESC, created_at DESC')
    alerts = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return alerts


def delete_alert(alert_id):
    """Delete an alert by id"""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM price_alerts WHERE id = ?', (alert_id,))
    conn.commit()
    deleted = cursor.rowcount > 0
    conn.close()
    if deleted:
        print(f"🗑️ Alert #{alert_id} deleted")
    return deleted


def toggle_alert(alert_id, active):
    """Enable or disable an alert"""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('UPDATE price_alerts SET active = ? WHERE id = ?', (1 if active else 0, alert_id))
    conn.commit()
    conn.close()


def delete_triggered_alerts():
    """Delete all triggered alerts"""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM price_alerts WHERE triggered = 1')
    count = cursor.rowcount
    conn.commit()
    conn.close()
    if count > 0:
        print(f"🗑️ Deleted {count} triggered alert(s)")
    return count


# Gotify push notification configuration
GOTIFY_URL = "http://xxx.crabdance.com/message"
GOTIFY_TOKEN = "xxxxx"


def send_gotify_notification(title, message, priority=5):
    """Send a push notification via Gotify"""
    try:
        data = urllib.parse.urlencode({
            'title': title,
            'message': message,
            'priority': str(priority)
        }).encode('utf-8')
        
        url = f"{GOTIFY_URL}?token={GOTIFY_TOKEN}"
        req = urllib.request.Request(url, data=data, method='POST')
        req.add_header('Content-Type', 'application/x-www-form-urlencoded')
        
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                print(f"📱 Gotify notification sent: {title}")
            else:
                print(f"⚠️ Gotify returned status {resp.status}")
    except Exception as e:
        print(f"⚠️ Failed to send Gotify notification: {e}")


# Pushover push notification configuration
PUSHOVER_API_URL = "https://api.pushover.net/1/messages.json"
PUSHOVER_APP_TOKEN = "xxxxxx"   # Your Pushover application API token
PUSHOVER_USER_KEY = "xxxxxx"    # Your Pushover user key


def send_pushover_notification(title, message, priority=0):
    """Send a push notification via Pushover"""
    if not PUSHOVER_APP_TOKEN or not PUSHOVER_USER_KEY:
        print("⚠️ Pushover not configured (missing token/user key)")
        return
    try:
        params = {
            'token': PUSHOVER_APP_TOKEN,
            'user': PUSHOVER_USER_KEY,
            'title': title,
            'message': message,
            'priority': str(priority)
        }
        # Priority 2 (emergency) requires retry and expire parameters
        if priority == 2:
            params['retry'] = '60'    # Retry every 60 seconds
            params['expire'] = '3600' # Stop retrying after 1 hour
        
        data = urllib.parse.urlencode(params).encode('utf-8')
        
        req = urllib.request.Request(PUSHOVER_API_URL, data=data, method='POST')
        req.add_header('Content-Type', 'application/x-www-form-urlencoded')
        
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                print(f"📱 Pushover notification sent: {title}")
            else:
                print(f"⚠️ Pushover returned status {resp.status}")
    except urllib.error.HTTPError as e:
        # Read the response body to get Pushover's error details
        error_body = e.read().decode('utf-8', errors='replace')
        print(f"⚠️ Pushover HTTP {e.code}: {error_body}")
    except Exception as e:
        print(f"⚠️ Failed to send Pushover notification: {e}")


def send_alert_notifications(title, message):
    """Send alert to all configured notification channels"""
    send_gotify_notification(title, message, priority=5)
    send_pushover_notification(title, message, priority=2)  # 2=emergency: repeats every 60s until acknowledged


def get_potential_loss_for_symbol(symbol):
    """Get the current total potential loss for a symbol from open MT5 positions"""
    try:
        positions = mt5.positions_get(symbol=symbol)
        if positions is None or len(positions) == 0:
            return 0
        metrics = calculate_risk_metrics_for_symbol(positions, symbol)
        return metrics.get('total_potential_loss', 0)
    except Exception as e:
        print(f"⚠️ Error getting potential loss for {symbol}: {e}")
        return 0


def get_all_potential_losses():
    """Get potential losses for all symbols with open positions (cached per check cycle)"""
    result = {}
    try:
        positions = mt5.positions_get()
        if positions is None or len(positions) == 0:
            return result
        # Group by symbol
        by_symbol = {}
        for p in positions:
            by_symbol.setdefault(p.symbol, []).append(p)
        for sym, pos_list in by_symbol.items():
            metrics = calculate_risk_metrics_for_symbol(pos_list, sym)
            result[sym] = metrics.get('total_potential_loss', 0)
    except Exception as e:
        print(f"⚠️ Error getting all potential losses: {e}")
    return result


def check_alerts():
    """Check all active alerts against current prices/potential loss and trigger when conditions are met"""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM price_alerts WHERE active = 1 AND triggered = 0')
    active_alerts = [dict(row) for row in cursor.fetchall()]
    conn.close()

    if not active_alerts:
        return

    # Separate price alerts and potential_loss alerts
    price_alerts = [a for a in active_alerts if a.get('alert_type', 'price') == 'price']
    loss_alerts = [a for a in active_alerts if a.get('alert_type') == 'potential_loss']

    # Cache prices for price alerts
    price_symbols = set(a['symbol'] for a in price_alerts)
    prices = {}
    for sym in price_symbols:
        prices[sym] = get_symbol_price(sym)

    # Cache potential losses for loss alerts (one MT5 call for all)
    potential_losses = {}
    if loss_alerts:
        potential_losses = get_all_potential_losses()

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    triggered_ids = []

    # Check price alerts
    for alert in price_alerts:
        symbol = alert['symbol']
        current_price = prices.get(symbol, 0)
        if current_price == 0:
            continue

        triggered = False
        if alert['condition'] == 'above' and current_price >= alert['price']:
            triggered = True
        elif alert['condition'] == 'below' and current_price <= alert['price']:
            triggered = True

        if triggered:
            triggered_ids.append(alert['id'])
            alert_msg = f"Price {alert['condition']} {alert['price']} → Current: {current_price}"
            print(f"")
            print(f"🚨🚨🚨 ALERT TRIGGERED! 🚨🚨🚨")
            print(f"   Symbol:    {symbol}")
            print(f"   Condition: Price {alert['condition']} {alert['price']}")
            print(f"   Current:   {current_price}")
            print(f"   Time:      {now}")
            print(f"")
            send_alert_notifications(
                title=f"🚨 {symbol} Alert",
                message=alert_msg
            )

    # Check potential loss alerts
    for alert in loss_alerts:
        symbol = alert['symbol']
        current_loss = potential_losses.get(symbol, 0)

        triggered = False
        if alert['condition'] == 'above' and current_loss >= alert['price']:
            triggered = True
        elif alert['condition'] == 'below' and current_loss <= alert['price']:
            triggered = True

        if triggered:
            triggered_ids.append(alert['id'])
            alert_msg = f"Potential Loss {alert['condition']} ${alert['price']:.2f} → Current: ${current_loss:.2f}"
            print(f"")
            print(f"🚨🚨🚨 POTENTIAL LOSS ALERT TRIGGERED! 🚨🚨🚨")
            print(f"   Symbol:    {symbol}")
            print(f"   Condition: Potential Loss {alert['condition']} ${alert['price']:.2f}")
            print(f"   Current:   ${current_loss:.2f}")
            print(f"   Time:      {now}")
            print(f"")
            send_alert_notifications(
                title=f"🚨 {symbol} Loss Alert",
                message=alert_msg
            )

    # Mark triggered alerts in DB
    if triggered_ids:
        conn = get_db_connection()
        cursor = conn.cursor()
        for aid in triggered_ids:
            cursor.execute('UPDATE price_alerts SET triggered = 1, triggered_at = ? WHERE id = ?', (now, aid))
        conn.commit()
        conn.close()


def alert_check_loop():
    """Background thread to continuously check price alerts"""
    print(f"🔔 Alert checker started (checking every {ALERT_CHECK_INTERVAL}s)")
    while True:
        try:
            check_alerts()
        except Exception as e:
            print(f"Alert check error: {e}")
        time.sleep(ALERT_CHECK_INTERVAL)


# ===== FUTURE RISK =====
# MT5 order types for pending orders
ORDER_TYPE_NAMES = {
    2: 'BUY LIMIT',
    3: 'SELL LIMIT',
    4: 'BUY STOP',
    5: 'SELL STOP',
    6: 'BUY STOP LIMIT',
    7: 'SELL STOP LIMIT',
}


def get_future_risk_data(symbol=None):
    """Get open positions + pending orders for a symbol (or all symbols) with risk projections"""
    try:
        current_price = get_symbol_price(symbol) if symbol else 0

        # --- Open positions ---
        if symbol:
            positions = mt5.positions_get(symbol=symbol)
        else:
            positions = mt5.positions_get()
        
        open_trades = []
        if positions:
            for pos in positions:
                config = get_symbol_config(pos.symbol)
                price_decimals = 5 if config['pip_value'] == 0.0001 else 3
                sl = get_position_sl(pos)
                risk_per_lot = 0
                if pos.type == 0 and sl > 0:  # BUY
                    risk_per_lot = (pos.price_open - sl) * config['contract_size']
                elif pos.type == 1 and sl > 0:  # SELL
                    risk_per_lot = (sl - pos.price_open) * config['contract_size']
                
                open_trades.append({
                    'ticket': pos.ticket,
                    'symbol': pos.symbol,
                    'type': 'BUY' if pos.type == 0 else 'SELL',
                    'volume': round(pos.volume, 2),
                    'price_open': round(pos.price_open, price_decimals),
                    'sl': round(sl, price_decimals) if sl > 0 else None,
                    'tp': round(pos.tp, price_decimals) if pos.tp > 0 else None,
                    'profit': round(pos.profit, 2),
                    'risk_per_lot': round(risk_per_lot, 2),
                    'total_risk': round(risk_per_lot * pos.volume, 2),
                    'status': 'open',
                })

        # --- Pending orders ---
        if symbol:
            orders = mt5.orders_get(symbol=symbol)
        else:
            orders = mt5.orders_get()
        
        pending_orders = []
        if orders:
            for order in orders:
                config = get_symbol_config(order.symbol)
                price_decimals = 5 if config['pip_value'] == 0.0001 else 3
                order_type_name = ORDER_TYPE_NAMES.get(order.type, f'TYPE_{order.type}')
                
                # Calculate potential risk if this pending order gets filled
                risk_per_lot = 0
                if order.sl > 0:
                    if order.type in (2, 4, 6):  # BUY types
                        risk_per_lot = (order.price_open - order.sl) * config['contract_size']
                    elif order.type in (3, 5, 7):  # SELL types
                        risk_per_lot = (order.sl - order.price_open) * config['contract_size']
                
                pending_orders.append({
                    'ticket': order.ticket,
                    'symbol': order.symbol,
                    'type': order_type_name,
                    'volume': round(order.volume_current, 2),
                    'price_open': round(order.price_open, price_decimals),
                    'sl': round(order.sl, price_decimals) if order.sl > 0 else None,
                    'tp': round(order.tp, price_decimals) if order.tp > 0 else None,
                    'risk_per_lot': round(risk_per_lot, 2),
                    'total_risk': round(risk_per_lot * order.volume_current, 2),
                    'status': 'pending',
                })

        # Sort pending orders by trigger price (price_open)
        pending_orders.sort(key=lambda o: o['price_open'])

        # --- Summary calculations ---
        current_risk = sum(t['total_risk'] for t in open_trades)
        pending_risk = sum(o['total_risk'] for o in pending_orders)
        total_future_risk = current_risk + pending_risk

        current_volume = sum(t['volume'] for t in open_trades)
        pending_volume = sum(o['volume'] for o in pending_orders)

        # Get all unique symbols present
        all_symbols = sorted(set(
            [t['symbol'] for t in open_trades] + [o['symbol'] for o in pending_orders]
        ))

        return {
            'symbol_filter': symbol,
            'current_price': round(current_price, 5) if current_price else 0,
            'open_trades': open_trades,
            'pending_orders': pending_orders,
            'summary': {
                'open_count': len(open_trades),
                'pending_count': len(pending_orders),
                'current_risk': round(current_risk, 2),
                'pending_risk': round(pending_risk, 2),
                'total_future_risk': round(total_future_risk, 2),
                'current_volume': round(current_volume, 2),
                'pending_volume': round(pending_volume, 2),
                'total_volume': round(current_volume + pending_volume, 2),
            },
            'available_symbols': all_symbols,
            'supported_symbols': list(SUPPORTED_SYMBOLS.keys()),
        }

    except Exception as e:
        print(f"Error getting future risk data: {e}")
        import traceback
        traceback.print_exc()
        return {
            'symbol_filter': symbol,
            'current_price': 0,
            'open_trades': [],
            'pending_orders': [],
            'summary': {
                'open_count': 0, 'pending_count': 0,
                'current_risk': 0, 'pending_risk': 0, 'total_future_risk': 0,
                'current_volume': 0, 'pending_volume': 0, 'total_volume': 0,
            },
            'available_symbols': [],
            'supported_symbols': list(SUPPORTED_SYMBOLS.keys()),
        }


def cancel_all_pending_orders(symbol):
    """Cancel all pending orders for a symbol. Returns (success_count, failed_count, messages)."""
    orders = mt5.orders_get(symbol=symbol) if symbol else []
    if not orders:
        return 0, 0, []
    success_count = 0
    failed_count = 0
    messages = []
    for order in orders:
        request = {
            "action": mt5.TRADE_ACTION_REMOVE,
            "order": order.ticket,
        }
        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            success_count += 1
            messages.append(f"Order #{order.ticket} cancelled")
        else:
            failed_count += 1
            comment = result.comment if result else "Unknown error"
            messages.append(f"Order #{order.ticket} failed: {comment}")
    return success_count, failed_count, messages


def cancel_pending_order(order_ticket):
    """Cancel a single pending order by ticket. Returns (success, message)."""
    request = {
        "action": mt5.TRADE_ACTION_REMOVE,
        "order": int(order_ticket),
    }
    result = mt5.order_send(request)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        return True, f"Order #{order_ticket} cancelled"
    comment = result.comment if result else "Unknown error"
    return False, f"Order #{order_ticket} failed: {comment}"


# Initialize MT5 connection
def initialize_mt5():
    if not mt5.initialize():
        print("MT5 initialization failed")
        return False
    print("MT5 initialized successfully")
    return True


# Get current XAUUSD price
def get_symbol_price(symbol):
    """Get current mid price for any symbol (MT5 first, then crypto API fallback)"""
    # Try MT5 first
    try:
        tick = mt5.symbol_info_tick(symbol)
        if tick is not None:
            return (tick.ask + tick.bid) / 2  # Mid price
    except Exception as e:
        print(f"Error getting MT5 price for {symbol}: {e}")

    # Fallback to crypto API for crypto symbols
    if symbol in CRYPTO_SYMBOLS:
        return get_crypto_price(symbol)

    return 0


# Legacy function for backward compatibility
def get_xauusd_price():
    return get_symbol_price("XAUUSD")


# Get stop loss information for a position
def get_position_sl(position):
    try:
        # Get position details
        position_info = mt5.positions_get(ticket=position.ticket)
        if position_info and len(position_info) > 0:
            return position_info[0].sl  # Stop loss price
    except Exception as e:
        print(f"Error getting SL for position {position.ticket}: {e}")
    return 0


# Calculate risk metrics for XAUUSD positions
def calculate_risk_metrics_for_symbol(positions, symbol):
    """Calculate risk metrics for a specific symbol"""
    config = get_symbol_config(symbol)
    contract_size = config['contract_size']
    
    if not positions:
        return {
            'symbol': symbol,
            'description': config['description'],
            'total_volume': 0,
            'total_profit': 0,
            'total_swap': 0,
            'total_buy_volume': 0,
            'total_sell_volume': 0,
            'total_potential_loss': 0,
            'total_potential_gain': 0,
            'avg_entry_price': 0,
            'current_price': 0,
            'total_exposure': 0,
            'risk_reward_ratio': 0,
            'total_positions': 0
        }

    total_volume = 0
    total_profit = 0
    total_swap = 0
    total_buy_volume = 0
    total_sell_volume = 0
    total_potential_loss = 0
    total_potential_gain = 0
    total_entry_value = 0

    current_price = get_symbol_price(symbol)

    for position in positions:
        total_volume += position.volume
        total_profit += position.profit
        total_swap += position.swap

        if position.type == 0:  # BUY
            total_buy_volume += position.volume

            # Calculate potential loss if stop loss is hit
            # Includes profitable stop losses (negative values) to offset actual losses
            sl = get_position_sl(position)
            if sl > 0:
                loss_per_lot = position.price_open - sl  # Positive = loss, Negative = profitable SL
                potential_loss = loss_per_lot * position.volume * contract_size
                total_potential_loss += potential_loss

            # Calculate potential gain (distance to breakeven)
            gain_per_lot = current_price - position.price_open
            potential_gain = gain_per_lot * position.volume * contract_size
            total_potential_gain += potential_gain

        elif position.type == 1:  # SELL
            total_sell_volume += position.volume

            # Calculate potential loss if stop loss is hit
            # Includes profitable stop losses (negative values) to offset actual losses
            sl = get_position_sl(position)
            if sl > 0:
                loss_per_lot = sl - position.price_open  # Positive = loss, Negative = profitable SL
                potential_loss = loss_per_lot * position.volume * contract_size
                total_potential_loss += potential_loss

            # Calculate potential gain (distance to breakeven)
            gain_per_lot = position.price_open - current_price
            potential_gain = gain_per_lot * position.volume * contract_size
            total_potential_gain += potential_gain

        # Calculate entry value
        total_entry_value += position.volume * position.price_open * contract_size

    # Calculate net volume (buy - sell)
    net_volume = total_buy_volume - total_sell_volume

    # Calculate average entry price
    avg_entry_price = total_entry_value / (total_volume * contract_size) if total_volume > 0 else 0

    # Calculate total exposure (in USD)
    total_exposure = total_volume * contract_size * current_price

    # Calculate risk/reward ratio
    risk_reward_ratio = abs(total_profit / total_potential_loss) if total_potential_loss != 0 else 0

    return {
        'symbol': symbol,
        'description': config['description'],
        'total_volume': round(total_volume, 2),
        'total_profit': round(total_profit, 2),
        'total_swap': round(total_swap, 2),
        'total_buy_volume': round(total_buy_volume, 2),
        'total_sell_volume': round(total_sell_volume, 2),
        'net_volume': round(net_volume, 2),
        'total_potential_loss': round(total_potential_loss, 2),
        'total_potential_gain': round(total_potential_gain, 2),
        'avg_entry_price': round(avg_entry_price, 5),
        'current_price': round(current_price, 5),
        'total_exposure': round(total_exposure, 2),
        'risk_reward_ratio': round(risk_reward_ratio, 2) if risk_reward_ratio > 0 else 0,
        'total_positions': len(positions)
    }


# Legacy function for backward compatibility
def calculate_risk_metrics(positions):
    return calculate_risk_metrics_for_symbol(positions, 'XAUUSD')


# Get all open trades grouped by symbol
def get_all_trades():
    """Get all open trades grouped by symbol with risk metrics per symbol"""
    try:
        t_total = time.time()
        
        # Get all open positions
        t0 = time.time()
        positions = mt5.positions_get()
        log_performance("mt5.positions_get", t0, f"{len(positions) if positions else 0} positions")

        if positions is None:
            print("No positions found or error getting positions")
            return {}, {}, {}

        # Cache symbol prices to avoid redundant MT5 API calls
        t0 = time.time()
        price_cache = {}
        unique_symbols = set(p.symbol for p in positions)
        for sym in unique_symbols:
            price_cache[sym] = get_symbol_price(sym)
        log_performance("price_cache", t0, f"{len(unique_symbols)} symbols")

        # Group positions by symbol
        positions_by_symbol = {}
        all_trades = []
        active_tickets = []
        
        t0 = time.time()
        for position in positions:
            symbol = position.symbol
            config = get_symbol_config(symbol)
            contract_size = config['contract_size']
            
            sl = get_position_sl(position)
            current_price = price_cache.get(symbol, 0)
            
            # Calculate current risk for this position
            risk_per_lot = 0
            if position.type == 0:  # BUY
                if sl > 0:
                    risk_per_lot = (position.price_open - sl) * contract_size
            elif position.type == 1:  # SELL
                if sl > 0:
                    risk_per_lot = (sl - position.price_open) * contract_size

            # Determine price decimal places based on symbol
            price_decimals = 5 if config['pip_value'] == 0.0001 else 3

            trade_data = {
                'ticket': position.ticket,
                'symbol': symbol,
                'type': "BUY" if position.type == 0 else "SELL",
                'volume': round(position.volume, 2),
                'price_open': round(position.price_open, price_decimals),
                'price_current': round(current_price, price_decimals),
                'profit': round(position.profit, 2),
                'swap': round(position.swap, 2),
                'sl': round(sl, price_decimals) if sl > 0 else None,
                'tp': round(position.tp, price_decimals) if position.tp > 0 else None,
                'risk_per_lot': round(risk_per_lot, 2),
                'total_risk': round(risk_per_lot * position.volume, 2),
                'time': datetime.fromtimestamp(position.time).strftime('%Y-%m-%d %H:%M:%S')
            }
            
            all_trades.append(trade_data)
            active_tickets.append(position.ticket)
            
            # Add to grouped positions
            if symbol not in positions_by_symbol:
                positions_by_symbol[symbol] = []
            positions_by_symbol[symbol].append(position)
        
        log_performance("build_trade_data", t0, f"{len(all_trades)} trades")
        
        # Batch DB operations: upsert all trades in one connection
        batch_upsert_trades(all_trades)
        
        # Batch fetch DB info for all tickets in one query
        db_info_map = batch_get_trade_db_info(active_tickets)
        for trade_data in all_trades:
            ticket = trade_data['ticket']
            db_info = db_info_map.get(ticket, {
                'autotrail': False,
                'trail_pips': get_default_trail_for_symbol(trade_data['symbol']),
                'status': 'open'
            })
            trade_data['autotrail'] = db_info['autotrail']
            trade_data['trail_pips'] = db_info['trail_pips']
            trade_data['status'] = db_info['status']
        
        # Mark trades as closed if they're no longer active
        mark_closed_trades(active_tickets)

        # Calculate risk metrics per symbol (uses price_cache)
        t0 = time.time()
        risk_metrics_by_symbol = {}
        for symbol, symbol_positions in positions_by_symbol.items():
            metrics = calculate_risk_metrics_for_symbol(symbol_positions, symbol)
            # Add default_trail to metrics for UI
            metrics['default_trail'] = get_default_trail_for_symbol(symbol)
            risk_metrics_by_symbol[symbol] = metrics
        log_performance("risk_metrics", t0, f"{len(risk_metrics_by_symbol)} symbols")

        # Group trades by symbol for output
        trades_by_symbol = {}
        for trade in all_trades:
            symbol = trade['symbol']
            if symbol not in trades_by_symbol:
                trades_by_symbol[symbol] = []
            trades_by_symbol[symbol].append(trade)

        # Calculate total risk metrics across all symbols
        total_metrics = {
            'total_profit': sum(m['total_profit'] for m in risk_metrics_by_symbol.values()),
            'total_swap': sum(m['total_swap'] for m in risk_metrics_by_symbol.values()),
            'total_potential_loss': sum(m['total_potential_loss'] for m in risk_metrics_by_symbol.values()),
            'total_potential_gain': sum(m['total_potential_gain'] for m in risk_metrics_by_symbol.values()),
            'total_exposure': sum(m['total_exposure'] for m in risk_metrics_by_symbol.values()),
            'total_positions': len(all_trades)
        }

        log_performance("get_all_trades TOTAL", t_total, f"{len(all_trades)} trades, {len(unique_symbols)} symbols, mem={get_memory_usage_mb():.1f}MB")
        return trades_by_symbol, risk_metrics_by_symbol, total_metrics

    except Exception as e:
        perf_logger.error(f"Error getting trades: {e}")
        import traceback
        traceback.print_exc()
        return {}, {}, {}


# Legacy function for backward compatibility
def get_xauusd_trades():
    trades_by_symbol, risk_metrics_by_symbol, _ = get_all_trades()
    xauusd_trades = trades_by_symbol.get('XAUUSD', [])
    xauusd_metrics = risk_metrics_by_symbol.get('XAUUSD', calculate_risk_metrics_for_symbol([], 'XAUUSD'))
    return xauusd_trades, xauusd_metrics


# Enhanced HTML template with risk management information
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Multi-Symbol Risk Management Dashboard</title>
    <link rel="icon" type="image/png" href="https://cdn-icons-png.flaticon.com/512/4002/4002224.png">
    <style>
        :root {
            --primary-color: #2c3e50;
            --profit-color: #27ae60;
            --loss-color: #e74c3c;
            --warning-color: #f39c12;
            --info-color: #3498db;
            --light-bg: #f8f9fa;
            --card-shadow: 0 4px 6px rgba(0,0,0,0.1);
        }

        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            margin: 0;
            padding: 20px;
            background-color: #f0f2f5;
            color: #333;
        }

        .container {
            max-width: 1400px;
            margin: 0 auto;
        }

        .header {
            background: linear-gradient(135deg, var(--primary-color), #1a2530);
            color: white;
            padding: 25px;
            border-radius: 12px;
            margin-bottom: 25px;
            text-align: center;
            box-shadow: var(--card-shadow);
        }

        .header h1 {
            margin: 0;
            font-size: 2.2em;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 15px;
        }

        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
            gap: 20px;
            margin-bottom: 25px;
        }

        .stat-card {
            background: white;
            padding: 25px;
            border-radius: 12px;
            box-shadow: var(--card-shadow);
            transition: transform 0.3s ease;
        }

        .stat-card:hover {
            transform: translateY(-5px);
        }

        .stat-card.warning {
            border-left: 5px solid var(--warning-color);
        }

        .stat-card.danger {
            border-left: 5px solid var(--loss-color);
        }

        .stat-card.success {
            border-left: 5px solid var(--profit-color);
        }

        .stat-card.info {
            border-left: 5px solid var(--info-color);
        }

        .stat-value {
            font-size: 2.5em;
            font-weight: 700;
            margin: 10px 0;
        }

        .stat-label {
            color: #7f8c8d;
            font-size: 0.9em;
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-bottom: 5px;
        }

        .profit-positive {
            color: var(--profit-color);
        }

        .profit-negative {
            color: var(--loss-color);
        }

        .controls {
            background: white;
            padding: 20px;
            border-radius: 12px;
            margin-bottom: 25px;
            text-align: center;
            box-shadow: var(--card-shadow);
        }

        .refresh-btn {
            background: var(--info-color);
            color: white;
            border: none;
            padding: 12px 24px;
            border-radius: 8px;
            cursor: pointer;
            font-size: 16px;
            margin: 0 10px;
            transition: background 0.3s ease;
            display: inline-flex;
            align-items: center;
            gap: 8px;
        }

        .refresh-btn:hover {
            background: #2980b9;
        }

        .trade-table {
            background: white;
            border-radius: 12px;
            overflow: hidden;
            box-shadow: var(--card-shadow);
            margin-bottom: 25px;
        }

        table {
            width: 100%;
            border-collapse: collapse;
        }

        thead {
            background: var(--primary-color);
        }

        th {
            color: white;
            padding: 18px 15px;
            text-align: left;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            font-size: 0.9em;
        }

        tbody tr {
            border-bottom: 1px solid #eee;
            transition: background 0.2s ease;
        }

        tbody tr:hover {
            background-color: #f8f9fa;
        }

        td {
            padding: 16px 15px;
        }

        .buy-badge, .sell-badge {
            padding: 6px 12px;
            border-radius: 20px;
            font-size: 0.85em;
            font-weight: 600;
            display: inline-block;
        }

        .buy-badge {
            background-color: var(--profit-color);
            color: white;
        }

        .sell-badge {
            background-color: var(--loss-color);
            color: white;
        }

        .risk-indicator {
            display: inline-block;
            width: 12px;
            height: 12px;
            border-radius: 50%;
            margin-right: 8px;
        }

        .risk-high {
            background-color: var(--loss-color);
        }

        .risk-medium {
            background-color: var(--warning-color);
        }

        .risk-low {
            background-color: var(--profit-color);
        }

        .no-trades {
            text-align: center;
            padding: 60px 20px;
            color: #7f8c8d;
        }

        .last-update {
            text-align: center;
            color: #7f8c8d;
            font-size: 0.9em;
            padding: 15px;
        }

        .autotrail-checkbox {
            width: 18px;
            height: 18px;
            cursor: pointer;
            accent-color: var(--info-color);
        }

        .symbol-autotrail-item {
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 6px;
        }

        .symbol-autotrail-item label {
            cursor: pointer;
            font-size: 0.85em;
            color: #666;
        }

        .symbol-autotrail-item input[type="checkbox"] {
            width: 16px;
            height: 16px;
            cursor: pointer;
            accent-color: var(--info-color);
        }

        .autotrail-label {
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 5px;
        }

        .trail-pips-input {
            width: 60px;
            padding: 4px 6px;
            border: 1px solid #ddd;
            border-radius: 4px;
            font-size: 0.9em;
            text-align: center;
        }

        .trail-pips-input:disabled {
            background-color: #f5f5f5;
            color: #999;
            cursor: not-allowed;
        }

        .trail-pips-input:focus {
            outline: none;
            border-color: var(--info-color);
            box-shadow: 0 0 3px rgba(52, 152, 219, 0.3);
        }

        .autotrail-container {
            display: flex;
            align-items: center;
            gap: 8px;
            justify-content: center;
        }

        .status-badge {
            padding: 4px 10px;
            border-radius: 12px;
            font-size: 0.8em;
            font-weight: 600;
            text-transform: uppercase;
        }

        .status-open {
            background-color: var(--profit-color);
            color: white;
        }

        .status-close {
            background-color: #95a5a6;
            color: white;
        }

        .status-waiting {
            background-color: var(--warning-color);
            color: white;
        }

        .symbol-section {
            background: white;
            border-radius: 12px;
            margin-bottom: 25px;
            box-shadow: var(--card-shadow);
            overflow: hidden;
        }

        .symbol-header {
            background: linear-gradient(135deg, var(--primary-color), #1a2530);
            color: white;
            padding: 15px 20px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .symbol-header h3 {
            margin: 0;
            font-size: 1.3em;
        }

        .symbol-stats {
            display: flex;
            gap: 20px;
            font-size: 0.9em;
        }

        .symbol-stat {
            display: flex;
            flex-direction: column;
            align-items: center;
        }

        .symbol-stat-label {
            opacity: 0.8;
            font-size: 0.8em;
        }

        .symbol-stat-value {
            font-weight: 600;
        }

        .symbol-summary {
            background: var(--light-bg);
            padding: 15px 20px;
            border-bottom: 1px solid #eee;
        }

        .symbol-summary-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(100px, 1fr));
            gap: 15px;
        }

        .summary-item {
            text-align: center;
            padding: 8px;
            background: white;
            border-radius: 8px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.05);
        }

        .summary-value {
            font-size: 1.2em;
            font-weight: 700;
            color: var(--primary-color);
        }

        .summary-label {
            font-size: 0.75em;
            color: #7f8c8d;
            margin-top: 4px;
            text-transform: uppercase;
        }

        .total-summary {
            background: linear-gradient(135deg, #1a2530, var(--primary-color));
            color: white;
            padding: 20px;
            border-radius: 12px;
            margin-bottom: 25px;
            box-shadow: var(--card-shadow);
        }

        .total-summary h3 {
            margin: 0 0 15px 0;
        }

        .total-stats {
            display: flex;
            flex-wrap: wrap;
            gap: 20px;
        }

        .total-stat {
            flex: 1;
            min-width: 120px;
            text-align: center;
        }

        .total-stat-value {
            font-size: 1.5em;
            font-weight: 700;
        }

        .total-stat-label {
            opacity: 0.8;
            font-size: 0.85em;
        }

        .risk-summary {
            background: white;
            padding: 25px;
            border-radius: 12px;
            margin-bottom: 25px;
            box-shadow: var(--card-shadow);
        }

        .risk-summary h3 {
            margin-top: 0;
            color: var(--primary-color);
        }

        .risk-metrics {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
            margin-top: 20px;
        }

        .metric-item {
            padding: 15px;
            background: var(--light-bg);
            border-radius: 8px;
        }

        .metric-value {
            font-size: 1.8em;
            font-weight: 700;
            margin: 5px 0;
        }

        .metric-label {
            color: #666;
            font-size: 0.9em;
        }

        /* ===== MOBILE STYLES ===== */
        @media (max-width: 768px) {
            body {
                padding: 8px;
                font-size: 14px;
            }

            .container {
                padding: 0;
            }

            .header {
                padding: 15px;
                margin-bottom: 12px;
                border-radius: 8px;
            }

            .header h1 {
                font-size: 1.2em;
                gap: 8px;
            }

            .header p {
                font-size: 0.8em;
                margin: 5px 0 0 0;
            }

            .controls {
                padding: 10px;
                margin-bottom: 12px;
                display: flex;
                gap: 8px;
                justify-content: center;
            }

            .refresh-btn {
                padding: 10px 16px;
                font-size: 14px;
                margin: 0;
                flex: 1;
                justify-content: center;
            }

            /* Total Portfolio Summary - compact grid */
            .total-summary {
                padding: 12px;
                margin-bottom: 12px;
                border-radius: 8px;
            }

            .total-summary h3 {
                font-size: 1em;
                margin-bottom: 10px;
            }

            .total-stats {
                display: grid;
                grid-template-columns: repeat(3, 1fr);
                gap: 8px;
            }

            .total-stat {
                min-width: unset;
            }

            .total-stat-value {
                font-size: 1em;
            }

            .total-stat-label {
                font-size: 0.65em;
            }

            /* Symbol sections */
            .symbol-section {
                margin-bottom: 12px;
                border-radius: 8px;
            }

            .symbol-header {
                padding: 10px 12px;
                flex-direction: column;
                gap: 6px;
                align-items: flex-start;
            }

            .symbol-header h3 {
                font-size: 1em;
            }

            .symbol-stats {
                font-size: 0.8em;
            }

            /* Symbol summary grid - 3 columns on mobile */
            .symbol-summary {
                padding: 10px;
            }

            .symbol-summary-grid {
                grid-template-columns: repeat(3, 1fr);
                gap: 6px;
            }

            .summary-item {
                padding: 6px 4px;
            }

            .summary-value {
                font-size: 0.9em;
            }

            .summary-label {
                font-size: 0.6em;
            }

            .symbol-autotrail-item label {
                font-size: 0.6em;
            }

            /* === TRADE TABLE -> CARD LAYOUT ON MOBILE === */
            table {
                display: block;
                width: 100%;
            }

            thead {
                display: none;
            }

            tbody {
                display: flex;
                flex-direction: column;
                gap: 8px;
                padding: 8px;
            }

            tbody tr {
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 4px 12px;
                background: white;
                border: 1px solid #e8e8e8;
                border-radius: 8px;
                padding: 10px 12px;
                border-bottom: none;
                position: relative;
            }

            tbody tr:hover {
                background-color: white;
            }

            td {
                padding: 3px 0;
                font-size: 0.85em;
            }

            /* Label each cell with its column name */
            td::before {
                content: attr(data-label);
                display: block;
                font-size: 0.65em;
                color: #7f8c8d;
                text-transform: uppercase;
                letter-spacing: 0.5px;
                font-weight: 600;
            }

            /* Ticket spans full width as card header */
            td:nth-child(1) {
                grid-column: 1 / -1;
                border-bottom: 1px solid #eee;
                padding-bottom: 6px;
                margin-bottom: 2px;
                display: flex;
                justify-content: space-between;
                align-items: center;
            }

            /* Hide Type column (already shown in ticket header) */
            td:nth-child(2) {
                display: none;
            }

            /* Hide Time column on mobile (less critical) */
            td:nth-child(9) {
                display: none;
            }

            /* Autotrail cell spans full width */
            td:last-child {
                grid-column: 1 / -1;
                border-top: 1px solid #eee;
                padding-top: 6px;
                margin-top: 2px;
            }

            .autotrail-container {
                justify-content: flex-start;
            }

            .trail-pips-input {
                width: 50px;
                font-size: 0.85em;
            }

            /* Status badge on mobile */
            td:nth-child(10) {
                grid-column: 1 / -1;
            }

            /* Risk metrics grid */
            .risk-metrics {
                grid-template-columns: repeat(2, 1fr);
                gap: 8px;
            }

            .metric-value {
                font-size: 1.3em;
            }

            .stat-value {
                font-size: 1.8em;
            }

            .stats-grid {
                grid-template-columns: 1fr;
                gap: 10px;
            }

            .no-trades {
                padding: 30px 15px;
            }
        }

        /* Extra small phones */
        @media (max-width: 380px) {
            .total-stats {
                grid-template-columns: repeat(2, 1fr);
            }

            .symbol-summary-grid {
                grid-template-columns: repeat(2, 1fr);
            }

            .header h1 {
                font-size: 1em;
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>💰 Multi-Symbol Risk Management Dashboard</h1>
            <p>Monitor positions, exposure, and potential losses across all symbols</p>
        </div>

        <div class="controls">
            <button class="refresh-btn" onclick="refreshData()">
                <span>🔄</span> Refresh Data
            </button>
            <button class="refresh-btn" onclick="location.reload()">
                <span>↻</span> Reload Page
            </button>
            <a href="/alert" class="refresh-btn" style="text-decoration:none; color:white;">
                <span>🔔</span> Price Alerts
            </a>
            <a href="/future_risk" class="refresh-btn" style="text-decoration:none; color:white;">
                <span>🔮</span> Future Risk
            </a>
            <a href="/trading" class="refresh-btn" style="text-decoration:none; color:white;">
                <span>📊</span> Trading
            </a>
        </div>

        <div id="risk-summary-container">
            <!-- Risk summary will be loaded here -->
        </div>

        <div id="stats-container">
            <!-- Stats cards will be loaded here -->
        </div>

        <div id="trades-container">
            <!-- Trades table will be loaded here -->
        </div>

        <div class="last-update" id="last-update">
            Last updated: <span id="update-time">Loading...</span>
        </div>
    </div>

    <script>
        // Store autotrail state for each ticket (persists across refreshes)
        const autotrailState = {};
        const trailPipsState = {};
        // Store per-symbol autotrail-new-orders setting
        const symbolAutotrailState = {};

        function toggleSymbolAutotrail(symbol) {
            const newValue = !symbolAutotrailState[symbol];
            symbolAutotrailState[symbol] = newValue;
            
            console.log(`Autotrail new orders for ${symbol}: ${newValue ? 'ON' : 'OFF'}`);
            
            fetch('/symbol_autotrail', {
                method: 'POST',
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ symbol: symbol, enabled: newValue })
            })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    console.log(`Symbol autotrail setting saved for ${symbol}`);
                } else {
                    console.error('Failed to save symbol autotrail:', data.error);
                }
            })
            .catch(error => {
                console.error('Error saving symbol autotrail:', error);
            });
        }

        function formatNumber(num) {
            if (num === null || num === undefined) return '0.00';
            return num.toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
        }

        function formatPrice(num) {
            if (num === null || num === undefined) return '0.000';
            return num.toFixed(3);
        }

        function toggleAutotrail(ticket) {
            const newValue = !autotrailState[ticket];
            autotrailState[ticket] = newValue;
            
            // Get current trail pips value from state (always synced from server)
            const trailPips = trailPipsState[ticket] || 20;
            
            console.log(`Autotrail for ticket ${ticket}: ${newValue ? 'ON' : 'OFF'} (${trailPips} = ${trailPips * 100} MT5 points)`);
            
            // Update autotrail in database via API (include trail_pips)
            fetch('/autotrail', {
                method: 'POST',
                credentials: 'same-origin',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    ticket: ticket,
                    autotrail: newValue,
                    trail_pips: trailPips
                })
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    console.log(`Autotrail updated in database for ticket ${ticket}`);
                } else {
                    console.error('Failed to update autotrail:', data.error);
                }
            })
            .catch(error => {
                console.error('Error updating autotrail:', error);
            });
        }

        function updateTrailPips(ticket) {
            const trailInput = document.getElementById(`trail-pips-${ticket}`);
            if (!trailInput) return;
            
            const trailPips = parseInt(trailInput.value) || 20;
            trailPipsState[ticket] = trailPips;
            
            console.log(`Trail pips for ticket ${ticket} updated to ${trailPips}`);
            
            // Update trail_pips in database via API
            fetch('/trail_pips', {
                method: 'POST',
                credentials: 'same-origin',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    ticket: ticket,
                    trail_pips: trailPips
                })
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    console.log(`Trail pips saved for ticket ${ticket}`);
                } else {
                    console.error('Failed to update trail pips:', data.error);
                }
            })
            .catch(error => {
                console.error('Error updating trail pips:', error);
            });
        }

        function refreshData() {
            fetch('/data', {
                credentials: 'same-origin'
            })
                .then(response => {
                    if (response.status === 401) {
                        location.reload();  // Trigger re-authentication
                        return;
                    }
                    return response.json();
                })
                .then(data => {
                    if (data) updateDisplay(data);
                })
                .catch(error => {
                    console.error('Error:', error);
                });
        }

        function getRiskClass(profit, potentialLoss) {
            if (potentialLoss === 0) return 'info';
            const riskRatio = Math.abs(profit / potentialLoss);
            if (riskRatio > 2) return 'success';
            if (riskRatio > 1) return 'warning';
            return 'danger';
        }

        function updateDisplay(data) {
            // Update total summary across all symbols
            // Sync per-symbol autotrail settings from server (default: OFF for new symbols)
            const serverAutotrailSettings = data.symbol_autotrail_settings || {};
            for (const [sym, enabled] of Object.entries(serverAutotrailSettings)) {
                if (symbolAutotrailState[sym] === undefined) {
                    symbolAutotrailState[sym] = enabled;
                }
            }
            // Explicit default false for symbols not in server (Auto-Trail New Orders off by default)
            for (const sym of Object.keys(data.trades_by_symbol || {})) {
                if (symbolAutotrailState[sym] === undefined) {
                    symbolAutotrailState[sym] = false;
                }
            }

            const totalMetrics = data.total_metrics || {};
            const riskSummaryHtml = `
                <div class="total-summary">
                    <h3>📊 Total Portfolio Summary</h3>
                    <div class="total-stats">
                        <div class="total-stat">
                            <div class="total-stat-value">${data.total_trades}</div>
                            <div class="total-stat-label">Total Positions</div>
                        </div>
                        <div class="total-stat">
                            <div class="total-stat-value ${totalMetrics.total_profit >= 0 ? 'profit-positive' : 'profit-negative'}">
                                $${formatNumber(totalMetrics.total_profit || 0)}
                            </div>
                            <div class="total-stat-label">Total P&L</div>
                        </div>
                        <div class="total-stat">
                            <div class="total-stat-value">$${formatNumber(totalMetrics.total_exposure || 0)}</div>
                            <div class="total-stat-label">Total Exposure</div>
                        </div>
                        <div class="total-stat">
                            <div class="total-stat-value ${(totalMetrics.total_potential_loss || 0) <= 0 ? 'profit-positive' : 'profit-negative'}">$${formatNumber(totalMetrics.total_potential_loss || 0)}</div>
                            <div class="total-stat-label">Potential Loss</div>
                        </div>
                        <div class="total-stat">
                            <div class="total-stat-value profit-positive">$${formatNumber(totalMetrics.total_potential_gain || 0)}</div>
                            <div class="total-stat-label">Potential Gain</div>
                        </div>
                        <div class="total-stat">
                            <div class="total-stat-value">${data.symbols ? data.symbols.length : 0}</div>
                            <div class="total-stat-label">Active Symbols</div>
                        </div>
                    </div>
                </div>
            `;
            document.getElementById('risk-summary-container').innerHTML = riskSummaryHtml;

            // Hide stats container (we'll show per-symbol stats)
            document.getElementById('stats-container').innerHTML = '';

            // Update trades grouped by symbol
            const symbols = data.symbols || [];
            
            if (symbols.length > 0) {
                let tradesHtml = '';
                
                symbols.forEach(symbol => {
                    const symbolTrades = data.trades_by_symbol[symbol] || [];
                    const symbolMetrics = data.risk_metrics_by_symbol[symbol] || {};
                    
                    // Calculate risk/reward ratio for symbol
                    const symbolRR = symbolMetrics.total_potential_loss !== 0 
                        ? Math.abs(symbolMetrics.total_profit / symbolMetrics.total_potential_loss) 
                        : 0;
                    
                    tradesHtml += `
                        <div class="symbol-section">
                            <div class="symbol-header">
                                <h3>📈 ${symbol} - ${symbolMetrics.description || symbol}</h3>
                                <div class="symbol-stats">
                                    <div class="symbol-stat">
                                        <span class="symbol-stat-label">Price</span>
                                        <span class="symbol-stat-value">${symbolMetrics.current_price || 0}</span>
                                    </div>
                                </div>
                            </div>
                            
                            <!-- Symbol Portfolio Summary -->
                            <div class="symbol-summary">
                                <div class="symbol-summary-grid">
                                    <div class="summary-item">
                                        <div class="summary-value">${symbolMetrics.total_positions || 0}</div>
                                        <div class="summary-label">Positions</div>
                                    </div>
                                    <div class="summary-item">
                                        <div class="summary-value">${symbolMetrics.total_volume || 0}</div>
                                        <div class="summary-label">Total Volume</div>
                                    </div>
                                    <div class="summary-item">
                                        <div class="summary-value">${symbolMetrics.total_buy_volume || 0} / ${symbolMetrics.total_sell_volume || 0}</div>
                                        <div class="summary-label">Buy / Sell Vol</div>
                                    </div>
                                    <div class="summary-item">
                                        <div class="summary-value ${symbolMetrics.net_volume >= 0 ? 'profit-positive' : 'profit-negative'}">${symbolMetrics.net_volume > 0 ? '+' : ''}${symbolMetrics.net_volume || 0}</div>
                                        <div class="summary-label">Net Volume</div>
                                    </div>
                                    <div class="summary-item">
                                        <div class="summary-value ${symbolMetrics.total_profit >= 0 ? 'profit-positive' : 'profit-negative'}">$${formatNumber(symbolMetrics.total_profit || 0)}</div>
                                        <div class="summary-label">P&L</div>
                                    </div>
                                    <div class="summary-item">
                                        <div class="summary-value">$${formatNumber(symbolMetrics.total_swap || 0)}</div>
                                        <div class="summary-label">Swap</div>
                                    </div>
                                    <div class="summary-item">
                                        <div class="summary-value">$${formatNumber(symbolMetrics.total_exposure || 0)}</div>
                                        <div class="summary-label">Exposure</div>
                                    </div>
                                    <div class="summary-item">
                                        <div class="summary-value ${(symbolMetrics.total_potential_loss || 0) <= 0 ? 'profit-positive' : 'profit-negative'}">$${formatNumber(symbolMetrics.total_potential_loss || 0)}</div>
                                        <div class="summary-label">Potential Loss</div>
                                    </div>
                                    <div class="summary-item">
                                        <div class="summary-value profit-positive">$${formatNumber(symbolMetrics.total_potential_gain || 0)}</div>
                                        <div class="summary-label">Potential Gain</div>
                                    </div>
                                    <div class="summary-item">
                                        <div class="summary-value ${symbolRR >= 1 ? 'profit-positive' : 'profit-negative'}">${symbolRR.toFixed(2)}:1</div>
                                        <div class="summary-label">Risk/Reward</div>
                                    </div>
                                    <div class="summary-item symbol-autotrail-item">
                                        <div class="summary-value">
                                            <input type="checkbox" 
                                                   id="symbol-autotrail-${symbol}"
                                                   ${symbolAutotrailState[symbol] ? 'checked' : ''}
                                                   onchange="toggleSymbolAutotrail('${symbol}')">
                                        </div>
                                        <div class="summary-label">
                                            <label for="symbol-autotrail-${symbol}">Auto-Trail New Orders</label>
                                        </div>
                                    </div>
                                </div>
                            </div>
                            
                            <table>
                                <thead>
                                    <tr>
                                        <th>Ticket</th>
                                        <th>Type</th>
                                        <th>Volume</th>
                                        <th>Open Price</th>
                                        <th>Current</th>
                                        <th>SL</th>
                                        <th>Profit</th>
                                        <th>Risk</th>
                                        <th>Time</th>
                                        <th>Status</th>
                                        <th>Autotrail</th>
                                    </tr>
                                </thead>
                                <tbody>
                    `;
                    
                    symbolTrades.forEach(trade => {
                        const slDisplay = trade.sl ? trade.sl : 'No SL';
                        const riskClass = trade.total_risk > 1000 ? 'risk-high' : 
                                         trade.total_risk > 500 ? 'risk-medium' : 'risk-low';

                        // Sync autotrail state from database
                        // Use symbol-specific default trail from server config (correct per symbol)
                        const defaultTrail = symbolMetrics.default_trail || 50;
                        if (autotrailState[trade.ticket] === undefined) {
                            autotrailState[trade.ticket] = trade.autotrail || false;
                        }
                        // Use symbol default_trail as the value; override only if user has customized it
                        // (trade.trail_pips comes from DB, defaultTrail from symbol config)
                        trailPipsState[trade.ticket] = trade.trail_pips || defaultTrail;
                        const isChecked = autotrailState[trade.ticket] ? 'checked' : '';
                        const currentTrailPips = trailPipsState[trade.ticket];

                        tradesHtml += `
                            <tr>
                                <td data-label="Ticket">${trade.ticket} <span class="${trade.type.toLowerCase()}-badge">${trade.type}</span></td>
                                <td data-label="Type"><span class="${trade.type.toLowerCase()}-badge">${trade.type}</span></td>
                                <td data-label="Volume">${trade.volume}</td>
                                <td data-label="Open">${trade.price_open}</td>
                                <td data-label="Current">${trade.price_current}</td>
                                <td data-label="SL">${slDisplay}</td>
                                <td data-label="Profit" class="${trade.profit >= 0 ? 'profit-positive' : 'profit-negative'}">
                                    $${formatNumber(trade.profit)}
                                </td>
                                <td data-label="Risk" class="${trade.total_risk > 0 ? 'profit-negative' : ''}">
                                    <span class="risk-indicator ${riskClass}"></span>
                                    $${formatNumber(trade.total_risk)}
                                </td>
                                <td data-label="Time">${trade.time}</td>
                                <td data-label="Status">
                                    <span class="status-badge status-${trade.status || 'open'}">${trade.status || 'open'}</span>
                                </td>
                                <td data-label="Autotrail">
                                    <div class="autotrail-container">
                                        <input type="checkbox" 
                                               class="autotrail-checkbox" 
                                               id="autotrail-${trade.ticket}"
                                               ${isChecked}
                                               onchange="toggleAutotrail(${trade.ticket})">
                                        <input type="number" 
                                               class="trail-pips-input" 
                                               id="trail-pips-${trade.ticket}"
                                               value="${currentTrailPips}"
                                               min="2"
                                               max="200"
                                               onchange="updateTrailPips(${trade.ticket})"
                                               title="Trail distance (pips)">
                                    </div>
                                </td>
                            </tr>
                        `;
                    });
                    
                    tradesHtml += `
                                </tbody>
                            </table>
                        </div>
                    `;
                });
                
                document.getElementById('trades-container').innerHTML = tradesHtml;
            } else {
                document.getElementById('trades-container').innerHTML = `
                    <div class="no-trades">
                        <h3>📭 No Open Trades</h3>
                        <p>There are currently no open positions across any symbol.</p>
                    </div>
                `;
            }

            // Update timestamp
            document.getElementById('update-time').textContent = data.timestamp;
        }

        // Load initial data
        refreshData();

        // Auto-refresh every 10 seconds
        setInterval(refreshData, 10000);
    </script>
</body>
</html>
"""


# Alerts HTML page template
ALERTS_HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Alerts - MT5 Dashboard</title>
    <link rel="icon" type="image/png" href="https://cdn-icons-png.flaticon.com/512/4002/4002224.png">
    <style>
        :root {
            --primary-color: #2c3e50;
            --profit-color: #27ae60;
            --loss-color: #e74c3c;
            --warning-color: #f39c12;
            --info-color: #3498db;
            --light-bg: #f8f9fa;
            --card-shadow: 0 4px 6px rgba(0,0,0,0.1);
        }

        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            margin: 0;
            padding: 20px;
            background-color: #f0f2f5;
            color: #333;
        }

        .container {
            max-width: 900px;
            margin: 0 auto;
        }

        .header {
            background: linear-gradient(135deg, var(--primary-color), #1a2530);
            color: white;
            padding: 25px;
            border-radius: 12px;
            margin-bottom: 25px;
            text-align: center;
            box-shadow: var(--card-shadow);
        }

        .header h1 {
            margin: 0;
            font-size: 2em;
        }

        .header p {
            margin: 8px 0 0 0;
            opacity: 0.85;
        }

        .nav-link {
            color: white;
            text-decoration: none;
            background: rgba(255,255,255,0.15);
            padding: 6px 16px;
            border-radius: 20px;
            font-size: 0.85em;
            display: inline-block;
            margin-top: 10px;
        }

        .nav-link:hover {
            background: rgba(255,255,255,0.3);
        }

        .card {
            background: white;
            border-radius: 12px;
            box-shadow: var(--card-shadow);
            padding: 25px;
            margin-bottom: 25px;
        }

        .card h2 {
            margin-top: 0;
            color: var(--primary-color);
            font-size: 1.3em;
        }

        .form-row {
            display: flex;
            gap: 12px;
            align-items: flex-end;
            flex-wrap: wrap;
        }

        .form-group {
            flex: 1;
            min-width: 120px;
        }

        .form-group label {
            display: block;
            font-size: 0.85em;
            color: #666;
            margin-bottom: 5px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }

        .form-group input, .form-group select {
            width: 100%;
            padding: 10px 12px;
            border: 1px solid #ddd;
            border-radius: 8px;
            font-size: 1em;
            box-sizing: border-box;
            transition: border-color 0.2s;
        }

        .form-group input:focus, .form-group select:focus {
            outline: none;
            border-color: var(--info-color);
            box-shadow: 0 0 4px rgba(52, 152, 219, 0.2);
        }

        .btn {
            padding: 10px 24px;
            border: none;
            border-radius: 8px;
            cursor: pointer;
            font-size: 1em;
            font-weight: 600;
            transition: background 0.2s, transform 0.1s;
        }

        .btn:active {
            transform: scale(0.97);
        }

        .btn-primary {
            background: var(--info-color);
            color: white;
        }

        .btn-primary:hover {
            background: #2980b9;
        }

        .btn-danger {
            background: var(--loss-color);
            color: white;
            padding: 6px 14px;
            font-size: 0.85em;
        }

        .btn-danger:hover {
            background: #c0392b;
        }

        .btn-small {
            padding: 5px 12px;
            font-size: 0.8em;
        }

        .btn-danger {
            background: #e74c3c;
            color: white;
            border: none;
            padding: 8px 16px;
            border-radius: 6px;
            cursor: pointer;
            font-size: 0.9em;
            font-weight: 600;
        }
        .btn-danger:hover {
            background: #c0392b;
        }

        table {
            width: 100%;
            border-collapse: collapse;
        }

        thead {
            background: var(--primary-color);
        }

        th {
            color: white;
            padding: 14px 12px;
            text-align: left;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            font-size: 0.85em;
        }

        tbody tr {
            border-bottom: 1px solid #eee;
            transition: background 0.2s;
        }

        tbody tr:hover {
            background-color: #f8f9fa;
        }

        td {
            padding: 12px;
        }

        .badge {
            padding: 4px 10px;
            border-radius: 12px;
            font-size: 0.8em;
            font-weight: 600;
            text-transform: uppercase;
            display: inline-block;
        }

        .badge-active {
            background: var(--profit-color);
            color: white;
        }

        .badge-triggered {
            background: var(--warning-color);
            color: white;
        }

        .badge-inactive {
            background: #95a5a6;
            color: white;
        }

        .badge-above {
            background: #e8f5e9;
            color: var(--profit-color);
            border: 1px solid var(--profit-color);
        }

        .badge-below {
            background: #fce4ec;
            color: var(--loss-color);
            border: 1px solid var(--loss-color);
        }

        .badge-loss {
            background: #fff3e0;
            color: var(--warning-color);
            border: 1px solid var(--warning-color);
        }

        .badge-price-type {
            background: #e3f2fd;
            color: var(--info-color);
            border: 1px solid var(--info-color);
        }

        .current-price {
            font-weight: 600;
            color: var(--primary-color);
        }

        .distance {
            font-size: 0.8em;
            color: #999;
        }

        .no-alerts {
            text-align: center;
            padding: 40px;
            color: #999;
        }

        .status-info {
            font-size: 0.8em;
            color: #999;
            text-align: center;
            margin-top: 10px;
        }

        @media (max-width: 768px) {
            body { padding: 8px; }
            .header { padding: 15px; }
            .header h1 { font-size: 1.3em; }
            .card { padding: 15px; }
            .form-row { flex-direction: column; gap: 10px; }
            .form-group { min-width: unset; }
            
            thead { display: none; }
            tbody { display: flex; flex-direction: column; gap: 8px; }
            tbody tr {
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 4px 12px;
                border: 1px solid #e8e8e8;
                border-radius: 8px;
                padding: 10px 12px;
                border-bottom: none;
            }
            td { padding: 3px 0; font-size: 0.85em; }
            td::before {
                content: attr(data-label);
                display: block;
                font-size: 0.65em;
                color: #7f8c8d;
                text-transform: uppercase;
                font-weight: 600;
            }
            td:first-child {
                grid-column: 1 / -1;
                border-bottom: 1px solid #eee;
                padding-bottom: 6px;
            }
            td:last-child {
                grid-column: 1 / -1;
                border-top: 1px solid #eee;
                padding-top: 6px;
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🔔 Alerts</h1>
            <p>Get notified when price or potential loss crosses your threshold</p>
            <a href="/" class="nav-link">← Back to Dashboard</a>
        </div>

        <div class="card">
            <h2>➕ Create New Alert</h2>
            <div class="form-row">
                <div class="form-group">
                    <label>Symbol</label>
                    <input type="text" id="alert-symbol" placeholder="e.g. XAUUSD" list="symbol-list">
                    <datalist id="symbol-list"></datalist>
                </div>
                <div class="form-group">
                    <label>Condition</label>
                    <select id="alert-condition" onchange="onConditionChange()">
                        <option value="price_above">Price goes ABOVE</option>
                        <option value="price_below">Price goes BELOW</option>
                        <option value="loss_above">Potential Loss goes ABOVE $</option>
                        <option value="loss_below">Potential Loss goes BELOW $</option>
                    </select>
                </div>
                <div class="form-group">
                    <label id="alert-value-label">Price</label>
                    <input type="number" id="alert-price" step="any" placeholder="Target price">
                </div>
                <div class="form-group" style="flex: 0;">
                    <label>&nbsp;</label>
                    <button class="btn btn-primary" onclick="createAlert()">Create Alert</button>
                </div>
            </div>
            <div id="create-feedback" style="margin-top:10px; font-size:0.9em;"></div>
        </div>

        <div class="card">
            <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px;">
                <h2 style="margin:0;">📋 Active Alerts</h2>
                <button class="btn btn-danger" id="clear-triggered-btn" style="display:none;" onclick="clearTriggered()">🗑️ Clear Triggered</button>
            </div>
            <div id="alerts-container">
                <div class="no-alerts">Loading...</div>
            </div>
        </div>

        <div class="status-info">
            Alerts checked every 10 seconds | Triggered alerts print to server console<br>
            Last refreshed: <span id="last-refresh">-</span>
        </div>
    </div>

    <script>
        function formatPrice(num) {
            if (num === null || num === undefined || num === 0) return '-';
            if (num >= 100) return num.toFixed(2);
            if (num >= 1) return num.toFixed(4);
            return num.toFixed(5);
        }

        function onConditionChange() {
            const cond = document.getElementById('alert-condition').value;
            const label = document.getElementById('alert-value-label');
            const input = document.getElementById('alert-price');
            if (cond.startsWith('loss_')) {
                label.textContent = 'Amount ($)';
                input.placeholder = 'e.g. 500';
            } else {
                label.textContent = 'Price';
                input.placeholder = 'Target price';
            }
        }

        function createAlert() {
            const symbol = document.getElementById('alert-symbol').value.trim().toUpperCase();
            const condValue = document.getElementById('alert-condition').value;
            const price = parseFloat(document.getElementById('alert-price').value);
            const feedback = document.getElementById('create-feedback');

            // Parse combined condition: "price_above" -> type=price, condition=above
            const parts = condValue.split('_');
            const alertType = parts[0] === 'loss' ? 'potential_loss' : 'price';
            const condition = parts[1]; // 'above' or 'below'

            if (!symbol) { feedback.innerHTML = '<span style="color:red">Please enter a symbol</span>'; return; }
            if (isNaN(price)) { feedback.innerHTML = '<span style="color:red">Please enter a valid value</span>'; return; }

            feedback.innerHTML = 'Creating...';

            fetch('/alerts/create', {
                method: 'POST',
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ symbol, condition, price, alert_type: alertType })
            })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    feedback.innerHTML = '<span style="color:green">Alert created!</span>';
                    document.getElementById('alert-symbol').value = '';
                    document.getElementById('alert-price').value = '';
                    setTimeout(() => { feedback.innerHTML = ''; }, 2000);
                    refreshAlerts();
                } else {
                    feedback.innerHTML = '<span style="color:red">' + (data.error || 'Error') + '</span>';
                }
            })
            .catch(err => { feedback.innerHTML = '<span style="color:red">Network error</span>'; });
        }

        function deleteAlert(id) {
            if (!confirm('Delete this alert?')) return;
            fetch('/alerts/delete', {
                method: 'POST',
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ id })
            })
            .then(r => r.json())
            .then(() => refreshAlerts())
            .catch(err => console.error('Error:', err));
        }

        function toggleAlert(id, currentActive) {
            fetch('/alerts/toggle', {
                method: 'POST',
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ id, active: !currentActive })
            })
            .then(r => r.json())
            .then(() => refreshAlerts())
            .catch(err => console.error('Error:', err));
        }

        function clearTriggered() {
            if (!confirm('Delete all triggered alerts?')) return;
            fetch('/alerts/clear_triggered', {
                method: 'POST',
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({})
            })
            .then(r => r.json())
            .then(data => {
                if (data.deleted > 0) console.log('Cleared ' + data.deleted + ' triggered alert(s)');
                refreshAlerts();
            })
            .catch(err => console.error('Error:', err));
        }

        function refreshAlerts() {
            fetch('/alerts/data', { credentials: 'same-origin' })
                .then(r => {
                    if (r.status === 401) { location.reload(); return; }
                    return r.json();
                })
                .then(data => {
                    if (!data) return;
                    const alerts = data.alerts || [];
                    updateAlertsList(alerts);
                    updateSymbolList(data.supported_symbols || []);
                    // Show/hide "Clear Triggered" button
                    const hasTriggered = alerts.some(a => a.triggered);
                    document.getElementById('clear-triggered-btn').style.display = hasTriggered ? 'inline-block' : 'none';
                    document.getElementById('last-refresh').textContent = new Date().toLocaleTimeString();
                })
                .catch(err => console.error('Error:', err));
        }

        function updateSymbolList(symbols) {
            const datalist = document.getElementById('symbol-list');
            datalist.innerHTML = symbols.map(s => '<option value="' + s + '">').join('');
        }

        function formatUsd(num) {
            if (num === null || num === undefined) return '-';
            return '$' + num.toFixed(2);
        }

        function updateAlertsList(alerts) {
            const container = document.getElementById('alerts-container');

            if (alerts.length === 0) {
                container.innerHTML = '<div class="no-alerts">No alerts configured. Create one above.</div>';
                return;
            }

            let html = '<table><thead><tr>';
            html += '<th>Symbol</th><th>Type</th><th>Condition</th><th>Target</th><th>Current</th><th>Distance</th><th>Status</th><th>Actions</th>';
            html += '</tr></thead><tbody>';

            alerts.forEach(alert => {
                const isLoss = (alert.alert_type === 'potential_loss');
                const statusClass = alert.triggered ? 'badge-triggered' : (alert.active ? 'badge-active' : 'badge-inactive');
                const statusText = alert.triggered ? 'Triggered' : (alert.active ? 'Active' : 'Paused');
                const condClass = alert.condition === 'above' ? 'badge-above' : 'badge-below';
                const arrow = alert.condition === 'above' ? '↑' : '↓';
                const typeClass = isLoss ? 'badge-loss' : 'badge-price-type';
                const typeLabel = isLoss ? '💰 Loss' : '📈 Price';

                let currentDisplay, targetDisplay, distance;
                if (isLoss) {
                    const currentVal = alert.current_value || 0;
                    targetDisplay = formatUsd(alert.price);
                    currentDisplay = formatUsd(currentVal);
                    const diff = alert.price - currentVal;
                    distance = (currentVal !== 0) ? ((diff >= 0 ? '+' : '') + formatUsd(diff)) : '-';
                } else {
                    targetDisplay = formatPrice(alert.price);
                    currentDisplay = formatPrice(alert.current_price);
                    if (alert.current_price > 0) {
                        const diff = alert.price - alert.current_price;
                        const pct = ((diff / alert.current_price) * 100).toFixed(2);
                        distance = (diff >= 0 ? '+' : '') + formatPrice(diff) + ' (' + pct + '%)';
                    } else {
                        distance = '-';
                    }
                }

                html += '<tr>';
                html += '<td data-label="Symbol"><strong>' + alert.symbol + '</strong></td>';
                html += '<td data-label="Type"><span class="badge ' + typeClass + '">' + typeLabel + '</span></td>';
                html += '<td data-label="Condition"><span class="badge ' + condClass + '">' + arrow + ' ' + alert.condition + '</span></td>';
                html += '<td data-label="Target">' + targetDisplay + '</td>';
                html += '<td data-label="Current" class="current-price">' + currentDisplay + '</td>';
                html += '<td data-label="Distance" class="distance">' + distance + '</td>';
                html += '<td data-label="Status"><span class="badge ' + statusClass + '">' + statusText + '</span></td>';
                html += '<td data-label="Actions">';
                if (!alert.triggered) {
                    html += '<button class="btn btn-small" style="background:#95a5a6;color:white;margin-right:4px;" onclick="toggleAlert(' + alert.id + ',' + (alert.active ? 'true' : 'false') + ')">';
                    html += alert.active ? 'Pause' : 'Resume';
                    html += '</button>';
                }
                html += '<button class="btn btn-danger btn-small" onclick="deleteAlert(' + alert.id + ')">Delete</button>';
                html += '</td>';
                html += '</tr>';
            });

            html += '</tbody></table>';
            container.innerHTML = html;
        }

        // Initial load
        refreshAlerts();
        // Auto-refresh every 10 seconds
        setInterval(refreshAlerts, 10000);
    </script>
</body>
</html>
"""


# Trading page HTML template
TRADING_HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Trading - MT5 Dashboard</title>
    <link rel="icon" type="image/png" href="https://cdn-icons-png.flaticon.com/512/4002/4002224.png">
    <style>
        :root {
            --primary-color: #2c3e50;
            --profit-color: #27ae60;
            --loss-color: #e74c3c;
            --warning-color: #f39c12;
            --info-color: #3498db;
            --light-bg: #f8f9fa;
            --card-shadow: 0 4px 6px rgba(0,0,0,0.1);
        }
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            margin: 0; padding: 20px;
            background-color: #f0f2f5; color: #333;
        }
        .container { max-width: 960px; margin: 0 auto; }
        .header {
            background: linear-gradient(135deg, var(--primary-color), #1a2530);
            color: white; padding: 25px; border-radius: 12px;
            margin-bottom: 25px; text-align: center;
            box-shadow: var(--card-shadow);
        }
        .header h1 { margin: 0; font-size: 2em; }
        .header p { margin: 8px 0 0 0; opacity: 0.85; }
        .nav-link {
            color: white; text-decoration: none;
            background: rgba(255,255,255,0.15);
            padding: 6px 16px; border-radius: 20px;
            font-size: 0.85em; display: inline-block; margin-top: 10px;
        }
        .nav-link:hover { background: rgba(255,255,255,0.3); }
        .card {
            background: white; border-radius: 12px;
            box-shadow: var(--card-shadow);
            padding: 25px; margin-bottom: 25px;
        }
        .card h2 { margin-top: 0; color: var(--primary-color); font-size: 1.3em; }
        .form-row {
            display: flex; gap: 12px; align-items: flex-end; flex-wrap: wrap;
        }
        .form-group { flex: 1; min-width: 120px; }
        .form-group label {
            display: block; font-size: 0.85em; color: #666;
            margin-bottom: 5px; font-weight: 600;
            text-transform: uppercase; letter-spacing: 0.5px;
        }
        .form-group input, .form-group select {
            width: 100%; padding: 10px 12px;
            border: 1px solid #ddd; border-radius: 8px;
            font-size: 1em; box-sizing: border-box;
            transition: border-color 0.2s;
        }
        .form-group input:focus, .form-group select:focus {
            outline: none; border-color: var(--info-color);
            box-shadow: 0 0 4px rgba(52,152,219,0.2);
        }
        .btn {
            padding: 10px 24px; border: none; border-radius: 8px;
            cursor: pointer; font-size: 1em; font-weight: 600;
            transition: background 0.2s, transform 0.1s;
        }
        .btn:active { transform: scale(0.97); }
        .btn-primary { background: var(--info-color); color: white; }
        .btn-primary:hover { background: #2980b9; }
        .btn-success { background: var(--profit-color); color: white; }
        .btn-success:hover { background: #219a52; }
        .btn:disabled {
            background: #bdc3c7; cursor: not-allowed; opacity: 0.6;
        }
        .btn-row { display: flex; gap: 12px; margin-top: 15px; }
        table { width: 100%; border-collapse: collapse; margin-top: 15px; }
        thead { background: var(--primary-color); }
        th {
            color: white; padding: 12px; text-align: left;
            font-weight: 600; text-transform: uppercase;
            letter-spacing: 0.5px; font-size: 0.85em;
        }
        tbody tr { border-bottom: 1px solid #eee; transition: background 0.2s; }
        tbody tr:hover { background-color: #f8f9fa; }
        td { padding: 10px 12px; }
        .badge {
            padding: 4px 10px; border-radius: 12px;
            font-size: 0.8em; font-weight: 600;
            display: inline-block;
        }
        .badge-buy { background: #e8f5e9; color: var(--profit-color); border: 1px solid var(--profit-color); }
        .badge-sell { background: #fce4ec; color: var(--loss-color); border: 1px solid var(--loss-color); }
        .badge-pending { background: #fff3e0; color: var(--warning-color); border: 1px solid var(--warning-color); }
        .result-success { color: var(--profit-color); font-weight: 600; }
        .result-fail { color: var(--loss-color); font-weight: 600; }
        .current-price-info {
            background: var(--light-bg); padding: 12px 18px;
            border-radius: 8px; margin-bottom: 15px;
            font-size: 0.95em; display: none;
        }
        .current-price-info strong { color: var(--primary-color); }
        .status-info {
            font-size: 0.8em; color: #999; text-align: center; margin-top: 10px;
        }
        #execution-results { margin-top: 15px; }
        .exec-summary {
            padding: 15px; border-radius: 8px; margin-top: 10px;
            font-weight: 600; font-size: 1.05em;
        }
        .exec-summary.success { background: #e8f5e9; color: var(--profit-color); }
        .exec-summary.partial { background: #fff3e0; color: var(--warning-color); }
        .exec-summary.fail { background: #fce4ec; color: var(--loss-color); }
        @media (max-width: 768px) {
            body { padding: 8px; }
            .header { padding: 15px; }
            .header h1 { font-size: 1.3em; }
            .card { padding: 15px; }
            .form-row { flex-direction: column; gap: 10px; }
            .form-group { min-width: unset; }
            .btn-row { flex-direction: column; }
            thead { display: none; }
            tbody { display: flex; flex-direction: column; gap: 8px; }
            tbody tr {
                display: grid; grid-template-columns: 1fr 1fr;
                gap: 4px 12px; border: 1px solid #e8e8e8;
                border-radius: 8px; padding: 10px 12px;
            }
            td { padding: 3px 0; font-size: 0.85em; }
            td::before {
                content: attr(data-label);
                display: block; font-size: 0.65em; color: #7f8c8d;
                text-transform: uppercase; font-weight: 600;
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>📊 Trading - Buy/Sell Stop Orders</h1>
            <p>Create multiple buy or sell stop orders with even spacing</p>
            <a href="/" class="nav-link">← Back to Dashboard</a>
        </div>

        <div class="card">
            <h2>➕ Configure Orders</h2>
            <div class="form-row">
                <div class="form-group">
                    <label>Order Type</label>
                    <select id="trade-order-type" onchange="onOrderTypeChange()">
                        <option value="buy_stop">Buy Stop</option>
                        <option value="sell_stop">Sell Stop</option>
                    </select>
                </div>
                <div class="form-group">
                    <label>Symbol</label>
                    <select id="trade-symbol" onchange="onSymbolChange()">
                        <option value="">-- Select --</option>
                    </select>
                </div>
                <div class="form-group">
                    <label>Volume (lots)</label>
                    <input type="number" id="trade-volume" step="0.01" min="0.01" value="0.01" placeholder="0.01">
                </div>
            </div>
            <div class="current-price-info" id="current-price-info">
                Current price for <strong id="cp-symbol">-</strong>: <strong id="cp-price">-</strong>
            </div>
            <div class="form-row" style="margin-top:12px;">
                <div class="form-group">
                    <label id="trade-first-price-label">First Buy Stop Price</label>
                    <input type="number" id="trade-first-price" step="any" placeholder="e.g. 2950.00">
                </div>
                <div class="form-group">
                    <label>Distance Between Orders</label>
                    <input type="number" id="trade-distance" step="any" placeholder="e.g. 5.00">
                </div>
                <div class="form-group">
                    <label>Number of Orders</label>
                    <input type="number" id="trade-count" step="1" min="1" max="50" value="3" placeholder="e.g. 5">
                </div>
                <div class="form-group">
                    <label>Trail Stop (pips)</label>
                    <input type="number" id="trade-trail" step="1" min="0" placeholder="default" title="Trail distance in pips from entry price">
                </div>
            </div>
            <div class="form-row" style="margin-top:12px;">
                <div class="form-group">
                    <label>Increase size every N orders</label>
                    <input type="number" id="trade-increase-every" step="1" min="0" value="0" placeholder="0 = disabled" title="Add base volume every N orders (0 = same size for all)">
                </div>
            </div>
            <div class="btn-row">
                <button class="btn btn-primary" id="btn-preview" onclick="previewOrders()">👁 Preview</button>
                <button class="btn btn-success" id="btn-execute" onclick="executeOrders()" disabled>🚀 Execute</button>
            </div>
            <div id="form-feedback" style="margin-top:10px; font-size:0.9em;"></div>
        </div>

        <div class="card" id="preview-card" style="display:none;">
            <h2>👁 Order Preview</h2>
            <div id="preview-table"></div>
        </div>

        <div class="card" id="results-card" style="display:none;">
            <h2>📋 Execution Results</h2>
            <div id="execution-results"></div>
        </div>

        <div class="status-info">
            Orders are placed as GTC (Good Till Cancelled)
        </div>
    </div>

    <script>
        let previewedOrders = null;
        let symbolConfigs = {};

        function loadSymbols() {
            fetch('/trading/symbols', { credentials: 'same-origin' })
                .then(r => { if (r.status === 401) location.reload(); return r.json(); })
                .then(data => {
                    symbolConfigs = data.configs || {};
                    const sel = document.getElementById('trade-symbol');
                    (data.symbols || []).forEach(s => {
                        const opt = document.createElement('option');
                        opt.value = s; opt.textContent = s;
                        sel.appendChild(opt);
                    });
                })
                .catch(err => console.error('Error loading symbols:', err));
        }

        function onOrderTypeChange() {
            const type = document.getElementById('trade-order-type').value;
            const label = document.getElementById('trade-first-price-label');
            label.textContent = type === 'sell_stop' ? 'First Sell Stop Price' : 'First Buy Stop Price';
            previewedOrders = null;
            document.getElementById('btn-execute').disabled = true;
            document.getElementById('preview-card').style.display = 'none';
        }

        function onSymbolChange() {
            const symbol = document.getElementById('trade-symbol').value;
            const info = document.getElementById('current-price-info');
            previewedOrders = null;
            document.getElementById('btn-execute').disabled = true;
            document.getElementById('preview-card').style.display = 'none';
            document.getElementById('results-card').style.display = 'none';

            if (!symbol) { info.style.display = 'none'; return; }

            // Set default trail from symbol config
            const cfg = symbolConfigs[symbol];
            if (cfg) {
                document.getElementById('trade-trail').value = cfg.default_trail;
                document.getElementById('trade-trail').placeholder = cfg.default_trail + ' (default)';
            }

            fetch('/trading/price?symbol=' + symbol, { credentials: 'same-origin' })
                .then(r => r.json())
                .then(data => {
                    document.getElementById('cp-symbol').textContent = symbol;
                    document.getElementById('cp-price').textContent = data.price > 0 ? data.price : 'N/A';
                    info.style.display = 'block';
                })
                .catch(() => { info.style.display = 'none'; });
        }

        function computeTrailSL(entryPrice, trailPips, symbol) {
            const cfg = symbolConfigs[symbol];
            if (!cfg || trailPips <= 0) return 0;
            // trail_distance = trail_pips * trail_multiplier * pip_value
            const trailDistance = trailPips * cfg.trail_multiplier * cfg.pip_value;
            return entryPrice - trailDistance;
        }

        function getOrderVolume(baseVolume, index, increaseEvery) {
            if (increaseEvery <= 0) return baseVolume;
            const bumps = Math.floor(index / increaseEvery);
            return Math.round((baseVolume + bumps * baseVolume) * 100) / 100;
        }

        function previewOrders() {
            const symbol = document.getElementById('trade-symbol').value;
            const orderType = document.getElementById('trade-order-type').value;
            const isBuy = orderType === 'buy_stop';
            const baseVolume = parseFloat(document.getElementById('trade-volume').value);
            const firstPrice = parseFloat(document.getElementById('trade-first-price').value);
            const distance = parseFloat(document.getElementById('trade-distance').value);
            const count = parseInt(document.getElementById('trade-count').value);
            const trailPips = parseInt(document.getElementById('trade-trail').value) || 0;
            const increaseEvery = parseInt(document.getElementById('trade-increase-every').value) || 0;
            const feedback = document.getElementById('form-feedback');

            if (!symbol) { feedback.innerHTML = '<span style="color:red">Select a symbol</span>'; return; }
            if (isNaN(baseVolume) || baseVolume <= 0) { feedback.innerHTML = '<span style="color:red">Enter a valid volume</span>'; return; }
            if (isNaN(firstPrice) || firstPrice <= 0) { feedback.innerHTML = '<span style="color:red">Enter a valid first price</span>'; return; }
            if (isNaN(distance) || distance <= 0) { feedback.innerHTML = '<span style="color:red">Enter a valid distance</span>'; return; }
            if (isNaN(count) || count < 1 || count > 50) { feedback.innerHTML = '<span style="color:red">Enter 1-50 orders</span>'; return; }
            feedback.innerHTML = '';

            const cfg = symbolConfigs[symbol] || {};
            const contractSize = cfg.contract_size || 100000;
            const trailDistance = trailPips > 0 ? trailPips * (cfg.trail_multiplier || 1) * (cfg.pip_value || 0.0001) : 0;
            const hasSL = trailPips > 0;

            const orders = [];
            for (let i = 0; i < count; i++) {
                const price = isBuy ? firstPrice + (i * distance) : firstPrice - (i * distance);
                const vol = getOrderVolume(baseVolume, i, increaseEvery);
                const sl = hasSL ? (isBuy ? price - trailDistance : price + trailDistance) : 0;

                const maxLossPerOrder = hasSL ? trailDistance * vol * contractSize : 0;

                let worstCaseTotal = 0;
                if (hasSL) {
                    const slLevel = isBuy ? price - trailDistance : price + trailDistance;
                    for (let j = 0; j <= i; j++) {
                        const entryJ = isBuy ? firstPrice + (j * distance) : firstPrice - (j * distance);
                        const volJ = getOrderVolume(baseVolume, j, increaseEvery);
                        worstCaseTotal += isBuy
                            ? (slLevel - entryJ) * volJ * contractSize
                            : (entryJ - slLevel) * volJ * contractSize;
                    }
                }

                orders.push({
                    index: i + 1,
                    symbol: symbol,
                    order_type: orderType,
                    type: isBuy ? 'BUY STOP' : 'SELL STOP',
                    price: price,
                    volume: vol,
                    sl: sl,
                    trail_pips: trailPips,
                    maxLossPerOrder: Math.round(maxLossPerOrder * 100) / 100,
                    worstCaseTotal: Math.round(worstCaseTotal * 100) / 100,
                });
            }

            previewedOrders = orders;

            // --- Orders table ---
            let html = '<table><thead><tr>';
            html += '<th>#</th><th>Type</th><th>Price</th>';
            if (hasSL) html += '<th>Stop Loss</th>';
            html += '<th>Volume</th>';
            if (hasSL) html += '<th>Max Loss</th><th>Worst Case Total</th>';
            html += '</tr></thead><tbody>';

            const badgeClass = isBuy ? 'badge-buy' : 'badge-sell';
            orders.forEach(o => {
                const wcClass = o.worstCaseTotal >= 0 ? 'result-success' : 'result-fail';
                html += '<tr>';
                html += '<td data-label="#">' + o.index + '</td>';
                html += '<td data-label="Type"><span class="badge ' + badgeClass + '">' + o.type + '</span></td>';
                html += '<td data-label="Price"><strong>' + o.price.toFixed(5) + '</strong></td>';
                if (hasSL) html += '<td data-label="Stop Loss" style="color:#e74c3c;font-weight:600;">' + o.sl.toFixed(5) + '</td>';
                html += '<td data-label="Volume">' + o.volume + '</td>';
                if (hasSL) {
                    html += '<td data-label="Max Loss" style="color:#e74c3c;">-$' + o.maxLossPerOrder.toFixed(2) + '</td>';
                    html += '<td data-label="Worst Case" class="' + wcClass + '">' + (o.worstCaseTotal >= 0 ? '+$' + o.worstCaseTotal.toFixed(2) : '-$' + Math.abs(o.worstCaseTotal).toFixed(2)) + '</td>';
                }
                html += '</tr>';
            });
            html += '</tbody></table>';

            // --- Risk summary ---
            const totalVolume = orders.reduce((s, o) => s + o.volume, 0);
            const totalVolumeRounded = Math.round(totalVolume * 100) / 100;

            html += '<div style="margin-top:15px; display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:10px;">';

            html += '<div style="padding:12px;background:#e3f2fd;border-radius:8px;text-align:center;">';
            html += '<div style="font-size:0.75em;color:#999;text-transform:uppercase;">Total Volume</div>';
            html += '<div style="font-size:1.3em;font-weight:700;color:#2c3e50;">' + totalVolumeRounded + ' lots</div>';
            html += '</div>';

            if (hasSL) {
                const maxSingleLoss = Math.max(...orders.map(o => o.maxLossPerOrder));
                const worstTotal = orders[orders.length - 1].worstCaseTotal;

                html += '<div style="padding:12px;background:#fce4ec;border-radius:8px;text-align:center;">';
                html += '<div style="font-size:0.75em;color:#999;text-transform:uppercase;">Max Loss (largest order)</div>';
                html += '<div style="font-size:1.3em;font-weight:700;color:#e74c3c;">-$' + maxSingleLoss.toFixed(2) + '</div>';
                html += '</div>';

                html += '<div style="padding:12px;background:' + (worstTotal >= 0 ? '#e8f5e9' : '#fce4ec') + ';border-radius:8px;text-align:center;">';
                html += '<div style="font-size:0.75em;color:#999;text-transform:uppercase;">Worst Case (all SLs hit)</div>';
                html += '<div style="font-size:1.3em;font-weight:700;color:' + (worstTotal >= 0 ? '#27ae60' : '#e74c3c') + ';">' + (worstTotal >= 0 ? '+$' : '-$') + Math.abs(worstTotal).toFixed(2) + '</div>';
                html += '</div>';

                html += '<div style="padding:12px;background:#e3f2fd;border-radius:8px;text-align:center;">';
                html += '<div style="font-size:0.75em;color:#999;text-transform:uppercase;">Trail Stop</div>';
                html += '<div style="font-size:1.3em;font-weight:700;color:#2c3e50;">' + trailPips + ' pips</div>';
                html += '</div>';
            }

            html += '</div>';

            document.getElementById('preview-table').innerHTML = html;
            document.getElementById('preview-card').style.display = 'block';
            document.getElementById('results-card').style.display = 'none';
            document.getElementById('btn-execute').disabled = false;
        }

        function executeOrders() {
            if (!previewedOrders || previewedOrders.length === 0) return;
            const ot = previewedOrders[0] ? (previewedOrders[0].order_type === 'sell_stop' ? 'SELL STOP' : 'BUY STOP') : 'BUY STOP';
            if (!confirm('Place ' + previewedOrders.length + ' ' + ot + ' order(s)?')) return;

            const btn = document.getElementById('btn-execute');
            const btnPreview = document.getElementById('btn-preview');
            btn.disabled = true;
            btn.textContent = '⏳ Executing...';
            btnPreview.disabled = true;

            fetch('/trading/execute', {
                method: 'POST',
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    orders: previewedOrders,
                    order_type: previewedOrders[0] ? previewedOrders[0].order_type : 'buy_stop'
                })
            })
            .then(r => r.json())
            .then(data => {
                btn.textContent = '🚀 Execute';
                btnPreview.disabled = false;

                if (!data.results) {
                    document.getElementById('execution-results').innerHTML =
                        '<div class="exec-summary fail">Error: ' + (data.error || 'Unknown') + '</div>';
                    document.getElementById('results-card').style.display = 'block';
                    return;
                }

                const showSL = data.results.some(r => r.sl > 0);
                let html = '<table><thead><tr>';
                html += '<th>#</th><th>Price</th>' + (showSL ? '<th>SL</th>' : '') + '<th>Result</th><th>Details</th>';
                html += '</tr></thead><tbody>';

                let successCount = 0;
                data.results.forEach(r => {
                    if (r.success) successCount++;
                    const cls = r.success ? 'result-success' : 'result-fail';
                    const icon = r.success ? '✅' : '❌';
                    html += '<tr>';
                    html += '<td data-label="#">' + r.index + '</td>';
                    html += '<td data-label="Price">' + r.price.toFixed(5) + '</td>';
                    if (showSL) html += '<td data-label="SL" style="color:#e74c3c;">' + (r.sl > 0 ? r.sl.toFixed(5) : '-') + '</td>';
                    html += '<td data-label="Result" class="' + cls + '">' + icon + (r.success ? ' OK' : ' Failed') + '</td>';
                    html += '<td data-label="Details">' + r.message + '</td>';
                    html += '</tr>';
                });
                html += '</tbody></table>';

                const total = data.results.length;
                let summaryClass = 'success';
                let summaryText = '✅ All ' + total + ' orders placed successfully';
                if (successCount === 0) {
                    summaryClass = 'fail';
                    summaryText = '❌ All ' + total + ' orders failed';
                } else if (successCount < total) {
                    summaryClass = 'partial';
                    summaryText = '⚠️ ' + successCount + '/' + total + ' orders placed';
                }
                html += '<div class="exec-summary ' + summaryClass + '">' + summaryText + '</div>';

                document.getElementById('execution-results').innerHTML = html;
                document.getElementById('results-card').style.display = 'block';
                previewedOrders = null;
            })
            .catch(err => {
                btn.textContent = '🚀 Execute';
                btnPreview.disabled = false;
                document.getElementById('execution-results').innerHTML =
                    '<div class="exec-summary fail">Network error: ' + err + '</div>';
                document.getElementById('results-card').style.display = 'block';
            });
        }

        loadSymbols();
    </script>
</body>
</html>
"""

# Future Risk HTML page template
FUTURE_RISK_HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Future Risk - MT5 Dashboard</title>
    <link rel="icon" type="image/png" href="https://cdn-icons-png.flaticon.com/512/4002/4002224.png">
    <style>
        :root {
            --primary-color: #2c3e50;
            --profit-color: #27ae60;
            --loss-color: #e74c3c;
            --warning-color: #f39c12;
            --info-color: #3498db;
            --pending-color: #8e44ad;
            --light-bg: #f8f9fa;
            --card-shadow: 0 4px 6px rgba(0,0,0,0.1);
        }
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            margin: 0; padding: 20px;
            background-color: #f0f2f5; color: #333;
        }
        .container { max-width: 1200px; margin: 0 auto; }

        .header {
            background: linear-gradient(135deg, var(--primary-color), #1a2530);
            color: white; padding: 25px; border-radius: 12px;
            margin-bottom: 25px; text-align: center;
            box-shadow: var(--card-shadow);
        }
        .header h1 { margin: 0; font-size: 2em; }
        .header p { margin: 8px 0 0 0; opacity: 0.85; }
        .nav-link {
            color: white; text-decoration: none;
            background: rgba(255,255,255,0.15);
            padding: 6px 16px; border-radius: 20px;
            font-size: 0.85em; display: inline-block; margin-top: 10px;
        }
        .nav-link:hover { background: rgba(255,255,255,0.3); }

        .card {
            background: white; border-radius: 12px;
            box-shadow: var(--card-shadow);
            padding: 25px; margin-bottom: 25px;
        }
        .card h2 { margin-top: 0; color: var(--primary-color); font-size: 1.3em; }

        /* Symbol picker */
        .picker-row {
            display: flex; gap: 12px; align-items: flex-end; flex-wrap: wrap;
        }
        .picker-group { flex: 1; min-width: 150px; }
        .picker-group label {
            display: block; font-size: 0.85em; color: #666;
            margin-bottom: 5px; font-weight: 600;
            text-transform: uppercase; letter-spacing: 0.5px;
        }
        .picker-group select, .picker-group input {
            width: 100%; padding: 10px 12px;
            border: 1px solid #ddd; border-radius: 8px;
            font-size: 1em; box-sizing: border-box;
        }
        .picker-group select:focus, .picker-group input:focus {
            outline: none; border-color: var(--info-color);
        }
        .btn {
            padding: 10px 24px; border: none; border-radius: 8px;
            cursor: pointer; font-size: 1em; font-weight: 600;
            transition: background 0.2s;
        }
        .btn-primary { background: var(--info-color); color: white; }
        .btn-primary:hover { background: #2980b9; }
        .btn-secondary { background: #95a5a6; color: white; }
        .btn-secondary:hover { background: #7f8c8d; }
        .btn-danger { background: #e74c3c; color: white; }
        .btn-danger:hover { background: #c0392b; }
        .btn-small { padding: 5px 10px; font-size: 0.8em; }

        /* Summary cards */
        .summary-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
            gap: 12px; margin-bottom: 20px;
        }
        .summary-card {
            text-align: center; padding: 15px 10px;
            background: var(--light-bg); border-radius: 10px;
        }
        .summary-card.risk { border-left: 4px solid var(--loss-color); }
        .summary-card.pending-risk { border-left: 4px solid var(--pending-color); }
        .summary-card.total-risk { border-left: 4px solid var(--warning-color); }
        .summary-card.count { border-left: 4px solid var(--info-color); }
        .summary-card .value {
            font-size: 1.5em; font-weight: 700; color: var(--primary-color);
        }
        .summary-card .label {
            font-size: 0.7em; color: #7f8c8d; text-transform: uppercase;
            margin-top: 4px; letter-spacing: 0.5px;
        }
        .value.negative { color: var(--loss-color); }
        .value.positive { color: var(--profit-color); }
        .value.pending-color { color: var(--pending-color); }
        .value.warning { color: var(--warning-color); }

        /* Tables */
        table { width: 100%; border-collapse: collapse; }
        thead { background: var(--primary-color); }
        thead.pending-head { background: var(--pending-color); }
        th {
            color: white; padding: 14px 12px; text-align: left;
            font-weight: 600; text-transform: uppercase;
            letter-spacing: 0.5px; font-size: 0.85em;
        }
        tbody tr { border-bottom: 1px solid #eee; transition: background 0.2s; }
        tbody tr:hover { background-color: #f8f9fa; }
        td { padding: 12px; }

        .badge {
            padding: 4px 10px; border-radius: 12px;
            font-size: 0.8em; font-weight: 600;
            display: inline-block; text-transform: uppercase;
        }
        .badge-buy { background: #e8f5e9; color: var(--profit-color); border: 1px solid var(--profit-color); }
        .badge-sell { background: #fce4ec; color: var(--loss-color); border: 1px solid var(--loss-color); }
        .badge-pending { background: #f3e5f5; color: var(--pending-color); border: 1px solid var(--pending-color); }
        .badge-open { background: var(--profit-color); color: white; }

        .profit-positive { color: var(--profit-color); }
        .profit-negative { color: var(--loss-color); }

        .section-title {
            display: flex; align-items: center; gap: 10px;
            margin: 25px 0 15px 0; font-size: 1.1em; color: var(--primary-color);
        }
        .section-count {
            background: var(--light-bg); padding: 2px 10px;
            border-radius: 12px; font-size: 0.8em; color: #666;
        }

        .no-data {
            text-align: center; padding: 30px; color: #999; font-style: italic;
        }

        .price-tag {
            font-weight: 600; color: var(--primary-color);
            font-size: 1.1em;
        }

        .status-info {
            font-size: 0.8em; color: #999; text-align: center; margin-top: 10px;
        }

        @media (max-width: 768px) {
            body { padding: 8px; }
            .header { padding: 15px; }
            .header h1 { font-size: 1.3em; }
            .card { padding: 15px; }
            .picker-row { flex-direction: column; gap: 10px; }
            .picker-group { min-width: unset; }
            .summary-grid { grid-template-columns: repeat(2, 1fr); gap: 8px; }
            .summary-card .value { font-size: 1.1em; }

            thead { display: none; }
            tbody { display: flex; flex-direction: column; gap: 8px; }
            tbody tr {
                display: grid; grid-template-columns: 1fr 1fr;
                gap: 4px 12px; border: 1px solid #e8e8e8;
                border-radius: 8px; padding: 10px 12px; border-bottom: none;
            }
            td { padding: 3px 0; font-size: 0.85em; }
            td::before {
                content: attr(data-label); display: block;
                font-size: 0.65em; color: #7f8c8d; text-transform: uppercase; font-weight: 600;
            }
            td:first-child {
                grid-column: 1 / -1;
                border-bottom: 1px solid #eee; padding-bottom: 6px;
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🔮 Future Risk Analysis</h1>
            <p>View open positions and pending orders that could add to your exposure</p>
            <a href="/" class="nav-link">← Back to Dashboard</a>
            <a href="/alert" class="nav-link" style="margin-left: 8px;">🔔 Alerts</a>
        </div>

        <div class="card">
            <h2>🔍 Select Symbol</h2>
            <div class="picker-row">
                <div class="picker-group">
                    <label>Symbol</label>
                    <select id="symbol-select">
                        <option value="">All Symbols</option>
                    </select>
                </div>
                <div class="picker-group" style="flex: 0;">
                    <label>&nbsp;</label>
                    <button class="btn btn-primary" onclick="loadData()">Analyze</button>
                </div>
                <div class="picker-group" style="flex: 0;">
                    <label>&nbsp;</label>
                    <button class="btn btn-secondary" onclick="document.getElementById('symbol-select').value=''; loadData();">Show All</button>
                </div>
            </div>
        </div>

        <div id="content-area">
            <div class="no-data">Select a symbol and click Analyze, or Show All to see everything.</div>
        </div>

        <div class="status-info">
            Last refreshed: <span id="last-refresh">-</span>
        </div>
    </div>

    <script>
        function formatNumber(num) {
            if (num === null || num === undefined) return '0.00';
            return num.toFixed(2).replace(/\\B(?=(\\d{3})+(?!\\d))/g, ",");
        }
        function formatPrice(num) {
            if (!num) return '-';
            if (num >= 100) return num.toFixed(2);
            if (num >= 1) return num.toFixed(4);
            return num.toFixed(5);
        }

        function cancelOrder(ticket) {
            if (!confirm('Cancel order #' + ticket + '?')) return;
            fetch('/future_risk/cancel_order', {
                method: 'POST',
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ order: ticket })
            })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    loadData();
                } else {
                    alert(data.message || 'Failed to cancel order');
                }
            })
            .catch(err => { alert('Error: ' + err); });
        }

        function closeAllPending(symbol) {
            if (!confirm('Cancel all pending orders for ' + symbol + '?')) return;
            fetch('/future_risk/close_pending', {
                method: 'POST',
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ symbol: symbol })
            })
            .then(r => r.json())
            .then(data => {
                if (data.cancelled > 0 || data.failed > 0) {
                    alert(data.cancelled + ' cancelled, ' + data.failed + ' failed. ' + (data.messages || []).join(' | '));
                    loadData();
                }
            })
            .catch(err => { alert('Error: ' + err); });
        }

        function loadData() {
            const symbol = document.getElementById('symbol-select').value;
            const url = symbol ? '/future_risk/data?symbol=' + encodeURIComponent(symbol) : '/future_risk/data';
            
            fetch(url, { credentials: 'same-origin' })
                .then(r => {
                    if (r.status === 401) { location.reload(); return; }
                    return r.json();
                })
                .then(data => {
                    if (data) renderData(data);
                    document.getElementById('last-refresh').textContent = new Date().toLocaleTimeString();
                })
                .catch(err => console.error('Error:', err));
        }

        function populateSymbols(symbols) {
            const sel = document.getElementById('symbol-select');
            const current = sel.value;
            // Keep "All" option, add symbols
            sel.innerHTML = '<option value="">All Symbols</option>';
            symbols.forEach(s => {
                sel.innerHTML += '<option value="' + s + '"' + (s === current ? ' selected' : '') + '>' + s + '</option>';
            });
        }

        function renderData(data) {
            // Populate symbol dropdown with all known symbols
            const allSymbols = [...new Set([
                ...(data.supported_symbols || []),
                ...(data.available_symbols || [])
            ])].sort();
            populateSymbols(allSymbols);

            const s = data.summary;
            const filterLabel = data.symbol_filter ? data.symbol_filter : 'All Symbols';
            const priceHtml = data.current_price > 0
                ? '<span class="price-tag">' + formatPrice(data.current_price) + '</span>'
                : '';

            let html = '';

            // Summary
            html += '<div class="card">';
            html += '<h2>📊 Risk Summary — ' + filterLabel + ' ' + priceHtml + '</h2>';
            html += '<div class="summary-grid">';

            html += '<div class="summary-card count"><div class="value">' + s.open_count + '</div><div class="label">Open Positions</div></div>';
            html += '<div class="summary-card count"><div class="value">' + s.pending_count + '</div><div class="label">Pending Orders</div></div>';
            html += '<div class="summary-card"><div class="value">' + s.total_volume + '</div><div class="label">Total Volume</div></div>';
            
            const riskClass = s.current_risk > 0 ? 'negative' : (s.current_risk < 0 ? 'positive' : '');
            html += '<div class="summary-card risk"><div class="value ' + riskClass + '">$' + formatNumber(s.current_risk) + '</div><div class="label">Current Risk</div></div>';
            
            const pendingClass = s.pending_risk > 0 ? 'pending-color' : (s.pending_risk < 0 ? 'positive' : '');
            html += '<div class="summary-card pending-risk"><div class="value ' + pendingClass + '">$' + formatNumber(s.pending_risk) + '</div><div class="label">Pending Risk</div></div>';
            
            const totalClass = s.total_future_risk > 0 ? 'warning' : (s.total_future_risk < 0 ? 'positive' : '');
            html += '<div class="summary-card total-risk"><div class="value ' + totalClass + '">$' + formatNumber(s.total_future_risk) + '</div><div class="label">Total Future Risk</div></div>';

            if (data.symbol_filter && s.pending_count > 0) {
                html += '<div style="grid-column:1/-1; display:flex; justify-content:flex-end; align-items:center; margin-top:10px;">';
                html += '<button class="btn btn-danger" data-symbol="' + data.symbol_filter + '" onclick="closeAllPending(this.getAttribute(&quot;data-symbol&quot;))">🗑️ Close All Pending (' + data.symbol_filter + ')</button>';
                html += '</div>';
            }
            
            html += '</div></div>';

            // Open trades table
            html += '<div class="section-title">📈 Open Positions <span class="section-count">' + s.open_count + '</span></div>';
            if (data.open_trades.length > 0) {
                html += '<div class="card" style="padding:0; overflow:hidden;">';
                html += '<table><thead><tr>';
                html += '<th>Ticket</th><th>Symbol</th><th>Type</th><th>Volume</th><th>Open</th><th>SL</th><th>TP</th><th>Profit</th><th>Risk</th>';
                html += '</tr></thead><tbody>';
                data.open_trades.forEach(t => {
                    const typeClass = t.type === 'BUY' ? 'badge-buy' : 'badge-sell';
                    html += '<tr>';
                    html += '<td data-label="Ticket">' + t.ticket + '</td>';
                    html += '<td data-label="Symbol">' + t.symbol + '</td>';
                    html += '<td data-label="Type"><span class="badge ' + typeClass + '">' + t.type + '</span></td>';
                    html += '<td data-label="Volume">' + t.volume + '</td>';
                    html += '<td data-label="Open">' + formatPrice(t.price_open) + '</td>';
                    html += '<td data-label="SL">' + (t.sl ? formatPrice(t.sl) : '<span style="color:#ccc">None</span>') + '</td>';
                    html += '<td data-label="TP">' + (t.tp ? formatPrice(t.tp) : '<span style="color:#ccc">None</span>') + '</td>';
                    html += '<td data-label="Profit" class="' + (t.profit >= 0 ? 'profit-positive' : 'profit-negative') + '">$' + formatNumber(t.profit) + '</td>';
                    html += '<td data-label="Risk" class="' + (t.total_risk > 0 ? 'profit-negative' : 'profit-positive') + '">$' + formatNumber(t.total_risk) + '</td>';
                    html += '</tr>';
                });
                html += '</tbody></table></div>';
            } else {
                html += '<div class="card"><div class="no-data">No open positions' + (data.symbol_filter ? ' for ' + data.symbol_filter : '') + '</div></div>';
            }

            // Pending orders table
            html += '<div class="section-title">⏳ Pending Orders <span class="section-count">' + s.pending_count + '</span></div>';
            if (data.pending_orders.length > 0) {
                html += '<div class="card" style="padding:0; overflow:hidden;">';
                html += '<table><thead class="pending-head"><tr>';
                html += '<th>Ticket</th><th>Symbol</th><th>Type</th><th>Volume</th><th>Trigger Price</th><th>SL</th><th>TP</th><th>Potential Risk</th><th>Actions</th>';
                html += '</tr></thead><tbody>';
                data.pending_orders.forEach(o => {
                    html += '<tr>';
                    html += '<td data-label="Ticket">' + o.ticket + '</td>';
                    html += '<td data-label="Symbol">' + o.symbol + '</td>';
                    html += '<td data-label="Type"><span class="badge badge-pending">' + o.type + '</span></td>';
                    html += '<td data-label="Volume">' + o.volume + '</td>';
                    html += '<td data-label="Trigger">' + formatPrice(o.price_open) + '</td>';
                    html += '<td data-label="SL">' + (o.sl ? formatPrice(o.sl) : '<span style="color:#ccc">None</span>') + '</td>';
                    html += '<td data-label="TP">' + (o.tp ? formatPrice(o.tp) : '<span style="color:#ccc">None</span>') + '</td>';
                    html += '<td data-label="Risk" class="' + (o.total_risk > 0 ? 'profit-negative' : 'profit-positive') + '">$' + formatNumber(o.total_risk) + '</td>';
                    html += '<td data-label="Actions"><button class="btn btn-danger btn-small" data-order="' + o.ticket + '" onclick="cancelOrder(this.getAttribute(&quot;data-order&quot;))">Delete</button></td>';
                    html += '</tr>';
                });
                html += '</tbody></table></div>';
            } else {
                html += '<div class="card"><div class="no-data">No pending orders' + (data.symbol_filter ? ' for ' + data.symbol_filter : '') + '</div></div>';
            }

            document.getElementById('content-area').innerHTML = html;
        }

        // Load all data on page load
        loadData();
        // Auto-refresh every 15 seconds
        setInterval(loadData, 15000);
    </script>
</body>
</html>
"""


# HTTP request handler
class MT5Handler(BaseHTTPRequestHandler):
    def send_auth_required(self):
        """Send 401 Unauthorized response"""
        self.send_response(401)
        self.send_header('WWW-Authenticate', 'Basic realm="MT5 Dashboard"')
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        self.wfile.write(b'<h1>401 - Unauthorized</h1><p>Please provide valid credentials.</p>')
    
    def do_GET(self):
        # Check authentication
        auth_header = self.headers.get('Authorization')
        if not check_auth(auth_header):
            self.send_auth_required()
            return
        
        if self.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(HTML_TEMPLATE.encode())

        elif self.path == '/health':
            # Health/monitoring endpoint - no auth required for health checks
            mem_mb = get_memory_usage_mb()
            gc_stats = gc.get_stats()
            health_data = {
                'status': 'ok',
                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'memory_mb': round(mem_mb, 1),
                'gc_collections': [s['collections'] for s in gc_stats],
                'gc_collected': [s['collected'] for s in gc_stats],
                'active_threads': threading.active_count(),
                'python_info': {
                    'pid': os.getpid(),
                }
            }
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(health_data, indent=2).encode())

        elif self.path == '/data':
            t_request = time.time()
            
            # Get current trades data with risk metrics (grouped by symbol)
            trades_by_symbol, risk_metrics_by_symbol, total_metrics = get_all_trades()

            # Flatten all trades for total count
            all_trades = []
            for symbol_trades in trades_by_symbol.values():
                all_trades.extend(symbol_trades)

            # Calculate basic statistics
            total_trades = len(all_trades)
            total_buy = len([t for t in all_trades if t['type'] == 'BUY'])
            total_sell = len([t for t in all_trades if t['type'] == 'SELL'])

            # Get per-symbol autotrail settings for UI
            symbol_autotrail_settings = get_symbol_autotrail_settings()

            # Prepare response data
            response_data = {
                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'total_trades': total_trades,
                'total_buy': total_buy,
                'total_sell': total_sell,
                'total_metrics': total_metrics,
                'trades_by_symbol': trades_by_symbol,
                'risk_metrics_by_symbol': risk_metrics_by_symbol,
                'symbols': list(trades_by_symbol.keys()),
                'symbol_autotrail_settings': symbol_autotrail_settings
            }

            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(response_data).encode())
            
            log_performance("/data endpoint", t_request, f"{total_trades} trades")

        elif self.path == '/trades/history':
            # Get all trades from database (including closed)
            trades = get_all_trades_from_db()
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({'trades': trades}, indent=2).encode())

        elif self.path == '/alert':
            # Serve the alerts HTML page
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(ALERTS_HTML_TEMPLATE.encode())

        elif self.path == '/alerts/data':
            # Get all alerts + current prices/potential losses
            alerts = get_all_alerts()
            price_cache = {}
            loss_cache = None  # Lazy-loaded only if needed

            for alert in alerts:
                sym = alert['symbol']
                alert_type = alert.get('alert_type', 'price')

                if alert_type == 'potential_loss':
                    # Attach current potential loss
                    if loss_cache is None:
                        loss_cache = get_all_potential_losses()
                    alert['current_value'] = round(loss_cache.get(sym, 0), 2)
                    alert['current_price'] = 0  # Not relevant for loss alerts
                else:
                    # Attach current price
                    if sym not in price_cache:
                        price_cache[sym] = get_symbol_price(sym)
                    alert['current_price'] = round(price_cache[sym], 5)
                    alert['current_value'] = 0
            
            # Also return the list of supported symbols for the dropdown
            supported = list(SUPPORTED_SYMBOLS.keys()) + list(CRYPTO_SYMBOLS.keys())
            
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({
                'alerts': alerts,
                'supported_symbols': supported
            }).encode())

        elif self.path == '/trading':
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(TRADING_HTML_TEMPLATE.encode())

        elif self.path == '/trading/symbols':
            symbols_data = {}
            for sym in sorted(SUPPORTED_SYMBOLS.keys()):
                cfg = SUPPORTED_SYMBOLS[sym]
                symbols_data[sym] = {
                    'default_trail': cfg.get('default_trail', DEFAULT_TRAIL_PIPS),
                    'pip_value': cfg['pip_value'],
                    'trail_multiplier': cfg.get('trail_multiplier', 1),
                    'contract_size': cfg['contract_size'],
                    'description': cfg.get('description', sym),
                }
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({'symbols': list(symbols_data.keys()), 'configs': symbols_data}).encode())

        elif self.path.startswith('/trading/price'):
            symbol = None
            if '?' in self.path:
                query = self.path.split('?', 1)[1]
                params = urllib.parse.parse_qs(query)
                symbol = params.get('symbol', [None])[0]
            price = get_symbol_price(symbol) if symbol else 0
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({'symbol': symbol, 'price': round(price, 5)}).encode())

        elif self.path == '/future_risk':
            # Serve the future risk HTML page
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(FUTURE_RISK_HTML_TEMPLATE.encode())

        elif self.path.startswith('/future_risk/data'):
            # Parse optional ?symbol=XXX query param
            symbol_filter = None
            if '?' in self.path:
                query = self.path.split('?', 1)[1]
                params = urllib.parse.parse_qs(query)
                symbol_filter = params.get('symbol', [None])[0]
                if symbol_filter:
                    symbol_filter = symbol_filter.upper()
            
            data = get_future_risk_data(symbol_filter)
            
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())

        else:
            self.send_response(404)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(b'<h1>404 - Page Not Found</h1>')
    
    def do_POST(self):
        # Check authentication
        auth_header = self.headers.get('Authorization')
        if not check_auth(auth_header):
            self.send_auth_required()
            return
        
        content_length = int(self.headers.get('Content-Length', 0))
        post_data = self.rfile.read(content_length)
        
        try:
            data = json.loads(post_data.decode('utf-8'))
        except json.JSONDecodeError:
            self.send_response(400)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': 'Invalid JSON'}).encode())
            return
        
        if self.path == '/autotrail':
            # Update autotrail status for a trade (optionally with trail_pips)
            ticket = data.get('ticket')
            autotrail = data.get('autotrail', False)
            trail_pips = data.get('trail_pips')  # Optional
            
            if ticket is None:
                self.send_response(400)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': 'Missing ticket'}).encode())
                return
            
            # Validate and clamp trail_pips if provided (range 5-200)
            if trail_pips is not None:
                trail_pips = max(2, min(200, int(trail_pips)))
            
            update_trade_autotrail(ticket, autotrail, trail_pips)
            
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            response = {
                'success': True,
                'ticket': ticket,
                'autotrail': autotrail
            }
            if trail_pips is not None:
                response['trail_pips'] = trail_pips
            self.wfile.write(json.dumps(response).encode())
        
        elif self.path == '/trail_pips':
            # Update trail_pips for a trade
            ticket = data.get('ticket')
            trail_pips = data.get('trail_pips')
            
            if ticket is None or trail_pips is None:
                self.send_response(400)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': 'Missing ticket or trail_pips'}).encode())
                return
            
            # Validate trail_pips range (5-200)
            trail_pips = max(2, min(200, int(trail_pips)))
            
            update_trade_trail_pips(ticket, trail_pips)
            
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({
                'success': True,
                'ticket': ticket,
                'trail_pips': trail_pips
            }).encode())
        
        elif self.path == '/status':
            # Update status for a trade
            ticket = data.get('ticket')
            status = data.get('status')
            
            if ticket is None or status not in ['open', 'close', 'waiting']:
                self.send_response(400)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': 'Invalid ticket or status'}).encode())
                return
            
            update_trade_status(ticket, status)
            
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({
                'success': True,
                'ticket': ticket,
                'status': status
            }).encode())
        
        elif self.path == '/symbol_autotrail':
            # Toggle autotrail for new orders on a specific symbol
            symbol = data.get('symbol')
            enabled = data.get('enabled', False)
            
            if not symbol:
                self.send_response(400)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': 'Missing symbol'}).encode())
                return
            
            set_symbol_autotrail_setting(symbol, enabled)
            
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({
                'success': True,
                'symbol': symbol,
                'autotrail_new_orders': enabled
            }).encode())
        
        elif self.path == '/alerts/create':
            symbol = data.get('symbol', '').strip().upper()
            condition = data.get('condition', '')
            price = data.get('price')
            alert_type = data.get('alert_type', 'price')
            
            if alert_type not in ('price', 'potential_loss'):
                alert_type = 'price'
            
            if not symbol or condition not in ('above', 'below') or price is None:
                self.send_response(400)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': 'Missing or invalid symbol, condition, or price'}).encode())
                return
            
            try:
                price = float(price)
            except (ValueError, TypeError):
                self.send_response(400)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': 'Value must be a number'}).encode())
                return
            
            alert_id = create_alert(symbol, condition, price, alert_type)
            
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({
                'success': True,
                'id': alert_id,
                'symbol': symbol,
                'condition': condition,
                'price': price
            }).encode())
        
        elif self.path == '/alerts/delete':
            alert_id = data.get('id')
            if alert_id is None:
                self.send_response(400)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': 'Missing alert id'}).encode())
                return
            
            deleted = delete_alert(int(alert_id))
            
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({'success': deleted, 'id': alert_id}).encode())
        
        elif self.path == '/alerts/toggle':
            alert_id = data.get('id')
            active = data.get('active', True)
            if alert_id is None:
                self.send_response(400)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': 'Missing alert id'}).encode())
                return
            
            toggle_alert(int(alert_id), active)
            
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({'success': True, 'id': alert_id, 'active': active}).encode())
        
        elif self.path == '/trading/execute':
            orders = data.get('orders', [])
            if not orders:
                self.send_response(400)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': 'No orders provided'}).encode())
                return

            order_type = data.get('order_type', 'buy_stop')
            place_fn = place_sell_stop_order if order_type == 'sell_stop' else place_buy_stop_order

            results = []
            for order in orders:
                symbol = order.get('symbol', '')
                price = float(order.get('price', 0))
                volume = float(order.get('volume', 0))
                index = order.get('index', 0)
                sl = float(order.get('sl', 0))

                success, message = place_fn(symbol, price, volume, sl=sl)
                results.append({
                    'index': index,
                    'symbol': symbol,
                    'price': price,
                    'volume': volume,
                    'sl': sl,
                    'success': success,
                    'message': message,
                })
                if not success:
                    send_pushover_notification(
                        f"❌ Buy Stop Failed: {symbol}",
                        f"Order #{index} @ {price} vol={volume}: {message}",
                        priority=1
                    )

            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({'success': True, 'results': results}).encode())

        elif self.path == '/future_risk/cancel_order':
            order_ticket = data.get('order')
            if order_ticket is None:
                self.send_response(400)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': 'Order ticket required'}).encode())
                return
            success, message = cancel_pending_order(order_ticket)
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({'success': success, 'message': message}).encode())

        elif self.path == '/future_risk/close_pending':
            symbol = (data.get('symbol') or '').strip().upper()
            if not symbol:
                self.send_response(400)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': 'Symbol required'}).encode())
                return
            success_count, failed_count, messages = cancel_all_pending_orders(symbol)
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({
                'success': True,
                'symbol': symbol,
                'cancelled': success_count,
                'failed': failed_count,
                'messages': messages,
            }).encode())

        elif self.path == '/alerts/clear_triggered':
            count = delete_triggered_alerts()
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({'success': True, 'deleted': count}).encode())
        
        else:
            self.send_response(404)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': 'Not found'}).encode())


# Background thread to keep MT5 connection alive
def mt5_heartbeat():
    while True:
        try:
            if not mt5.terminal_info():
                print("MT5 connection lost, attempting to reconnect...")
                initialize_mt5()
        except:
            print("MT5 heartbeat check failed")
        time.sleep(60)  # Check every minute


def main():
    # Initialize database
    init_database()
    
    # Initialize MT5
    if not initialize_mt5():
        print("Failed to initialize MT5. Please check your MT5 installation and credentials.")
        return

    # Start MT5 heartbeat thread
    heartbeat_thread = threading.Thread(target=mt5_heartbeat, daemon=True)
    heartbeat_thread.start()
    
    # Start autotrail thread
    autotrail_thread = threading.Thread(target=autotrail_loop, daemon=True)
    autotrail_thread.start()

    # Start price alert checker thread
    alert_thread = threading.Thread(target=alert_check_loop, daemon=True)
    alert_thread.start()

    # Start HTTPS server (threaded to handle concurrent requests without blocking)
    server_address = ('0.0.0.0', 8443)
    httpd = ThreadingHTTPServer(server_address, MT5Handler)
    
    # SSL Configuration - REQUIRED, server will NOT start without valid certificates
    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    cert_file = 'oronvision.crabdance.com-crt.pem'
    key_file = 'oronvision.crabdance.com-key.pem'
    try:
        ssl_context.load_cert_chain(certfile=cert_file, keyfile=key_file)
        httpd.socket = ssl_context.wrap_socket(httpd.socket, server_side=True)
        print(f"🔒 HTTPS Server started on https://localhost:8443")
        print("📊 Open your browser and navigate to: https://localhost:8443")
    except FileNotFoundError:
        print("❌ FATAL: SSL certificates not found!")
        print(f"   Missing: {cert_file} and/or {key_file}")
        print("")
        print("   This server requires HTTPS to protect your credentials.")
        print("   HTTP fallback is disabled for security reasons.")
        print("")
        print("   To generate self-signed certificates, run:")
        print(f'   openssl req -x509 -newkey rsa:4096 -keyout {key_file} -out {cert_file} -days 365 -nodes -subj "/CN=localhost"')
        print("")
        print("🛑 Server NOT started. Fix the certificates and try again.")
        httpd.server_close()
        mt5.shutdown()
        return
    
    print("🔐 Login required - ")
    print("💰 Risk management dashboard now includes:")
    print("   • Total exposure in USD")
    print("   • Potential loss based on stop losses")
    print("   • Risk/reward ratio")
    print("   • Individual position risk")
    print("   • Net volume exposure")
    print("🎯 Autotrail: per-symbol trailing stop (XAUUSD: 20, EURUSD: 75, others: 50)")
    print("🔄 Auto-refresh every 10 seconds")
    print(f"🧠 Memory at startup: {get_memory_usage_mb():.1f}MB | PID: {os.getpid()}")
    print("📡 /health endpoint available for monitoring (memory, threads, GC)")
    print("📝 Performance logs enabled - watch for [PERF] entries")
    print("🛑 Press Ctrl+C to stop the server")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Server shutting down...")
        mt5.shutdown()
        httpd.server_close()
        print("✅ Server stopped")


if __name__ == "__main__":
    # Check if MT5 is installed
    try:
        import MetaTrader5

        main()
    except ImportError:
        print("❌ MetaTrader5 module not installed!")
        print("Please install it using: pip install MetaTrader5")
        print("\nAlso ensure that:")
        print("1. MetaTrader 5 is installed on your system")
        print("2. You have an account with a broker")
        print("3. MT5 terminal is running")
        print("4. You're logged into your account")