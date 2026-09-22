import unittest
import os
from unittest.mock import patch, MagicMock
from core.notifier import DiscordNotifier


class TestDiscordNotifier(unittest.TestCase):
    def setUp(self):
        self._mute = patch("core.notifier.discord_muted", return_value=False)
        self._mute.start()
        self.notifier = DiscordNotifier(webhook_url="https://discord.com/api/webhooks/mock_test_12345")

    def tearDown(self):
        self._mute.stop()

    def test_is_enabled(self):
        self.assertTrue(self.notifier.is_enabled())
        empty_notifier = DiscordNotifier(webhook_url="")
        self.assertFalse(empty_notifier.is_enabled())

    @patch("urllib.request.urlopen")
    def test_send_message_success(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.status = 204
        mock_urlopen.return_value.__enter__.return_value = mock_response

        result = self.notifier.send_message("テストメッセージ")
        self.assertTrue(result)
        mock_urlopen.assert_called_once()

    @patch("urllib.request.urlopen")
    def test_send_hourly_report(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.status = 200
        mock_urlopen.return_value.__enter__.return_value = mock_response

        result = self.notifier.send_hourly_report(
            strategy_name="InventorySkewMM",
            symbol="FX_BTC_JPY",
            timeframe="5m",
            current_price=11810000.0,
            position_btc=0.001,
            unrealized_pnl=150.0,
            realized_pnl=320.0,
            total_trades=5,
            win_rate_pct=60.0,
            approved_strategies_count=1,
            pipeline_status="正常稼働中"
        )
        self.assertTrue(result)
        mock_urlopen.assert_called_once()

    @patch("urllib.request.urlopen")
    def test_send_alert(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.status = 204
        mock_urlopen.return_value.__enter__.return_value = mock_response

        result = self.notifier.send_alert(
            title="戦略ホットリロード完了",
            message="新戦略に無停止で差し替えました",
            level="success"
        )
        self.assertTrue(result)

    @patch("urllib.request.urlopen")
    def test_mute_flag_skips_post(self, mock_urlopen):
        self._mute.stop()
        with patch("core.notifier.discord_muted", return_value=True):
            result = self.notifier.send_message("muted")
        self.assertFalse(result)
        mock_urlopen.assert_not_called()
        self._mute = patch("core.notifier.discord_muted", return_value=False)
        self._mute.start()


if __name__ == "__main__":
    unittest.main()
