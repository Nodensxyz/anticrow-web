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
                config_summary += f"\n・{sym}: Lot={cfg['LOT']} SL={cfg['SL_POINTS']} TP={cfg['TP_POINTS']} RSI={cfg.get('RSI_TREND_BUY',35)}/{cfg.get('RSI_TREND_SELL',65)}"
            start_msg = f"✅ **Antigravity Bot v3.0 起動**\n資産: {self.last_balance:,.0f} 円\n監視対象: {', '.join(enabled_symbols)}\nモード: 攻め設定{config_summary}"
            logging.info(f"\n{start_msg}")
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
        
        return df.iloc[-1]

    def check_trading_conditions(self, symbol, data, config, same_dir_count=0):
        current_price = data['close']
        sma200 = data['sma200']
        sma20 = data['sma20']
        rsi = data['rsi']
        atr = data['atr']
        
        is_uptrend = current_price > sma200
        is_downtrend = current_price < sma200
        
        ma_distance = abs(current_price - sma200)
        is_range = ma_distance < (current_price * 0.0005) 

        signal = "WAIT"
        reason = ""
        strategy = ""

        # RSI閾値 (config.jsonから読み込み、v3.0)
        rsi_trend_buy = config.get("RSI_TREND_BUY", 35)
        rsi_trend_sell = config.get("RSI_TREND_SELL", 65)
        rsi_range_buy = config.get("RSI_RANGE_BUY", 28)
        rsi_range_sell = config.get("RSI_RANGE_SELL", 72)

        # ナンピン強化 (v3.1): 同方向にポジションがある場合、RSI条件を厳格化
        nanpin_offset = config.get("NANPIN_RSI_OFFSET", 5) * same_dir_count

        # ロジック判定 (ナンピン時はRSIオフセット適用)
        if is_range:
            if rsi <= rsi_range_buy - nanpin_offset:
                eff_thresh = rsi_range_buy - nanpin_offset
                signal = "BUY"; reason = f"レンジ逆張り (RSI <= {eff_thresh})"; strategy = "Range"
            elif rsi >= rsi_range_sell + nanpin_offset:
                eff_thresh = rsi_range_sell + nanpin_offset
                signal = "SELL"; reason = f"レンジ逆張り (RSI >= {eff_thresh})"; strategy = "Range"
        elif is_uptrend:
            if rsi <= rsi_trend_buy - nanpin_offset:
                eff_thresh = rsi_trend_buy - nanpin_offset
                signal = "BUY"; reason = f"上昇トレンド押し目 (RSI <= {eff_thresh})"; strategy = "Trend"
        elif is_downtrend:
            if rsi >= rsi_trend_sell + nanpin_offset:
                eff_thresh = rsi_trend_sell + nanpin_offset
                signal = "SELL"; reason = f"下降トレンド戻り (RSI >= {eff_thresh})"; strategy = "Trend"

        # トレンドフィルター (v2.1)
        if config.get("TREND_FILTER", True) and signal != "WAIT":
             if signal == "BUY" and current_price < sma20:
                 return "WAIT", "トレンドフィルタ(短期下落中によりBUY見送り)", ""
             if signal == "SELL" and current_price > sma20:
                 return "WAIT", "トレンドフィルタ(短期上昇中によりSELL見送り)", ""

        return signal, reason, strategy

    def check_cooldown(self, symbol, direction, config):
        """連敗時のクールダウン判定 (v2.1)"""
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
            
            # 最後の決済時間から経過時間を計算
            close_time = datetime.fromisoformat(last1["close_time"])
            if datetime.now() - close_time < timedelta(minutes=cooldown_min):
                logging.info(f"{symbol}: {direction}方向2連敗中のためクールダウン中 (残り時間あり)")
                return True
                
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

        # 3.5 ナンピンRSI再チェック (v3.1)
        if same_dir_count > 0:
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

        logging.info("監視を開始します... (Ctrl+Cで停止)")
        try:
            while True:
                # 1. オープンポジション監視と決済通知 (v2.2)
                self.monitor_open_trades()
                
                # 2. 定時レポートチェック
                self.send_daily_report()

                # 3. 各シンボルごとの監視ループ
                log_line = f"\r[{datetime.now().strftime('%H:%M:%S')}] "
                
                for symbol, config in SYMBOLS_CONFIG.items():
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
