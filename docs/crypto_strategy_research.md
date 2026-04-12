# Crypto & AI Trading Strategy Research
## Compiled from YouTube Video Transcripts
### Date: 2026-04-12 | Videos: 8 | Total Words: 31395

---

# PART 1: ACTIONABLE STRATEGY SUMMARIES

## Strategy 1: Bollinger Bands + RSI Mean Reversion
**Source:** "Bollinger Band + RSI Trading Strategy That Actually Works" (pCmJ8wsAS_w)

### Strategy Type
Mean reversion on any liquid asset (stocks, crypto, forex)

### Indicator Settings
- Bollinger Bands: Length = 30, Standard Deviation = 2
- RSI: Length = 13, Overbought = 75, Oversold = 25

### Entry Signals
- LONG: Price closes below lower Bollinger Band AND RSI drops below 25
- SHORT: Price closes above upper Bollinger Band AND RSI rises above 75

### Exit Signals
- Exit when price returns to the middle Bollinger Band (the moving average)

### Risk Management
- AVOID trading when Bollinger Bands are narrow/squeezed (sideways market)
- Narrow bands followed by breakout = momentum move, NOT mean reversion
- Look for RSI divergence as confirmation: price makes lower low but RSI makes higher low = strong reversal signal

### Key Parameters for Coding
```
bb_length: 30
bb_std: 2
rsi_length: 13
rsi_overbought: 75
rsi_oversold: 25
exit_target: middle_bollinger_band (SMA)
filter: skip_when_bb_bandwidth < threshold (squeeze detection)
```

## Strategy 2: BB + RSI Backtested Across Markets (FreqTrade)
**Source:** "I Tested RSI + Bollinger Bands Strategy in 2025" (j2ESnjhT2no)

### Strategy Type
Mean reversion with FreqTrade hyperparameter optimization

### Indicator Settings (Default from video)
- Bollinger Bands: default period with configurable std
- RSI: standard with configurable overbought/oversold thresholds

### Entry Signals
- LONG: Price crosses below lower Bollinger Band AND RSI crosses below oversold threshold
- SHORT: Price crosses above upper Bollinger Band AND RSI crosses above overbought threshold

### Exit Signals
- Exit LONG when price crosses middle Bollinger Band AND RSI above threshold
- Exit SHORT when price crosses middle Bollinger Band AND RSI below threshold

### Backtest Results (CRITICAL FINDING)
- CRYPTO: Strategy performed POORLY across ALL timeframes (1m to 1d)
  - 1-day: Sharpe 12, profit only $70 (marginal)
  - 4h: Sharpe -1.17, loss $370
  - 1h: Sharpe -33, loss $3,000
  - 15m: Sharpe -42, loss $1,800
  - 5m: Sharpe -90, loss $2,000
  - 1m: Sharpe -1000, loss $3,700
- STOCKS: Mixed results, 1h and 15m slightly positive
- FUTURES: 5m (Sharpe 8.69, +$300) and 1m (Sharpe 25, +$375) worked
- Hyperparameter optimization provided only marginal improvement

### CONCLUSION: Simple BB+RSI is NOT reliable for crypto
The raw BB+RSI mean reversion strategy does NOT work consistently on crypto markets.
It needs additional filters (trend, ADX, volume) to be viable.

## Strategy 3: Advanced Mean Reversion with ADX + Multi-Timeframe RSI (FreqTrade)
**Source:** "Mean Reversion Trading Strategy Explained & Backtested -- 179% Profit" (c9-SIpy3dEw)

### Strategy Type
Mean reversion with trend filtering on 1H timeframe, optimized via FreqTrade

### Indicator Settings
- Bollinger Bands: 20-period SMA, 2 standard deviations (1H chart)
- RSI: Used on 4H timeframe as TREND FILTER (not entry signal)
- ADX: Applied on both 1H and 4H timeframes
- ATR: Used for dynamic stop-loss placement

### Entry Signals (LONG)
1. 4H RSI > 55 (confirms bullish trend)
2. ADX > 20 on 1H chart (sufficient trend strength)
3. ADX > 25 on 4H chart (medium-term trend confirmed)
4. Price closes below lower Bollinger Band on 1H (oversold)
5. Enter on NEXT candle after all conditions met

### Entry Signals (SHORT)
1. 4H RSI < 45 (confirms bearish trend)
2. ADX > 20 on 1H chart
3. ADX > 25 on 4H chart
4. Price closes above upper Bollinger Band on 1H (overbought)
5. Enter on NEXT candle

### Exit Signals
- LONG exit: Price closes above upper Bollinger Band
- SHORT exit: Price closes below lower Bollinger Band

### Risk Management
- Stop Loss: Below signal candle low minus 4.5x ATR (for longs)
- Stop Loss: Above signal candle high plus 4.5x ATR (for shorts)

### Backtest Results
- Total profit: 179% on NEAR perpetual futures, 1H timeframe, 1 year
- Market returned -66% in the same period
- Max drawdown: 19.3%
- Tested with 8-month in-sample / 4-month out-of-sample split

### Key Parameters for Coding
```
timeframe: 1H (trading), 4H (trend filter)
bb_period: 20 (optimizable)
bb_std: 2 (optimizable)
htf_rsi_long_threshold: 55
htf_rsi_short_threshold: 45
adx_threshold_1h: 20
adx_threshold_4h: 25
atr_multiplier_stoploss: 4.5
exit: opposite_bollinger_band
```

## Strategy 4: AI/ML Sentiment-Driven Trading Bot (Lumibot + Alpaca)
**Source:** "How to Code a AI Trading bot (so you can make $$$)" (c9OjEThuJjY)

### Strategy Type
News sentiment analysis using FinBERT ML model, daily timeframe

### Architecture
- Framework: Lumibot (Python trading framework)
- Broker: Alpaca API (paper trading)
- ML Model: FinBERT for financial sentiment analysis
- Data: Alpaca News API, last 3 days of news

### Entry Signals
- BUY: Sentiment is positive AND probability > 0.999 AND no existing position
- SELL/SHORT: Sentiment is negative AND probability > 0.999 AND last trade was buy

### Position Sizing
- cash_at_risk = 0.5 (50% of available cash per trade)
- quantity = (cash * cash_at_risk) / last_price

### Risk Management
- Bracket orders with:
  - Take profit (long): +20% from entry
  - Stop loss (long): -5% from entry
  - Take profit (short): -20% from entry (price * 0.8)
  - Stop loss (short): +5% from entry (price * 1.05)

### Backtest Results
- 4-year backtest (2020-2023) on SPY
- Cumulative return: 234.40%
- Compound annual growth rate: 23.8%
- Bot correctly short-sold during market downturns
- Peak portfolio value: ~$537,500 (from $100K)

### Key Parameters for Coding
```
symbol: SPY (adaptable to crypto)
sleep_time: 24H (daily trading)
cash_at_risk: 0.5
sentiment_model: FinBERT
sentiment_threshold: 0.999 probability
news_lookback: 3 days
take_profit_long: 1.20 (20%)
stop_loss_long: 0.95 (5%)
take_profit_short: 0.80 (20%)
stop_loss_short: 1.05 (5%)
order_type: bracket
```

## Strategy 5: RSI Crypto Trading Bot (Real-Time Websocket)
**Source:** "Build a Real-Time Crypto Trading Bot in under 100 Lines" (GdlFhF6gjKo)

### Strategy Type
RSI overbought/oversold on 1-minute candlesticks via Binance websocket

### Indicator Settings
- RSI Period: 14 (default ta-lib)
- Overbought threshold: 70
- Oversold threshold: 30
- Timeframe: 1-minute candles (real-time)

### Entry Signals
- BUY: RSI crosses below 30 (oversold)
- SELL: RSI crosses above 70 (overbought)

### Architecture
- Data: Binance websocket stream (ethusdt@kline_1m)
- Indicator: ta-lib RSI calculation on closing prices array
- Execution: Binance API market orders
- Position tracking: boolean in_position flag

### Key Parameters for Coding
```
symbol: ETHUSDT
timeframe: 1m candles
rsi_period: 14
rsi_overbought: 70
rsi_oversold: 30
order_type: market
data_source: websocket (binance)
```

## Strategy 6: Dual Moving Average Crossover
**Source:** "Algorithmic Trading Strategy Using Python" (SEQbb8w7VTw)

### Strategy Type
Trend-following moving average crossover

### Indicator Settings
- Short-term SMA: 30-day window
- Long-term SMA: 100-day window
- Applied to: Adjusted Close Price

### Entry Signals
- BUY: 30-day SMA crosses ABOVE 100-day SMA (golden cross)
- SELL: 30-day SMA crosses BELOW 100-day SMA (death cross)

### Key Parameters for Coding
```
sma_short: 30
sma_long: 100
price_field: adjusted_close
signal: crossover detection
```

---

# PART 2: SYNTHESIS -- WHAT TO IMPLEMENT FOR ATLAS LITE

## Recommended Strategy: Advanced Mean Reversion (Strategy 3)
This is the most backtested and production-ready strategy from the research.
It addresses the key failure of simple BB+RSI (Strategy 2 proved it fails on crypto)
by adding crucial filters:

### Why Strategy 3 Wins
1. Multi-timeframe confirmation (1H trade, 4H trend filter)
2. ADX filter prevents trading in choppy/sideways markets
3. RSI used as trend filter (not entry), avoiding false signals
4. ATR-based dynamic stop loss adapts to volatility
5. 179% profit vs -66% market, with only 19% max drawdown
6. Tested with proper in-sample/out-of-sample methodology

### Implementation Parameters for ATLAS Lite
```json
{
  "strategy_name": "mean_reversion_adx",
  "timeframe_trade": "1H",
  "timeframe_trend": "4H",
  "bollinger_period": 20,
  "bollinger_std": 2.0,
  "rsi_trend_long": 55,
  "rsi_trend_short": 45,
  "adx_threshold_trade": 20,
  "adx_threshold_trend": 25,
  "atr_stop_multiplier": 4.5,
  "exit_rule": "opposite_bollinger_band",
  "coins": ["BTC/USD", "ETH/USD"],
  "max_position_pct": 5,
  "cash_reserve_pct": 20
}
```

### Secondary Strategy: Sentiment-Driven (Strategy 4)
Could complement mean reversion as an additional signal layer:
- Use FinBERT or similar model on crypto news
- High-confidence sentiment (>99.9%) as trade filter
- Bracket orders with 20% take-profit, 5% stop-loss
- Daily timeframe to avoid noise

### What NOT to Implement
- Simple BB+RSI without filters (Strategy 2 proves it loses money on crypto)
- Pure RSI on 1-minute candles (too noisy, high fees eat profits)
- Moving average crossover alone (too slow for crypto volatility)

---

# PART 3: FULL TRANSCRIPTS

## Transcript: Build a Real-Time Crypto Trading Bot in under 100 Lines of Code
**Video ID:** GdlFhF6gjKo | **Words:** 12222
**URL:** https://www.youtube.com/watch?v=GdlFhF6gjKo

