import os
import time
import json
import math
import threading
import requests
import psycopg2
from datetime import datetime
from collections import defaultdict, deque
from pybit.unified_trading import WebSocket, HTTP
from fastapi import FastAPI

# ==================== НАСТРОЙКИ ТЕЛЕГРАМ ====================
TELEGRAM_BOT_TOKEN = "8828927799:AAGQf8_YwE5rkdLzrPGNnZ2d5zsp4Lrx_qg"
TELEGRAM_CHAT_ID = "1190982420"

# ==================== НАСТРОЙКИ BYBIT & БАЗЫ ДАННЫХ ====================
DATABASE_URL = os.getenv("DATABASE_URL")
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET", "")

# ==================== НАСТРОЙКИ СТРАТЕГИИ ====================
CATEGORY = "linear"            # Фьючерсы USDT (Mainnet)
DEFAULT_TOP_COINS_LIMIT = 20   # Оптимизировано до TOP-20 для защиты CPU на Render
DEFAULT_INITIAL_BALANCE = 20.0 # Базовый депозит
DEFAULT_LEVERAGE = 5           # Кредитное плечо (5x)
MAX_DRAWDOWN_PCT = 10.0        # Остановка при потере -10%

SLIPPAGE_PCT = 0.02            # Учет проскальзывания 0.02%
MAX_CONSECUTIVE_LOSSES = 5     # Жесткий авто-бан после 5 убытков подряд

PERFORMANCE_WINDOW_SEC = 1800  # Окно анализа монеты (30 минут)
MAX_LOSSES_IN_WINDOW = 3       # Макс. убытков за 30 минут -> Временный бан
MAX_WINDOW_PNL_LOSS = -0.30    # Макс. суммарный пролив монеты за 30 мин (-0.30%)

VWAP_WINDOW_SEC = 900          # Окно расчета микро-тренда VWAP (15 минут)
TAPE_DELTA_WINDOW_SEC = 10     # Окно анализа ленты перед входом (10 секунд)
MAX_IMBALANCE_RATIO = 0.30     # Макс. допустимый объем противника к стенке (30%)

POSITION_TIMEOUT_SEC = 45      # Эвакуация через 45 секунд
COOLDOWN_SEC = 5               # Пауза между сделками 5 секунд
EAT_THRESHOLD_PCT = 60.0       # Выход при разъедании на 60%

TAKE_PROFIT_PCT = 0.28         # Тейк-профит (+0.28%)
STOP_LOSS_PCT = 0.18           # Стоп-лосс (-0.18%)
BREAKEVEN_TRIGGER_PCT = 0.12   # Перенос в БУ при +0.12%
TAKER_FEE_PCT = 0.055 * 2      # Комиссия биржи (~0.11%)

PROXIMITY_PCT = 0.22           # Дистанция до стенки (<= 0.22%)
MIN_WALL_LIFETIME_SEC = 0.0    # Мгновенный вход
MAX_SPREAD_PCT = 0.06          # Спред (<= 0.06%)
MIN_TRADES_PER_MIN = 10        # Минимум 10 сделок в минуту

REST_5_PCT_SEC = 900           
LOG_INTERVAL_SEC = 15           
TG_UPDATE_INTERVAL_SEC = 3.5   

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE_PATH = os.path.join(SCRIPT_DIR, "trade_log.txt")

# ==================== ОЧЕРЕДЬ ТЕЛЕГРАМ ====================
tg_queue = deque()
tg_last_sent_time = 0.0

def tg_worker():
    global tg_last_sent_time
    while True:
        try:
            if tg_queue:
                now = time.time()
                if now - tg_last_sent_time >= 1.05:
                    task = tg_queue.popleft()
                    action = task.get("action")
                    payload = task.get("payload")
                    
                    if action == "send":
                        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
                        res = requests.post(url, json=payload, timeout=5).json()
                        if res.get("ok"):
                            tg_last_sent_time = time.time()
                            cb = task.get("callback")
                            if cb:
                                cb(res.get("result", {}).get("message_id"))
                        elif res.get("error_code") == 429:
                            retry_after = res.get("parameters", {}).get("retry_after", 5)
                            time.sleep(retry_after)
                            tg_queue.appendleft(task)
                            
                    elif action == "update":
                        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
                        res = requests.post(url, json=payload, timeout=5).json()
                        if res.get("ok"):
                            tg_last_sent_time = time.time()
                        elif res.get("error_code") == 429:
                            retry_after = res.get("parameters", {}).get("retry_after", 5)
                            time.sleep(retry_after)

            time.sleep(0.1)
        except Exception as e:
            print(f"⚠️ [TG WORKER ERROR] {e}")
            time.sleep(2)

