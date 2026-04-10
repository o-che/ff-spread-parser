import requests
import time
import os
import json
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

SYMBOL = "FFUSDT"
SPOT_URL          = f"https://api.binance.com/api/v3/ticker/price?symbol={SYMBOL}"
FUTURES_URL       = f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={SYMBOL}"
SPOT_DEPTH_URL    = f"https://api.binance.com/api/v3/depth?symbol={SYMBOL}&limit=1000"
FUTURES_DEPTH_URL = f"https://fapi.binance.com/fapi/v1/depth?symbol={SYMBOL}&limit=1000"

TG_TOKEN = os.environ["TG_TOKEN"]
TG_URL   = f"https://api.telegram.org/bot{TG_TOKEN}"

SIGNAL_PCT = 60.0
CHECKPOINTS = [5, 15, 30, 60]   # секунд после сигнала

CHANNELS = [
    {"chat_id": "-1003793887302", "depth": 2.0, "cooldown": 10,  "stats_file": "stats_2pct.json"},
    {"chat_id": "-1003561488618", "depth": 3.0, "cooldown": 10,  "stats_file": "stats_3pct.json"},
    {"chat_id": "-1003953760432", "depth": 5.0, "cooldown": 60,  "stats_file": "stats_5pct.json"},
]

LABEL = {"buy": "🟢 ПОКУПКА", "sell": "🔴 ПРОДАЖА", "balance": "⚪️ баланс"}


# ── Stats ─────────────────────────────────────────────────────────────────────

def load_stats(path):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {"spot": {"total": 0, "correct": 0}, "futures": {"total": 0, "correct": 0}}


def save_stats(path, stats):
    with open(path, "w") as f:
        json.dump(stats, f)


def acc(stats, market):
    t = stats[market]["total"]
    c = stats[market]["correct"]
    if t == 0:
        return "нет данных"
    return f"{c}/{t} ({c/t*100:.0f}%)"


# ── Telegram ──────────────────────────────────────────────────────────────────

def send_telegram(chat_id, text):
    r = requests.post(f"{TG_URL}/sendMessage", json={
        "chat_id": chat_id, "text": text, "parse_mode": "HTML"
    }, timeout=10)
    data = r.json()
    if data.get("ok"):
        return data["result"]["message_id"]
    return None


def edit_telegram(chat_id, message_id, text):
    requests.post(f"{TG_URL}/editMessageText", json={
        "chat_id": chat_id, "message_id": message_id,
        "text": text, "parse_mode": "HTML"
    }, timeout=10)


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
        fs = ex.submit(get_spot)
        ff = ex.submit(get_futures)
        fb = ex.submit(get_orderbook, SPOT_DEPTH_URL)
        fd = ex.submit(get_orderbook, FUTURES_DEPTH_URL)
        return fs.result(), ff.result(), fb.result(), fd.result()


def analyze_book(book, mid_price, depth_pct):
    low  = mid_price * (1 - depth_pct / 100)
    high = mid_price * (1 + depth_pct / 100)
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


# ── Dynamic result tracking ───────────────────────────────────────────────────

def track_result(chat_id, message_id, spot_sig, fut_sig,
                 spot_price, fut_price, base_text, stats, stats_file, stats_lock):
    results = []
    prev_checkpoint = 0

    for cp in CHECKPOINTS:
        time.sleep(cp - prev_checkpoint)
        prev_checkpoint = cp
        try:
            cur_spot = get_spot()
            diff = cur_spot - spot_price
            diff_pct = diff / spot_price * 100
            sign = "+" if diff_pct >= 0 else ""

            if spot_sig == "buy":
                ok = cur_spot > spot_price
            elif spot_sig == "sell":
                ok = cur_spot < spot_price
            else:
                ok = None

            verdict = ("✅" if ok else "❌") if ok is not None else "➖"
            results.append(
                f"⏱ +{cp}с:  <code>{cur_spot:.6f}</code>  "
                f"(<b>{sign}{diff_pct:.3f}%</b>)  {verdict}"
            )

            # На последнем чекпоинте записываем в статистику
            if cp == CHECKPOINTS[-1]:
                with stats_lock:
                    if spot_sig != "balance":
                        stats["spot"]["total"] += 1
                        if ok:
                            stats["spot"]["correct"] += 1
                    if fut_sig != "balance":
                        try:
                            cur_fut = get_futures()
                            fut_ok = (fut_sig == "buy" and cur_fut > fut_price) or \
                                     (fut_sig == "sell" and cur_fut < fut_price)
                            stats["futures"]["total"] += 1
                            if fut_ok:
                                stats["futures"]["correct"] += 1
                        except Exception:
                            pass
                    save_stats(stats_file, stats)

            result_block = "\n".join(results)
            edit_telegram(chat_id, message_id, base_text + f"\n\n{result_block}")

        except Exception as e:
            print(f"[track] Error at +{cp}s: {e}")


# ── Message builder ───────────────────────────────────────────────────────────

