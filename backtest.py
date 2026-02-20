"""
Antigravity Bot バックテスト v3.1
========================================
昨日のGOLD 1分足CSVデータを読み込み、
v2.0(旧設定) と v3.1(新設定) の仮想トレード結果を比較表示する。

使い方:
  python backtest.py
"""

import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

import pandas as pd
import json
import os
from datetime import datetime

# ─── 設定読み込み ───────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_FILE = os.path.join(SCRIPT_DIR, "GOLD#_M1_NEW.csv")

with open(os.path.join(SCRIPT_DIR, "config.json"), "r") as f:
    full_config = json.load(f)

# ─── テスト設定 ─────────────────────────────────────────
# v2.0 設定 (前回テストした緩い設定)
CONFIG_V2 = {
    "LOT": 0.02,
    "SL_POINTS": 400,
    "TP_POINTS": 600,
    "MAX_POSITIONS": 3,
    "MIN_DISTANCE": 200,
    "ATR_THRESHOLD": 3.0,
    "TREND_FILTER": True,
    "COOLDOWN_MINUTES": 45,
    "RSI_TREND_BUY": 40,
    "RSI_TREND_SELL": 60,
    "RSI_RANGE_BUY": 30,
    "RSI_RANGE_SELL": 70,
    "NANPIN_RSI_OFFSET": 0,
}

# v3.1 現在設定 (タイトSL、厳格RSI)
CONFIG_V31 = full_config["SYMBOLS"]["GOLD#"].copy()

# GOLD特化ハイブリッド型 (バランス設定)
CONFIG_HYBRID = {
    "LOT": 0.02,
    "SL_POINTS": 600,
    "TP_POINTS": 600,
    "MAX_POSITIONS": 3,
    "MIN_DISTANCE": 200,
    "ATR_THRESHOLD": 5.0, 
    "TREND_FILTER": True,
    "COOLDOWN_MINUTES": 45,
    "RSI_TREND_BUY": 37,
    "RSI_TREND_SELL": 63,
    "RSI_RANGE_BUY": 37,
    "RSI_RANGE_SELL": 63,
    "NANPIN_RSI_OFFSET": 5,
}

POINT = 0.01  # GOLD# の1ポイント = $0.01
WAIT_SECONDS = full_config.get("WAIT_SECONDS", 300)

# ─── テクニカル指標の計算 ─────────────────────────────────
def calc_indicators(df):
    """SMA200, SMA20, RSI14, ATR14 を計算"""
    df["sma200"] = df["close"].rolling(200).mean()
    df["sma20"] = df["close"].rolling(20).mean()
    
    # RSI
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.rolling(14).mean()
    avg_loss = loss.rolling(14).mean()
    rs = avg_gain / avg_loss
    df["rsi"] = 100 - (100 / (1 + rs))
    
    # ATR
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()
    
    return df

# ─── シグナル判定 (main.py と同一ロジック) ─────────────────
def check_signal(row, config, same_dir_count=0):
    """main.py の check_trading_conditions と同じロジック"""
    close = row["close"]
    sma200 = row["sma200"]
    sma20 = row["sma20"]
    rsi = row["rsi"]
    
    if pd.isna(sma200) or pd.isna(rsi):
        return "WAIT", "", ""
    
    is_uptrend = close > sma200
    is_downtrend = close < sma200
    ma_distance = abs(close - sma200)
    is_range = ma_distance < (close * 0.0005)
    
    # RSI閾値
    rsi_trend_buy = config.get("RSI_TREND_BUY", 35)
    rsi_trend_sell = config.get("RSI_TREND_SELL", 65)
    rsi_range_buy = config.get("RSI_RANGE_BUY", 28)
    rsi_range_sell = config.get("RSI_RANGE_SELL", 72)
    
    # ナンピンオフセット
    nanpin_offset = config.get("NANPIN_RSI_OFFSET", 0) * same_dir_count
    
    signal = "WAIT"
    reason = ""
    strategy = ""
    
    if is_range:
        if rsi <= rsi_range_buy - nanpin_offset:
            signal = "BUY"; reason = f"レンジ逆張り"; strategy = "Range"
        elif rsi >= rsi_range_sell + nanpin_offset:
            signal = "SELL"; reason = f"レンジ逆張り"; strategy = "Range"
    elif is_uptrend:
        if rsi <= rsi_trend_buy - nanpin_offset:
            signal = "BUY"; reason = f"トレンド押し目"; strategy = "Trend"
    elif is_downtrend:
        if rsi >= rsi_trend_sell + nanpin_offset:
            signal = "SELL"; reason = f"トレンド戻り"; strategy = "Trend"
    
    # トレンドフィルター
    if config.get("TREND_FILTER", True) and signal != "WAIT":
        if signal == "BUY" and close < sma20:
            return "WAIT", "フィルタ", ""
        if signal == "SELL" and close > sma20:
            return "WAIT", "フィルタ", ""
    
    return signal, reason, strategy

