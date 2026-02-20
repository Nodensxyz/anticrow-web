import MetaTrader5 as mt5
import pandas as pd

def get_price(symbol="XAUUSD", timeframe=mt5.TIMEFRAME_M1, count=100):
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
    
    if rates is None or len(rates) == 0:
        # MT5がその銘柄を認識していない可能性があるので、選択状態にする
        mt5.symbol_select(symbol, True)
        rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
        if rates is None or len(rates) == 0:
            print(f"❌ {symbol} のデータ取得に失敗。MT5の気配値にこの銘柄があるか確認してください。")
            return pd.DataFrame()

    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    
    # 全ての列名の先頭を大文字にする (close -> Close)
    df.columns = [col.capitalize() for col in df.columns]
    
    return df