def build_message(spot, futures, spread_pct,
                  spot_sig, spot_bid_pct, spot_ask_pct, spot_bid, spot_ask,
                  fut_sig,  fut_bid_pct,  fut_ask_pct,  fut_bid,  fut_ask,
                  depth_pct, stats, prev_result_line, now):

    spread_sign  = "+" if spread_pct >= 0 else ""
    spread_arrow = "🔼" if spread_pct > 0 else ("🔽" if spread_pct < 0 else "➡️")
    spread_label = ""
    if spread_pct > 4.0:
        spread_label = "  ⚠️ спред высокий"
    elif spread_pct < 0:
        spread_label = "  🔵 спред отрицательный"

    depth_str = f"±{depth_pct:.0f}%"

    spot_block = (
        f"<b>Спот ({depth_str}):</b>\n"
        f"  🟢 Биды: <code>${spot_bid:,.0f}</code> ({spot_bid_pct:.0f}%)\n"
        f"  🔴 Аски: <code>${spot_ask:,.0f}</code> ({spot_ask_pct:.0f}%)\n"
        f"  → {LABEL[spot_sig]}\n"
        f"  📊 Точность: <b>{acc(stats, 'spot')}</b>"
    )

    fut_block = (
        f"<b>Фьючерс ({depth_str}):</b>\n"
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


# ── Channel worker ────────────────────────────────────────────────────────────

class ChannelWorker:
    def __init__(self, cfg):
        self.chat_id    = cfg["chat_id"]
        self.depth      = cfg["depth"]
        self.cooldown   = cfg["cooldown"]
        self.stats_file = cfg["stats_file"]
        self.stats      = load_stats(self.stats_file)
        self.stats_lock = threading.Lock()

        self.prev_spot_sig   = None
        self.prev_fut_sig    = None
        self.prev_spot_price = None
        self.prev_fut_price  = None
        self.last_alert_time = 0

    def process(self, spot, futures, spot_book, fut_book):
        spot_bid, spot_ask = analyze_book(spot_book, spot, self.depth)
        fut_bid,  fut_ask  = analyze_book(fut_book,  futures, self.depth)

        spot_sig, spot_bid_pct, spot_ask_pct = get_signal(spot_bid, spot_ask)
        fut_sig,  fut_bid_pct,  fut_ask_pct  = get_signal(fut_bid,  fut_ask)

        elapsed = time.time() - self.last_alert_time
        trigger = (
            spot_sig != "balance"
            and spot_sig != self.prev_spot_sig
            and elapsed >= self.cooldown
        )

        if not trigger:
            return

        avg        = (spot + futures) / 2
        spread_pct = ((spot - futures) / avg) * 100
        now        = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Результат предыдущего колла
        prev_result_line = ""
        if self.prev_spot_sig is not None:
            parts = []
            if self.prev_spot_sig != "balance":
                diff_pct = (spot - self.prev_spot_price) / self.prev_spot_price * 100
                ok = (self.prev_spot_sig == "buy" and spot > self.prev_spot_price) or \
                     (self.prev_spot_sig == "sell" and spot < self.prev_spot_price)
                sign = "+" if diff_pct >= 0 else ""
                parts.append(f"Спот {LABEL[self.prev_spot_sig]}: {'✅' if ok else '❌'} ({sign}{diff_pct:.3f}%)")
            if self.prev_fut_sig and self.prev_fut_sig != "balance":
                diff_pct = (futures - self.prev_fut_price) / self.prev_fut_price * 100
                ok = (self.prev_fut_sig == "buy" and futures > self.prev_fut_price) or \
                     (self.prev_fut_sig == "sell" and futures < self.prev_fut_price)
                sign = "+" if diff_pct >= 0 else ""
                parts.append(f"Фьючерс {LABEL[self.prev_fut_sig]}: {'✅' if ok else '❌'} ({sign}{diff_pct:.3f}%)")
            if parts:
                prev_result_line = "↩️ <b>Предыдущий колл:</b>\n" + "\n".join(parts)

        base_text = build_message(
            spot, futures, spread_pct,
            spot_sig, spot_bid_pct, spot_ask_pct, spot_bid, spot_ask,
            fut_sig,  fut_bid_pct,  fut_ask_pct,  fut_bid,  fut_ask,
            self.depth, self.stats, prev_result_line, now
        )

        message_id = send_telegram(self.chat_id, base_text)

        if message_id:
            threading.Thread(
                target=track_result,
                args=(self.chat_id, message_id, spot_sig, fut_sig,
                      spot, futures, base_text, self.stats, self.stats_file, self.stats_lock),
                daemon=True
            ).start()

        print(f"[{now}] depth={self.depth}% chat={self.chat_id} spot={spot_sig}({spot_bid_pct:.0f}%) — отправлено")

        self.prev_spot_sig   = spot_sig
        self.prev_fut_sig    = fut_sig
        self.prev_spot_price = spot
        self.prev_fut_price  = futures
        self.last_alert_time = time.time()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("Starting FF spread parser (3 channels)...")
    workers = [ChannelWorker(cfg) for cfg in CHANNELS]

    while True:
        try:
            spot, futures, spot_book, fut_book = get_all_data()
            for w in workers:
                try:
                    w.process(spot, futures, spot_book, fut_book)
                except Exception as e:
                    print(f"[worker {w.depth}%] Error: {e}")
        except requests.RequestException as e:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] API Error: {e}")
        except Exception as e:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Error: {e}")

        time.sleep(1)


if __name__ == "__main__":
    main()
