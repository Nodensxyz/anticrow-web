import pandas as pd

def judge(df):
    # データが足りない（20行未満）場合は判定しない
    if len(df) < 20: 
        return "WAIT"
    
    # 最新の1行を取得
    r = df.iloc[-1]
    
    # 必要な数値を取得
    close = r["Close"]
    open_p = r["Open"]
    high = r["High"]
    low = r["Low"]
    bb_upper = r["Bb_upper"]
    bb_lower = r["Bb_lower"]
    bb_mid = r["Bb_mid"]
    
    # --- 1. 乖離率の計算 ---
    deviation = (close - bb_mid) / bb_mid
    target_dev = 0.08 / 100 
    
    # --- 2. ローソク足の形状分析 ---
    candle_range = max(high - low, 0.01) # 足全体の長さ
    body_size = abs(open_p - close)       # 実体の長さ
    
    # 上髭の割合（高値から実体の上辺まで）
    upper_wick = high - max(open_p, close)
    upper_wick_ratio = upper_wick / candle_range
    
    # 下髭の割合（実体の下辺から安値まで）
    lower_wick = min(open_p, close) - low
    lower_wick_ratio = lower_wick / candle_range

    # --- 3. キリ番（ラウンドナンバー）判定 ---
    # ゴールド用：10ドル刻みのキリ番（例：2600.00, 2610.00）の前後0.5ドル以内か
    # 価格を10で割った余りが 0.5以下、または 9.5以上ならキリ番付近
    is_near_round_number = (close % 10 <= 0.5) or (close % 10 >= 9.5)

    # --- 最終判定 ---

    # 【売り判定】
    # 条件：3σ上抜け ＋ 乖離十分 ＋ 上髭が全体の30%以上 ＋ キリ番付近
    if (close > bb_upper) and (deviation > target_dev):
        if upper_wick_ratio > 0.3 and is_near_round_number:
            return "SELL"
    
    # 【買い判定】
    # 条件：3σ下抜け ＋ 乖離十分 ＋ 下髭が全体の30%以上 ＋ キリ番付近
    if (close < bb_lower) and (deviation < -target_dev):
        if lower_wick_ratio > 0.3 and is_near_round_number:
            return "BUY"

    # 条件に合わなければ待機
    return "WAIT"