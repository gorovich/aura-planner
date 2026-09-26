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

# ==================== НАСТРОЙКИ СТРАТЕГИИ (ПО УМОЛЧАНИЮ) ====================
CATEGORY = "linear"            # Фьючерсы USDT (Mainnet)
DEFAULT_TOP_COINS_LIMIT = 100   # Top-30 активных альтов
DEFAULT_INITIAL_BALANCE = 20.0 # Базовый депозит по умолчанию
DEFAULT_LEVERAGE = 5           # Кредитное плечо (5x)
MAX_DRAWDOWN_PCT = 10.0        # Остановка при потере -10%

EAT_THRESHOLD_PCT = 75.0       # Закрыть, если стенку разъели/сняли на 75%
TAKE_PROFIT_PCT = 0.35         # Тейк-профит (+0.35%)
STOP_LOSS_PCT = 0.20           # Жесткий стоп-лосс (-0.20%)
BREAKEVEN_TRIGGER_PCT = 0.20   # Перенос SL в Безубыток при достижении +0.20%
COOLDOWN_SEC = 20              # Пауза после закрытия сделки (20 сек)
TAKER_FEE_PCT = 0.055 * 2      # Комиссия биржи (~0.11% round-trip)
POSITION_TIMEOUT_SEC = 180     # Режим эвакуации через 3 минуты (180 сек)

# --- ФИЛЬТР АКТИВНОСТИ ТОРГОВЛИ (ЛЕНТА СДЕЛОК) ---
MIN_TRADES_PER_MIN = 25        # Минимум 25 сделок за последние 60 сек

# --- СКАЛЬПЕРСКИЕ ФИЛЬТРЫ И СКОРОСТЬ ---
PROXIMITY_PCT = 0.12           # Дистанция до стенки (<= 0.12%)
MIN_WALL_LIFETIME_SEC = 0.1    # МГНОВЕННЫЙ ВХОД (0.1 сек)
MAX_SPREAD_PCT = 0.04          # Максимальный спред (<= 0.04%)

# --- ДВУХУРОВНЕВАЯ ЗАЩИТА ОТ ШТОРМА ---
REST_5_PCT_SEC = 900           # Слив 5% -> отдых 15 минут
REST_10_PCT_SEC = 3600         # Слив 10% -> отдых 1 час

LOG_INTERVAL_SEC = 1           # Обновление пульса в консоли раз в 1 сек
TG_UPDATE_INTERVAL_SEC = 3.0   # Обновление шкалы в TG раз в 3 секунды

# --- ФАЙЛЫ ДАННЫХ И ЛОГОВ ---
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE_PATH = os.path.join(SCRIPT_DIR, "trade_log.txt")
STATE_FILE_PATH = os.path.join(SCRIPT_DIR, "state.json")
# =====================================================================