```
hey welcome back to my channel if you've
never been here before i discuss the
intersection of software development in
the financial markets
so if you're someone who's always had an
interest in financial markets
whether it's stocks and options or the
crypto market and you're interested in
coding
whether it is to analyze some market
data
whether it's to back test some type of
trading strategy
or you want to build some type of
trading bot you're in the right place
also if you're someone that has software
development experience
perhaps you're a professional software
engineer and you've always been
interested
in how traders apply technical analysis
and indicators in order to find good
trade ideas
uh this is the right place for you as
well so like and subscribe and i'll be
delivering new tutorials every week
where i build entire
projects and show you how to build them
from scratch
step by step in this tutorial i'm going
to be showing you how to build an
automated trading bot in under 100 lines
of code
i'm going to be using python ta lib
binance and websockets to retrieve
real-time price data and apply technical
indicators
to this price data in order to execute
trades automatically
so first i'm going to show you what
we're building in the end and then i'm
going to show you step by step how to
build it so
let's go ahead and get started so i have
visual studio code open here and i'm
going to show you what we're building
first
you can see at the top we're importing a
variety of libraries that we need for
this project
we're going to be using web sockets so
we're going to need the websocket client
we're using json data we're going to
need the numpy library
ta lib we have a local config file and
we're going to pretty print some data
just so that we can analyze the data
that's coming in
from binance and we're also going to be
using the python binance package
so we're going to need to install all of
those packages first
at the top here i have a number of
constants
written out where we define some values
that we're going to be using in our
program
and this allows us to easily configure
how our program's running
so since i'm using the rsi indicator as
a simple example of an indicator we can
use to make trades
i'm using i'm defining some rsi values
here so
we're using like the defaults of rsi
period 14
an overbought threshold of 70 and an
oversold threshold
of 30. and i'll talk more about what the
rsi is in a moment but those are the
comment the default numbers that are
used
and if we wanted to change those
defaults then we could just change them
right here
the symbol we're going to be trading is
ethereum so i have the symbol coded in
here
if we wanted to trade bitcoin we could
just change the symbol to bitcoin
btc usd we're going to buy just a small
amount since we're just
demonstrating this for educational
purpose so um i'm
buying .05 ethereum and since ethereum
is over
400 us dollars now that's actually
spending about 20
so we're gonna buy about 20 25 worth of
ethereum and buy and sell it based on an
rsi indicator
uh we're going to be getting our data
from a websocket and so
if you look up uh binance websockets in
their official documentation
they have a candlestick stream here and
it has a specific url format
and this is the data format and we're
going to be using a websocket stream
here
and i'll talk about that a little bit
more in a moment but we're going to be
streaming
ethereum usdt data and we're going to be
getting one minute candlesticks
and the reason i'm showing a really
short time frame candlesticks
is because uh people are really
interested in short-term data
on this channel i've been doing a lot of
daily examples or weekly and monthly
examples
but people a lot of viewers out there
have been very interested in very
short-term
trading so they want to see a lot of
action happen real quick also it's
easier to make a video and show it
actually executing when we don't have to
record this across multiple days so i'll
actually be able to show you
a data in real time and show you
candlesticks that are closing
and new data points being added to our
series for analysis by ta lib
all right we're going to be tracking all
of the closing prices of each
candlestick so we initialize an array
or a list and we're tracking whether or
not we're already in the position
we're going to be initializing a binance
client so that we can execute buy and
sell orders
against binance and i'm going to go into
all this in detail in a moment i'm just
skimming over this to show you
where we're going and then we'll show
you how to get there okay
we're going to define a function to
order and so let's start at the
beginning of the program
we're actually going to connect to a
websocket so we're going to create a
websocket app
we're going to give it a socket to
listen on so that'll be
our websocket stream and then we're
going to specify a number of callback
functions
uh whenever we listen on the stream so
we're going to be listening for data to
come in
and we're going to call different
functions whenever we get a new message
and when we open a connection so we're
going to run the websocket
and then each time we receive a message
we're going to be receiving some price
data
and then when we receive that price data
we're going to be processing that price
data in the form of candlesticks
we're going to look at the close of the
candlesticks and apply a technical
indicator
to those closes in real time and
whenever our rsi
is above the overbought threshold we're
going to execute an order
and then when our rsi is below the
oversold threshold
uh we're going to execute a cell so
you kind of see where we're going with
this so i'm going to show you how to do
this step by step
so first i'm going to draw a diagram and
then we're going to install
some of the dependencies so let's get
started with
actually building this thing out so if
you've been to this channel before you
may notice i'm trying a few different
things this time i'm trying a little bit
better camera and lights
and this arm here to see if i can move
around a little bit better rather than
putting a webcam on top of a set of
books and things like that
and i'm also going to try some
illustrations with the ipad here
to see if that helps explain some
concepts a little bit better
so in this tutorial i'm going to start
off with a diagram of what we're
building
and so i'm going to draw uh binance here
so this is going to represent
binance right
and what binance is going to do for us
it's going to going to be
our broker so we're going to execute
orders through the binance api
but uh it's also going to be our data
provider and so our data provider
and our broker are not always the same
thing in this case we can get the data
from binance
and there's a variety of ways to get
data from binance we can use the rest
api to pull data
from binance as we did in the previous
tutorials
but in this video we're going to use
websockets and so the way websockets
work right
is websock binance provides a bunch of
streams so
there's the streaming data coming
through and those
streams all have different names and
what we can do is have a client
connect to one of those websocket
streams
and maintain a persistent connection
rather than always requesting data with
an http
http request and so uh binance
you know it's producing these streams of
data right and let's say one of these
streams of data
is the bitcoin prices right so it'll
have bitcoin usdt
uh ethereum usdt right usdt
and then maybe it has litecoin and a
bunch of other cryptocurrencies so these
streams are
all being pushed out at all times and so
this data is being published
and on these streams we have streams of
price data right
and so what you'll get is open high low
enclosed data
candlesticks and also individual ticks
that make up those
uh candlesticks so um we'll get a price
for ethereum for instance of you know
432.21
and then 432.32
and so forth right and so there's a
bunch of prices coming in
and as new orders come in these prices
fluctuate up and down right
and so it provides these streams of data
and then now all we need to do is
provide some type of client
to connect and read this data from this
websocket server
right and so we've already built a
couple of different clients before
we've built the browser client right and
we use
javascript to connect to this stream of
data
and then we've also used
wscat which is a
node.js tool that we can use from the
command line to connect to this
data stream and then now we're going to
use the python
websocket client right python
websocket client socket client
and this is going to be reading data
from these streams
so this data is always being provided
and we just need these clients to
walk up to this beautiful river of data
and drink this data from the stream
and then in our little python client
here
we're going to read in this data right
and so
python right is going to
get some data coming in
and we're going to process that data and
so we're going to be interested
in just the closing prices so it might
be closing prices of one two three four
5 right and then we're going to use ta
lib
to apply the rsi indicator but we could
apply any of our candlestick patterns
we've talked about in previous videos
and we're going to apply the rsi
indicator to this series of price data
and then based on the value of that it's
going to give us a value like 42.31
and so forth and then if that number is
greater than 70
we're going to hit binance as a broker
here
okay binance is our broker and they give
us an api for buying and selling so if
it's greater
than 70 we're going to issue a cell
http request to binance and if it's less
than 30
then that means the stock is oversold
and we're going to issue a buy
right and that's the basic mechanics of
what we're building
we can add you know different logic here
to
uh you know and use any indicators that
we want
any patterns whatever you want to fill
in here but i'm just showing the general
flow of data
applying a strategy to that data and
then issuing buys and sell orders
so now that we have the diagram written
out
let's go ahead and install the websocket
client and ta lib
so that we can start processing this
price data so as usual i have a new
visual studio code
editor open i have a new folder i called
it rsi bot there's nothing in it yet
i'm going to create a new file called
requirements.txt
and this is where i usually keep track
of the different python dependencies we
have
and the various third-party libraries
that we rely on
and so we're going to need a few
libraries in order to
build this project the first thing we we
rely on is python binance
so if you look this up finance
there is a wrapper around the binance
api
it provides some easy to use methods for
placing orders
and creating a binance client so you
just need to install
python dash binance the next thing we
need is
ta lib which is the technical analysis
library
so if you look up python talib you'll
see a reference to that
and all we need to do is do a pip
install
of that and so if you go to the
documentation here it has some
installation instructions and i've done
this in other videos as well
so you do pip install ta lib some users
of windows have reported they have
different instructions they need to use
so make sure you review that a lot of
people are installing
installing it separately using a wheel
file since windows is a little bit
different in this regard
on osx i had to type brew install ta lib
first i believe
and then install it with pip okay so we
need python binance we need ta lib
we need numpy which is used for
calculations
on numpy arrays so it provides a way to
perform
uh really efficient um optimal
calculations on series of numbers all
right
and then we also what else do we need
i think that's it oh we also need the
websocket client for python
and so if you look up uh python
websocket client uh you'll get
this page here and you see the package
is called
websocket client and so we need to
install that as well
i already have these packages installed
so if you don't have them installed you
need to go ahead and do that
so i'm going to create a new file called
pythonbot.pi
and i have my visual studio code editor
configured
to activate a virtual environment and so
if i run this you'll see it'll it'll run
this automatically
and i'm already have this virtual
environment activated and so i just need
to do pip install dash r
requirements text and it will install
all of the packages
that are listed in this requirements.txt
and so we should have everything
good to go now and so now what do we
need to do
so we're going to import
websocket which is part of this a
websocket client package
and then we're going to go ahead and
create a new
websocket client so we're going to do ws
equals
websocket dot web socket
app and then we need to give it a
socket to read on so we need some type
of a stream that we're going to read
and so we need to look at our binance
documentation
right and see the names of the different
streams we use
and so if you look here at k-line
candlestick streams
they provide some streams and then we
get
all this price data and so you see we
get a time stamp here
and then we also get this open high low
and close data
that they provide so we just need to
find the url format that they use
so if we look uh for wss colon
slash you see we have this base endpoint
of
stream.binance.com so this is where all
the data streams from
and so let's create a variable uh for
that we're gonna call this uh
socket and so we'll put that in quotes
and that'll be
the base there and then we also need
the address for
the stream so we need a slash ws so
we'll need a slash ws
and then we need to give it a stream
name and so if we look
at our candlestick streams um we should
see
oh yeah so we just need symbol and then
at k line and then an interval
so i'm going to put that in there as
well and so we need a symbol
so the symbol we're interested in is
ethereum usdt
which provides prices for ethereum and
then we want
candlesticks or k lines and then we need
to provide some interval here
and so which stream are we interested in
so they provide streaming data across
multiple time frames
and so binance will give us a one minute
candlestick stream
three minutes five minutes 15 minutes or
a daily stream
or whatever time frame we're interested
in and since we're trying to
retrieve data more frequently and want
more candlesticks
so that we can show this in a video
we're going to do a short time frame
so we're going to use the one minute
candlesticks so instead of interval here
i'm going to put uh 1m and so that'll
give us
our socket and so our websocket app
needs to listen
on a socket and then it'll connect right
and so what this websocket app requires
us to do is to give it some callback
functions
so it'll connect to this socket and then
it wants us to define a variety of
functions here
and since i have the visual studio code
python extension installed i get some
auto completion here so i can see
what's available and so what we need is
a function
for when we open the connection we need
it to call a function
we need to provide it a close so when
the connection is closed we need to give
it
a function to execute to show it what to
do and then we also need an
on message and this is the most
important one right
and so we give it those parameters and
so we can define these functions here
on open and then
like that and then we can print opened
connection
and so we can say on open equals and
then we pass it the name of this
function
right on open equals on open on close
equals on close so i'll define
the logic i want to execute when the
websocket is closed
so print
close connection right and then on
message
right and then we can print received
message
right and so we pass it these callback
functions so
after we connect we receive all these
different events when we're connected to
the websocket
and then we tell python what functions
to execute
when these events happen and so let's
see if we can run this
okay and we need one more thing we need
ws
dot run and it's called run forever
and we run that we initialize the app we
give it the callback functions and we
say run the websocket app
and let's see if this actually runs i'm
gonna hit my play button here and run
the bot
and it says error on close takes
positional arguments
but one was given all right so let's
look at our websocket documentation here
and see what it needs and so it actually
needs a reference to the websocket so
that'll be called ws
right and so each of these functions
need
to receive an input of ws and the
onmessage1
receives an actual message and so we
need some input parameters
so these get a reference to the
websocket
and then this one gets a reference to
the websocket and a copy of the message
right so i added those input parameters
and now i'm going to run it again
so i ran it and it just said closed
connection it didn't say open so there
must be something wrong with our
connection
so let's see what happened so if i look
up here you'll notice i actually left
this angle bracket in
oops now run it again
and now it says opened connection and
now it says received message
received message so we're receiving
messages successfully but we don't know
what messages we're receiving
and that's what this reference is for
this variable
message so every time this onmessage
event fires
we receive the message itself and so
instead of just presenting
printing receive message let's print the
actual message
and so i'm going to stop that and run it
again you'll see it opens the connection
and there look at that just like that
we're receiving messages and those
messages
have open high low enclosed data on
the candlestick so if you look at the
documentation here
you'll see that we get this json data
format
and in the attribute key or the
attribute k
here actually has the candlestick data
that we're interested in
and so we can parse that data out and
start using it
and so let's see if we can parse this
out so
we're receiving this data in json format
and so all we need to do
is parse those messages and so to do
that
we need to import json because we're
getting json data
instead of just message we're going to
do a
json message equals json.loads and so
what that will do is take a json string
and it will convert it to a python data
structure that we can use
and so i'm going to do json message
equals json.loads
and i can then print the json message
right and it's not that easy to read
this here and so i'm going to use
the python pretty printing functionality
so i'm going to also import pprint
and so if i do a print dot
p print json message like that
this will give me a little easier format
to use and so
let's think about what data we're
interested in here right so we're
getting our price data for ethereum
and we see uh the opening price was
429.46 for so 429.46 right
and so uh we're getting this candlestick
data in in real time right
right and you'll also see what it does
is
um the timestamp so this t here is a
timestamp
and so if i type that into
unixtimestamp.com for instance
you'll see what time it is where i am so
it actually gives it
this utc time and so you'll see this is
for august
16th at 1 in the morning utc
time and i'm like 7 hours before that
all right
so we have these time stamps coming in
and then we know the prices that
correspond
uh to that timestamp so that's good
um but we don't really want like all the
data so this is
this is just streaming data as it comes
in
more and more prices right but we're
only interested in the open high low and
close like the final value
for that candlestick and so what i've
noticed is
see that x equals true there that'll
tell you
when it's the final value of the
candlestick right
so if you look at this documentation
you'll see x
false is this k line closed and so
this tells us if it's the last tick that
we received so this is the very end of
the candlestick right
and so you'll see these time stamps are
going to stay the same
between between data data points
right so you'll see that's 8 000 at the
end you'll see that one
is 8 000 thousandth in so we're getting
more and more data but it's for the same
candlestick and it's only when that x
becomes true
that we got like a new candlestick has
started right so i'm gonna stop that
real quick
and let's let's think about this so
since we're going to be using the rsi
indicator all we really care about
is the close of each candlestick so i
just need to
capture the closing value right so
we have since we know that when x is
true it's the close of the candlestick
we can say is candle closed
closed equals and then i also want to
reference to just
this part so i'm going to do a candle
equals
message k right and so i'll say
is candle closed is candle
x and then the candle closing price
so the close is candle and then
c is the close so we have a ref an easy
reference
to whether the candle just closed and
what that close was
so if uh is candle closed
print candle closed at
dot format close and then let's just
print
the closing price of each candlestick as
it comes in
so if i run this again you'll see that
data starts to stream in
and it says stream string indices must
be integers
oh and that's because i renamed it from
a message to json message
so let me just call that json message so
candle
is a reference to json message and then
if i run this again let's see what
happens
all right and so we're streaming in data
and we're only going to capture
the final closing price of the candle uh
when it actually closes
okay right there you can just see that x
became a true right there
that means it was the very last tick of
that candlestick
so x is true and so we printed our
candle closed at
and so the closing price for that candle
was 429 31
and then we continue to stream you know
data second by second
until we get another close and so we
have a way to get the closing price
so now what we want to do is we're
thinking in terms of the rsi
indicator which uh can process our data
based on a
series of closes and so what we want to
do is actually
track a series of closes so
at the top here i'm going to create a
global variable
and i'm just going to call it closes and
start it as an empty list like that
and then here i can get a reference to
that
so i can do global closes and so if the
candle is closed
let's go ahead and append closest dot
append
the close right and then
over here i can just print closes
and then i can also print the actual
closes themselves so the string closes
and then the closes and then you'll see
these start to build up over time
and so we'll eventually get a series of
dozens and hundreds of closes
minutely closes right and then what
we're going to do is convert those to a
numpy array
and then have ta lib apply an indicator
to those closing values
so you can see i've just rerun the
program again and it
built a list so we just appended a
closing candlestick price
to our list of closes and printed out
the list
and it looks like it printed out as a
string so we're getting a string
so for numpy to operate on that we need
to actually convert this to
a number and so instead of just
appending the close
there let's go ahead and cast it to a
float and so we'll do float of close
and just uh keep uh keep appending
these numbers to our list and eventually
you'll see this list starts to build up
and so yeah that's pretty much all we
need for the websocket client part of
this
we're successfully getting ethereum
pricing pricing data from the websocket
similar to the diagram i showed so the
second part of that is we need to talk
about the rsi indicator and ta lib
and how it applies to these closing
prices
so i'm going to let this run and
continue to build up candlesticks for a
while
and then let's go back to our diagram
and
show you how the rsi indicator works and
i know a lot of people already know that
but i want to break this down even
further step by step and try to teach
these
principles from scratch so in my
previous tutorials on binance we already
talked about the rsi a little bit we
showed it
using trading view and also wrote a back
test to show us the results of trading
with the rsi
sometimes on some time frames it works
out well and other times
it works out poorly and you lose money
and so
for the examples we gave we looked at
bitcoin over the daily time frame for
the year 2020
here and you see uh this is bitcoin and
in march
it tanked along with the market right
and you can see the rsi goes below
if you look at the bottom here under 30.
uh so it's oversold there
you make a buy we're probably getting
bitcoin below 5000. so 4 000 something
we bought it when it's oversold we got
one oversold signal
perfect and then we get this overbought
signal and sell it
um at what uh over eight thousand nearly
nine thousand
uh per bitcoin and so that's a that's a
great trade right you
made over fifty percent you got one
oversold and one over bought you make
one trade
not many commissions to pay a great you
know fifty
sixty percent return so uh that's
awesome that's what we want to happen
but we showed how that's not always the
case you can uh
show your strategy here and it looks
like it worked for one time period
but then often uh as we demonstrated we
wrote this backtrader uh backtesting
uh function here where we downloaded
15-minute candlestick data
for binance for the year and you see
here we showed where uh these uh
entry points are not always great so we
should we talked about some of the flaws
right so
here uh bitcoin went down
and it flagged oversold actually right
here right
right here and then you see it popped up
real quick and then
gap down even lower here and so you can
see where it got even
more oversold on this particular chart
and
and so you are underwater with your
position so one thing that can happen
is something that's oversold can stay
oversold and
an asset that is overbought can stay
overbought and you miss out
on a lot of your returns right and so
this is a particular case where we
showed where you could sometimes be
underwater by trading with this strategy
so the important point
is you know to back test your strategy
over multiple time frames or at least
have a back test to apply that applies
to the time frame that you're using
and then the other lesson is that
usually people don't use the rsi
indicator by itself usually they use it
in conjunction with other strategies
like the macd
or other indicators it's not often
a good idea to use it by itself and the
reason we're using it by itself here is
because this lesson isn't necessarily
about
uh finding the optimal strategy or
combination of multiple
multiple indicators the idea is simply
showing
a real the mechanics of subscribing to a
websocket data feed
uh applying some technical indicators
using talib to that data feed
and then executing those buys and sell
orders against our brokerage account
uh using a simple set of rules and so
this provides us kind of a hello world
example
of a similar setup a simple set of rules
similar to most hello world examples are
like a moving average crossover strategy
and so this is an rsi strategy and we're
going to buy and sell based
on whether the rsi is overbought or
oversold
right and so that's great we're looking
at this rsi
oscillating on a chart but you know we
haven't really talked about like how do
you even calculate the rsi where does
that number even come from
and so i'm going to go even more in
depth in this tutorial
and draw a quick picture and do some
hand calculations to show you how that's
calculated
if you don't want to dive into that much
detail feel free to skip along
but i'm trying out some different
teaching techniques in this particular
video
and so i'm going to work on some
diagrams here and illustrate
how this number is actually calculated
so let's talk about the relative
strength index a little more
and to start the discussion i'm going to
pull up a textbook
example an analogy that i got out of
this book it's a technical analysis
explained
by martin pring pretty good book i'll
put a link in case you're interested in
it
pretty lengthy book but i took a couple
snapshots
that i'm going to show on the ipad here
so on the ipad here i have a couple
screenshots from
the book and this is on the chapter on
momentum
and so this analogy in the introduction
i thought was pretty good so the concept
of upside momentum is illustrated in the
following example
when a ball is thrown into the air it
begins its trajectory at a very fast
pace
it possesses strong momentum the speed
at which the ball rises gradually
diminishes
until finally comes to a temporary
standstill the force of gravity then
causes it
to reverse course the slowing down
process known as the loss of
upward momentum is a phenomenon that is
also experienced in financial markets
the flight of a ball can be
equated to a market price the price's
rate of advance begins to slow down
noticeably
before the ultimate peak in prices is
reached
and then it goes on to talk about if you
throw a ball inside of a room and it
hits the ceiling it bounces back down
and then on chapter 14 of the book on
the first page it shows you the rsi
formula this is all over the internet
nothing specific to this book but i was
just pointing out
the formula the relative strength
indicator was developed by wells wilder
it's a momentum indicator oscillator
that measures the relative internal
strength of a market against itself
and then we have this formula rsi equals
100 minus 100 over one plus rs
where rs is the average of x days up
closest divided by
the average of x days down closes and
i'm going to show you how to calculate
that
using ethereum price data that we're
getting from these web sockets right now
so we're going to go back to our program
and we're going to get some price data
and i'm going to write it down
and calculate an rsi to show you what an
oversold
condition or value looks like on the
relative strength index
so by default we're going to be using
the rsi 14 which is 14 periods
and we're using minutes in this case now
whether or not the rsi
is meant to apply to ethereum data over
the course of
minute candlesticks that we're not going
to get in that debate right now
typically in the past they've applied
this to daily time frames but i'm going
to apply it to the minute time frame
and show you how you would calculate
whether something's overbought or
oversold on the minute by minute
uh time frame right and so what i've
done here
is got some data points that we got from
our ethereum data and i wrote down the
minutes
so to calculate uh the rsi 14 right
we need 15 data points actually right
and then i have the closing prices here
and so what i have is two columns here
up and down
and we're going to get the difference to
show how much the
uh price of ethereum went up or down on
each uh
between each of these candlesticks and
so here from 44 32 73 to 432 80
this went up by seven cents right
and then it went back down right by
seven cents
then next it went down by .03
next it went up by 0.16
and then it went down pretty strongly by
what's at
0.67.67
and basically we just keep filling this
in on our chart and i've
calculated this in advance just to save
a little bit of time
so we actually had more down days than
updates in this particular case
and so i have the values of .01
0.2 and 0.18
and then for downward changes we have
0.07.03
0.67 0.04
we have .05 0.03
0.35 0.29
and .07 right
and so you can see that obviously we
have a lot more down days
so for these 14 price changes we have
nine down days and only five
updates so it's very likely this might
be oversold
because it's had strong downward
momentum like for instance this is a
large change in price down and so is
this one
and so is this one right and so what we
want to do is add up both of these
columns
and divide them each by 14. so for the
sum of our updates
i have 0.38.39
0.55 so that would be 0.62
and we would divide that by 14
and then for the sum of our down days i
got
a 1.6 so 1.6
which we'd also divide by 14 right so if
we look back at our definition of rsi
we have rsi
equals 100 minus
100 divided by 1
plus rs where rs
equals the average gain
over the average loss right
so our rs equals the average gain so the
total gain divided by the period so 0.62
divided by 14 is
0.044 0.044
and then divide that by 1.6
divided by 14. 1.6 divided by 14
is 0.114
0.114 okay
and then i can just divide both of those
values point
zero four four divided by point one one
four
and i get uh point eight three or point
three eight six
rs equals point three eight
and so we do 100 minus 100
over 1 plus 0.386
right and then so
if we do that we get
1 plus 0.386
so that's 100 divided by 1.386
which is 72.15 and then if we do 100
minus 72.15
we get an rsi of 27.85
that number is less than 30 and that's
how we calculate
an oversold signal and so
on the other hand if on this column we
had a whole bunch of updates that were
really strong
updates and the upward price momentum
was a lot more than the downward price
uh momentum then we would have 100
minus a smaller number and if we got a
number over 70 that would indicate an
overbought
value all right so yeah that's how you
calculate the
relative strength index on paper
and now what we're going to actually do
is install ta lib
since someone's already written a python
library to calculate all these things
for us
because it'd be very tedious for us to
keep track of all this stuff on paper
i picture people doing this decades ago
like calculating this
on paper but there's no reason you know
we have python to calculate these things
for us
we have python to analyze data for us so
there's no reason for a reason for us to
stare at charts all day this can all be
automated so
let's try to automate all of this
trading for us
okay i'm back in visual studio code and
we've been watching all of the
price data stream in from the binance
websockets
and when it's a clo the closing tick of
a candle
we've been capturing the final open high
low and close
data for that candlestick and we've been
appending
the final close here um into
a list called closes and so you can see
we accumulated a number of closes
so now what we want to do is apply ta
libs
rsi indicator to this series of closes
and get an rsi value so
in order to do that we need to go ahead
and first import ta lib
so at the top here we've already
imported websocket
json and print and so we're going to
import
ta lib and numpy because we're going to
need those
for our calculations all right and then
also we're going to set up a constant
for our rsi that way we can
tweak it so i'm going to say rsi period
equals 14 and these are just the default
settings that people
uh normally use but we can adjust these
if we want it to be more overbought or
more oversold or we want to do a
different
uh period so i'm going to do rsi
overbought equals 70
and rsi oversold
equals 30. and those are going to be our
various thresholds that we're going to
use to determine whether
to execute or buy or sell order okay
i'm going to create another constant
here called a trade
symbol and we're going to be trading
ethereum
here and so this is the symbol from
binance it that we figured out from
our lat one of our previous videos and
then we're going to determine a quantity
that we're going to buy
so i'm logged into my binance account so
i can check the prices if you don't have
a binance account for trading
i'm going to leave a referral link if
you want to support the channel and want
to sign up using that link
feel free to do so so in my finance
account
i say buy crypto and i'm just going to
see how much ethereum happens to be
so let me see what 20 worth is well
i'll go to the symbol for ethereum right
and let's say i want to buy a
twenty dollars worth looks like that's
about point zero four six so i'm gonna
just say point zero five
ethereum is the amount i wanna buy so
i'm gonna do uh
trade quantity as a value and i'm just
going to store this
and these are just going to be our
settings for our bot and then we can
tweak them if we want to adjust our
bot's behavior
so we have those uh constants in um
and we're listening on the websocket
already and so the next thing we're
going to do
is keep track of the number of closes so
we've been appending
uh the closes as they close we're
appending it to this list
and to calculate the rsi we're going to
calculate the rsi 14
and so to calculate the rsi for over 14
periods right
as we calculated on paper um
you know the first just one or two
closes isn't enough so we need
at least 15 closes in order to even
start
calculating an rsi that we can use and
so we're going to say
first of all if the length of closes
so let's make sure we have 15 closes
first is greater than
rsi period right so if we have
15 closes which is greater than our side
period 14
then we can actually start using a ta
lib right
so we have a list of closing prices that
we've been accumulating
and we've converted them to floats in
the latest version of the code
and for talib to operate on this
it expects a numpy array so we're going
to take this list and convert it to a
numpy array
and so i'm going to say
np closes equals numpy
and you can just do numpy dot array and
just give it a regular python list
and that'll convert it to a numpy array
of closes and then to calculate the rsi
we can do rsi equals talib dot capital
rsi
and so if you didn't watch previous
videos
if you search python ta lib pull this up
you'll see there's a number of
indicators available
and if you look at momentum indicators
here
and look at rsi you have this rsi and
you just need to close
and then the time period by default is
14 but you can override that
so we're going to just use the default
period
and i'm going to pass it an array of
closes
so we're going to call the rsi function
which is part of the ta lib package and
we're going to
pass it np closes which is our numpy
array of closes
and we're going to give it the default
period or we can just pass it
you know rsi period here and if we
change the period to 10 for instance
a 10 would go here which is good so then
we'll start calculating rsi values
so what this is going to do is calculate
a series of rsi's
values so once we get 15 closes we'll
get one rsi value but on the 16th close
we'll get the second rsi value and so
forth
so this rsi is going to actually be
multiple values and i'll show you what
that looks like
in a second so let's go ahead and just
print out the rsis that it's calculating
right
and so i'll print
all rsis calculated so far
and this will be a series of rsis and
then what we also want to do is get the
last rsi calculated right
so the last rsi calculated is going to
be the one we used in order to make our
trading decision
and so we want to get the last rsi and
set it equal to
the last value of this series and so we
can use
a negative index to start from the end
so rsi
negative one there is going to be our
last rsi value and we'll print that as
well so we'll just say print
the current rsi is
and dot format and this is just for
debugging purposes
and i'll say last rsi and then what we
want to do is say if
the last rsi is greater than our rsi
oversold or overbought right if the rsi
is greater than our overbought
then we can print sell
sell sell sell right
and then we can also say if last rsi
is less than rsi oversold
then we print buy buy buy
right and so we have a condition
under which to buy and sell so there's
one problem with this so far
is if we were to buy when ethereum
is oversold right if it's less than rsi
oversold
what happens if is uh what if the rsi is
29
and then the next minute it's 28.5 and
the next minute it's 28 right it's still
oversold
each time around so we would keep buying
over and over again but that's not
really what we want to do
for this particular strategy maybe
there's another strategy where you'd
want to do this
but we want to just buy one time a fixed
amount when it's oversold the first time
and then if we're already in the
position we don't want to keep uh
keep buying more and more and likewise
if it's already overbought
you know we we're going to just sell
what we have we're not going to sell it
in pieces or anything so
what we want to do is check if we're
already in the position that way we
don't need to keep
buying over and over again because we
don't have unlimited
money we're working with we're just
looking to risk about 20 bucks here
right and so what we want to
do is check if we're in position first
so uh
up at top here we're going to keep track
of the state
so at the beginning of our program when
we first started we're going to say
imposition is false
right we don't own it and then over here
if the rsi is oversold we'll first check
if we're in the position right
and if we're already in the position we
can just say it is oversold
sold but you already own it nothing to
do
right so there's nothing to do here
otherwise if we're not a position
then we want to buy right so we're going
to leave a comment here i want to say
put
finance order logic here right
and on the other side of the coin here
when it's overbought
if we're in position then we want to
actually sell
right so we'll plug in our logic for up
so we'll say put binance
order as cell logic here
and i'll put by order logic there
otherwise if we're on the position
there's nothing to sell
and so we'll just say print
we don't own any
nothing to do right
so we'll say it is over bought but we
don't own any
right
all right so we have some messaging in
place and so we have the mechanics of
this
working and so now all we need to do is
actually
fill in the binance ordering code and so
i'm going to run this real quick and let
it run for a second
just to make sure we don't have any
errors which we do have an error so let
me fix that
so i forgot an equal sign and let that
run for a while
and make sure we're still receiving
messages we're still receiving messages
which is good and this should keep
running so now the next thing we need to
do
is we need to pull in some uh the code
to actually
buy and sell crypto using the binance
api
and as we said earlier we're using the
python binance package
so if we look up python binance api you
get
this library here
and there's methods for ordering
and let's find those ordering endpoints
so under account there's order endpoints
and place an order right so it's
client.createorder
and now we're going to piggyback off our
previous video if you followed along
we built out this app.pi here and i'm
going to pull it
in just to show you what we did in
previous videos if you want to learn
more we actually built a web application
where you could buy and sell crypto
and we built a front end for showing
real time charts and just to show that
off real quick i'm going to do flask run
and i'll post the source code for this
as well
and i think i already have it running
actually so i'm going to run it on
localhost 5000
we called this coinview and
um here we were actually plotting the
real-time
prices of bitcoin so we called the
application coin view it let us buy and
sell crypto from the web it showed our
current balances that we had in our
binance account
and were able to tweak some settings and
so we could actually use this as the
front end
for this bot and we could enter in our
rsi settings here
and publish that to the websocket for uh
our client to read and then you know we
could tweak our settings from the web
similar to how you can do
uh with training view so this we're
talking about how you would build uh
that type of functionality yourself so
this is the front end we are working
with
um and what i wanted to show you here is
the actual source code
so this is available in the repository
i'll post a link
below but we had in our app.pi
we have a
binance client and so we're going to
copy what we did here
since we've already written this code
before so we need the binance client
and we need these enums which are some
constant values
that we're going to use so i'm going to
copy these two lines
here and i'm going to put them in our
program
so i'm going to import the binance
client
and i'm also going to copy this part
where
we instantiated a new client and so
we're just going to do this at the top
here um we're going to
create a new client okay
all right so we have some constants we
have a client object we're going to
instantiate it
and we're going to put a config.pi
with our api keys and so we need an
api key and an api secret so i'm going
to do
api key equals and api secret equals
and i'm going to fill this in with my
api keys from
the binance site so if i go back to
binance
you can look at your account and there's
this api management here where you can
find your api keys
and copy them to this config and i'll
put these in here and not show you mine
now that i have my api keys inside of my
config file
i just import config
and then i can do just like this config
dot api key and config.api
secret and it'll use my api key and
secret from this config file
and then i'm not going to commit that to
the source code repository
but i'll put a sample config
dot pi where you can fill in uh your own
or i'll commit i'll commit it with empty
strings just so you know
um to uh fill it in there and then
you'll notice i have this tl
d equals us there and since i'm in the
us and i'm using binance us
it required this tld parameter here or
it didn't work i forgot the error it
gives but that's the reason that's there
so make adjustments to that as needed so
now that we have our client instantiated
we're just going to make a quick
function for ordering so we need to
decide what that looks like
so let's go back to the app.pi that we
had for
our other application where we actually
create buy orders so we had this by
endpoint
that we you can use from the web and the
same functionality can be used in a
function from the command line
and we can abstract this out and build
our own little library if we want to but
it's pretty abstract at this point
because we're already using the python
binance client
and so our code can be reused inside of
our command line program
and so i'm going to make an order
function here
and we're going to have that order
function accept a couple of things
looks like we need a symbol right and
since we're not using a form anymore we
want that symbol to come from
a function input so we'll have a couple
parameters
so to order something we're going to
need a symbol
we're going to need a quantity that we
want to buy
and then we need a side so whether it's
a buy or a cell
and we need an order type right and so
we're going to call this function from
our websocket client and we're going to
give it the necessary parameters
to buy or sell so the symbol is going to
be the symbol that comes in when we call
the function
the quantity is not going to come from a
form it's going to come from our
function call
so uh right now it's filled in with side
by and order type market these are
actually
constants that come from this pi uh
finance enums library and you see they
already have a bunch of constants
defined
and some of those are side by side cell
these intervals and so forth so
that way they can define these string
values all in one place and so we're
just importing them
and using them right here and i want to
be able to make this function work for
both
buying and selling so i want the side to
come in as a parameter
and so i'm going to use side that comes
in from here not the constant
and then i'm just leave the order type
mark it
actually i'm just going to give it a
default order type of market
and then you can override it if you want
to and so
type equals order type and then so
uh the parameters from what the
websocket we're going to call
we're going to say quantity simple side
i'm going to change the order of these a
little bit i'm going to say
side so it'll be by and then i have the
quantity
and then the symbol that seems more
natural to me so it would be like by
.05 ethereum okay
so that's our order function so
client.createorder
and let me just verify this one last
time
right so i'm gonna put these on one line
so it's easy for me to read
okay and so what we'll do here
and let's wrap this in a try catch
block that way if there's any exceptions
from this we'll say the order failed
so i'll choose try right
and then let's just return true let's
print out what happened so let's print
out the order
and then we turn true so we'll say
print sending
i'm going to print sending order so
we're going to print a lot of debugging
information because we're going to run
this for the first time soon
so i'm going to print sending order
order equals create order
with these parameters i'm going to print
the order and return true
if for some reason exception happens uh
let's return false here
okay matter of fact though we can return
true at the end here
so if no exception happens if all this
stuff happens and we'll return true at
the end
if an exception happens we'll return
false all right so that's
our order function okay and so now all
we got to do
let's just get our function signature
here
and let's just call this from within
here where we said to put our binance
cell logic in by logic
so we're going to say
order succeeded equals
order and then we're going to say side
is
uh side
cell when it's overbought quantity is
going to be the quantity
that we defined at the top so we said
trade quantity we called it so we'll say
trade quantity
and trade symbol which we have defined
at the top
and our order type is going to be market
so we'll leave that as the default
take off this colon all right so that's
order succeeded
so for in position and the rsi
that was last is overbought then we need
to sell
so we're going to do an order cell or
quantity
of a certain symbol and that looks good
and then if the order succeeded
then we're no longer in the position so
we can set in position equals false so
so that we don't keep trying to uh sell
it over and over again
and then otherwise
then uh it's overbought we don't own any
okay that's good
and then let's look at our oversold
condition
so if last rsi is below the oversold
threshold
we can do and
and if we're not in position yet let's
put our binance by order logic here
so we'll say order succeeded
equals order side by
trade quantity trade symbol
so we'll buy 0.05
ethereum and if the order succeeds
we'll say in position equals true
because now we own
ethereum all right
and i think that's all we need if
there's no syntax errors
we should be able to just run this
program and have it
buy and sell orders in our binance
account and have it run overnight
and come back and see if we made or lost
money and make adjustments from there
and we can just leave this running
forever
so i'm gonna stop the program i'm gonna
run this just to make sure there's no uh
syntax errors
it looks like um it's running
so it looks like there's no fatal errors
at the gate and so i'm gonna let this
run for a while and just see what
happens
and see if we can capture any buys and
sells and so
i'll come back and edit the video out if
this runs for hours and hours
it's possible it can run you know for
hours and not trigger
an oversold conditions so it just
depends what happens i'm running this
in real time i have no idea what's going
to happen and so it looks like it's
running
and then we had a close of 428.29
and it's 9 40 pm pacific time on
saturday
and so we're receiving messages i'm
gonna let this accumulate we need to get
15 minutes of candlestick data first
before we even get a single rsi
so i'm going to walk away and come back
when
this is further along so i'm back and
i've let the program run for a little
while
it's after 10 pm and i'm looking here
and you can see how this program has
been running
and it gradually calculated and stored a
bunch of closes
in a list here and we kept showing our
closest ss list screw
and then eventually it grew to 15 closes
and
luckily enough for us it didn't take us
a long time
to get an oversold condition it actually
happened pretty quickly
just a coincidence of when we started
our program but you can see
that once we had 15 candles it started
calculating an rsi value
and the very first rsi we got was 26.11
and it was actually an oversold
condition all right so if you were
watching really closely earlier i didn't
know that
noticed this until after this caught the
oversold condition
but the buy order actually didn't
execute after
the oversold condition hit and there's a
reason why so if you look
in our oversold code here order
succeeded equals order
and i had trade symbol twice when i
needed trade
quantity and then trade symbol and
that's just a result
of letting the autocomplete work and i
hit tab too early
and so it filled it in with trade
symbols so need to double check that
there and so i fixed that
and ran it but unfortunately i can't
show you the buy execute in real time
just since
because it actually didn't happen so we
successfully detected the oversold
condition but didn't execute the buy
so what we can do right now is show you
what would have happened real quick so
i'm going to go into trading view here
now that a little bit of time has passed
and i'm going to show you uh the results
and so it's actually this period right
after 10 pm here
ta lib actually calculated this was an
oversold condition
uh right at this point a little after 10
pm so we would have made the purchase
here and then uh the overbought next
overbought condition would have been
after these powerful candlesticks up
here
and so we would have bought it around
here and sold it around here
and that would have been a profitable
trade which is good
however that doesn't mean that much
right
because i actually let this program run
overnight so
this is the next morning that i'm
recording this right now
and so i let this program run from
midnight
till 9 00 a.m and just went to sleep
and let this execute trades while i
slept
to see how good our trading bot would
perform
right and so now that it's the morning
i'm going to see what happened right
and so i'm going to expect that i woke
up this made thousands of trades and i
woke up and made millions of dollars
right
is that what we expect i don't know
let's see what happened all right so if
i go into my order history
in binance uh you can see what happened
and also to show you uh
that i really ran this overnight i can
go to my editor
and you can see where we
accumulated tons of prices in these
closes right
and you can also see our messages like
overbought but we don't own any nothing
to do
candle close and so we have hundreds of
data points here
and hundreds of rsis that are calculated
right
and so we need to see what actually
happened right
and so if we go back to binance let's
see if we made our millions of dollars
i'm gonna go to my order history
starting after midnight and see what
happened
and you'll see that just after midnight
we made a buy
and at six a.m we made a sell so we made
a whopping one trade that entire period
and so we can go back and verify that
everything worked correctly but there
was actually only one trade
and we actually lost money so you can
see
uh bit uh ethereum price was 426.68
here at 1241 and we
bought it so it must have been oversold
and then you can see it wasn't
overbought till 6
11 a.m in the morning and then we put we
sold it right and so we bought .05
ethereum that cost us 21.34 cents
and then we sold it for 21 and 13 cents
so that lost 21 cents right
there and we also had a transaction fee
so .02
right two cents on each side so 21 cents
plus four cents
so all that effort and waiting overnight
and we lost 25 cents right
but that's not really unexpected right
we already back tested
uh these types of strategies this short
time short term rsi
on its own in previous videos and showed
you know this is not really
a good strategy to use so why did we use
it
well we want to go through the pain of
actually
building something like this out and
seeing how it operates and so we did
achieve the goal the goal of building
something end to end
and studying our results and part of the
process
is getting a bad result and then
tweaking that until you find what works
for you
so like the first time i ever traded a
stock option for instance i just
went on an app i clicked buy it a weed
option or something like that and uh
and i pretty much expected to lose the
money and
just that feeling of losing money made
me actually pay more attention it like
made me learn really quickly so then all
of a sudden
i it motivated me to learn what i was
doing
it motivated me to research all the
terms and try different things by short
short term option buy one that's in the
money buy one further out of the money
buy one that's longer dated and just
kind of observe the price action and
learn from my mistakes so
what we've done is build a trading bot
based on a simple indicator
and it made a trade overnight while
we're sleeping so we achieved the
hands-free automation parts but we have
not yet
demonstrated a strategy where we made
some money consistently
we made money on the first trade but the
overnight trade
was a loss and one final thing i'm going
to do is verify that this was correct
was there only really one buy and one
cell
let's look at what happened in the price
in rsi overnight so i'll pull up the
chart
and verify that our program was correct
so that next time we run it we know that
it's working
so i'll go back to this chart and if you
look at overnight
starting on august 16th at midnight
you'll see
where the oversold reading occurs so
this is the rsi bouncing up and down
this is midnight right here and you can
see indeed
41 minutes after the hour right
you'll see that this uh price action
downwards so you see all these red
candles here going down
it had a lot of downward price momentum
and it got
oversold and that's when we made our
purchase right there
a little above 426. right
and let's see what happened why did we
lose money here
right it's oversold right uh it bounces
but it doesn't get overbought and goes
back it's still oversold but we're
already in the position
so we didn't we didn't do anything we
didn't add
goes back up never gets overbought again
and then goes all the way
back down and gets deeply oversold we're
still in the position right so we didn't
do anything
and then if we keep going you can see
that
uh it kind of downward trends and just
keeps getting oversold
uh periodically and then just kind of
bouncing up
to the middle and so it never gets an
extreme overbought reading
and so this runs just all night and just
bounces and oversold and so you can see
ethereum last night at least uh kept
selling off over and over again so it
would come back up keep selling off and
getting oversold again so
looks like overnight if we look i'm
zooming out
right and so we started with ethereum at
428
and then it just gets oversold and it
looks like it just starts this big
downtrend overnight
from about 3 a.m till
6 a.m it just keeps down trending and so
there's never really a good entry point
that would have worked uh using this
indicator and so if you would have
coincidentally
started your program here then you would
have got the entry point and felt like
you were smart right
and so you can see it never really gets
overbought until
right here uh after 6 a.m and that's
when our sell
order triggered so it's overbought and
then it got overbought
over and over again and then we just
said we're already overbought there's
nothing to do
so indeed our program was correct we
left it on overnight there was one
oversold reading where we entered
and then one overbought reading much
later
when we exited the trade and we actually
exited
as a loss because it kept downtrending
and got more and more oversold and so by
the time
it got overbought again it was actually
under the price that we originally
bought it at
so that's the lesson there i hope you
learned something from
studying what can go wrong in with this
particular
indicator and also i hope you learned
how to build a bot from scratch
because now that we have ta lib
installed and we're reading price data
you can plug in any indicators you want
or any other
algorithms that that you want or you can
plug in your you know
some proprietary data sets and all these
things
and all you got to really do is swap out
the strategy because we already have the
brokerage execution part we already have
the real-time data part
and we already have some libraries for
performing a technical analysis
so you just need to decide what strategy
you want to take
so that's it for this tutorial i know it
was a long one i was experimenting with
some new things like
different cameras different lighting and
ipad and drawing
and also i'm even trying a new uh video
editing software called screenflow i was
using camtasia before
so i'm just putting this all together
and experimenting a little bit to find
the
best format for teaching you this type
of content
and i hope you liked the video we
covered a lot of material
we learned how to connect to websockets
from the python
websocket client we retrieved real-time
data from binance
over web sockets we were able to use ta
lib which we've discussed
to apply technical indicators to this
real-time data
and we were able to in real time execute
buy and sell orders using the binance
rest api
and the python binance package so we
covered how to build a trading bot
end to end and you know this video was a
bit over an hour long so congratulations
if you were able to reach till the end
or you can watch this in chunks
later on so i'm going to post this final
source code to this
to the github repository at github.com
hackingthemarkets so you can follow
along with this video using the final
source code
and if you want to support the channel
you can sign up for binance using the
referral link below and also just
subscribe to the channel and follow
along follow me on twitter i'm going to
be posting new tutorials every single
week and my goal is to make all this
content better and better over time
and to get better at editing these
videos and communicating these
ideas so that other people can learn how
to do this themselves
so uh thanks a lot for watching and stay
tuned for the next video
```

