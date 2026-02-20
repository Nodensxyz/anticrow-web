import streamlit as st
import pandas as pd
import json
from streamlit_calendar import calendar
from datetime import datetime

# --- ページ設定 ---
st.set_page_config(page_title="AntiCrow Cal", layout="centered") # layoutをcenteredに

# --- スマホ用カスタムCSS ---
st.markdown("""
    <style>
    /* 文字サイズと余白の調整 */
    h1 { font-size: 1.5rem !important; }
    .stMetric { font-size: 0.8rem !important; }
    [data-testid="stMetricValue"] { font-size: 1.2rem !important; }
    
    /* カレンダーの高さをスマホに合わせる */
    .fc { font-size: 0.8em !important; max-height: 450px !important; }
    </style>
    """, unsafe_allow_html=True)

st.title("🐦 AntiCrow Analysis")

# --- データ読み込み ---
def load_data():
    with open('trade_history.json', 'r', encoding='utf-8') as f:
        data = json.load(f)
    df = pd.DataFrame(data)
    df['close_time'] = pd.to_datetime(df['close_time'], format='mixed')
    return df

try:
    df = load_data()
    df['date'] = df['close_time'].dt.date
    daily_stats = df.groupby('date')['profit'].sum().reset_index()

    # --- 統計情報を最上部に配置（スマホで見やすく） ---
    total_profit = df['profit'].sum()
    win_count = len(df[df['profit'] > 0])
    total_trades = len(df)
    
    m1, m2, m3 = st.columns(3)
    m1.metric("累計損益", f"{total_profit:+,.0f}")
    m2.metric("勝率", f"{(win_count/total_trades)*100:.0f}%")
    m3.metric("取引数", f"{total_trades}")

    # --- カレンダー用イベント作成 ---
    calendar_events = []
    for _, row in daily_stats.iterrows():
        p = row['profit']
        color = "#2ecc71" if p > 0 else "#e74c3c"
        calendar_events.append({
            "title": f"{p:+,.0f}",
            "start": row['date'].isoformat(),
            "backgroundColor": color,
            "borderColor": color,
            "allDay": True
        })

    # --- カレンダー設定（スライド禁止・固定表示） ---
    calendar_options = {
        "headerToolbar": {"left": "prev,next", "center": "title", "right": ""}, # 表示切替を消去
        "initialView": "dayGridMonth",
        "fixedWeekCount": False, # 月によって週数を変えてコンパクトに
        "height": "auto",        # 内容に合わせて高さを自動調整
        "handleWindowResize": True,
        "longPressDelay": 1000,  # 誤操作防止
    }
    
    calendar(events=calendar_events, options=calendar_options)

except Exception as e:
    st.error(f"Error: {e}")