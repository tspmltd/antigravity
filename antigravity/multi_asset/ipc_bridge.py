"""
antigravity/multi_asset/ipc_bridge.py
======================================
Python司令塔 (Regime Orchestrator) と Go超高速HFTエンジン (Fusion Engine) を
Unix Domain Socket (UDS) 経由で直結する超低レイテンシIPCブリッジ。

役割:
1. STOP 命令の物理遮断通達 (マイクロ秒でGo発注ゲートを閉鎖)
2. REDUCE_50 / HYBRID / TREND モードの動的切り替え
3. リスクバジェット乗数の即時伝達
4. Goエンジンからの約定テレメトリ・PnL・Hawkes強度の取得
"""

import os
import json
import socket
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger("antigravity.multi_asset.ipc_bridge")

DEFAULT_SOCKET_PATH = "/tmp/antigravity_fusion.sock"


class FusionEngineIPCClient:
    """
    Go Fusion Engine と通信する Unix Domain Socket クライアント
    """

    def __init__(self, socket_path: str = DEFAULT_SOCKET_PATH, timeout: float = 2.0):
        self.socket_path = socket_path
        self.timeout = timeout

    def is_socket_available(self) -> bool:
        """ソケットファイルが存在し、接続可能かを確認"""
        return os.path.exists(self.socket_path)

    def send_command(self, command: str, **kwargs) -> Optional[Dict[str, Any]]:
        """
        Go Fusion EngineへJSONコマンドを送信し、即時応答を受信
        """
        if not self.is_socket_available():
            logger.debug(f"[IPC] Socket file not found: {self.socket_path}")
            return None

        payload = {"command": command}
        payload.update(kwargs)

        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.timeout)
                sock.connect(self.socket_path)

                msg = json.dumps(payload) + "\n"
                sock.sendall(msg.encode("utf-8"))

                # 応答受信
                response_data = b""
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    response_data += chunk
                    if b"\n" in response_data:
                        break

                if not response_data:
                    return None

                line = response_data.decode("utf-8").strip()
                resp = json.loads(line)
                return resp

        except Exception as e:
            logger.warning(f"[IPC] Failed to communicate with Fusion Engine at {self.socket_path}: {e}")
            return None

    def stop(self, reason: str = "MACRO SHOCK CIRCUIT BREAKER") -> bool:
        """物理的遮断 (STOP) を即時発令"""
        resp = self.send_command("STOP", reason=reason)
        if resp and resp.get("status") == "OK":
            logger.info(f"[IPC -> GO] 🛑 Emergency STOP transmitted successfully: {reason}")
            return True
        return False

    def reduce_50(self, reason: str = "WARNING REGIME REDUCE RISK") -> bool:
        """リスク半減 (REDUCE_50) を発令"""
        resp = self.send_command("REDUCE_50", reason=reason, risk_multiplier=0.5)
        if resp and resp.get("status") == "OK":
            logger.info(f"[IPC -> GO] ⚠️ REDUCE_50 transmitted successfully: {reason}")
            return True
        return False

    def resume(self, target_mode: str = "HYBRID", risk_multiplier: float = 1.0, reason: str = "NORMAL") -> bool:
        """取引再開・通常モード復帰を発令"""
        resp = self.send_command("RESUME", target_mode=target_mode, risk_multiplier=risk_multiplier, reason=reason)
        if resp and resp.get("status") == "OK":
            logger.info(f"[IPC -> GO] ✅ RESUME transmitted successfully: mode={target_mode}, risk={risk_multiplier}")
            return True
        return False

    def set_mode(self, target_mode: str, risk_multiplier: float = 1.0, reason: str = "") -> bool:
        """動作モードを明示指定"""
        resp = self.send_command("SET_MODE", target_mode=target_mode, risk_multiplier=risk_multiplier, reason=reason)
        return bool(resp and resp.get("status") == "OK")

    def get_state(self) -> Optional[Dict[str, Any]]:
        """Goエンジンの最新ガバナンス状態およびテレメトリ（PnL・レイテンシ等）を取得"""
        resp = self.send_command("GET_STATE")
        return resp

    def ping(self) -> bool:
        """死活確認"""
        resp = self.send_command("PING")
        return bool(resp and resp.get("status") == "OK")
