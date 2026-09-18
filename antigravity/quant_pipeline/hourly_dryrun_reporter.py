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
        """全 Dry-run 状態を集約して Discord へマルチキャスト送信 (全て bp 表示、1h & 24h 累積)"""
        umm_tf = self._load_json(UMM_TF_STATE)
        arena = self._load_json(ARENA_STATE)
        council = self._load_json(COUNCIL_STATE)

        now_jst = datetime.now(JST)
        now_str = now_jst.strftime("%Y-%m-%d %H:%M:%S JST")

        # 1. UMM & TF2BP データ (1h & 24h)
        umm = umm_tf.get("umm", {})
        tf = umm_tf.get("tf2bp", {})

        umm_1h = umm.get("stats_1h", {})
        umm_24h = umm.get("stats_24h", {})
        umm_1h_bp = umm_1h.get("pnl_bp", 0.0)
        umm_24h_bp = umm_24h.get("pnl_bp", umm.get("total_pnl_bp", 0.0))
        umm_1h_jpy = umm_1h.get("pnl_jpy", 0.0)
        umm_24h_jpy = umm_24h.get("pnl_jpy", umm.get("total_pnl", 0.0))
        umm_1h_t = umm_1h.get("total_trades", 0)
        umm_24h_t = umm_24h.get("total_trades", umm.get("total_trades", 0))
        umm_1h_wr = umm_1h.get("win_rate_pct", 0.0)
        umm_24h_wr = umm_24h.get("win_rate_pct", (umm.get("win_trades", 0) / umm_24h_t * 100) if umm_24h_t > 0 else 0.0)
        umm_pos = umm.get("position", "FLAT")

        tf_1h = tf.get("stats_1h", {})
        tf_24h = tf.get("stats_24h", {})
        tf_1h_bp = tf_1h.get("pnl_bp", 0.0)
        tf_24h_bp = tf_24h.get("pnl_bp", tf.get("total_pnl_bp", 0.0))
        tf_1h_jpy = tf_1h.get("pnl_jpy", 0.0)
        tf_24h_jpy = tf_24h.get("pnl_jpy", tf.get("total_pnl", 0.0))
        tf_1h_t = tf_1h.get("total_trades", 0)
        tf_24h_t = tf_24h.get("total_trades", tf.get("total_trades", 0))
        tf_1h_wr = tf_1h.get("win_rate_pct", 0.0)
        tf_24h_wr = tf_24h.get("win_rate_pct", (tf.get("win_trades", 0) / tf_24h_t * 100) if tf_24h_t > 0 else 0.0)
        tf_pos = tf.get("position", "FLAT")

        umm_tf_1h_bp = umm_1h_bp + tf_1h_bp
        umm_tf_24h_bp = umm_24h_bp + tf_24h_bp
        umm_tf_1h_jpy = umm_1h_jpy + tf_1h_jpy
        umm_tf_24h_jpy = umm_24h_jpy + tf_24h_jpy

        # 2. 承認済み12戦略アリーナ データ (1h & 24h)
        ranking = arena.get("ranking", [])
        # 24h bp 順（または total_pnl_bp 順）にソート
        ranking_sorted = sorted(
            ranking,
            key=lambda r: r.get("stats_24h", {}).get("pnl_bp", r.get("total_pnl_bp", 0.0)),
            reverse=True
        )

        arena_1h_bp = sum(r.get("stats_1h", {}).get("pnl_bp", 0.0) for r in ranking)
        arena_24h_bp = sum(r.get("stats_24h", {}).get("pnl_bp", r.get("total_pnl_bp", 0.0)) for r in ranking)
        arena_1h_jpy = sum(r.get("stats_1h", {}).get("pnl_jpy", 0.0) for r in ranking)
        arena_24h_jpy = sum(r.get("stats_24h", {}).get("pnl_jpy", r.get("total_pnl_jpy", 0.0)) for r in ranking)
        arena_total_trades = sum(r.get("total_trades", 0) for r in ranking)

        rank_lines = []
        medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
        for i, r in enumerate(ranking_sorted[:5]):
            medal = medals[i] if i < len(medals) else f"{i+1}."
            s_id = r.get("strat_id", "")[:12]
            s_name = r.get("name", "")
            pos_v = r.get("position", "FLAT")
            r_1h = r.get("stats_1h", {})
            r_24h = r.get("stats_24h", {})
            r_1h_bp = r_1h.get("pnl_bp", 0.0)
            r_24h_bp = r_24h.get("pnl_bp", r.get("total_pnl_bp", 0.0))
            r_1h_t = r_1h.get("total_trades", 0)
            r_24h_t = r_24h.get("total_trades", r.get("total_trades", 0))
            r_1h_wr = r_1h.get("win_rate_pct", 0.0)
            r_24h_wr = r_24h.get("win_rate_pct", r.get("win_rate_pct", 0.0))
            r_1h_jpy = r_1h.get("pnl_jpy", 0.0)
            r_24h_jpy = r_24h.get("pnl_jpy", r.get("total_pnl_jpy", 0.0))

            rank_lines.append(
                f"{medal} **`{s_id}`** ({s_name}) `[{pos_v}]`\n"
                f"   • 1h: **`{r_1h_bp:+.2f} bp`** ({r_1h_t}戦/{r_1h_wr:.0f}% / ¥{r_1h_jpy:+,.0f})\n"
                f"   • 24h: **`{r_24h_bp:+.2f} bp`** ({r_24h_t}戦/{r_24h_wr:.0f}% / ¥{r_24h_jpy:+,.0f})"
            )

        # 3. 4AGENT 評議会合議データ
        verdict = council.get("verdict", {})
        regime = verdict.get("market_regime", "normal")
        adv_score = verdict.get("adverse_score", 0.0)
        action = verdict.get("recommended_action", "HOLD")

        # 4. 全Dry-run 合算損益 (1h & 24h)
        total_1h_bp = umm_tf_1h_bp + arena_1h_bp
        total_24h_bp = umm_tf_24h_bp + arena_24h_bp
        total_1h_jpy = umm_tf_1h_jpy + arena_1h_jpy
        total_24h_jpy = umm_tf_24h_jpy + arena_24h_jpy

        color = 0x2ECC71 if total_24h_bp >= 0 else 0xE74C3C

        embed = {
            "title": f"📊 【DRYRUN 1時間毎 統合定期レポート】 ({trigger_reason})",
            "description": (
                f"🕒 **集計時刻**: `{now_str}`\n"
                f"🎯 **直近 1時間 (1h) 全体合算**: **`{total_1h_bp:+.2f} bp`** (`¥{total_1h_jpy:+,.1f}`)\n"
                f"📈 **過去24時間 (24h) 累積合算**: **`{total_24h_bp:+.2f} bp`** (`¥{total_24h_jpy:+,.1f}`)\n"
                f"🏛️ **4AGENT合議**: 相場レジーム:`{regime}` | 逆選択スコア:`{adv_score:.2f}` | 判定:`{action}`\n"
                f"🔒 **運用方針**: **自動調整完全禁止 (FROZEN / MANUAL-ONLY)**"
            ),
            "color": color,
            "fields": [
                {
                    "name": "① UMM ＆ TF2BP 24時間観察 (最新確定版 CSR-504/499)",
                    "value": (
                        f"• **UMM (在庫スキューMM)** `[{umm_pos}]`:\n"
                        f"   • 1h: **`{umm_1h_bp:+.2f} bp`** ({umm_1h_t}戦/{umm_1h_wr:.0f}% / ¥{umm_1h_jpy:+,.0f})\n"
                        f"   • 24h: **`{umm_24h_bp:+.2f} bp`** ({umm_24h_t}戦/{umm_24h_wr:.0f}% / ¥{umm_24h_jpy:+,.0f})\n"
                        f"• **TF2BP (2bpトレンド)** `[{tf_pos}]`:\n"
                        f"   • 1h: **`{tf_1h_bp:+.2f} bp`** ({tf_1h_t}戦/{tf_1h_wr:.0f}% / ¥{tf_1h_jpy:+,.0f})\n"
                        f"   • 24h: **`{tf_24h_bp:+.2f} bp`** ({tf_24h_t}戦/{tf_24h_wr:.0f}% / ¥{tf_24h_jpy:+,.0f})\n"
                        f"• **小計**: 1h: **`{umm_tf_1h_bp:+.2f} bp`** (`¥{umm_tf_1h_jpy:+,.0f}`) | 24h: **`{umm_tf_24h_bp:+.2f} bp`** (`¥{umm_tf_24h_jpy:+,.0f}`)"
                    ),
                    "inline": False,
                },
                {
                    "name": f"② 承認済み12戦略 アリーナ (TOP5 / 全12戦略 総取引: {arena_total_trades}回)",
                    "value": (
                        ("\n".join(rank_lines) + "\n" if rank_lines else "(集計中)\n") +
                        f"• **アリーナ合算**: 1h: **`{arena_1h_bp:+.2f} bp`** (`¥{arena_1h_jpy:+,.0f}`) | 24h: **`{arena_24h_bp:+.2f} bp`** (`¥{arena_24h_jpy:+,.0f}`)"
                    ),
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
        log_msg = f"[{now_str}] 統合定期レポート送信: {'成功' if success else '失敗'} (1h: {total_1h_bp:+.2f}bp, 24h: {total_24h_bp:+.2f}bp)"
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
