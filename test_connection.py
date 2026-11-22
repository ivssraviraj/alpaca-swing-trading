
import os
from datetime import datetime, timedelta, timezone

from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed

key = os.environ["APCA_API_KEY_ID"]
secret = os.environ["APCA_API_SECRET_KEY"]

trading = TradingClient(key, secret, paper=True)
account = trading.get_account()

print("Paper account:", account.status)
print("Buying power:", account.buying_power)

data = StockHistoricalDataClient(key, secret)
end = datetime.now(timezone.utc) - timedelta(minutes=20)

request = StockBarsRequest(
    symbol_or_symbols=["AAPL"],
    timeframe=TimeFrame(5, TimeFrameUnit.Minute),
    start=end - timedelta(days=7),
    end=end,
    feed=DataFeed.IEX,
)

bars = data.get_stock_bars(request).df
assert not bars.empty, "No historical bars returned"
print(bars.tail(5))
print("Connection test passed")
