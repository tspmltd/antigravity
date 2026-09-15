import os
import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
from dotenv import load_dotenv

# カレントディレクトリをパスに追加
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from core.bitflyer_client import BitFlyerClient


def main():
    load_dotenv()
    api_key = os.environ.get("BITFLYER_API_KEY", "")
    api_secret = os.environ.get("BITFLYER_API_SECRET", "")

    print("=" * 60)
    print("         bitFlyer Private API 接続・安全確認テスト         ")
    print("=" * 60)

    client = BitFlyerClient()

    # --- Public API レイテンシ・相場診断 ---
    print("\n[Public API 健全性・レイテンシ診断]")
    try:
        lat = client.measure_api_latency(product_code="FX_BTC_JPY", count=3)
        if lat.get("success"):
            print(f"  ⚡ API往復レイテンシ (RTT): 平均 {lat['avg_ms']:.1f}ms (最小 {lat['min_ms']:.1f}ms / 最大 {lat['max_ms']:.1f}ms)")
        else:
            print("  ⚠️ APIレイテンシ計測に失敗しました。")

        depth = client.diagnose_market_depth(product_code="FX_BTC_JPY")
        print(f"  📊 気配値・スプレッド診断 ({depth['product_code']}):")
        print(f"     • 仲値: {depth['mid_price']:,.0f} 円 | スプレッド: {depth['spread']:,.0f} 円 ({depth['spread_bp']:.2f} bp)")
        print(f"     • 最良買気配: {depth['best_bid']:,.0f} 円 (厚み: {depth['bid_depth']:.3f} BTC)")
        print(f"     • 最良売気配: {depth['best_ask']:,.0f} 円 (厚み: {depth['ask_depth']:.3f} BTC)")
        imb_sign = "BUY優勢" if depth['imbalance_ratio'] > 0.1 else ("SELL優勢" if depth['imbalance_ratio'] < -0.1 else "拮抗")
        print(f"     • 板不均衡比率 (Imbalance): {depth['imbalance_ratio']:+.2f} ({imb_sign})")
    except Exception as e:
        print(f"  ⚠️ Public API 診断エラー: {e}")

    # --- Private API 口座診断 ---
    print("\n[Private API 口座・残高確認]")
    if not api_key or not api_secret or api_key == "your_bitflyer_api_key_here":
        print("\n[INFO] .env ファイルに有効な APIキー がまだ設定されていません。")
        print("以下の手順で設定してください:")
        print("1. プロジェクト直下に '.env' ファイルを作成 (または .env.example をコピー)")
        print("2. BITFLYER_API_KEY=あなたのAPIキー")
        print("3. BITFLYER_API_SECRET=あなたのAPIシークレット")
        print("4. 保存後、再度このスクリプトを実行してください: python scripts/check_bitflyer_account.py\n")
        return

    print(f"\n[1] APIキー検知: {api_key[:6]}...{api_key[-4:]} (シークレット: 設定済み)")
    
    try:
        print("[2] 残高照会 API (/v1/me/getbalance) を呼び出しています...")
        balances = client.get_balance()
        
        print("\n--- 口座残高情報 ---")
        for b in balances:
            currency = b.get("currency_code") or b.get("currency", "UNKNOWN")
            amount = float(b.get("amount") or 0.0)
            available = float(b.get("available") or 0.0)
            if amount > 0:
                print(f"  {currency:6}: 総残高 {amount:14.8f} | 取引可能 {available:14.8f}")

        print("\n[3] 仮想注文 (Dry Run / ペーパートレード) 安全ガードテスト...")
        sim_res = client.send_order(
            product_code="BTC_JPY",
            side="BUY",
            size=0.001,
            order_type="MARKET"
        )
        print(f"  結果: {sim_res.get('status')} - {sim_res.get('message')}")
        print("\n[SUCCESS] bitFlyer Private API クライアントは正常に動作可能です！")

    except Exception as e:
        print(f"\n[ERROR] bitFlyer API接続エラー: {e}")


if __name__ == "__main__":
    main()