threading.Thread(target=tg_worker, daemon=True).start()

def send_tg_message_async(text, reply_markup=None, callback=None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    payload = {
        "chat_id": TELEGRAM_CHAT_ID, 
        "text": text, 
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    tg_queue.append({"action": "send", "payload": payload, "callback": callback})

def update_tg_message_async(message_id, text, reply_markup=None):
    if not message_id or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "message_id": message_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    tg_queue.append({"action": "update", "payload": payload})

def make_progress_bar(elapsed_sec, total_sec=45, length=10):
    pct = min(1.0, elapsed_sec / total_sec)
    filled_length = int(length * pct)
    bar = "🟩" * filled_length + "░" * (length - filled_length)
    pct_digits = int(pct * 100)
    mins, secs = divmod(int(elapsed_sec), 60)
    total_mins, total_secs = divmod(total_sec, 60)
    time_str = f"{mins:02d}:{secs:02d} / {total_mins:02d}:{total_secs:02d}"
    warning_suffix = " ⚡ *Скоро эвакуация!*" if pct >= 0.75 else ""
    return f"`{bar}` *{pct_digits}%* ({time_str}){warning_suffix}"

class LeverageRealBot:
    def __init__(self):
        print("⚡ Инициализация РЕАЛЬНОГО БОТА (API BYBIT) с защитой 24/7...")
        
        self.lock = threading.Lock()
        
        self.http_client = HTTP(
            testnet=False,
            api_key=BYBIT_API_KEY,
            api_secret=BYBIT_API_SECRET,
        )
        self.symbol_rules = {}
        
        self.in_position = False
        self.active_symbol = None
        self.position_side = None
        
        self.leverage = DEFAULT_LEVERAGE
        self.top_coins_limit = DEFAULT_TOP_COINS_LIMIT
        self.session_start_balance = DEFAULT_INITIAL_BALANCE
        self.current_balance = DEFAULT_INITIAL_BALANCE
        self.wins_count = 0
        self.losses_count = 0
        self.manual_paused = False
        
        self.user_blacklist = set()
        self.consecutive_losses = defaultdict(int)
        self.coin_trade_history = defaultdict(list)
        self.awaiting_input_action = None
        
        self._init_db()
        self._load_state()
        self._fetch_instrument_rules()

        self.ws_client = None
        self.targets = self._get_top_mainnet_symbols()
        self.trade_history = defaultdict(deque)

        self.entry_price = 0.0
        self.wall_price = 0.0
        self.initial_wall_size = 0.0
        self.entry_time = 0.0
        self.tp_price = 0.0
        self.sl_price = 0.0
        self.is_breakeven_set = False
        self.last_close_time = 0
        self.last_log_time = time.time()
        self.last_ws_data_time = time.time()
        self.last_tg_update_time = 0.0
        self.active_tg_msg_id = None
        self.max_seen_wall = {"symbol": "", "usd": 0}
        self.is_stopped = False

        self.wall_tracker = {}
        self.pause_until = 0
        self.triggered_5_pct_pause = False

    def _fetch_instrument_rules(self):
        try:
            res = self.http_client.get_instruments_info(category=CATEGORY)
            if res.get("retCode") == 0:
                for item in res["result"]["list"]:
                    self.symbol_rules[item["symbol"]] = {
                        "qtyStep": float(item["lotSizeFilter"]["qtyStep"]),
                        "tickSize": float(item["priceFilter"]["tickSize"])
                    }
            print(f"📦 [BYBIT] Загружены торговые правила для {len(self.symbol_rules)} пар.")
        except Exception as e:
            print(f"❌ [BYBIT ERROR] Ошибка загрузки спецификаций: {e}")

    def _normalize_qty(self, symbol, raw_qty):
        step = self.symbol_rules.get(symbol, {}).get("qtyStep", 0.001)
        precision = 0
        if step < 1:
            step_str = str(step)
            if 'e' in step_str.lower():
                precision = int(step_str.lower().split('e-')[1])
            else:
                precision = len(step_str.split('.')[1])
        
        normalized = math.floor(raw_qty / step) * step
        if precision > 0:
            return f"{normalized:.{precision}f}"
        return str(int(normalized))

    def _get_db_connection(self):
        if not DATABASE_URL:
            return None
        return psycopg2.connect(DATABASE_URL)

    def _init_db(self):
        try:
            conn = self._get_db_connection()
            if not conn:
                print("⚠️ DATABASE_URL не найден, бот работает во временной памяти.")
                return
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_state (
                    id INT PRIMARY KEY,
                    data JSONB NOT NULL
                );
            """)
            conn.commit()
            cur.close()
            conn.close()
            print("🗄 [POSTGRES] Таблица 'bot_state' успешно проверена/создана в aura-db!")
        except Exception as e:
            print(f"❌ [POSTGRES ERROR] Ошибка БД: {e}")

    def _save_state(self):
        data = {
            "current_balance": self.current_balance,
            "session_start_balance": self.session_start_balance,
            "wins_count": self.wins_count,
            "losses_count": self.losses_count,
            "leverage": self.leverage,
            "top_coins_limit": self.top_coins_limit,
            "user_blacklist": list(self.user_blacklist)
        }
        try:
            conn = self._get_db_connection()
            if not conn: return
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO bot_state (id, data) 
                VALUES (1, %s) 
                ON CONFLICT (id) 
                DO UPDATE SET data = EXCLUDED.data;
            """, (json.dumps(data),))
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            print(f"❌ [POSTGRES ERROR] Ошибка сохранения: {e}")

    def _load_state(self):
        try:
            conn = self._get_db_connection()
            if not conn: return
            cur = conn.cursor()
            cur.execute("SELECT data FROM bot_state WHERE id = 1;")
            row = cur.fetchone()
            cur.close()
            conn.close()

            if row:
                data = row[0] if isinstance(row[0], dict) else json.loads(row[0])
                self.current_balance = data.get("current_balance", DEFAULT_INITIAL_BALANCE)
                self.session_start_balance = data.get("session_start_balance", DEFAULT_INITIAL_BALANCE)
                self.wins_count = data.get("wins_count", 0)
                self.losses_count = data.get("losses_count", 0)
                self.leverage = data.get("leverage", DEFAULT_LEVERAGE)
                self.top_coins_limit = data.get("top_coins_limit", DEFAULT_TOP_COINS_LIMIT)
                self.user_blacklist = set(data.get("user_blacklist", []))
                print(f"📦 [POSTGRES] Баланс БД: ${self.current_balance:.2f} | Плечо: {self.leverage}x")
            else:
                self._save_state()
        except Exception as e:
            print(f"⚠️ [POSTGRES ERROR] Ошибка чтения из aura-db: {e}")

    def _get_top_mainnet_symbols(self):
        url = "https://api.bybit.com/v5/market/tickers?category=linear"
        for attempt in range(3):
            try:
                res = requests.get(url, timeout=10)
                if res.status_code == 429:
                    time.sleep(5)
                    continue
                data = res.json()
                tickers = data.get("result", {}).get("list", [])
                
                usdt_tickers = [t for t in tickers if t.get("symbol", "").endswith("USDT")]
                usdt_tickers.sort(key=lambda x: float(x.get("turnover24h", 0)), reverse=True)
                
                targets = {}
                EXPLICIT_BAN = ["BTCUSDT", "ETHUSDT", "USDCUSDT", "USDEUSDT", "FDUSDUSDT"]

                for t in usdt_tickers:
                    symbol = t["symbol"]
                    if self.in_position and self.active_symbol == symbol:
                        turnover = float(t.get("turnover24h", 0))
                        wall_threshold = 250_000 if symbol == "SOLUSDT" else max(100_000, round(turnover * 0.0008, -3))
                        targets[symbol] = wall_threshold
                        continue

                    if symbol in EXPLICIT_BAN or symbol in self.user_blacklist or "XAU" in symbol or "XAG" in symbol or "GOLD" in symbol:
                        continue  

                    turnover = float(t.get("turnover24h", 0))
                    wall_threshold = 250_000 if symbol == "SOLUSDT" else max(100_000, round(turnover * 0.0008, -3))
                    targets[symbol] = wall_threshold

                    if len(targets) >= self.top_coins_limit:
                        break

                return targets
            except Exception:
                time.sleep(2)
        return {"SOLUSDT": 250000, "XRPUSDT": 100000}

    def hard_reconnect_websocket(self):
        with self.lock:
            print("\n🔄 [HARD RECONNECT] Плавное пересоздание WebSocket сокетов Bybit...")
            try:
                if self.ws_client:
                    self.ws_client._exit()
            except Exception:
                pass
            
            time.sleep(2)
            self.targets = self._get_top_mainnet_symbols()
            self.wall_tracker.clear()
            
            new_ws = WebSocket(testnet=False, channel_type=CATEGORY, ping_interval=20, ping_timeout=10)
            self.ws_client = new_ws

            count = 0
            for symbol in list(self.targets.keys()):
                try:
                    new_ws.orderbook_stream(depth=50, symbol=symbol, callback=self.on_orderbook_update)
                    new_ws.trade_stream(symbol=symbol, callback=self.on_public_trade_update)
                    count += 1
                    time.sleep(0.2) # Важно: Задержка 200мс между парами для защиты от ping/pong timeout
                except Exception as e:
                    print(f"⚠️ Ошибка подписки на {symbol}: {e}")
            
            self.last_ws_data_time = time.time()
            print(f"✅ [HARD RECONNECT] Подписано {count} пар без перегрузки сокетов!\n")

    def on_public_trade_update(self, message):
        self.last_ws_data_time = time.time()
        symbol = message.get("topic", "").split(".")[-1]
        if symbol in self.user_blacklist or symbol not in self.targets:
            return
        now = time.time()
        trades = message.get("data", [])
        for tr in trades:
            price = float(tr.get("p", 0))
            vol = float(tr.get("v", 0))
            side = tr.get("S", "")
            self.trade_history[symbol].append((now, price, vol, side))

        while self.trade_history[symbol] and self.trade_history[symbol][0][0] < now - VWAP_WINDOW_SEC:
            self.trade_history[symbol].popleft()

    def get_recent_trade_count(self, symbol):
        now = time.time()
        return sum(1 for tr in self.trade_history[symbol] if tr[0] >= now - 60)

    def calculate_vwap_15m(self, symbol):
        now = time.time()
        pv_sum = 0.0
        vol_sum = 0.0
        for ts, price, vol, _ in self.trade_history[symbol]:
            if ts >= now - VWAP_WINDOW_SEC:
                pv_sum += price * vol
                vol_sum += vol
        return (pv_sum / vol_sum) if vol_sum > 0 else None

    def check_tape_aggressors_usd(self, symbol, side_to_open, wall_size_usd):
        now = time.time()
        opposite_side = "Sell" if side_to_open == "Buy" else "Buy"
        aggressor_vol_usd = 0.0
        for ts, price, vol, side in self.trade_history[symbol]:
            if ts >= now - TAPE_DELTA_WINDOW_SEC and side == opposite_side:
                aggressor_vol_usd += price * vol
        if aggressor_vol_usd > (wall_size_usd * MAX_IMBALANCE_RATIO):
            return False
        return True

    def is_coin_failing_recently(self, symbol):
        now = time.time()
        recent_pnl = [pnl for ts, pnl in self.coin_trade_history[symbol] if ts >= now - PERFORMANCE_WINDOW_SEC]
        if len(recent_pnl) >= 3:
            recent_losses = [pnl for pnl in recent_pnl if pnl < 0]
            sum_pnl = sum(recent_pnl)
            if len(recent_losses) >= MAX_LOSSES_IN_WINDOW and sum_pnl <= MAX_WINDOW_PNL_LOSS:
                return True, len(recent_losses), sum_pnl
        return False, 0, 0.0

    def get_main_menu_keyboard(self):
        pause_btn_text = "▶️ СНЯТЬ ПАУЗУ" if self.manual_paused else "⏸️ ПАУЗА БОТА"
        return {
            "inline_keyboard": [
                [{"text": "🛑 ЗАКРЫТЬ СЕЙЧАС", "callback_data": "close_now"}],
                [{"text": pause_btn_text, "callback_data": "toggle_pause"}, {"text": "📊 СТАТИСТИКА", "callback_data": "show_stats"}],
                [{"text": "⚙️ НАСТРОЙКИ", "callback_data": "menu_settings"}, {"text": "⛔ ЧЕРНЫЙ СПИСОК", "callback_data": "menu_blacklist"}],
                [{"text": "⚡ ПРОПУСТИТЬ ПАУЗУ", "callback_data": "skip_rest"}, {"text": "🔄 ПЕРЕЗАГРУЗИТЬ", "callback_data": "reboot_bot"}]
            ]
        }

    def _generate_open_card_text(self, elapsed_sec):
        position_usd = self.current_balance * self.leverage
        side_icon = "🟢 LONG" if self.position_side == "Buy" else "🔴 SHORT"
        bar_text = make_progress_bar(elapsed_sec, POSITION_TIMEOUT_SEC)
        be_status = " 🛡️ *(SL в Безубытке)*" if self.is_breakeven_set else ""

        return (
            f"🎯 *ВХОД В СДЕЛКУ* — `{self.active_symbol}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 Направление: `{side_icon}` ({self.leverage}x)\n"
            f"💵 Вход по цене: `{self.entry_price}` USDT\n"
            f"🧱 Плотность: `${self.entry_price * self.initial_wall_size:,.0f}`\n"
            f"💼 Объём позиции: `${position_usd:.2f}`\n\n"
            f"🎯 *TP:* `{self.tp_price}`  |  🛑 *SL:* `{self.sl_price}`{be_status}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⏱️ *Быстрый тайм-аут (45 сек):*\n"
            f"{bar_text}"
        )

    def on_orderbook_update(self, message):
        now = time.time()
        self.last_ws_data_time = now

        if self.is_stopped or self.manual_paused or now < self.pause_until:
            return

        symbol = message.get("topic", "").split(".")[-1]
        if symbol in self.user_blacklist or symbol not in self.targets:
            return

        is_bad, losses_cnt, sum_pnl = self.is_coin_failing_recently(symbol)
        if is_bad:
            self.user_blacklist.add(symbol)
            self._save_state()
            self.targets = self._get_top_mainnet_symbols()
            send_tg_message_async(f"🛡️ `{symbol}` добавлена в бан за убытки.")
            return

        data = message.get("data", {})
        bids = {float(p): float(s) for p, s in data.get("b", [])}
        asks = {float(p): float(s) for p, s in data.get("a", [])}

        if not bids or not asks:
            return

        best_bid = max(bids.keys())
        best_ask = min(asks.keys())

        if self.in_position:
            if self.active_symbol == symbol:
                current_wall_map = bids if self.position_side == "Buy" else asks
                current_wall_size = current_wall_map.get(self.wall_price, 0.0)

                eaten_pct = ((self.initial_wall_size - current_wall_size) / self.initial_wall_size) * 100
                current_price = best_bid if self.position_side == "Sell" else best_ask
                elapsed_time = now - self.entry_time

                if self.position_side == "Buy":
                    current_pnl_pct = ((current_price - self.entry_price) / self.entry_price) * 100
                else:
                    current_pnl_pct = ((self.entry_price - current_price) / self.entry_price) * 100

                if current_pnl_pct >= BREAKEVEN_TRIGGER_PCT and not self.is_breakeven_set:
                    self.sl_price = self.entry_price
                    self.is_breakeven_set = True

                if now - self.last_tg_update_time >= TG_UPDATE_INTERVAL_SEC and self.active_tg_msg_id:
                    updated_text = self._generate_open_card_text(elapsed_time)
                    update_tg_message_async(self.active_tg_msg_id, updated_text, self.get_main_menu_keyboard())
                    self.last_tg_update_time = now

                if elapsed_time >= POSITION_TIMEOUT_SEC:
                    if current_pnl_pct >= 0.15:
                        self.close_real_position("Быстрый сброс в профит (45 сек)", current_price)
                        return
                    elif elapsed_time >= 70:
                        self.close_real_position("Тайм-аут без движения (70 сек)", current_price)
                        return

                if self.position_side == "Buy":
                    if current_price >= self.tp_price:
                        self.close_real_position(f"Take-Profit (+{TAKE_PROFIT_PCT}%)", current_price)
                        return
                    elif current_price <= self.sl_price:
                        self.close_real_position(f"Stop-Loss / BU ({self.sl_price})", current_price)
                        return
                else:
                    if current_price <= self.tp_price:
                        self.close_real_position(f"Take-Profit (+{TAKE_PROFIT_PCT}%)", current_price)
                        return
                    elif current_price >= self.sl_price:
                        self.close_real_position(f"Stop-Loss / BU ({self.sl_price})", current_price)
                        return

                if eaten_pct >= EAT_THRESHOLD_PCT:
                    if current_pnl_pct < 0.15:
                        reason = f"Стенку разъели на {eaten_pct:.1f}%"
                        self.close_real_position(reason, current_price)
            return

        wall_threshold_usd = self.targets[symbol]
        spread_pct = ((best_ask - best_bid) / best_bid) * 100
        if spread_pct > MAX_SPREAD_PCT: return

        max_bid = max([p * s for p, s in bids.items()], default=0)
        max_ask = max([p * s for p, s in asks.items()], default=0)
        current_max = max(max_bid, max_ask)

        if now - self.last_log_time > LOG_INTERVAL_SEC:
            status_str = "ПАУЗА" if self.manual_paused else (f"В ПОЗИЦИИ [{self.active_symbol}]" if self.in_position else f"ПОИСК СТЕНОК (Депо: ${self.current_balance:.2f})")
            print(f"📡 [PULSE] Сканирование {symbol}... - Стенка: ${current_max:,.0f} - {status_str}")
            self.last_log_time = now

        if now - self.last_close_time < COOLDOWN_SEC: return

        recent_trades = self.get_recent_trade_count(symbol)
        vwap_15m = self.calculate_vwap_15m(symbol)

        for price, size in bids.items():
            wall_usd = price * size
            if wall_usd >= wall_threshold_usd:
                dist_pct = ((best_bid - price) / best_bid) * 100
                if dist_pct <= PROXIMITY_PCT:
                    if recent_trades < MIN_TRADES_PER_MIN or (vwap_15m and best_bid < vwap_15m) or not self.check_tape_aggressors_usd(symbol, "Buy", wall_usd):
                        continue
                    key = (symbol, "Buy", price)
                    if key not in self.wall_tracker: self.wall_tracker[key] = now
                    elif now - self.wall_tracker[key] >= MIN_WALL_LIFETIME_SEC:
                        self.open_real_position(symbol, "Buy", price, size)
                        self.wall_tracker.clear()
                        return

        for price, size in asks.items():
            wall_usd = price * size
            if wall_usd >= wall_threshold_usd:
                dist_pct = ((price - best_ask) / best_ask) * 100
                if dist_pct <= PROXIMITY_PCT:
                    if recent_trades < MIN_TRADES_PER_MIN or (vwap_15m and best_ask > vwap_15m) or not self.check_tape_aggressors_usd(symbol, "Sell", wall_usd):
                        continue
                    key = (symbol, "Sell", price)
                    if key not in self.wall_tracker: self.wall_tracker[key] = now
                    elif now - self.wall_tracker[key] >= MIN_WALL_LIFETIME_SEC:
                        self.open_real_position(symbol, "Sell", price, size)
                        self.wall_tracker.clear()
                        return

    def set_active_msg_id(self, msg_id):
        self.active_tg_msg_id = msg_id

    def open_real_position(self, symbol, side, price, wall_size):
        with self.lock:
            if self.in_position or not BYBIT_API_KEY:
                return
            self.in_position = True

        try:
            try:
                self.http_client.set_leverage(category=CATEGORY, symbol=symbol, buyLeverage=str(self.leverage), sellLeverage=str(self.leverage))
            except Exception: pass 

            pos_usd = self.current_balance * self.leverage
            raw_qty = pos_usd / price
            qty_str = self._normalize_qty(symbol, raw_qty)

            order = self.http_client.place_order(
                category=CATEGORY,
                symbol=symbol,
                side=side,
                orderType="Market",
                qty=qty_str,
                timeInForce="GTC"
            )

            if order.get("retCode") == 0:
                self.active_symbol = symbol
                self.position_side = side
                self.wall_price = price
                self.initial_wall_size = wall_size
                self.entry_time = time.time()
                self.last_tg_update_time = self.entry_time
                self.is_breakeven_set = False

                self.entry_price = price
                try:
                    pos_info = self.http_client.get_positions(category=CATEGORY, symbol=symbol)
                    for p in pos_info.get("result", {}).get("list", []):
                        if float(p.get("size", 0)) > 0:
                            self.entry_price = float(p.get("avgPrice", price))
                            break
                except Exception: pass

                self.tp_price = round(self.entry_price * (1 + TAKE_PROFIT_PCT / 100 if side == "Buy" else 1 - TAKE_PROFIT_PCT / 100), 4)
                self.sl_price = round(self.entry_price * (1 - STOP_LOSS_PCT / 100 if side == "Buy" else 1 + STOP_LOSS_PCT / 100), 4)

                msg_text = self._generate_open_card_text(0)
                send_tg_message_async(msg_text, reply_markup=self.get_main_menu_keyboard(), callback=self.set_active_msg_id)
            else:
                print(f"❌ Ошибка Bybit API: {order.get('retMsg')}")
                self.in_position = False

        except Exception as e:
            print(f"❌ Исключение при открытии позиции: {e}")
            self.in_position = False

    def close_real_position(self, reason, close_price):
        with self.lock:
            if not self.in_position: return

            try:
                close_side = "Sell" if self.position_side == "Buy" else "Buy"
                qty = "0"
                pos_info = self.http_client.get_positions(category=CATEGORY, symbol=self.active_symbol)
                for p in pos_info.get("result", {}).get("list", []):
                    if float(p.get("size", 0)) > 0:
                        qty = p.get("size")
                        break

                if float(qty) > 0:
                    self.http_client.place_order(
                        category=CATEGORY,
                        symbol=self.active_symbol,
                        side=close_side,
                        orderType="Market",
                        qty=qty,
                        reduceOnly=True,
                        timeInForce="GTC"
                    )

                now = time.time()
                slippage_mult = (1 - SLIPPAGE_PCT / 100) if self.position_side == "Buy" else (1 + SLIPPAGE_PCT / 100)
                actual_close_price = round(close_price * slippage_mult, 4)

                gross_pnl_pct = ((actual_close_price - self.entry_price) / self.entry_price * 100) if self.position_side == "Buy" else ((self.entry_price - actual_close_price) / self.entry_price * 100)
                net_pnl_pct = gross_pnl_pct - TAKER_FEE_PCT
                net_usd_pnl = (net_pnl_pct / 100) * (self.current_balance * self.leverage)

                self.current_balance += net_usd_pnl
                closed_symbol = self.active_symbol
                self.coin_trade_history[closed_symbol].append((now, net_pnl_pct))

                if net_pnl_pct > 0:
                    self.wins_count += 1
                    status_icon = "🟢 ПРОФИТ"
                else:
                    self.losses_count += 1
                    status_icon = "🔴 УБЫТОК"

                self._save_state()

                msg = (
                    f"⚠️ *ЗАКРЫТИЕ СДЕЛКИ* — `{closed_symbol}`\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"📌 Позиция: `{'🟢 LONG' if self.position_side == 'Buy' else '🔴 SHORT'}`\n"
                    f"💡 Причина: `{reason}`\n"
                    f"📊 Результат: *{status_icon}* (`{net_pnl_pct:+.2f}%` / `${net_usd_pnl:+.2f}`)\n\n"
                    f"💳 Баланс: `${self.current_balance:.2f}`"
                )
                send_tg_message_async(msg)

            except Exception as e:
                print(f"❌ Ошибка при закрытии позиции: {e}")
            finally:
                self.in_position = False
                self.active_symbol = None
                self.position_side = None
                self.active_tg_msg_id = None
                self.last_close_time = time.time()

# ==================== СЕРВЕР ОБРАБОТКИ КОМАНД ====================
global_bot_instance = None

def process_telegram_updates():
    offset = 0
    try:
        res = requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?offset=-1", timeout=5).json()
        if res.get("ok") and res.get("result"):
            offset = res["result"][-1]["update_id"] + 1
    except: pass

    while True:
        try:
            res = requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?offset={offset}&timeout=10", timeout=12).json()
            if res.get("ok"):
                for update in res.get("result", []):
                    offset = update["update_id"] + 1
                    bot = global_bot_instance
                    if not bot: continue

                    if "message" in update and "text" in update["message"]:
                        msg_text = update["message"]["text"].strip()
                        
                        if msg_text.startswith("/setstats"):
                            try:
                                parts = msg_text.split()
                                bot.current_balance = float(parts[1])
                                bot.wins_count = int(parts[2])
                                bot.losses_count = int(parts[3])
                                bot._save_state()
                                send_tg_message_async(f"✅ *Статистика обновлена!* Баланс: `${bot.current_balance:.2f}`")
                            except Exception:
                                send_tg_message_async("⚠️ Формат: `/setstats 254.82 857 327`")
                            continue

                        if msg_text in ["/start", "/menu"]:
                            start_msg = f"⚙️ *БОЕВОЙ БОТ BYBIT*\n💳 Баланс: `${bot.current_balance:.2f}`"
                            send_tg_message_async(start_msg, reply_markup=bot.get_main_menu_keyboard())

                    if "callback_query" in update:
                        cq = update["callback_query"]
                        cq_id, data = cq["id"], cq.get("data")
                        try: requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery", json={"callback_query_id": cq_id}, timeout=2)
                        except: pass

                        if data == "close_now":
                            if bot.in_position:
                                bot.close_real_position("Ручной сброс", bot.entry_price)
                                send_tg_message_async("🛑 *Позиция закрыта!*", reply_markup=bot.get_main_menu_keyboard())
                            else:
                                send_tg_message_async("ℹ Нет активной позиции.", reply_markup=bot.get_main_menu_keyboard())
                        elif data == "toggle_pause":
                            bot.manual_paused = not bot.manual_paused
                            send_tg_message_async("⏸️ *Пауза.*" if bot.manual_paused else "▶ *В работе!*", reply_markup=bot.get_main_menu_keyboard())
                        elif data == "show_stats":
                            send_tg_message_async(f"📊 *СТАТИСТИКА*\n💳 `${bot.current_balance:.2f}`", reply_markup=bot.get_main_menu_keyboard())
            elif res.get("error_code") == 429: time.sleep(5)
        except: time.sleep(3)
        time.sleep(0.5)

def websocket_watchdog_thread(bot_instance):
    while True:
        try:
            time.sleep(5)
            if bot_instance and not bot_instance.manual_paused and not bot_instance.is_stopped:
                if time.time() - bot_instance.last_ws_data_time > 15:
                    bot_instance.hard_reconnect_websocket()
        except: time.sleep(5)

# ==================== ЗАПУСК ДЛЯ RENDER И FASTAPI ====================
def start_bot_thread():
    global global_bot_instance
    bot = LeverageRealBot()
    global_bot_instance = bot

    threading.Thread(target=process_telegram_updates, daemon=True).start()
    threading.Thread(target=websocket_watchdog_thread, args=(bot,), daemon=True).start()

    bot.hard_reconnect_websocket()
    send_tg_message_async("🚀 *Сканер Bybit запущен 24/7 на Render!*", reply_markup=bot.get_main_menu_keyboard())

# Инициализация FastAPI приложения для Render
app = FastAPI()

@app.api_route("/", methods=["GET", "HEAD"])
def health_check():
    return {"status": "ok", "bot": "working"}

@app.on_event("startup")
def startup_event():
    threading.Thread(target=start_bot_thread, daemon=True).start()

if __name__ == "__main__":
    start_bot_thread()