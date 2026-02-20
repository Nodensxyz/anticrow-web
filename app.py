import streamlit as st
import pandas as pd
import json
import requests
from datetime import datetime

# --- ページ設定 ---
st.set_page_config(page_title="AntiCrow Cal", layout="centered")

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

# --- データ読み込み (v4.5: GitHubから最新データを取得) ---
GITHUB_RAW_URL = "https://raw.githubusercontent.com/Nodensxyz/anticrow-web/main/trade_history.json"

@st.cache_data(ttl=30)  # 30秒キャッシュ（リロードで最新取得）
def load_data():
    try:
        # まずGitHubから最新を取得
        resp = requests.get(GITHUB_RAW_URL, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        # フォールバック: ローカルファイル
        with open('trade_history.json', 'r', encoding='utf-8') as f:
            data = json.load(f)
    
    df = pd.DataFrame(data)
    df = df[df['status'] == 'CLOSED']  # 決済済みのみ
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
    from streamlit_calendar import calendar as st_calendar

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
        "headerToolbar": {"left": "prev,next", "center": "title", "right": ""},
        "initialView": "dayGridMonth",
        "fixedWeekCount": False,
        "height": "auto",
        "handleWindowResize": True,
        "longPressDelay": 1000,
    }
    
    st_calendar(events=calendar_events, options=calendar_options)

    # --- 最終更新時刻 ---
    st.caption(f"最終更新: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

except Exception as e:
    st.error(f"Error: {e}")