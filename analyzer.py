import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime, timedelta

# --- main.pyのロジックをシミュレーション用に移植 ---
def simulate_trading(df, lot, rsi_buy, rsi_sell, cooldown_min):
    balance = 100000  # 初期資金10万円
    positions = []
    closed_trades = []
    
    # エラー対策：初期値を「データの開始時間」に設定
    last_close_time = df['time'].iloc[0]
    
    # 指標の計算
    df['sma200'] = df['close'].rolling(window=200).mean()
    df['sma20'] = df['close'].rolling(window=20).mean()
    
    # RSI計算
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df['rsi'] = 100 - (100 / (1 + rs))

    for i in range(200, len(df)):
        row = df.iloc[i]
        current_time = row['time']
        
        # 決済チェック (簡易版: 固定SL/TP 600 points = 6.0ドル幅)
        if positions:
            pos = positions[0]
            profit = 0
            if pos['type'] == 'BUY':
                if row['high'] >= pos['tp']: profit = 600 * 10 
                elif row['low'] <= pos['sl']: profit = -600 * 10
            else: # SELL
                if row['low'] <= pos['tp']: profit = 600 * 10
                elif row['high'] >= pos['sl']: profit = -600 * 10
            
            if profit != 0:
                trade_pnl = (profit * lot / 0.01)
                balance += trade_pnl
                closed_trades.append({
                    'time': current_time, 
                    'type': pos['type'],
                    'profit': trade_pnl, 
                    'balance': balance
                })
                last_close_time = current_time
                positions = []
                continue

        # エントリー判定 (main.pyのロジック流用)
        if not positions and (current_time - last_close_time) >= timedelta(minutes=cooldown_min):
            # 順張り押し目買い判定
            if row['close'] > row['sma200'] and row['rsi'] <= rsi_buy and row['close'] > row['sma20']:
                tp = row['close'] + 6.0
                sl = row['close'] - 6.0
                positions.append({'type': 'BUY', 'entry': row['close'], 'tp': tp, 'sl': sl})
            # 順張り戻り売り判定
            elif row['close'] < row['sma200'] and row['rsi'] >= rsi_sell and row['close'] < row['sma20']:
                tp = row['close'] - 6.0
                sl = row['close'] + 6.0
                positions.append({'type': 'SELL', 'entry': row['close'], 'tp': tp, 'sl': sl})

    return pd.DataFrame(closed_trades), balance

# --- Streamlit UI ---
st.set_page_config(page_title="Antigravity Analyzer Pro", layout="wide")
st.title("🚀 Antigravity Analyzer Pro v1.1")

st.sidebar.header("📊 パラメータ設定")
input_lot = st.sidebar.slider("ロット数", 0.01, 0.10, 0.03)
input_rsi_buy = st.sidebar.slider("買いRSI (押し目)", 30, 50, 43)
input_rsi_sell = st.sidebar.slider("売りRSI (戻り)", 50, 70, 57)
input_cooldown = st.sidebar.slider("クールダウン(分)", 0, 60, 30)

uploaded_file = st.file_uploader("MT5のCSV(GOLD 1分足)をアップロード", type='csv')

if uploaded_file:
    # MT5のエクスポート形式(タブ区切り)に対応
    data = pd.read_csv(uploaded_file, sep='\t', names=['date', 'time', 'open', 'high', 'low', 'close', 'tickvol', 'vol', 'spread'], header=0)
    data['time'] = pd.to_datetime(data['date'] + ' ' + data['time'])
    
    with st.spinner('計算中...'):
        results, final_balance = simulate_trading(data, input_lot, input_rsi_buy, input_rsi_sell, input_cooldown)
    
    st.subheader("🏁 シミュレーション結果")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("最終残高", f"{final_balance:,.0f}円")
    c2.metric("純利益", f"{final_balance - 100000:+,.0f}円")
    c3.metric("トレード回数", f"{len(results)}回")
    win_count = len(results[results['profit'] > 0]) if not results.empty else 0
    win_rate = (win_count / len(results) * 100) if len(results) > 0 else 0
    c4.metric("勝率", f"{win_rate:.1f}%")

    if not results.empty:
        st.subheader("📈 資産曲線")
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=results['time'], y=results['balance'], mode='lines+markers', name='資産'))
        st.plotly_chart(fig, use_container_width=True)
        
        st.subheader("📝 トレード履歴")
        st.dataframe(results)
    else:
        st.warning("この設定ではトレードが発生しませんでした。数値を緩めてみてください。")