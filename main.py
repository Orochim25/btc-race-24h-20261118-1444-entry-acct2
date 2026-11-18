"""Snapback-100: a one-asset dip-and-bounce bot for the BTC race.

Idea: the race window is a quiet, range-bound day, so the bot does not chase
momentum. It waits for the price to fall hard and then turn up, and buys the
turn. Two shapes:

* dip: a sharp fall from the recent high (60-bar lookback, short enough to
  catch a dip that starts early in the window) on a day that is NOT in
  free-fall, with the RSI weak and the latest close ticking up. It exits on
  a fixed take-profit (+100 bps from the decision close) or a hard stop
  (-60 bps), whichever comes first, or after 300 bars.
* bounce: a capitulation day (a big drop from the 720-bar high) where a bar
  prints a long lower wick and reverses back above the previous bar's high
  while the RSI turns up. It exits on a trailing stop (armed at +70 bps,
  trailing 70 bps behind the peak) or a hard stop below the wick low,
  or after 300 bars.

Guards: at most 3 trades in total (1 dip + 2 bounces), 60 bars of cooling
off after an exit, no entry after bar 1320, no entry once equity has fallen
below 98% of the start, and no more bounces after two losing bounces in a
row. Orders carry a 200 bps max-slippage bound. Everything is a pure
function of the bars seen so far: no clock, no randomness, no environment.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"
BPS = 10_000

# --- history / guards -------------------------------------------------------
MIN_BARS = 62            # closes needed before the first entry
LAST_ENTRY_BAR = 1320    # no new entry after this bar
COOLDOWN_BARS = 60       # bars to wait after an exit
MAX_TRADES = 3           # total entries per window
MAX_DIP_TRADES = 1
MAX_BOUNCE_TRADES = 2
MAX_SLIPPAGE_BPS = 200
EQUITY_FLOOR = 980_000_000  # 98% of the 1,000 USDC start, in quote units

# --- dip (Setup D, short lookback) ------------------------------------------
DIP_DROP60_MIN = 120     # close >= 120 bps below the 60-bar high
DIP_DROP720_MAX = 550     # ... but < 550 bps below the 720-bar high
DIP_FALL60_MIN = 150     # close >= 150 bps below the close 60 bars ago
DIP_RSI_MAX = 50          # RSI (Wilder 14) below 50
DIP_STOP_BPS = 60         # hard stop, bps below the decision close
DIP_TP_BPS = 100          # fixed take-profit, bps above the decision close
DIP_HOLD_MAX = 300        # bars

# --- bounce (Setup B, capitulation) -----------------------------------------
BOUNCE_FALL60_MIN = 150   # close >= 150 bps below the 60-bar high, or ...
BOUNCE_FALL120_MIN = 250  # ... >= 250 bps below the 120-bar high
BOUNCE_DROP720_MIN = 550  # ... on a day >= 550 bps below the 720-bar high
BOUNCE_WICK_MIN = 20      # lower wick of the bar, bps
BOUNCE_REVERSAL_MIN = 60  # close >= 60 bps above the bar's low
BOUNCE_STOP_MIN = 50      # hard-stop distance, clamped to [50, 100] bps
BOUNCE_STOP_MAX = 100
BOUNCE_RR_MIN = 2         # reward must be >= 2x the risk
BOUNCE_ARM_BPS = 70       # trailing stop arms once the peak is +70 bps
BOUNCE_TRAIL_BPS = 70     # ... then trails 70 bps behind the peak
BOUNCE_HOLD_MAX = 300     # bars


def _wilder_rsi(closes, period=14):
    """(RSI now, RSI one bar ago), Wilder smoothing; (None, None) if too short.

    The first average is the plain mean of the first `period` one-bar
    changes; later averages smooth with weight 1/period on the new change.
    """
    n = len(closes)
    if n < period + 2:
        return None, None
    gain = 0.0
    loss = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d > 0:
            gain += d
        else:
            loss -= d
    avg_gain = gain / period
    avg_loss = loss / period

    def _value(ag, al):
        if al == 0:
            return 100.0
        return 100.0 - 100.0 / (1.0 + ag / al)

    prev = _value(avg_gain, avg_loss)
    now = prev
    for i in range(period + 1, n):
        d = closes[i] - closes[i - 1]
        g = d if d > 0 else 0
        l = -d if d < 0 else 0
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
        prev = now
        now = _value(avg_gain, avg_loss)
    return now, prev


class Tape:
    """Rolling one-minute bars plus the indicators the strategy reads."""

    def __init__(self):
        self.opens = []
        self.highs = []
        self.lows = []
        self.closes = []

    def push(self, bar):
        self.opens.append(int(bar["o"]))
        self.highs.append(int(bar["h"]))
        self.lows.append(int(bar["l"]))
        self.closes.append(int(bar["c"]))

    def __len__(self):
        return len(self.closes)

    @property
    def close(self):
        return self.closes[-1]

    @property
    def prev_close(self):
        return self.closes[-2]

    def highest(self, span):
        return max(self.highs[-span:])

    def drop_from_high(self, span):
        """Bps the latest close sits below the high of the last `span` bars."""
        top = self.highest(span)
        return (top - self.close) * BPS // top

    def span_fall(self, span):
        """Bps the close has fallen vs the close `span` bars ago."""
        old = self.closes[-span - 1]
        return (old - self.close) * BPS // old

    def rsi_pair(self):
        return _wilder_rsi(self.closes)


def dip_signal(tape):
    """True when the tape shows a dip worth buying (short lookback)."""
    if len(tape) < MIN_BARS:
        return False
    if tape.drop_from_high(60) < DIP_DROP60_MIN:
        return False
    if tape.drop_from_high(720) >= DIP_DROP720_MAX:
        return False
    if tape.span_fall(60) < DIP_FALL60_MIN:
        return False
    rsi_now, _ = tape.rsi_pair()
    if rsi_now is None or rsi_now >= DIP_RSI_MAX:
        return False
    if not tape.close > tape.prev_close:
        return False
    return True


def bounce_signal(tape):
    """True when the tape shows a capitulation wick + reversal.

    Returns the hard-stop distance in bps (clamped to [50, 100]) when the
    setup fires, else None.
    """
    if len(tape) < MIN_BARS:
        return None
    c = tape.close
    o = tape.opens[-1]
    low = tape.lows[-1]
    fall60 = tape.drop_from_high(60)
    fall120 = tape.drop_from_high(120)
    if not (fall60 >= BOUNCE_FALL60_MIN or fall120 >= BOUNCE_FALL120_MIN):
        return None
    if tape.drop_from_high(720) < BOUNCE_DROP720_MIN:
        return None
    wick = (min(o, c) - low) * BPS // c
    if wick < BOUNCE_WICK_MIN:
        return None
    reversal = (c - low) * BPS // low
    if reversal < BOUNCE_REVERSAL_MIN:
        return None
    if not c > tape.highs[-2]:
        return None
    rsi_now, rsi_prev = tape.rsi_pair()
    if rsi_now is None or rsi_prev is None or not rsi_now > rsi_prev:
        return None
    stop_raw = (c - low) * BPS // c
    stop_dist = min(max(stop_raw, BOUNCE_STOP_MIN), BOUNCE_STOP_MAX)
    target = tape.highest(60) if fall60 >= BOUNCE_FALL60_MIN else tape.highest(120)
    reward = (target - c) * BPS // c
    if reward < BOUNCE_RR_MIN * stop_dist:
        return None
    return stop_dist


class OpenTrade:
    """One open position and the plan for getting out of it."""

    def __init__(self, kind, entry_seq, entry_close, entry_cost):
        self.kind = kind  # "dip" or "bounce"
        self.entry_seq = entry_seq
        self.entry_close = entry_close
        self.entry_cost = entry_cost
        self.peak = entry_close
        self.tp_level = None
        self.stop_level = None
        self.arm_level = None
        self.trail_gap_bps = 0
        self.hold_max = 300

    def arm_dip(self):
        self.tp_level = self.entry_close * (BPS + DIP_TP_BPS) // BPS
        self.stop_level = self.entry_close * (BPS - DIP_STOP_BPS) // BPS
        self.hold_max = DIP_HOLD_MAX

    def arm_bounce(self, stop_dist_bps):
        self.stop_level = self.entry_close * (BPS - stop_dist_bps) // BPS
        self.arm_level = self.entry_close * (BPS + BOUNCE_ARM_BPS) // BPS
        self.trail_gap_bps = BOUNCE_TRAIL_BPS
        self.hold_max = BOUNCE_HOLD_MAX

    def exit_now(self, tape, seq):
        """True when this bar says get out (trailing peak updated first)."""
        c = tape.close
        if c > self.peak:
            self.peak = c
        if seq - self.entry_seq >= self.hold_max:
            return True
        if self.stop_level is not None and c <= self.stop_level:
            return True
        if self.kind == "dip":
            if self.tp_level is not None and c >= self.tp_level:
                return True
        else:
            if self.peak >= self.arm_level:
                trail = self.peak * (BPS - self.trail_gap_bps) // BPS
                if c <= trail:
                    return True
        return False


class SnapbackBot:
    """The decision loop: reconcile fills, manage the open trade, enter flat."""

    def __init__(self):
        self.tape = Tape()
        self.trade = None
        self.pending_buy = False
        self.trades = 0
        self.dip_trades = 0
        self.bounce_trades = 0
        self.bounce_losses = 0
        self.last_exit_seq = -(10 ** 9)

    def decide(self, state, batch):
        bar = None
        for b in batch.get("bars", []):
            if b.get("symbol") == SYMBOL:
                bar = b
                break
        if bar is None:
            return []
        self.tape.push(bar)
        seq = state.seq
        self._reconcile(state, seq)
        if self.trade is not None:
            if self.pending_buy:
                return []  # buy decided last bar; wait for its fill
            if self.trade.exit_now(self.tape, seq):
                qty = state.positions[SYMBOL].qty
                return [{"side": "sell", "symbol": SYMBOL, "qty": qty,
                         "max_slippage_bps": MAX_SLIPPAGE_BPS}]
            return []
        if not self._may_enter(state, seq):
            return []
        if self.dip_trades < MAX_DIP_TRADES and dip_signal(self.tape):
            return self._buy(state, seq, "dip")
        stop_dist = bounce_signal(self.tape)
        if (stop_dist is not None and self.bounce_trades < MAX_BOUNCE_TRADES
                and self.bounce_losses < 2):
            return self._buy(state, seq, "bounce", stop_dist)
        return []

    def _reconcile(self, state, seq):
        """Sync the open-trade record with what the account actually holds."""
        if self.trade is None:
            return
        held = SYMBOL in state.positions
        if self.pending_buy:
            if held:
                self.pending_buy = False
                self.trades += 1
                if self.trade.kind == "dip":
                    self.dip_trades += 1
                else:
                    self.bounce_trades += 1
            else:
                # the buy never filled (rejected): forget it, it costs no slot
                self.trade = None
            return
        if not held:
            # sold (or flattened): book the result
            pnl = state.cash - self.trade.entry_cost
            if self.trade.kind == "bounce":
                if pnl < 0:
                    self.bounce_losses += 1
                else:
                    self.bounce_losses = 0
            self.last_exit_seq = seq
            self.trade = None

    def _may_enter(self, state, seq):
        if len(self.tape) < MIN_BARS:
            return False
        if seq > LAST_ENTRY_BAR:
            return False
        if self.trades >= MAX_TRADES:
            return False
        if seq - self.last_exit_seq < COOLDOWN_BARS:
            return False
        if state.equity < EQUITY_FLOOR:
            return False
        return True

    def _buy(self, state, seq, kind, stop_dist=None):
        notional = state.cash
        trade = OpenTrade(kind, seq, self.tape.close, notional)
        if kind == "dip":
            trade.arm_dip()
        else:
            trade.arm_bounce(stop_dist)
        self.trade = trade
        self.pending_buy = True
        return [{"side": "buy", "symbol": SYMBOL,
                 "notional_quote_units": notional,
                 "max_slippage_bps": MAX_SLIPPAGE_BPS}]


_BOT = SnapbackBot()


def on_batch(state, batch):
    return _BOT.decide(state, batch)


if __name__ == "__main__":
    makefun_bars.run(on_batch)