---

## Transcript: How to Code a AI Trading bot (so you can make $$$)
**Video ID:** c9OjEThuJjY | **Words:** 6727
**URL:** https://www.youtube.com/watch?v=c9OjEThuJjY

```
this is how to create an AI powered
trading bot that's 100% automated and
makes money some of the biggest hedge
funds in the world are powered by
algorithmic trading Cel Renaissance two
Sigma but they're shrouded in secrecy
why these guys may look like your
average Bankers by day but by night
they're bulk buying Yachts they're
beating the market using some of the
most sophisticated algorithms in the
world channeling my inner wolf for Wall
Street I'm going to try to create my own
but what makes a good AI trading bot can
anyone build this or is it just limited
to hedge fundies and does machine
learning or AI even make a difference
I've been working on this for months
researching strategies using different
ml models and test coding over 600 lines
of experiments I'm going to break it
down in just five steps and it begins
with building our Baseline block all
righty guys so the first thing that we
need to go on ahead and do is build our
Baseline but in the interest of time I'm
going to set a 15minute timer and kick
things off the first thing that we need
to go on ahead and do is create a new
file and I'm going to call this trading
bot. pip we then need to import a bunch
of dependencies the main set are going
to be from a library called lumot which
sort of gives us a trading framework so
from
lumot Brokers we're going to import our
Pacer from
lumot dob testing we're going to be
importing Yahoo data back testing from
lumot do
strategies.
strategy we're going to import
strategy so super quickly our pack is
going to be broker Yahoo data back
testing is going to give us our
framework for back testing and strategy
is going to be our actual trading bot we
also need one more thing so Lumi bot dot
Trader Traders we're going to import
Trader so n why do I keep hitting the
wrong Keys uh so that is going to give
us our deployment capability if you
wanted to go and run this live then
we're also going to go from date
time we're going to import date time and
then we actually need to create a couple
of variables to to hold our API Keys cuz
we need to get some data so we're going
to create a variable called API key set
that to a blank uh string for now we're
going to create one called API secret
also a blank string and then a base URL
also a blank string now we actually need
to pass through a dictionary to this
alpaca broker over here so let's create
that so I'm going to say alpaca
creds is equal to a blank dictionary and
then inside of that we're going to
create a variable called API key and
we're going to set it to our API key
which is currently blank so we're
effectively creating a dictionary key
and we're setting it equal to what we've
got over here and that's because we're
going to need to use it in two ways
later on so let's close that we don't
need that uh let's go and set our API
secret and then we need to determine
whether or not we're going to be paper
trading so for now we're going to paper
trade cuz we're not dropping real cach
just yet or maybe we will all right so
where do we get our API key our API
secret and our base URL well if you go
to an alpaca account you can actually go
and hit view API keys and then in this
particular case we can go and hit
regenerate generate new keys we can then
copy this key let's bring that over here
so we can that's our API key we then
want our API secret where's it going
now down here on our API secret and we
also need that base URL which is this
over
here cool so once you go and generate
it's going to disappear so if you need
it again make sure you copy it put it in
a safe place don't share it with anyone
all right so those are the main
variables that we need already so what
we're going to do is we're going to
create our broker we're going to set
that to our Pacer and then pass through
our our Paca creds we'll come back to
that a little bit later okay but let's
get on to our strategy so we're going to
create a bit of a framework for our
strategy so we're going to create a new
class we're going to call it ml strategy
actually we'll call it ml
Trader to that we're going to allow it
to inherit from the strategy class which
we imported up here and so this is going
to form the backbone of our actual
trading bot so everything that we
encapsulate in terms of our trading
logic goes in sort of inside of that
class we're going to create a new
function or new method so we're going to
say def
initialize that spell that i n i t i a
yep cool all right going to pass through
the same instance just going to set it
to pass for now and then we need the uh
on trading iteration function or method
perfect okay so the initial these are
called life cycle methods so basically
when you start up your Bot the
initialized method is going to run once
the on trading iteration is going to run
every time we get a tick so every time
we get new data from our data source
we're going to be able to execute a
trade or do something with it so we're
going to effectively have all of our
trading logic in here all of our setup
stuff in here okay that's looking good
now what we actually want to do is
create an instance of our strategy so
let's do that so I'm going to say
strategy
is equal to ml Trader and then we
actually need to pass through a couple
of things to this so the first thing
that we need to do is specify a name so
I'm going to set it to ml Strat can name
it whatever you like we also need to
pass to our broker which is why we set
it up
already and then eventually we're going
to be able to specify a number of
parameters so we're just going to set
that to a blank dictionary for now but
eventually we'll come back and use that
then what we want to do is we want to
actually set up our back testing so
we're going to this is how we're going
to evaluate how well we're going to be
able to run our b or at least a guess of
how well it's actually going to run so
we've got our strategy there to our
strategy we want to pass through that
Yahoo
data so Yahoo data back testing then we
need a start date and end
date as well as any parameters here as
well and our parameters are just going
to be a blank dictionary for now so
we'll come back to that but we actually
need to create our start date and our
end date so our start date start date is
going to to be equal to date time so
that's why we imported this date time
function up here cuz it just makes it a
bit easier to create datetime objects so
for now we're just going to keep this
pretty simple we're just going to set
our date time to 2023 we're going to set
it to uh let's say halfway through the
month halfway through
December and our end date is going to be
date is going to be the end of
December we'll say 2023 comma 12 comma 1
beautiful right so these two start and
end dates are going into our back test
so if you wanted to go and test for
longer or shorter you can adjust these
we're going to bring this back way later
once we actually go and do our final run
okay but that is pretty much some of
most of the admin stuff actually now
done inside of our initialized function
we want to create a parameter so we're
going to create a parameter called
symbol that's going to be a string and
we're going to set that equal to spy so
it's going to be the Spy index for now
then what we want to do over here is
make this avail ailable as an attribute
inside of our class so we're going to
say self. symbol is equal to symbol over
here to actually pass this through when
we going and initialize our strategy we
just go and create a new parameter and
we're going to set that equal to spy
over here and I'm going to copy this
kind of a little bit of a weird way to
set this up if I'm completely honest but
it works we also need to set self. sleep
time so this basically dictates how
frequently we're going to trade so self.
sleep time we're going to set this equal
to 24 hour
we're not playing in the minutes here
but if you could if you wanted to you
definitely could I like sort of bringing
it up to a high level we also need to
set self. last
trade or creative attribute for self.
last trade so this is going to capture
what our last trade was so if we want to
go and undo some of our sells or undo
some of our buys we've got the ability
to do that right so I've got symbol
sleep time last trade that is looking
good now what we want to do is let's
just go and create like a baseline trade
right so we're going to say um if self.
last
trade equals equals none then we're
going to create a trade and this is like
sort of the backbone of how to actually
build these trading Bots we can create
an order we're going to say order is
equal to self do create
order and then we got to pass through a
couple of variables here so we need to
pass through first up the symbol so
we're going to say self.
symbol we then need to pass through how
many of that particular symbol we want
to buy we're going to set it 10 for now
but we're definitely going to do Dynamic
order pricing and position management
little bit later we'll get to that okay
so we've got self do symbol we also want
to specify what type of order we want to
create so we want to buy and then we
also want to specify what type of actual
order so the order type so whether or
not we want to Market a limit whether or
not we want a bracket order I'll explain
that a little bit more okay so that is
our order now created but that hasn't
actually executed yet so if I went and
run this we wouldn't actually have an
order generated so let's actually go and
just pass this order and then we'll
actually be able to test it out quickly
and see this working so let's go and how
do we go and execute this order so we
can say self. submit order and then pass
through that order and then just for
good measure we're going to say self.
last trade is equal to buy because
that's effectively what we've gone and
done okay so we've gone and done a ton
of stuff so let's go and test this out
so I'm going to say python trading
botp and all things holding equal we
should get a back test kickoff I am
getting these weird SSL errors you can
see one right there let's just try that
again sucking up our time not happy
about this all right that's our back
test starting so you can see take a look
it's all
done all right and that is our trade
created so you can see that right down
here we've gone and created a trade so I
bought 10 spy uh contracts at a price of
$469 49 it went and had a total of$
4,694 this is our tear sheet so it tells
us how well we actually performed kind
of crap right now so we've built our
Baseline bot but right now we're just
randomly buying 10 units here or there
how do we make sure that we're
appropriately managing our money this
brings us to part two position sizing
and limits we are now on to position
sizing and limits so rather than just
buying 10 we're going to make this a
little bit more Dynamic so let's
actually go and create this so we're
going to say uh dep position sizing so
this is a good part of actually building
and trading B position management and
cash management absolutely critical so
we're going to create our position size
and to do that we're going to first need
to get our cash so how much cash we've
got left in our account and if you
actually go back and take a look at our
trade that green line is the cash value
so you can see it started out at 100K
and then when we went and bought it went
down to 95k so we can get that
dynamically using the self. getet cach
method so we can say self. getet cash
we actually need to pass through self
into
here so that'll give us our cache we
then want to go and determine what the
last price was so we can say last price
is equal to self.
getet last
price and to that method we need to pass
through the symbol so we can pass self
dot my head Block in that I hope not uh
close self.
symbol and then we actually want to
calculate our position size our position
size is going to be calculated based on
a metric called cash at risk so how much
of our cash balance do we want to risk
on every trade so we're going to create
a variable called cach at risk it's
going to be a float and we are going to
set it initially
to.5 so then what we can do is we can
make this available as an attribute so
self. at risk and we're going to set it
to cash at risk beautiful and then what
we can do is calculate our position
sizing we should also set it down here
just so we don't forget so we're going
to say inside of Val
parameters dash at risk let's just put
this on another
line we are going to set that 2.5 and
again if you want a riskier bot or a
less risky bot you can adjust this up or
down see heart all right cool so we've
now got and added that parameter to our
parameters inside of our back test and
inside of our strategy up here as well
so what we now need to do is calculate
our sizing so I'm just going to use the
formula I've kind of always used so
we're going to say quantity is equal to
our cash multiplied by our cash at risk
so that effectively determines how much
of our cash balance we want to risk cash
at risk and then we are going to divide
it by the last price so that's going to
tell us how
many units we're going to get per amount
that we want to risk and then we're
we're going to round it down to the
nearest whole
value perfect so this should give us a
single quantity so we can return our
cash our last price and our
quantity there we go all right cool and
we are going to dynamically set that
when we actually go and place our trade
so up here we're going to get our cash
just copy
this we're going to get it back by
running self do position
sizing beautiful and then we are going
to specify that we want to buy that many
down there now I like having another
check so I like going and saying if the
cash balance is greater than the last
price so that effectively means that
we're not just buying when we don't have
cash then we are going to then execute
the trade okay so that's looking much
better now so we've now gone and sort of
started our position sizing the the
other thing that I sort of wanted to do
is set limits so I always like to set a
take profit and a stop loss so a take
profit effectively starts a trade which
basically sort of sets a bound so if
we're going to hit that point then we're
automatically going to sell and we're
going to take some of our profit back
rather than just leaving it out there in
the market or willy-nilly so let's go
and set this so we can change our type
of order to a bracket and then we can
set a take profit by setting take profit
price and that is going to be equal to
our last price and for now we're going
to set it to 20% so we ideally wanted to
go up by 20% to have a sufficient
takeprofit and we're also going to set a
stoploss price and we're going to set
that to our last price so this is going
to need to change depending on whether
or not we're placing a short order or
long order and we'll we'll update that
in a sec as well once we sort of get
into the nitty-gritty of the algorithm
our stop loss price is going to be 5%
right so that sort of gives us a bit of
a barrier ideally we're hopefully making
more money than we're losing okay so
that is our position sizing so if we go
and run this now we should effectively
get dynamically sized um trades but
we're still really going to be executing
one trades please not SSL
Error find if I don't have a time limit
I just sort of drag on all right cool
that that happened so it looked like we
place the trade that's our next trade
let's take a look so take a look so this
time we went and spent 50% of our cash
at risk so we actually spent oh we
actually bought 106 spy contracts we at
a price of
the total value is
49765 TE sheet there as well okay so now
we've got that's our timer gone now
we're going to keep cracking on cuz we
want to build this bot all right
Baseline bot's done and we've set some
appropriate cash management strategies
in place but so far we haven't actually
done any machine learning or used any AI
this brings us to part three getting
some news the next thing that we want to
go on ahead and do is finally get some
news so this is where we begin to get to
our ml component so the way that we're
actually going to build this up is we're
going to dynamically find out what news
is happening over the last 3 days and
then we're going to take that news and
we're going to pass it through to a
machine learning model to calculate the
sentiment based on that and so ideally
we should get a good feel for where the
market is going based on that news so
that means that we need two key parts we
need the news and we're also going to
need a machine learning model let's
start taking a look at our news first so
first things first let's Zoom back in so
we are going to create a new method and
we're going to say def get the news and
we are going to set that to self over
here so we need a bunch of stuff so we
actually need an API to be able to get
the news back and that is why I created
these values over here because we're
actually going to get the news back
directly from alpaca so we can go from
alpaca trade API and you can just do a
pip install I'll include a complete set
of requirements in the GitHub so from
alpaca trade API we are going to import
rest and this means that we can go and
dynamically get a bunch of stuff from
the trade alpaca trade API we also need
one additional thing here so we're going
to say from time Delta we're going to
import time Delta so this just makes it
a way easier to actually calculate the
difference between days and weeks and
time so on we're going to just use that
now all right so let's go and create an
instance of our API so we're going to
say self. API is equal to
rest and then we want to go and set a
bunch of stuff so we need to set our
base URL which is equal to this over
here we need to set now uh what else do
we need we need our API
key AP what's it called called key ID
and we also need my head's not blocking
that uh we need secret secret key which
is this secret key perfect let's paste
that in all right cool so now we can go
and call this API to get our new so we
can effectively go self.
api. get some news uh where is it get
get news okay yep cool all right cool
cool so we got news so when we call this
we should get our news back now to this
we need to pass through our symbol so
we're going to say self these keyword
arguments yeah self. symbol is equal to
self.
symbol we also need a start date
so start date is going to be equal
actually no it's just start and we need
an end date over here so how do we go
and calculate these well because we're
going to be back testing we want to get
the dynamic date based on our back test
so we can't just go and get today's date
we actually need to get the date based
on when we're trading so we can actually
go and do this and rather than just
doing it right here let's actually just
go and create another method to do this
so we're going to say self. getet
dates and let's do a little magic here
so we're going to grab uh today's date
which is today with respect to the back
test or to the live training so self
do get what is it get dat time
right and so this is going to return the
current date based on the back test or
wherever we're actually going and
deploying this then we need to get the
we're going to get grab three days worth
of news right so we need 3 days prior so
3 days
prior it's a terrible variable name
let's say three days prior okay that's
still not great whatever all right so
we're going to say today is equal to uh
no so 3 days PRI is equal to today minus
3 days so we can use our time Delta
method that we just imported in over
here and what we're going to do is we're
going to set that equal to days equal 3
so that will effectively give us 3 days
prior cuz we're taking today we're
minusing 3 math then what we want to do
is we want to return today and we want
to format it as a string CU that's the
way that the API expects it so we're
going to say today is equal to
St R format time it's like this weird
set of
St today
dot here somewhere
data get weird okay it's
stft time get the feeling that there's
an error there but we will soon find out
um okay so that's our daytime and then
we want to set that equal to percentage
year- percentage month- percentage day
cool so that should return year month
day as a string and then we also want to
do the same thing for 3 days prior so
we're just going to copy that and we are
going to copy this weird method which
doesn't seem to be doing syntax
highlighting so that is effectively what
we've got now so we're going to be
getting today's date we're going to get
3 days prior and then we're going to
return those two values then inside of
our get news function we're going to
call that so we're going to say self.
get
dates and we will uh unpack that so
we're going to say today and then 3 days
let's just copy this let's not write
that beautiful okay so today is going to
be equal to the n end because that's the
end and then the start is going to be 3
days prior perfect right so that is
effectively what that looks like now so
we've now got our symbol we've got our
start and we've got our end cool so
we've now got the news now we I want to
do a little bit of processing on this
rather than just returning it because it
returns it like a jumble and I'm just
going to show you how to do this so news
is actually going to be equal to we're
going to Loop through each value so for
event in news I'm going to say EV not
elect vehicle EV doore dict Took a ton
of time to work out how to do thisor raw
and then this should be a
string and then I love it when it's a
nightmare then a headline okay cool all
right that should give us our news right
so now we're going to get our dates get
our news and then format our news so now
if we return our news return news let's
actually go and call it inside of our
trading Loop so our trading Loop is over
here on trading iteration
so let's say that
we are going to go and initialize
another trade let's actually just cheuck
it here so we can say news equals uh
self. getet
news getor news it is self dog news yeah
Okay cool so and then let's print out
our
news Perfect all right so now
effectively when we go and run our
trading Loop let's go and run it we can
run it by running that's weird can run
it by running python Trading
BP but you knew that all right
cool again SEL error we shall see SSL
beautiful
great let's try again this looks
promising all right cool this is
starting and what happened all right so
we had an error there uh self knew that
we had an error I could tell let's zoom
out it should not be double T's there
you go all right let's try that string
format time all right let's try
again probably a
seller I might not have saved that let's
do that
again okay take a look all right so that
is our news so let's just wait for this
pop up to
go so you can see that we've now printed
out our news over here well we popping
out with a whole bunch of stuff okay um
so we've got all of these different
headlines so if you invested a th000 in
X on mobile when Joe Biden was elected
president here's how much you have today
US senators blah blah blah whole bunch
of news so we've got our news but that
by itself doesn't really help us make
trades this brings us to part four bring
in the machine learning model aka the AI
we actually want to bring some ml into
the mix so this is where we bring in our
ml powered sentiment model so what we
want to do is rather than go and write a
new function I'm actually just going to
sub this out so I'm going to change a
get news function and I'm going to get
news method and I'm going to convert
that to get sentiment and all we really
need to do is import this from my
finbert utils utility so if you've seen
a couple of my sentiment based videos on
the channel before really really similar
basically we're using Transformers and
pytorch we're setting whether or not
we're going to use Cuda or our CPU we're
then taking the process AI finbert model
which is an amazing model when it comes
to finding
and if you actually go to the hugging
face model repository you can read all
about it it's an brilliant model which
has been fine-tuned on finance sentiment
so we're going to be using that so we're
going to be passing our news to the
tokenizer to the model and then what
we're going to do is we're going to
effectively sum it all up and then take
the highest result so to that we're
going to get back our probability and we
are also going to get back our sentiment
um so let's go on ahead and do
that this is all extra stuff oh uh we
don't need this I think I added that my
bad um also in here I included a little
example so if you actually wanted to go
and test this out let's actually go and
test it out so over here I've got two
examples so markets responded positively
to the news trasers were pleasantly
surprised so if we actually go and run
that function so we're going to run
Python finbert utils and this will all
be available inside of the GitHub
repository you can test it out as well
ideally what we're going to get back is
we're going to get back a sentiment so
positive or negative and then the
position or the the actual probability
so over here my head's probably blocking
that let me bring it over and bring it
up let me just bring it up so you can
see that all right you can see that so
you can see that we're getting back
positive sentiment and we've also got
our probability of 89.7 n% so this tells
us that we have positive sentiment if we
change it negatively so Traders
responded negatively to the news and
were pleasantly uh were unha uh dis
please let's set a 10minute timer let's
let's try to crack on 10 minutes left
all right let's go let's go um so let's
not screw around all right let's try
this
again so now we should get Negative
sentiment back as well as the
probability
hopefully and take a look we got
negative and we've got our probability
all right cool so we're effectively
going to be passing our news in the same
way but we're going to be integrating
that signal into our trading algorithm
so let's actually go and bring that in
first so rather than leaving it as get
sentiment we are going to bring back our
function so we actually need to import
it up here so from bin BT
utils we are going to import get
sentiment actually it's estimate
sentiment beautiful okay so this method
or this function over here is going to
be used down here and we are going to
get back what's the order again we're
going to get back probability and
sentiment so
jump back into our trading bot so over
here we're going to say we're going to
get back probability and
sentiment and we're going to set that
equal to estimate sentiment and then
we're going to pass through our news
into it beautiful and then we're going
to take this probability and sentiment
and that's what we're going to return
back so now rather than running get news
we're going to run get
sentiment beautiful and let's print out
uh so we're going to our
probability and we are also going to be
getting back our sentiment over
here so let's print out these and see
how we
go we're going to run our trading bot
again so this time we should execute the
back back test and then we should also
get our sentiment
back and take a look we've effectively
got it so over here wait for this second
one to pop
up so over here you can see that we've
got our sentiment so in this case it is
100% and we also have the fact that we
have neutral sentiment so we're not
going to actually trade on a neutral
sentiment we actually only want to trade
on strong positive or strong negative
sentiment which is why we need that
probability home stretch now we've got
our machine learning model up and
running and generating sentiment but how
do we bring this into our trading
strategy this brings up to part five
integrating our trading signal but this
brings us to our final segment we're
actually going to bring it all together
and integrate our sentiment signal into
our trading algorithm because we've
brought it in we haven't actually made
any decisions based on it as of yet so
let's go on ahead and do that so what
we're going to do is we're going to grab
this line over here and we're actually
going to bring it out so we don't really
care whether or not our last trade was
none so we're going to get rid of that
as well we're going to get rid of these
two lines so really we want to be left
with our order and we're going to create
two different types of orders we're
going to create a sell order and we're
going or a buy order and a sell order so
we're effectively going to short or go
long so this is really good the only
thing is we just need a bit of decision
logic so we're going to say if the
probability actually let's start with
sentiment if
sentiment is
positive and the
probability is greater than
.999 then we're going to issue a buy
order which is looking good we need to
tab this in the only thing is if we've
got an existing sell order and we the
market goes positive we sort of want to
cut that other one out right so we can
do that pretty easily so we can say if
this is why we implemented last order
right if self. last
order equals equals cell then we're
going to sell all of those existing
positions so self do sell
all
beautiful and then we're going to issue
our buy order and then we're effectively
just going to replicate this so we can
copy that
over and bring this
out perfect we're going to say l if the
sentiment is
negative and the probability is at 99999
and if the last order was buy then we're
going to sell all and then down here
we're going to change this to a sell and
we're going to change a couple of things
in this order so rather than doing a buy
here we're going to convert this to a
sell and we need to change our take
profit and our stop- loss price so our
take profit this time we're going to go
the other way so effectively we're
subtracting a profit margin from a value
of one so it's going to be8 here and our
take profit is going to be adding 05 or
5% so we're going to say this is
1.05 and I think that is it so we should
effectively have our trading algorithm
now up and running okay so that's
looking pretty good let's actually go
and test this out so let's test it out
on half month and then we're going to
unleash it on the full back test period
so let's clear this and we're going to
run it
again okay so it looks like we're making
some
trades looks like we made one trade
what's happened there all right let's
extend this out and unleash it so it
looks like we did definitely made one
trade at least we made a Buy trade over
here so we bought uh $49,000 worth of
spy over here looks like the market went
up which is probably a good thing how
much do we make off that first one so we
made 5.2% not too bad considering that
just sort of mimic the market but now we
want to unleash this for all time so
rather than testing from 2023 we're
going to set it to 2020 we're going to
set the first date we're going to
convert it to January the 1st 2020 and
unleash this I'm going to save it and
let's let it run okay and we've got a
bunch of Errors so let's go and debug
this
uh what's happened ml Trader has no
object last
order did we use last
order
oh this should be the last trade
right my
bad
all righty guys and there we have our
results so this is the tear sheet and
this is a breakdown of all the trades
that were placed over a 4year back
testing period so if we quickly take a
look at the tear sheet we can see the
yellow line is the SP index so
effectively what you would have invested
or what you would have returned if you
invested in The Benchmark our blue line
is how our ml strategy performed so
right at the start looks like we sort of
went into the negative and then we sort
of came back and eventually produced
some pretty significant returns but then
sort of Crash back down which I still
think is not too bad cuz when you take a
look at these performance metrics over
here our cumulative return was
234.40 over 4 years with a compound
annual growth rate of
23.8% personally I think that's actually
really really good but what I'm actually
going to do is I'm going to include all
of the trade logs including what you're
seeing on the screen right now inside of
the GitHub repo so you can go and
investigate this yourself plus keep in
mind the only thing that I do want to
call out is this is obviously paper
trading so it doesn't include
commissions doesn't include fees so when
you go and trade be careful out there
guys I'm showing you how to build this
but when you go and deploy it it's
really important to to remember that lot
trading live is so different to to paper
trading but it does give a glimpse as to
what is possible with AI powered or
machine learning powered trading because
over here if you actually go and scroll
on over we actually short sold
appropriately when the market went down
short sold short sold and the market was
trending down so we did get a cash bump
there over here we managed to get some
massive performance gain so we got some
short cells and we managed to to bump
out our our profit or our portfolio
value so if you actually take a look at
one point our portfolio value is
537,500 our Trader and then effectively
run all and that will run our strategy
inside of our broker and all we need to
do is sort of comment out our back test
cuz we don't need that anymore catch you
later
```

