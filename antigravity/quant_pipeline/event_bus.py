"""
Antigravity In-Memory EventBus (Zero-Overhead Pub/Sub)
- エージェント間・特徴量エンジン間の高速マルチキャスト
- 単一プロセス内でのゼロコピー参照渡し（CPU・メモリオーバーヘッド極小）
"""
from typing import Dict, List, Callable, Any


class EventBus:
    def __init__(self):
        self._subscribers: Dict[str, List[Callable[[Any], None]]] = {}

    def subscribe(self, topic: str, callback: Callable[[Any], None]):
        """トピック購読"""
        self._subscribers.setdefault(topic, []).append(callback)

    def publish(self, topic: str, data: Any):
        """トピック配信"""
        if topic in self._subscribers:
            for cb in self._subscribers[topic]:
                try:
                    cb(data)
                except Exception as ex:
                    print(f"[EventBus] Error in callback for '{topic}': {ex}")