# ─── バックテストエンジン ──────────────────────────────────
class BacktestEngine:
    def __init__(self, config, label):
        self.config = config
        self.label = label
        self.positions = []       # {direction, entry_price, entry_time, sl, tp, strategy}
        self.closed_trades = []   # {direction, entry_price, exit_price, entry_time, exit_time, pnl, result, strategy}
        self.last_trade_time = 0
        self.cooldown_until = 0
        self.consecutive_losses = 0
    
    def run(self, df):
        for i, row in df.iterrows():
            if pd.isna(row["sma200"]): continue
            
            ts = row["timestamp"]
            close = row["close"]
            high = row["high"]
            low = row["low"]
            atr = row["atr"]
            
            # 1. 既存ポジションのSL/TPチェック
            new_positions = []
            for pos in self.positions:
                hit_sl = False
                hit_tp = False
                
                if pos["direction"] == "BUY":
                    if low <= pos["sl"]:
                        hit_sl = True
                        exit_price = pos["sl"]
                    elif high >= pos["tp"]:
                        hit_tp = True
                        exit_price = pos["tp"]
                else:  # SELL
                    if high >= pos["sl"]:
                        hit_sl = True
                        exit_price = pos["sl"]
                    elif low <= pos["tp"]:
                        hit_tp = True
                        exit_price = pos["tp"]
                
                if hit_sl or hit_tp:
                    if pos["direction"] == "BUY":
                        pnl_points = (exit_price - pos["entry_price"]) / POINT
                    else:
                        pnl_points = (pos["entry_price"] - exit_price) / POINT
                    
                    # 損益計算 (GOLD 0.01Lot = 1円/point 概算)
                    pnl_yen = pnl_points * (self.config["LOT"] / 0.01)
                    
                    self.closed_trades.append({
                        "direction": pos["direction"],
                        "entry_price": pos["entry_price"],
                        "exit_price": exit_price,
                        "entry_time": pos["entry_time"],
                        "exit_time": ts,
                        "pnl": pnl_yen,
                        "result": "WIN" if pnl_yen > 0 else "LOSS",
                        "strategy": pos["strategy"],
                    })
                    
                    if pnl_yen <= 0:
                        self.consecutive_losses += 1
                    else:
                        self.consecutive_losses = 0
                else:
                    new_positions.append(pos)
            
            self.positions = new_positions
            
            # 2. クールダウンチェック
            if ts < self.cooldown_until:
                continue
            
            # 連敗クールダウン
            cooldown_min = self.config.get("COOLDOWN_MINUTES", 0)
            if cooldown_min > 0 and self.consecutive_losses >= 2:
                self.cooldown_until = ts + cooldown_min * 60
                self.consecutive_losses = 0
                continue
            
            # 3. 新規エントリー判定
            if ts - self.last_trade_time < WAIT_SECONDS:
                continue
            
            if len(self.positions) >= self.config["MAX_POSITIONS"]:
                continue
            
            # ATRフィルタ
            if pd.notna(atr) and atr > self.config.get("ATR_THRESHOLD", 99999):
                continue
            
            # 同方向ポジション数カウント
            # まず通常シグナルチェック
            signal, reason, strategy = check_signal(row, self.config, 0)
            
            if signal == "WAIT":
                continue
            
            # 同方向ポジション数
            same_dir_count = sum(1 for p in self.positions if p["direction"] == signal)
            
            # MIN_DISTANCEチェック
            if same_dir_count > 0:
                same_dir_positions = [p for p in self.positions if p["direction"] == signal]
                last_pos = same_dir_positions[-1]
                dist = abs(close - last_pos["entry_price"])
                min_dist_val = self.config.get("MIN_DISTANCE", 0) * POINT
                if dist < min_dist_val:
                    continue
                
                # ナンピンRSI再チェック (v3.1)
                signal2, _, _ = check_signal(row, self.config, same_dir_count)
                if signal2 != signal:
                    continue  # ナンピン厳格化条件を満たさない
            
            # エントリー実行
            sl_p = self.config["SL_POINTS"]
            tp_p = self.config["TP_POINTS"]
            
            if signal == "BUY":
                sl_price = close - sl_p * POINT
                tp_price = close + tp_p * POINT
            else:
                sl_price = close + sl_p * POINT
                tp_price = close - tp_p * POINT
            
            self.positions.append({
                "direction": signal,
                "entry_price": close,
                "entry_time": ts,
                "sl": sl_price,
                "tp": tp_price,
                "strategy": strategy,
            })
            self.last_trade_time = ts
    
    def summary(self):
        wins = [t for t in self.closed_trades if t["result"] == "WIN"]
        losses = [t for t in self.closed_trades if t["result"] == "LOSS"]
        total = len(self.closed_trades)
        win_rate = (len(wins) / total * 100) if total > 0 else 0
        total_pnl = sum(t["pnl"] for t in self.closed_trades)
        
        # 未決済ポジションの含み損益
        unrealized = len(self.positions)
        
        return {
            "total": total,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": win_rate,
            "total_pnl": total_pnl,
            "avg_win": sum(t["pnl"] for t in wins) / len(wins) if wins else 0,
            "avg_loss": sum(t["pnl"] for t in losses) / len(losses) if losses else 0,
            "unrealized": unrealized,
        }

