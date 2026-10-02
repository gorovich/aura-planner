import os
import time
import json
import threading
import requests
from datetime import datetime
from collections import defaultdict, deque
from pybit.unified_trading import WebSocket
from fastapi import FastAPI
import uvicorn

# ==================== НАСТРОЙКИ ТЕЛЕГРАМ ====================
TELEGRAM_BOT_TOKEN = "8528320744:AAHHUFF1NlunIRfQNfPYIgt71zmQbTrb9cs"
TELEGRAM_CHAT_ID = "1190982420"

# ==================== НАСТРОЙКИ СТРАТЕГИИ ====================
CATEGORY = "linear"            # Фьючерсы USDT (Mainnet)
DEFAULT_TOP_COINS_LIMIT = 40   # TOP-40 активных альтов
DEFAULT_INITIAL_BALANCE = 20.0 # Базовый депозит
DEFAULT_LEVERAGE = 5           # Кредитное плечо (5x)
MAX_DRAWDOWN_PCT = 10.0        # Остановка при потере -10%

SLIPPAGE_PCT = 0.02            # Учет проскальзывания 0.02%
MAX_CONSECUTIVE_LOSSES = 5     # Жесткий авто-бан после 5 убытков подряд

# --- УМНЫЕ ФИЛЬТРЫ ЗА X МИНУТ ---
PERFORMANCE_WINDOW_SEC = 1800  # Окно анализа монеты (30 минут)
MAX_LOSSES_IN_WINDOW = 3       # Макс. убытков за 30 минут -> Временный бан
MAX_WINDOW_PNL_LOSS = -0.30    # Макс. суммарный пролив монеты за 30 мин (-0.30%)

VWAP_WINDOW_SEC = 900          # Окно расчета микро-тренда VWAP (15 минут)
TAPE_DELTA_WINDOW_SEC = 10     # Окно анализа ленты перед входом (10 секунд)
MAX_IMBALANCE_RATIO = 0.30     # Макс. допустимый объем противника к стенке (30%)

# --- БЫСТРЫЕ ТАЙМ-АУТЫ ---
POSITION_TIMEOUT_SEC = 45      # Эвакуация через 45 секунд
COOLDOWN_SEC = 5               # Пауза между сделками 5 секунд
EAT_THRESHOLD_PCT = 60.0       # Выход при разъедании на 60%

# --- ТАРГЕТЫ ---
TAKE_PROFIT_PCT = 0.28         # Тейк-профит (+0.28%)
STOP_LOSS_PCT = 0.18           # Стоп-лосс (-0.18%)
BREAKEVEN_TRIGGER_PCT = 0.12   # Перенос в БУ при +0.12%
TAKER_FEE_PCT = 0.055 * 2      # Комиссия биржи (~0.11%)

# --- ФИЛЬТРЫ СТАКАНА ---
PROXIMITY_PCT = 0.22           # Дистанция до стенки (<= 0.22%)
MIN_WALL_LIFETIME_SEC = 0.0    # Мгновенный вход
MAX_SPREAD_PCT = 0.06          # Спред (<= 0.06%)
MIN_TRADES_PER_MIN = 10        # Минимум 10 сделок в минуту

REST_5_PCT_SEC = 900           # Слив 5% -> отдых 15 минут
REST_10_PCT_SEC = 3600         # Слив 10% -> отдых 1 час

LOG_INTERVAL_SEC = 15           
TG_UPDATE_INTERVAL_SEC = 3.0   

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE_PATH = os.path.join(SCRIPT_DIR, "trade_log.txt")
STATE_FILE_PATH = os.path.join(SCRIPT_DIR, "state.json")
TMP_STATE_FILE_PATH = os.path.join(SCRIPT_DIR, "state.json.tmp")
# =====================================================================

