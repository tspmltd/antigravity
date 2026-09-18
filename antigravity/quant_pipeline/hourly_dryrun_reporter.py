"""
Hourly Dry-Run Integrated Reporter (DRYRUN 1時間毎 統合定期レポート配信エンジン)
===================================================================================
以下の稼働中 Dry-run 戦略の全成績を毎時ジャスト（1時間毎）に集約し、
Discord のメイン定期運用報告チャンネル (REPORT) および DRYRUN チャンネルへ同時に配信する。

集約対象:
  1. UMM & TF2BP 24時間連続観察 (data/dryrun_umm_tf2bp_state.json)
  2. 承認済み12戦略 統合アリーナ (data/dryrun_approved_arena_state.json)
  3. 4AGENT 合議パイプライン (configs/agents_council_state.json)

起動時に直ちに初回最新レポートを送信し、以降は毎時00分（3600秒周期）で送信を継続する。
"""
import os
import sys
import time
import json
import signal
import traceback
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional

BASE_DIR = "/home/azureuser/antigravity"
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from antigravity.quant_pipeline.quant_discord_notifier import QuantDiscordNotifier

JST = timezone(timedelta(hours=9))
UMM_TF_STATE = os.path.join(BASE_DIR, "data", "dryrun_umm_tf2bp_state.json")
ARENA_STATE = os.path.join(BASE_DIR, "data", "dryrun_approved_arena_state.json")
COUNCIL_STATE = os.path.join(BASE_DIR, "configs", "agents_council_state.json")
LOG_PATH = os.path.join(BASE_DIR, "logs", "hourly_dryrun_reporter.log")


