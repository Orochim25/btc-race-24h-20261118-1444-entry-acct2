# Snapback-100

A dip-and-bounce BTC trading bot for the make.fun BTC race
(btc-race-24h-20261118-1444). It trades Coinbase Wrapped BTC (cbBTC)
against USDC, one minute at a time, long only, $1,000 per trade.

## Strategy

Quiet, range-bound days are where this bot lives — it does not chase
momentum. It waits for the price to fall hard and then turn up, and buys
the turn:

- **Dip** (short lookback, 60-bar high): the close sits 120+ bps below the
  60-bar high and 150+ bps below the close 60 bars ago, on a day that is
  not in free-fall (under 550 bps below the 720-bar high), with RSI under
  50 and the latest close ticking up. Exit: fixed take-profit +100 bps
  from the decision close, hard stop −60 bps, or 300 bars.
- **Bounce** (capitulation): a day 550+ bps below the 720-bar high where a
  bar prints a long lower wick (20+ bps) and reverses back above the
  previous bar's high while RSI turns up, with reward at least 2x the
  risk. Exit: trailing stop (arms at +70 bps, trails 70 bps behind the
  peak), hard stop below the wick low, or 300 bars.

Guards: at most 3 trades (1 dip + 2 bounces), 60 bars cooling off after
an exit, no entry after bar 1320, no entry below 98% equity, and no more
bounces after two losing bounces in a row. Orders carry a 200 bps
max-slippage bound. Pure function of the bars seen: no clock, no
randomness, no network.

[![make.fun](https://make.fun/badge/btc-race-24h-20261118-1444.svg)](https://make.fun/bounty/btc-race-24h-20261118-1444)
[![rank](https://make.fun/badge/btc-race-24h-20261118-1444/Orochim25/btc-race-24h-20261118-1444-entry-acct2/rank.svg)](https://make.fun/bounty/btc-race-24h-20261118-1444)
[![reward](https://make.fun/badge/btc-race-24h-20261118-1444/Orochim25/btc-race-24h-20261118-1444-entry-acct2/reward.svg)](https://make.fun/bounty/btc-race-24h-20261118-1444)
