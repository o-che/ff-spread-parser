import requests
import time
import os
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

SYMBOL = "FFUSDT"
SPOT_URL = f"https://api.binance.com/api/v3/ticker/price?symbol={SYMBOL}"
FUTURES_URL = f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={SYMBOL}"

TG_TOKEN = os.environ["TG_TOKEN"]
TG_CHAT_ID = os.environ["TG_CHAT_ID"]
TG_URL = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"

THRESHOLD = 0.5  # минимальное изменение спреда (%) для отправки алерта


def get_spot():
    r = requests.get(SPOT_URL, timeout=10)
    r.raise_for_status()
    return float(r.json()["price"])


def get_futures():
    r = requests.get(FUTURES_URL, timeout=10)
    r.raise_for_status()
    return float(r.json()["price"])


def get_both_prices():
    with ThreadPoolExecutor(max_workers=2) as executor:
        f_spot = executor.submit(get_spot)
        f_futures = executor.submit(get_futures)
        return f_spot.result(), f_futures.result()


def send_telegram(text):
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }
    requests.post(TG_URL, json=payload, timeout=10)


def build_message(spot, futures, spread_pct, prev_spread_pct, now):
    sign = "+" if spread_pct >= 0 else ""
    arrow = "🔼" if spread_pct > 0 else ("🔽" if spread_pct < 0 else "➡️")

    if prev_spread_pct is None:
        change_line = "📊 Изменение спреда: <i>первый запуск</i>"
    else:
        delta = spread_pct - prev_spread_pct
        delta_sign = "+" if delta >= 0 else ""
        change_arrow = "📈" if delta > 0 else ("📉" if delta < 0 else "➡️")
        change_line = f"{change_arrow} Изменение спреда: <b>{delta_sign}{delta:.4f}%</b>"

    return (
        f"🟡 <b>FF Spread (Binance) — {now}</b>\n\n"
        f"Спот:     <code>{spot:.6f}</code>\n"
        f"Фьючерс: <code>{futures:.6f}</code>\n\n"
        f"{arrow} Спред: <b>{sign}{spread_pct:.4f}%</b>\n"
        f"{change_line}"
    )


def main():
    print("Starting FF spread parser...")
    prev_spread_pct = None
    while True:
        try:
            spot, futures = get_both_prices()
            avg = (spot + futures) / 2
            spread_pct = ((spot - futures) / avg) * 100
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            if prev_spread_pct is None or abs(spread_pct - prev_spread_pct) >= THRESHOLD:
                msg = build_message(spot, futures, spread_pct, prev_spread_pct, now)
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
