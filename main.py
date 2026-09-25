import os
import time
import threading
import requests
from datetime import datetime
from contextlib import asynccontextmanager
from pybit.unified_trading import WebSocket
from fastapi import FastAPI
import uvicorn

# ==================== НАСТРОЙКИ ТЕЛЕГРАМ ====================
TELEGRAM_BOT_TOKEN = "8528320744:AAHHUFF1NlunIRfQNfPYIgt71zmQbTrb9cs"
TELEGRAM_CHAT_ID = "1190982420"

# ==================== НАСТРОЙКИ СТРАТЕГИИ ====================
CATEGORY = "linear"            # Фьючерсы USDT (Mainnet)
TOP_COINS_LIMIT = 50           # Отслеживаем top-50 пар

# --- ДЕПОЗИТ И КРЕДИТНОЕ ПЛЕЧО ---
INITIAL_BALANCE = 31.12         # Твой стартовый реальный депозит ($20)
LEVERAGE = 5                   # Кредитное плечо (5x)
MAX_DRAWDOWN_PCT = 10.0        # Остановка бота при потере -10% от депо ($2.00)

EAT_THRESHOLD_PCT = 40.0       # Закрыть, если стенку разъели/сняли на 40%
TAKE_PROFIT_PCT = 0.4          # Тейк-профит (+0.4%)
STOP_LOSS_PCT = 0.25           # Стоп-лосс (-0.25%)
COOLDOWN_SEC = 10              # Стандартная пауза после сделки (30 сек)
TAKER_FEE_PCT = 0.055 * 2      # Комиссия биржи (~0.11% round-trip)

# --- БОЕВЫЕ СКАЛЬПЕРСКИЕ ФИЛЬТРЫ ---
PROXIMITY_PCT = 0.08           # Дистанция до стенки <= 0.08%
MIN_WALL_LIFETIME_SEC = 3.0    # Стенка должна простоять в стакане >= 3 сек
MAX_SPREAD_PCT = 0.04          # Максимальный спред <= 0.04%
TILT_COOLDOWN_SEC = 60        # Пауза 1 минут при 2 стопах за 10 мин

LOG_INTERVAL_SEC = 5           
LOG_FILE_NAME = "trade_log.txt" 
# =====================================================================

def send_tg_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"❌ [LOG] Ошибка отправки в TG: {e}")

def write_file_log(line_text):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    formatted_line = f"[{timestamp}] {line_text}\n"
    try:
        with open(LOG_FILE_NAME, "a", encoding="utf-8") as f:
            f.write(formatted_line)
    except Exception as e:
        print(f"❌ [FILE ERROR] Ошибка записи в файл: {e}")