---

## Transcript: Building your own cryptocurrency trading bot using Python and the Poloniex API
**Video ID:** fpqzXgZjSqM | **Words:** 2595
**URL:** https://www.youtube.com/watch?v=fpqzXgZjSqM

```
welcome back in this video I'm going to
show you how to program your own basic
trading bot U knowing how to code will
surely help you get through this video
but even even if you don't know how to
code you should be able to follow along
well enough and graph the basic concepts
uh I'm going to go over how to use Bots
as well so non-coders will still find
valuable information in this video um
first of all a little
disclaimer the bot we're building is for
demonstration purposes only please don't
try using this bot for real it's very
simple and uh will not make you any
significant money uh let this be bot be
a starting point for learning how to
build Bots that will make you a lot more
money and when those Bots are raking in
the profits go ahead and send them to me
as a thank you um I'm going to use
Python for this simple bot and it's
going to connect to the ponx
cryptocurrency exchange uh ponx has a
great API for this kind of thing and
even a python rapper already written so
we'll be able to get up and running
really quickly uh also I've gone ahead
and put all the code I write in this
video on GitHub so you can just uh watch
the video for now and easily grab all
the code later from the link in the
description of this video uh our simple
bot is going to trade based on a
currency's moving average to wrap your
mind around how moving averages work
check out my video on moving averages
that should pop up on a card here or you
can look in this video's description for
a link to
it so here's the strategy our bot is
going to trade with when the price on
the chart drops below the moving average
we're going to be in a potential buy
position when the price starts
increasing again we're going to enter a
buy position also when the price on the
chart rises above the moving average
we're going to be in a potential sell
position when the price turns around and
starts dropping we're going to enter a
sell position one more time this trading
strategy is absolute garbage and will
not make you money don't trade using it
however it's simple enough that we can
get the code up and running quickly and
then at the end of the video we'll look
at some ways we can easily tweak that
strategy to get it to go from garbage to
pretty
decent um so to get started let's look
at the components of our simple bot
here's the bare minimum of what we need
to do uh we need the bot to operate
based on variables we give it so that uh
we can pass in settings basically to the
script uh we need the bot to give us
some kind of feedback um we're just
going to be turning out to the command
line uh we need the bot to connect to
the ponx exchange and use its API we
need the bot to calculate a moving
average as it goes we need the bot to be
able to back test historical data for
testing purposes then finally we need to
the bot to actually make trades for
us um that's it let's get started first
let's take a look at the Bare Bones
container we're going to be writing this
code within it's just a loop that
repeats every so many seconds um pretty
basic basic stuff now once again this is
a very stripped down very basic example
of a trading bot I am well aware that
there are much better ways to do time
loops and run service like scripts and
python uh but I'll leave uh the
upgrading basic stuff like that as an
exercise to the viewer I'm just trying
to get this up and running quickly and
simply in order to focus on the guts of
the bot so our first requirement is that
we should be able to pass settings into
the
bot this is pretty standard python stuff
I'm not going to go through it in too
much detail uh on line eight we're
defining two flags one is DH and one is-
p h is the standard help feature which
tells you what flags the application
accepts and P is a flag that we're using
to set the period now for back testing
using the ponx API we're going to need
to adhere to their set periods of 300
900,00 7200 14,400
86,400 uh and those are all in seconds
so we'll go ahead and enforce that right
from the start if you pass in one of the
accept acceptable periods the period
gets set to that if not it spits out in
error message and
exits now most of the time software
developers typically design scripts and
applications to fail silently and things
like that error should be logged and the
software should try to figure out what
to do next based on its defaults or
logic or something like that uh I never
do that with Bots there's potentially
real money at stake here if there is
ever even the slightest hint that
something may be wrong or even not as
expected stop everything immediately and
shut the party
down now our second thing we need the
bot to do is give us uh some kind of
feedback uh we've already kind of done
this one along the way but let's start
creating the stub of the kind of output
we're really going to see um we've added
some more real information to the print
statement on line
26 uh simply adding a time stamp and
showing what the period is set to now
here's an example of this code being run
and the kind of feedback we can expect
um just for the purposes of
demonstration I add it at 10-second
period option um so you can see so far
so good working uh pretty much as
expected um our third step is to connect
to the ponix exchange API once again
this one is pretty straightforward as
well uh pon has graciously offered a
python wrapper for access to their API
uh I'll have that in the repo on GitHub
as well so you won't have to go out and
find it yourself but uh if we um all we
have to do is download it and put it in
the same directory as our bot and then
we can simply import it um then we can
connect using our API Keys now you have
to get your API Keys through your panix
account um the keys will go in here I've
obvious obviously obscured mine so you
guys can't steal everything I have on
ponx
um and then finally we will be querying
information from The Exchange here I'm
just getting the latest price of
whatever currency pair we provide on the
command line you can see I've added
another option up here for you to set a
currency pair or it's uh BTC XML by
default so this is then getting the last
price of that from the API call and then
lastly uh printing it out to the command
line for us to
see so go ahead and test what we have so
far looks like it's working pretty much
as expected um once again I uh just
added the
10c um period for demonstration purposes
uh and then I was able to pass in BTC
XMR that's uh Bitcoin versus Monero and
it every 10 seconds it's given us the
price um the last price for that
currency pair everything seems to be
working as as expected let's move on to
the next
all right now we're finally starting to
get to the interesting stuff so every um
so as this bot is going we want to
calculate the moving average um once
again here's a link to my video about
how moving averages work um we're just
doing an arithmetic uh moving average
which takes uh the previous X number of
prices uh calculates the average between
them and then and that's our moving
average so you can see here around line
uh
42 we start that process so basically
we're just dumping all of the last pair
of price the current prices into a
python
list then we are um
calculating
the um the based on the last few you
know truncating that to the relevant um
data and then just simply getting
an average from it and displaying that
on the command line I've also built in a
way to pass it on the command line the
number of data points you want to use in
your moving average uh which is
typically referred to as the period of
the moving average but that's kind of
confusing because we're already using
the term period for the length of time
in our Loop so now once again we're
getting the expected output when we've
run this script um you'll notice we we
did add the um the other setting for the
number of data points we want our uh
moving average consider so as expected
the first uh at the first data point we
should not have a moving average because
there aren't any data points um previous
so there's no way to calculate that then
after that it starts um calculating it
and it looks like it's
working here we're at a point where the
price has finally changed and the moving
average changed accar early so I think
we're good to go here let's move on to
the next
step all right so now it's time we try
this with some real data um we're
actually going to look at historical
data first for back testing uh the pl X
API makes this really really simple we
can get the data with one API um query
and that's the return chart data uh
query here we send it in a currency pair
the start time for the data the end time
for the data and the period now the
start and end time have to in Unix
timestamps um I actually just keep uh
file handy for timestamps so I can copy
paste so what we do is we assume that if
a start time is passed in on the command
line then that means this this run of
the bot we're looking at historical data
um so we'll go ahead and retrieve that
historical data and iterate over that
historical data instead of the real time
data that we normally go
over now uh one thing I've done as well
uh is in here I only do that sleep for
the loop if we're getting real time data
if it's looking at historical data then
let's just process it all as fast as we
can our bot should Pro process all the
historical data uh between April 1st and
April 2nd of this year with a period of
1800 seconds so cross your fingers and
let's try this out boom there it goes I
didn't speed up that video or anything
it was that quick we tested our bot over
a period of 24 hours we got all the
values uh with a period of 1800 and
calculated a moving average for that
period let's look at the numbers the
numbers look pretty good all seems good
to me let's move on to the next
step all right so here we are finally
we're getting to the guts of the bot the
uh logic that actually decides when we
enter a
trade um it's actually pretty
straightforward we went over the trade
the strategy we're using at the
beginning of the video if the uh the
price of whatever C currency we're
looking at uh rises above the moving
average and then decreases in value um
we go ahead and put in a sell order uh
if it goes the other way if it uh goes
below the moving average and then starts
moving back up we put in a buy order and
then in either of those situations once
the price crosses back over the moving
average we exit the trade so I've also
put in some code here to guarantee um
we're only in one trade at a time again
I'm just doing that because this is a
simple example and I don't want the
output to get all messy and future Bots
will'll be able to do multiple trades at
the same time but you'll notice I
actually haven't put in the code for the
buy or sell orders yet let's go ahead
and run a test first where it just
simply PRS prints out this is where a
sell would happen or this is where a buy
would happen look at the numbers and see
if the bot is behaving how we want to
before we actually try to make some
trades all right let's go ahead and test
this on some historical data first see
what kind of output we
get all right so you can see right away
it would have placed a sell order here
uh it would have exited that trade here
and another sell order another exit sell
order exit sell order and then our um
our time frame
ended so you can see the Bots working as
expected if you look at the numbers but
it doesn't look like we would have
actually made any money with this spot
because um these are kind of garbage
positions and then finally the moment
you've all been waiting for how we
actually place the trades on the ponx
cryptocurrency exchange using their
python wrapper it's really simple the
names of the functions are sell that's
not closing the position that's creating
a sell order or a buy order um you got
to track the order number that's the
return item from these calls and then
when the bot deems it's time time to
exit the trade you just call the cancel
function it's really that simple I'm not
actually going to uh show you an example
of this running because like I said this
isn't a very good bot and I don't want
to lose money for the sake of a YouTube
video so that's it feel free to check
out the GitHub for all this code and use
it as the basis for your own Bots um
after after looking at the back testing
of this strategy it's clear that this
bot is a bit too rigid to actually make
money um it exit positions at the
faintest hint of price changing
directions um which exited some of our
positions much too early um see if you
can devise a system to get around that
defect here's a hint you could probably
try to add a second moving average
that's much shorter than our existing
moving average that could our that could
make our strategy a lot less impulsive
um also It's always important to protect
yourself this bot has nothing even
remotely resembling a stoploss or
anything like that uh the focus of the
next video will be building out that fun
functionality in this spot uh we'll see
you then and let me know in the comments
if you have any questions
```

