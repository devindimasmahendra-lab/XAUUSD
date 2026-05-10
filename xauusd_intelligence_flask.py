# -*- coding: utf-8 -*-
# XAUUSD Realtime Simple Flask - single file
# Login: admin / admin123
# Run: pip install flask requests werkzeug && python xauusd_realtime_simple_pro.py

import os, csv, io, math, sqlite3
from datetime import datetime, timedelta, timezone
from functools import wraps

try:
    import requests
except Exception:
    requests = None

from flask import Flask, request, jsonify, render_template_string, redirect, session, Response
from werkzeug.security import generate_password_hash, check_password_hash


APP = "XAUUSD Realtime"
DB = "xauusd_realtime_simple_pro.db"
TZ = timezone(timedelta(hours=7))

app = Flask(__name__)
app.secret_key = os.getenv("XAUUSD_SECRET_KEY", "change-me")


# ============================================================
# DATABASE
# ============================================================

def now():
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def today():
    return datetime.now(TZ).strftime("%Y-%m-%d")


def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            password_hash TEXT,
            created_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS journal(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT,
            trade_date TEXT,
            symbol TEXT,
            timeframe TEXT,
            side TEXT,
            entry REAL,
            sl REAL,
            tp REAL,
            lot REAL,
            result TEXT,
            pnl REAL,
            balance_after REAL,
            note TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS active_trades(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT,
            symbol TEXT,
            timeframe TEXT,
            side TEXT,
            entry REAL,
            sl REAL,
            tp REAL,
            lot REAL,
            note TEXT,
            status TEXT DEFAULT 'OPEN'
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS ai_memory(
            timeframe TEXT,
            side TEXT,
            total INTEGER DEFAULT 0,
            wins INTEGER DEFAULT 0,
            losses INTEGER DEFAULT 0,
            pnl_sum REAL DEFAULT 0,
            last_updated TEXT,
            PRIMARY KEY(timeframe, side)
        )
    """)

    if cur.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"] == 0:
        cur.execute(
            "INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)",
            ("admin", generate_password_hash("admin123"), now())
        )

    defaults = {
        "symbol": "XAU/USD",
        "default_tf": "15min",
        "twelvedata_api_key": os.getenv("TWELVEDATA_API_KEY", ""),
        "initial_balance": "1000",
        "risk_percent": "1",
        "pip_value_per_lot": "1",
        "refresh_seconds": "10",
        "theme": "dark",
        "currency": "USD",
        "exchange_rate_usd_idr": "15500",
        "news_api_key": os.getenv("NEWS_API_KEY", ""),
        "news_url": "",
        "news_enabled": "1",
    }

    for k, v in defaults.items():
        cur.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))

    # ===== MIGRATIONS =====
    # Add new columns to active_trades if not exist
    existing_cols = [row[1] for row in cur.execute("PRAGMA table_info(active_trades)").fetchall()]
    migration_cols = {
        "close_price": "REAL",
        "close_price_type": "TEXT DEFAULT 'TP'",
        "closed_at": "TEXT",
        "pnl_closed": "REAL DEFAULT 0",
    }
    for col_name, col_type in migration_cols.items():
        if col_name not in existing_cols:
            try:
                cur.execute(f"ALTER TABLE active_trades ADD COLUMN {col_name} {col_type}")
            except Exception:
                pass

    conn.commit()
    conn.close()


def get_setting(key, default=""):
    conn = db()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key, value):
    conn = db()
    conn.execute(
        "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
        (key, str(value))
    )
    conn.commit()
    conn.close()


# ============================================================
# AUTH
# ============================================================

def login_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not session.get("uid"):
            return redirect("/login")
        return func(*args, **kwargs)
    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    err = ""

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        conn = db()
        user = conn.execute(
            "SELECT * FROM users WHERE username=?",
            (username,)
        ).fetchone()
        conn.close()

        if user and check_password_hash(user["password_hash"], password):
            session["uid"] = user["id"]
            session["username"] = username
            return redirect("/")

        err = "Username/password salah"

    return render_template_string(LOGIN_HTML, err=err, app=APP)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/change-password", methods=["POST"])
@login_required
def change_password():
    old = request.form.get("old_password", "")
    new = request.form.get("new_password", "")

    if len(new) < 6:
        return jsonify(ok=False, error="Password minimal 6 karakter"), 400

    conn = db()
    user = conn.execute(
        "SELECT * FROM users WHERE id=?",
        (session["uid"],)
    ).fetchone()

    if not user or not check_password_hash(user["password_hash"], old):
        conn.close()
        return jsonify(ok=False, error="Password lama salah"), 400

    conn.execute(
        "UPDATE users SET password_hash=? WHERE id=?",
        (generate_password_hash(new), session["uid"])
    )
    conn.commit()
    conn.close()

    return jsonify(ok=True)


# ============================================================
# MARKET DATA
# ============================================================

def safe_float(x, default=0.0):
    try:
        if x is None or x == "":
            return default
        return float(x)
    except Exception:
        return default


def tf_ok(tf):
    allowed = [
        "1min", "3min", "5min", "15min", "30min",
        "45min", "1h", "2h", "4h", "1day"
    ]
    return tf if tf in allowed else "15min"


def tf_minutes(tf):
    return {
        "1min": 1,
        "3min": 3,
        "5min": 5,
        "15min": 15,
        "30min": 30,
        "45min": 45,
        "1h": 60,
        "2h": 120,
        "4h": 240,
        "1day": 1440,
    }.get(tf, 15)


def demo_candles(n=220, tf="15min"):
    """
    Data demo kalau API key belum diisi.
    """
    out = []
    price = 2350.0
    step = tf_minutes(tf)
    start = datetime.now(TZ) - timedelta(minutes=n * step)

    for i in range(n):
        o = price
        c = o + math.sin(i / 9) * 1.2 + math.sin(i * 1.7) * 0.65
        h = max(o, c) + 1.4 + abs(math.sin(i)) * 0.9
        l = min(o, c) - 1.4 - abs(math.cos(i)) * 0.9

        out.append({
            "time": (start + timedelta(minutes=i * step)).strftime("%Y-%m-%d %H:%M:%S"),
            "open": round(o, 2),
            "high": round(h, 2),
            "low": round(l, 2),
            "close": round(c, 2),
        })

        price = c

    return out


def fetch_json(url, params):
    if not requests:
        raise RuntimeError("requests belum terinstall")
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def fetch_candles(tf="15min", size=220):
    """
    Near realtime candle:
    - mengambil candle terbaru dari Twelve Data REST API
    - browser auto-refresh setiap beberapa detik
    """
    tf = tf_ok(tf)
    key = get_setting("twelvedata_api_key")
    symbol = get_setting("symbol", "XAU/USD")

    if not key:
        return demo_candles(size, tf), "DEMO - isi Twelve Data API Key untuk data real"

    try:
        data = fetch_json(
            "https://api.twelvedata.com/time_series",
            {
                "symbol": symbol,
                "interval": tf,
                "outputsize": size,
                "apikey": key,
                "timezone": "Asia/Jakarta",
                "order": "ASC",
            }
        )

        if "values" not in data:
            return demo_candles(size, tf), "API error: " + data.get("message", "unknown")

        rows = []
        for x in data["values"]:
            rows.append({
                "time": x.get("datetime"),
                "open": safe_float(x.get("open")),
                "high": safe_float(x.get("high")),
                "low": safe_float(x.get("low")),
                "close": safe_float(x.get("close")),
            })

        return rows, "REAL DATA"

    except Exception as e:
        return demo_candles(size, tf), "Fallback demo: " + str(e)


# ============================================================
# INDICATORS SUPER INTELLIGENCE
# ============================================================

def ema(values, period):
    if not values:
        return []
    k = 2 / (period + 1)
    out = []
    prev = values[0]

    for v in values:
        prev = v * k + prev * (1 - k)
        out.append(prev)

    return out


def sma(values, period):
    out = []
    for i in range(len(values)):
        if i < period -1:
            out.append(sum(values[:i+1]) / max(1, i+1))
        else:
            out.append(sum(values[i-period+1:i+1]) / period)
    return out


def rsi(values, period=14):
    if len(values) < period + 1:
        return [50] * len(values)

    out = [50] * len(values)
    gains = []
    losses = []

    for i in range(1, period + 1):
        diff = values[i] - values[i - 1]
        gains.append(max(diff, 0))
        losses.append(abs(min(diff, 0)))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    for i in range(period, len(values)):
        if i > period:
            diff = values[i] - values[i - 1]
            avg_gain = (avg_gain * (period - 1) + max(diff, 0)) / period
            avg_loss = (avg_loss * (period - 1) + abs(min(diff, 0))) / period

        out[i] = 100 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)

    return out


def atr(candles, period=14):
    if len(candles) < 2:
        return [0] * len(candles)

    tr = [0]

    for i in range(1, len(candles)):
        tr.append(max(
            candles[i]["high"] - candles[i]["low"],
            abs(candles[i]["high"] - candles[i - 1]["close"]),
            abs(candles[i]["low"] - candles[i - 1]["close"]),
        ))

    out = []
    val = tr[1]

    for i, x in enumerate(tr):
        if i < period:
            val = sum(tr[:i + 1]) / max(1, i + 1)
        else:
            val = (val * (period - 1) + x) / period
        out.append(val)

    return out


def macd(closes):
    e12 = ema(closes, 12)
    e26 = ema(closes, 26)
    macd_line = [a - b for a,b in zip(e12, e26)]
    signal_line = ema(macd_line, 9)
    histogram = [a - b for a,b in zip(macd_line, signal_line)]
    return macd_line, signal_line, histogram


def bollinger_bands(closes, period=20, std_dev=2):
    ma = sma(closes, period)
    upper = []
    lower = []
    
    for i in range(len(closes)):
        if i < period -1:
            upper.append(ma[i])
            lower.append(ma[i])
        else:
            window = closes[i-period+1:i+1]
            mean = sum(window)/period
            variance = sum((x-mean)**2 for x in window)/period
            std = math.sqrt(variance)
            upper.append(ma[i] + std_dev * std)
            lower.append(ma[i] - std_dev * std)
    
    return ma, upper, lower


def stochastic_rsi(closes, period=14, smooth_k=3, smooth_d=3):
    r = rsi(closes, period)
    out_k = [50] * len(r)
    out_d = [50] * len(r)
    
    for i in range(period, len(r)):
        window = r[i-period:i+1]
        lowest = min(window)
        highest = max(window)
        if highest == lowest:
            stoch = 50
        else:
            stoch = 100 * (r[i] - lowest) / (highest - lowest)
        out_k[i] = stoch
    
    out_k = sma(out_k, smooth_k)
    out_d = sma(out_k, smooth_d)
    
    return out_k, out_d


def adx(candles, period=14):
    if len(candles) < period + 1:
        return [20] * len(candles)
    
    tr = [0] * len(candles)
    plus_dm = [0] * len(candles)
    minus_dm = [0] * len(candles)
    
    for i in range(1, len(candles)):
        tr[i] = max(
            candles[i]["high"] - candles[i]["low"],
            abs(candles[i]["high"] - candles[i-1]["close"]),
            abs(candles[i]["low"] - candles[i-1]["close"])
        )
        
        up_move = candles[i]["high"] - candles[i-1]["high"]
        down_move = candles[i-1]["low"] - candles[i]["low"]
        
        plus_dm[i] = up_move if up_move > down_move and up_move > 0 else 0
        minus_dm[i] = down_move if down_move > up_move and down_move > 0 else 0
    
    atr_smooth = sma(tr, period)
    
    plus_di = []
    minus_di = []
    
    for x, y in zip(sma(plus_dm, period), atr_smooth):
        if y < 0.0001:
            plus_di.append(0)
        else:
            plus_di.append(100 * x / y)
    
    for x, y in zip(sma(minus_dm, period), atr_smooth):
        if y < 0.0001:
            minus_di.append(0)
        else:
            minus_di.append(100 * x / y)
    
    dx = []
    for p, m in zip(plus_di, minus_di):
        if (p + m) > 0:
            dx.append(100 * abs(p - m) / (p + m))
        else:
            dx.append(0)
    
    return sma(dx, period)


def is_good_trading_hour():
    """
    XAUUSD optimal trading hours:
    Sesi London + New York overlap: 14.00 - 00.00 WIB
    Di luar jam ini likuiditas rendah, spread besar, banyak noise
    """
    hour = datetime.now(TZ).hour
    return 13 <= hour <= 23


def detect_sideways(adx_val, bb_width, bb_width_ma):
    """Deteksi apakah pasar sedang dalam kondisi range/sideways"""
    if adx_val < 22 and bb_width < bb_width_ma * 0.85:
        return True
    return False


def detect_support_resistance(candles, lookback=15):
    """Deteksi level Support & Resistance dari swing high/low terakhir"""
    highs = [x["high"] for x in candles[-lookback:]]
    lows = [x["low"] for x in candles[-lookback:]]
    supports = []
    resistances = []
    
    for i in range(1, len(highs)-1):
        # Swing high: candle tengah lebih tinggi dari tetangga
        if highs[i] > highs[i-1] and highs[i] > highs[i+1]:
            resistances.append(highs[i])
        # Swing low: candle tengah lebih rendah dari tetangga
        if lows[i] < lows[i-1] and lows[i] < lows[i+1]:
            supports.append(lows[i])
    
    # Ambil level terdekat dengan harga saat ini
    price = candles[-1]["close"]
    nearest_support = max([s for s in supports if s < price], default=None)
    nearest_resistance = min([r for r in resistances if r > price], default=None)
    
    return nearest_support, nearest_resistance


def detect_candlestick_pattern(c):
    patterns = []
    body = abs(c['close'] - c['open'])
    wick_top = c['high'] - max(c['open'], c['close'])
    wick_bot = min(c['open'], c['close']) - c['low']
    total = c['high'] - c['low']
    
    if total == 0:
        return patterns
    
    body_ratio = body / total
    
    # Doji
    if body_ratio < 0.1:
        patterns.append("Doji")
    
    # Hammer / Inverted Hammer
    if body_ratio < 0.3 and wick_bot > body * 2 and wick_top < body * 0.5:
        patterns.append("Hammer ✅")
    if body_ratio < 0.3 and wick_top > body * 2 and wick_bot < body * 0.5:
        patterns.append("Inverted Hammer")
    
    # Shooting Star / Hanging Man
    if body_ratio < 0.3 and wick_top > body * 2.5 and wick_bot < body * 0.3:
        patterns.append("Shooting Star ❌")
    
    return patterns


def timeframe_profile(tf):
    mins = tf_minutes(tf)
    if mins <= 3:
        return {"name": "ULTRA SCALPING", "trend": 0.75, "momentum": 1.20, "news": 0.50, "min_trades": 5}
    if mins <= 5:
        return {"name": "SCALPING", "trend": 0.85, "momentum": 1.10, "news": 0.65, "min_trades": 5}
    if mins <= 15:
        return {"name": "INTRADAY FAST", "trend": 1.00, "momentum": 1.00, "news": 0.80, "min_trades": 4}
    if mins <= 60:
        return {"name": "INTRADAY TREND", "trend": 1.15, "momentum": 0.90, "news": 1.00, "min_trades": 3}
    return {"name": "SWING / MACRO", "trend": 1.30, "momentum": 0.80, "news": 1.25, "min_trades": 3}


def ai_memory_summary(tf):
    conn = db()
    rows = [dict(x) for x in conn.execute("SELECT * FROM ai_memory WHERE timeframe=?", (tf,)).fetchall()]
    conn.close()
    out = {"timeframe": tf, "total": 0, "buy_winrate": None, "sell_winrate": None, "pnl_sum": 0}
    for r in rows:
        total = int(r.get("total") or 0)
        wins = int(r.get("wins") or 0)
        side = (r.get("side") or "").upper()
        wr = round(wins / max(1, total) * 100, 1)
        out["total"] += total
        out["pnl_sum"] += safe_float(r.get("pnl_sum"))
        if side == "BUY":
            out["buy_winrate"] = wr
        elif side == "SELL":
            out["sell_winrate"] = wr
    out["pnl_sum"] = round(out["pnl_sum"], 2)
    return out


def learn_from_trade(timeframe, side, pnl):
    tf = tf_ok(timeframe or get_setting("default_tf", "15min"))
    side = (side or "").upper()
    pnl = safe_float(pnl)
    if side not in ["BUY", "SELL"] or pnl == 0:
        return
    conn = db()
    conn.execute("""
        INSERT INTO ai_memory(timeframe,side,total,wins,losses,pnl_sum,last_updated)
        VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(timeframe,side) DO UPDATE SET
            total=total+1,
            wins=wins+excluded.wins,
            losses=losses+excluded.losses,
            pnl_sum=pnl_sum+excluded.pnl_sum,
            last_updated=excluded.last_updated
    """, (tf, side, 1, 1 if pnl > 0 else 0, 1 if pnl < 0 else 0, pnl, now()))
    conn.commit()
    conn.close()


def fetch_news_items(limit=8):
    if get_setting("news_enabled", "1") != "1":
        return []
    key = get_setting("news_api_key", "")
    custom_url = get_setting("news_url", "").strip()
    if requests and (key or custom_url):
        try:
            url = custom_url or "https://newsapi.org/v2/everything"
            params = {"q": "gold OR XAUUSD OR USD OR Federal Reserve", "language": "en", "pageSize": limit, "sortBy": "publishedAt"}
            if key:
                params["apiKey"] = key
            data = fetch_json(url, params)
            articles = data.get("articles", []) if isinstance(data, dict) else []
            return [{"title": a.get("title", ""), "source": (a.get("source") or {}).get("name", "News"), "url": a.get("url", "")} for a in articles[:limit]]
        except Exception:
            pass
    return [{"title": "Fallback: pantau USD Index, US yield, CPI/FOMC, dan geopolitik sebelum entry XAUUSD", "source": "LOCAL", "url": ""}]


def fundamental_bias():
    news = fetch_news_items()
    bullish = ["war", "conflict", "recession", "inflation", "dovish", "rate cut", "weak dollar", "safe haven", "crisis"]
    bearish = ["strong dollar", "hawkish", "rate hike", "higher yields", "risk-on", "jobs beat", "hot payroll", "sticky inflation"]
    score = 0
    hits = []
    for n in news:
        title = (n.get("title") or "").lower()
        for k in bullish:
            if k in title:
                score += 2
                hits.append("bullish:" + k)
        for k in bearish:
            if k in title:
                score -= 2
                hits.append("bearish:" + k)
    score = max(-10, min(10, score))
    label = "BULLISH GOLD" if score > 2 else ("BEARISH GOLD" if score < -2 else "NEUTRAL")
    return {"score": score, "label": label, "hits": hits[:6], "news": news}


def analyze(candles, tf="15min"):
    tf = tf_ok(tf)
    closes = [x["close"] for x in candles]
    opens = [x["open"] for x in candles]
    highs = [x["high"] for x in candles]
    lows = [x["low"] for x in candles]
    
    # All indicators
    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    e100 = ema(closes, 100)
    e200 = ema(closes, 200)
    rr = rsi(closes)
    aa = atr(candles)
    adx_vals = adx(candles)
    macd_line, signal_line, hist = macd(closes)
    bb_mid, bb_upper, bb_lower = bollinger_bands(closes)
    stoch_k, stoch_d = stochastic_rsi(closes)
    
    price = closes[-1]
    reason = []
    warnings = []
    profile = timeframe_profile(tf)
    memory = ai_memory_summary(tf)
    news_bias = fundamental_bias()
    reason.append(f"🧠 AI Profile {tf}: {profile['name']}")
    
    # --------------------------
    # SUPPORT & RESISTANCE LEVELS
    # --------------------------
    nearest_support, nearest_resistance = detect_support_resistance(candles, 15)
    if nearest_support:
        reason.append(f"🔵 Support terdekat: {round(nearest_support, 2)}")
    if nearest_resistance:
        reason.append(f"🔴 Resistance terdekat: {round(nearest_resistance, 2)}")
    
    # --------------------------
    # VOLUME-LIKE ANALYSIS (range candle sebagai proxy)
    # --------------------------
    ranges = [x["high"] - x["low"] for x in candles[-20:]]
    avg_range = sum(ranges) / max(1, len(ranges))
    current_range = candles[-1]["high"] - candles[-1]["low"]
    volume_surge = current_range > avg_range * 1.6
    if volume_surge:
        reason.append(f"📊 Volume surge: range {round(current_range, 2)} > rata-rata {round(avg_range, 2)}")
    
    # --------------------------
    # ✅ NEW: TRADING SESSION FILTER
    # --------------------------
    good_hour = is_good_trading_hour()
    if not good_hour:
        warnings.append("⚠️  DILUAR JAM TRADING OPTIMAL: Sesi Asia, likuiditas rendah")
        warnings.append("⚠️  Semua sinyal dinonaktifkan otomatis")
        # Naikkan threshold secara drastis di luar jam bagus
        min_score_buy = 92
        min_score_sell = 8
    else:
        reason.append("✅ Jam trading optimal: Sesi London + New York aktif")

    # --------------------------
    # TIMEFRAME ADAPTIVE SETTINGS - SUPER INTELLIGENCE v3
    # --------------------------
    tf_mins = tf_minutes(tf)
    
    # Deteksi scalping mode untuk 1m/3m
    is_scalping = tf_mins <= 3
    is_ultra_fast = tf_mins <= 5
    
    # Dynamic threshold berdasarkan timeframe
    if tf_mins <= 3:
        # 1m/3m: SUPER SCALPING - noise maksimal, filter super ketat
        min_trend = 22
        min_score_buy = 85 if good_hour else 94
        min_score_sell = 15 if good_hour else 6
        reason.append(f"⚡ Mode SCALPING {tf}: Filter super ketat, hidden divergence NONAKTIF")
    elif tf_mins <= 5:
        # 5m: Scalping, masih noise tinggi
        min_trend = 20
        min_score_buy = 82 if good_hour else 92
        min_score_sell = 18 if good_hour else 8
        reason.append(f"⚡ Mode {tf}: Filter scalping aktif")
    elif tf_mins <= 15:
        # M15: balance
        min_trend = 12
        min_score_buy = 72 if good_hour else 90
        min_score_sell = 28 if good_hour else 10
    elif tf_mins <= 60:
        # H1: lebih longgar, trend lebih jelas
        min_trend = 8
        min_score_buy = 68 if good_hour else 88
        min_score_sell = 32 if good_hour else 12
    else:
        # H4+ : trend sangat kuat
        min_trend = 5
        min_score_buy = 65 if good_hour else 85
        min_score_sell = 35 if good_hour else 15
        reason.append(f"⚙️  Mode {tf}: Trend mode aktif")
    
    # Scalping noise reduction: kurangi EMA weight 20% untuk 1m/3m
    ema_scale = (0.8 if is_scalping else (0.9 if is_ultra_fast else 1.0)) * profile["trend"]
    if ema_scale < 1.0:
        reason.append(f"🔇 Noise filter: EMA weight {int(ema_scale*100)}%")
    
    # --------------------------
    # SMART TREND ANALYSIS v2
    # --------------------------
    trend_score = 0
    
    # EMA Alignment Check (dengan scalping noise reduction)
    if e20[-1] > e50[-1]: trend_score += 15 * ema_scale
    else: trend_score -= 15 * ema_scale
    
    if e50[-1] > e100[-1]: trend_score += 10 * ema_scale
    else: trend_score -= 10 * ema_scale
    
    if e100[-1] > e200[-1]: trend_score += 8 * ema_scale
    else: trend_score -= 8 * ema_scale
    
    # Price position vs EMAs
    if price > e20[-1]: trend_score += 7 * ema_scale
    else: trend_score -= 7 * ema_scale
    
    if price > e50[-1]: trend_score += 5 * ema_scale
    else: trend_score -= 5 * ema_scale
    
    if price > e100[-1]: trend_score += 4 * ema_scale
    else: trend_score -= 4 * ema_scale
    
    # Trend slope detection (momentum trend)
    e20_slope = e20[-1] - e20[-3]
    if e20_slope > 0.3: trend_score += 6 * ema_scale
    elif e20_slope < -0.3: trend_score -= 6 * ema_scale
    
    # Trend confirmation
    if trend_score >= 25:
        reason.append("✅ Trend BULLISH SANGAT KUAT")
    elif trend_score >= min_trend:
        reason.append("✅ Trend BULLISH")
    elif trend_score <= -25:
        reason.append("❌ Trend BEARISH SANGAT KUAT")
    elif trend_score <= -min_trend:
        reason.append("❌ Trend BEARISH")
    else:
        reason.append("⚠️  Market sideways / range")
        warnings.append("Pasar tidak ada trend jelas, hindari trading")
    
    # --------------------------
    # MOMENTUM INDICATORS v2
    # --------------------------
    momentum_score = 0
    
    # RSI dengan zone
    if rr[-1] < 25:
        momentum_score += 15
        reason.append("✅ RSI DEEP OVERSOLD")
    elif rr[-1] < 35:
        momentum_score += 10
        reason.append("✅ RSI Oversold")
    elif rr[-1] > 75:
        momentum_score -=15
        reason.append("❌ RSI DEEP OVERBOUGHT")
    elif rr[-1] > 65:
        momentum_score -=10
        reason.append("❌ RSI Overbought")
    elif 45 <= rr[-1] <= 55:
        momentum_score +=5
    
    # MACD histogram acceleration
    if hist[-1] > 0 and hist[-1] > hist[-2] and hist[-2] > hist[-3]:
        momentum_score += 14
        reason.append("✅ MACD percepatan BULLISH")
    elif hist[-1] < 0 and hist[-1] < hist[-2] and hist[-2] < hist[-3]:
        momentum_score -=14
        reason.append("❌ MACD percepatan BEARISH")
    elif hist[-1] > signal_line[-1] and hist[-1] > 0:
        momentum_score += 8
    elif hist[-1] < signal_line[-1] and hist[-1] < 0:
        momentum_score -=8
    
    # Stochastic RSI cross
    if stoch_k[-1] < 20 and stoch_k[-1] > stoch_d[-1] and stoch_d[-1] < stoch_d[-2]:
        momentum_score +=12
        reason.append("✅ Stoch RSI Golden Cross")
    elif stoch_k[-1] > 80 and stoch_k[-1] < stoch_d[-1] and stoch_d[-1] > stoch_d[-2]:
        momentum_score -=12
        reason.append("❌ Stoch RSI Death Cross")
    
    # Bollinger Bands squeeze + breakout
    bb_width = bb_upper[-1] - bb_lower[-1]
    bb_width_ma = sum(bb_upper[i] - bb_lower[i] for i in range(-20, 0)) / 20
    
    # ✅ NEW: ADX + SIDEWAYS DETECTION
    adx_current = adx_vals[-1]
    is_sideways = detect_sideways(adx_current, bb_width, bb_width_ma)
    
    if adx_current < 22:
        reason.append(f"📊 ADX {round(adx_current,1)}: Trend LEMAH")
    elif adx_current < 30:
        reason.append(f"📊 ADX {round(adx_current,1)}: Trend NORMAL")
    else:
        reason.append(f"📊 ADX {round(adx_current,1)}: Trend SANGAT KUAT")
    
    if is_sideways:
        warnings.append("⚠️  PASAR SIDEWAYS TERDETEKSI: Semua sinyal dinonaktifkan")
        # Naikkan threshold secara drastis saat sideways
        min_score_buy = 90
        min_score_sell = 10
        # Kurangi semua score 20%
        trend_score = trend_score * 0.8
        momentum_score = momentum_score * 0.8
    
    if bb_width < bb_width_ma * 0.7:
        warnings.append("⚠️  BB SQUEEZE: Volatilitas akan segera meningkat")
    
    if price < bb_lower[-1] and price < closes[-2]:
        momentum_score +=12
        reason.append("✅ Breakout bawah BB")
    elif price > bb_upper[-1] and price > closes[-2]:
        momentum_score -=12
        reason.append("❌ Breakout atas BB")
    
    # --------------------------
    # PATTERN + MULTI CANDLE ANALYSIS
    # --------------------------
    # Candle pattern terakhir
    patterns = detect_candlestick_pattern(candles[-1])
    for p in patterns:
        if '✅' in p:
            momentum_score += 9
        elif '❌' in p:
            momentum_score -=9
        reason.append(f"Candle: {p}")
    
    # 3 Candle pattern detection
    # Three white soldiers
    if closes[-1] > closes[-2] > closes[-3] and closes[-1] > opens[-1] and closes[-2] > opens[-2]:
        momentum_score += 10
        reason.append("✅ Three White Soldiers")
    
    # Three black crows
    if closes[-1] < closes[-2] < closes[-3] and closes[-1] < opens[-1] and closes[-2] < opens[-2]:
        momentum_score -=10
        reason.append("❌ Three Black Crows")
    
    # --------------------------
    # ADVANCED DIVERGENCE DETECTION v2
    # - Lookback 10 candle (lebih akurat dari 5)
    # - Hidden divergence dinonaktifkan untuk scalping (1m/3m)
    # --------------------------
    div_lookback = 10
    # Bullish divergence (10 candle lookback)
    price_low = min(lows[-div_lookback:])
    pos_low = len(lows) - div_lookback + lows[-div_lookback:].index(price_low)
    rsi_at_low = rr[pos_low]
    
    if lows[-1] < price_low and rr[-1] > rsi_at_low and rr[-1] < 50:
        momentum_score +=20
        reason.append("✅✅ BULLISH REGULAR DIVERGENCE - HIGH PROBABILITY!")
    
    # Bearish divergence
    price_high = max(highs[-div_lookback:])
    pos_high = len(highs) - div_lookback + highs[-div_lookback:].index(price_high)
    rsi_at_high = rr[pos_high]
    
    if highs[-1] > price_high and rr[-1] < rsi_at_high and rr[-1] > 50:
        momentum_score -=20
        reason.append("❌❌ BEARISH REGULAR DIVERGENCE - HIGH PROBABILITY!")
    
    # Hidden divergence - dinonaktifkan untuk scalping (1m/3m)
    if not is_scalping:
        if lows[-1] > lows[-3] and rr[-1] < rr[-3]:
            momentum_score +=12
            reason.append("✅ Hidden Bullish Divergence")
        
        if highs[-1] < highs[-3] and rr[-1] > rr[-3]:
            momentum_score -=12
            reason.append("❌ Hidden Bearish Divergence")
    
    # --------------------------
    # TREND EXHAUSTION DETECTION
    # --------------------------
    # Jika EMA20 slope sudah sangat curam tapi momentum mulai melambat
    exhaustion = False
    if e20_slope > 0.8 and hist[-1] < hist[-2] and hist[-2] > hist[-3]:
        exhaustion = True
        momentum_score -= 10
        reason.append("⚠️  TREND EXHAUSTION: EMA20 curam tapi MACD melambat, koreksi mungkin terjadi")
    elif e20_slope < -0.8 and hist[-1] > hist[-2] and hist[-2] < hist[-3]:
        exhaustion = True
        momentum_score += 10
        reason.append("⚠️  TREND EXHAUSTION: EMA20 curam turun tapi MACD melambat, reversal mungkin terjadi")
    
    # --------------------------
    # VOLATILITY FILTER
    # --------------------------
    atr_current = aa[-1]
    atr_ma = sum(aa[-20:])/20
    
    if atr_current > atr_ma * 1.7:
        warnings.append("⚠️  VOLATILITAS EXTREME: Naikkan SL 30%")
        # Kurangi score saat volatility terlalu tinggi
        momentum_score = momentum_score * 0.8
    elif atr_current < atr_ma * 0.6:
        warnings.append("ℹ️  Volatilitas sangat rendah, pasar sedang akumulasi")

    # --------------------------
    # AI MEMORY + FUNDAMENTAL NEWS BIAS
    # --------------------------
    if memory["total"] >= profile["min_trades"]:
        if memory["buy_winrate"] is not None:
            if memory["buy_winrate"] < 45:
                min_score_buy += 5
                warnings.append(f"🧠 Memory {tf}: BUY winrate rendah ({memory['buy_winrate']}%), filter BUY diperketat")
            elif memory["buy_winrate"] > 60:
                min_score_buy -= 3
                reason.append(f"🧠 Memory {tf}: BUY historis kuat ({memory['buy_winrate']}%)")
        if memory["sell_winrate"] is not None:
            if memory["sell_winrate"] < 45:
                min_score_sell -= 5
                warnings.append(f"🧠 Memory {tf}: SELL winrate rendah ({memory['sell_winrate']}%), filter SELL diperketat")
            elif memory["sell_winrate"] > 60:
                min_score_sell += 3
                reason.append(f"🧠 Memory {tf}: SELL historis kuat ({memory['sell_winrate']}%)")
    else:
        reason.append(f"🧠 Memory {tf}: data belajar belum cukup ({memory['total']}/{profile['min_trades']})")

    news_adjust = news_bias["score"] * profile["news"]
    if news_adjust:
        momentum_score += news_adjust
        reason.append(f"📰 News bias: {news_bias['label']} ({round(news_adjust,1)} score)")
    if abs(news_bias["score"]) >= 6:
        warnings.append("📰 High impact news bias terdeteksi, kurangi lot / tunggu volatilitas stabil")

    stats_now = calc_stats()
    daily_guard = {
        "today_pnl": stats_now["today_pnl"],
        "max_consecutive_loss": stats_now["max_consecutive_loss"],
        "status": "OK",
    }
    if stats_now["today_pnl"] <= -abs(stats_now["balance"] * 0.03):
        daily_guard["status"] = "DANGER"
        warnings.append("🛑 Daily guard: PNL hari ini sudah melewati -3% balance, sebaiknya stop trading")
    elif stats_now["max_consecutive_loss"] >= 3:
        daily_guard["status"] = "CAUTION"
        warnings.append("⚠️ Daily guard: loss streak tinggi, kurangi risiko")
    
    # --------------------------
    # FINAL SCORE CALCULATION
    # --------------------------
    total_score = 50 + trend_score + momentum_score
    total_score = max(0, min(100, total_score))
    
    # --------------------------
    # SMART FILTER SINYAL PALSU v2
    # --------------------------
    signal = "WAIT"
    confidence = 0
    
    # Multi konfirmasi wajib: trend + momentum harus searah
    trend_ok_buy = trend_score > min_trend
    trend_ok_sell = trend_score < -min_trend
    
    if total_score >= min_score_buy and trend_ok_buy:
        signal = "BUY"
        confidence = min(95, total_score + 5)
    elif total_score <= min_score_sell and trend_ok_sell:
        signal = "SELL"
        confidence = min(95, 100 - total_score +5)
    else:
        confidence = 50

    ai_checklist = {
        "trend_ok": bool(trend_ok_buy or trend_ok_sell),
        "momentum_ok": bool(abs(momentum_score) >= 10),
        "volatility_ok": bool(atr_ma > 0 and atr_current <= atr_ma * 1.7),
        "news_ok": bool(abs(news_bias["score"]) < 6),
        "memory_ok": bool(memory["total"] < profile["min_trades"] or memory["pnl_sum"] >= 0),
        "daily_guard_ok": daily_guard["status"] == "OK",
    }
    no_trade_reasons = []
    if is_sideways:
        no_trade_reasons.append("sideways")
    if not ai_checklist["volatility_ok"]:
        no_trade_reasons.append("volatilitas ekstrem")
    if not ai_checklist["news_ok"]:
        no_trade_reasons.append("news high impact")
    if daily_guard["status"] == "DANGER":
        no_trade_reasons.append("daily loss limit")
    if confidence < 55:
        no_trade_reasons.append("confidence rendah")

    if no_trade_reasons:
        warnings.append("🚫 NO TRADE ZONE: " + ", ".join(no_trade_reasons))

    quality_points = 0
    quality_points += 2 if confidence >= 80 else (1 if confidence >= 65 else 0)
    quality_points += 1 if ai_checklist["trend_ok"] else 0
    quality_points += 1 if ai_checklist["momentum_ok"] else 0
    quality_points += 1 if ai_checklist["volatility_ok"] else -1
    quality_points += 1 if ai_checklist["news_ok"] else -1
    quality_points += 1 if ai_checklist["memory_ok"] else -1
    quality_points += 1 if ai_checklist["daily_guard_ok"] else -2
    if no_trade_reasons:
        quality_grade = "NO TRADE"
        risk_multiplier_ai = 0
    elif quality_points >= 7:
        quality_grade = "A+"
        risk_multiplier_ai = 1.0
    elif quality_points >= 5:
        quality_grade = "A"
        risk_multiplier_ai = 0.8
    elif quality_points >= 3:
        quality_grade = "B"
        risk_multiplier_ai = 0.55
    elif quality_points >= 1:
        quality_grade = "C"
        risk_multiplier_ai = 0.3
    else:
        quality_grade = "D"
        risk_multiplier_ai = 0.15
    reason.append(f"🏁 Trade Quality: {quality_grade} | Risk factor {int(risk_multiplier_ai*100)}%")
    
    # --------------------------
    # SMART RISK MANAGEMENT v2
    # --------------------------
    # Dynamic SL TP berdasarkan volatilitas, confidence, DAN timeframe
    base_risk = atr_current * 1.0
    
    # Sesuaikan berdasarkan timeframe
    if tf_mins <= 5:
        risk_multiplier = 1.5
    elif tf_mins <= 15:
        risk_multiplier = 1.3
    elif tf_mins <= 60:
        risk_multiplier = 1.1
    else:
        risk_multiplier = 1.0
    
    # Kurangi RR bila confidence rendah
    risk_multiplier += (100 - confidence) / 150
    
    opens = [x["open"] for x in candles]
    
    if signal == "BUY":
        entry = price
        sl = price - base_risk * risk_multiplier
        tp1 = entry + (entry - sl) * 1.0
        tp2 = entry + (entry - sl) * 1.6
        tp = tp2
    elif signal == "SELL":
        entry = price
        sl = price + base_risk * risk_multiplier
        tp1 = entry - (sl - entry) * 1.0
        tp2 = entry - (sl - entry) * 1.6
        tp = tp2
    else:
        entry = price
        sl = None
        tp = None
        tp1 = None
        tp2 = None

    return {
        "signal": signal,
        "score": round(total_score, 1),
        "confidence": round(confidence, 0),
        "price": round(price, 2),
        "ema20": round(e20[-1], 2),
        "ema50": round(e50[-1], 2),
        "rsi": round(rr[-1], 1),
        "atr": round(aa[-1], 2),
        "macd_histogram": round(hist[-1], 3),
        "entry": round(entry, 2),
        "sl": round(sl, 2) if sl else None,
        "tp": round(tp, 2) if tp else None,
        "tp1": round(tp1, 2) if tp1 else None,
        "tp2": round(tp2, 2) if tp2 else None,
        "ai_memory": memory,
        "news_bias": {k: v for k, v in news_bias.items() if k != "news"},
        "quality_grade": quality_grade,
        "risk_multiplier_ai": round(risk_multiplier_ai, 2),
        "no_trade_reasons": no_trade_reasons,
        "ai_checklist": ai_checklist,
        "daily_guard": daily_guard,
        "reason": reason,
        "warnings": warnings,
    }


def series(candles, values):
    return [
        {
            "time": c["time"],
            "value": round(v, 4)
        }
        for c, v in zip(candles, values)
    ]


# ============================================================
# JOURNAL + STATS
# ============================================================

def journal_rows():
    conn = db()
    rows = [
        dict(x)
        for x in conn.execute("SELECT * FROM journal ORDER BY id DESC").fetchall()
    ]
    conn.close()
    return rows


def calc_stats():
    rows = journal_rows()

    initial = safe_float(get_setting("initial_balance", "1000"), 1000)
    pnl = sum(safe_float(x.get("pnl")) for x in rows)
    balance = initial + pnl

    closed = [
        x for x in rows
        if (x.get("result") or "").upper() in ["WIN", "LOSS", "BE"]
        or safe_float(x.get("pnl")) != 0
    ]

    wins = [
        x for x in closed
        if (x.get("result") or "").upper() == "WIN"
        or safe_float(x.get("pnl")) > 0
    ]

    losses = [
        x for x in closed
        if (x.get("result") or "").upper() == "LOSS"
        or safe_float(x.get("pnl")) < 0
    ]

    be = [
        x for x in closed
        if (x.get("result") or "").upper() == "BE"
        or safe_float(x.get("pnl")) == 0
    ]

    gross_profit = sum(max(0, safe_float(x.get("pnl"))) for x in closed)
    gross_loss = abs(sum(min(0, safe_float(x.get("pnl"))) for x in closed))

    cur_loss = 0
    max_loss_streak = 0

    for x in reversed(rows):
        if (x.get("result") or "").upper() == "LOSS" or safe_float(x.get("pnl")) < 0:
            cur_loss += 1
            max_loss_streak = max(max_loss_streak, cur_loss)
        elif (x.get("result") or "").upper() == "WIN" or safe_float(x.get("pnl")) > 0:
            cur_loss = 0

    today_pnl = sum(
        safe_float(x.get("pnl"))
        for x in rows
        if x.get("trade_date") == today()
    )

    return {
        "initial_balance": round(initial, 2),
        "balance": round(balance, 2),
        "total_pnl": round(pnl, 2),
        "total_trades": len(rows),
        "closed_trades": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "be": len(be),
        "winrate": round(len(wins) / max(1, len(wins) + len(losses)) * 100, 2),
        "profit_factor": round(gross_profit / max(gross_loss, 0.00001), 2),
        "avg_pnl": round(pnl / max(1, len(closed)), 2),
        "today_pnl": round(today_pnl, 2),
        "max_consecutive_loss": max_loss_streak,
    }


# ============================================================
# ROUTES
# ============================================================

@app.route("/")
@login_required
def home():
    return render_template_string(
        MAIN_HTML,
        app=APP,
        user=session.get("username"),
        tf=get_setting("default_tf", "15min"),
        theme=get_setting("theme", "dark"),
    )


@app.route("/api/settings", methods=["GET", "POST"])
@login_required
def api_settings():
    keys = [
        "symbol",
        "default_tf",
        "twelvedata_api_key",
        "initial_balance",
        "risk_percent",
        "pip_value_per_lot",
        "refresh_seconds",
        "theme",
        "currency",
        "exchange_rate_usd_idr",
        "news_api_key",
        "news_url",
        "news_enabled",
    ]

    if request.method == "POST":
        data = request.get_json(force=True)
        for k in keys:
            if k in data:
                set_setting(k, data[k])
        return jsonify(ok=True)

    return jsonify({k: get_setting(k) for k in keys})


@app.route("/api/market")
@login_required
def api_market():
    tf = tf_ok(request.args.get("tf", get_setting("default_tf", "15min")))
    candles, status = fetch_candles(tf, 220)
    closes = [x["close"] for x in candles]

    return jsonify(
        status=status,
        server_time=now(),
        candles=candles,
        ema20=series(candles, ema(closes, 20)),
        ema50=series(candles, ema(closes, 50)),
        analysis=analyze(candles, tf),
        stats=calc_stats(),
    )


@app.route("/api/news")
@login_required
def api_news():
    return jsonify(ok=True, news=fetch_news_items(), fundamental=fundamental_bias())


@app.route("/api/ai-memory")
@login_required
def api_ai_memory():
    tf = tf_ok(request.args.get("tf", get_setting("default_tf", "15min")))
    return jsonify(ok=True, memory=ai_memory_summary(tf))


@app.route("/api/risk", methods=["POST"])
@login_required
def api_risk():
    data = request.get_json(force=True)

    balance = safe_float(data.get("balance", calc_stats()["balance"]))
    risk_pct = safe_float(data.get("risk_percent", get_setting("risk_percent", "1")), 1)
    entry = safe_float(data.get("entry"))
    sl = safe_float(data.get("sl"))
    pip_value = safe_float(get_setting("pip_value_per_lot", "1"), 1)

    if entry <= 0 or sl <= 0 or entry == sl:
        return jsonify(ok=False, error="Entry/SL tidak valid"), 400

    risk_amount = balance * risk_pct / 100
    distance = abs(entry - sl)
    lot = risk_amount / max(distance * pip_value, 0.00001)

    return jsonify(
        ok=True,
        balance=round(balance, 2),
        risk_amount=round(risk_amount, 2),
        sl_distance=round(distance, 2),
        suggested_lot=round(lot, 3),
    )


@app.route("/api/convert")
def api_convert():
    amount_usd = safe_float(request.args.get("usd", 0))
    rate = safe_float(get_setting("exchange_rate_usd_idr", "15500"), 15500)
    return jsonify(usd=amount_usd, idr=round(amount_usd * rate, 2), rate=rate)


@app.route("/api/journal", methods=["GET", "POST"])
@login_required
def api_journal():
    conn = db()

    if request.method == "POST":
        data = request.get_json(force=True)

        pnl = safe_float(data.get("pnl"))
        balance_after = calc_stats()["balance"] + pnl

        conn.execute("""
            INSERT INTO journal(
                created_at,
                trade_date,
                symbol,
                timeframe,
                side,
                entry,
                sl,
                tp,
                lot,
                result,
                pnl,
                balance_after,
                note
            )
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            now(),
            today(),
            get_setting("symbol", "XAU/USD"),
            data.get("timeframe"),
            data.get("side"),
            safe_float(data.get("entry")),
            safe_float(data.get("sl")),
            safe_float(data.get("tp")),
            safe_float(data.get("lot")),
            data.get("result", "OPEN"),
            pnl,
            balance_after,
            data.get("note", ""),
        ))

        conn.commit()
        conn.close()

        learn_from_trade(data.get("timeframe"), data.get("side"), pnl)

        return jsonify(ok=True, stats=calc_stats())

    rows = [
        dict(x)
        for x in conn.execute("SELECT * FROM journal ORDER BY id DESC LIMIT 300").fetchall()
    ]

    conn.close()

    return jsonify(rows=rows, stats=calc_stats())


