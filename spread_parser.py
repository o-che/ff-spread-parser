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

BOOK_DEPTH = 3.0       # анализируем ±3% от цены
SIGNAL_PCT = 60.0      # порог для колла (%)
COOLDOWN   = 10        # минимум секунд между алертами
STATS_FILE = "stats.json"

LABEL = {"buy": "🟢 ПОКУПКА", "sell": "🔴 ПРОДАЖА", "balance": "⚪️ баланс"}


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


def acc(stats, market):
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


# ── Сообщение ─────────────────────────────────────────────────────────────────

def build_message(spot, futures, spread_pct,
                  spot_sig, spot_bid_pct, spot_ask_pct, spot_bid, spot_ask,
                  fut_sig,  fut_bid_pct,  fut_ask_pct,  fut_bid,  fut_ask,
                  prev_result_line, stats, now):

    spread_sign  = "+" if spread_pct >= 0 else ""
    spread_arrow = "🔼" if spread_pct > 0 else ("🔽" if spread_pct < 0 else "➡️")
    spread_label = ""
    if spread_pct > 4.0:
        spread_label = "  ⚠️ спред высокий"
    elif spread_pct < 0:
        spread_label = "  🔵 спред отрицательный"

    spot_block = (
        f"<b>Спот (±3%):</b>\n"
        f"  🟢 Биды: <code>${spot_bid:,.0f}</code> ({spot_bid_pct:.0f}%)\n"
        f"  🔴 Аски: <code>${spot_ask:,.0f}</code> ({spot_ask_pct:.0f}%)\n"
        f"  → {LABEL[spot_sig]}\n"
        f"  📊 Точность: <b>{acc(stats, 'spot')}</b>"
    )

    fut_block = (
        f"<b>Фьючерс (±3%):</b>\n"
        f"  🟢 Биды: <code>${fut_bid:,.0f}</code> ({fut_bid_pct:.0f}%)\n"
        f"  🔴 Аски: <code>${fut_ask:,.0f}</code> ({fut_ask_pct:.0f}%)\n"
        f"  → {LABEL[fut_sig]}\n"
        f"  📊 Точность: <b>{acc(stats, 'futures')}</b>"
    )

    msg = (
        f"🟡 <b>FF — {now}</b>\n\n"
        f"Спот:     <code>{spot:.6f}</code>\n"
        f"Фьючерс: <code>{futures:.6f}</code>\n"
        f"{spread_arrow} Спред: <b>{spread_sign}{spread_pct:.4f}%</b>{spread_label}\n\n"
        f"{spot_block}\n\n"
        f"{fut_block}"
    )
    if prev_result_line:
        msg += f"\n\n{prev_result_line}"
    return msg


# ── Main ──────────────────────────────────────────────────────────────────────

def evaluate(sig, old_p, cur_p, market, stats):
    if sig == "balance":
        return None
    correct = (sig == "buy" and cur_p > old_p) or \
              (sig == "sell" and cur_p < old_p)
    stats[market]["total"] += 1
    if correct:
        stats[market]["correct"] += 1
    return correct


def main():
    print("Starting FF spread parser...")
    stats = load_stats()

    prev_spot_sig   = None
    prev_fut_sig    = None
    prev_spot_price = None
    prev_fut_price  = None
    last_alert_time = 0

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

            elapsed = time.time() - last_alert_time
            trigger = (
                spot_sig != "balance"
                and spot_sig != prev_spot_sig
                and elapsed >= COOLDOWN
            )

            if trigger:
                # Считаем результат предыдущих коллов
                prev_result_line = ""
                if prev_spot_sig is not None and prev_spot_price is not None:
                    spot_ok = evaluate(prev_spot_sig, prev_spot_price, spot, "spot", stats)
                    fut_ok  = evaluate(prev_fut_sig,  prev_fut_price,  futures, "futures", stats)
                    save_stats(stats)

                    parts = []
                    if spot_ok is not None:
                        diff_pct = (spot - prev_spot_price) / prev_spot_price * 100
                        sign = "+" if diff_pct >= 0 else ""
                        parts.append(
                            f"Спот {LABEL[prev_spot_sig]}: "
                            f"{'✅' if spot_ok else '❌'} ({sign}{diff_pct:.3f}%)"
                        )
                    if fut_ok is not None:
                        diff_pct = (futures - prev_fut_price) / prev_fut_price * 100
                        sign = "+" if diff_pct >= 0 else ""
                        parts.append(
                            f"Фьючерс {LABEL[prev_fut_sig]}: "
                            f"{'✅' if fut_ok else '❌'} ({sign}{diff_pct:.3f}%)"
                        )
                    if parts:
                        prev_result_line = "↩️ <b>Предыдущий колл:</b>\n" + "\n".join(parts)

                msg = build_message(
                    spot, futures, spread_pct,
                    spot_sig, spot_bid_pct, spot_ask_pct, spot_bid, spot_ask,
                    fut_sig,  fut_bid_pct,  fut_ask_pct,  fut_bid,  fut_ask,
                    prev_result_line, stats, now
                )
                send_telegram(msg)
                print(f"[{now}] spot={spot_sig}({spot_bid_pct:.0f}%) — отправлено")

                prev_spot_sig   = spot_sig
                prev_fut_sig    = fut_sig
                prev_spot_price = spot
                prev_fut_price  = futures
                last_alert_time = time.time()
            else:
                print(f"[{now}] spot={spot_sig}({spot_bid_pct:.0f}%) — пропущено")

        except requests.RequestException as e:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Error: {e}")
        except Exception as e:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Error: {e}")

        time.sleep(1)


if __name__ == "__main__":
    main()
