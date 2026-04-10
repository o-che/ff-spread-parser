import requests
import time
import os
import json
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

THRESHOLD = 1.0
BOOK_DEPTH = 2.0
STATS_FILE = "stats.json"


# ── Статистика ────────────────────────────────────────────────────────────────

def load_stats():
    if os.path.exists(STATS_FILE):
        with open(STATS_FILE) as f:
            return json.load(f)
    return {
        "spot":    {"total": 0, "correct": 0},
        "futures": {"total": 0, "correct": 0},
    }


def save_stats(stats):
    with open(STATS_FILE, "w") as f:
        json.dump(stats, f)


def record_result(stats, market, signal, prev_price, curr_price):
    """Записывает был ли предыдущий колл правильным."""
    if signal in ("buy", "sell"):
        correct = (signal == "buy" and curr_price > prev_price) or \
                  (signal == "sell" and curr_price < prev_price)
        stats[market]["total"] += 1
        if correct:
            stats[market]["correct"] += 1
        save_stats(stats)
        return correct
    return None


def accuracy_line(stats, market):
    t = stats[market]["total"]
    c = stats[market]["correct"]
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
        f_spot     = ex.submit(get_spot)
        f_futures  = ex.submit(get_futures)
        f_spot_bk  = ex.submit(get_orderbook, SPOT_DEPTH_URL)
        f_fut_bk   = ex.submit(get_orderbook, FUTURES_DEPTH_URL)
        return f_spot.result(), f_futures.result(), f_spot_bk.result(), f_fut_bk.result()


def analyze_book(book, mid_price):
    low  = mid_price * (1 - BOOK_DEPTH / 100)
    high = mid_price * (1 + BOOK_DEPTH / 100)
    bid_vol = sum(float(p) * float(q) for p, q in book["bids"] if float(p) >= low)
    ask_vol = sum(float(p) * float(q) for p, q in book["asks"] if float(p) <= high)
    return bid_vol, ask_vol


def get_signal_key(bid_vol, ask_vol):
    if bid_vol > ask_vol * 1.2:
        return "buy"
    elif ask_vol > bid_vol * 1.2:
        return "sell"
    return "balance"


def send_telegram(text):
    payload = {"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}
    requests.post(TG_URL, json=payload, timeout=10)


# ── Сообщение ─────────────────────────────────────────────────────────────────

SIGNAL_LABEL = {
    "buy":     "🟢 покупка",
    "sell":    "🔴 продажа",
    "balance": "⚪️ баланс",
}


def book_block(bid_vol, ask_vol, signal_key, prev_result, acc_line, title):
    total = bid_vol + ask_vol
    bid_pct = bid_vol / total * 100 if total else 0
    ask_pct = ask_vol / total * 100 if total else 0

    if prev_result is None:
        result_line = "  ↩️ Предыдущий колл: <i>первый</i>"
    elif prev_result:
        result_line = "  ✅ Предыдущий колл: <b>верный</b>"
    else:
        result_line = "  ❌ Предыдущий колл: <b>неверный</b>"

    return (
        f"<b>{title} (±2%):</b>\n"
        f"  🟢 Биды: <code>${bid_vol:,.0f}</code> ({bid_pct:.0f}%)\n"
        f"  🔴 Аски: <code>${ask_vol:,.0f}</code> ({ask_pct:.0f}%)\n"
        f"  → {SIGNAL_LABEL[signal_key]}\n"
        f"{result_line}\n"
        f"  📊 Точность: <b>{acc_line}</b>"
    )


def build_message(spot, futures, spread_pct, prev_spread_pct,
                  spot_book, fut_book, stats,
                  spot_prev_result, fut_prev_result, now):

    sign  = "+" if spread_pct >= 0 else ""
    arrow = "🔼" if spread_pct > 0 else ("🔽" if spread_pct < 0 else "➡️")

    if prev_spread_pct is None:
        change_line = "📊 Изменение спреда: <i>первый запуск</i>"
    else:
        delta = spread_pct - prev_spread_pct
        delta_sign = "+" if delta >= 0 else ""
        change_arrow = "📈" if delta > 0 else ("📉" if delta < 0 else "➡️")
        change_line = f"{change_arrow} Изменение спреда: <b>{delta_sign}{delta:.4f}%</b>"

    if spread_pct > 4.0:
        label = "\n⚠️ <b>СПРЕД ВЫСОКИЙ (&gt;4%)</b>"
    elif spread_pct < 0:
        label = "\n🔵 <b>СПРЕД ОТРИЦАТЕЛЬНЫЙ</b>"
    else:
        label = ""

    spot_bid, spot_ask   = analyze_book(spot_book, spot)
    fut_bid,  fut_ask    = analyze_book(fut_book,  futures)
    spot_sig = get_signal_key(spot_bid, spot_ask)
    fut_sig  = get_signal_key(fut_bid,  fut_ask)

    spot_block = book_block(spot_bid, spot_ask, spot_sig,
                            spot_prev_result, accuracy_line(stats, "spot"), "Спот")
    fut_block  = book_block(fut_bid,  fut_ask,  fut_sig,
                            fut_prev_result,  accuracy_line(stats, "futures"), "Фьючерс")

    return (
        f"🟡 <b>FF Spread (Binance) — {now}</b>\n\n"
        f"Спот:     <code>{spot:.6f}</code>\n"
        f"Фьючерс: <code>{futures:.6f}</code>\n\n"
        f"{arrow} Спред: <b>{sign}{spread_pct:.4f}%</b>\n"
        f"{change_line}{label}\n\n"
        f"{spot_block}\n\n"
        f"{fut_block}"
    ), spot_sig, fut_sig


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("Starting FF spread parser...")
    stats          = load_stats()
    prev_spread    = None
    prev_spot_sig  = None
    prev_fut_sig   = None
    prev_spot_price = None
    prev_fut_price  = None

    while True:
        try:
            spot, futures, spot_book, fut_book = get_all_data()
            avg        = (spot + futures) / 2
            spread_pct = ((spot - futures) / avg) * 100
            now        = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            if prev_spread is None or abs(spread_pct - prev_spread) >= THRESHOLD:

                # Проверяем предыдущие коллы
                spot_prev_result = None
                fut_prev_result  = None

                if prev_spot_sig is not None and prev_spot_price is not None:
                    spot_prev_result = record_result(
                        stats, "spot", prev_spot_sig, prev_spot_price, spot)

                if prev_fut_sig is not None and prev_fut_price is not None:
                    fut_prev_result = record_result(
                        stats, "futures", prev_fut_sig, prev_fut_price, futures)

                msg, spot_sig, fut_sig = build_message(
                    spot, futures, spread_pct, prev_spread,
                    spot_book, fut_book, stats,
                    spot_prev_result, fut_prev_result, now
                )
                send_telegram(msg)
                print(f"[{now}] spread={spread_pct:+.4f}% spot_sig={spot_sig} fut_sig={fut_sig} — отправлено")

                prev_spread     = spread_pct
                prev_spot_sig   = spot_sig
                prev_fut_sig    = fut_sig
                prev_spot_price = spot
                prev_fut_price  = futures

            else:
                print(f"[{now}] spread={spread_pct:+.4f}% — пропущено")

        except requests.RequestException as e:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Error: {e}")
        except Exception as e:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Error: {e}")

        time.sleep(1)


if __name__ == "__main__":
    main()