@app.route("/api/active-trades", methods=["GET", "POST", "DELETE"])
@login_required
def api_active_trades():
    conn = db()
    
    if request.method == "POST":
        data = request.get_json(force=True)
        conn.execute("""
            INSERT INTO active_trades(
                created_at,
                symbol,
                timeframe,
                side,
                entry,
                sl,
                tp,
                lot,
                note
            ) VALUES(?,?,?,?,?,?,?,?,?)
        """, (
            now(),
            get_setting("symbol", "XAU/USD"),
            data.get("timeframe"),
            data.get("side"),
            safe_float(data.get("entry")),
            safe_float(data.get("sl")),
            safe_float(data.get("tp")),
            safe_float(data.get("lot")),
            data.get("note", ""),
        ))
        conn.commit()
        conn.close()
        return jsonify(ok=True)
    
    if request.method == "DELETE":
        tid = request.args.get("id")
        close_price = safe_float(request.args.get("close_price"))
        conn.close()
        return jsonify(ok=True)

    rows = [dict(x) for x in conn.execute("SELECT * FROM active_trades WHERE status='OPEN' ORDER BY id DESC").fetchall()]
    conn.close()
    return jsonify(rows=rows)


@app.route("/api/journal/<int:jid>", methods=["DELETE"])
@login_required
def api_journal_delete(jid):
    conn = db()
    conn.execute("DELETE FROM journal WHERE id=?", (jid,))
    conn.commit()
    conn.close()
    return jsonify(ok=True, stats=calc_stats())


