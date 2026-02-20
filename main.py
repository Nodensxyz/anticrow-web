import MetaTrader5 as mt5
import time
import pandas as pd
import requests
import json
import os
import sys
from datetime import datetime, timedelta
import logging

# --- ログ設定 ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE_DIR, "system_log.txt")
TRADE_HISTORY_FILE = os.path.join(BASE_DIR, "trade_history.json")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8', mode='a'),
        logging.StreamHandler(sys.stdout)
    ]
)

# --- 設定ファイルの読み込み ---
def load_config():
    if not os.path.exists(CONFIG_FILE):
        logging.error(f"{CONFIG_FILE} が見つかりません。")
        print(f"[ERROR] {CONFIG_FILE} が見つかりません。")
        sys.exit(1)
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

CONFIG = load_config()

# --- 定数設定 (Global) ---
MAGIC_NUMBER = CONFIG.get("MAGIC_NUMBER", 123456)
WAIT_SECONDS = CONFIG.get("WAIT_SECONDS", 300)
WEBHOOK_URL = CONFIG.get("WEBHOOK_URL", "")
SYMBOLS_CONFIG = CONFIG.get("SYMBOLS", {})

class AntigravityBot:
    def __init__(self):
        self.last_balance = 0.0
        self.last_trade_time = {} # Symbolごとに管理
        self.is_connected = False
        self.trades = self.load_trades()
        self.last_report_date = datetime.now().date() - timedelta(days=1)
        self.error_notified = {}      # シンボルごとのエラー通知済みフラグ (v3.1)
        self.error_pause_until = {}   # シンボルごとのリトライ待機時刻 (v3.1)
        self._cooldown_notified = {}  # クールダウン通知済みフラグ (v4.0)
        self._last_nanpin_time = {}   # ナンピン最終エントリー時刻 (v4.0)
        self._trail_max_profit = {}   # トレイリング最大含み益 (v4.2)
        
        # シンボルごとの最終トレード時間を初期化
        for sym in SYMBOLS_CONFIG.keys():
            self.last_trade_time[sym] = 0.0

    def load_trades(self):
        """トレード履歴を読み込む"""
        if not os.path.exists(TRADE_HISTORY_FILE):
            return []
        try:
            with open(TRADE_HISTORY_FILE, 'r', encoding='utf-8') as f:
                trades = json.load(f)
                # 旧データ（シンボルなし）への互換性対応
                for t in trades:
                    if "symbol" not in t:
                        t["symbol"] = "GOLD#" # デフォルトをGOLD#と仮定
                return trades
        except Exception as e:
            logging.error(f"履歴読み込みエラー: {e}")
            return []

    def save_trade(self, ticket, symbol, strategy, direction, price):
        """新規トレードを記録"""
        trade = {
            "ticket": ticket,
            "symbol": symbol,
            "strategy": strategy,
            "direction": direction,
            "entry_time": datetime.now().isoformat(),
            "entry_price": price,
            "status": "OPEN",
            "profit": 0.0,
            "close_time": None
        }
        self.trades.append(trade)
        self._save_file()

    def monitor_open_trades(self):
        """オープンポジションを監視し、決済されたら即時通知する (v2.2)"""
        if not self.trades: return

        # 現在のMT5上のポジションを全て取得
        current_positions = mt5.positions_get()
        current_tickets = [p.ticket for p in current_positions] if current_positions else []

        updated = False
        # メモリ上のOPENトレードをチェック
        for trade in self.trades:
            if trade["status"] == "OPEN":
                # MT5上のポジションリストに存在しなければ、決済されたとみなす
                if trade["ticket"] not in current_tickets:
                    # 詳細情報を取得するため、履歴をピンポイントで検索
                    # (ポジションID = ticket で検索)
                    from_date = datetime.now() - timedelta(days=5) # 念のため広めに
                    deals = mt5.history_deals_get(position=trade["ticket"])
                    
                    if deals:
                        # 決済Dealを探す (Entry In以外)
                        close_deal = None
                        total_profit = 0.0
                        
                        for deal in deals:
                             if deal.entry == mt5.DEAL_ENTRY_OUT or deal.entry == mt5.DEAL_ENTRY_INOUT:
                                 close_deal = deal
                                 total_profit += (deal.profit + deal.swap + deal.commission)
                        
                        if close_deal:
                            trade["status"] = "CLOSED"
                            trade["profit"] = total_profit
                            trade["close_time"] = datetime.fromtimestamp(close_deal.time).isoformat()
                            updated = True
                            
                            self.notify_close(trade)
                            logging.info(f"決済検知: Ticket {trade['ticket']} ({trade['symbol']})")

        if updated:
            self._save_file()

    def _save_file(self):
        """履歴ファイルを保存"""
        try:
            with open(TRADE_HISTORY_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.trades, f, indent=4, ensure_ascii=False)
        except Exception as e:
            logging.error(f"履歴保存エラー: {e}")

    def notify_close(self, trade):
        """決済通知を送る (v2.2: 本日収益追加)"""
        stats, (daily_total, _) = self.calculate_stats()
        win_rate_msg = self.get_win_rate_str(stats)
        
        symbol = trade.get("symbol", "GOLD#")
        profit = trade["profit"]
        strategy = trade["strategy"]

        # 本日の収益状況
        daily_pnl_str = f"💰 本日合計: {daily_total:+,.0f}円"

        if profit > 0:
            msg = f"🎉 **利確決済 ({symbol} / {strategy})** (+{profit:,.0f}円)\n{daily_pnl_str}\n\n{win_rate_msg}"
        else:
            msg = f"💸 **損切り決済 ({symbol} / {strategy})** ({profit:,.0f}円)\n{daily_pnl_str}\n\n{win_rate_msg}"
        
        self.send_discord(msg)
        logging.info(f"\n{msg}")

    def calculate_stats(self):
        """勝率と損益を集計 (シンボル別対応)"""
        stats = {
            "total": {"wins": 0, "losses": 0, "profit": 0.0},
            "Trend": {"wins": 0, "losses": 0},
            "Range": {"wins": 0, "losses": 0}
        }
        # シンボルごとの集計初期化
        for sym in SYMBOLS_CONFIG.keys():
            stats[sym] = {"profit": 0.0, "wins": 0, "losses": 0}
        
        today_str = datetime.now().strftime('%Y-%m-%d')
        daily_profit_total = 0.0
        daily_profit_breakdown = {sym: 0.0 for sym in SYMBOLS_CONFIG.keys()}

        for trade in self.trades:
            if trade["status"] != "CLOSED": continue
            
            p = trade["profit"]
            strat = trade["strategy"]
            sym = trade["symbol"]
            
            # 全体集計
            stats["total"]["profit"] += p
            if p > 0:
                stats["total"]["wins"] += 1
                if strat in stats: stats[strat]["wins"] += 1
            else:
                stats["total"]["losses"] += 1
                if strat in stats: stats[strat]["losses"] += 1

            # シンボル別集計
            if sym in stats:
                stats[sym]["profit"] += p
                if p > 0: stats[sym]["wins"] += 1
                else: stats[sym]["losses"] += 1
            
            # 日次損益計算
            if trade.get("close_time", "").startswith(today_str):
                daily_profit_total += p
                if sym in daily_profit_breakdown:
                    daily_profit_breakdown[sym] += p

        return stats, (daily_profit_total, daily_profit_breakdown)

    def get_win_rate_str(self, stats):
        """勝率表示用文字列を生成"""
        def calc_rate(w, l):
            return f"{w/(w+l)*100:.1f}%" if (w+l) > 0 else "-"

        report = f"📊 **勝率レポート**\n・全体: {calc_rate(stats['total']['wins'], stats['total']['losses'])} ({stats['total']['wins']}勝{stats['total']['losses']}敗)\n"
        
        # シンボル別表示
        for sym in SYMBOLS_CONFIG.keys():
            s_data = stats.get(sym, {"wins":0, "losses":0})
            if s_data["wins"] + s_data["losses"] > 0:
                report += f"・{sym}: {calc_rate(s_data['wins'], s_data['losses'])}\n"

        return report

    def send_discord(self, message):
        if not WEBHOOK_URL: return
        try:
            requests.post(WEBHOOK_URL, json={"content": message}, timeout=5)
        except Exception as e:
            logging.error(f"Discord通知エラー: {e}")

    def initialize_mt5(self):
        if not mt5.initialize():
            error_msg = f"MT5初期化失敗: {mt5.last_error()}"
            logging.error(error_msg)
            self.send_discord(f"🚨 **クリティカルエラー**\n{error_msg}")
            return False
        
        # 全シンボルの確認と有効化
        enabled_symbols = []
        for sym in SYMBOLS_CONFIG.keys():
            if not mt5.symbol_select(sym, True):
                error_msg = f"シンボル {sym} が見つかりません。"
                logging.error(error_msg)
                self.send_discord(f"⚠️ **設定エラー**\n{error_msg}")
            else:
                enabled_symbols.append(sym)
        
        if not enabled_symbols:
            return False

        account_info = mt5.account_info()
        if account_info:
            self.last_balance = account_info.balance
            # 設定値サマリー
            config_summary = ""
            for sym, cfg in SYMBOLS_CONFIG.items():
                config_summary += f"\n・{sym}: Lot={cfg['LOT']} SL/TP={cfg['SL_POINTS']}/{cfg['TP_POINTS']} RSI={cfg.get('RSI_TREND_BUY',40)}/{cfg.get('RSI_TREND_SELL',60)}"
            
            start_msg = f"✅ **Antigravity Bot v4.2 起動**\n資産: {self.last_balance:,.0f} 円\n監視対象: {', '.join(enabled_symbols)}\n\n💡 設定サマリー:{config_summary}\n🆕 v4.2: 建値移動{CONFIG.get('BE_TRIGGER_RATIO',0.013)*100:.1f}% / 一括TP{CONFIG.get('BASKET_TP_RATIO',0.025)*100:.1f}% / トレイリング"
            
            logging.info(f"\n{start_msg.replace('**', '')}")
            self.send_discord(start_msg)
            self.is_connected = True
            return True
        return False

    def get_data(self, symbol):
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 0, 250)
        if rates is None or len(rates) < 200: return None
        
        df = pd.DataFrame(rates)
        df['time'] = pd.to_datetime(df['time'], unit='s')
        
        # 200 SMA (長期トレンド)
        df['sma200'] = df['close'].rolling(window=200).mean()
        
        # 20 SMA (短期トレンド: フィルタ用)
        df['sma20'] = df['close'].rolling(window=20).mean()
        
        # RSI 14
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        df['rsi'] = 100 - (100 / (1 + rs))

        # ATR 14
        df['tr'] = pd.concat([
            df['high'] - df['low'],
            (df['high'] - df['close'].shift(1)).abs(),
            (df['low'] - df['close'].shift(1)).abs()
        ], axis=1).max(axis=1)
        df['atr'] = df['tr'].rolling(window=14).mean()
        
        # SMA200の傾き (v4.0): 過去20本のSMA200変化率で横ばい判定
        sma200_now = df['sma200'].iloc[-1]
        sma200_prev = df['sma200'].iloc[-20] if len(df) >= 220 else df['sma200'].iloc[-1]
        df['sma200_slope'] = abs(sma200_now - sma200_prev) / sma200_now if sma200_now else 0
        
        return df.iloc[-1]

    def check_trading_conditions(self, symbol, data, config, same_dir_count=0):
        current_price = data['close']
        sma200 = data['sma200']
        sma20 = data['sma20']
        rsi = data['rsi']
        atr = data['atr']
        sma200_slope = data.get('sma200_slope', 0)  # v4.0
        
        is_uptrend = current_price > sma200
        is_downtrend = current_price < sma200
        
        ma_distance = abs(current_price - sma200)

        # ─── レンジ判定の厳格化 (v4.0) ─────────────────────────
        # 条件: SMA200付近 かつ ATRが低い かつ SMA200が横ばい
        is_near_sma200 = ma_distance < (current_price * 0.0005)
        is_low_volatility = atr < config.get("RANGE_ATR_MAX", 2.0)  # ATR閾値
        is_sma200_flat = sma200_slope < 0.0003  # SMA200の傾きが小さい
        is_range = is_near_sma200 and is_low_volatility and is_sma200_flat

        signal = "WAIT"
        reason = ""
        strategy = ""

        # RSI閾値 (config.jsonから読み込み)
        rsi_trend_buy = config.get("RSI_TREND_BUY", 43)
        rsi_trend_sell = config.get("RSI_TREND_SELL", 57)
        rsi_range_buy = config.get("RSI_RANGE_BUY", 43)
        rsi_range_sell = config.get("RSI_RANGE_SELL", 57)

        # ナンピン強化 (v3.1): 同方向にポジションがある場合、RSI条件を厳格化
        nanpin_offset = config.get("NANPIN_RSI_OFFSET", 5) * same_dir_count

        # ─── ロジック判定 ────────────────────────────────────
        if is_range:
            if rsi <= rsi_range_buy - nanpin_offset:
                eff_thresh = rsi_range_buy - nanpin_offset
                signal = "BUY"; reason = f"レンジ逆張り (RSI<={eff_thresh:.0f})"; strategy = "Range"
            elif rsi >= rsi_range_sell + nanpin_offset:
                eff_thresh = rsi_range_sell + nanpin_offset
                signal = "SELL"; reason = f"レンジ逆張り (RSI>={eff_thresh:.0f})"; strategy = "Range"
        elif is_uptrend:
            if rsi <= rsi_trend_buy - nanpin_offset:
                eff_thresh = rsi_trend_buy - nanpin_offset
                signal = "BUY"; reason = f"上昇トレンド押し目 (RSI<={eff_thresh:.0f})"; strategy = "Trend"
        elif is_downtrend:
            if rsi >= rsi_trend_sell + nanpin_offset:
                eff_thresh = rsi_trend_sell + nanpin_offset
                signal = "SELL"; reason = f"下降トレンド戻り (RSI>={eff_thresh:.0f})"; strategy = "Trend"

        # ─── トレンドフィルター + 緊急エントリー条項 (v4.0) ───
        if config.get("TREND_FILTER", True) and signal != "WAIT":
            # 緊急エントリー条項: RSIが極端値なら SMA20フィルタを解除
            emergency_buy = rsi <= config.get("EMERGENCY_RSI_BUY", 30)
            emergency_sell = rsi >= config.get("EMERGENCY_RSI_SELL", 70)
            
            if signal == "BUY" and current_price < sma20:
                if emergency_buy:
                    reason += " [緊急エントリー]"
                else:
                    return "WAIT", "トレンドフィルタ(短期下落中によりBUY見送り)", ""
            if signal == "SELL" and current_price > sma20:
                if emergency_sell:
                    reason += " [緊急エントリー]"
                else:
                    return "WAIT", "トレンドフィルタ(短期上昇中によりSELL見送り)", ""

        return signal, reason, strategy

    def check_cooldown(self, symbol, direction, config):
        """連敗時のクールダウン判定 (v4.0改修)"""
        cooldown_min = config.get("COOLDOWN_MINUTES", 0)
        if cooldown_min <= 0: return False

        # 直近の履歴を確認
        symbol_trades = [t for t in self.trades if t.get("symbol") == symbol and t["status"] == "CLOSED"]
        if len(symbol_trades) < 2: return False
        
        # 直近2回が同じ方向かつ負けトレードかチェック
        last1 = symbol_trades[-1]
        last2 = symbol_trades[-2]
        
        if (last1["direction"] == direction and last1["profit"] < 0) and \
           (last2["direction"] == direction and last2["profit"] < 0):
            
            # 最後の損切り決済時間から経過時間を計算
            try:
                close_time = datetime.fromisoformat(last1["close_time"])
            except (ValueError, TypeError):
                return False
            
            elapsed = datetime.now() - close_time
            remaining = timedelta(minutes=cooldown_min) - elapsed
            
            if remaining.total_seconds() > 0:
                # クールダウンキー（同一クールダウン期間を識別）
                cd_key = f"{symbol}_{direction}_{last1.get('ticket', '')}"
                
                # 初回のみ通知・ログ出力
                if not hasattr(self, '_cooldown_notified'):
                    self._cooldown_notified = {}
                
                if cd_key not in self._cooldown_notified:
                    self._cooldown_notified[cd_key] = True
                    remaining_min = int(remaining.total_seconds() // 60)
                    resume_time = (datetime.now() + remaining).strftime("%H:%M")
                    msg = (
                        f"⏸️ **クールダウン発動** ({symbol})\n"
                        f"{direction}方向で2連敗\n"
                        f"クールダウン: {cooldown_min}分\n"
                        f"🕐 トレード復帰予定: {resume_time}"
                    )
                    logging.info(f"{symbol}: {direction}方向2連敗 → {cooldown_min}分クールダウン (復帰: {resume_time})")
                    self.send_discord(msg)
                
                return True
            else:
                # クールダウン終了 → 通知フラグをクリア
                if hasattr(self, '_cooldown_notified'):
                    cd_key = f"{symbol}_{direction}_{last1.get('ticket', '')}"
                    self._cooldown_notified.pop(cd_key, None)
                    
        return False

    def execute_order(self, symbol, direction, reason, strategy, current_price, atr, config):
        # 0. エラー後の待機チェック (v3.1)
        if time.time() < self.error_pause_until.get(symbol, 0):
            return
        # エラー待機期間が終了したらフラグをリセット
        if self.error_notified.get(symbol, False):
            self.error_notified[symbol] = False
            logging.info(f"{symbol}: エラー待機期間終了、注文を再開します")

        # 1. 待機時間チェック
        if time.time() - self.last_trade_time[symbol] < WAIT_SECONDS:
            return

        # 2. 連敗クールダウンチェック (v2.1)
        if self.check_cooldown(symbol, direction, config):
            return

        # 3. ポジション数と間隔チェック
        max_pos = config.get("MAX_POSITIONS", 1)
        min_dist = config.get("MIN_DISTANCE", 0)
        
        positions = mt5.positions_get(symbol=symbol)
        same_dir_count = 0  # 同方向ポジション数 (v3.1)
        if positions:
            pos_count = len(positions)
            if pos_count >= max_pos: return
            
            # 同方向ポジション数をカウント (v3.1)
            for p in positions:
                if (direction == "BUY" and p.type == mt5.POSITION_TYPE_BUY) or \
                   (direction == "SELL" and p.type == mt5.POSITION_TYPE_SELL):
                    same_dir_count += 1
            
            if pos_count > 0:
                last_pos = positions[-1]
                if (direction == "BUY" and last_pos.type == mt5.POSITION_TYPE_BUY) or \
                   (direction == "SELL" and last_pos.type == mt5.POSITION_TYPE_SELL):
                    symbol_info = mt5.symbol_info(symbol)
                    dist = abs(current_price - last_pos.price_open)
                    min_dist_val = min_dist * symbol_info.point
                    if dist < min_dist_val: return

        # 3.5 ナンピン制限 (v4.0改修)
        if same_dir_count > 0:
            # ナンピン最低間隔: 5分（連続ナンピンによる大損防止）
            nanpin_key = f"{symbol}_{direction}"
            nanpin_interval = config.get("NANPIN_INTERVAL_SEC", 300)  # デフォルト5分
            if time.time() - self._last_nanpin_time.get(nanpin_key, 0) < nanpin_interval:
                return  # ナンピン間隔が短すぎる
            
            # RSI再チェック (v3.1)
            data = self.get_data(symbol)
            if data is not None:
                signal, _, _ = self.check_trading_conditions(symbol, data, config, same_dir_count)
                if signal != direction:
                    return  # ナンピン厳格化条件を満たさない

        # 4. ATRフィルタ
        atr_thresh = config.get("ATR_THRESHOLD", 99999.0)
        if atr > atr_thresh:
            return

        tick = mt5.symbol_info_tick(symbol)
        if not tick: return

        price = tick.ask if direction == "BUY" else tick.bid
        symbol_info = mt5.symbol_info(symbol)
        if not symbol_info: return
        point = symbol_info.point
        digits = symbol_info.digits

        sl_p = config["SL_POINTS"]
        tp_p = config["TP_POINTS"]
        lot = config["LOT"]
        
        # SL/TPの正規化
        sl_price = round(price - (sl_p * point) if direction == "BUY" else price + (sl_p * point), digits)
        tp_price = round(price + (tp_p * point) if direction == "BUY" else price - (tp_p * point), digits)

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": lot,
            "type": mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL,
            "price": price,
            "sl": sl_price,
            "tp": tp_price,
            "magic": MAGIC_NUMBER,
            "comment": f"AG:{strategy}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)
        
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            nanpin_label = f" [ナンピン#{same_dir_count+1}]" if same_dir_count > 0 else ""
            log_msg = f"🔔 **エントリー実行 ({symbol}){nanpin_label}**\n戦略: {strategy}\n方向: {direction}\nLot: {lot}\n価格: {price}\nSL: {sl_price} / TP: {tp_price}"
            self.send_discord(log_msg)
            logging.info(f"\n{log_msg}")
            
            self.save_trade(result.order, symbol, strategy, direction, price)
            self.last_trade_time[symbol] = time.time()
            # ナンピン時刻を記録 (v4.0)
            if same_dir_count > 0:
                nanpin_key = f"{symbol}_{direction}"
                self._last_nanpin_time[nanpin_key] = time.time()
        else:
            # エラーコードに応じた理由を特定 (v3.1)
            error_reasons = {
                10004: "リクオート（価格変動が速すぎます）",
                10006: "注文が拒否されました（ブローカー側の制限の可能性）",
                10007: "注文がキャンセルされました",
                10014: "無効なロットサイズです（config.jsonのLOT値を確認してください）",
                10015: "無効なSL/TP価格です（SL_POINTS/TP_POINTSを確認してください）",
                10016: "取引が停止されています（市場クローズまたはメンテナンスの可能性）",
                10019: "証拠金不足です（ロットを下げるか入金が必要です）",
                10027: "MT5の自動売買が無効です。MT5画面の『自動売買』ボタンを有効にしてください",
            }
            reason_text = error_reasons.get(result.retcode, f"不明なエラー（考えられる原因: 接続不良/サーバー障害/ブローカー制限）")
            
            # 1回だけDiscord通知 (v3.1)
            if not self.error_notified.get(symbol, False):
                err_msg = f"🚨 **注文エラー ({symbol})**\nコード: {result.retcode}\n原因: {reason_text}\n\n5分後に再試行します"
                self.send_discord(err_msg)
                self.error_notified[symbol] = True
            
            # 5分間のリトライ停止 (v3.1)
            self.error_pause_until[symbol] = time.time() + 300
            logging.error(f"注文失敗 ({symbol}): {result.retcode} {result.comment} → 5分間停止")

    # ─── v4.2 ポートフォリオ管理 ──────────────────────────────

    def manage_break_even(self, balance):
        """比率ベース建値移動 (v4.2): 含み益が残高の一定%を超えたらSLを建値へ"""
        be_trigger = CONFIG.get("BE_TRIGGER_RATIO", 0.013)
        be_offset = CONFIG.get("BE_OFFSET_RATIO", 0.001)
        trigger_amount = balance * be_trigger
        
        positions = mt5.positions_get()
        if not positions: return
        
        for pos in positions:
            profit = pos.profit + pos.swap
            if profit >= trigger_amount:
                # 建値 + オフセット = 新しいSL
                symbol_info = mt5.symbol_info(pos.symbol)
                if not symbol_info: continue
                point = symbol_info.point
                offset_price = balance * be_offset / (pos.volume * symbol_info.trade_contract_size) if pos.volume > 0 else 0
                
                if pos.type == mt5.POSITION_TYPE_BUY:
                    new_sl = round(pos.price_open + offset_price, symbol_info.digits)
                    if pos.sl >= new_sl: continue  # 既に建値以上
                else:
                    new_sl = round(pos.price_open - offset_price, symbol_info.digits)
                    if pos.sl != 0 and pos.sl <= new_sl: continue
                
                request = {
                    "action": mt5.TRADE_ACTION_SLTP,
                    "position": pos.ticket,
                    "symbol": pos.symbol,
                    "sl": new_sl,
                    "tp": pos.tp,
                }
                result = mt5.order_send(request)
                if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                    msg = f"🔒 **建値移動** (Ticket {pos.ticket})\n{pos.symbol} 含み益: {profit:+,.0f}円\nSL → {new_sl} (建値+α)"
                    self.send_discord(msg)
                    logging.info(f"建値移動: {pos.symbol} Ticket {pos.ticket} → SL={new_sl}")

    def manage_basket_tp(self, balance):
        """比率ベース一括決済 (v4.2): 全ポジ合計が残高の一定%を超えたら全決済"""
        basket_ratio = CONFIG.get("BASKET_TP_RATIO", 0.025)
        target_profit = balance * basket_ratio
        
        positions = mt5.positions_get()
        if not positions or len(positions) < 1: return False
        
        total_profit = sum(p.profit + p.swap for p in positions)
        
        if total_profit >= target_profit:
            # 全ポジション決済
            closed_count = 0
            for pos in positions:
                close_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
                tick = mt5.symbol_info_tick(pos.symbol)
                if not tick: continue
                close_price = tick.bid if pos.type == mt5.POSITION_TYPE_BUY else tick.ask
                
                request = {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "position": pos.ticket,
                    "symbol": pos.symbol,
                    "volume": pos.volume,
                    "type": close_type,
                    "price": close_price,
                    "magic": MAGIC_NUMBER,
                    "comment": "AG:BasketTP",
                    "type_time": mt5.ORDER_TIME_GTC,
                    "type_filling": mt5.ORDER_FILLING_IOC,
                }
                result = mt5.order_send(request)
                if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                    closed_count += 1
            
            if closed_count > 0:
                msg = (
                    f"🎯 **一括利確 (Basket TP)** v4.2\n"
                    f"合計含み益: {total_profit:+,.0f}円 (目標: {target_profit:,.0f}円 = {basket_ratio*100:.1f}%)\n"
                    f"決済: {closed_count}/{len(positions)}ポジション"
                )
                self.send_discord(msg)
                logging.info(f"一括利確: {total_profit:+,.0f}円 ({closed_count}ポジション)")
                return True
        return False

    def manage_trailing_profit(self, balance):
        """比率ベーストレイリング (v4.2): 最大含み益から一定%下落したら決済"""
        trail_trigger = CONFIG.get("TRAIL_TRIGGER_RATIO", 0.018)
        trail_stop = CONFIG.get("TRAIL_STOP_RATIO", 0.005)
        
        positions = mt5.positions_get()
        if not positions: return
        
        for pos in positions:
            profit = pos.profit + pos.swap
            profit_ratio = profit / balance if balance > 0 else 0
            
            # トレイリング状態を管理
            if not hasattr(self, '_trail_max_profit'):
                self._trail_max_profit = {}
            
            key = pos.ticket
            current_max = self._trail_max_profit.get(key, 0)
            
            if profit > current_max:
                self._trail_max_profit[key] = profit
                current_max = profit
            
            max_ratio = current_max / balance if balance > 0 else 0
            
            # トリガー判定: 最大比率が閾値を超えた後、下落量が許容範囲外なら決済
            if max_ratio >= trail_trigger:
                drop = current_max - profit
                drop_limit = balance * trail_stop
                
                if drop >= drop_limit:
                    close_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
                    tick = mt5.symbol_info_tick(pos.symbol)
                    if not tick: continue
                    close_price = tick.bid if pos.type == mt5.POSITION_TYPE_BUY else tick.ask
                    
                    request = {
                        "action": mt5.TRADE_ACTION_DEAL,
                        "position": pos.ticket,
                        "symbol": pos.symbol,
                        "volume": pos.volume,
                        "type": close_type,
                        "price": close_price,
                        "magic": MAGIC_NUMBER,
                        "comment": "AG:Trail",
                        "type_time": mt5.ORDER_TIME_GTC,
                        "type_filling": mt5.ORDER_FILLING_IOC,
                    }
                    result = mt5.order_send(request)
                    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                        msg = (
                            f"📉 **トレイリング決済** (Ticket {pos.ticket})\n"
                            f"最大含み益: {current_max:+,.0f}円 ({max_ratio*100:.1f}%)\n"
                            f"決済時利益: {profit:+,.0f}円\n"
                            f"利益保護: {current_max - profit:,.0f}円の下落を検知"
                        )
                        self.send_discord(msg)
                        logging.info(f"トレイリング決済: Ticket {pos.ticket} 最大{current_max:+,.0f}→{profit:+,.0f}")
                        self._trail_max_profit.pop(key, None)

    def send_daily_report(self):
        now = datetime.now()
        if now.date() > self.last_report_date and now.hour == 0:
            stats, (daily_total, daily_breakdown) = self.calculate_stats()
            win_rate_msg = self.get_win_rate_str(stats)
            
            breakdown_msg = "\n".join([f"・{k}: {v:+,.0f}円" for k, v in daily_breakdown.items()])
             
            report = (
                f"📅 **【日次レポート】** ({now.strftime('%Y-%m-%d')})\n"
                f"💰 本日の合計収益: {daily_total:+,.0f}円\n"
                f"内訳:\n{breakdown_msg}\n\n"
                f"{win_rate_msg}"
            )
            
            self.send_discord(report)
            logging.info(f"日次レポート送信: {daily_total}円")
            self.last_report_date = now.date()

    def run(self):
        if not self.initialize_mt5(): return

        STOP_LOSS_BALANCE = 90000  # 安全装置: この金額を下回ったら強制終了

        logging.info("監視を開始します... (Ctrl+Cで停止)")
        try:
            while True:
                # 0. 安全装置: 資金チェック
                account = mt5.account_info()
                if account:
                    current_equity = account.equity  # 含み損益込みの有効証拠金
                    if current_equity < STOP_LOSS_BALANCE:
                        stop_msg = (
                            f"🚨🚨🚨 **【安全装置発動】自動売買を緊急停止しました** 🚨🚨🚨\n\n"
                            f"💰 現在の有効証拠金: {current_equity:,.0f}円\n"
                            f"⚠️ 停止ライン: {STOP_LOSS_BALANCE:,.0f}円\n"
                            f"📉 不足額: {STOP_LOSS_BALANCE - current_equity:,.0f}円\n\n"
                            f"新規エントリーを停止し、Botを終了します。\n"
                            f"再開するには手動でBotを起動してください。"
                        )
                        logging.critical(f"\n{stop_msg.replace('**', '').replace('🚨', '!')}")
                        self.send_discord(stop_msg)
                        break  # ループを抜けてfinally節でMT5切断

                # 1. オープンポジション監視と決済通知 (v2.2)
                self.monitor_open_trades()
                
                # 1.5 v4.2 ポートフォリオ管理
                current_balance = account.balance if account else self.last_balance
                basket_closed = self.manage_basket_tp(current_balance)
                if not basket_closed:
                    self.manage_break_even(current_balance)
                    self.manage_trailing_profit(current_balance)
                
                # 2. 定時レポートチェック
                self.send_daily_report()

                # 3. 各シンボルごとの監視ループ
                log_line = f"\r[{datetime.now().strftime('%H:%M:%S')}] "
                
                for symbol, config in SYMBOLS_CONFIG.items():
                    if basket_closed: break  # 一括決済直後はエントリーしない
                    data = self.get_data(symbol)
                    if data is not None:
                        signal, reason, strategy = self.check_trading_conditions(symbol, data, config)
                        
                        # ログ用表示（短縮）
                        log_line += f"| {symbol.replace('#','')} P:{data['close']:.1f} Sig:{signal} "

                        if signal != "WAIT":
                            self.execute_order(symbol, signal, reason, strategy, data['close'], data['atr'], config)
                
                print(log_line, end="")
                time.sleep(1)

        except KeyboardInterrupt:
            logging.info("\n停止シグナル検知。終了します。")
        except Exception as e:
            logging.error(f"\n予期せぬエラー: {e}")
        finally:
            mt5.shutdown()
            logging.info("MT5接続を切断しました。")

if __name__ == "__main__":
    bot = AntigravityBot()
    bot.run()
