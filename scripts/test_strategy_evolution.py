"""
Strategy Evolution & Hot-Reload 総合検証テスト
==============================================
自律戦略進化＆ホットリロードの全ライフサイクルを検証:
1. strategies/approved/ 内の合格戦略一覧スキャン
2. Discord 分析・重み更新サーバー (#approved-strategies) への採用提案通知
3. 本番ポートフォリオへの昇格 (configs/active_strategy.json)
4. Discord 本番LIVE & 分析サーバーへのホットリロード完了通知
"""
import sys
import os

sys.path.insert(0, "/home/azureuser/antigravity")

from antigravity.quant_pipeline.strategy_evolver import StrategyEvolver


def main():
    print("=" * 70)
    print("   🧬 戦略進化＆無停止ホットリロード (StrategyEvolver) 総合検証テスト   ")
    print("=" * 70)

    evolver = StrategyEvolver()

    # 1. 承認候補戦略スキャン
    print("\n[1/3] 🔍 承認済み候補戦略のスキャン中...")
    strats = evolver.list_approved_strategies()
    print(f"  ➔ 発見された戦略数: {len(strats)} 件")
    for s in strats[:3]:
        print(f"    • {s['name']} | 更新: {s['updated_at']}")

    if not strats:
        print("❌ 検証対象の戦略が存在しません。")
        return

    # 2. Discord 分析サーバーへの採用提案
    print("\n[2/3] 📢 分析・重み更新サーバーへの新戦略提案Embed送信中...")
    success_propose = evolver.propose_latest_strategy()
    print(f"  ➔ 提案Embed送信結果: {'✅ 成功 (HTTP 204)' if success_propose else '❌ 失敗'}")

    # 3. 本番昇格 & ホットリロード通知
    print("\n[3/3] 🏆 本番昇格＆ホットリロード完了通知テスト...")
    target_strat = strats[0]["file_path"]
    success_promote = evolver.promote_strategy(target_strat, author="あかり")
    print(f"  ➔ 昇格処理＆LIVE/分析通知結果: {'✅ 成功 (HTTP 204)' if success_promote else '❌ 失敗'}")

    print("\n" + "=" * 70)
    if success_propose and success_promote:
        print("🎉 【全シナリオ合格】 戦略進化の自動化＆無停止ホットリロードが完全動作しました！")
    else:
        print("⚠️ 一部通知に失敗しました。")
    print("=" * 70)


if __name__ == "__main__":
    main()