---

## Transcript: Algorithmic Trading Strategy Using Python
**Video ID:** SEQbb8w7VTw | **Words:** 4228
**URL:** https://www.youtube.com/watch?v=SEQbb8w7VTw

```
Hello everyone and welcome to this video
on the Python programming language. So
in this video I'm going to attempt to
create a strategy called the dualm
moving average crossover that will help
me to determine when to buy and sell
stock. Now currently I am on Google's
website called collab.resarch.google.com
because it makes it really easy to start
programming in Python, which is what I'm
going to be using in order to use this
strategy. And all you have to do is go
to this website and then log in using
your Google account and get started
writing your Python code. So let's go
ahead and get started writing this code.
The first thing that you're going to
want to do is click on file and then new
notebook where a new tab will open up
for you. Now before I write any logic
for the program, I want to talk a little
bit more about the strategy. So again,
the strategy is called the dual moving
average crossover and it occurs when a
short-term average crosses a long-term
average. This signal is used to identify
that momentum is shifting in the
direction of the short-term average. And
a buy signal is generated when the
short-term average crosses the long-term
average and rises above it. While a sell
signal is triggered by a short-term
average crossing a long-term average and
falling below it. Okay, so with all that
being said, that definition is actually
straight from data camp. So let's go
ahead and get started with this program.
So the first thing I'm going to do is
write a comment in this cell and I'm
going to put in a description about the
program. So this program uses the dual
moving
average crossover
to determine when to buy and sell stock.
Okay. So now I'm going to go ahead and
create a new cell by clicking this code
button in the top left. And I'm going to
import the libraries that I'm going to
be using throughout the program.
All right. So I want to import pandas as
pd. And I'm going to import numpy as np.
Then from datetime, I'm going to import
datetime
and make sure I spell that correctly.
Okay. Next, I'm going to import mattplot
live.pipot.
And actually, I might not use that
datetime, but I'll leave it there for
now. Anyways, this will be
mattplotlife.pipplot
as plt. And then I'm going to give the
plot a style. So type plt dostyle
do use.
And I'm going to use the 538 style.
All right. Now I'm going to run this
cell by clicking this button here to the
left. And hopefully there are no errors.
So let's see. Give it some time. And
looks good. All right. So I'm going to
go ahead and create a new cell. And I'm
going to bring that up a little bit. And
now I'm going to load the data. So, I'm
using Google's website. So, I need to
use Google's library to load this data
onto the site. So, from Google.colab,
I'm going to import files and I'm going
to create a variable called uploaded and
set it equal to files.upload.
All right. And I'm going to run this
cell and click on choose files. And
perfect. So, now I'm going to upload
that apple.csv file which contains the
stock price for Apple. Okay. Okay, it
looks like it's uploaded. So, let's go
ahead and create a new cell. And in this
cell, I want to store the data. So, I'm
going to create a variable called AAPL.
I'm going to set it equal to PD do read
CSV. And I need to tell it what file I
want it to read. And that's aapl.csv.
And then I want to show the data. So,
I'm going to type a. And then let's run
this cell. All right. And now I can see
the data for this file. All right. And I
can see that this has Apple's stock
price from October 2nd, 2006 to December
30th, 2011.
Okay. And then we can see that it has
the open price, it has the high price,
the low price, the close price, the
adjusted close price, and the volume.
Okay. And of course, the date. All
right. So, let's go ahead and create a
new cell. And I kind of want to
visualize this data. see how it looks
over this time span. So here I'm going
to visualize the data. So just type
plt.figure and then I'm going to give
the figure a figure size. I'm going to
set it equal to 12.5 in by 4.5 in. And
then I'm going to plot the data. So I'm
going type plt.plot.
And I want to plot only the adjusted
close price. So, I'm going to type APL
and then my brackets and then adjusted
close price. Okay, so that looks good.
And I go ahead and give it a label as
well. So, I'm just going to call it APL
for now. And then I'm going to give the
plot a title. So, I'm going type
plt.title. And the title will be Apple
adjusted close price
history. Something like that.
And then I want to give the X-axis a
label. So I'll type plt.x label.
And the label will be October
2nd, 2006
to December
30th,
2011.
All right. And I want to give the y-axis
a label. So I type plt.y label. and it
will be the adjusted
close price in
US dollars.
So I put USD here.
Okay, so that looks good. And then let's
give this plot a legend.
So type PLT.leend and put it in the
upper left location
and then show the plot. So type
PLT.show. Let's run this.
All right. So now we get a visualization
of this data from October 2nd, 2006 to
December 30th, 2011. And what's
interesting is that you can see if you
had bought basically one one share of
the company here
for it looks like a little under $10,
then in December
30th, 2011, you would have had over $50
from that one share. So you would have
profited what of extra $40, right? So
your money would have increased five
times. So it probably would have been a
good investment in 2006 to invest into
Apple and uh to sell over here. But you
know the price definitely I believe has
went up from that uh now as of today.
All right. So let's go ahead and create
a new cell.
And now I want to create that shortterm
average. All right. So I'm going to
create a
a simple moving average and it's going
to have a window of 30 days. So here I'm
going to create the simple moving
average
with a 30-day window. All right. So I'm
going create a variable called SM A 3030
and I'm going to set this equal to PD do
data frame. All right.
And that needs to be a capital there.
All right. So
now I'm going to
uh set a column equal to whatever data
that I want, which in this case I want
the adjusted close price of the stock.
So I'm going to type SMA 30 and then I
will call this adjusted close price. I'm
going to set it equal to APL
adjusted
close price.
Okay. But then I want to roll it up. So
rolling
with a window
equal to 30. And I want the mean.
All right, then let's take a look at the
data now that I'm done with that. So,
just type SMA30 and let's run this.
Okay, and now I have this nice simple
moving average for the 30-day window.
All right, let's go ahead and create a
new cell. And I want to create the
long-term average. So, I'm going to
basically create
another simple moving average, but this
time it's going to be for 100 days. So,
I'm going to create a
simple moving 100day
average. All right. So, let's create a
variable called SMA 100. And I'm going
to set it equal to PD dataf frame.
All right. And then I'm going to give it
a column. So, SMA 100 column will be
called adjusted close price. And I'm
going to set it equal to actually let me
go back up here really quick. Make sure
I didn't put a space. All right, good.
Okay, so it's going to be equal to
APL
adjusted close price.
And I want to roll it up as well. So
just type dot rolling
and the window will be 100. All right.
And of course I want the average so
mean. And then let's take a look at this
data set. So type SMA 100. And then
let's run this cell.
Okay. So everything looks good there as
well. All right. Now that we have these
data sets, let's look at a visual of
this data. All right. So, I'm going to
visualize the data again.
All right. So, what I can do, I'm
basically going to be creating almost
the same plot from before. So, I'm just
going to go back up here where I created
this chart. I'm going to highlight it
and copy it using C and then come back
down here and paste it using Ctrl +V.
All right. And this time I'm going to
add two more uh two more plots. So I'm
going type PLT.plot
and I'm going to plot SMA 30.
All right. I'm going to plot the
adjusted close price.
Adjusted
close price. Okay. And I'm going to give
it a label.
So, I'm going to set the label equal to
SMA30.
And then I'm going to basically copy
this using Ctrl + C and then paste it
using Ctrl +V and just change SMA30 to
SMA 100
and the label as well. All right. And
now, let me go ahead and run this.
And now we can see the data. We can see
the simple moving 30-day average in the
uh orang-ish color and then the
the simple moving 100 day average in the
yellowish color. All right.
Okay. And so now what the strategy is
telling us is that we should we should
basically be
be buying anytime the short-term average
crosses the long-term average. So
anytime the the SMA 30 crosses the SMA
100
that would be basically a signal to buy.
All right. So basically if I look here
um let's actually start I'm going to
start it here. Why not? So the orange
again is the shortterm average. And so
it looks like it's crossing that
long-term average here. So right here
will be a point where it's telling us to
buy. Okay.
And then you can see
uh right around here
it crosses and it it goes lower than the
than the long-term average.
And so at this point we should
definitely sell because it's telling us
that the momentum is
going to drop or go down. It's telling
us the direction of the momentum. All
right. So, I kind of want to plot this
and see this for myself using Python.
So, let's see how well this strategy
actually works. So, I'm going to create
a new cell.
And
in this cell, I'm basically going to
create a new data frame that I'll use to
plot all the data. So, here I'm going to
create a new data frame to store all the
data.
And I'll go ahead and bring this up a
little bit.
Okay. So, I'm going to call this data.
Set it equal to PD. Dataf frame.
And
the data column called AAPL will contain
AAPL
adjusted. Uhoh. Aapl adjusted
close price.
And then the column
SMA 30 will contain SMA 30
adjusted close price and then the column
SMA
100
will contain SMA 100
adjusted close price. All right, so that
looks good. Let's go ahead and take a
look at the data just by typing data and
run this cell. Okay. And now I have this
nice data set. So let's go ahead and
create a new cell. And now I want to
I want to basically create a function
that will return the the buy and sell
price. Okay. And this will we'll use
that price to then plot it on the chart.
Okay. So we'll use it to plot when to
buy and when to sell onto that graph or
chart. So here I'm going to create a
function to signal
when to buy and sell the asset or the
stock.
All right. So let's call this
buyers_ell.
And it's going to take in some data.
And then the
the signal price to buy
will be an empty list. And the signal
price to sell will be an empty list.
Okay. Then I'm going to create a flag
that's basically going to tell me when I
or or it's basically going to tell me
when the two moving averages crossed
past each other. So I'm basically going
to use the flag for that. Okay. So now I
want to create a loop. So let me bring
this up a little bit.
All right. So for I in range
data uh actually for I in range lin
data.
All right. If data
SMA
SMA
30
at position I.
If that is greater than data
SMA 100 at position I
then
I need to check the flag. So if the flag
does not equal one then we have not been
here right and right now the flag does
not equal when the flag equals negative
1. So in that case I want to get that
price. So sig price
buy. So the signal price to buyappend
and then I'm going to append the price
which is data at AAPL
position I.
All right. And then the price to sell
the the signal price to sell
will be uh not available, right? It'll
be nothing. So it's just type np.n.
So I'm going to input a uh nan value.
All right. And then I'm going to set my
flag equal to one signaling that I just
went here. Okay. Else
the signal price to buy
dot append
will be a nan value. I will append a nan
value.
All right. And I need to do the same
thing for the price to sell
np.net.
All right. And I'm probably making this
more complicated than what it has to be,
but oh well. Let's keep going. So, else
if
else if data
uh at position
or at column SMA 30, position I if that
is
less than data
at position SMA 100
column I.
Then I'm going to check to see if we've
been here already. So I'm going to see
if the flag is equal to or if the flag
does not equal I need some other number.
So I'm going to say zero.
Then
the
the signal price to buy
the signal yeah signal price to
buy.append
will be nothing. We won't append any
values. This right here is telling us,
this is signaling us to sell. Right now,
the the the
simple moving 30-day average is less
than the uh simple moving 100 day
average. All right. So, it's telling us
to sell. So, I need to do sig price
sale.append
and I need to append that price
which is at column APL. and position I.
Okay.
Else
I will just append nothing. So sigp
price
append np.net.
All right. And sigp price cell.append
npn
net.
All right. So now if neither of those
two are the case, so else
then
I'm just going to append sick price to
buy.append
np.net.
So I'm just not going to append
anything.
So I'm only appending when
when the uh simple moving 30-day average
is less than the simple moving 100 day
average. and when it's greater than the
simple moving 100 day average.
All right, so that's the only time I'm
adding values. So here I'm gonna put
mp.net.
Okay, so I'm very sure I made that more
complicated than what it needed to be,
but anyways, I'm going to go ahead and
return
sick price buy
and sick price sell. All right, let's go
ahead and run this. I hope that I'm not
making any mistakes here. So, let's run
this. Ah, looks like there are some
mistakes. So, where did I mess up at?
Okay, so s priceappend
here. All right, right there. Get rid of
that. And then let's run this again.
Okay, now it looks good. So, I'm going
to create a new cell.
And now I'm going to store the buy and
sell data into a variable.
So, more specifically into that data
variable that was created earlier. So,
first I'm going to create a by cell
variable and I'm going to set that equal
to by cell and it's going to input data.
And then the data column that I'm going
to create is going to be called by buy
signal
price. And I'm going to set this equal
to by cell at position zero. And I'm
going to create another column for this
data set called sale signal
price. And actually I want all these to
be capitals.
All right. And I'm going to set this
equal to buy
sell at position one.
And that looks good. So, let's run this.
And it looks like I messed up somewhere.
So, let's see what's going on here. Uh,
sig price sale is
sick price sale. Did I not create sick
price sale? It is. All right. So, let's
look at this error here. So it says name
sick price cell is not defined
and
here sig
ah right here. All right so it needs to
be a capital there and I think that'll
do it. So I'm going to rerun this
function here and let's run this now.
Okay. So we got some more errors.
So, where is sick price
sale? Let's keep looking here.
All right. And where else? I think that
is it. I'm going to run it one more
time. Hopefully, this the last time. Run
this again. And now everything works.
All right. So, I'm go ahead and create a
new cell. And in this cell, I want to
show the data. So, just type data. And
then let's run this.
Okay, now I get back all of these
columns and it looks good. So this is my
data set with all this data. All this
data manipulation that I've done is all
here in this one nice data set. So I'm
go ahead and create a new cell here. And
now it's time to visualize the data and
the strategy
strategy to buy and sell. Okay. To buy
and sell the stock. Okay. So, just type
plt dot
figure. And then again, I'm going to
give it a figure size. Going to set the
figure size equal to this time I will do
h, you know, I keep it the same. 12
point we do 12.6. Why not? 12.6 in by
4.6 in.
And then type plt.plot.
And I'm going to plot uh data at
position APL.
And then I'm going to give it a label.
So I'm going to set the label equal to
APL. So that's the adjusted close price.
And
that looks good for now. So next I want
to plot the simple moving average. The
simple moving 30-day average to be more
specific. And let's give it a label. It
will be SMA 30.
All right. Then I want to plot the
the
simple moving average for the 100 day
window. So SMA 100
and the label will be SMA 100.
Okay. Then I want to type PLT.catter
and put in data.index.
And then I want this data to be the by
by
signal
price.
Okay. And the label
the label will be by
and I'm actually going to give this a
marker. So the marker will be
this nice little
up signal there or up character.
Okay. And I'm going to give it a color
as well. So the color will be green,
right? Because it's going up.
And then type
scatter
data.index.
And I'm going to plot data at
position or data column
cell
signal
price.
And I'm going to give it a label as
well. And I will just put sell here. And
it will have a marker as well.
And the marker will be showing it go
down. And then the color will be red
because that kind of indicates that you
know the momentum shift is going down.
All right. Then I'm going to give the
plot a title. So type plt.title
and this will be Apple
adjusted close price history
buy and
sell signal
or signals. Okay. And then I will give
the Xaxs a label. So PLT dox
label. And it's basically going to be
the same thing I did before. It's
October 2nd,
2006
to December 30th,
2011.
All right. And then same thing for the
Y-axis. PLT.Y by label.
It's going to be the adjusted close
price USD
dollar sign.
And then I want to give it a legend. So
PLT.leend
and the location will be on the upper
left.
And then plt.show.
All right.
Okay. So now if I didn't make any
mistakes, this hopefully will run. Let's
go ahead and run this. And it looks like
I did make some mistakes. So, let's go
back up and let's see where the mistakes
are. Right here. This needs to be data.
So, let's run this again. And now it
looks good. Okay.
So, when I say it looks good, it
actually doesn't look that good. It
looks um well, one thing, I can't see
the indicators, and it looks like
there's more indicators than what there
should be. So, what that means is one, I
need to change this graph. So, let's
change the alpha here. I'm going to put
0.35.
And I'm just going to copy this using
control C and paste it here and here.
And let's run this again. Okay. So, now
I can see the arrows. But this is
definitely wrong. So, that means I did
something wrong with my function here.
And I can check out the strategy. So, I
know exactly what it is. Um, here I did
not set the flags equal to zero, right?
to indicate that I actually went there.
So the flag needs to be set equal to
zero here. I did set the flag equal to
one here and everything else looks good.
So let's run this again and then let's
run this again and let's run this again
and then run this. Okay. And now the
chart looks a lot better. So now I can
see the the signals when to buy and when
to sell the Apple stock based off of the
adjusted close price and this strategy
here. All right. So it looks like uh for
the strategy I should buy here and then
sell it here. Okay. And then the
strategy is telling me to buy here and
sell here. Now, this is a problem
because I would lose money if I did that
uh by buying here and selling here. So,
that's not good. But then it tells us to
the strategy tells us to buy down here
and then sell up here, which is good
because again, I still would have made
some profit. It wouldn't have been the
optimal place to sell, right? The
optimal place to sell probably would
have been like right here somewhere or
here. I don't know which one's higher. I
think this this right here may be a
little bit higher but
uh yeah it's we still would have made
profit after buying here and selling
here and then the the strategy tells us
to buy here
right and then sell it here. So again
not the optimal place to sell but still
profitable. And then the strategy tells
us to buy here and sell here. And I
really can't tell
um just by looking at this chart here if
if we would have lost money or gained
money. It really seems about the same.
So I'm really not sure. Uh maybe maybe
we would have gained just a little bit.
Look like it it might be a little bit
higher.
I you know I really can't tell. So
anyways, I know for sure that we would
have lost money here. Okay, so this
strategy seems okay. It seems decent,
right? But I only looked at this small
data set and I only looked at this one
asset to test this strategy. So a lot
more testing will be needed with the
strategy in order to see how well it
performs. So anyways, I hope you all
enjoyed this video. Please leave any
questions you have in the comment
section. Don't forget to hit that like
button and that subscribe button. And I
have many other videos on stocks and on
Python and on uh chat bots and
programming in general and machine
learning. So be sure to check all those
out if you're interested. And as always,
thank you for watching and I'll see you
all in the next video.
```

