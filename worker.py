import json
import logging
import os
import time
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("gold-scalper")

# PAPER TRADING ONLY — no broker or real-order code.
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
DATA_KEY = os.getenv("TWELVE_DATA_API_KEY", "")
SYMBOL = os.getenv("SYMBOL", "XAU/USD")
START_CASH = float(os.getenv("START_BALANCE", "50"))
FEE = float(os.getenv("FEE_RATE", "0.001"))
SLIPPAGE = float(os.getenv("SLIPPAGE_RATE", "0.0005"))
TAKE_PROFIT = float(os.getenv("TAKE_PROFIT_PCT", "0.0015"))
STOP_LOSS = float(os.getenv("STOP_LOSS_PCT", "0.003"))
POLL_SECONDS = max(30, int(os.getenv("POLL_SECONDS", "60")))
MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS_PCT", "0.03"))
STATE_FILE = os.getenv("STATE_FILE", "state.json")


def notify(message):
    if not TOKEN or not CHAT_ID:
        log.info("Telegram not configured: %s", message.replace("\n", " | "))
        return
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    body = json.dumps({"chat_id": CHAT_ID, "text": message}).encode()
    request = Request(
        url, data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            result = json.loads(response.read().decode())
        if not result.get("ok"):
            log.warning("Telegram rejected notification")
    except Exception:
        log.exception("Telegram notification failed")


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as file:
                return json.load(file)
        except Exception:
            log.exception("State file could not be read")
    return {
        "cash": START_CASH,
        "position": None,
        "trades": [],
        "day": datetime.now(timezone.utc).date().isoformat(),
        "day_start_equity": START_CASH,
        "halted": False,
        "last_candle": None,
        "last_heartbeat": 0,
    }


def save_state(state):
    temp = STATE_FILE + ".tmp"
    with open(temp, "w", encoding="utf-8") as file:
        json.dump(state, file, indent=2)
    os.replace(temp, STATE_FILE)


def get_candles():
    if not DATA_KEY:
        raise RuntimeError("Missing TWELVE_DATA_API_KEY environment variable")
    params = urlencode({
        "symbol": SYMBOL,
        "interval": "1min",
        "outputsize": "100",
        "timezone": "UTC",
        "apikey": DATA_KEY,
    })
    request = Request(
        "https://api.twelvedata.com/time_series?" + params,
        headers={"User-Agent": "GoldScalperPaper/1.0"},
    )
    with urlopen(request, timeout=20) as response:
        data = json.loads(response.read().decode())
    if "values" not in data:
        raise RuntimeError(
            "Price provider error: " + str(data.get("message", "unknown error"))
        )
    frame = pd.DataFrame(data["values"])
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame = frame.dropna(subset=["close"]).sort_values("datetime")
    if len(frame) < 30:
        raise RuntimeError("Not enough one-minute candles")
    return frame.reset_index(drop=True)


def equity(state, price):
    position = state["position"]
    if not position:
        return state["cash"]
    return state["cash"] + position["units"] * price


def close_position(state, price, reason):
    pos = state["position"]
    if not pos:
        return
    exit_price = price * (1 - SLIPPAGE)
    gross = pos["units"] * exit_price
    exit_fee = gross * FEE
    pnl = gross - exit_fee - pos["entry_cost"]
    state["cash"] += gross - exit_fee
    state["trades"].append({
        "time": datetime.now(timezone.utc).isoformat(),
        "entry": pos["entry_price"],
        "exit": exit_price,
        "pnl": pnl,
        "reason": reason,
    })
    state["position"] = None
    notify(
        f"🔴 PAPER SELL — {SYMBOL}\n"
        f"Reason: {reason}\nEntry: ${pos['entry_price']:.2f}\n"
        f"Exit: ${exit_price:.2f}\n"
        f"Simulated net P&L: {pnl:+.4f} USD\n"
        f"Virtual cash: ${state['cash']:.2f}\n"
        "Paper trading only; no real order."
    )
    log.info("SELL %s, net P&L %.4f", reason, pnl)


def open_position(state, price, candle):
    if state["position"] or state["halted"]:
        return
    spend = state["cash"] * 0.10
    if spend < 1:
        return
    entry = price * (1 + SLIPPAGE)
    entry_fee = spend * FEE
    units = max(0, (spend - entry_fee) / entry)
    if units <= 0:
        return
    state["cash"] -= spend
    state["position"] = {
        "entry_price": entry,
        "units": units,
        "entry_cost": units * entry + entry_fee,
        "target": entry * (1 + TAKE_PROFIT),
        "stop": entry * (1 - STOP_LOSS),
        "candle": candle,
    }
    notify(
        f"🟢 PAPER BUY — {SYMBOL}\n"
        f"Market price: ${price:.2f}\n"
        f"Simulated entry: ${entry:.2f}\n"
        f"Virtual allocation: ${spend:.2f}\n"
        f"Take profit: ${entry * (1 + TAKE_PROFIT):.2f}\n"
        f"Stop loss: ${entry * (1 - STOP_LOSS):.2f}\n"
        "Paper trading only; no real order."
    )
    log.info("BUY at %.2f", entry)


def main():
    state = load_state()
    notify(
        f"🤖 Gold Scalper paper worker started\n"
        f"Symbol: {SYMBOL}\nStarting virtual cash: ${START_CASH:.2f}\n"
        "No real-money orders."
    )

    while True:
        try:
            today = datetime.now(timezone.utc).date().isoformat()
            if state["day"] != today:
                state["day"] = today
                state["day_start_equity"] = equity(
                    state, state.get("last_price", 0)
                )
                state["halted"] = False

            candles = get_candles()
            close = candles["close"]
            fast = close.ewm(span=9, adjust=False).mean()
            slow = close.ewm(span=21, adjust=False).mean()
            price = float(close.iloc[-1])
            candle = str(candles["datetime"].iloc[-1])
            state["last_price"] = price

            if state["last_candle"] != candle:
                pos = state["position"]
                if pos:
                    if price >= pos["target"]:
                        close_position(state, price, "take profit")
                    elif price <= pos["stop"]:
                        close_position(state, price, "stop loss")

                if not state["position"] and not state["halted"]:
                    crossed_up = (
                        fast.iloc[-2] <= slow.iloc[-2]
                        and fast.iloc[-1] > slow.iloc[-1]
                    )
                    if crossed_up:
                        open_position(state, price, candle)
                state["last_candle"] = candle

            current_equity = equity(state, price)
            daily_loss = max(
                0, state["day_start_equity"] - current_equity
            )
            if (
                daily_loss >= state["day_start_equity"] * MAX_DAILY_LOSS
                and not state["halted"]
            ):
                state["halted"] = True
                if state["position"]:
                    close_position(state, price, "daily loss limit")
                notify("🛑 Daily loss limit reached. New entries paused.")

            if time.time() - state["last_heartbeat"] > 21600:
                notify(
                    f"💓 Gold Scalper status\nPrice: ${price:.2f}\n"
                    f"Virtual equity: ${equity(state, price):.2f}\n"
                    f"Closed trades: {len(state['trades'])}\n"
                    f"Status: {'PAUSED' if state['halted'] else 'RUNNING'}"
                )
                state["last_heartbeat"] = time.time()

            save_state(state)
            log.info(
                "Price %.2f; equity %.2f; position=%s",
                price, current_equity, bool(state["position"]),
            )
        except Exception as error:
            log.exception("Worker cycle failed")
            notify(f"⚠️ Gold Scalper error: {str(error)[:400]}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    if os.getenv("DRY_RUN", "true").lower() != "true":
        raise RuntimeError("This worker is paper-only; DRY_RUN must be true.")
    main()
