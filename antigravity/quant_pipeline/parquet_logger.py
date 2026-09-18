"""
Antigravity Parquet Batch Logger (Zero-Latency I/O)
- メモリキューに蓄積し、別スレッドで 30秒または500件ごとに Parquet ファイルへ一括書き出し
- メインスレッド（執行ループ）を1マイクロ秒もブロックしない非同期設計
- PyArrow による高速列指向圧縮 (Snappy)
"""
import os
import time
import queue
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

import pandas as pd
try:
    import pyarrow as pa
    import pyarrow.parquet as pq
    HAS_PYARROW = True
except ImportError:
    HAS_PYARROW = False


class ParquetBatchLogger:
    def __init__(
        self,
        base_dir: str = "/home/azureuser/antigravity/data/parquet",
        flush_interval_sec: float = 30.0,
        batch_size: int = 500,
    ):
        self.base_dir = os.path.abspath(base_dir)
        self.flush_interval_sec = flush_interval_sec
        self.batch_size = batch_size
        self.queues: Dict[str, queue.Queue] = {
            "orderbook_micro": queue.Queue(),
            "market_snapshot": queue.Queue(),
            "core_event": queue.Queue(),
            "signal_metrics": queue.Queue(),
            "fusion_log": queue.Queue(),
            "execution_log": queue.Queue(),
            "pnl_log": queue.Queue(),
        }
        self.is_running = True
        self.worker = threading.Thread(target=self._flush_loop, daemon=True, name="ParquetBatchWriter")
        self.worker.start()

    def log(self, table_name: str, record: Any):
        """メインスレッドからゼロ遅延でキューに投入"""
        if table_name not in self.queues:
            self.queues[table_name] = queue.Queue()
        data = asdict(record) if hasattr(record, "__dataclass_fields__") else record
        self.queues[table_name].put(data)

    def _flush_loop(self):
        last_flush = time.time()
        while self.is_running:
            time.sleep(1.0)
            now = time.time()
            for table_name, q in list(self.queues.items()):
                if q.qsize() >= self.batch_size or (now - last_flush >= self.flush_interval_sec and not q.empty()):
                    self._flush_table(table_name, q)
            if now - last_flush >= self.flush_interval_sec:
                last_flush = now

    def _flush_table(self, table_name: str, q: queue.Queue):
        records = []
        while not q.empty() and len(records) < 5000:
            try:
                records.append(q.get_nowait())
            except queue.Empty:
                break
        if not records:
            return

        # 日付パーティションディレクトリ: data/parquet/{table}/date=YYYY-MM-DD/
        dt_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        out_dir = os.path.join(self.base_dir, table_name, f"date={dt_str}")
        os.makedirs(out_dir, exist_ok=True)

        file_name = f"part_{int(time.time() * 1000)}.parquet"
        file_path = os.path.join(out_dir, file_name)

        try:
            df = pd.DataFrame(records)
            if HAS_PYARROW:
                table = pa.Table.from_pandas(df)
                pq.write_table(table, file_path, compression="SNAPPY")
            else:
                df.to_parquet(file_path, index=False, compression="snappy")
        except Exception as e:
            # 万一のフォールバック
            fallback_path = file_path.replace(".parquet", ".jsonl")
            try:
                df.to_json(fallback_path, orient="records", lines=True)
            except Exception:
                pass

    def stop(self):
        """終了時の全バッファ強制フラッシュ"""
        self.is_running = False
        for table_name, q in self.queues.items():
            if not q.empty():
                self._flush_table(table_name, q)