---

## Transcript: Intro and Getting Stock Price Data - Python Programming for Finance p.1
**Video ID:** 2BrpKpWwT2A | **Words:** 1911
**URL:** https://www.youtube.com/watch?v=2BrpKpWwT2A

```
what's going on everybody Welcome to a
new python for finance tutorial Series
in this series we're going to hopefully
go over pretty much everything you could
want to know for using python with
Finance to start we're going to do
pretty basic things like how to actually
get data how to work with that data uh
for so to begin we'll just use one
company and then we're going to get into
okay now let's work with data from the
entire S&P 500 companies how do we kind
of combine all that data together how do
we visualize that kind of information
how do we start to analyze relationships
between companies how might we formulate
trading strategies and also how might we
analyze such trading strategies so we
have a lot of work ahead of us so let's
go ahead and get started uh before we
get to far uh here are some of the
packages that you're going to for sure
need for this series also uh I'm
assuming that you at least know the
basics of python uh before you start
this series if you don't that's okay
I've got tutorials for that you just
probably need to follow them so you can
go to python program.net come down to
python fundamentals go to the basics you
don't need to do the entire course if
you look at the sidebar that's the
entire course all the way down there you
don't have to go all the way down there
but you definitely want to get to um how
to install python packages and modules
with Pip so I think it's like 11 or 12
or something tutorials it's really not
too much but that'll give you enough of
the basics so you can pretty much follow
any tutorial not just this one also of
course you'll need python we'll be using
actually I'm on python 3.5 I'm not
updating the python 3.6 just yet because
it's not fully supported by all the
libraries I use um but you can feel free
to follow along all the libraries that
we'll use here I believe will support
36 so uh follow along with whatever the
latest version is hopefully just make
sure you're using three or greater uh
python 2 you can try following along but
there's going to be a lot of little
things that you're going to have to
change once you have those things you're
going to need uh the following third
party module or third party libraries
really um you're going to need pandas
pandas Das data reader so when you go to
use pip to install these you'll do Pan's
Dash data reader when you import this
it's underscore because you can't have a
dash um you need that you're going to
need matap plot lib you're going to need
beautiful soup for and I am sure there's
something else I'm forgetting um but
that should that should get us oh the
other thing will be S kit learn so kit
learn and then when you go to actually
import it it's sklearn so if you don't
have all those packages go ahead and
pause this get them um a dependency of
pandas is just numpy I'm pretty sure if
you use pip install you will just Auto
install numpy but you might need to
install numpy first anyway uh do that
pretty sure map plot Li is also a
dependency so you might get away with
just doing the these things but anyway
that's what we're going to be using
without further Ado let's hop into it
let's get some data so the first thing
we're going to do
is um import date time as DTE we're
going to use date time just so we can
set some starting and end dates for the
data that we're going to pull then we're
going to go from matplot lib uh.
pyplot we're going to import map plot
li. pyplot as PLT then from matplot lib
we're going to import style so PLT just
lets us utilize pip plot which lets us
make plots charts graphs these sorts of
things uh we're importing Styles so we
can make our graphs look a little better
pretty much the number one rule in
finance is at least have good-looking
graphs um so if you're losing money as
long as your graphs look good
everything's good now we're going to
import pandas as PD and then we're going
to import
pandascore dat reader. dat reader. dat
as web if you're following along you
already have panas installed chances are
you don't have pandascore dat reader
this
replaces um the pan I forget if it's
panda. i.a or
p.a. but one of those it replaces that
uh so it's it's it's another library
that you'll need regardless cool that's
what how we're going to actually grab
data from um from the yaho finance API
there's a lot of ways you could do it
this just makes it really easy because
it's going to return us a pandas data
frame so what's pandas pandas is a
really really popular data analysis
Library um also just as a quick aside uh
if you want to learn more about any of
the things that I cover here I'm going
to be covering especially like Matt plot
lib and pandas like really fast I'm not
going to be spending a lot of time on
them if you want to learn more about
them though you can just go to python
program.net data analysis and then
there's this data visualization and then
uh data analysis with pandas and there's
entire series on just about everything
you could possibly want to do with both
matplot lib and
pandas so check those out if at any
point I'm doing something or I went too
fast or whatever uh feel free to ask
questions um I'll do my best to help you
out chances are if it's on a tuto or if
it's on like a topic or a module or
something I probably already have it so
like a tutorial on it already that I
could link you to um you can also just
come here and search okay so if if you
want to know how to like map functions
or something like that um for pandas for
example we type Map There's rolling
apply and mapping functions uh for data
analysis with pandas you could find that
out so you can also use the search um
but you can feel free to ask if you're
stuck anyway moving this
along what we're going to do now is
style. use ggplot it's just a one of the
many styles you can use if you can if
you want feel free to look into all the
other styles you could use now we're
going to say start equals um actually
we're going to say dt. dat time and
we're just going to say the start date
is the year 2000 month of January and
the first of that month then I'm going
to just like copy this paste and say end
is now going going to be 20 we'll just
do 2016 12 31 so uh if you live in the
United States this is somewhat foreign
to you this one might not be absolutely
clear but yeah December 31st 2016 we
like to not do things in proper order
now what we're going to say is we can do
DF DF is going to be short for a data
frame and what we're getting a data
frame from don't worry I'll explain the
data frame in a moment is web. dat
reader
so web here which we're getting from
this web. dat reader now we're going to
specify a ticker a ticker is just a
symbol for the company it's usually
somewhere between like three and five
letters um for a stock in this case TSLA
it's Tesla um then we're going to say
where we're going to get it from uh
we're going to get it and just because
someone's going to complain about the
mixture of quotes we're going to get it
from Yahoo and then for what times we're
going to say the start time and the end
time okay so so that gives us a data
frame a data frame you can think of a
data frame like a spreadsheet or
something like that it's going to look
hopefully pretty familiar to you if you
use spreadsheets that is um and what we
can do is we can print DF do head this
is just a really useful kind of
debugging thing that you can do so I'll
press F5 to run
that and no module called Matt plb let's
fix that Matt oh that's not a fix Matt
plot lib let's try again let's try
again beautiful so what we have here is
a nice uh data frame just making sure
you all can see that so uh so what DF do
head does for us is it just prints by
default the first five rows in our data
frame uh you could change it though you
could say I want six so you could pass
Six and you'll get six six rows instead
okay uh that'll give you the first ones
you can also get the last ones with DF
do
tail so as you can see we get data all
the way up to December 30th okay so you
can do that now um what was I going to
point out okay yes so so these are your
columns open high low close volume and
adjusted close uh adjusted close is
adjusted for uh what's called stock
splits this happens when a company
decides their price maybe is too high
per share they say okay every share is
now two shares actually so let's say you
had one share of Apple at $1,000 now
they're going to say okay you actually
have two shares of Apple 00 well
historically to the data like if you
were to go through that historically
suddenly price would go all the way up
to $1,000 and suddenly it crashed to 500
so we have adjusted columns this one
only has close a lot of data will have
adjusted open high low and close uh but
this is adjusted for stock splits over
time Tesla as far as I can think right
now does never never had a stock split
yet uh but like apple has and so on and
companies just do it because most people
can only buy a single share increments
of single shares so $1,000 kind of
precludes a lot of people from being
investors in that
company uh so anyways uh that's just a
really quick introduction to pandas and
bringing in data in the next tutorial
what we're going to talk about is um a
little bit more on input output because
it's not necessarily always going to be
the case that you're going to be using
the Yahoo finance API maybe you've got
your own spreadsheets or whatever and
then also we're going to just show a
really simple way to actually graph this
data um and all that so if you have any
questions comments concerns whatever up
to this point please feel free to leave
them below otherwise I'll see you in the
next
tutorial
```

