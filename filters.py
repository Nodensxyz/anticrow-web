from datetime import datetime, time

# 超重要指標が出やすい時間（日本時間目安）
DANGER_TIMES = [
    (time(21, 20), time(21, 40)),  # 米指標
    (time(23, 50), time(0, 10)),   # FOMC系（日を跨ぐパターン）
]

def is_danger_time(now: datetime) -> bool:
    """
    指定された時刻が危険時間帯に含まれるか判定する。
    """
    t = now.time()
    
    for start, end in DANGER_TIMES:
        if start <= end:
            # 通常の時間帯（例：21:20 - 21:40）
            # start <= t <= end の形にすることで、範囲内のみをTrueにします
            if start <= t <= end:
                return True
        else:
            # 日を跨ぐ時間帯（例：23:50 - 0:10）
            # 開始時刻より後、または終了時刻より前であればTrue
            if t >= start or t <= end:
                return True
                
    return False