@app.route("/export/journal.csv")
@login_required
def export_csv():
    out = io.StringIO()
    writer = csv.writer(out)

    cols = [
        "id",
        "created_at",
        "trade_date",
        "symbol",
        "timeframe",
        "side",
        "entry",
        "sl",
        "tp",
        "lot",
        "result",
        "pnl",
        "balance_after",
        "note",
    ]

    writer.writerow(cols)

    for r in journal_rows():
        writer.writerow([r.get(c, "") for c in cols])

    return Response(
        out.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=xauusd_realtime_journal.csv"}
    )


# ============================================================
# AUTO-CLOSE SL/TP & MANUAL CLOSE
# ============================================================

def close_trade(trade_id, close_price, close_type):
    """Internal function: close trade, calculate PNL, move to journal"""
    conn = db()
    trade = conn.execute("SELECT * FROM active_trades WHERE id=? AND status='OPEN'", (trade_id,)).fetchone()
    if not trade:
        conn.close()
        return None
    
    trade = dict(trade)
    side = trade["side"].upper()
    entry = trade["entry"]
    
    pnl = 0
    lot = trade["lot"] or 0
    if lot > 0 and entry > 0:
        diff = close_price - entry
        pnl = diff * lot if side == "BUY" else -diff * lot
    
    result = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "BE")
    
    balance_before = calc_stats()["balance"]
    
    # Update active_trades status
    conn.execute("""
        UPDATE active_trades SET status='CLOSED', close_price=?, close_price_type=?,
        closed_at=?, pnl_closed=?
        WHERE id=?
    """, (round(close_price, 2), close_type, now(), round(pnl, 2), trade_id))
    
    # Insert to journal
    conn.execute("""
        INSERT INTO journal(
            created_at, trade_date, symbol, timeframe,
            side, entry, sl, tp, lot, result, pnl, balance_after, note
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        now(),
        today(),
        trade["symbol"],
        trade.get("timeframe", ""),
        trade["side"],
        round(entry, 2),
        round(trade["sl"], 2) if trade["sl"] else None,
        round(trade["tp"], 2) if trade["tp"] else None,
        lot,
        result,
        round(pnl, 2),
        round(balance_before + pnl, 2),
        f"Close {close_type}: {trade.get('note', '')}"
    ))
    
    conn.commit()
    conn.close()
    learn_from_trade(trade.get("timeframe", ""), trade["side"], pnl)
    return {"result": result, "pnl": round(pnl, 2), "close_price": round(close_price, 2)}


@app.route("/api/check-trades", methods=["POST"])
@login_required
def api_check_trades():
    """Loop semua active_trades, cek apakah harga sentuh SL/TP"""
    conn = db()
    trades = [dict(x) for x in conn.execute("SELECT * FROM active_trades WHERE status='OPEN'").fetchall()]
    conn.close()
    
    # Ambil harga terbaru dari market
    tf = get_setting("default_tf", "15min")
    candles, _ = fetch_candles(tf, 5)
    if not candles:
        return jsonify(ok=False, error="Gagal ambil data harga")
    
    current_price = candles[-1]["close"]
    current_high = candles[-1]["high"]
    current_low = candles[-1]["low"]
    
    results = []
    for t in trades:
        side = t["side"].upper()
        did_close = False
        close_price = None
        close_type = None
        
        if side == "BUY":
            # Cek SL kena: low terendah <= SL
            if t["sl"] and current_low <= t["sl"]:
                did_close = True
                close_price = t["sl"]
                close_type = "SL"
            # Cek TP kena: high tertinggi >= TP
            elif t["tp"] and current_high >= t["tp"]:
                did_close = True
                close_price = t["tp"]
                close_type = "TP"
        else:  # SELL
            # Cek SL kena: high tertinggi >= SL
            if t["sl"] and current_high >= t["sl"]:
                did_close = True
                close_price = t["sl"]
                close_type = "SL"
            # Cek TP kena: low terendah <= TP
            elif t["tp"] and current_low <= t["tp"]:
                did_close = True
                close_price = t["tp"]
                close_type = "TP"
        
        if did_close:
            result = close_trade(t["id"], close_price, close_type)
            if result:
                results.append({"id": t["id"], **result})
    
    return jsonify(ok=True, closed=results, stats=calc_stats())


@app.route("/api/active-trades/<int:tid>/close", methods=["PUT"])
@login_required
def api_close_trade(tid):
    """Manual close trade by ID"""
    data = request.get_json(force=True) if request.is_json else {}
    close_price = safe_float(data.get("close_price"))
    
    if close_price <= 0:
        return jsonify(ok=False, error="Close price tidak valid"), 400
    
    result = close_trade(tid, close_price, "MANUAL")
    if not result:
        return jsonify(ok=False, error="Trade tidak ditemukan atau sudah closed"), 404
    
    return jsonify(ok=True, **result, stats=calc_stats())


# ============================================================
# HTML
# ============================================================

LOGIN_HTML = r'''
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Login · XAUUSD</title>
<style>
*{box-sizing:border-box;margin:0}
body{
    min-height:100vh;
    display:grid;
    place-items:center;
    font-family:Inter,Segoe UI,system-ui,Arial;
    background:linear-gradient(135deg,#0b1120 0%,#1a2744 40%,#0f172a 100%);
    color:#e2e8f0;
    padding:16px;
}
.card{
    width:min(420px,92vw);
    background:rgba(15,23,42,.85);
    backdrop-filter:blur(20px);
    -webkit-backdrop-filter:blur(20px);
    border:1px solid rgba(148,163,184,.12);
    border-radius:24px;
    padding:36px 28px;
    box-shadow:0 25px 80px rgba(0,0,0,.6),0 0 0 1px rgba(148,163,184,.06) inset;
    transition:transform .2s;
}
.card:hover{transform:translateY(-2px)}
.logo{
    font-size:28px;
    font-weight:800;
    letter-spacing:-.5px;
    background:linear-gradient(135deg,#facc15,#eab308);
    -webkit-background-clip:text;
    -webkit-text-fill-color:transparent;
    margin-bottom:4px;
}
.sub{color:#94a3b8;font-size:14px;margin-bottom:20px}
.input{
    width:100%;
    padding:14px 16px;
    margin:6px 0;
    border-radius:14px;
    border:1px solid rgba(148,163,184,.15);
    background:rgba(30,41,59,.6);
    color:#e2e8f0;
    font-size:15px;
    outline:none;
    transition:border-color .2s,box-shadow .2s;
}
.input:focus{
    border-color:#facc15;
    box-shadow:0 0 0 3px rgba(250,204,21,.12);
}
.input::placeholder{color:#64748b}
.btn{
    width:100%;
    padding:14px;
    border:0;
    border-radius:14px;
    background:linear-gradient(135deg,#facc15,#eab308);
    color:#0f172a;
    font-weight:700;
    font-size:15px;
    cursor:pointer;
    margin-top:8px;
    transition:transform .15s,box-shadow .15s;
}
.btn:hover{transform:scale(1.02);box-shadow:0 8px 30px rgba(250,204,21,.25)}
.btn:active{transform:scale(.98)}
.err{
    background:rgba(239,68,68,.12);
    border:1px solid rgba(239,68,68,.25);
    border-radius:12px;
    padding:10px 14px;
    margin-bottom:12px;
    color:#fca5a5;
    font-size:14px;
}
.footer{margin-top:20px;font-size:12px;color:#64748b;text-align:center}
.loading{display:none;text-align:center;margin-top:8px;color:#94a3b8;font-size:13px}
</style>
</head>
<body>
<form class="card" method="post" onsubmit="document.getElementById('load').style.display='block'">
    <div class="logo">📊 {{app}}</div>
    <p class="sub">Masuk ke dashboard trading</p>

    {% if err %}
    <div class="err">{{err}}</div>
    {% endif %}

    <input class="input" name="username" placeholder="Username" autocomplete="username" autofocus>
    <input class="input" type="password" name="password" placeholder="Password" autocomplete="current-password">
    <button class="btn" type="submit">→ Masuk</button>
    <div class="loading" id="load">⏳ Memproses...</div>
    <div class="footer">Default: admin / admin123</div>
</form>
</body>
</html>
'''


MAIN_HTML = r'''
<!doctype html>
<html data-theme="{{theme}}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>{{app}}</title>
<script src="https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"></script>

<style>
/* ===== CSS VARIABLES ===== */
:root{
    --bg:#f0f2f5;
    --side:#fff;
    --card:#fff;
    --card2:#f8fafc;
    --card3:#f1f5f9;
    --txt:#0f172a;
    --txt2:#475569;
    --mut:#94a3b8;
    --line:#e2e8f0;
    --accent:#1e293b;
    --green:#059669;
    --green-bg:#ecfdf5;
    --red:#dc2626;
    --red-bg:#fef2f2;
    --gold:#ca8a04;
    --gold-bg:#fefce8;
    --blue:#2563eb;
    --blue-bg:#eff6ff;
    --shadow:0 1px 3px rgba(0,0,0,.06),0 1px 2px rgba(0,0,0,.04);
    --shadow-lg:0 10px 40px rgba(0,0,0,.08);
    --radius:12px;
    --radius-lg:16px;
    --sidebar-w:220px;
}

html[data-theme="dark"]{
    --bg:#080c14;
    --side:#0f172a;
    --card:#0f172a;
    --card2:#1a2332;
    --card3:#1e293b;
    --txt:#f1f5f9;
    --txt2:#94a3b8;
    --mut:#64748b;
    --line:#1e293b;
    --accent:#facc15;
    --green:#34d399;
    --green-bg:rgba(52,211,153,.1);
    --red:#fb7185;
    --red-bg:rgba(251,113,133,.1);
    --gold:#facc15;
    --gold-bg:rgba(250,204,21,.1);
    --blue:#60a5fa;
    --blue-bg:rgba(96,165,250,.1);
    --shadow:0 1px 3px rgba(0,0,0,.2);
    --shadow-lg:0 10px 40px rgba(0,0,0,.4);
}

/* ===== BASE ===== */
*{box-sizing:border-box;margin:0}
body{
    margin:0;
    background:var(--bg);
    color:var(--txt);
    font-family:Inter,Segoe UI,system-ui,sans-serif;
    font-size:14px;
    overflow-x:hidden;
    display:flex;
    min-height:100vh;
}

/* ===== SCROLLBAR ===== */
::-webkit-scrollbar{width:5px;height:5px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--mut);border-radius:10px}

/* ===== SIDEBAR ===== */
.sidebar{
    width:var(--sidebar-w);
    background:var(--side);
    border-right:1px solid var(--line);
    padding:0;
    display:flex;
    flex-direction:column;
    position:fixed;
    top:0;left:0;bottom:0;
    z-index:10;
    transition:transform .25s ease;
}
.sidebar-brand{
    padding:20px 18px 14px;
    font-size:13px;
    font-weight:800;
    letter-spacing:-.3px;
    background:linear-gradient(135deg,#facc15,#eab308);
    -webkit-background-clip:text;
    -webkit-text-fill-color:transparent;
    border-bottom:1px solid var(--line);
    display:flex;
    align-items:center;
    gap:8px;
}
.sidebar-brand span{font-size:18px;-webkit-text-fill-color:initial;color:var(--txt)}
.sidebar-nav{
    flex:1;
    overflow-y:auto;
    padding:8px;
}
.nav-item{
    display:flex;
    align-items:center;
    gap:10px;
    padding:11px 14px;
    border-radius:var(--radius);
    color:var(--txt2);
    cursor:pointer;
    font-weight:500;
    font-size:13px;
    transition:all .15s;
    margin-bottom:2px;
    user-select:none;
}
.nav-item:hover{background:var(--card2);color:var(--txt)}
.nav-item.active{
    background:var(--gold-bg);
    color:var(--gold);
    font-weight:600;
}
.nav-item .icon{font-size:16px;width:20px;text-align:center}
.sidebar-footer{
    padding:12px 14px;
    border-top:1px solid var(--line);
    font-size:12px;
    color:var(--mut);
    display:flex;
    align-items:center;
    justify-content:space-between;
}

/* ===== MAIN CONTENT ===== */
.main{
    flex:1;
    margin-left:var(--sidebar-w);
    padding:0;
    min-height:100vh;
}
.topbar{
    padding:14px 24px;
    display:flex;
    align-items:center;
    justify-content:space-between;
    border-bottom:1px solid var(--line);
    background:var(--card);
    position:sticky;
    top:0;
    z-index:5;
    gap:12px;
    flex-wrap:wrap;
}
.topbar-left{display:flex;align-items:center;gap:12px}
.topbar-right{display:flex;align-items:center;gap:8px}
.topbar .brand-mobile{display:none;font-weight:700;font-size:16px;color:var(--gold)}
.content{padding:16px 24px 40px}

/* ===== TAB SYSTEM ===== */
.tab{display:none}
.tab.active{display:block;animation:fadeIn .2s ease}
@keyframes fadeIn{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}

/* ===== COMPONENTS ===== */
.card{
    background:var(--card);
    border:1px solid var(--line);
    border-radius:var(--radius-lg);
    padding:16px 20px;
    box-shadow:var(--shadow);
    margin-bottom:14px;
}
.card-header{
    display:flex;
    align-items:center;
    justify-content:space-between;
    margin-bottom:12px;
}
.card-title{font-weight:600;font-size:15px;color:var(--txt)}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.btn{
    background:var(--card2);
    color:var(--txt);
    border:1px solid var(--line);
    border-radius:var(--radius);
    padding:8px 14px;
    cursor:pointer;
    font-weight:500;
    font-size:13px;
    transition:all .15s;
    display:inline-flex;
    align-items:center;
    gap:5px;
    white-space:nowrap;
}
.btn:hover{background:var(--card3);border-color:var(--mut)}
.btn:active{transform:scale(.97)}
.btn-primary{
    background:var(--gold);
    color:#0f172a;
    border-color:var(--gold);
    font-weight:600;
}
.btn-primary:hover{filter:brightness(1.1)}
.btn-danger{background:var(--red-bg);color:var(--red);border-color:transparent}
.btn-danger:hover{background:var(--red);color:#fff}
.input, .select{
    background:var(--card2);
    color:var(--txt);
    border:1px solid var(--line);
    border-radius:var(--radius);
    padding:9px 12px;
    font-size:13px;
    outline:none;
    transition:border-color .15s;
}
.input:focus,.select:focus{border-color:var(--gold)}
.input::placeholder{color:var(--mut)}
.select{cursor:pointer;appearance:none;padding-right:28px;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%2394a3b8' stroke-width='2'%3E%3Cpath d='M6 9l6 6 6-6'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 10px center}
label{gap:5px;display:inline-flex;align-items:center;cursor:pointer}

/* ===== KPI GRID ===== */
.kpi-grid{
    display:grid;
    grid-template-columns:repeat(auto-fill,minmax(140px,1fr));
    gap:10px;
}
.kpi-box{
    background:var(--card2);
    border:1px solid var(--line);
    border-radius:var(--radius);
    padding:12px;
    text-align:center;
}
.kpi-box .val{
    display:block;
    font-size:18px;
    font-weight:700;
    line-height:1.3;
}
.kpi-box .lbl{
    font-size:11px;
    color:var(--mut);
    text-transform:uppercase;
    letter-spacing:.3px;
}

/* ===== SIGNAL DISPLAY ===== */
.signal-box{
    text-align:center;
    padding:20px;
    border-radius:var(--radius-lg);
    background:var(--card2);
    border:2px solid var(--line);
    margin-bottom:10px;
    transition:all .3s;
}
.signal-box.buy{border-color:var(--green);background:var(--green-bg)}
.signal-box.sell{border-color:var(--red);background:var(--red-bg)}
.signal-box.wait{border-color:var(--gold);background:var(--gold-bg)}
.signal-text{
    font-size:42px;
    font-weight:900;
    letter-spacing:-1px;
}
.signal-text.buy{color:var(--green)}
.signal-text.sell{color:var(--red)}
.signal-text.wait{color:var(--gold)}
.signal-score{font-size:13px;color:var(--txt2);margin-top:4px}
.signal-meta{display:flex;gap:16px;justify-content:center;margin-top:8px;font-size:13px;color:var(--txt2)}
.signal-meta b{color:var(--txt)}

/* ===== CHART ===== */
#chart{height:480px;width:100%}
#chart2{height:480px;width:100%}
.chart-controls{
    display:flex;
    gap:8px;
    align-items:center;
    flex-wrap:wrap;
    margin-bottom:10px;
}
.tf-btn{
    padding:5px 12px;
    border-radius:8px;
    border:1px solid var(--line);
    background:var(--card2);
    color:var(--txt2);
    cursor:pointer;
    font-size:12px;
    font-weight:500;
    transition:all .1s;
}
.tf-btn.active{background:var(--gold);color:#0f172a;border-color:var(--gold);font-weight:600}
.tf-btn:hover{background:var(--card3)}

/* ===== TABLES ===== */
.table-wrap{overflow-x:auto;max-height:400px;overflow-y:auto}
table{width:100%;border-collapse:collapse;font-size:12px}
th{
    position:sticky;
    top:0;
    background:var(--card2);
    padding:9px 8px;
    text-align:left;
    font-weight:600;
    color:var(--txt2);
    border-bottom:2px solid var(--line);
    white-space:nowrap;
    cursor:pointer;
    user-select:none;
}
th:hover{color:var(--txt)}
td{
    padding:8px;
    border-bottom:1px solid var(--line);
    white-space:nowrap;
}
tr:hover td{background:var(--card2)}
.badge{
    display:inline-block;
    padding:2px 8px;
    border-radius:10px;
    font-size:11px;
    font-weight:600;
}
.badge-win{background:var(--green-bg);color:var(--green)}
.badge-loss{background:var(--red-bg);color:var(--red)}
.badge-be{background:var(--gold-bg);color:var(--gold)}
.badge-open{background:var(--blue-bg);color:var(--blue)}

/* ===== TOAST ===== */
.toast-container{
    position:fixed;
    top:20px;
    right:20px;
    z-index:999;
    display:flex;
    flex-direction:column;
    gap:8px;
}
.toast{
    padding:12px 18px;
    border-radius:var(--radius);
    color:#fff;
    font-weight:500;
    font-size:13px;
    box-shadow:var(--shadow-lg);
    animation:slideIn .2s ease;
    max-width:360px;
}
.toast-success{background:var(--green)}
.toast-error{background:var(--red)}
.toast-info{background:var(--blue)}
@keyframes slideIn{from{transform:translateX(60px);opacity:0}to{transform:translateX(0);opacity:1}}

/* ===== ANALYTICS ===== */
.analytics-grid{
    display:grid;
    grid-template-columns:1fr 1fr;
    gap:14px;
}
.analytics-chart{height:280px;width:100%}

/* ===== CONFETTI ===== */
.confetti-container{
    position:fixed;
    top:0;left:0;right:0;bottom:0;
    pointer-events:none;
    z-index:100;
}
.confetti-piece{
    position:absolute;
    width:8px;height:8px;
    border-radius:2px;
    animation:confettiFall linear forwards;
}
@keyframes confettiFall{
    0%{transform:translateY(-20px) rotate(0deg);opacity:1}
    100%{transform:translateY(100vh) rotate(720deg);opacity:0}
}

/* ===== RESPONSIVE ===== */
.sidebar-toggle{display:none;background:none;border:none;color:var(--txt);font-size:22px;cursor:pointer;padding:4px}
.sidebar-backdrop{display:none}

@media(max-width:900px){
    body{display:block}
    .sidebar{transform:translateX(-100%);z-index:30;width:min(82vw,280px);box-shadow:var(--shadow-lg)}
    .sidebar.open{transform:translateX(0)}
    .sidebar-backdrop{display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:20}
    .sidebar-backdrop.show{display:block}
    .sidebar-toggle{display:block}
    .main{margin-left:0}
    .topbar{padding:10px 12px;gap:8px;align-items:flex-start}
    .topbar-left{min-width:0;flex:1;gap:8px}
    .topbar-right{gap:6px;flex-wrap:wrap;justify-content:flex-end}
    .topbar .brand-mobile{display:block;white-space:nowrap}
    #status{display:block;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .analytics-grid{grid-template-columns:1fr}
    .content{padding:12px}
    .card{padding:14px;margin-bottom:12px;border-radius:14px}
    .card-header{align-items:flex-start;gap:8px;flex-wrap:wrap}
    .row,.chart-controls{gap:6px}
    .row .input,.row .select{flex:1 1 110px;min-width:0}
    .btn,.input,.select{min-height:38px}
    .kpi-grid{grid-template-columns:repeat(2,1fr)}
    .signal-meta{flex-wrap:wrap;gap:8px}
    .table-wrap{max-width:100%;-webkit-overflow-scrolling:touch}
    #chart,#chart2{height:320px}
    .analytics-chart{height:240px}
    .toast-container{top:10px;right:10px;left:10px}
    .toast{max-width:none}
}

@media(max-width:500px){
    body{font-size:13px}
    .content{padding:10px 8px 28px}
    .card{padding:12px}
    .kpi-grid{grid-template-columns:1fr 1fr}
    .kpi-box{padding:10px 6px}
    .kpi-box .val{font-size:15px}
    .kpi-box .lbl{font-size:10px}
    .signal-text{font-size:32px}
    .signal-box{padding:16px 10px}
    .topbar-right .btn{font-size:11px;padding:6px 10px}
    .tf-btn{flex:1 1 calc(25% - 6px);justify-content:center;padding:7px 8px}
    .chart-controls label{margin-left:0!important}
    #chart,#chart2{height:280px}
    #tab-settings .card>div[style*="grid-template-columns"]{grid-template-columns:1fr!important;max-width:none!important}
    #tab-settings .row .input,#tab-settings .row .select,#tab-settings .row .btn{width:100%!important;flex-basis:100%}
    #tab-journal .row .input,#tab-journal .row .select,#tab-journal .row .btn,#tab-journal .row a.btn,
    #tab-trades .row .input,#tab-trades .row .select,#tab-trades .row .btn{width:100%!important;flex-basis:100%}
}
</style>
</head>

<body>
<!-- ===== SIDEBAR ===== -->
<nav class="sidebar" id="sidebar">
    <div class="sidebar-brand">📊 {{app}} <span>XAUUSD</span></div>
    <div class="sidebar-nav">
        <div class="nav-item active" data-tab="dashboard"><span class="icon">📈</span> Dashboard</div>
        <div class="nav-item" data-tab="chart"><span class="icon">🕯️</span> Chart</div>
        <div class="nav-item" data-tab="journal"><span class="icon">📓</span> Journal</div>
        <div class="nav-item" data-tab="analytics"><span class="icon">📊</span> Analytics</div>
        <div class="nav-item" data-tab="trades"><span class="icon">⚡</span> Active Trades</div>
        <div class="nav-item" data-tab="settings"><span class="icon">⚙️</span> Settings</div>
    </div>
    <div class="sidebar-footer">
        <span id="sideUser">{{user}}</span>
        <a href="/logout" style="color:var(--mut);text-decoration:none;font-size:12px">Logout</a>
    </div>
</nav>
<div class="sidebar-backdrop" id="sidebarBackdrop" onclick="toggleSidebar(false)"></div>

<!-- ===== MAIN CONTENT ===== -->
<div class="main">
    <div class="topbar">
        <div class="topbar-left">
            <button class="sidebar-toggle" onclick="toggleSidebar()">☰</button>
            <div class="brand-mobile">📊 XAUUSD</div>
            <span class="muted" id="status" style="font-size:12px;color:var(--mut)">-</span>
        </div>
        <div class="topbar-right">
            <label style="font-size:12px;color:var(--txt2)">
                <input type="checkbox" id="live" checked onchange="if(this.checked)startTimer();else if(window.timer)clearInterval(window.timer)">
                Auto
            </label>
            <button class="btn btn-primary" onclick="loadMarket()">⟳ Refresh</button>
            <button class="btn" id="themeBtn" onclick="toggleTheme()">🌙</button>
        </div>
    </div>

    <div class="content" id="contentArea">
        <!-- ===== TAB: DASHBOARD ===== -->
        <div class="tab active" id="tab-dashboard">
            <div class="card">
                <div id="signalBox" class="signal-box wait">
                    <div id="signalText" class="signal-text wait">WAIT</div>
                    <div class="signal-score">Score: <b id="score">0</b>% · Price: <b id="price">-</b> · Confidence: <b id="confidence">-</b></div>
                </div>
                <div id="warnings" style="margin-top:8px"></div>
            </div>

            <div class="card">
                <div class="card-header"><span class="card-title">🧠 AI Memory & News Bias</span></div>
                <div id="aiBox" class="row" style="font-size:13px;color:var(--txt2)"></div>
            </div>

            <div class="kpi-grid" id="statsGrid"></div>

            <div class="card" style="margin-top:14px">
                <div class="card-header"><span class="card-title">📊 Market Overview</span></div>
                <div class="row">
                    <button class="btn tf-btn" data-tf="1min" onclick="switchTF('1min')">M1</button>
                    <button class="btn tf-btn" data-tf="3min" onclick="switchTF('3min')">M3</button>
                    <button class="btn tf-btn" data-tf="5min" onclick="switchTF('5min')">M5</button>
                    <button class="btn tf-btn active" data-tf="15min" onclick="switchTF('15min')">M15</button>
                    <button class="btn tf-btn" data-tf="1h" onclick="switchTF('1h')">H1</button>
                    <button class="btn tf-btn" data-tf="4h" onclick="switchTF('4h')">H4</button>
                    <button class="btn tf-btn" data-tf="1day" onclick="switchTF('1day')">D1</button>
                    <select id="candleType" class="select" style="font-size:11px;padding:5px 8px" onchange="changeCandleType(this.value)">
                        <option value="candle">🕯️ Candle</option>
                        <option value="heiken">🌊 Heiken Ashi</option>
                        <option value="line">📈 Line</option>
                        <option value="area">📊 Area</option>
                        <option value="bar">📉 Bar</option>
                    </select>
                    <div style="flex:1"></div>
                    <button class="btn btn-primary" id="predBtn" onclick="runPrediction()">🔮 Prediksi</button>
                </div>
                <div id="chart"></div>
            </div>

            <div class="card">
                <div class="card-header"><span class="card-title">📋 Analisa</span></div>
                <div id="reasonBox"></div>
            </div>
        </div>

        <!-- ===== TAB: CHART ===== -->
        <div class="tab" id="tab-chart">
            <div class="card">
                <div class="card-header"><span class="card-title">🕯️ Full Chart</span></div>
                <div class="chart-controls">
                    <button class="btn tf-btn" data-tf2="1min" onclick="switchTF2('1min')">M1</button>
                    <button class="btn tf-btn" data-tf2="3min" onclick="switchTF2('3min')">M3</button>
                    <button class="btn tf-btn" data-tf2="5min" onclick="switchTF2('5min')">M5</button>
                    <button class="btn tf-btn active" data-tf2="15min" onclick="switchTF2('15min')">M15</button>
                    <button class="btn tf-btn" data-tf2="1h" onclick="switchTF2('1h')">H1</button>
                    <button class="btn tf-btn" data-tf2="4h" onclick="switchTF2('4h')">H4</button>
                    <button class="btn tf-btn" data-tf2="1day" onclick="switchTF2('1day')">D1</button>
                    <label style="margin-left:12px;font-size:12px;color:var(--txt2)">
                        <input type="checkbox" id="showEma20_2" checked onchange="e20_2Ref.setVisible(this.checked)">
                        EMA20
                    </label>
                    <label style="font-size:12px;color:var(--txt2)">
                        <input type="checkbox" id="showEma50_2" checked onchange="e50_2Ref.setVisible(this.checked)">
                        EMA50
                    </label>
                    <button class="btn" onclick="chart2Ref.timeScale().fitContent()">🔲 Fit</button>
                </div>
                <div id="chart2"></div>
            </div>

            <div class="card">
                <div class="card-header">
                    <span class="card-title">Signal</span>
                    <div>
                        <span id="signal2" style="font-weight:700;font-size:16px;padding:4px 12px;border-radius:8px"></span>
                    </div>
                </div>
                <div class="row" style="gap:16px">
                    <span>Score: <b id="score2">0</b>%</span>
                    <span>RSI: <b id="rsi2">-</b></span>
                    <span>Entry: <b id="entry2">-</b></span>
                    <span>SL: <b id="sl2">-</b></span>
                    <span>TP: <b id="tp2">-</b></span>
                </div>
                <div id="reason2" style="margin-top:8px"></div>
                <button class="btn btn-primary" style="margin-top:8px" onclick="fillJournalFromSignal()">📝 Isi Journal</button>
            </div>
        </div>

        <!-- ===== TAB: JOURNAL ===== -->
        <div class="tab" id="tab-journal">
            <div class="card">
                <div class="card-header"><span class="card-title">📓 Trading Journal</span></div>
                <div class="row" style="margin-bottom:10px;flex-wrap:wrap">
                    <select id="j_side" class="select"><option>BUY</option><option>SELL</option></select>
                    <input id="j_entry" class="input" placeholder="Entry" style="width:80px">
                    <input id="j_sl" class="input" placeholder="SL" style="width:80px">
                    <input id="j_tp" class="input" placeholder="TP" style="width:80px">
                    <input id="j_lot" class="input" placeholder="Lot" style="width:70px">
                    <select id="j_result" class="select">
                        <option>OPEN</option><option>WIN</option><option>LOSS</option><option>BE</option>
                    </select>
                    <input id="j_pnl" class="input" placeholder="PNL $" style="width:80px">
                    <input id="j_note" class="input" placeholder="Catatan" style="width:120px">
                    <button class="btn btn-primary" onclick="saveJournal()">Save</button>
                    <a class="btn" href="/export/journal.csv">CSV</a>
                </div>
                <div class="row" style="margin-bottom:10px;gap:6px">
                    <input id="jFilterText" class="input" placeholder="🔍 Cari..." style="width:160px" oninput="filterJournal()">
                    <select id="jFilterResult" class="select" onchange="filterJournal()">
                        <option value="">Semua</option><option>WIN</option><option>LOSS</option><option>BE</option><option>OPEN</option>
                    </select>
                    <select id="jFilterSide" class="select" onchange="filterJournal()">
                        <option value="">Side</option><option>BUY</option><option>SELL</option>
                    </select>
                    <span style="font-size:12px;color:var(--mut)" id="jCount"></span>
                </div>
                <div class="table-wrap" id="journalTable"></div>
                <div class="row" style="margin-top:8px;justify-content:center;gap:4px" id="jPagination"></div>
            </div>
        </div>

        <!-- ===== TAB: ANALYTICS ===== -->
        <div class="tab" id="tab-analytics">
            <div class="card">
                <div class="card-header"><span class="card-title">💰 Equity Curve</span></div>
                <div id="equityChart" class="analytics-chart"></div>
            </div>
            <div class="analytics-grid">
                <div class="card">
                    <div class="card-header"><span class="card-title">🥧 Win / Loss</span></div>
                    <div id="pieChart" style="height:240px;display:flex;align-items:center;justify-content:center"></div>
                </div>
                <div class="card">
                    <div class="card-header"><span class="card-title">📊 Monthly PNL</span></div>
                    <div id="monthlyChart" style="height:240px"></div>
                </div>
            </div>
            <div class="card">
                <div class="card-header"><span class="card-title">📈 Performance Summary</span></div>
                <div id="perfSummary" style="font-size:13px"></div>
            </div>
        </div>

        <!-- ===== TAB: TRADES ===== -->
        <div class="tab" id="tab-trades">
            <div class="card">
                <div class="card-header"><span class="card-title">⚡ Active Trades</span></div>
                <div class="row" style="margin-bottom:10px;flex-wrap:wrap">
                    <select id="at_side" class="select"><option>BUY</option><option>SELL</option></select>
                    <input id="at_entry" class="input" placeholder="Entry" style="width:100px">
                    <input id="at_sl" class="input" placeholder="SL" style="width:100px">
                    <input id="at_tp" class="input" placeholder="TP" style="width:100px">
                    <input id="at_lot" class="input" placeholder="Lot" style="width:80px">
                    <input id="at_note" class="input" placeholder="Note" style="width:120px">
                    <button class="btn btn-primary" onclick="saveActiveTrade()">+ Open Trade</button>
                </div>
                <div id="activeTradesList"></div>
            </div>
        </div>

        <!-- ===== TAB: SETTINGS ===== -->
        <div class="tab" id="tab-settings">
            <div class="card">
                <div class="card-header"><span class="card-title">⚙️ Settings</span></div>
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;max-width:500px">
                    <input id="set_symbol" class="input" placeholder="Symbol XAU/USD">
                    <input id="set_td" class="input" placeholder="Twelve Data API Key">
                    <input id="set_balance" class="input" placeholder="Initial Balance">
                    <input id="set_risk" class="input" placeholder="Risk %">
                    <input id="set_pip" class="input" placeholder="Pip value per lot">
                    <input id="set_refresh" class="input" placeholder="Refresh seconds">
                </div>
                <div class="row" style="margin-top:10px">
                    <button class="btn btn-primary" onclick="saveSettings()">💾 Save</button>
                </div>
            </div>
            <div class="card">
                <div class="card-header"><span class="card-title">🔐 Change Password</span></div>
                <div class="row">
                    <input id="oldp" type="password" class="input" placeholder="Password lama" style="width:180px">
                    <input id="newp" type="password" class="input" placeholder="Password baru" style="width:180px">
                    <button class="btn" onclick="changePass()">Ganti</button>
                </div>
                <p id="msg" style="margin-top:6px;font-size:12px;color:var(--mut)"></p>
            </div>
            <div class="card">
                <div class="card-header"><span class="card-title">🛠 Risk Calculator</span></div>
                <div class="row">
                    <input id="r_balance" class="input" placeholder="Balance" style="width:120px">
                    <input id="r_risk" class="input" placeholder="Risk %" style="width:80px">
                    <input id="r_entry" class="input" placeholder="Entry" style="width:100px">
                    <input id="r_sl" class="input" placeholder="SL" style="width:100px">
                    <button class="btn btn-primary" onclick="calcRisk()">Hitung</button>
                </div>
                <pre id="riskOut" style="margin-top:8px;font-size:12px;color:var(--txt2)"></pre>
            </div>
        </div>
    </div>
</div>

<!-- ===== TOAST CONTAINER ===== -->
<div class="toast-container" id="toastContainer"></div>

<script>
// ===== STATE =====
let chartRef, candlesRef, e20Ref, e50Ref;
let chart2Ref, candles2Ref, e20_2Ref, e50_2Ref;
let lastAnalysis = null;
let refreshSec = 10;
let timer = null;
let lastCandleTime = 0;
let currentCandle = null;
let targetPrice = 0;
let smoothTimer = null;
let priceVelocity = 0;
let currentTF = '{{tf}}';
let allJournalData = [];
let journalPage = 1;
const PER_PAGE = 25;

// ===== TOAST =====
function showToast(msg, type='info'){
    const c = document.getElementById('toastContainer');
    const t = document.createElement('div');
    t.className = 'toast toast-' + type;
    t.textContent = msg;
    c.appendChild(t);
    setTimeout(() => {t.style.opacity='0';t.style.transition='opacity .3s';setTimeout(()=>t.remove(),300)}, 3000);
}

// ===== MOBILE SIDEBAR & CHART RESIZE =====
function toggleSidebar(force){
    const sidebar = document.getElementById('sidebar');
    const backdrop = document.getElementById('sidebarBackdrop');
    const open = force === undefined ? !sidebar.classList.contains('open') : !!force;
    sidebar.classList.toggle('open', open);
    backdrop.classList.toggle('show', open);
}

function resizeCharts(){
    const chartBox = document.getElementById('chart');
    const chart2Box = document.getElementById('chart2');
    if(chartRef && chartBox) chartRef.applyOptions({width: chartBox.clientWidth, height: chartBox.clientHeight || (window.innerWidth <= 500 ? 280 : 320)});
    if(chart2Ref && chart2Box) chart2Ref.applyOptions({width: chart2Box.clientWidth, height: chart2Box.clientHeight || (window.innerWidth <= 500 ? 280 : 320)});
}

// ===== TAB SWITCHING =====
document.querySelectorAll('.nav-item').forEach(item => {
    item.addEventListener('click', function(){
        document.querySelectorAll('.nav-item').forEach(x => x.classList.remove('active'));
        this.classList.add('active');
        document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
        document.getElementById('tab-' + this.dataset.tab).classList.add('active');
        if(this.dataset.tab === 'analytics') renderAnalytics();
        if(this.dataset.tab === 'trades') loadActiveTrades();
        if(this.dataset.tab === 'journal') loadJournal();
        if(this.dataset.tab === 'chart') loadChartTab();
        if(window.innerWidth <= 900) toggleSidebar(false);
        setTimeout(resizeCharts, 120);
    });
});

// ===== TIMEFRAME SWITCH (Dashboard) =====
function switchTF(tf){
    currentTF = tf;
    document.querySelectorAll('[data-tf]').forEach(x => x.classList.remove('active'));
    document.querySelector('[data-tf="'+tf+'"]')?.classList.add('active');
    loadMarket();
}
function switchTF2(tf){
    currentTF = tf;
    document.querySelectorAll('[data-tf2]').forEach(x => x.classList.remove('active'));
    document.querySelector('[data-tf2="'+tf+'"]')?.classList.add('active');
    loadChartTab();
}

// ===== TOGGLE THEME =====
async function toggleTheme(){
    const html = document.documentElement;
    const isDark = html.dataset.theme === 'dark';
    html.dataset.theme = isDark ? 'light' : 'dark';
    document.getElementById('themeBtn').textContent = isDark ? '🌙' : '☀️';
    await fetch('/api/settings', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({theme: html.dataset.theme})
    });
    showToast('Theme diganti', 'info');
}

// ===== TO UNIX =====
function toUnix(t){
    return Math.floor(new Date(String(t).replace(' ','T')).getTime()/1000);
}

// ===== RUN PREDICTION =====
async function runPrediction(){
    const btn = document.getElementById('predBtn');
    btn.disabled = true;
    btn.textContent = '⏳...';
    lastCandleTime = 0;
    await new Promise(r => setTimeout(r, 600));
    await loadMarket();
    fillJournalFromSignal();
    btn.textContent = '🔮 Prediksi';
    btn.disabled = false;
}

// ===== LOAD MARKET =====
async function loadMarket(){
    try {
        let data = await fetch('/api/market?tf=' + currentTF).then(r => r.json());
        document.getElementById('status').textContent = data.status + ' | ' + data.server_time;

        let newCandles = data.candles.map(x => ({
            time: toUnix(x.time), open: x.open, high: x.high, low: x.low, close: x.close
        }));
        let last = newCandles[newCandles.length - 1];

        if(last.time === lastCandleTime && currentCandle){
            currentCandle.open = last.open;
            currentCandle.high = Math.max(currentCandle.high, last.high);
            currentCandle.low = Math.min(currentCandle.low, last.low);
        } else {
            candlesRef.setData(newCandles);
            if(candles2Ref) candles2Ref.setData(newCandles);
            lastCandleTime = last.time;
            currentCandle = {...last};
            targetPrice = last.close;
            priceVelocity = 0;
        }

        e20Ref.setData(data.ema20.map(x => ({time: toUnix(x.time), value: x.value})));
        e50Ref.setData(data.ema50.map(x => ({time: toUnix(x.time), value: x.value})));
        if(e20_2Ref) e20_2Ref.setData(data.ema20.map(x => ({time: toUnix(x.time), value: x.value})));
        if(e50_2Ref) e50_2Ref.setData(data.ema50.map(x => ({time: toUnix(x.time), value: x.value})));

        renderAnalysis(data.analysis);
        renderStats(data.stats);
        loadJournal();

        // Update chart tab too
        if(document.getElementById('tab-chart').classList.contains('active')){
            updateChartTabSignals(data.analysis);
        }

        // Auto check SL/TP untuk active trades
        try {
            await fetch('/api/check-trades', {method:'POST'});
        } catch(e){}
        
        // Update position lines on chart from active trades
        drawPositionLines();
        
        startSmoothSimulation();
    } catch(e){
        showToast('Gagal load market', 'error');
    }
}

// ===== RENDER ANALYSIS =====
function renderAnalysis(a){
    lastAnalysis = a;
    const box = document.getElementById('signalBox');
    const text = document.getElementById('signalText');

    box.className = 'signal-box ' + (a.signal === 'BUY' ? 'buy' : a.signal === 'SELL' ? 'sell' : 'wait');
    text.className = 'signal-text ' + (a.signal === 'BUY' ? 'buy' : a.signal === 'SELL' ? 'sell' : 'wait');
    text.textContent = a.signal;
    document.getElementById('score').textContent = a.score;
    document.getElementById('confidence').textContent = a.confidence + '%';
    document.getElementById('price').textContent = a.price;

    // Warnings
    const w = document.getElementById('warnings');
    w.innerHTML = (a.warnings || []).map(x => '<div style="padding:6px 12px;background:var(--red-bg);border-radius:8px;margin-bottom:4px;font-size:12px;color:var(--red)">' + x + '</div>').join('');

    // Reason
    const r = document.getElementById('reasonBox');
    r.innerHTML = '<ul style="padding-left:18px;font-size:13px;line-height:1.7">' +
        (a.reason || []).map(x => '<li>' + x + '</li>').join('') + '</ul>';

    const ai = document.getElementById('aiBox');
    if(ai){
        const m = a.ai_memory || {};
        const n = a.news_bias || {};
        ai.innerHTML = `
            <span class="badge badge-open">TF ${m.timeframe || currentTF}</span>
            <span>Learned trades: <b>${m.total || 0}</b></span>
            <span>BUY WR: <b>${m.buy_winrate ?? '-' }%</b></span>
            <span>SELL WR: <b>${m.sell_winrate ?? '-' }%</b></span>
            <span>Memory PNL: <b style="color:${(m.pnl_sum||0)>=0?'var(--green)':'var(--red)'}">$${m.pnl_sum || 0}</b></span>
            <span>News: <b>${n.label || 'NEUTRAL'}</b> (${n.score || 0})</span>
            <span>Grade: <b style="color:${a.quality_grade==='NO TRADE'?'var(--red)':'var(--gold)'}">${a.quality_grade || '-'}</b></span>
            <span>Risk factor: <b>${Math.round((a.risk_multiplier_ai || 0) * 100)}%</b></span>
        `;
        const c = a.ai_checklist || {};
        ai.innerHTML += '<div style="flex-basis:100%;margin-top:6px">' +
            Object.entries(c).map(([k,v]) => `<span class="badge ${v?'badge-win':'badge-loss'}" style="margin:2px">${v?'✅':'❌'} ${k.replaceAll('_',' ')}</span>`).join('') +
            '</div>';
    }

    // Fill risk calc
    document.getElementById('r_entry').value = a.entry || '';
    document.getElementById('r_sl').value = a.sl || '';
}

// ===== RENDER STATS =====
function renderStats(s){
    const grid = document.getElementById('statsGrid');
    grid.innerHTML = `
        <div class="kpi-box"><span class="val" style="color:var(--gold)">$${s.balance}</span><span class="lbl">Saldo</span></div>
        <div class="kpi-box"><span class="val">${s.winrate}%</span><span class="lbl">Winrate</span></div>
        <div class="kpi-box"><span class="val">${s.total_trades}</span><span class="lbl">Total Trade</span></div>
        <div class="kpi-box"><span class="val" style="color:${s.total_pnl>=0?'var(--green)':'var(--red)'}">$${s.total_pnl}</span><span class="lbl">Total PNL</span></div>
        <div class="kpi-box"><span class="val">${s.wins}/${s.losses}/${s.be}</span><span class="lbl">W/L/BE</span></div>
        <div class="kpi-box"><span class="val">${s.profit_factor}</span><span class="lbl">Profit Factor</span></div>
        <div class="kpi-box"><span class="val" style="color:${s.today_pnl>=0?'var(--green)':'var(--red)'}">$${s.today_pnl}</span><span class="lbl">PNL Hari Ini</span></div>
        <div class="kpi-box"><span class="val">${s.max_consecutive_loss}</span><span class="lbl">Max Loss Streak</span></div>
    `;
    document.getElementById('r_balance').value = s.balance;

    // Check winstreak for confetti
    if(s.max_consecutive_loss === 0 && s.wins >= 3){
        // Just triggered confetti if recently won
    }
}

// ===== LOAD JOURNAL =====
async function loadJournal(){
    try {
        let data = await fetch('/api/journal').then(r => r.json());
        allJournalData = data.rows;
        renderStats(data.stats);
        filterJournal();
    } catch(e){}
}

function filterJournal(){
    const txt = (document.getElementById('jFilterText').value || '').toLowerCase();
    const rs = document.getElementById('jFilterResult').value;
    const sd = document.getElementById('jFilterSide').value;
    let filtered = allJournalData;
    if(txt) filtered = filtered.filter(r => (r.symbol||'').toLowerCase().includes(txt) || (r.note||'').toLowerCase().includes(txt) || String(r.entry).includes(txt));
    if(rs) filtered = filtered.filter(r => (r.result||'').toUpperCase() === rs);
    if(sd) filtered = filtered.filter(r => (r.side||'').toUpperCase() === sd);
    journalPage = 1;
    renderJournalPage(filtered);
}

function renderJournalPage(data){
    const total = data.length;
    const pages = Math.ceil(total / PER_PAGE);
    if(journalPage > pages) journalPage = pages || 1;
    const start = (journalPage-1) * PER_PAGE;
    const pageData = data.slice(start, start + PER_PAGE);
    document.getElementById('jCount').textContent = total + ' trades · Page ' + journalPage + '/' + (pages||1);

    const html = '<table><tr><th onclick="sortJournal(0)">Date</th><th onclick="sortJournal(1)">TF</th><th onclick="sortJournal(2)">Side</th><th onclick="sortJournal(3)">Entry</th><th onclick="sortJournal(4)">SL</th><th onclick="sortJournal(5)">TP</th><th>Lot</th><th onclick="sortJournal(6)">Result</th><th onclick="sortJournal(7)">PNL</th><th>Saldo</th><th></th></tr>' +
        pageData.map(r => {
            const rs = (r.result||'').toUpperCase();
            const badge = rs === 'WIN' ? 'badge-win' : rs === 'LOSS' ? 'badge-loss' : rs === 'BE' ? 'badge-be' : 'badge-open';
            return '<tr><td>' + (r.created_at||'').slice(0,16) + '</td><td>' + (r.timeframe||'') + '</td><td>' + r.side + '</td><td>' + r.entry + '</td><td>' + (r.sl||'') + '</td><td>' + (r.tp||'') + '</td><td>' + (r.lot||'') + '</td><td><span class="badge ' + badge + '">' + rs + '</span></td><td style="color:' + (r.pnl>0?'var(--green)':r.pnl<0?'var(--red)':'') + '">' + (r.pnl||'') + '</td><td>' + (r.balance_after||'') + '</td><td><span class="btn btn-danger" style="padding:2px 8px;font-size:11px" onclick="deleteJournal(' + r.id + ')">✕</span></td></tr>';
        }).join('') + '</table>';

    document.getElementById('journalTable').innerHTML = html;

    // Pagination
    let p = '<button class="btn" onclick="journalPage--;loadJournal()" ' + (journalPage<=1?'disabled':'') + '>‹</button>';
    for(let i=Math.max(1,journalPage-2); i<=Math.min(pages,journalPage+2); i++){
        p += '<button class="btn ' + (i===journalPage?'btn-primary':'') + '" onclick="journalPage=' + i + ';loadJournal()">' + i + '</button>';
    }
    p += '<button class="btn" onclick="journalPage++;loadJournal()" ' + (journalPage>=pages?'disabled':'') + '>›</button>';
    document.getElementById('jPagination').innerHTML = p;
}

let sortCol = 0, sortDir = 1;
function sortJournal(col){
    if(sortCol === col) sortDir *= -1;
    else {sortCol = col; sortDir = 1;}
    const keys = ['created_at','timeframe','side','entry','sl','tp','result','pnl'];
    allJournalData.sort((a,b) => {
        let va = a[keys[col]]||'', vb = b[keys[col]]||'';
        if(col === 3 || col === 4 || col === 5 || col === 7) return sortDir * (parseFloat(va) - parseFloat(vb));
        return sortDir * String(va).localeCompare(String(vb));
    });
    filterJournal();
}

async function saveJournal(){
    const body = {
        timeframe: currentTF,
        side: document.getElementById('j_side').value,
        entry: document.getElementById('j_entry').value,
        sl: document.getElementById('j_sl').value,
        tp: document.getElementById('j_tp').value,
        lot: document.getElementById('j_lot').value,
        result: document.getElementById('j_result').value,
        pnl: document.getElementById('j_pnl').value,
        note: document.getElementById('j_note').value
    };
    await fetch('/api/journal', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    document.getElementById('j_pnl').value = '';
    document.getElementById('j_note').value = '';
    showToast('Journal tersimpan', 'success');
    loadJournal();
}

async function deleteJournal(id){
    if(!confirm('Hapus?')) return;
    await fetch('/api/journal/' + id, {method:'DELETE'});
    showToast('Dihapus', 'info');
    loadJournal();
}

function fillJournalFromSignal(){
    if(!lastAnalysis) return;
    document.getElementById('j_side').value = lastAnalysis.signal === 'SELL' ? 'SELL' : 'BUY';
    document.getElementById('j_entry').value = lastAnalysis.entry || '';
    document.getElementById('j_sl').value = lastAnalysis.sl || '';
    document.getElementById('j_tp').value = lastAnalysis.tp || '';
    showToast('Journal diisi dari signal', 'info');
}

// ===== RISK CALC =====
async function calcRisk(){
    const r = await fetch('/api/risk', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({
        balance: document.getElementById('r_balance').value,
        risk_percent: document.getElementById('r_risk').value,
        entry: document.getElementById('r_entry').value,
        sl: document.getElementById('r_sl').value
    })}).then(r => r.json());
    document.getElementById('riskOut').textContent = JSON.stringify(r, null, 2);
    if(r.suggested_lot) document.getElementById('j_lot').value = r.suggested_lot;
}

// ===== ACTIVE TRADES =====
async function saveActiveTrade(){
    await fetch('/api/active-trades', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({
        timeframe: currentTF,
        side: document.getElementById('at_side').value,
        entry: document.getElementById('at_entry').value,
        sl: document.getElementById('at_sl').value,
        tp: document.getElementById('at_tp').value,
        lot: document.getElementById('at_lot').value,
        note: document.getElementById('at_note').value
    })});
    document.getElementById('at_entry').value='';
    document.getElementById('at_sl').value='';
    document.getElementById('at_tp').value='';
    document.getElementById('at_lot').value='';
    document.getElementById('at_note').value='';
    showToast('Trade aktif tersimpan', 'success');
    loadActiveTrades();
}

async function loadActiveTrades(){
    try {
        const data = await fetch('/api/active-trades').then(r => r.json());
        const list = document.getElementById('activeTradesList');
        if(!data.rows || data.rows.length === 0){
            list.innerHTML = '<div style="padding:20px;text-align:center;color:var(--mut)">Belum ada trade aktif</div>';
            return;
        }
        list.innerHTML = '<table><tr><th>Date</th><th>Side</th><th>Entry</th><th>SL</th><th>TP</th><th>Lot</th><th>Note</th><th>Close</th></tr>' +
            data.rows.map(r => {
                const green = r.side === 'BUY' ? 'green' : 'red';
                return '<tr style="border-left:3px solid ' + green + '"><td>' + (r.created_at||'').slice(0,16) + '</td><td>' + r.side + '</td><td>' + r.entry + '</td><td>' + r.sl + '</td><td>' + r.tp + '</td><td>' + r.lot + '</td><td>' + (r.note||'') + '</td>' +
                '<td><input class="input" id="cp_' + r.id + '" placeholder="Harga" style="width:70px;padding:4px 6px;font-size:11px"> ' +
                '<span class="btn btn-danger" style="padding:2px 8px;font-size:11px" onclick="closeTrade(' + r.id + ')">✕ Close</span></td></tr>';
            }).join('') + '</table>';
    } catch(e){}
}

async function closeTrade(id){
    const cp = document.getElementById('cp_' + id);
    const price = parseFloat(cp ? cp.value : 0);
    if(!price || price <= 0){
        showToast('Masukkan close price terlebih dahulu', 'error');
        return;
    }
    if(!confirm('Close trade? PNL akan dihitung otomatis')) return;
    try {
        const r = await fetch('/api/active-trades/' + id + '/close', {
            method:'PUT',
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify({close_price: price})
        }).then(r => r.json());
        if(r.ok){
            showToast('Trade closed: ' + r.result + ' | PNL: $' + r.pnl.toFixed(2), r.result === 'WIN' ? 'success' : 'info');
            loadActiveTrades();
            loadJournal();
        } else {
            showToast(r.error || 'Gagal close', 'error');
        }
    } catch(e){
        showToast('Error close trade', 'error');
    }
}

// ===== SETTINGS =====
async function saveSettings(){
    await fetch('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({
        symbol: document.getElementById('set_symbol').value,
        twelvedata_api_key: document.getElementById('set_td').value,
        initial_balance: document.getElementById('set_balance').value,
        risk_percent: document.getElementById('set_risk').value,
        pip_value_per_lot: document.getElementById('set_pip').value,
        refresh_seconds: document.getElementById('set_refresh').value,
        theme: document.documentElement.dataset.theme
    })});
    refreshSec = parseInt(document.getElementById('set_refresh').value || 10);
    showToast('Settings tersimpan', 'success');
    startTimer();
    loadMarket();
}

async function changePass(){
    const f = new FormData();
    f.append('old_password', document.getElementById('oldp').value);
    f.append('new_password', document.getElementById('newp').value);
    const r = await fetch('/change-password', {method:'POST', body:f}).then(r => r.json());
    document.getElementById('msg').textContent = r.ok ? '✅ Password diganti' : '❌ ' + r.error;
}

// ===== ANALYTICS =====
let equityChartRef = null;
function renderAnalytics(){
    if(allJournalData.length === 0){
        document.getElementById('equityChart').innerHTML = '<div style="text-align:center;padding:40px;color:var(--mut)">Belum ada data journal</div>';
        document.getElementById('pieChart').innerHTML = '<div style="color:var(--mut)">Belum ada data</div>';
        document.getElementById('monthlyChart').innerHTML = '';
        document.getElementById('perfSummary').innerHTML = '';
        return;
    }

    // Equity curve
    const container = document.getElementById('equityChart');
    container.innerHTML = '';
    if(!equityChartRef){
        equityChartRef = LightweightCharts.createChart(container, {
            layout: {background:{color:'transparent'}, textColor: getComputedStyle(document.documentElement).getPropertyValue('--txt2').trim()},
            grid: {vertLines:{color:'#9992'}, horzLines:{color:'#9992'}},
            crosshair: {mode: 0},
            height: 280,
        });
    }

    const equitySeries = equityChartRef.addAreaSeries({
        color: 'rgba(52,211,153,0.3)',
        lineColor: '#34d399',
        lineWidth: 2,
        topColor: 'rgba(52,211,153,0.2)',
        bottomColor: 'rgba(52,211,153,0)',
    });

    // Cumulative PNL reversed (oldest first)
    const reversed = [...allJournalData].reverse();
    let cum = parseFloat(document.getElementById('r_balance').value || 1000) - (reversed.reduce((s,r) => s + parseFloat(r.pnl||0), 0));
    const eqData = reversed.map(r => {
        cum += parseFloat(r.pnl||0);
        return {time: toUnix(r.created_at), value: cum};
    }).filter(x => x.time > 0);
    if(eqData.length > 0){
        equitySeries.setData(eqData);
        equityChartRef.timeScale().fitContent();
    }

    // Pie chart
    const wins = allJournalData.filter(r => (r.result||'').toUpperCase() === 'WIN' || parseFloat(r.pnl||0) > 0).length;
    const losses = allJournalData.filter(r => (r.result||'').toUpperCase() === 'LOSS' || parseFloat(r.pnl||0) < 0).length;
    const be = allJournalData.filter(r => (r.result||'').toUpperCase() === 'BE' || parseFloat(r.pnl||0) === 0).length;
    const total = wins + losses + be;
    document.getElementById('pieChart').innerHTML = total === 0 ? '<div style="color:var(--mut)">Belum ada data</div>' :
        '<div style="width:180px;height:180px;border-radius:50%;position:relative;overflow:hidden;display:flex;align-items:center;justify-content:center;box-shadow:inset 0 0 0 3px var(--card)">' +
        '<div style="position:absolute;inset:0;border-radius:50%;background:conic-gradient(var(--green) 0deg ' + (wins/total*360) + 'deg, var(--red) ' + (wins/total*360) + 'deg ' + ((wins+losses)/total*360) + 'deg, var(--gold) ' + ((wins+losses)/total*360) + 'deg 360deg)"></div>' +
        '<div style="position:relative;background:var(--card);border-radius:50%;width:80px;height:80px;display:flex;flex-direction:column;align-items:center;justify-content:center;font-weight:700;font-size:16px">' + wins + '/' + losses + '<span style="font-size:11px;font-weight:400;color:var(--mut)">W/L</span></div></div>' +
        '<div style="margin-top:8px;font-size:12px;display:flex;gap:16px;justify-content:center"><span><span style="color:var(--green)">●</span> Win ' + wins + '</span><span><span style="color:var(--red)">●</span> Loss ' + losses + '</span><span><span style="color:var(--gold)">●</span> BE ' + be + '</span></div>';

    // Monthly chart
    const months = {};
    allJournalData.forEach(r => {
        const m = (r.created_at||'').slice(0,7);
        if(!m) return;
        months[m] = (months[m]||0) + parseFloat(r.pnl||0);
    });
    const mc = document.getElementById('monthlyChart');
    mc.innerHTML = Object.keys(months).length === 0 ? '<div style="color:var(--mut);text-align:center;padding:40px">Belum ada data</div>' :
        '<div style="display:flex;align-items:flex-end;gap:6px;height:200px;padding:10px 0">' +
        Object.entries(months).map(([m,v]) =>
            '<div style="flex:1;display:flex;flex-direction:column;align-items:center;height:100%;justify-content:flex-end">' +
            '<span style="font-size:10px;color:var(--mut);margin-bottom:2px">' + (v>0?'+':'') + v.toFixed(0) + '</span>' +
            '<div style="width:100%;height:' + Math.max(3, Math.abs(v) / Math.max(...Object.values(months).map(x=>Math.abs(x))) * 160) + 'px;background:' + (v>=0?'var(--green)':'var(--red)') + ';border-radius:4px 4px 0 0;transition:height .3s"></div>' +
            '<span style="font-size:9px;color:var(--mut);margin-top:4px;writing-mode:vertical-lr;text-orientation:mixed;height:30px;overflow:hidden;text-overflow:ellipsis">' + m.slice(5) + '</span></div>'
        ).join('') + '</div>';

    // Performance summary
    const stats = allJournalData.reduce((acc, r) => {
        acc.count++;
        const p = parseFloat(r.pnl||0);
        acc.totalPnl += p;
        if(p > 0) {acc.wins++; acc.grossProfit += p}
        else if(p < 0) {acc.losses++; acc.grossLoss += Math.abs(p)}
        else acc.be++;
        return acc;
    }, {count:0, wins:0, losses:0, be:0, totalPnl:0, grossProfit:0, grossLoss:0});
    document.getElementById('perfSummary').innerHTML =
        '<div class="kpi-grid" style="grid-template-columns:repeat(auto-fill,minmax(160px,1fr))">' +
        '<div class="kpi-box"><span class="val">' + stats.count + '</span><span class="lbl">Total</span></div>' +
        '<div class="kpi-box"><span class="val">' + (stats.wins+stats.losses > 0 ? (stats.wins/(stats.wins+stats.losses)*100).toFixed(1) + '%' : '-') + '</span><span class="lbl">Winrate</span></div>' +
        '<div class="kpi-box"><span class="val" style="color:' + (stats.totalPnl>=0?'var(--green)':'var(--red)') + '">$' + stats.totalPnl.toFixed(2) + '</span><span class="lbl">Net PNL</span></div>' +
        '<div class="kpi-box"><span class="val">' + (stats.grossLoss > 0 ? (stats.grossProfit/stats.grossLoss).toFixed(2) : '∞') + '</span><span class="lbl">Profit Factor</span></div>' +
        '<div class="kpi-box"><span class="val">' + (stats.count > 0 ? (stats.totalPnl/stats.count).toFixed(2) : '0') + '</span><span class="lbl">Avg PNL</span></div>' +
        '</div>';
}

// ===== CHART TAB =====
function loadChartTab(){
    // Delay dengan setTimeout untuk memastikan browser sudah me-render tab (display:block)
    // requestAnimationFrame saja tidak cukup karena layout belum dihitung
    setTimeout(() => {
        // Fix: buat chart saat tab pertama kali dibuka
        if(!chart2Ref) {
            const container = document.getElementById('chart2');
            // Pastikan container memiliki dimensi
            console.log('Chart2 container:', container.offsetWidth, container.offsetHeight);
            chart2Ref = LightweightCharts.createChart(container, {
                layout: {background:{color:'transparent'}, textColor: getComputedStyle(document.documentElement).getPropertyValue('--txt2').trim()},
                grid: {vertLines:{color:'#9992'}, horzLines:{color:'#9992'}},
                height: 480,
            });
            candles2Ref = chart2Ref.addCandlestickSeries({upColor:'#059669', downColor:'#e11d48', borderVisible:false, wickUpColor:'#059669', wickDownColor:'#e11d48'});
            e20_2Ref = chart2Ref.addLineSeries({color:'#ca8a04', lineWidth:2});
            e50_2Ref = chart2Ref.addLineSeries({color:'#2563eb', lineWidth:2});
            // Load market data setelah chart terbuat
            loadMarket();
        } else {
            loadMarket();
        }
        // Force chart resize saat tab terbuka - pakai setTimeout lagi untuk memastikan chart sudah siap
        setTimeout(() => {
            if(chart2Ref) {
                chart2Ref.timeScale().fitContent();
            }
        }, 100);
    }, 100);
}

function updateChartTabSignals(a){
    const el = document.getElementById('signal2');
    el.textContent = a.signal + ' | Score: ' + a.score + '%';
    el.style.background = a.signal === 'BUY' ? 'var(--green-bg)' : a.signal === 'SELL' ? 'var(--red-bg)' : 'var(--gold-bg)';
    el.style.color = a.signal === 'BUY' ? 'var(--green)' : a.signal === 'SELL' ? 'var(--red)' : 'var(--gold)';
    document.getElementById('score2').textContent = a.score;
    document.getElementById('rsi2').textContent = a.rsi;
    document.getElementById('entry2').textContent = a.entry;
    document.getElementById('sl2').textContent = a.sl || '-';
    document.getElementById('tp2').textContent = a.tp || '-';
    document.getElementById('reason2').innerHTML = (a.reason || []).map(x => '<span style="font-size:12px;display:inline-block;margin:2px 4px 2px 0;padding:2px 8px;border-radius:6px;background:var(--card2)">' + x + '</span>').join('');
}

// ===== SMOOTH CANDLE =====
function startSmoothSimulation(){
    if(smoothTimer) clearInterval(smoothTimer);
    smoothTimer = setInterval(() => {
        if(!currentCandle) return;
        const delta = (targetPrice - currentCandle.close) * 0.15;
        const noise = (Math.random() - 0.5) * 0.35;
        priceVelocity = priceVelocity * 0.8 + (delta + noise) * 0.2;
        currentCandle.close += priceVelocity;
        currentCandle.high = Math.max(currentCandle.high, currentCandle.close);
        currentCandle.low = Math.min(currentCandle.low, currentCandle.close);
        candlesRef.update(currentCandle);
        if(candles2Ref) candles2Ref.update(currentCandle);
    }, 200);
}

// ===== TIMER =====
function startTimer(){
    if(timer) clearInterval(timer);
    timer = setInterval(() => {
        if(document.getElementById('live').checked) loadMarket();
    }, Math.max(5, refreshSec) * 1000);
}

// ===== CANDLE TYPE SWITCHING =====
let candleType = 'candle';
let rawCandles = [];
function heikinAshi(candles) {
    if (!candles.length) return [];
    const ha = [];
    let prevHa = { open: candles[0].open, close: candles[0].close };
    for (let i = 0; i < candles.length; i++) {
        const c = candles[i];
        const haClose = (c.open + c.high + c.low + c.close) / 4;
        const haOpen = i === 0 ? c.open : (prevHa.open + prevHa.close) / 2;
        const haHigh = Math.max(c.high, haOpen, haClose);
        const haLow = Math.min(c.low, haOpen, haClose);
        ha.push({ time: c.time, open: haOpen, high: haHigh, low: haLow, close: haClose });
        prevHa = { open: haOpen, close: haClose };
    }
    return ha;
}
function changeCandleType(type) {
    candleType = type;
    if (!rawCandles.length) return;
    let data = type === 'heiken' ? heikinAshi(rawCandles) : rawCandles;

    // Remove extra series from chart
    chartRef.removeSeries(candlesRef);
    e20Ref.setVisible(true);
    e50Ref.setVisible(true);

    if (type === 'line') {
        e20Ref.setVisible(false);
        e50Ref.setVisible(false);
        candlesRef = chartRef.addLineSeries({ color: '#2563eb', lineWidth: 2, priceFormat: { type: 'price' } });
        candlesRef.setData(data.map(x => ({ time: x.time, value: x.close })));
    } else if (type === 'area') {
        e20Ref.setVisible(false);
        e50Ref.setVisible(false);
        candlesRef = chartRef.addAreaSeries({ color: 'rgba(37,99,235,0.2)', lineColor: '#2563eb', lineWidth: 2, topColor: 'rgba(37,99,235,0.2)', bottomColor: 'rgba(37,99,235,0)' });
        candlesRef.setData(data.map(x => ({ time: x.time, value: x.close })));
    } else {
        candleType = type;
        const isHA = type === 'heiken';
        candlesRef = chartRef.addCandlestickSeries({ upColor: '#059669', downColor: '#e11d48', borderVisible: false, wickUpColor: '#059669', wickDownColor: '#e11d48' });
        candlesRef.setData(data);
    }
    if (type !== 'candle' && type !== 'heiken') {
        chartRef.timeScale().fitContent();
    }
}

// ===== POSITION LINES ON CHART =====
let posLineRefs = [];
async function drawPositionLines(){
    // Hapus garis lama
    posLineRefs.forEach(ref => {
        try { chartRef.removeSeries(ref); } catch(e){}
    });
    posLineRefs = [];
    
    try {
        const data = await fetch('/api/active-trades').then(r => r.json());
        if(!data.rows || data.rows.length === 0) return;
        
        data.rows.forEach(r => {
            const isBuy = r.side === 'BUY';
            const entryColor = isBuy ? '#059669' : '#dc2626';
            const slColor = '#ea580c';
            const tpColor = '#2563eb';
            
            // Entry line
            const entryLine = chartRef.addLineSeries({
                color: entryColor,
                lineWidth: 2,
                lineStyle: 2, // dashed
                lastValueVisible: true,
                priceLineVisible: false,
            });
            const now = Math.floor(Date.now() / 1000);
            entryLine.setData([
                {time: now - 7200, value: r.entry},
                {time: now, value: r.entry}
            ]);
            posLineRefs.push(entryLine);
            
            // SL line
            if(r.sl){
                const slLine = chartRef.addLineSeries({
                    color: slColor,
                    lineWidth: 1,
                    lineStyle: 2,
                    lastValueVisible: true,
                    priceLineVisible: false,
                });
                slLine.setData([
                    {time: now - 7200, value: r.sl},
                    {time: now, value: r.sl}
                ]);
                posLineRefs.push(slLine);
            }
            
            // TP line
            if(r.tp){
                const tpLine = chartRef.addLineSeries({
                    color: tpColor,
                    lineWidth: 1,
                    lineStyle: 2,
                    lastValueVisible: true,
                    priceLineVisible: false,
                });
                tpLine.setData([
                    {time: now - 7200, value: r.tp},
                    {time: now, value: r.tp}
                ]);
                posLineRefs.push(tpLine);
            }
        });
    } catch(e){}
}

// ===== IDR CONVERTER =====
let idrCache = { rate: 15500, time: 0 };
async function getIDR(usd) {
    if (Date.now() - idrCache.time > 60000) {
        try { const r = await fetch('/api/convert?usd=1').then(r => r.json()); idrCache = { rate: r.rate, time: Date.now() }; } catch(e) {}
    }
    return 'Rp' + (usd * idrCache.rate).toLocaleString('id-ID', {minimumFractionDigits:0,maximumFractionDigits:0});
}

// ===== INIT =====
function init(){
    // Dashboard chart
    chartRef = LightweightCharts.createChart(document.getElementById('chart'), {
        layout: {background:{color:'transparent'}, textColor: getComputedStyle(document.documentElement).getPropertyValue('--txt2').trim()},
        grid: {vertLines:{color:'#9992'}, horzLines:{color:'#9992'}},
        height: 480,
    });
    candlesRef = chartRef.addCandlestickSeries({upColor:'#059669', downColor:'#e11d48', borderVisible:false, wickUpColor:'#059669', wickDownColor:'#e11d48'});
    e20Ref = chartRef.addLineSeries({color:'#ca8a04', lineWidth:2});
    e50Ref = chartRef.addLineSeries({color:'#2563eb', lineWidth:2});

    // Theme button
    document.getElementById('themeBtn').textContent = '{{theme}}' === 'dark' ? '☀️' : '🌙';

    // Load settings
    fetch('/api/settings').then(r => r.json()).then(s => {
        document.getElementById('set_symbol').value = s.symbol;
        document.getElementById('set_td').value = s.twelvedata_api_key;
        document.getElementById('set_balance').value = s.initial_balance;
        document.getElementById('set_risk').value = s.risk_percent;
        document.getElementById('set_pip').value = s.pip_value_per_lot;
        document.getElementById('set_refresh').value = s.refresh_seconds;
        idrCache.rate = parseFloat(s.exchange_rate_usd_idr || 15500);
        refreshSec = parseInt(s.refresh_seconds || 10);
    });

    loadMarket();
    loadActiveTrades();
    startTimer();
    window.addEventListener('resize', resizeCharts);
    window.addEventListener('orientationchange', () => setTimeout(resizeCharts, 250));
    setTimeout(resizeCharts, 200);
}

init();
</script>
</body>
</html>
'''


if __name__ == "__main__":
    init_db()
    print("\n" + APP)
    print("URL   : http://127.0.0.1:4000")
    print("Login : admin / admin123")
    app.run(host="127.0.0.1", port=4000, debug=True)