---

## Transcript: Bollinger Band + RSI Trading Strategy That Actually Works
**Video ID:** pCmJ8wsAS_w | **Words:** 1292
**URL:** https://www.youtube.com/watch?v=pCmJ8wsAS_w

```
Bollinger Bands
In this video, I m going to show you a trading
strategy that works so good I just have to
tell you guys. And it involves using Bollinger
Bands and RS. It s simple, easy to use, and
has a pretty high success rate if you use
it correctly. Well enough talk, let s get
straight to it.
The first step for this strategy is setting
up the Bollinger bands. If you ve never used
Bollinger Bands before, your lucky you clicked
this video, because this indicator is absolutely
incredible and is great way to find support
and resistance levels.
So the first thing we want to do is to go
to trading view or whatever trading chart
platform you use, click the indicators tab,
and type in Bollinger Bands .
Now, the default settings trading view will
give you, are nice, but we want to change
them for this specific strategy.
First thing you want to do is go to the length
section and change this value to 30, make
sure the standard deviation is set to 2.
Then I m also going to remove the background
of the indicator, and change the lines to
red. This is all personal preference, you
can do whatever you prefer here.
So if you didn t know, this is how Bollinger
bands work. The center line, is a moving average.
So whatever amount the length is, this indicator
is going back that many candle sticks and
finding the average between them. So for this
strategy we are going back 30 candlesticks.
The lower and upper lines are standard deviation
lines.
Alright we got the Bollinger Bands setup,
now let s add the RSI.
To do this, just go to your indicators tab,
type in RSI and then click this top one that
says Relative strength index
Now that we have RSI added let s modify the
settings a bit. Make sure that upper band
is set to 70, and your lower band is set to
30.
Then this length is probably going to be set
to 14, make sure to change that to 13 for
this strategy.
If you haven t used RSI before it s a great
indicator to tell you if a certain security
is being overbought or oversold.
If the line is above the 70 value, its being
overbought, if the line is below the 30 value,
its being oversold.
Now it s time to pair these 2 indicators together
and get some profitable trades going.
Make sure you watch the rest this video, because
im going to explain a crucial tip to make
this strategy working just okay, to turn into
a money making machine.
So there are 2 different strategies you can
choose from when using these indicators. Both
of these strategies are built around the concept
called mean reversion.
What s mean reversion you my ask? Well, mean
refers to the word average, and the word reversion
means returning to
So to put it simply, the phrase mean reversion
just means returning to the average. Which
is exactly what we are going to be shooting
for with this strategy.
So as an example, in most cases if there is
extreme price movement in one direction whether
its up or down, we can pretty much predict
after that big price movement, the price will
return back to the average (the moving average
right here).
So if the price goes above the upper Bollinger
band, we want to enter a short trade. If the
prices goes below the bottom Bollinger band,
we want to enter a long trade.
Simple enough right? Well, if you did this
by itself, odds are it wouldn t work to good.
That s why we added the RSI earlier.
So as you can see in this example the price
went above or below the Bollinger bands many
times giving lots of false signals. So if
you went in every time here, you probably
lost money.
To solve this issue we are going to add one
more requirement. You can only go in if the
RSI hits an extreme value.
Meaning, we can only enter a long trade if
the price moves below the lower Bollinger
band and the rsi goes below the value of 25.
For shorting, we will only enter a trade if
the price goes above the upper Bollinger band,
and the rsi goes over the value of 75.
So as an example here we are looing at apple
and the price starts going below the bottom
Bollinger band here, but at this point in
time the RSI is still not where we want it
to be. So we wait it out a bit until the line
goes below 25. which it proceeds to go down
all the way to 20, we enter a long trade at
this point, and the price returns to the average
just like we predicted it would.
Now there is some crucial information you
need to know about this strategy or else it
simply just wont work. This next tip im about
to tell you is the difference between making
this strategy profitable or making a money
loser.
Where this strategy has its faults is when
the market is moving sideways. For almost
all trading strategies when the markets is
moving sideways it s a lot harder to predict.
So as example here, the price goes below the
lower Bollinger band, and the RSI is below
25, but the price just keeps on dropping.
So if you entered here, you would have an
absolute massive loss.
So now im going to show you a nice little
trick, so you know before hand before these
huge price movement even happen.
So in this example notice how skinny the Bollinger
bands are at this point in time, they are
very close together, and there is barely any
price movement, and the market is moving sideways.
You can also see that the RSI is being very
tame and staying in between the purple rectangle
and not going above or below the major points
we made before. This alone, should immediately
raise a huge red flag.
Most times when the market is like this for
a while, when the price does break in a certain
direction, it goes in that direction with
huge amounts of momentum.
So in these types of examples, you never want
to trade trying to catch a falling knife when
the price starts to act like this.
Let me show you another strategy we can use,
to help solve this, that works like absolute
magic.
Here you can see the price starts to drop
to the lower Bollinger band and then reverses.
The next time it comes down, it makes a new
lower lower from the previous low, but something
interesting is happening. The RSI made a higher
lower.
So we have price making a lower low, but the
RSI is making a higher low. This right here,
is called divergence and is an absolute great
sign to enter a trade, especially if rsi was
previously below 25, and the price went below
the lower Bollinger band.
If this happens, there s an extremely great
chance the price will reverse and make a new
higher high.
I just showed you how to pair Bollinger bands
with RSI and make it a profitable trading
strategy. All I ask for in return is if you
can take 2 seconds out of your day and like
this video.
You should also watch my other video where
I go over a MACD strategy which gets an 86%
win rate, which is absolutely insane when
it comes to trading.
So go check that out, and ill see you guys
next time.
```

