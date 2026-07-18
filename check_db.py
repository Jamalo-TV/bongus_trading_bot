import sqlite3
import pandas as pd
conn = sqlite3.connect('/mnt/data/bongus_trading_bot/bongus/data/live_trader_v2.db')
print(pd.read_sql('SELECT symbol, recovery_state FROM positions', conn))
