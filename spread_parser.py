import requests
import time
import os
import json
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

SYMBOL = "FFUSDT"
SPOT_URL = f"https://api.binance.com/api/v3/ticker/price?symbol={SYMBOL}"
FUTURES_URL = f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={SYMBOL}"
SPOT_DEPTH_URL = f"https://api.binance.com/api/v3/depth?symbol={SYMBOL}&limit=100"
FUTURES_DEPTH_URL = f"https://fapi.binance.com/fapi/v1/depth?symbol={SYMBOL}&limit=100"

TG_TOKEN = os.environ["TG_TOKEN"]
TG_CHAT_ID = os.environ["TG_CHAT_ID"]
TG_URL = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"

BOOK_DEPTH = 2.0
SIGNAL_PCT = 60.0
CHECK_DELAY = 10       # секунд до проверки результата
STATS_FILE = "stats.json"
stats_lock = threading.Lock()


# ── Статистика ────────────────────────────────────────────────────────────────

def load_stats():
    if os.path.exists(STATS_FILE):
        with open(STATS_FILE) as f:
            return json.load(f)
    return {"spot": {"total": 0, "correct": 0}}


def save_stats(stats):
    with open(STATS_FILE, "w") as f:
        json.dump(stats, f)


def accuracy_line(stats):
    t = stats["spot"]["total"]
    c = stats["spot"]["correct"]
    if t == 0:
        return "нет данных"
    return f"{c}/{t} ({c/t*100:.0f}%)"


# ── API ───────────────────────────────────────────────────────────────────────

def get_spot():
    r = requests.get(SPOT_URL, timeout=10)
    r.raise_for_status()
    return float(r.json()["price"])


def get_futures():
    r = requests.get(FUTURES_URL, timeout=10)
    r.raise_for_status()
    return float(r.json()["price"])


def get_orderbook(url):
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    return r.json()


def get_all_data():
    with ThreadPoolExecutor(max_workers=4) as ex:
        f_spot    = ex.submit(get_spot)
        f_futures = ex.submit(get_futures)
        f_spot_bk = ex.submit(get_orderbook, SPOT_DEPTH_URL)
        f_fut_bk  = ex.submit(get_orderbook, FUTURES_DEPTH_URL)
        return f_spot.result(), f_futures.result(), f_spot_bk.result(), f_fut_bk.result()


def analyze_book(book, mid_price):
    low  = mid_price * (1 - BOOK_DEPTH / 100)
    high = mid_price * (1 + BOOK_DEPTH / 100)
    bid_vol = sum(float(p) * float(q) for p, q in book["bids"] if float(p) >= low)
    ask_vol = sum(float(p) * float(q) for p, q in book["asks"] if float(p) <= high)
    return bid_vol, ask_vol


def get_signal(bid_vol, ask_vol):
    total = bid_vol + ask_vol
    if total == 0:
        return "balance", 50.0, 50.0
    bid_pct = bid_vol / total * 100
    ask_pct = ask_vol / total * 100
    if bid_pct >= SIGNAL_PCT:
        return "buy", bid_pct, ask_pct
    elif ask_pct >= SIGNAL_PCT:
        return "sell", bid_pct, ask_pct
    return "balance", bid_pct, ask_pct