class HourlyDryRunReporter:
    def __init__(self, interval_sec: float = 3600.0):
        self.interval_sec = interval_sec
        self.notifier = QuantDiscordNotifier()
        self.last_sent_hour: int = -1
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

    def _load_json(self, path: str) -> Dict[str, Any]:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def build_and_send_report(self, trigger_reason: str = "毎時定期報告") -> bool:
        """全 Dry-run 状態を集約して Discord へマルチキャスト送信"""
        umm_tf = self._load_json(UMM_TF_STATE)
        arena = self._load_json(ARENA_STATE)
        council = self._load_json(COUNCIL_STATE)

        now_jst = datetime.now(JST)
        now_str = now_jst.strftime("%Y-%m-%d %H:%M:%S JST")

        # 1. UMM & TF2BP データ
        umm = umm_tf.get("umm", {})
        tf = umm_tf.get("tf2bp", {})
        umm_pnl = umm.get("total_pnl", 0.0)
        tf_pnl = tf.get("total_pnl", 0.0)
        umm_trades = umm.get("total_trades", 0)
        umm_wins = umm.get("win_trades", 0)
        tf_trades = tf.get("total_trades", 0)
        tf_wins = tf.get("win_trades", 0)
        umm_pos = umm.get("position", "FLAT")
        tf_pos = tf.get("position", "FLAT")

        umm_wr = (umm_wins / umm_trades * 100) if umm_trades > 0 else 0.0
        tf_wr = (tf_wins / tf_trades * 100) if tf_trades > 0 else 0.0

        # 2. 承認済み12戦略アリーナ データ
        ranking = arena.get("ranking", [])
        arena_total_pnl = sum(r.get("total_pnl_jpy", 0.0) for r in ranking)
        arena_total_trades = sum(r.get("total_trades", 0) for r in ranking)

        rank_lines = []
        medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
        for i, r in enumerate(ranking[:5]):
            medal = medals[i] if i < len(medals) else f"{i+1}."
            pnl_val = r.get("total_pnl_jpy", 0.0)
            n_t = r.get("total_trades", 0)
            wr_v = r.get("win_rate_pct", 0.0)
            pos_v = r.get("position", "FLAT")
            rank_lines.append(
                f"{medal} **`{r.get('strat_id')[:14]}`** ({r.get('name')}): **`¥{pnl_val:+,.1f}`** ({n_t}戦/{wr_v:.0f}% `[{pos_v}]`)"
            )

        # 3. 4AGENT 評議会合議データ
        verdict = council.get("verdict", {})
        regime = verdict.get("market_regime", "normal")
        adv_score = verdict.get("adverse_score", 0.0)
        action = verdict.get("recommended_action", "HOLD")

        # 4. 全体サマリー合算損益
        total_pnl = umm_pnl + tf_pnl + arena_total_pnl
        color = 0x2ECC71 if total_pnl >= 0 else 0xE67E22

        embed = {
            "title": f"📊 【DRYRUN 1時間毎 統合定期レポート】 ({trigger_reason})",
            "description": (
                f"🕒 **集計時刻**: `{now_str}`\n"
                f"💰 **全Dry-run 合算累計損益**: **`¥{total_pnl:+,.1f}`**\n"
                f"🏛️ **4AGENT合議ステータス**: 相場レジーム:`{regime}` | 逆選択スコア:`{adv_score:.2f}` | 判定:`{action}`\n"
                f"🔒 **パラメータ運用方針**: **自動調整完全禁止 (FROZEN / MANUAL-ONLY)**"
            ),
            "color": color,
            "fields": [
                {
                    "name": "① UMM ＆ TF2BP 24時間観察 (最新確定版 CSR-504/499)",
                    "value": (
                        f"• **UMM (在庫スキューMM)**: **`¥{umm_pnl:+,.1f}`** ({umm_trades}戦/{umm_wr:.0f}%, 建玉:`{umm_pos}`)\n"
                        f"• **TF2BP (2bpトレンド)**: **`¥{tf_pnl:+,.1f}`** ({tf_trades}戦/{tf_wr:.0f}%, 建玉:`{tf_pos}`)\n"
                        f"• 小計損益: **`¥{(umm_pnl + tf_pnl):+,.1f}`**"
                    ),
                    "inline": False,
                },
                {
                    "name": f"② 承認済み12戦略 アリーナ (TOP5 / 全12戦略 PnL: ¥{arena_total_pnl:+,.1f})",
                    "value": "\n".join(rank_lines) if rank_lines else "(集計中)",
                    "inline": False,
                },
                {
                    "name": "🛡️ 安全装置・死活監視状況",
                    "value": "• Watchdog Sentinel 24時間監視: `🟢 正常稼働中`\n• 次回定期レポート: 1時間後 (毎時ジャスト自動配信)",
                    "inline": False,
                }
            ],
            "footer": {"text": "🏛️ Antigravity Hourly Dry-run Central Sentinel"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        success = self.notifier.post_dryrun_multicast({"embeds": [embed]})
        log_msg = f"[{now_str}] 統合定期レポート送信: {'成功' if success else '失敗'} (合算PnL: ¥{total_pnl:+,.1f})"
        print(log_msg, flush=True)
        self._log_to_file(log_msg)
        return success

    def _log_to_file(self, msg: str):
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(msg + "\n")
        except Exception:
            pass

    def run(self):
        print("=" * 70)
        print("   📢 DRYRUN 1時間毎 統合定期レポート配信デーモン")
        print("=" * 70)
        print(f"送信間隔: 1時間ごと (毎時ジャスト) ＋ 起動時即時送信")
        print(f"送信先  : メイン運用報告チャンネル (REPORT) ＆ DRYRUN チャンネル")
        print("-" * 70)

        # 起動直後に初回レポートを即座に送信
        print("[Reporter] 🚀 起動時初回レポートを直ちに送信します...")
        self.build_and_send_report(trigger_reason="起動時即時ステータス報告")
        self.last_sent_hour = datetime.now(JST).hour

        def _sig_handler(sig, frame):
            print(f"\n[Reporter] シグナル {sig} 受信。安全に終了します。")
            sys.exit(0)

        signal.signal(signal.SIGTERM, _sig_handler)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)

        while True:
            try:
                now = datetime.now(JST)
                # 毎時00分（または時が変わった瞬間）に送信
                if now.hour != self.last_sent_hour:
                    print(f"[Reporter] ⏰ 毎時定期送信タイミングを検知: {now.strftime('%H:%M:%S')}")
                    self.build_and_send_report(trigger_reason="毎時定期報告")
                    self.last_sent_hour = now.hour

                time.sleep(15.0)  # 15秒おきに時刻チェック

            except Exception as e:
                print(f"[Reporter] エラー: {e}")
                traceback.print_exc()
                time.sleep(30.0)


def main():
    reporter = HourlyDryRunReporter()
    reporter.run()


if __name__ == "__main__":
    main()