# ─── メイン実行 ──────────────────────────────────────────
def main():
    print("=" * 70)
    print("  Antigravity Bot 比較バックテスト")
    print(f"  データ: GOLD# 1分足")
    print("=" * 70)
    
    # CSV読み込み
    df = pd.read_csv(CSV_FILE, sep="\t")
    df.columns = ["date", "time", "open", "high", "low", "close", "tickvol", "vol", "spread"]
    
    # タイムスタンプ変換
    df["datetime"] = pd.to_datetime(df["date"] + " " + df["time"], format="%Y.%m.%d %H:%M:%S")
    df["timestamp"] = df["datetime"].apply(lambda x: x.timestamp())
    df["day"] = df["datetime"].dt.date
    
    # テクニカル指標計算
    print("  テクニカル指標を計算中...", end="", flush=True)
    df = calc_indicators(df)
    print(" 完了")
    
    available_days = sorted(df["day"].unique())
    print(f"  期間: {available_days[0]} ～ {available_days[-1]} ({len(available_days)}日間)")
    print()

    configs = [
        (CONFIG_V31, "現設定(GOLD強気HB)"),
    ]

    # 全期間実行
    engines = []
    for cfg, label in configs:
        engine = BacktestEngine(cfg, label)
        engine.run(df)
        engines.append(engine)

    # ─── 日別分析 (2/16, 2/17, 2/18 にフォーカス) ─────────────────
    print("■ 16日・17日・18日の日別シミュレーション結果")
    print("  " + "─" * 60)
    target_days = [pd.to_datetime(d).date() for d in ["2026-02-16", "2026-02-17", "2026-02-18"]]
    
    grand_total = 0
    for day in target_days:
        if day not in available_days: continue
        for i, (cfg, label) in enumerate(configs):
            day_df = df[df["day"] == day].copy()
            engine = BacktestEngine(cfg, label)
            engine.run(day_df)
            res = engine.summary()
            grand_total += res['total_pnl']
            print(f"【{day}】エントリー {res['total']:>2}回 / 勝{res['wins']}敗{res['losses']} / 損益 {res['total_pnl']:>+8.0f}円 / 勝率 {res['win_rate']:>4.1f}%")
            # トレード詳細
            for j, t in enumerate(engine.closed_trades, 1):
                dt = datetime.fromtimestamp(t["entry_time"]).strftime("%H:%M")
                marker = "🎉" if t["result"] == "WIN" else "💸"
                print(f"    {marker} #{j} {dt} {t['direction']:>4} @{t['entry_price']:.2f}→{t['exit_price']:.2f} {t['pnl']:>+8.0f}円")
        print()
    
    print(f"  {'═' * 55}")
    print(f"  3日間合計損益: {grand_total:>+8.0f}円")


if __name__ == "__main__":
    main()
