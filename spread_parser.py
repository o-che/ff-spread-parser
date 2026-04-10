import requests
import time
import os
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

THRESHOLD = 1.0   # алерт при изменении спреда на 1%
BOOK_DEPTH = 2.0  # анализ стакана в пределах ±2% от цены


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


def analyze_book(book, mid_price):
    """Считает объём бидов и асков в пределах BOOK_DEPTH% от mid_price."""
    low = mid_price * (1 - BOOK_DEPTH / 100)
    high = mid_price * (1 + BOOK_DEPTH / 100)

    bid_vol = sum(float(p) * float(q) for p, q in book["bids"] if float(p) >= low)
    ask_vol = sum(float(p) * float(q) for p, q in book["asks"] if float(p) <= high)
    return bid_vol, ask_vol


def book_summary(bid_vol, ask_vol, label):
    total = bid_vol + ask_vol
    if total == 0:
        return f"<b>{label}:</b> нет данных"

    bid_pct = bid_vol / total * 100
    ask_pct = ask_vol / total * 100

    if bid_vol > ask_vol * 1.2:
        pressure = "🟢 покупка"
    elif ask_vol > bid_vol * 1.2:
        pressure = "🔴 продажа"
    else:
        pressure = "⚪️ баланс"

    return (
        f"<b>{label} (±2%):</b>\n"
        f"  🟢 Биды: <code>${bid_vol:,.0f}</code> ({bid_pct:.0f}%)\n"
        f"  🔴 Аски: <code>${ask_vol:,.0f}</code> ({ask_pct:.0f}%)\n"
        f"  → {pressure}"
    )


def get_all_data():
    with ThreadPoolExecutor(max_workers=4) as ex:
        f_spot = ex.submit(get_spot)
        f_futures = ex.submit(get_futures)
        f_spot_book = ex.submit(get_orderbook, SPOT_DEPTH_URL)
        f_fut_book = ex.submit(get_orderbook, FUTURES_DEPTH_URL)
        return f_spot.result(), f_futures.result(), f_spot_book.result(), f_fut_book.result()


def send_telegram(text):
    payload = {"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}
    requests.post(TG_URL, json=payload, timeout=10)


def build_message(spot, futures, spread_pct, prev_spread_pct, spot_book, fut_book, now):
    sign = "+" if spread_pct >= 0 else ""
    arrow = "🔼" if spread_pct > 0 else ("🔽" if spread_pct < 0 else "➡️")

    if prev_spread_pct is None:
        change_line = "📊 Изменение спреда: <i>первый запуск</i>"
    else:
        delta = spread_pct - prev_spread_pct
        delta_sign = "+" if delta >= 0 else ""
        change_arrow = "📈" if delta > 0 else ("📉" if delta < 0 else "➡️")
        change_line = f"{change_arrow} Изменение спреда: <b>{delta_sign}{delta:.4f}%</b>"

    if spread_pct > 4.0:
        label = "⚠️ <b>СПРЕД ВЫСОКИЙ (&gt;4%)</b>"
    elif spread_pct < 0:
        label = "🔵 <b>СПРЕД ОТРИЦАТЕЛЬНЫЙ</b>"
    else:
        label = ""

    spot_bid, spot_ask = analyze_book(spot_book, spot)
    fut_bid, fut_ask = analyze_book(fut_book, futures)

    spot_book_str = book_summary(spot_bid, spot_ask, "Спот")
    fut_book_str = book_summary(fut_bid, fut_ask, "Фьючерс")

    msg = (
        f"🟡 <b>FF Spread (Binance) — {now}</b>\n\n"
        f"Спот:     <code>{spot:.6f}</code>\n"
        f"Фьючерс: <code>{futures:.6f}</code>\n\n"
        f"{arrow} Спред: <b>{sign}{spread_pct:.4f}%</b>\n"
        f"{change_line}"
    )
    if label:
        msg += f"\n\n{label}"
    msg += f"\n\n{spot_book_str}\n\n{fut_book_str}"
    return msg


def main():
    print("Starting FF spread parser...")
    prev_spread_pct = None
    while True:
        try:
            spot, futures, spot_book, fut_book = get_all_data()
            avg = (spot + futures) / 2
            spread_pct = ((spot - futures) / avg) * 100
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            if prev_spread_pct is None or abs(spread_pct - prev_spread_pct) >= THRESHOLD:
                msg = build_message(spot, futures, spread_pct, prev_spread_pct, spot_book, fut_book, now)
                send_telegram(msg)
                print(f"[{now}] SPOT={spot:.6f} FUT={futures:.6f} spread={spread_pct:+.4f}% — отправлено")
                prev_spread_pct = spread_pct
            else:
                print(f"[{now}] SPOT={spot:.6f} FUT={futures:.6f} spread={spread_pct:+.4f}% — пропущено")

        except requests.RequestException as e:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Error: {e}")
        except Exception as e:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Error: {e}")

        time.sleep(1)


if __name__ == "__main__":
    main()
