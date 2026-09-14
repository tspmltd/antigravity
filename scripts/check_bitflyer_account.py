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
        client = BitFlyerClient()
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
