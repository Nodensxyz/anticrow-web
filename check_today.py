import json
from datetime import datetime

with open('trade_history.json', 'r', encoding='utf-8') as f:
    trades = json.load(f)

today = datetime.now().strftime('%Y-%m-%d')
today_trades = [t for t in trades if t.get('close_time', '').startswith(today)]

print(f"=== {today} の決済トレード ===")
total = 0
for t in today_trades:
    pnl = t.get('profit', 0)
    total += pnl
    ct = t.get('close_time', '?')[-8:]
    d = t.get('direction', '?')
    s = t.get('symbol', '?')
    print(f"  {ct} {d:>4} {s} -> {pnl:+,.0f}")

print(f"\n合計: {total:+,.0f} ({len(today_trades)}件)")

# 全CLOSEDも確認
all_closed = [t for t in trades if t.get('status') == 'CLOSED']
print(f"\nCLOSED全件数: {len(all_closed)}")
