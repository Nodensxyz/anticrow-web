import pandas as pd

def add_indicators(df):
    # ボリンジャーバンドの計算 (期間20, 3σ)
    # 20日移動平均
    df["Bb_mid"] = df["Close"].rolling(window=20).mean()
    # 標準偏差
    std = df["Close"].rolling(window=20).std()
    # 3σの上下限
    df["Bb_upper"] = df["Bb_mid"] + (std * 3)
    df["Bb_lower"] = df["Bb_mid"] - (std * 3)
    
    # ATRの計算 (期間14)
    high_low = df["High"] - df["Low"]
    high_close = (df["High"] - df["Close"].shift()).abs()
    low_close = (df["Low"] - df["Close"].shift()).abs()
    
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df["Atr"] = tr.rolling(window=14).mean()
    
    return df