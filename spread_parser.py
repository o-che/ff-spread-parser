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

ALERT_THRESHOLD = 3.0   # алерт только если |спред| >= 3%
REPEAT_DELTA = 0.5      # повторный алерт если спред изменился на 0.5% от последнего


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


def get_signal(spread_pct):
    # Спот > Фьючерс: спот перегрет → SHORT (продавай спот / покупай фьючерс)
    # Фьючерс > Спот: фьючерс перегрет → LONG (покупай спот / продавай фьючерс)
    if spread_pct >= ALERT_THRESHOLD:
        return "🔴 <b>ШОРТ</b> — спот перегрет относительно фьючерса"
    elif spread_pct <= -ALERT_THRESHOLD:
        return "🟢 <b>ЛОНГ</b> — фьючерс перегрет относительно спота"
    return None


def build_message(spot, futures, spread_pct, prev_spread_pct, signal, now):
    sign = "+" if spread_pct >= 0 else ""
    arrow = "🔼" if spread_pct > 0 else "🔽"

    if prev_spread_pct is None:
        change_line = "📊 Изменение спреда: <i>первый сигнал</i>"
    else:
        delta = spread_pct - prev_spread_pct
        delta_sign = "+" if delta >= 0 else ""
        change_arrow = "📈" if delta > 0 else "📉"
        change_line = f"{change_arrow} Изменение спреда: <b>{delta_sign}{delta:.4f}%</b>"

    return (
        f"🟡 <b>FF Spread (Binance) — {now}</b>\n\n"
        f"Спот:     <code>{spot:.6f}</code>\n"
        f"Фьючерс: <code>{futures:.6f}</code>\n\n"
        f"{arrow} Спред: <b>{sign}{spread_pct:.4f}%</b>\n"
        f"{change_line}\n\n"
        f"Сигнал: {signal}"
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

            signal = get_signal(spread_pct)

            if signal is not None:
                should_send = (
                    prev_spread_pct is None
                    or abs(spread_pct - prev_spread_pct) >= REPEAT_DELTA
                )
                if should_send:
                    msg = build_message(spot, futures, spread_pct, prev_spread_pct, signal, now)
                    send_telegram(msg)
                    print(f"[{now}] SPOT={spot:.6f} FUT={futures:.6f} spread={spread_pct:+.4f}% — {signal[:10]}... отправлено")
                    prev_spread_pct = spread_pct
                else:
                    print(f"[{now}] SPOT={spot:.6f} FUT={futures:.6f} spread={spread_pct:+.4f}% — пропущено")
            else:
                if prev_spread_pct is not None and abs(spread_pct) < ALERT_THRESHOLD:
                    prev_spread_pct = None  # сброс — спред вернулся в норму
                print(f"[{now}] SPOT={spot:.6f} FUT={futures:.6f} spread={spread_pct:+.4f}% — ниже порога")

        except requests.RequestException as e:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Error: {e}")
        except Exception as e:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Error: {e}")

        time.sleep(1)


if __name__ == "__main__":
    main()