---

## Transcript: I Tested RSI + Bollinger Bands Strategy in 2025: Crypto, Stocks, Futures, Forex Markets
**Video ID:** j2ESnjhT2no | **Words:** 1244
**URL:** https://www.youtube.com/watch?v=j2ESnjhT2no

```
Hi everyone, it's Trading Midbusters. On
this channel, we test popular trading
strategies and see if they work or if
they don't. In this video, we're
checking this video with 1.5 million
views. The idea here is strategy based
on two indicators, bowling a bands plus
RSI. As you can see here, we got two
indicators. First is bowling bands. They
give us upper bowling a band, middle and
lower, and RSI here below. And the idea
is like this. If our price crosses lower
bowling a band plus we add the condition
that lower RSI crosses our lower
threshold we open a long trade and uh we
exit when it crosses middle band plus
the threshold for RSI and the opposite
for short entries. The main idea here is
mini reversion. What it is? It's uh the
assumption that if uh our price went up
or down under certain conditions, it
will always return to mean. And uh as
you can see here, the middle bowling
band is actually our moving average. And
here is the results table after all the
testing. I will explain to you in
details what it is in the end of the
video. We are going to check the
performance of the strategy using back
testing. And here are a few words about
what it is. So here is an example of
Apple stocks file. We have downloaded
similar files for all most liquid
crypto, futures, stocks and uh forex.
And uh in this file for example we have
prices for each 15 minutes and uh that's
actually enough to simulate our trading
logic using Python language. Let me
quickly show you the code of our back
testing. So firstly config.json it's a
default configuration file for freak
trade package. Next uh in back test.json
we specify some parameters for our
trading strategy like uh maximum amount
of open trades and our initial capital.
Then in crypto.json we specify the
tickers that we are going to trade on
crypto market. Here we have just 100
most liquid crypto tickers. And same for
forex, US futures and US stocks. Now
main script of our trading strategy.
Here we specify our indicators bowling
bands length and std. It's how wide
bowling events are and also RSI
parameters. We use parameters from the
video and uh later in the video I will
optimize them. Stay tuned. Next in this
part we calculate our indicators and in
populate entry trend we check our entry
conditions. As we've discussed our entry
condition for short, for example, is
when we crossed our upper bowlinger band
and RSI's overbought which is defined
here and same for exit trend. So we
cross middle bowling band and RSI is
higher or lower than our predefined
threshold. Now let's see the
visualization of our trading strategy.
It's just one ticker SNX. In the bottom
you see RSI indicator and here you see
lower bowling band middle and upper. So
as we've discussed here we crossed lower
bowling band and RSI was lower than our
threshold. So we entered a long position
and we exited with profit. Then here you
can see that there was some short
entries which we closed prematurely due
to stop-loss and there was another short
entry that we also closed with profit
when it met our condition. And it means
that we crossed our middle bowling band
and the SI was more than our threshold.
Now let's get to the results on crypto
market. First we start with 1 day time
frame and here are best and worst
performing tickers and our sharp ratio
is 12 not actually impressive and we've
made just $70. Next we have 4 hours time
frame once again best and worst
performing tickers and our sharp ratio
is negative. It's - 1.17 and we've lost
$370.
Next 1 hour time frame, our sharp ratio
is - 33. It's very low. And we've also
lost $3,000. Now 30 minutes time frame,
sharp ratio is -23. Still very low,
still negative. And we've lost $15,000.
Now 15 minutes time frame, sharp is -42.
And we've lost $1.8,000.
5 minutes time frame our sharp ratio is
-90 and we've lost $2,000 so not quite
impressive. Now 3 minute time frame and
sharp is more than minus 100. We're low
and we've lost nearly $25,000. Now let's
see 1 minute time frame. Our sharp is
still negative nearly -1,000
and we've lost $3.7,000.
Now let's go to USA stocks. Firstly one
day time frame. Sharp is positive 1.13
and we've actually made $200. Next 4
hours time frame, sharp is negative
minus $1.6 and now we've lost $250. Now
1 hour time frame, sharp is positive 2.4
and we've made $100. Now 30 minute time
frame, sharp is negative minus 7.9 and
we've lost $300. Now 15 minutes time
frame, sharp is 10.8 8 and we've made
$300. 5 minute time frame, sharp is -12
and we've lost $180. 1 minute time
frame. Let's see the results. And sharp
is nearly - 100 and we've lost $400. Now
let's check futures market 15 minutes
time frame and sharp is minus3 and we've
lost $169.
Next moving to 5m minute time frame.
Sharp is positive $8.69. 69 and we've
made $300. Next moving to 1 minute time
frame. Finally sharp is positive 25.
That's impressive. And we've made $375.
Now we move to Forex market. Starting
with 1 day time frame, our sharp is
minus.06
and we've lost just $4. Next 4 hours
time frame, positive sharp $1.77 and
we've made $70. Next 1 hour time frame,
our sharp is 255 and we've made just $4.
Next 30 minute time frame, sharp is
minus.14
and we've lost just $1. Next 15 minutes
time frame, our sharp is minus3 and
we've lost $22. 5 minutes time frame,
our sharp is 27 quite a lot and we've
made $84. And finally, just 1 minute
time frame, sharp ratio is - 32 and
we've lost $36. That's it for the
results. And as I've mentioned before,
we have some hyperparameters. We used
values from the original video, but they
are optimizable. So here are ranges for
each parameter. And uh I've ran
hyperparameter optimization. Let's check
the results. I ran it using this command
in the top. And uh it turned out that
best parameters are here. They are quite
uh similar to parameters from the video
but a little bit different. And now
let's analyze our results. As you can
see on crypto market, our strategy
performed poorly and we only made some
$69 on one day time frame, which is a
random event. So, it's not very
reliable. And uh the same can be said
about all other markets except for maybe
futures where we were in total in
positive. But overall uh this strategy
doesn't seem reliable because uh we
haven't gotten any consistent results.
And let's check the results table with
hyperparameter optimization. So I have
ran hyperparameter optimization for each
market. So for example for crypto there
is a slight increase from $69 to $115 of
total profit. In stocks we also have a
slight increase but nothing dramatic. In
futures we still have negative profit
but it's actually we've lost less than
before. And same for forex. So as we can
see uh hyperparameter optimization
increases our profits or decreases our
losses but uh for this exact strategy
it's nothing impressive.
```

---

## Transcript: Mean Reversion Trading Strategy Explained & Backtested – 179% Profit
**Video ID:** c9-SIpy3dEw | **Words:** 1176
**URL:** https://www.youtube.com/watch?v=c9-SIpy3dEw

```
Hey everyone. In today's video, we're
testing a mean reversion strategy. We'll
walk through the entire process from
explaining how the strategy works to
optimizing it using Freck Trade, an
open-source trading bot, and finally
running back tests to see how it
performs. If you're looking for a
complete beginnerfriendly Freck Trade
tutorial, check out the link in the
description below. Before we dive into
the strategy, let's first break down the
key indicators we'll be using. One,
Ballinger bands. Ballinger bands are one
of the most popular technical indicators
used by traders to analyze volatility
and price movement. They were developed
by John Ballinger and are composed of
three lines. Middle band. This is
usually a 20 period simple moving
average. It shows the average price over
the past 20 candles. Upper band, this is
the middle band plus two standard
deviations.
Lower band, this is the middle band
minus two standard deviations.
Ballinger bands are often used for mean
reversion strategies. When the closing
price falls below the lower band, it may
indicate the asset is oversold. So, we
consider entering a long position. When
the price crosses above the upper band,
it may suggest the asset is overbought.
So, we consider entering a short
position. However, we can't rely on
Ballinger bands alone. We need to
combine them with other indicators to
avoid false signals.
Two, RSI, the relative strength index,
helps us measure the strength and
momentum of price movements. While RSI
is often used to identify overbought and
oversold conditions, in this strategy,
we use it as a trend filter instead.
Since we're trading on the 1 hour time
frame, the 1 hour RSI can be noisy and
less reliable due to short-term
fluctuations.
That's why we use the RSI on a higher
time frame, 4hour, to identify the
broader trend. If the 4hour RSI is above
55, we only look for long positions. If
the 4hour RSI is below 45, we only look
for short positions. This approach helps
us stay aligned with the overall market
direction and avoid trading against the
trend.
3. Ax.
The average directional index is a
powerful indicator that tells us whether
the market is trending or ranging, but
it doesn't indicate the direction of the
trend. It ranges from 0 to 100. An ADX
value below 20 suggests a weak trend or
sideways choppy market. An ADX value
above 20 indicates a strong trend,
whether it's upward or downward. We
apply ADX to both the 4hour time frame
and the 1 hour trading time frame. To
confirm a valid trend, we want the ADX
to be above 20. This helps us avoid
trading in sideways or choppy markets.
Now that we understand how each
indicator works, let's walk through how
we combine them to identify trade setups
and generate signals. To enter a long
position, we wait for all of the
following conditions to be met. 4hour
RSI is above 55. This indicates the
overall market trend is bullish and we
should only consider long trades. The
ADX must be above 20 on the 1 hour time
frame and above 25 on the 4hour time
frame. This confirms there is enough
trend strength and momentum across both
short and medium-term charts. The
closing price must fall below the lower
Ballinger band on the 1-hour chart. This
suggests the market is oversold and a
potential reversal to the mean may
occur. Once all of these conditions are
met, we enter the long trade on the next
candle. This approach allows us to
combine momentum, volatility, and trend
confirmation, increasing the probability
of a high quality setup. After entering
the trade, we set the stop loss just
below the low of the signal candle to
give the trade enough breathing room and
avoid being stopped out by small
fluctuations.
We further subtract 4.5 times the ATR,
average true range, from the closing
price of the signal candle. We exit the
trade when the price closes above the
upper Ballinger band. This typically
indicates the price has become
overbought and a pullback may follow.
By exiting at this point, we lock in
profits before the market reverses.
To enter a short position, all of the
following conditions must be met. 4hour
RSI is below 45. Indicates a bearish
trend. ADX is above 20 on the 1 hour
chart and above 25 on the 4hour chart.
Confirms trend strength. Price closes
above the upper Ballinger band. Suggests
overbought condition.
We enter the short trade on the next
candle. Stop loss is set just above the
high of the signal candle plus 4.5 times
ATR for volatility buffer. Take profit
when price closes below the lower
Ballinger band indicates a potential
reversal.
Now that we understand the strategy,
let's move on to optimization using fret
trade. We'll test on the 1 hour time
frame and split the data into in sample
8 months. For tuning the strategy, out
of sample 4 months for testing on unseen
data, key parameters to optimize include
BB period and BBSTDS
control the length and width of the
Ballinger bands, HTF RSI threshold helps
define the RSI level used to filter
trade direction. HTFA DX threshold and a
DX threshold ensure sufficient trend
strength on both time frames. ATR
multiplier determines how far the stop
loss is placed from the signal candle.
We'll use Freck Trade's hyper opt
feature to find the best combination of
these parameters. Once optimization is
done, we'll run a full back test to see
how the strategy performs with the best
parameters in place.
All right, let's take a closer look at
the back testing results. We ran the
back test on near perpetual futures
using the 1-hour time frame over the
past year with the optimized settings.
Here's what we found. The strategy
delivered a total profit of 179%.
For comparison, the market itself
returned around -66%
during the same period. The maximum draw
down was 19.318%.
which means the strategy did experience
some pullbacks, but overall it managed
risk well and stayed profitable. Now,
let's take a look at the visual summary
of these results. This strategy was
optimized using 8 months of insample
data, then tested on 4 months of outof
sample data that helps ensure it's not
just curve fitted to the past, but also
performs well in new unseen market
conditions. If you'd like access to the
full strategy file along with the
complete step-by-step fract tutorial,
it's available to supporters through the
link in the description. It's a great
way to support the channel, and in
return, you get everything you need to
build, run, and customize this strategy
on your own. There's also a pair
optimized version available, so you can
run it across multiple trading pairs,
each with its own fine-tuned parameters
for better performance. If you found
this video helpful, don't forget to
subscribe, turn on notifications, and
share it with your friends who might
find it useful, too. Thanks for
watching, and I'll catch you in the next
one.
```

---