def send_telegram(text):
    payload = {"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}
    requests.post(TG_URL, json=payload, timeout=10)


# ── Проверка результата через 10 секунд ───────────────────────────────────────

def check_result(signal, price_at_signal, stats):
    time.sleep(CHECK_DELAY)
    try:
        current_price = get_spot()
        correct = (signal == "buy" and current_price > price_at_signal) or \
                  (signal == "sell" and current_price < price_at_signal)

        with stats_lock:
            stats["spot"]["total"] += 1
            if correct:
                stats["spot"]["correct"] += 1
            save_stats(stats)
            acc = accuracy_line(stats)

        diff = current_price - price_at_signal
        diff_pct = diff / price_at_signal * 100
        diff_sign = "+" if diff >= 0 else ""

        if correct:
            verdict = "✅ <b>ОТЫГРАЛО</b>"
        else:
            verdict = "❌ <b>НЕ ОТЫГРАЛО</b>"

        signal_label = "🟢 ПОКУПКА" if signal == "buy" else "🔴 ПРОДАЖА"

        msg = (
            f"{verdict}\n\n"
            f"Колл: {signal_label}\n"
            f"Цена при колле: <code>{price_at_signal:.6f}</code>\n"
            f"Цена через {CHECK_DELAY}с:  <code>{current_price:.6f}</code>\n"
            f"Движение: <b>{diff_sign}{diff_pct:.4f}%</b>\n\n"
            f"📊 Точность коллов: <b>{acc}</b>"
        )
        send_telegram(msg)
        print(f"[check] signal={signal} was={'correct' if correct else 'wrong'} acc={acc}")

    except Exception as e:
        print(f"[check] Error: {e}")


# ── Сообщение ─────────────────────────────────────────────────────────────────

def build_message(spot, futures, spread_pct,
                  spot_bid, spot_ask, spot_sig, spot_bid_pct, spot_ask_pct,
                  fut_bid, fut_ask, fut_sig, fut_bid_pct, fut_ask_pct,
                  stats, now):

    spread_sign  = "+" if spread_pct >= 0 else ""
    spread_arrow = "🔼" if spread_pct > 0 else ("🔽" if spread_pct < 0 else "➡️")

    if spread_pct > 4.0:
        spread_label = "  ⚠️ спред высокий"
    elif spread_pct < 0:
        spread_label = "  🔵 спред отрицательный"
    else:
        spread_label = ""

    SIGNAL_LABEL = {"buy": "🟢 ПОКУПКА", "sell": "🔴 ПРОДАЖА", "balance": "⚪️ баланс"}

    spot_block = (
        f"<b>Спот стакан (±2%):</b>\n"
        f"  🟢 Биды: <code>${spot_bid:,.0f}</code> ({spot_bid_pct:.0f}%)\n"
        f"  🔴 Аски: <code>${spot_ask:,.0f}</code> ({spot_ask_pct:.0f}%)\n"
        f"  → {SIGNAL_LABEL[spot_sig]}"
    )

    fut_block = (
        f"<b>Фьючерс стакан (±2%):</b>\n"
        f"  🟢 Биды: <code>${fut_bid:,.0f}</code> ({fut_bid_pct:.0f}%)\n"
        f"  🔴 Аски: <code>${fut_ask:,.0f}</code> ({fut_ask_pct:.0f}%)\n"
        f"  → {SIGNAL_LABEL[fut_sig]}"
    )

    return (
        f"🟡 <b>FF — {now}</b>\n\n"
        f"Спот:     <code>{spot:.6f}</code>\n"
        f"Фьючерс: <code>{futures:.6f}</code>\n"
        f"{spread_arrow} Спред: <b>{spread_sign}{spread_pct:.4f}%</b>{spread_label}\n\n"
        f"{spot_block}\n\n"
        f"{fut_block}\n\n"
        f"📊 Точность коллов: <b>{accuracy_line(stats)}</b>\n"
        f"⏱ Результат через {CHECK_DELAY} сек..."
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("Starting FF spread parser...")
    stats = load_stats()

    prev_spot_sig = None

    while True:
        try:
            spot, futures, spot_book, fut_book = get_all_data()
            avg        = (spot + futures) / 2
            spread_pct = ((spot - futures) / avg) * 100
            now        = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            spot_bid, spot_ask = analyze_book(spot_book, spot)
            fut_bid,  fut_ask  = analyze_book(fut_book,  futures)

            spot_sig, spot_bid_pct, spot_ask_pct = get_signal(spot_bid, spot_ask)
            fut_sig,  fut_bid_pct,  fut_ask_pct  = get_signal(fut_bid,  fut_ask)

            trigger = spot_sig != "balance" and spot_sig != prev_spot_sig

            if trigger:
                msg = build_message(
                    spot, futures, spread_pct,
                    spot_bid, spot_ask, spot_sig, spot_bid_pct, spot_ask_pct,
                    fut_bid,  fut_ask,  fut_sig,  fut_bid_pct,  fut_ask_pct,
                    stats, now
                )
                send_telegram(msg)
                print(f"[{now}] spot={spot_sig}({spot_bid_pct:.0f}%b) — отправлено, проверка через {CHECK_DELAY}с")

                # Запускаем проверку результата в фоне
                threading.Thread(
                    target=check_result,
                    args=(spot_sig, spot, stats),
                    daemon=True
                ).start()

                prev_spot_sig = spot_sig
            else:
                print(f"[{now}] spot={spot_sig}({spot_bid_pct:.0f}%b) — пропущено")

        except requests.RequestException as e:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Error: {e}")
        except Exception as e:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Error: {e}")

        time.sleep(1)


if __name__ == "__main__":
    main()
