import MetaTrader5 as mt5
import time
import pandas as pd
import requests
import json
import os
import sys
import subprocess
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
        self.last_trade_time = {}
        self.is_connected = False
        self.trades = self.load_trades()
        self.last_report_date = datetime.now().date() - timedelta(days=1)
        self.error_notified = {}          # シンボルごとのエラー通知済みフラグ (v3.1)
        self.error_pause_until = {}       # シンボルごとのリトライ待機時刻 (v3.1)
        self._cooldown_notified = {}      # クールダウン通知済みフラグ
        self._trail_max_profit = {}       # トレイリング最大含み益 (v4.2)
        self._daily_loss = 0.0            # 当日累計損失 (v5.0)
        self._daily_loss_date = None      # 日次損失リセット用 (v5.0)

        for sym in SYMBOLS_CONFIG.keys():
            self.last_trade_time[sym] = 0.0

    def load_trades(self):
        if not os.path.exists(TRADE_HISTORY_FILE):
            return []
        try:
            with open(TRADE_HISTORY_FILE, 'r', encoding='utf-8') as f:
                trades = json.load(f)
                for t in trades:
                    if "symbol" not in t:
                        t["symbol"] = "GOLD#"
                return trades
        except Exception as e:
            logging.error(f"履歴読み込みエラー: {e}")
            return []

    def save_trade(self, ticket, symbol, strategy, direction, price):
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
        """オープンポジションを監視し、決済されたら即時通知する"""
        if not self.trades: return

        current_positions = mt5.positions_get()
        current_tickets = [p.ticket for p in current_positions] if current_positions else []

        updated = False
        for trade in self.trades:
            if trade["status"] == "OPEN":
                if trade["ticket"] not in current_tickets:
                    deals = mt5.history_deals_get(position=trade["ticket"])

                    if deals:
                        close_deal = None
                        total_profit = 0.0

                        for deal in deals:
                             if deal.entry == mt5.DEAL_ENTRY_OUT or deal.entry == mt5.DEAL_ENTRY_INOUT:
                                 close_deal = deal
                                 total_profit += (deal.profit + deal.swap + deal.commission)

                        if close_deal:
                            trade["status"] = "CLOSED"
                            trade["profit"] = total_profit
                            # v5.0: JST基準で記録（MT5サーバーTZのズレを回避）
                            trade["close_time"] = datetime.now().isoformat()
                            updated = True

                            # 日次損失追跡 (v5.0)
                            if total_profit < 0:
                                self._daily_loss += total_profit

                            self.notify_close(trade)
                            logging.info(f"決済検知: Ticket {trade['ticket']} ({trade['symbol']})")

                            # v5.0: 損失時は即座にクールダウン判定・通知
                            if total_profit < 0:
                                sym = trade.get('symbol', 'GOLD#')
                                cfg = SYMBOLS_CONFIG.get(sym, {})
                                self.check_cooldown(sym, trade['direction'], cfg)

        if updated:
            self._save_file()
            self.sync_to_github()

    def _save_file(self):
        try:
            with open(TRADE_HISTORY_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.trades, f, indent=4, ensure_ascii=False)
        except Exception as e:
            logging.error(f"履歴保存エラー: {e}")

    def sync_to_github(self):
        """トレード履歴をGitHubへ自動プッシュする (v4.5)"""
        try:
            subprocess.run(["git", "add", "trade_history.json"], cwd=BASE_DIR, check=True, timeout=10, capture_output=True)
            subprocess.run(["git", "commit", "-m", f"Auto-update: Trade {datetime.now().strftime('%Y-%m-%d %H:%M')}"], cwd=BASE_DIR, check=True, timeout=10, capture_output=True)
            subprocess.run(["git", "push"], cwd=BASE_DIR, check=True, timeout=30, capture_output=True)
            logging.info("GitHub同期完了 ✅")
        except subprocess.TimeoutExpired:
            logging.warning("GitHub同期: タイムアウト")
        except subprocess.CalledProcessError:
            pass  # 変更なしでcommit失敗は正常
        except Exception as e:
            logging.error(f"GitHub同期エラー: {e}")

    def notify_close(self, trade):
        """決済通知 (本日収益追加)"""
        stats, (daily_total, _) = self.calculate_stats()
        win_rate_msg = self.get_win_rate_str(stats)

        symbol = trade.get("symbol", "GOLD#")
        profit = trade["profit"]
        strategy = trade["strategy"]
        daily_pnl_str = f"💰 本日合計: {daily_total:+,.0f}円"

        if profit > 0:
            msg = f"🎉 **利確決済 ({symbol} / {strategy})** (+{profit:,.0f}円)\n{daily_pnl_str}\n\n{win_rate_msg}"
        else:
            msg = f"💸 **損切り決済 ({symbol} / {strategy})** ({profit:,.0f}円)\n{daily_pnl_str}\n\n{win_rate_msg}"

        self.send_discord(msg)
        logging.info(f"\n{msg}")

    def calculate_stats(self):
        stats = {
            "total": {"wins": 0, "losses": 0, "profit": 0.0},
            "Trend": {"wins": 0, "losses": 0},
            "Range": {"wins": 0, "losses": 0}
        }
        for sym in SYMBOLS_CONFIG.keys():
            stats[sym] = {"profit": 0.0, "wins": 0, "losses": 0}

        today_str = datetime.now().strftime('%Y-%m-%d')
        daily_profit_total = 0.0
        daily_profit_breakdown = {sym: 0.0 for sym in SYMBOLS_CONFIG.keys()}

        for trade in self.trades:
            if trade["status"] != "CLOSED": continue

            p = trade["profit"]
            strat = trade["strategy"]
            sym = trade.get("symbol", "GOLD#")

            stats["total"]["profit"] += p
            if p > 0:
                stats["total"]["wins"] += 1
                if strat in stats: stats[strat]["wins"] += 1
            else:
                stats["total"]["losses"] += 1
                if strat in stats: stats[strat]["losses"] += 1

            if sym in stats:
                stats[sym]["profit"] += p
                if p > 0: stats[sym]["wins"] += 1
                else: stats[sym]["losses"] += 1

            close_time_str = trade.get("close_time", "")
            if close_time_str and close_time_str.startswith(today_str):
                daily_profit_total += p
                if sym in daily_profit_breakdown:
                    daily_profit_breakdown[sym] += p

        return stats, (daily_profit_total, daily_profit_breakdown)

    def get_win_rate_str(self, stats):
        def calc_rate(w, l):
            return f"{w/(w+l)*100:.1f}%" if (w+l) > 0 else "-"

        report = f"📊 **勝率レポート**\n・全体: {calc_rate(stats['total']['wins'], stats['total']['losses'])} ({stats['total']['wins']}勝{stats['total']['losses']}敗)\n"

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

        enabled_symbols = []
        for sym in SYMBOLS_CONFIG.keys():
            if not mt5.symbol_select(sym, True):
                logging.error(f"シンボル {sym} が見つかりません。")
            else:
                enabled_symbols.append(sym)

        if not enabled_symbols:
            return False

        account_info = mt5.account_info()
        if account_info:
            self.last_balance = account_info.balance

            config_summary = ""
            for sym, cfg in SYMBOLS_CONFIG.items():
                config_summary += f"\n・{sym}: Lot={cfg['LOT']} SL/TP={cfg['SL_POINTS']}/{cfg['TP_POINTS']}"

            start_msg = (
                f"✅ **Antigravity Bot v5.0 起動**\n"
                f"資産: {self.last_balance:,.0f} 円\n"
                f"監視対象: {', '.join(enabled_symbols)}\n"
                f"\n💡 設定サマリー:{config_summary}\n"
                f"🆕 v5.0: v2.2ロジック + ポートフォリオ管理 + 日次損失リミット"
            )
            logging.info(f"\n{start_msg.replace('**', '')}")
            self.send_discord(start_msg)
            self.is_connected = True
            return True
        return False

    # ─────────────────────────────────────────────
    # テクニカル分析 (v2.2準拠 = 2/18に71%勝率を出したロジック)
    # ─────────────────────────────────────────────

    def get_data(self, symbol):
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 0, 250)
        if rates is None or len(rates) < 200: return None

        df = pd.DataFrame(rates)
        df['time'] = pd.to_datetime(df['time'], unit='s')

        df['sma200'] = df['close'].rolling(window=200).mean()
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

    def check_trading_conditions(self, symbol, data, config):
        """
        v5.0 トレード判定 — v2.2のシンプルロジックをベースに微調整
        
        Design:
        - RSI 40/60 (Trend) / 30/70 (Range) = 2/18に実績のある閾値
        - レンジ判定 = SMA200との距離 0.05% 以内
        - SMA20フィルター = 短期逆行を防ぐ
        - v4.xの「緊急エントリー」は削除（暴走リスクが高すぎた）
        """
        current_price = data['close']
        sma200 = data['sma200']
        sma20 = data['sma20']
        rsi = data['rsi']

        is_uptrend = current_price > sma200
        is_downtrend = current_price < sma200

        ma_distance = abs(current_price - sma200)
        is_range = ma_distance < (current_price * 0.0005)

        signal = "WAIT"
        reason = ""
        strategy = ""

        # ── ロジック判定 (v2.2準拠 + 微調整) ──
        if is_range:
            # レンジ: 厳しいRSI閾値で逆張り
            if rsi <= 30:
                signal = "BUY"; reason = "レンジ逆張り (RSI <= 30)"; strategy = "Range"
            elif rsi >= 70:
                signal = "SELL"; reason = "レンジ逆張り (RSI >= 70)"; strategy = "Range"
        elif is_uptrend:
            # 上昇トレンド: RSI<=40で押し目買い
            if rsi <= 40:
                signal = "BUY"; reason = "上昇トレンド押し目 (RSI <= 40)"; strategy = "Trend"
        elif is_downtrend:
            # 下降トレンド: RSI>=60で戻り売り
            if rsi >= 60:
                signal = "SELL"; reason = "下降トレンド戻り (RSI >= 60)"; strategy = "Trend"

        # ── SMA20トレンドフィルター ──
        if config.get("TREND_FILTER", True) and signal != "WAIT":
            if signal == "BUY" and current_price < sma20:
                return "WAIT", "トレンドフィルタ(短期下落中によりBUY見送り)", ""
            if signal == "SELL" and current_price > sma20:
                return "WAIT", "トレンドフィルタ(短期上昇中によりSELL見送り)", ""

        return signal, reason, strategy

    # ─────────────────────────────────────────────
    # クールダウン (v5.0: 方向非依存に拡張)
    # ─────────────────────────────────────────────

    def check_cooldown(self, symbol, direction, config):
        """
        v5.0 クールダウン — 方向に関係なく直近2回が負けなら発動
        (往復ビンタ防止: BUY負け→SELL負け でもクールダウン)
        """
        cooldown_min = config.get("COOLDOWN_MINUTES", 0)
        if cooldown_min <= 0: return False

        symbol_trades = [t for t in self.trades if t.get("symbol") == symbol and t["status"] == "CLOSED"]
        if len(symbol_trades) < 2: return False

        last1 = symbol_trades[-1]
        last2 = symbol_trades[-2]

        # v5.0: 方向に関係なく直近2連敗でクールダウン
        if last1["profit"] < 0 and last2["profit"] < 0:
            try:
                close_time = datetime.fromisoformat(last1["close_time"])
            except (ValueError, TypeError):
                return False

            elapsed = datetime.now() - close_time
            remaining = timedelta(minutes=cooldown_min) - elapsed

            if remaining.total_seconds() > 0:
                cd_key = f"{symbol}_{last1.get('ticket', '')}"

                if cd_key not in self._cooldown_notified:
                    self._cooldown_notified[cd_key] = True
                    resume_time = (datetime.now() + remaining).strftime("%H:%M")
                    msg = (
                        f"⏸️ **クールダウン発動** ({symbol})\n"
                        f"直近2連敗 ({last2['direction']}→{last1['direction']})\n"
                        f"🕐 トレード復帰予定: {resume_time}"
                    )
                    logging.info(f"{symbol}: 2連敗クールダウン (復帰: {resume_time})")
                    self.send_discord(msg)

                return True
            else:
                cd_key = f"{symbol}_{last1.get('ticket', '')}"
                self._cooldown_notified.pop(cd_key, None)

        return False

    # ─────────────────────────────────────────────
    # 注文実行 (v5.0: ナンピン制限強化)
    # ─────────────────────────────────────────────

    def execute_order(self, symbol, direction, reason, strategy, current_price, atr, config):
        # 1. 待機時間チェック (v2.2ベース: WAIT_SECONDS=300)
        if time.time() - self.last_trade_time[symbol] < WAIT_SECONDS:
            return

        # 2. 連敗クールダウンチェック
        if self.check_cooldown(symbol, direction, config):
            return

        # 3. 日次損失リミット (v5.0)
        daily_loss_limit = CONFIG.get("DAILY_LOSS_LIMIT_RATIO", 0.03)
        if self.last_balance > 0 and abs(self._daily_loss) >= self.last_balance * daily_loss_limit:
            logging.info(f"日次損失リミット到達: {self._daily_loss:+,.0f}円 (上限: {self.last_balance * daily_loss_limit:,.0f}円)")
            return

        # 4. エラー中の一時停止 (v3.1)
        if symbol in self.error_pause_until:
            if time.time() < self.error_pause_until[symbol]:
                return
            else:
                del self.error_pause_until[symbol]
                self.error_notified.pop(symbol, None)

        # 5. ポジション数と間隔チェック
        max_pos = config.get("MAX_POSITIONS", 2)
        min_dist = config.get("MIN_DISTANCE", 0)

        positions = mt5.positions_get(symbol=symbol)
        same_dir_count = 0

        if positions:
            pos_count = len(positions)
            if pos_count >= max_pos: return

            for pos in positions:
                if (direction == "BUY" and pos.type == mt5.POSITION_TYPE_BUY) or \
                   (direction == "SELL" and pos.type == mt5.POSITION_TYPE_SELL):
                    same_dir_count += 1

            if pos_count > 0:
                last_pos = positions[-1]
                if (direction == "BUY" and last_pos.type == mt5.POSITION_TYPE_BUY) or \
                   (direction == "SELL" and last_pos.type == mt5.POSITION_TYPE_SELL):
                    symbol_info = mt5.symbol_info(symbol)
                    dist = abs(current_price - last_pos.price_open)
                    min_dist_val = min_dist * symbol_info.point
                    if dist < min_dist_val: return

        # 6. ATRフィルタ
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

        if result is None:
            logging.error(f"注文送信失敗 ({symbol}): result is None")
            return

        if result.retcode == mt5.TRADE_RETCODE_DONE:
            nanpin_label = f" [ナンピン#{same_dir_count+1}]" if same_dir_count > 0 else ""
            log_msg = f"🔔 **エントリー実行 ({symbol}){nanpin_label}**\n戦略: {strategy}\n方向: {direction}\nLot: {lot}\n価格: {price}\nSL: {sl_price} / TP: {tp_price}"
            self.send_discord(log_msg)
            logging.info(f"\n{log_msg}")

            self.save_trade(result.order, symbol, strategy, direction, price)
            self.last_trade_time[symbol] = time.time()
        else:
            error_reasons = {
                10004: "リクオート",
                10006: "注文拒否（ブローカー制限）",
                10014: "無効なロットサイズ",
                10015: "無効なSL/TP",
                10016: "取引停止中（市場クローズ）",
                10019: "証拠金不足",
                10027: "MT5の自動売買が無効",
            }
            reason_text = error_reasons.get(result.retcode, "不明なエラー")

            if symbol not in self.error_notified:
                err_msg = f"⚠️ **注文失敗 ({symbol})**\nコード: {result.retcode}\n原因: {reason_text}"
                self.send_discord(err_msg)
                self.error_notified[symbol] = True

            self.error_pause_until[symbol] = time.time() + 300
            logging.error(f"注文失敗 ({symbol}): {result.retcode} {reason_text} → 5分間停止")

    # ─────────────────────────────────────────────
    # v4.2 ポートフォリオ管理
    # ─────────────────────────────────────────────

    def manage_break_even(self, balance):
        """比率ベース建値移動: 含み益が残高の一定%を超えたらSLを建値へ"""
        be_trigger = CONFIG.get("BE_TRIGGER_RATIO", 0.013)
        be_offset = CONFIG.get("BE_OFFSET_RATIO", 0.001)
        trigger_amount = balance * be_trigger

        positions = mt5.positions_get()
        if not positions: return

        for pos in positions:
            profit = pos.profit + pos.swap
            if profit >= trigger_amount:
                symbol_info = mt5.symbol_info(pos.symbol)
                if not symbol_info: continue
                offset_price = balance * be_offset / (pos.volume * symbol_info.trade_contract_size) if pos.volume > 0 else 0

                if pos.type == mt5.POSITION_TYPE_BUY:
                    new_sl = round(pos.price_open + offset_price, symbol_info.digits)
                    if pos.sl >= new_sl: continue
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
        """比率ベース一括決済: 全ポジ合計が残高の一定%を超えたら全決済"""
        basket_ratio = CONFIG.get("BASKET_TP_RATIO", 0.025)
        target_profit = balance * basket_ratio

        positions = mt5.positions_get()
        if not positions or len(positions) < 1: return False

        total_profit = sum(p.profit + p.swap for p in positions)

        if total_profit >= target_profit:
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
                    f"🎯 **一括利確 (Basket TP)** v5.0\n"
                    f"合計含み益: {total_profit:+,.0f}円 (目標: {target_profit:,.0f}円 = {basket_ratio*100:.1f}%)\n"
                    f"決済: {closed_count}/{len(positions)}ポジション"
                )
                self.send_discord(msg)
                logging.info(f"一括利確: {total_profit:+,.0f}円 ({closed_count}ポジション)")
                return True
        return False

    def manage_trailing_profit(self, balance):
        """比率ベーストレイリング: 最大含み益から一定%下落したら決済"""
        trail_trigger = CONFIG.get("TRAIL_TRIGGER_RATIO", 0.018)
        trail_stop = CONFIG.get("TRAIL_STOP_RATIO", 0.005)

        positions = mt5.positions_get()
        if not positions: return

        for pos in positions:
            profit = pos.profit + pos.swap

            key = pos.ticket
            current_max = self._trail_max_profit.get(key, 0)

            if profit > current_max:
                self._trail_max_profit[key] = profit
                current_max = profit

            max_ratio = current_max / balance if balance > 0 else 0

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
                            f"決済時利益: {profit:+,.0f}円"
                        )
                        self.send_discord(msg)
                        logging.info(f"トレイリング決済: Ticket {pos.ticket}")
                        self._trail_max_profit.pop(key, None)

    # ─────────────────────────────────────────────
    # レポートとメインループ
    # ─────────────────────────────────────────────

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

            # 日次損失リセット (v5.0)
            self._daily_loss = 0.0

    def run(self):
        if not self.initialize_mt5(): return

        STOP_LOSS_BALANCE = 90000

        logging.info("監視を開始します... (Ctrl+Cで停止)")
        try:
            while True:
                # 0. 安全装置: 資金チェック
                account = mt5.account_info()
                if account:
                    current_equity = account.equity
                    if current_equity < STOP_LOSS_BALANCE:
                        stop_msg = (
                            f"🚨🚨🚨 **【安全装置発動】自動売買を緊急停止しました** 🚨🚨🚨\n\n"
                            f"💰 現在の有効証拠金: {current_equity:,.0f}円\n"
                            f"⚠️ 停止ライン: {STOP_LOSS_BALANCE:,.0f}円\n"
                            f"📉 不足額: {STOP_LOSS_BALANCE - current_equity:,.0f}円\n\n"
                            f"新規エントリーを停止します。"
                        )
                        logging.critical(f"\n{stop_msg.replace('**', '')}")
                        self.send_discord(stop_msg)
                        break

                # 0.5 日次損失リセット (日付が変わったら)
                today = datetime.now().date()
                if self._daily_loss_date != today:
                    self._daily_loss = 0.0
                    self._daily_loss_date = today

                # 1. オープンポジション監視と決済通知
                self.monitor_open_trades()

                # 2. ポートフォリオ管理 (v4.2)
                current_balance = account.balance if account else self.last_balance
                basket_closed = self.manage_basket_tp(current_balance)
                if not basket_closed:
                    self.manage_break_even(current_balance)
                    self.manage_trailing_profit(current_balance)

                # 3. 定時レポートチェック
                self.send_daily_report()

                # 4. 各シンボルごとの監視ループ
                log_line = f"\r[{datetime.now().strftime('%H:%M:%S')}] "

                for symbol, config in SYMBOLS_CONFIG.items():
                    if basket_closed: break
                    data = self.get_data(symbol)
                    if data is not None:
                        signal, reason, strategy = self.check_trading_conditions(symbol, data, config)

                        log_line += f"| {symbol.replace('#','')} P:{data['close']:.1f} RSI:{data['rsi']:.0f} Sig:{signal} "

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
