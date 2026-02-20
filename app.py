import streamlit as st
import pandas as pd
import json
from streamlit_calendar import calendar
from datetime import datetime

# --- ページ設定 ---
st.set_page_config(page_title="Antigravity Trade Calendar", layout="wide")
st.title("🐦 AntiCrow Analysis: Trade Calendar")

# --- データ読み込み ---
def load_data():
    with open('trade_history.json', 'r', encoding='utf-8') as f:
        data = json.load(f)
    df = pd.DataFrame(data)
    
    # ここを修正：format='mixed' を追加
    df['close_time'] = pd.to_datetime(df['close_time'], format='mixed')
    
    return df

try:
    df = load_data()
    
    # --- 日次損益の集計 ---
    df['date'] = df['close_time'].dt.date
    daily_stats = df.groupby('date')['profit'].sum().reset_index()

    # --- カレンダー用イベントの作成 ---
    calendar_events = []
    for index, row in daily_stats.iterrows():
        profit = row['profit']
        color = "#2ecc71" if profit > 0 else "#e74c3c" # 勝ち=緑, 負け=赤
        
        calendar_events.append({
            "title": f"{profit:+,.0f} JPY",
            "start": row['date'].isoformat(),
            "backgroundColor": color,
            "borderColor": color,
            "allDay": True
        })

    # --- レイアウト ---
    col1, col2 = st.columns([3, 1])

    with col1:
        # カレンダー本体
        calendar_options = {
            "headerToolbar": {"left": "prev,next today", "center": "title", "right": "dayGridMonth"},
            "initialView": "dayGridMonth",
        }
        calendar(events=calendar_events, options=calendar_options)

    with col2:
        # 統計サマリー
        total_profit = df['profit'].sum()
        win_count = len(df[df['profit'] > 0])
        total_trades = len(df)
        
        st.subheader("📊 収支統計")
        st.metric("累計損益", f"{total_profit:+,.0f} JPY")
        st.metric("勝率", f"{(win_count/total_trades)*100:.1f} %")
        st.write(f"総トレード数: {total_trades}")
        
        # 直近残高の表示
        current_balance = 114875  # 直近の実績値
        st.info(f"現在の有効証拠金: {current_balance:,.0f} JPY")

except Exception as e:
    st.error(f"データの読み込みに失敗しました: {e}")