def send_tg_message(text, reply_markup=None):
    """Отправка сообщения с сериализованной клавиатурой кнопок"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        res = requests.post(url, json=payload, timeout=5).json()
        if res.get("ok"):
            return res.get("result", {}).get("message_id")
    except Exception as e:
        print(f"❌ [LOG] Ошибка отправки в TG: {e}")
    return None

def update_tg_message(message_id, text, reply_markup=None):
    """Динамическое редактирование существующего сообщения"""
    if not message_id:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "message_id": message_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        requests.post(url, json=payload, timeout=3)
    except Exception:
        pass

def make_progress_bar(elapsed_sec, total_sec=180, length=10):
    pct = min(1.0, elapsed_sec / total_sec)
    filled_length = int(length * pct)
    bar = "🟩" * filled_length + "░" * (length - filled_length)
    pct_digits = int(pct * 100)
    mins, secs = divmod(int(elapsed_sec), 60)
    total_mins, total_secs = divmod(total_sec, 60)
    time_str = f"{mins:02d}:{secs:02d} / {total_mins:02d}:{total_secs:02d}"
    warning_suffix = " ⚡ *Скоро эвакуация!*" if pct >= 0.8 else ""
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
        print("⚙️ Инициализация сканера...")
        
        self.lock = threading.Lock()
        
        self.leverage = DEFAULT_LEVERAGE
        self.top_coins_limit = DEFAULT_TOP_COINS_LIMIT
        self.session_start_balance = DEFAULT_INITIAL_BALANCE
        self.current_balance = DEFAULT_INITIAL_BALANCE
        self.wins_count = 0
        self.losses_count = 0
        self.manual_paused = False
        
        self.user_blacklist = set()
        self.awaiting_input_action = None
        
        self._load_state()

        self.ws_client = None
        self.targets = self._get_top_mainnet_symbols()
        self.trade_history = defaultdict(deque)

        self.max_allowed_loss = self.session_start_balance * (MAX_DRAWDOWN_PCT / 100)
        
        self.in_position = False
        self.active_symbol = None
        self.position_side = None
        self.entry_price = 0.0
        self.wall_price = 0.0
        self.initial_wall_size = 0.0
        self.entry_time = 0.0
        self.tp_price = 0.0
        self.sl_price = 0.0
        self.is_breakeven_set = False
        self.last_close_time = 0
        self.last_log_time = time.time()
        self.last_tg_update_time = 0.0
        self.active_tg_msg_id = None
        self.max_seen_wall = {"symbol": "", "usd": 0}
        self.is_stopped = False

        self.wall_tracker = {}
        self.pause_until = 0
        self.triggered_5_pct_pause = False

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
            with open(STATE_FILE_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
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
                    print(f"📦 [STATE] Восстановлен баланс: ${self.current_balance:.2f} | Плечо: {self.leverage}x | Бан-лист: {len(self.user_blacklist)} монет")
            except Exception as e:
                print(f"⚠️ [STATE ERROR] Ошибка чтения файла состояния: {e}")

    def _get_top_mainnet_symbols(self):
        url = "https://api.bybit.com/v5/market/tickers?category=linear"
        try:
            res = requests.get(url, timeout=10).json()
            tickers = res.get("result", {}).get("list", [])
            
            usdt_tickers = [t for t in tickers if t["symbol"].endswith("USDT")]
            usdt_tickers.sort(key=lambda x: float(x.get("turnover24h", 0)), reverse=True)
            
            targets = {}
            EXPLICIT_BAN = ["BTCUSDT", "ETHUSDT", "USDCUSDT", "USDEUSDT", "FDUSDUSDT"]

            for t in usdt_tickers:
                symbol = t["symbol"]
                
                if self.in_position and self.active_symbol == symbol:
                    turnover = float(t.get("turnover24h", 0))
                    wall_threshold = 350_000 if symbol == "SOLUSDT" else max(200_000, round(turnover * 0.0012, -3))
                    targets[symbol] = wall_threshold
                    continue

                if symbol in EXPLICIT_BAN or symbol in self.user_blacklist or "XAU" in symbol or "XAG" in symbol or "GOLD" in symbol:
                    continue  

                turnover = float(t.get("turnover24h", 0))
                wall_threshold = 350_000 if symbol == "SOLUSDT" else max(200_000, round(turnover * 0.0012, -3))
                targets[symbol] = wall_threshold

                if len(targets) >= self.top_coins_limit:
                    break

            return targets
        except Exception as e:
            print(f"❌ Ошибка получения тикеров: {e}")
            return {"SOLUSDT": 350000, "XRPUSDT": 200000, "DOGEUSDT": 200000}

    def reload_websocket_streams(self):
        """Безопасная перезагрузка WebSocket-стримов при смене настроек"""
        if not self.ws_client:
            return
        self.targets = self._get_top_mainnet_symbols()
        pos_size = self.current_balance * self.leverage
        print("\n🚀 [RELOAD] Переподключение WebSocket-потоков на новый список монет...")
        print(f"💳 Депозит: ${self.current_balance:.2f} - Плечо: {self.leverage}x - TOP-{self.top_coins_limit} - Объём: ${pos_size:.2f}\n")
        
        for symbol in self.targets.keys():
            try:
                self.ws_client.orderbook_stream(depth=50, symbol=symbol, callback=self.on_orderbook_update)
                self.ws_client.trade_stream(symbol=symbol, callback=self.on_public_trade_update)
            except Exception:
                pass

    def on_public_trade_update(self, message):
        symbol = message.get("topic", "").split(".")[-1]
        if symbol not in self.targets:
            return
        now = time.time()
        trades = message.get("data", [])
        for _ in trades:
            self.trade_history[symbol].append(now)

        while self.trade_history[symbol] and self.trade_history[symbol][0] < now - 60:
            self.trade_history[symbol].popleft()

    def get_recent_trade_count(self, symbol):
        now = time.time()
        while self.trade_history[symbol] and self.trade_history[symbol][0] < now - 60:
            self.trade_history[symbol].popleft()
        return len(self.trade_history[symbol])

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
            f"⏱️ *Тайм-аут позиции (3 мин):*\n"
            f"{bar_text}"
        )

    def on_orderbook_update(self, message):
        now = time.time()

        if self.is_stopped or self.manual_paused or now < self.pause_until:
            return

        symbol = message.get("topic", "").split(".")[-1]
        if symbol not in self.targets:
            return

        data = message.get("data", {})
        bids = {float(p): float(s) for p, s in data.get("b", [])}
        asks = {float(p): float(s) for p, s in data.get("a", [])}

        if not bids or not asks:
            return

        best_bid = max(bids.keys())
        best_ask = min(asks.keys())

        # 1. КОНТРОЛЬ СУЩЕСТВУЮЩЕЙ ПОЗИЦИИ
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
                    update_tg_message(self.active_tg_msg_id, updated_text, reply_markup=self.get_main_menu_keyboard())
                    self.last_tg_update_time = now

                is_in_profit_or_breakeven = current_pnl_pct >= 0.0

                if elapsed_time >= POSITION_TIMEOUT_SEC:
                    if is_in_profit_or_breakeven:
                        self.close_paper_position("Тайм-аут 3 мин (Эвакуация в 0/плюс)", current_price)
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
                    if not is_in_profit_or_breakeven:
                        reason = f"Стенку разъели/сняли на {eaten_pct:.1f}%"
                        self.close_paper_position(reason, current_price)
            return

        # 2. ФИЛЬТР ЧЕРНОГО СПИСКА ДЛЯ НОВЫХ ВХОДОВ
        if symbol in self.user_blacklist:
            return

        wall_threshold_usd = self.targets[symbol]

        spread_pct = ((best_ask - best_bid) / best_bid) * 100
        if spread_pct > MAX_SPREAD_PCT:
            return

        max_bid = max([p * s for p, s in bids.items()], default=0)
        max_ask = max([p * s for p, s in asks.items()], default=0)
        current_max = max(max_bid, max_ask)
        if current_max > self.max_seen_wall["usd"]:
            self.max_seen_wall = {"symbol": symbol, "usd": current_max}

        if now - self.last_log_time > LOG_INTERVAL_SEC:
            status_str = "ПАУЗА" if self.manual_paused else (f"В ПОЗИЦИИ [{self.active_symbol}]" if self.in_position else f"ПОИСК СТЕНОК (Депо: ${self.current_balance:.2f})")
            print(f"📡 [PULSE] Сканирование... - Макс. стенка: {self.max_seen_wall['symbol']} (${self.max_seen_wall['usd']:,.0f}) - {status_str}")
            self.last_log_time = now

        # 3. ПОИСК НОВОЙ ТОЧКИ ВХОДА
        if now - self.last_close_time < COOLDOWN_SEC:
            return

        recent_trades = self.get_recent_trade_count(symbol)

        # Bids (Buy)
        for price, size in bids.items():
            if price * size >= wall_threshold_usd:
                dist_pct = ((best_bid - price) / best_bid) * 100
                if dist_pct <= PROXIMITY_PCT:
                    if recent_trades < MIN_TRADES_PER_MIN:
                        continue
                    key = (symbol, "Buy", price)
                    if key not in self.wall_tracker:
                        self.wall_tracker[key] = now
                    elif now - self.wall_tracker[key] >= MIN_WALL_LIFETIME_SEC:
                        self.open_paper_position(symbol, "Buy", price, size)
                        self.wall_tracker.clear()
                        return

        # Asks (Sell)
        for price, size in asks.items():
            if price * size >= wall_threshold_usd:
                dist_pct = ((price - best_ask) / best_ask) * 100
                if dist_pct <= PROXIMITY_PCT:
                    if recent_trades < MIN_TRADES_PER_MIN:
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
        self.entry_price = price
        self.wall_price = price
        self.initial_wall_size = wall_size
        self.entry_time = time.time()
        self.last_tg_update_time = self.entry_time
        self.is_breakeven_set = False

        self.tp_price = round(price * (1 + TAKE_PROFIT_PCT / 100 if side == "Buy" else 1 - TAKE_PROFIT_PCT / 100), 4)
        self.sl_price = round(price * (1 - STOP_LOSS_PCT / 100 if side == "Buy" else 1 + STOP_LOSS_PCT / 100), 4)

        msg_text = self._generate_open_card_text(0)
        print(f"\n🔥 [LOG] {side} {symbol} по {price}! Стенка: ${price * wall_size:,.0f}")
        
        self.active_tg_msg_id = send_tg_message(msg_text, reply_markup=self.get_main_menu_keyboard())

    def close_paper_position(self, reason, close_price):
        with self.lock:
            if not self.in_position:
                return

            now = time.time()
            if self.position_side == "Buy":
                gross_pnl_pct = ((close_price - self.entry_price) / self.entry_price) * 100
            else:
                gross_pnl_pct = ((self.entry_price - close_price) / self.entry_price) * 100

            net_pnl_pct = gross_pnl_pct - TAKER_FEE_PCT
            position_usd = self.current_balance * self.leverage
            net_usd_pnl = (net_pnl_pct / 100) * position_usd

            self.current_balance += net_usd_pnl
            
            if net_pnl_pct > 0:
                self.wins_count += 1
                status_icon = "🟢 ПРОФИТ"
            else:
                self.losses_count += 1
                status_icon = "🔴 УБЫТОК"

            total_trades = self.wins_count + self.losses_count
            winrate = (self.wins_count / total_trades) * 100 if total_trades > 0 else 0.0

            session_pnl_usd = self.current_balance - self.session_start_balance
            session_pnl_pct = (session_pnl_usd / self.session_start_balance) * 100

            closed_symbol = self.active_symbol

            self._save_state()

            file_log_entry = (
                f"Открыл {closed_symbol} ({self.position_side}) по {self.entry_price}! "
                f"Закрылся по {close_price} ({reason}) - "
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
                f"💵 Выход по цене: `{close_price}` USDT\n"
                f"📊 Результат: *{status_icon}* (`{net_pnl_pct:+.2f}%` / `${net_usd_pnl:+.2f}`)\n\n"
                f"📈 *СТАТИСТИКА СЕССИИ*\n"
                f"⚔️ Сделки: 🟢 `{self.wins_count}`  |  🛑 `{self.losses_count}`\n"
                f"🎯 Винрейт: `{winrate:.1f}%`\n"
                f"💳 Баланс: `${self.current_balance:.2f}` (`{session_pnl_pct:+.2f}%` / `${session_pnl_usd:+.2f}`)\n"
                f"━━━━━━━━━━━━━━━━━━━━━━"
            )
            print(f"✅ [LOG] {file_log_entry}")
            send_tg_message(msg)

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
                send_tg_message(pause_msg)

            elif session_pnl_pct <= -10.0:
                self.is_stopped = True
                stop_msg = f"🛑 *АВАРИЙНАЯ ОСТАНОВКА (-10%)*\n💳 Баланс: `${self.current_balance:.2f}`"
                print(f"\n🚨 [STOP-OUT] Убыток -10%. Остановка торговли.")
                send_tg_message(stop_msg)

# ==================== СЕРВЕР ОБРАБОТКИ КОМАНД И НАЖАТИЙ КНОПОК ====================
global_bot_instance = None

def process_telegram_updates():
    offset = 0
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

                    # 1. ОБРАБОТКА ТЕКСТОВЫХ КОМАНД
                    if "message" in update and "text" in update["message"]:
                        msg_text = update["message"]["text"].strip()
                        
                        if bot.awaiting_input_action == "add_ban":
                            sym = msg_text.upper()
                            if not sym.endswith("USDT"):
                                sym += "USDT"
                            bot.user_blacklist.add(sym)
                            bot._save_state()
                            bot.reload_websocket_streams()
                            bot.awaiting_input_action = None
                            
                            extra_info = " *(доработает текущую сделку и уйдёт в бан)*" if bot.in_position and bot.active_symbol == sym else ""
                            send_tg_message(f"⛔ Монета `{sym}` добавлена в бан-лист!{extra_info}", reply_markup=bot.get_blacklist_keyboard())
                            continue

                        elif bot.awaiting_input_action == "remove_ban":
                            sym = msg_text.upper()
                            if not sym.endswith("USDT"):
                                sym += "USDT"
                            if sym in bot.user_blacklist:
                                bot.user_blacklist.remove(sym)
                                bot._save_state()
                                bot.reload_websocket_streams()
                                send_tg_message(f"✅ Монета `{sym}` удалена из бан-листа!", reply_markup=bot.get_blacklist_keyboard())
                            else:
                                send_tg_message(f"⚠️ Монета `{sym}` не найдена в бан-листе.", reply_markup=bot.get_blacklist_keyboard())
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
                            send_tg_message(start_msg, reply_markup=bot.get_main_menu_keyboard())

                        elif msg_text.startswith("/ban "):
                            sym = msg_text.split(" ")[-1].strip().upper()
                            if not sym.endswith("USDT"):
                                sym += "USDT"
                            bot.user_blacklist.add(sym)
                            bot._save_state()
                            bot.reload_websocket_streams()
                            send_tg_message(f"⛔ Монета `{sym}` добавлена в Черный Список!", reply_markup=bot.get_blacklist_keyboard())

                        elif msg_text.startswith("/unban "):
                            sym = msg_text.split(" ")[-1].strip().upper()
                            if not sym.endswith("USDT"):
                                sym += "USDT"
                            bot.user_blacklist.discard(sym)
                            bot._save_state()
                            bot.reload_websocket_streams()
                            send_tg_message(f"✅ Монета `{sym}` удалена из Черного Списка!", reply_markup=bot.get_blacklist_keyboard())

                    # 2. ОБРАБОТКА ИНТЕРАКТИВНЫХ КНОПОК
                    if "callback_query" in update:
                        cq = update["callback_query"]
                        cq_id = cq["id"]
                        data = cq.get("data")

                        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery", json={"callback_query_id": cq_id})

                        if data == "main_menu":
                            bot.awaiting_input_action = None
                            send_tg_message("⚙️ *ГЛАВНОЕ МЕНЮ*", reply_markup=bot.get_main_menu_keyboard())

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
                            send_tg_message(settings_msg, reply_markup=bot.get_settings_keyboard())

                        elif data == "menu_blacklist":
                            bl_text = ", ".join(f"`{s}`" for s in bot.user_blacklist) if bot.user_blacklist else "_Черный список пуст._"
                            msg = (
                                f"⛔ *УПРАВЛЕНИЕ ЧЕРНЫМ СПИСКОМ*\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"Монеты в бане ({len(bot.user_blacklist)}):\n{bl_text}\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━"
                            )
                            send_tg_message(msg, reply_markup=bot.get_blacklist_keyboard())

                        elif data == "add_ban_prompt":
                            bot.awaiting_input_action = "add_ban"
                            send_tg_message("✏️ *Напиши название тикера для бана* (например: `SOLUSDT` или `DOGE`):")

                        elif data == "remove_ban_prompt":
                            bot.awaiting_input_action = "remove_ban"
                            send_tg_message("✏️ *Напиши название тикера для разбана* (например: `SOLUSDT`):")

                        elif data == "show_blacklist":
                            bl_text = "\n".join(f"• `{s}`" for s in sorted(bot.user_blacklist)) if bot.user_blacklist else "_Список пуст._"
                            send_tg_message(f"📋 *ТЕКУЩИЙ ЧЕРНЫЙ СПИСОК:*\n\n{bl_text}", reply_markup=bot.get_blacklist_keyboard())

                        elif data == "set_lev_dialog":
                            bot.leverage = 10 if bot.leverage == 5 else (20 if bot.leverage == 10 else 5)
                            bot._save_state()
                            send_tg_message(f"🕹️ Плечо изменено на `{bot.leverage}x`!", reply_markup=bot.get_settings_keyboard())

                        elif data == "set_dep_dialog":
                            bot.current_balance = 50.0 if bot.current_balance == 20.0 else (100.0 if bot.current_balance == 50.0 else 20.0)
                            bot.session_start_balance = bot.current_balance
                            bot._save_state()
                            send_tg_message(f"💳 Баланс обновлён: `${bot.current_balance:.2f}`!", reply_markup=bot.get_settings_keyboard())

                        elif data == "set_top_dialog":
                            bot.top_coins_limit = 50 if bot.top_coins_limit == 30 else (10 if bot.top_coins_limit == 50 else 30)
                            bot._save_state()
                            bot.reload_websocket_streams()
                            send_tg_message(f"🏆 Теперь отслеживаем TOP-`{bot.top_coins_limit}` монет!", reply_markup=bot.get_settings_keyboard())

                        elif data == "close_now":
                            if bot.in_position:
                                bot.close_paper_position("Ручной сброс с телефона", bot.entry_price)
                                send_tg_message("🛑 *Позиция экстренно закрыта с телефона!*", reply_markup=bot.get_main_menu_keyboard())
                            else:
                                send_tg_message("ℹ️ Нет активной позиции.", reply_markup=bot.get_main_menu_keyboard())

                        elif data == "toggle_pause":
                            bot.manual_paused = not bot.manual_paused
                            st = "⏸️ *Сканер поставлен на паузу.*" if bot.manual_paused else "▶️ *Сканер возобновил работу!*"
                            send_tg_message(st, reply_markup=bot.get_main_menu_keyboard())

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
                            send_tg_message(stats_msg, reply_markup=bot.get_main_menu_keyboard())

                        elif data == "skip_rest":
                            bot.pause_until = 0
                            bot.triggered_5_pct_pause = False
                            send_tg_message("⚡ *Пауза защиты от шторма сброшена!*", reply_markup=bot.get_main_menu_keyboard())

                        elif data == "reboot_bot":
                            bot.wall_tracker.clear()
                            bot.reload_websocket_streams()
                            send_tg_message("🔄 *Сканер перезагружен, фильтры и тикеры обновлены!*", reply_markup=bot.get_main_menu_keyboard())

        except Exception:
            time.sleep(2)
        time.sleep(0.5)

# ==================== ЗАПУСК ДЛЯ ПК / RENDER ====================
def start_bot_thread():
    global global_bot_instance
    bot = LeveragePaperBot()
    global_bot_instance = bot

    threading.Thread(target=process_telegram_updates, daemon=True).start()

    ws = WebSocket(testnet=False, channel_type=CATEGORY)
    bot.ws_client = ws  # Привязываем экземпляр WS к объекту бота

    for symbol in bot.targets.keys():
        ws.orderbook_stream(depth=50, symbol=symbol, callback=bot.on_orderbook_update)
        ws.trade_stream(symbol=symbol, callback=bot.on_public_trade_update)

    print(f"🚀 Сканер запущен! WebSocket-потоки подвязаны к единой управляющей шине.\n")
    
    send_tg_message("🚀 *Сканер запущен! Напиши /start для открытия панели управления.*", reply_markup=bot.get_main_menu_keyboard())

    while True:
        if bot.is_stopped:
            print("🛑 Бот остановлен по лимиту убытка.")
            break
        time.sleep(1)

app = FastAPI()

@app.get("/")
def health_check():
    return {"status": "ok", "bot": "working"}

if __name__ == "__main__":
    start_bot_thread()