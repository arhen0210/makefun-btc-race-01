"""make.fun BTC race (btc-race) entry: dip-buy probe sized so a full round
trip costs ~12 bps (vs ~26 bps for a full-size trade), making a 0.2% bounce
profitable.

The competition is long only. A full-size round trip costs about 26 bps of
slippage/impact, so the bot runs two gears:

* probe - buys a small fixed notional (probe_usdc) after a sharp drop that is
  already bouncing, and sells it on a small take-profit, a stop-loss or a time
  limit. Small enough that its round trip costs about 12 bps, so a 0.2%
  bounce pays.
* main  - buys the whole account when price breaks a long high while the fast
  EMA is above the slow one, and leaves on a short-low break. Disabled here
  with use_main=0, but the code path is kept intact.

Parameters live in params.json beside this file so a sweep can rewrite them
without touching the code.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "vendor"))
import makefun_bars  # noqa: E402

SYMBOL = "CBBTC"
MAX_SLIPPAGE_BPS = 80          # slippage cap used on every order this bot sends
CLOSES_KEEP = 400               # rolling close-price history length

# Fallback defaults; params.json (if present) overrides them.
DEFAULTS = {
    "probe_usdc": 20,       # probe notional in USDC
    "drop_bps": 40,         # probe trigger: how far below the lookback high
    "lookback": 30,         # probe trigger: bars for that high
    "take_bps": 25,         # probe take-profit in bps
    "stop_bps": 30,         # probe stop-loss in bps
    "max_hold": 30,         # probe time exit, in bars held
    "fast": 12,             # fast EMA span (main gear)
    "slow": 48,             # slow EMA span (main gear)
    "brk_lookback": 120,    # main gear: breakout window
    "exit_lookback": 20,    # main gear: trailing-low exit window
    "use_main": 1,          # 1 = main gear enabled, 0 = probe only
}


def load_params():
    """DEFAULTS overridden by params.json next to this file, if readable."""
    p = dict(DEFAULTS)
    try:
        with open(os.path.join(HERE, "params.json")) as f:
            p.update(json.load(f))
    except OSError:
        pass
    return p


P = load_params()


class Bot:
    def __init__(self):
        self.closes = []          # rolling close prices
        self.ema_fast = None      # fast EMA of closes
        self.ema_slow = None      # slow EMA of closes
        self.probe = None         # open probe: dict(cost_price, bars)
        self.main_on = False      # main gear position flag

    # ------------------------------------------------------------------ #
    # history / indicators
    # ------------------------------------------------------------------ #
    def update_history(self, px):
        """Append this bar's close and keep the rolling EMA pair current."""
        self.closes.append(px)
        if len(self.closes) > CLOSES_KEEP:
            self.closes.pop(0)
        if self.ema_fast is None:
            # seed both EMAs with the first close
            self.ema_fast = self.ema_slow = px
        else:
            af = 2.0 / (P["fast"] + 1.0)
            as_ = 2.0 / (P["slow"] + 1.0)
            self.ema_fast = af * px + (1.0 - af) * self.ema_fast
            self.ema_slow = as_ * px + (1.0 - as_) * self.ema_slow

    # ------------------------------------------------------------------ #
    # main gear (disabled with use_main=0, kept intact)
    # ------------------------------------------------------------------ #
    def main_gear_orders(self, state, px, held):
        """Breakout entry with trend filter, trailing-low exit. Whole account.

        Entry: close above the highest close of the previous brk_lookback
        bars, while the fast EMA is above the slow EMA, and no probe open.
        Exit: close below the lowest close of the previous exit_lookback bars.
        """
        orders = []
        n = len(self.closes)
        if not (P["use_main"] and n > P["brk_lookback"] + 2):
            return orders

        brk_high = max(self.closes[-P["brk_lookback"] - 1:-1])
        exit_low = min(self.closes[-P["exit_lookback"] - 1:-1])

        if not self.main_on and self.probe is None \
                and self.ema_fast > self.ema_slow and px > brk_high:
            notional = state.cash
            if notional >= 1_000_000:
                self.main_on = True
                orders.append({"side": "buy", "symbol": SYMBOL,
                               "notional_quote_units": notional,
                               "max_slippage_bps": MAX_SLIPPAGE_BPS})
                return orders
        if self.main_on and held and px < exit_low:
            self.main_on = False
            orders.append({"side": "sell", "symbol": SYMBOL,
                          "qty": held.qty,
                          "max_slippage_bps": MAX_SLIPPAGE_BPS})
            return orders
        return orders

    # ------------------------------------------------------------------ #
    # probe gear
    # ------------------------------------------------------------------ #
    def probe_exit_order(self, state, px, held):
        """Sell the whole probe on take-profit, stop-loss or max_hold."""
        self.probe["bars"] += 1
        gain_bps = (px / self.probe["cost_price"] - 1.0) * 1e4
        exit_now = (gain_bps >= P["take_bps"]
                    or gain_bps <= -P["stop_bps"]
                    or self.probe["bars"] >= P["max_hold"])
        if exit_now and held:
            self.probe = None
            return [{"side": "sell", "symbol": SYMBOL,
                     "qty": held.qty,
                     "max_slippage_bps": MAX_SLIPPAGE_BPS}]
        return []

    def probe_entry_order(self, state, px):
        """Buy probe_usdc after a >= drop_bps dip that is bouncing this bar.

        Trigger: close is >= drop_bps below the highest close of the previous
        lookback bars AND this bar's close is above the previous close.
        """
        n = len(self.closes)
        if n <= P["lookback"] + 1:
            return []
        hi = max(self.closes[-P["lookback"] - 1:-1])
        drop_bps = (1.0 - px / hi) * 1e4
        up_bar = px > self.closes[-2]
        if drop_bps >= P["drop_bps"] and up_bar \
                and state.cash > P["probe_usdc"] * 2_000_000:
            self.probe = {"cost_price": px, "bars": 0}
            return [{"side": "buy", "symbol": SYMBOL,
                     "notional_quote_units": int(P["probe_usdc"] * 1_000_000),
                     "max_slippage_bps": MAX_SLIPPAGE_BPS}]
        return []

    # ------------------------------------------------------------------ #
    # per-bar entry point
    # ------------------------------------------------------------------ #
    def on_batch(self, state, batch):
        px = state.close(SYMBOL)
        if px is None:
            return []
        px = float(px)
        self.update_history(px)

        held = state.positions.get(SYMBOL)

        # main gear first; it owns the account when it fires
        orders = self.main_gear_orders(state, px, held)
        if orders:
            return orders
        if self.main_on:
            return []

        # probe gear: manage an open probe, else look for an entry
        if self.probe is not None:
            return self.probe_exit_order(state, px, held)
        if held:
            return []
        # entry waits for the bar that is already bouncing (up_bar), not the dip bar itself
        return self.probe_entry_order(state, px)


bot = Bot()


def on_batch(state, batch):
    return bot.on_batch(state, batch)


def on_hello(state):
    return None


def on_done(state, done):
    return None


if __name__ == "__main__":
    makefun_bars.run(on_batch, on_hello=on_hello, on_done=on_done)