class LeveragePaperBot:
    def __init__(self):
        print("⚙️ Инициализация прокачанного сканера...")
        
        self.targets = self._get_top_mainnet_symbols()
        
        # Финансы
        self.current_balance = INITIAL_BALANCE
        self.max_allowed_loss = INITIAL_BALANCE * (MAX_DRAWDOWN_PCT / 100) # $2.00
        
        self.in_position = False
        self.active_symbol = None
        self.position_side = None
        self.entry_price = 0.0
        self.wall_price = 0.0
        self.initial_wall_size = 0.0
        self.last_close_time = 0
        self.last_log_time = time.time()
        self.max_seen_wall = {"symbol": "", "usd": 0}
        self.is_stopped = False

        # Трекеры фильтров
        self.wall_tracker = {}         # {(symbol, side, price): first_seen_time}
        self.recent_sl_timestamps = [] # История стопов за 10 мин
        self.tilt_until = 0            # Время окончания Tilt-паузы

    def _get_top_mainnet_symbols(self):
        url = "https://api.bybit.com/v5/market/tickers?category=linear"
        try:
            res = requests.get(url, timeout=10).json()
            tickers = res.get("result", {}).get("list", [])
            
            usdt_tickers = [t for t in tickers if t["symbol"].endswith("USDT")]
            usdt_tickers.sort(key=lambda x: float(x.get("turnover24h", 0)), reverse=True)
            
            targets = {}
            for t in usdt_tickers[:TOP_COINS_LIMIT]:
                symbol = t["symbol"]
                turnover = float(t.get("turnover24h", 0))
                
                wall_threshold = max(80_000, round(turnover * 0.001, -3))
                if symbol == "BTCUSDT":
                    wall_threshold = 850_000  # Фильтр BTC от $850k
                elif symbol == "ETHUSDT":
                    wall_threshold = 500_000  # Фильтр ETH от $500k

                targets[symbol] = wall_threshold

            pos_size = INITIAL_BALANCE * LEVERAGE
            print("✅ Сканер готов!")
            print(f"💳 Депозит: ${INITIAL_BALANCE:.2f} - Плечо: {LEVERAGE}x - Объём сделки: ${pos_size:.2f}")
            print(f"🛑 Лимит максимальной потери: -${self.max_allowed_loss:.2f} (-{MAX_DRAWDOWN_PCT}%)")
            return targets
        except Exception as e:
            print(f"❌ Ошибка получения тикеров: {e}")
            return {"BTCUSDT": 850000, "ETHUSDT": 500000, "SOLUSDT": 150000}

    def on_orderbook_update(self, message):
        now = time.time()

        if self.is_stopped or now < self.tilt_until:
            return

        symbol = message.get("topic", "").split(".")[-1]
        if symbol not in self.targets:
            return

        wall_threshold_usd = self.targets[symbol]
        data = message.get("data", {})
        
        bids = {float(p): float(s) for p, s in data.get("b", [])}
        asks = {float(p): float(s) for p, s in data.get("a", [])}

        if not bids or not asks:
            return

        best_bid = max(bids.keys())
        best_ask = min(asks.keys())

        # 1. ФИЛЬТР СПРЕДА
        spread_pct = ((best_ask - best_bid) / best_bid) * 100
        if spread_pct > MAX_SPREAD_PCT:
            return

        # Пульс в консоль
        max_bid = max([p * s for p, s in bids.items()], default=0)
        max_ask = max([p * s for p, s in asks.items()], default=0)
        current_max = max(max_bid, max_ask)
        if current_max > self.max_seen_wall["usd"]:
            self.max_seen_wall = {"symbol": symbol, "usd": current_max}

        if now - self.last_log_time > LOG_INTERVAL_SEC:
            status_str = f"В ПОЗИЦИИ [{self.active_symbol}]" if self.in_position else f"ПОИСК СТЕНОК (Депо: ${self.current_balance:.2f})"
            print(f"📡 [PULSE] Сканирование... - Макс. стенка: {self.max_seen_wall['symbol']} (${self.max_seen_wall['usd']:,.0f}) - {status_str}")
            self.last_log_time = now

        if now - self.last_close_time < COOLDOWN_SEC:
            return

        # 2. ПОИСК ТОЧКИ ВХОДА С PROXIMITY И ANTI-SPOOFING
        if not self.in_position:
            # СНИЗУ: Ищем плотность в Bids для ПОКУПКИ (Buy)
            for price, size in bids.items():
                if price * size >= wall_threshold_usd:
                    # Проверяем дистанцию цены до стенки
                    dist_pct = ((best_bid - price) / best_bid) * 100
                    if dist_pct <= PROXIMITY_PCT:
                        key = (symbol, "Buy", price)
                        if key not in self.wall_tracker:
                            self.wall_tracker[key] = now
                        elif now - self.wall_tracker[key] >= MIN_WALL_LIFETIME_SEC:
                            self.open_paper_position(symbol, "Buy", price, size)
                            self.wall_tracker.clear()
                            return

            # СВЕРХУ: Ищем плотность в Asks для ПРОДАЖИ (Sell)
            for price, size in asks.items():
                if price * size >= wall_threshold_usd:
                    # Проверяем дистанцию цены до стенки
                    dist_pct = ((price - best_ask) / best_ask) * 100
                    if dist_pct <= PROXIMITY_PCT:
                        key = (symbol, "Sell", price)
                        if key not in self.wall_tracker:
                            self.wall_tracker[key] = now
                        elif now - self.wall_tracker[key] >= MIN_WALL_LIFETIME_SEC:
                            self.open_paper_position(symbol, "Sell", price, size)
                            self.wall_tracker.clear()
                            return

        # 3. КОНТРОЛЬ СТЕНКИ И TP/SL В ПОЗИЦИИ
        elif self.in_position and self.active_symbol == symbol:
            current_wall_map = bids if self.position_side == "Buy" else asks
            current_wall_size = current_wall_map.get(self.wall_price, 0.0)

            eaten_pct = ((self.initial_wall_size - current_wall_size) / self.initial_wall_size) * 100
            current_price = best_bid if self.position_side == "Sell" else best_ask

            if self.position_side == "Buy":
                if current_price >= self.tp_price:
                    self.close_paper_position(f"Take-Profit (+{TAKE_PROFIT_PCT}%)", current_price)
                    return
                elif current_price <= self.sl_price:
                    self.close_paper_position(f"Stop-Loss (-{STOP_LOSS_PCT}%)", current_price)
                    return
            else:
                if current_price <= self.tp_price:
                    self.close_paper_position(f"Take-Profit (+{TAKE_PROFIT_PCT}%)", current_price)
                    return
                elif current_price >= self.sl_price:
                    self.close_paper_position(f"Stop-Loss (-{STOP_LOSS_PCT}%)", current_price)
                    return

            if eaten_pct >= EAT_THRESHOLD_PCT:
                reason = f"Стенку разъели/сняли на {eaten_pct:.1f}%"
                self.close_paper_position(reason, current_price)

    def open_paper_position(self, symbol, side, price, wall_size):
        self.in_position = True
        self.active_symbol = symbol
        self.position_side = side
        self.entry_price = price
        self.wall_price = price
        self.initial_wall_size = wall_size

        self.tp_price = round(price * (1 + TAKE_PROFIT_PCT / 100 if side == "Buy" else 1 - TAKE_PROFIT_PCT / 100), 4)
        self.sl_price = round(price * (1 - STOP_LOSS_PCT / 100 if side == "Buy" else 1 + STOP_LOSS_PCT / 100), 4)

        position_usd = self.current_balance * LEVERAGE

        msg = (
            f"🎯 *ВХОД ({symbol})*\n\n"
            f"💵 Цена: `{price}` USDT\n"
            f"📦 Объём стенки: `${price * wall_size:,.0f}`\n"
            f"🚀 *Сделка {side} ({LEVERAGE}x плечо)*\n"
            f"💼 Объём позиции: `${position_usd:.2f}` (Маржа: `${self.current_balance:.2f}`)\n"
            f"🛡️ *Фильтры прошёл:* Стенка выстояла >3с | Дистанция <{PROXIMITY_PCT}%"
        )
        print(f"\n🔥 [LOG] {side} {symbol} по {price}! Стенка: ${price * wall_size:,.0f} - Плечо: {LEVERAGE}x")
        send_tg_message(msg)

    def close_paper_position(self, reason, close_price):
        now = time.time()
        if self.position_side == "Buy":
            gross_pnl_pct = ((close_price - self.entry_price) / self.entry_price) * 100
        else:
            gross_pnl_pct = ((self.entry_price - close_price) / self.entry_price) * 100

        net_pnl_pct = gross_pnl_pct - TAKER_FEE_PCT
        position_usd = self.current_balance * LEVERAGE
        net_usd_pnl = (net_pnl_pct / 100) * position_usd

        self.current_balance += net_usd_pnl
        total_loss = INITIAL_BALANCE - self.current_balance

        wall_usd = self.entry_price * self.initial_wall_size

        log_entry = (
            f"Открыл {self.active_symbol} ({self.position_side}) по {self.entry_price}! "
            f"Стенка: ${wall_usd:,.0f} - Плечо: {LEVERAGE}x - Закрылся по {close_price} ({reason}) - "
            f"PnL: {net_pnl_pct:+.2f}% (${net_usd_pnl:+.2f}) - Баланс: ${self.current_balance:.2f}"
        )
        write_file_log(log_entry)

        status_icon = "🟢 *ЧИСТЫЙ ПЛЮС*" if net_pnl_pct > 0 else "🔴 *УБЫТОК*"

        msg = (
            f"⚠️ *ЗАКРЫТИЕ ({self.active_symbol})*\n\n"
            f"📌 Направление: `{self.position_side}`\n"
            f"💡 Причина: {reason}\n"
            f"{status_icon}: *`{net_pnl_pct:+.2f}%` (`${net_usd_pnl:+.2f}`)*\n"
            f"💳 *Текущий баланс: `${self.current_balance:.2f}`*"
        )
        print(f"✅ [LOG] {log_entry}")
        send_tg_message(msg)

        self.in_position = False
        self.active_symbol = None
        self.position_side = None
        self.last_close_time = now

        # Проверка на Tilt Guard (Защита от серии стопов)
        if net_pnl_pct < 0:
            self.recent_sl_timestamps = [t for t in self.recent_sl_timestamps if now - t <= 600]
            self.recent_sl_timestamps.append(now)

            if len(self.recent_sl_timestamps) >= 2:
                self.tilt_until = now + TILT_COOLDOWN_SEC
                self.recent_sl_timestamps = []
                tilt_msg = (
                    f"🛡️ *TILT GUARD АКТИВИРОВАН!*\n\n"
                    f"⚠️ Получено 2 убыточных сделки за 10 минут.\n"
                    f"⏳ Бот берет паузу на 15 минут для переформирования рынка."
                )
                print(f"\n🛡️ [TILT GUARD] Пауза 15 минут из-за 2 стопов подряд.")
                send_tg_message(tilt_msg)

        # Stop-Out проверка
        if total_loss >= self.max_allowed_loss:
            self.is_stopped = True
            stop_msg = (
                f"🛑 *АВАРИЙНАЯ ОСТАНОВКА БОТА (STOP-OUT)!*\n\n"
                f"📉 Достигнут лимит убытка: `-{MAX_DRAWDOWN_PCT}%` (-${total_loss:.2f})\n"
                f"💳 Оставшийся баланс: `${self.current_balance:.2f}`\n"
                f"⚙️ Бот прекратил работу для защиты капитала."
            )
            print(f"\n🚨 [STOP-OUT] Достигнут лимит убытка -{MAX_DRAWDOWN_PCT}%. Бот остановлен!")
            write_file_log(f"🚨 [STOP-OUT] Бот остановлен при балансе ${self.current_balance:.2f}")
            send_tg_message(stop_msg)

# ==================== ЗАПУСК БОТА В ФОНЕ ====================
def start_bot_thread():
    bot = LeveragePaperBot()
    ws = WebSocket(testnet=False, channel_type=CATEGORY)
    for symbol in bot.targets.keys():
        ws.orderbook_stream(depth=50, symbol=symbol, callback=bot.on_orderbook_update)

    print(f"🚀 Сканер запущен на Mainnet с плечом {LEVERAGE}x!\n")
    while True:
        if bot.is_stopped:
            print("🛑 Бот остановлен по лимиту убытка.")
            break
        time.sleep(1)

# ==================== FASTAPI ВЕБ-СЕРВЕР ДЛЯ RENDER ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🚀 [SYSTEM] Сервер поднялся. Запускаем умный фоновый сканер Bybit...")
    bot_thread = threading.Thread(target=start_bot_thread, daemon=True)
    bot_thread.start()
    
    send_tg_message("🤖 *Умный сканер с защитой от спуфинга успешно запущен на Render!*")
    yield
    print("🛑 [SYSTEM] Сервер останавливается...")

app = FastAPI(lifespan=lifespan)

@app.get("/")
def health_check():
    return {"status": "ok", "bot": "working"}