def send_tg_message(text, reply_markup=None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return None
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID, 
        "text": text, 
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        res = requests.post(url, json=payload, timeout=2.5).json()
        if res.get("ok"):
            return res.get("result", {}).get("message_id")
        else:
            print(f"❌ [TG API ERROR] {res.get('description')}")
    except Exception as e:
        print(f"❌ [TG ERROR] Не удалось отправить лог: {e}")
    return None

def send_tg_message_async(text, reply_markup=None):
    """Безопасная отправка логов без блокировки основного сканера"""
    try:
        t = threading.Thread(target=send_tg_message, args=(text, reply_markup), daemon=True)
        t.start()
    except Exception as e:
        print(f"⚠️ [ASYNC TG ERROR] Ошибка запуска потока TG: {e}")

def update_tg_message(message_id, text, reply_markup=None):
    if not message_id or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "message_id": message_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        requests.post(url, json=payload, timeout=2.0)
    except Exception:
        pass

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

def write_file_log(line_text):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    formatted_line = f"[{timestamp}] {line_text}\n"
    try:
        with open(LOG_FILE_PATH, "a", encoding="utf-8") as f:
            f.write(formatted_line)
            f.flush()
            os.fsync(f.fileno())
    except Exception as e:
        print(f"❌ [FILE ERROR] Ошибка записи в файл: {e}")

class LeveragePaperBot:
    def __init__(self):
        print("⚡ Инициализация умного сканера с анализом за X минут и VWAP...")
        
        self.lock = threading.Lock()
        
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
        
        self._load_state()

        self.ws_client = None
        self.targets = self._get_top_mainnet_symbols()
        self.trade_history = defaultdict(deque)

        self.max_allowed_loss = self.session_start_balance * (MAX_DRAWDOWN_PCT / 100)
        
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

    def _save_state(self):
        """Атомарная запись состояния для предотвращения битых JSON-файлов"""
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
            with open(TMP_STATE_FILE_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
                f.flush()
                os.fsync(f.fileno())
            os.replace(TMP_STATE_FILE_PATH, STATE_FILE_PATH)
        except Exception as e:
            print(f"❌ [STATE ERROR] Ошибка сохранения состояния: {e}")

    def _load_state(self):
        if os.path.exists(STATE_FILE_PATH):
            try:
                with open(STATE_FILE_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.current_balance = data.get("current_balance", DEFAULT_INITIAL_BALANCE)
                    self.session_start_balance = data.get("session_start_balance", DEFAULT_INITIAL_BALANCE)
                    self.wins_count = data.get("wins_count", 0)
                    self.losses_count = data.get("losses_count", 0)
                    self.leverage = data.get("leverage", DEFAULT_LEVERAGE)
                    self.top_coins_limit = data.get("top_coins_limit", DEFAULT_TOP_COINS_LIMIT)
                    self.user_blacklist = set(data.get("user_blacklist", []))
                    print(f"📦 [STATE] Восстановлен баланс: ${self.current_balance:.2f} | Плечо: {self.leverage}x | В ЧС: {len(self.user_blacklist)} монет")
            except Exception as e:
                print(f"⚠️ [STATE] Ошибка чтения state.json ({e}). Восстанавливаем чистый файл...")
                self._save_state()

    def _get_top_mainnet_symbols(self):
        url = "https://api.bybit.com/v5/market/tickers?category=linear"
        try:
            res = requests.get(url, timeout=10)
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
        except Exception as e:
            print(f"❌ Ошибка получения тикеров: {e}")
            return {"SOLUSDT": 250000, "XRPUSDT": 100000, "DOGEUSDT": 100000, "SUIUSDT": 100000, "APTUSDT": 100000, "NEARUSDT": 100000}

    def hard_reconnect_websocket(self):
        with self.lock:
            print("\n🔄 [HARD RECONNECT] Пересоздание WebSocket сокетов Bybit...")
            try:
                if self.ws_client:
                    self.ws_client._exit()
            except Exception:
                pass
            
            time.sleep(1)
            self.targets = self._get_top_mainnet_symbols()
            self.wall_tracker.clear()
            
            new_ws = WebSocket(testnet=False, channel_type=CATEGORY, ping_interval=20, ping_timeout=10)
            self.ws_client = new_ws

            for symbol in self.targets.keys():
                try:
                    new_ws.orderbook_stream(depth=50, symbol=symbol, callback=self.on_orderbook_update)
                    new_ws.trade_stream(symbol=symbol, callback=self.on_public_trade_update)
                except Exception as e:
                    print(f"⚠️ Ошибка подписки на {symbol}: {e}")
            
            self.last_ws_data_time = time.time()
            print("✅ [HARD RECONNECT] WebSocket успешно переподключен и активен!\n")

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

    def get_settings_keyboard(self):
        return {
            "inline_keyboard": [
                [
                    {"text": f"🕹️ Плечо: {self.leverage}x", "callback_data": "set_lev_dialog"},
                    {"text": f"💳 Депо: ${self.current_balance:.0f}", "callback_data": "set_dep_dialog"}
                ],
                [
                    {"text": f"🏆 ТОП-Монет: {self.top_coins_limit}", "callback_data": "set_top_dialog"}
                ],
                [
                    {"text": "◀️ НАЗАД В МЕНЮ", "callback_data": "main_menu"}
                ]
            ]
        }

    def get_blacklist_keyboard(self):
        return {
            "inline_keyboard": [
                [{"text": "➕ ДОБАВИТЬ В БАН", "callback_data": "add_ban_prompt"}, {"text": "➖ СНЯТЬ ИЗ БАНА", "callback_data": "remove_ban_prompt"}],
                [{"text": "📋 ПОКАЗАТЬ БАН-ЛИСТ", "callback_data": "show_blacklist"}],
                [{"text": "◀️ НАЗАД В МЕНЮ", "callback_data": "main_menu"}]
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
            
            ban_text = (
                f"🛡️ *УПРЕЖДАЮЩИЙ АВТО-БАН* — `{symbol}`\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"⚠️ Монета за 30 мин принесла `{losses_cnt}` убытка (PnL: `{sum_pnl:+.2f}%`)\n"
                f"🚫 Монета забанена. Список отслеживаемых пар обновлён!"
            )
            print(f"\n🛡️ [PREVENTIVE BAN] Монета {symbol} забанена! Ротация монет выполнена.\n")
            send_tg_message_async(ban_text, reply_markup=self.get_blacklist_keyboard())
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
                    threading.Thread(target=update_tg_message, args=(self.active_tg_msg_id, updated_text, self.get_main_menu_keyboard()), daemon=True).start()
                    self.last_tg_update_time = now

                if elapsed_time >= POSITION_TIMEOUT_SEC:
                    if current_pnl_pct >= 0.15:
                        self.close_paper_position("Быстрый сброс в профит (45 сек)", current_price)
                        return
                    elif elapsed_time >= 70:
                        self.close_paper_position("Тайм-аут без движения (70 сек)", current_price)
                        return

                if self.position_side == "Buy":
                    if current_price >= self.tp_price:
                        self.close_paper_position(f"Take-Profit (+{TAKE_PROFIT_PCT}%)", current_price)
                        return
                    elif current_price <= self.sl_price:
                        self.close_paper_position(f"Stop-Loss / BU ({self.sl_price})", current_price)
                        return
                else:
                    if current_price <= self.tp_price:
                        self.close_paper_position(f"Take-Profit (+{TAKE_PROFIT_PCT}%)", current_price)
                        return
                    elif current_price >= self.sl_price:
                        self.close_paper_position(f"Stop-Loss / BU ({self.sl_price})", current_price)
                        return

                if eaten_pct >= EAT_THRESHOLD_PCT:
                    if current_pnl_pct < 0.15:
                        reason = f"Стенку разъели на {eaten_pct:.1f}%"
                        self.close_paper_position(reason, current_price)
            return

        wall_threshold_usd = self.targets[symbol]

        spread_pct = ((best_ask - best_bid) / best_bid) * 100
        if spread_pct > MAX_SPREAD_PCT:
            return

        max_bid = max([p * s for p, s in bids.items()], default=0)
        max_ask = max([p * s for p, s in asks.items()], default=0)
        current_max = max(max_bid, max_ask)
        
        if current_max > self.max_seen_wall["usd"] or self.max_seen_wall["symbol"] in self.user_blacklist:
            self.max_seen_wall = {"symbol": symbol, "usd": current_max}

        if now - self.last_log_time > LOG_INTERVAL_SEC:
            status_str = "ПАУЗА" if self.manual_paused else (f"В ПОЗИЦИИ [{self.active_symbol}]" if self.in_position else f"ПОИСК СТЕНОК (Депо: ${self.current_balance:.2f})")
            print(f"📡 [PULSE] Сканирование {symbol}... - Стенка: ${current_max:,.0f} - {status_str}")
            self.last_log_time = now

        if now - self.last_close_time < COOLDOWN_SEC:
            return

        recent_trades = self.get_recent_trade_count(symbol)
        vwap_15m = self.calculate_vwap_15m(symbol)

        # Bids (Buy - LONG)
        for price, size in bids.items():
            wall_usd = price * size
            if wall_usd >= wall_threshold_usd:
                dist_pct = ((best_bid - price) / best_bid) * 100
                if dist_pct <= PROXIMITY_PCT:
                    if recent_trades < MIN_TRADES_PER_MIN:
                        continue
                    if vwap_15m and best_bid < vwap_15m:
                        continue
                    if not self.check_tape_aggressors_usd(symbol, "Buy", wall_usd):
                        continue

                    key = (symbol, "Buy", price)
                    if key not in self.wall_tracker:
                        self.wall_tracker[key] = now
                    elif now - self.wall_tracker[key] >= MIN_WALL_LIFETIME_SEC:
                        self.open_paper_position(symbol, "Buy", price, size)
                        self.wall_tracker.clear()
                        return

        # Asks (Sell - SHORT)
        for price, size in asks.items():
            wall_usd = price * size
            if wall_usd >= wall_threshold_usd:
                dist_pct = ((price - best_ask) / best_ask) * 100
                if dist_pct <= PROXIMITY_PCT:
                    if recent_trades < MIN_TRADES_PER_MIN:
                        continue
                    if vwap_15m and best_ask > vwap_15m:
                        continue
                    if not self.check_tape_aggressors_usd(symbol, "Sell", wall_usd):
                        continue

                    key = (symbol, "Sell", price)
                    if key not in self.wall_tracker:
                        self.wall_tracker[key] = now
                    elif now - self.wall_tracker[key] >= MIN_WALL_LIFETIME_SEC:
                        self.open_paper_position(symbol, "Sell", price, size)
                        self.wall_tracker.clear()
                        return

    def open_paper_position(self, symbol, side, price, wall_size):
        self.in_position = True
        self.active_symbol = symbol
        self.position_side = side
        
        slippage_mult = (1 + SLIPPAGE_PCT / 100) if side == "Buy" else (1 - SLIPPAGE_PCT / 100)
        self.entry_price = round(price * slippage_mult, 4)
        
        self.wall_price = price
        self.initial_wall_size = wall_size
        self.entry_time = time.time()
        self.last_tg_update_time = self.entry_time
        self.is_breakeven_set = False

        self.tp_price = round(self.entry_price * (1 + TAKE_PROFIT_PCT / 100 if side == "Buy" else 1 - TAKE_PROFIT_PCT / 100), 4)
        self.sl_price = round(self.entry_price * (1 - STOP_LOSS_PCT / 100 if side == "Buy" else 1 + STOP_LOSS_PCT / 100), 4)

        msg_text = self._generate_open_card_text(0)
        print(f"\n⚡ [LOG] {side} {symbol} по {self.entry_price}! Стенка: ${price * wall_size:,.0f}")
        
        threading.Thread(target=self._async_send_open_card, args=(msg_text,), daemon=True).start()

    def _async_send_open_card(self, msg_text):
        self.active_tg_msg_id = send_tg_message(msg_text, reply_markup=self.get_main_menu_keyboard())

    def close_paper_position(self, reason, close_price):
        with self.lock:
            if not self.in_position:
                return

            now = time.time()
            
            slippage_mult = (1 - SLIPPAGE_PCT / 100) if self.position_side == "Buy" else (1 + SLIPPAGE_PCT / 100)
            actual_close_price = round(close_price * slippage_mult, 4)

            if self.position_side == "Buy":
                gross_pnl_pct = ((actual_close_price - self.entry_price) / self.entry_price) * 100
            else:
                gross_pnl_pct = ((self.entry_price - actual_close_price) / self.entry_price) * 100

            net_pnl_pct = gross_pnl_pct - TAKER_FEE_PCT
            position_usd = self.current_balance * self.leverage
            net_usd_pnl = (net_pnl_pct / 100) * position_usd

            self.current_balance += net_usd_pnl
            closed_symbol = self.active_symbol
            
            self.coin_trade_history[closed_symbol].append((now, net_pnl_pct))

            if net_pnl_pct > 0:
                self.wins_count += 1
                status_icon = "🟢 ПРОФИТ"
                self.consecutive_losses[closed_symbol] = 0
            else:
                self.losses_count += 1
                status_icon = "🔴 УБЫТОК"
                self.consecutive_losses[closed_symbol] += 1

            total_trades = self.wins_count + self.losses_count
            winrate = (self.wins_count / total_trades) * 100 if total_trades > 0 else 0.0

            session_pnl_usd = self.current_balance - self.session_start_balance
            session_pnl_pct = (session_pnl_usd / self.session_start_balance) * 100

            self._save_state()

            file_log_entry = (
                f"Открыл {closed_symbol} ({self.position_side}) по {self.entry_price}! "
                f"Закрылся по {actual_close_price} ({reason}) - "
                f"PnL: {net_pnl_pct:+.2f}% (${net_usd_pnl:+.2f}) - "
                f"Баланс: ${self.current_balance:.2f}"
            )
            write_file_log(file_log_entry)

            side_icon = "🟢 LONG" if self.position_side == "Buy" else "🔴 SHORT"

            msg = (
                f"⚠️ *ЗАКРЫТИЕ СДЕЛКИ* — `{closed_symbol}`\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📌 Позиция: `{side_icon}`\n"
                f"💡 Причина: `{reason}`\n"
                f"💵 Выход по цене: `{actual_close_price}` USDT\n"
                f"📊 Результат: *{status_icon}* (`{net_pnl_pct:+.2f}%` / `${net_usd_pnl:+.2f}`)\n\n"
                f"📈 *СТАТИСТИКА СЕССИИ*\n"
                f"⚔️ Сделки: 🟢 `{self.wins_count}`  |  🛑 `{self.losses_count}`\n"
                f"🎯 Винрейт: `{winrate:.1f}%`\n"
                f"💳 Баланс: `${self.current_balance:.2f}` (`{session_pnl_pct:+.2f}%` / `${session_pnl_usd:+.2f}`)\n"
                f"━━━━━━━━━━━━━━━━━━━━━━"
            )
            print(f"✅ [LOG] {file_log_entry}")
            
            send_tg_message_async(msg)

            if self.consecutive_losses[closed_symbol] >= MAX_CONSECUTIVE_LOSSES:
                self.user_blacklist.add(closed_symbol)
                self._save_state()
                auto_ban_msg = (
                    f"⛔ *АВТО-БАН МОНЕТЫ* — `{closed_symbol}`\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"⚠️ Монета получила `{MAX_CONSECUTIVE_LOSSES}` убытков подряд!\n"
                    f"🚫 `{closed_symbol}` автоматически добавлена в ЧС."
                )
                print(f"\n⛔ [AUTO-BAN] Монета {closed_symbol} забанена из-за {MAX_CONSECUTIVE_LOSSES} убытков подряд!\n")
                send_tg_message_async(auto_ban_msg, reply_markup=self.get_blacklist_keyboard())

            self.in_position = False
            self.active_symbol = None
            self.position_side = None
            self.active_tg_msg_id = None
            self.last_close_time = now

            if session_pnl_pct <= -5.0 and session_pnl_pct > -10.0 and not self.triggered_5_pct_pause:
                self.pause_until = now + REST_5_PCT_SEC
                self.triggered_5_pct_pause = True
                pause_msg = (
                    f"🛡️ *ЗАЩИТА ОТ ШТОРМА (-5%)*\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"📉 Сессионная просадка: `{session_pnl_pct:.2f}%`\n"
                    f"⏳ Пауза: *15 минут*, остываем."
                )
                print(f"\n⚠️ [PAUSE 15m] Достигнут убыток -5%. Пауза 15 минут.")
                send_tg_message_async(pause_msg)

            elif session_pnl_pct <= -10.0:
                self.is_stopped = True
                stop_msg = f"🛑 *АВАРИЙНАЯ ОСТАНОВКА (-10%)*\n💳 Баланс: `${self.current_balance:.2f}`"
                print(f"\n🚨 [STOP-OUT] Убыток -10%. Остановка торговли.")
                send_tg_message_async(stop_msg)

# ==================== СЕРВЕР ОБРАБОТКИ КОМАНД И НАЖАТИЙ КНОПОК ====================
global_bot_instance = None

def process_telegram_updates():
    offset = 0
    # Сбрасываем старые накопившиеся апдейты при старте
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?offset=-1"
        res = requests.get(url, timeout=5).json()
        if res.get("ok") and res.get("result"):
            offset = res["result"][-1]["update_id"] + 1
    except Exception:
        pass

    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?offset={offset}&timeout=10"
            res = requests.get(url, timeout=12).json()
            if res.get("ok"):
                for update in res.get("result", []):
                    offset = update["update_id"] + 1
                    bot = global_bot_instance

                    if not bot:
                        continue

                    if "message" in update and "text" in update["message"]:
                        msg_text = update["message"]["text"].strip()
                        
                        if bot.awaiting_input_action == "add_ban":
                            sym = msg_text.upper()
                            if not sym.endswith("USDT"):
                                sym += "USDT"
                            bot.user_blacklist.add(sym)
                            bot._save_state()
                            bot.awaiting_input_action = None
                            send_tg_message_async(f"⛔ Монета `{sym}` добавлена в бан-лист!", reply_markup=bot.get_blacklist_keyboard())
                            continue

                        elif bot.awaiting_input_action == "remove_ban":
                            sym = msg_text.upper()
                            if not sym.endswith("USDT"):
                                sym += "USDT"
                            if sym in bot.user_blacklist:
                                bot.user_blacklist.remove(sym)
                                bot.consecutive_losses[sym] = 0
                                bot.coin_trade_history[sym] = []
                                bot._save_state()
                                send_tg_message_async(f"✅ Монета `{sym}` удалена из бан-листа и её история очищена!", reply_markup=bot.get_blacklist_keyboard())
                            else:
                                send_tg_message_async(f"⚠️ Монета `{sym}` не найдена в бан-листе.", reply_markup=bot.get_blacklist_keyboard())
                            bot.awaiting_input_action = None
                            continue

                        if msg_text in ["/start", "/menu"]:
                            start_msg = (
                                "⚙️ *ПАНЕЛЬ УПРАВЛЕНИЯ СКАНЕРОМ BYBIT*\n"
                                "━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"💳 Баланс: `${bot.current_balance:.2f}` | 🕹️ Плечо: `{bot.leverage}x`\n"
                                f"🏆 ТОП-Монет: `{bot.top_coins_limit}` | ⛔ В бане: `{len(bot.user_blacklist)}` шт.\n"
                                "━━━━━━━━━━━━━━━━━━━━━━"
                            )
                            send_tg_message_async(start_msg, reply_markup=bot.get_main_menu_keyboard())

                        elif msg_text.startswith("/ban "):
                            sym = msg_text.split(" ")[-1].strip().upper()
                            if not sym.endswith("USDT"):
                                sym += "USDT"
                            bot.user_blacklist.add(sym)
                            bot._save_state()
                            send_tg_message_async(f"⛔ Монета `{sym}` добавлена в Черный Список!", reply_markup=bot.get_blacklist_keyboard())

                        elif msg_text.startswith("/unban "):
                            sym = msg_text.split(" ")[-1].strip().upper()
                            if not sym.endswith("USDT"):
                                sym += "USDT"
                            bot.user_blacklist.discard(sym)
                            bot._save_state()
                            send_tg_message_async(f"✅ Монета `{sym}` удалена из Черного Списка!", reply_markup=bot.get_blacklist_keyboard())

                    if "callback_query" in update:
                        cq = update["callback_query"]
                        cq_id = cq["id"]
                        data = cq.get("data")

                        try:
                            requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery", json={"callback_query_id": cq_id}, timeout=2)
                        except Exception:
                            pass

                        if data == "main_menu":
                            bot.awaiting_input_action = None
                            send_tg_message_async("⚙️ *ГЛАВНОЕ МЕНЮ*", reply_markup=bot.get_main_menu_keyboard())

                        elif data == "menu_settings":
                            settings_msg = (
                                f"⚙️ *НАСТРОЙКИ СТРАТЕГИИ*\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"🕹️ Кредитное плечо: `{bot.leverage}x`\n"
                                f"💳 Текущий депозит: `${bot.current_balance:.2f}`\n"
                                f"🏆 Лимит ТОП-монет: `{bot.top_coins_limit}`\n"
                                f"💼 Объём позиции: `${bot.current_balance * bot.leverage:.2f}`\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"_Выбери параметр для изменения:_"
                            )
                            send_tg_message_async(settings_msg, reply_markup=bot.get_settings_keyboard())

                        elif data == "menu_blacklist":
                            bl_text = ", ".join(f"`{s}`" for s in sorted(bot.user_blacklist)) if bot.user_blacklist else "_Черный список пуст._"
                            msg = (
                                f"⛔ *УПРАВЛЕНИЕ ЧЕРНЫМ СПИСКОМ*\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"Монеты в бане ({len(bot.user_blacklist)}):\n{bl_text}\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━"
                            )
                            send_tg_message_async(msg, reply_markup=bot.get_blacklist_keyboard())

                        elif data == "add_ban_prompt":
                            bot.awaiting_input_action = "add_ban"
                            send_tg_message_async("✏️ *Напиши название тикера для бана* (например: `SOLUSDT` или `DOGE`):")

                        elif data == "remove_ban_prompt":
                            bot.awaiting_input_action = "remove_ban"
                            send_tg_message_async("✏️ *Напиши название тикера для разбана* (например: `SOLUSDT`):")

                        elif data == "show_blacklist":
                            bl_text = "\n".join(f"• `{s}`" for s in sorted(bot.user_blacklist)) if bot.user_blacklist else "_Список пуст._"
                            send_tg_message_async(f"📋 *ТЕКУЩИЙ ЧЕРНЫЙ СПИСОК:*\n\n{bl_text}", reply_markup=bot.get_blacklist_keyboard())

                        elif data == "set_lev_dialog":
                            bot.leverage = 10 if bot.leverage == 5 else (20 if bot.leverage == 10 else 5)
                            bot._save_state()
                            send_tg_message_async(f"🕹️ Плечо изменено на `{bot.leverage}x`!", reply_markup=bot.get_settings_keyboard())

                        elif data == "set_dep_dialog":
                            bot.current_balance = 50.0 if bot.current_balance == 20.0 else (100.0 if bot.current_balance == 50.0 else 20.0)
                            bot.session_start_balance = bot.current_balance
                            bot._save_state()
                            send_tg_message_async(f"💳 Баланс обновлён: `${bot.current_balance:.2f}`!", reply_markup=bot.get_settings_keyboard())

                        elif data == "set_top_dialog":
                            bot.top_coins_limit = 50 if bot.top_coins_limit == 30 else (10 if bot.top_coins_limit == 50 else 30)
                            bot._save_state()
                            send_tg_message_async(f"🏆 Теперь отслеживаем TOP-`{bot.top_coins_limit}` монет!", reply_markup=bot.get_settings_keyboard())

                        elif data == "close_now":
                            if bot.in_position:
                                bot.close_paper_position("Ручной сброс с телефона", bot.entry_price)
                                send_tg_message_async("🛑 *Позиция экстренно закрыта с телефона!*", reply_markup=bot.get_main_menu_keyboard())
                            else:
                                send_tg_message_async("ℹ Нет активной позиции.", reply_markup=bot.get_main_menu_keyboard())

                        elif data == "toggle_pause":
                            bot.manual_paused = not bot.manual_paused
                            st = "⏸️ *Сканер поставлен на паузу.*" if bot.manual_paused else "▶️ *Сканер возобновил работу!*"
                            send_tg_message_async(st, reply_markup=bot.get_main_menu_keyboard())

                        elif data == "show_stats":
                            total = bot.wins_count + bot.losses_count
                            wr = (bot.wins_count / total * 100) if total > 0 else 0.0
                            pnl_usd = bot.current_balance - bot.session_start_balance
                            pnl_pct = (pnl_usd / bot.session_start_balance) * 100
                            stats_msg = (
                                f"📊 *ТЕКУЩАЯ СТАТИСТИКА*\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"💳 Баланс: `${bot.current_balance:.2f}` (`{pnl_pct:+.2f}%` / `${pnl_usd:+.2f}`)\n"
                                f"🕹️ Плечо: `{bot.leverage}x` | Объём: `${bot.current_balance * bot.leverage:.2f}`\n"
                                f"⚔️ Сделки: 🟢 `{bot.wins_count}` | 🛑 `{bot.losses_count}`\n"
                                f"🎯 Винрейт: `{wr:.1f}%`\n"
                                f"Статус: " + ("⏸️ На паузе" if bot.manual_paused else "🟢 В поиске")
                            )
                            send_tg_message_async(stats_msg, reply_markup=bot.get_main_menu_keyboard())

                        elif data == "skip_rest":
                            bot.pause_until = 0
                            bot.triggered_5_pct_pause = False
                            send_tg_message_async("⚡ *Пауза защиты от шторма сброшена!*", reply_markup=bot.get_main_menu_keyboard())

                        elif data == "reboot_bot":
                            threading.Thread(target=bot.hard_reconnect_websocket, daemon=True).start()
                            send_tg_message_async("🔄 *Сокеты и сокет-соединения с Bybit полностью пересозданы!*", reply_markup=bot.get_main_menu_keyboard())

        except Exception as e:
            print(f"⚠️ [TG POLL ERROR] {e}")
            time.sleep(2)
        time.sleep(0.5)

def websocket_watchdog_thread(bot_instance):
    while True:
        try:
            time.sleep(5)
            if bot_instance and not bot_instance.manual_paused and not bot_instance.is_stopped:
                idle_time = time.time() - bot_instance.last_ws_data_time
                if idle_time > 15:
                    print(f"\n🚨 [WATCHDOG] WebSocket застрял! Данных нет {idle_time:.1f} сек. Авто-реанимация...")
                    bot_instance.hard_reconnect_websocket()
        except Exception as e:
            print(f"⚠️ [WATCHDOG ERROR] {e}")

# ==================== ЗАПУСК ДЛЯ ПК / RENDER ====================
def start_bot_thread():
    global global_bot_instance
    bot = LeveragePaperBot()
    global_bot_instance = bot

    threading.Thread(target=process_telegram_updates, daemon=True).start()
    threading.Thread(target=websocket_watchdog_thread, args=(bot,), daemon=True).start()

    bot.hard_reconnect_websocket()

    print(f"⚡ Сканер запущен с умной аналитикой (VWAP + Дельта ленты + Упреждающий бан за 30 мин)!\n")
    send_tg_message_async("🚀 *Сканер запущен! VWAP-трекер, фильтр дельты и упреждающий бан за 30 минут активны.*", reply_markup=bot.get_main_menu_keyboard())

    while True:
        if bot.is_stopped:
            print("🛑 Бот остановлен по лимиту убытка.")
            break
        time.sleep(1)

app = FastAPI()

@app.api_route("/", methods=["GET", "HEAD"])
def health_check():
    return {"status": "ok", "bot": "working"}

@app.on_event("startup")
def startup_event():
    print("🚀 [RENDER START] Запуск фонового сканера Bybit...")
    threading.Thread(target=start_bot_thread, daemon=True).start()

if __name__ == "__main__":
    start_bot_thread()