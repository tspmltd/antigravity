"""
Factor Research Pipeline ①–⑫ + Interaction + Orthogonal Interaction (WIRE=NO)
=============================================================================
CSR-516/517/518/519

① 経済仮説 → … → ⑫ Interaction → Orthogonal Interaction → 人手承認

CSR-518: 単独 spread_bp 採用禁止 · 同族 IX = NEED_MORE
CSR-519: 次フェーズ = Orthogonal Interaction
  spread × {funding, realized_vol, trade_sign_imbalance, queue_position_proxy}

ユーザー評価（研究スコアカード · システム経済PASSではない）:
  単独マイクロ発見段階 · Interaction Alpha 未発見 · Strategy Design NOT READY

経済 PASS/FAIL・LIVE・auto_apply 禁止。VM軽量（parts/rows 上限）。
"""
from __future__ import annotations

import glob
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

JST = timezone(timedelta(hours=9))
BASE = "/home/azureuser/antigravity"
DEFAULT_PARQUET = os.path.join(BASE, "data", "parquet", "orderbook_micro")
DEFAULT_OUT = os.path.join(BASE, "data", "factor_ic_research")
FUNDING_CACHE = os.path.join(DEFAULT_OUT, "funding_cache.jsonl")
BITFLYER_FUNDING_URL = (
    "https://api.bitflyer.com/v1/getfundingrate?product_code=FX_BTC_JPY"
)

PIPELINE_STEPS = [
    "① 経済仮説",
    "② ファクター作成",
    "③ 散布図",
    "④ Rank IC",
    "⑤ ICIR",
    "⑥ Decile",
    "⑦ Rolling IC",
    "⑧ Return Distribution",
    "⑨ Hit Ratio",
    "⑩ Walk Forward",
    "⑪ Regime",
    "⑫ Interaction",
    "⑬ Economic Edge Review",  # CSR-520 · Strategy Design 前の合理性口頭試問
    "⑭ Strategy Design",
    "⑮ Entry Alpha",
    "⑯ Exit Alpha",
    "⑰ Portfolio Backtest",
]

# ① 経済仮説（ピン · 自動書き換え禁止）
ECONOMIC_HYPOTHESES: Dict[str, str] = {
    "imbalance": "買い板優勢(+imb)は短期的に mid 上昇しやすい（在庫・流動性供給の非対称）",
    "micro_dev_bp": "micro_price が mid より上なら買い圧力が価格に織り込まれつつある",
    "taker_aggressiveness": "成行攻撃が強いほど短期ドリフトが同方向に残る",
    "taker_flow_net": "ask削り(+)=買い圧力 → 前向きリターン（逆選択の裏）",
    "depth_imbalance": "全体厚みの買い寄りは支え、売り寄りは下押し",
    "cancel_minus_refill": "取消≫再配置は板が薄くなり逆行しやすい（毒性が高い）",
    "spread_bp": "スプレッド拡大は流動性枯渇・ボラ上昇で短期 mean/momentum が変わる",
    "bid_depth_1": "最良買い厚は下値支え → 上昇バイアス（薄いと崩れやすい）",
    "ask_depth_1": "最良売り厚は上値抑え → 下落バイアス",
    # CSR-520 次検証仮説（人間行動チェーン）
    "mm_inventory_risk": (
        "MM在庫偏重 → スキュー/撤退 → 片側流動性低下 → インパクト増 → 短期価格変動"
        "（予測対象は価格そのものではなく在庫回避行動）"
    ),
    "inventory_proxy": (
        "MM inventory偏り → スプレッド拡大 → 片側ヘッジ → 価格追随"
        "（−累積約定符号 = 吸収在庫の代理）"
    ),
    "liquidity_withdrawal": (
        "受動流動性減少 → 板薄化 → インパクト増加 → 価格変動"
    ),
}

CANDIDATE_FACTORS = list(ECONOMIC_HYPOTHESES.keys())

# 人間行動チェーン（Renaissance对齐 · 価格ではなく行動をモデリング）
BEHAVIORAL_CHAINS: Dict[str, List[str]] = {
    "funding": ["Funding", "ポジション偏重", "強制決済", "価格変動"],
    "oi": ["OI", "レバレッジ蓄積", "清算", "価格変動"],
    "spread": ["Spread", "流動性低下", "注文インパクト増加", "価格変動"],
    "mm_inventory_risk": ["MM inventory偏り", "スプレッド拡大", "片側ヘッジ", "価格追随"],
    "liquidity_withdrawal": ["受動流動性減少", "板薄化", "インパクト増加", "価格変動"],
}

# ユーザー判定ロック（CSR-518）: 単独 spread_bp は採用せず Interaction Test へ昇格
# Fusion 思想: 単独より交互作用が本命
PRIMARY_INTERACTIONS: List[Tuple[str, str]] = [
    ("spread_bp", "imbalance"),
    ("spread_bp", "depth_imbalance"),
]
# CSR-519: Orthogonal Interaction（異なる経済現象）
# CSR-520: MM Inventory Risk 最優先 · Liquidity Withdrawal 次点
ORTHOGONAL_INTERACTIONS: List[Tuple[str, str]] = [
    ("spread_bp", "inventory_proxy"),      # ★ MM Inventory Risk 本命
    ("spread_bp", "queue_position"),       # Liquidity Withdrawal 本命
    ("spread_bp", "funding_rate"),         # 基盤配線済 · n_unique=1 → OX_MISSING
    ("spread_bp", "realized_vol"),
    ("spread_bp", "trade_sign_imbalance"),
]
# 明示プライオリティ（報告・次ステップ用）
PRIORITY_LANES: List[Dict[str, str]] = [
    {
        "lane": "MM_Inventory_Risk",
        "priority": "P0",
        "hypothesis": "MM inventory偏り → スプレッド拡大 → 片側ヘッジ → 価格追随",
        "interaction": "spread_bp×inventory_proxy",
        "expect": "spread単独より情報量増加 → Economic Edge 説明可能に",
        "observe": "spread, microprice, depth skew, trade sign, inventory proxy",
    },
    {
        "lane": "Liquidity_Withdrawal",
        "priority": "P1",
        "hypothesis": "受動流動性減少 → 板薄化 → インパクト増加 → 価格変動",
        "interaction": "spread_bp×queue_position",
        "expect": "spread×queue が spread単独を超える",
        "observe": "spread, book depth, cancel rate, trade intensity, queue position",
        "queue_note": "現在 tip×micro proxy · 本命は実 queue rank（3番目 vs 300番目）",
    },
]
ORTHOGONAL_LEG_HYPOTHESES: Dict[str, str] = {
    "inventory_proxy": (
        "MMが吸収した在庫の代理（−累積約定符号）: 偏り→拡大spread下で片側ヘッジし価格追随を起こす"
    ),
    "funding_rate": "資金調達バイアスは中期キャリー・在庫コストを変え、spread状態下の流動性供給インセンティブを変調する",
    "realized_vol": "実現ボラ上昇は逆選択コストを上げ、広いspread下でのサイズ/スキュー最適点が変わる",
    "trade_sign_imbalance": "約定符号の偏りは板imbalanceと別チャネルの攻撃方向を示す（情報の増分）",
    "queue_position": (
        "本命は実queue rank。現在は tip×micro proxy。"
        "best bid の3番目と300番目は情報内容が全く違う（HFT/MM独立情報源）"
    ),
}
# 直交判定: |corr| with same-family micro factors
ORTHOGONAL_REF_FACTORS = ("imbalance", "depth_imbalance")
ORTHOGONAL_MAX_ABS_CORR = 0.50

INTERACTION_TEST_STEPS = [
    "1. Rolling IC",
    "2. Regime IC",
    "3. Interaction IC",
    "4. ICIR",
    "5. Walk Forward",
    "6. Economic Edge Review",  # CSR-520 · Design 前
    "7. Strategy Design",
]
ORTHOGONAL_TEST_STEPS = [
    "0. Orthogonality (|corr| vs imb/depth)",
    "1. Rolling IC",
    "2. Regime IC",
    "3. Orthogonal Interaction IC",
    "4. ICIR",
    "5. Walk Forward",
    "6. Economic Edge Review",
    "7. Strategy Design",
]
SINGLE_FACTOR_PROMOTE_TO_IX = {"spread_bp"}  # 単独CANDIDATE禁止 → INTERACTION_TEST

# ユーザー評価ロック（CSR-520 · システム経済PASSではない）
USER_FUSION_SCORECARD: Dict[str, str] = {
    "統計学": "PASS",
    "安定性": "PASS",
    "直交性": "未確定",
    "経済合理性": "検証中",
    "採用": "保留",
    "Economic Edge Review": "検証中",
    "Strategy Design": "NOT_READY",
    "phase": "シグナル探索 → エッジ探索（なぜ存在するかの説明）",
    "spread_bp_status": "HAS_CANDIDATE · USEFUL · NOT ADOPTED",
    "achievement": "高ICシグナル発見 → なぜ存在するかを説明する段階へ移行（成果）",
    "summary": (
        "IC追いではなく行動主体の説明。P0=MM Inventory Risk。"
        "Strategy Design は 統計PASS+Orthogonal PASS+Economic Edge PASS の3点セットでのみ解放。"
    ),
}

# Strategy Design 解放条件（ルネサンス对齐 · 3点セット必須）
STRATEGY_DESIGN_UNLOCK: Dict[str, str] = {
    "1_stats": "統計PASS（Rank IC / ICIR / WF / Regime）",
    "2_orthogonal": "Orthogonal PASS（増分情報 · 同族を超える）",
    "3_economic_edge": "Economic Edge PASS（なぜ儲かるかの口頭試問）",
    "rule": "3点すべて PASS のときのみ ⑭ Strategy Design 解放 · どれか欠ければ NOT_READY",
    "current": "NOT_READY（直交未確定 · Economic Edge 検証中）",
}

# 研究優先順位（ユーザー正本 · CSR-520）
RESEARCH_PRIORITIES: List[Dict[str, str]] = [
    {"rank": 1, "item": "funding 履歴蓄積", "why": "n_unique=1 では統計不能 · フィード配線は正資産"},
    {"rank": 2, "item": "true queue_position", "why": "tip×micro proxy → 実 queue rank（3番目 vs 300番目）"},
    {"rank": 3, "item": "MM Inventory Risk 検証", "why": "P0 · spread×inventory_proxy で情報量増→EER説明"},
    {"rank": 4, "item": "Liquidity Withdrawal 検証", "why": "P1 · spread×queue が単独を超えるか"},
    {"rank": 5, "item": "Orthogonal Interaction 再評価", "why": "履歴・真queue後に再判定"},
]

# Renaissance对齐ゲート（ユーザー指定）
RENAISSANCE_ALIGNED_GATES = [
    "⑪ Regime Test",
    "⑫ Interaction Test",
    "⑬ Economic Edge Review",
    "⑭ Strategy Design",
    "⑮ Entry Alpha",
    "⑯ Exit Alpha",
    "⑰ Portfolio Backtest",
]


def _day_key(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), JST).strftime("%Y-%m-%d")


def _spearman(x: np.ndarray, y: np.ndarray) -> float:
    if len(x) < 10:
        return float("nan")
    rx = pd.Series(x).rank(method="average").to_numpy()
    ry = pd.Series(y).rank(method="average").to_numpy()
    if np.std(rx) < 1e-12 or np.std(ry) < 1e-12:
        return float("nan")
    return float(np.corrcoef(rx, ry)[0, 1])


def _round_or_none(v: float, nd: int = 4) -> Optional[float]:
    if v is None or not np.isfinite(v):
        return None
    return round(float(v), nd)


def load_recent_micro(
    parquet_root: str = DEFAULT_PARQUET,
    max_parts: int = 60,
    max_rows: int = 6000,
) -> pd.DataFrame:
    paths = sorted(glob.glob(os.path.join(parquet_root, "date=*", "part_*.parquet")))
    if not paths:
        return pd.DataFrame()
    paths = paths[-max_parts:]
    frames = []
    for p in paths:
        try:
            frames.append(pd.read_parquet(p))
        except Exception:
            continue
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    if "timestamp" in df.columns:
        df = df.sort_values("timestamp").drop_duplicates(subset=["timestamp"], keep="last")
    if len(df) > max_rows:
        idx = np.linspace(0, len(df) - 1, max_rows).astype(int)
        df = df.iloc[idx].reset_index(drop=True)
    else:
        df = df.reset_index(drop=True)
    return df


def fetch_bitflyer_funding_rate(timeout_sec: float = 5.0) -> Optional[Dict[str, Any]]:
    """Public REST 1回 · 研究用 · LIVE配線なし。"""
    try:
        req = urllib.request.Request(
            BITFLYER_FUNDING_URL,
            headers={"User-Agent": "gapcore-factor-ic-research/csr519"},
        )
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
        rate = float(raw.get("current_funding_rate"))
        settle = str(raw.get("next_funding_rate_settledate") or "")
        now_ms = int(time.time() * 1000)
        row = {
            "ts_ms": now_ms,
            "funding_rate": rate,
            "next_settle": settle,
            "source": "bitflyer_public_rest",
            "fetched_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST"),
        }
        return row
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, TypeError, json.JSONDecodeError):
        return None


def append_funding_cache(row: Dict[str, Any], path: str = FUNDING_CACHE) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def load_funding_series(path: str = FUNDING_CACHE, *, fetch_live: bool = True) -> pd.DataFrame:
    """
    funding_cache.jsonl を読み、必要なら公開APIで1点追加。
    Bitflyer funding は ≈8h 刻みのため、as-of forward-fill で十分（軽量）。
    巨大 pulse tape は読まない（VM保護）。
    """
    rows: List[Dict[str, Any]] = []
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            rows = []
    # 直近キャッシュが古い / 無いときだけ live 1回
    need_live = True
    if rows:
        last_ts = float(rows[-1].get("ts_ms") or 0)
        if time.time() * 1000 - last_ts < 30 * 60 * 1000:  # 30分以内なら再利用
            need_live = False
    if fetch_live and need_live:
        live = fetch_bitflyer_funding_rate()
        if live is not None:
            append_funding_cache(live, path)
            rows.append(live)
    if not rows:
        return pd.DataFrame(columns=["ts_ms", "funding_rate"])
    out = pd.DataFrame(rows)
    out["ts_ms"] = pd.to_numeric(out["ts_ms"], errors="coerce")
    out["funding_rate"] = pd.to_numeric(out["funding_rate"], errors="coerce")
    out = out.dropna(subset=["ts_ms", "funding_rate"]).sort_values("ts_ms")
    out = out.drop_duplicates(subset=["ts_ms"], keep="last").reset_index(drop=True)
    return out[["ts_ms", "funding_rate"]]


def attach_funding_rate(df: pd.DataFrame, funding: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """micro timestamp に funding を as-of forward-fill。"""
    out = df.copy()
    if funding is None:
        funding = load_funding_series(fetch_live=True)
    if funding is None or funding.empty or "timestamp" not in out.columns:
        return out  # funding_rate 列なし → OX_MISSING
    ts = out["timestamp"].astype(np.int64).to_numpy()
    f_ts = funding["ts_ms"].astype(np.int64).to_numpy()
    f_rate = funding["funding_rate"].astype(float).to_numpy()
    # as-of: 各 micro ts 以前の最新 funding
    idx = np.searchsorted(f_ts, ts, side="right") - 1
    rates = np.full(len(out), np.nan)
    ok = idx >= 0
    rates[ok] = f_rate[idx[ok]]
    # サンプル先頭が最初の funding より前なら、最初の既知値で埋める（定数脚）
    if not ok.all() and len(f_rate):
        rates[~ok] = f_rate[0]
    out["funding_rate"] = rates
    out["funding_source"] = "bitflyer_cache_asof"
    return out


def build_factors(df: pd.DataFrame) -> pd.DataFrame:
    """② ファクター作成（マイクロ + 直交脚候補）"""
    out = df.copy()
    mid = out["mid_price"].astype(float).replace(0, np.nan)
    spread = (out["best_ask"] - out["best_bid"]).astype(float)
    out["spread_bp"] = (spread / mid * 10000.0).fillna(0.0)
    out["micro_dev_bp"] = (out["micro_dev"].astype(float) / mid * 10000.0).fillna(0.0)
    tb = out["taker_volume_bid"].astype(float).fillna(0.0)
    ta = out["taker_volume_ask"].astype(float).fillna(0.0)
    out["taker_flow_net"] = ta - tb
    bd = out["total_bid_depth"].astype(float).fillna(0.0)
    ad = out["total_ask_depth"].astype(float).fillna(0.0)
    out["depth_imbalance"] = ((bd - ad) / (bd + ad + 1e-9)).fillna(0.0)
    out["cancel_minus_refill"] = (
        out["cancel_rate"].astype(float).fillna(0.0) - out["refill_rate"].astype(float).fillna(0.0)
    )
    out["imbalance"] = out["imbalance"].astype(float).fillna(0.0)
    out["taker_aggressiveness"] = out["taker_aggressiveness"].astype(float).fillna(0.0)
    out["bid_depth_1"] = out["bid_depth_1"].astype(float).fillna(0.0)
    out["ask_depth_1"] = out["ask_depth_1"].astype(float).fillna(0.0)
    # ⑪ regime proxy（ボラ・スプレッド）
    ret1 = mid.pct_change().fillna(0.0) * 10000.0
    out["ret1_bp"] = ret1
    out["realized_vol_proxy"] = ret1.abs().rolling(30, min_periods=5).mean().fillna(0.0)
    out["realized_vol"] = out["realized_vol_proxy"]  # CSR-519 直交脚エイリアス
    # 約定符号インバランス（買い成行+ / 売り成行− · 短窓）
    signed = (ta - tb).to_numpy(dtype=float)
    out["trade_sign_imbalance"] = (
        pd.Series(signed).rolling(20, min_periods=3).mean().fillna(0.0).to_numpy()
    )
    # queue_position: Bitflyerに真の待ち行列無し
    # tip厚み符号 × |micro_dev|/spread （板内の相対位置 proxy）
    tip_b = out["bid_depth_1"].astype(float)
    tip_a = out["ask_depth_1"].astype(float)
    tip_imb = ((tip_b - tip_a) / (tip_b + tip_a + 1e-9)).fillna(0.0)
    half_spread = (spread / 2.0).replace(0, np.nan)
    micro_pen = (out["micro_dev"].astype(float) / (half_spread + 1e-9)).clip(-3, 3).fillna(0.0)
    out["queue_position"] = (tip_imb * (1.0 + micro_pen.abs() * 0.5)).fillna(0.0)
    out["queue_position_proxy"] = out["queue_position"]  # 後方互換エイリアス
    out["queue_source"] = "tip_micro_proxy"
    # CSR-520: MM Inventory Risk / Liquidity Withdrawal 仮説ファクター
    depth_tot = (bd + ad).replace(0, np.nan)
    depth_chg = depth_tot.pct_change().fillna(0.0).clip(-2, 2)
    # inventory_proxy: MMが吸収した在庫の代理 = −累積約定符号フロー
    # （攻撃側が買う→MMは売り在庫を抱えやすい）
    flow = (ta - tb).fillna(0.0)
    out["inventory_proxy"] = (-flow.rolling(40, min_periods=5).sum()).fillna(0.0)
    # 在庫リスク環境（偏り×ボラ）— 補助
    out["mm_inventory_risk"] = (
        out["inventory_proxy"].abs() * (1.0 + out["realized_vol_proxy"])
    ).fillna(0.0)
    # 流動性引き揚げ: 取消−再配置 + 厚み減少
    out["liquidity_withdrawal"] = (
        out["cancel_minus_refill"] + (-depth_chg.clip(upper=0.0))
    ).fillna(0.0)
    # trade intensity（LW観測候補）
    out["trade_intensity"] = (tb + ta).rolling(20, min_periods=3).sum().fillna(0.0)
    # depth skew エイリアス
    out["depth_skew"] = out["depth_imbalance"]
    sp_med = float(out["spread_bp"].median()) if len(out) else 1.0
    vol_med = float(out["realized_vol_proxy"].median()) if len(out) else 1.0
    out["regime"] = "range"
    out.loc[out["realized_vol_proxy"] >= max(vol_med * 1.5, 1e-6), "regime"] = "high_vol"
    out.loc[
        (out["regime"] != "high_vol") & (out["spread_bp"] <= sp_med * 0.8),
        "regime",
    ] = "tight"
    return out


def add_forward_returns(df: pd.DataFrame, horizons_sec: List[float]) -> pd.DataFrame:
    out = df.copy()
    ts = out["timestamp"].astype(np.int64).to_numpy()
    mid = out["mid_price"].astype(float).to_numpy()
    n = len(out)
    for h in horizons_sec:
        h_ms = int(h * 1000)
        fwd = np.full(n, np.nan)
        j = 0
        for i in range(n):
            target = ts[i] + h_ms
            if j < i:
                j = i
            while j + 1 < n and ts[j] < target:
                j += 1
            if j < n and ts[j] >= target and mid[i] > 0:
                fwd[i] = (mid[j] - mid[i]) / mid[i] * 10000.0
        out[f"fwd_bp_{h:g}s"] = fwd
    return out


def decile_stats(factor: np.ndarray, y: np.ndarray, n_bins: int = 10) -> Dict[str, Any]:
    mask = np.isfinite(factor) & np.isfinite(y)
    f = factor[mask]
    yy = y[mask]
    if len(f) < n_bins * 5:
        return {"n": int(len(f)), "deciles": [], "top_minus_bot": float("nan"), "mono_score": float("nan")}
    try:
        cats = pd.qcut(f, n_bins, labels=False, duplicates="drop")
    except ValueError:
        return {"n": int(len(f)), "deciles": [], "top_minus_bot": float("nan"), "mono_score": float("nan")}
    rows = []
    for d in sorted(pd.Series(cats).dropna().unique()):
        m = cats == d
        rows.append({
            "decile": int(d) + 1,
            "n": int(m.sum()),
            "mean_fwd_bp": float(np.mean(yy[m])),
            "mean_factor": float(np.mean(f[m])),
        })
    if len(rows) < 2:
        return {"n": int(len(f)), "deciles": rows, "top_minus_bot": float("nan"), "mono_score": float("nan")}
    top_bot = rows[-1]["mean_fwd_bp"] - rows[0]["mean_fwd_bp"]
    signs = np.sign([rows[i + 1]["mean_fwd_bp"] - rows[i]["mean_fwd_bp"] for i in range(len(rows) - 1)])
    overall = np.sign(top_bot) if top_bot != 0 else 0.0
    mono = float(np.mean(signs == overall)) if overall != 0 and len(signs) else 0.0
    return {"n": int(len(f)), "deciles": rows, "top_minus_bot": float(top_bot), "mono_score": mono}


def rolling_ic(factor: np.ndarray, y: np.ndarray, n_windows: int = 8) -> Dict[str, Any]:
    """⑦ Rolling IC + ⑤ ICIR = mean/std of window ICs"""
    mask = np.isfinite(factor) & np.isfinite(y)
    f = factor[mask]
    yy = y[mask]
    n = len(f)
    if n < 80:
        return {"ics": [], "icir": float("nan"), "mean_ic": float("nan"), "std_ic": float("nan")}
    win = max(40, n // n_windows)
    ics = []
    for start in range(0, n - win + 1, win):
        sl = slice(start, start + win)
        ic = _spearman(f[sl], yy[sl])
        if np.isfinite(ic):
            ics.append(float(ic))
    if len(ics) < 2:
        return {"ics": ics, "icir": float("nan"), "mean_ic": float("nan"), "std_ic": float("nan")}
    arr = np.array(ics)
    mean_ic = float(np.mean(arr))
    std_ic = float(np.std(arr, ddof=1)) if len(arr) > 1 else float("nan")
    icir = mean_ic / std_ic if std_ic and std_ic > 1e-12 else float("nan")
    return {
        "ics": [round(x, 4) for x in ics],
        "mean_ic": round(mean_ic, 4),
        "std_ic": _round_or_none(std_ic, 4),
        "icir": _round_or_none(icir, 4),
        "n_windows": len(ics),
    }


def return_distribution(factor: np.ndarray, y: np.ndarray) -> Dict[str, Any]:
    """⑧ Return Distribution — top/bot tercile fwd stats"""
    mask = np.isfinite(factor) & np.isfinite(y)
    f = factor[mask]
    yy = y[mask]
    if len(f) < 60:
        return {"n": int(len(f))}
    q33, q66 = np.quantile(f, [0.33, 0.66])
    bot = yy[f <= q33]
    mid = yy[(f > q33) & (f < q66)]
    top = yy[f >= q66]

    def _stats(a: np.ndarray) -> Dict[str, Any]:
        if len(a) == 0:
            return {}
        return {
            "n": int(len(a)),
            "mean_bp": round(float(np.mean(a)), 3),
            "std_bp": round(float(np.std(a)), 3),
            "p05_bp": round(float(np.quantile(a, 0.05)), 3),
            "p50_bp": round(float(np.quantile(a, 0.50)), 3),
            "p95_bp": round(float(np.quantile(a, 0.95)), 3),
        }

    return {"n": int(len(f)), "bot": _stats(bot), "mid": _stats(mid), "top": _stats(top)}


def hit_ratio(factor: np.ndarray, y: np.ndarray) -> Dict[str, Any]:
    """⑨ Hit Ratio — sign(factor)==sign(fwd) among |factor|>median"""
    mask = np.isfinite(factor) & np.isfinite(y)
    f = factor[mask]
    yy = y[mask]
    if len(f) < 40:
        return {"n": int(len(f)), "hit_ratio": None}
    thr = np.median(np.abs(f))
    active = np.abs(f) >= max(thr, 1e-12)
    if active.sum() < 20:
        return {"n": int(len(f)), "hit_ratio": None}
    hits = np.sign(f[active]) == np.sign(yy[active])
    # zero fwd neither hit nor miss strictly — count as miss for conservatism
    hits = hits & (yy[active] != 0)
    return {
        "n": int(active.sum()),
        "hit_ratio": round(float(np.mean(hits)), 4),
        "baseline": 0.5,
    }


def walk_forward_ic(factor: np.ndarray, y: np.ndarray, n_folds: int = 3) -> Dict[str, Any]:
    """⑩ Walk Forward — sequential folds, OOS IC each fold"""
    mask = np.isfinite(factor) & np.isfinite(y)
    f = factor[mask]
    yy = y[mask]
    n = len(f)
    if n < 150:
        return {"folds": [], "mean_oos_ic": None, "sign_stable_folds": None}
    fold_size = n // (n_folds + 1)
    folds = []
    for k in range(n_folds):
        is_end = fold_size * (k + 1)
        oos_end = min(n, is_end + fold_size)
        if oos_end - is_end < 40 or is_end < 40:
            continue
        ic_is = _spearman(f[:is_end], yy[:is_end])
        ic_oos = _spearman(f[is_end:oos_end], yy[is_end:oos_end])
        folds.append({
            "fold": k + 1,
            "n_is": int(is_end),
            "n_oos": int(oos_end - is_end),
            "ic_is": _round_or_none(ic_is),
            "ic_oos": _round_or_none(ic_oos),
            "sign_ok": bool(
                np.isfinite(ic_is) and np.isfinite(ic_oos) and np.sign(ic_is) == np.sign(ic_oos) and ic_oos != 0
            ),
        })
    oos_ics = [fl["ic_oos"] for fl in folds if fl["ic_oos"] is not None]
    mean_oos = float(np.mean(oos_ics)) if oos_ics else float("nan")
    sign_ok_rate = float(np.mean([fl["sign_ok"] for fl in folds])) if folds else float("nan")
    return {
        "folds": folds,
        "mean_oos_ic": _round_or_none(mean_oos),
        "sign_stable_folds": _round_or_none(sign_ok_rate, 3),
    }


def regime_ic(factor: np.ndarray, y: np.ndarray, regime: np.ndarray) -> Dict[str, Any]:
    """⑪ Regime-conditioned Rank IC"""
    out: Dict[str, Any] = {}
    for reg in sorted(set(str(r) for r in regime if r is not None and str(r) != "nan")):
        m = (regime.astype(str) == reg) & np.isfinite(factor) & np.isfinite(y)
        if m.sum() < 40:
            out[reg] = {"n": int(m.sum()), "rank_ic": None}
            continue
        out[reg] = {
            "n": int(m.sum()),
            "rank_ic": _round_or_none(_spearman(factor[m], y[m])),
        }
    return out


def interaction_ic(
    df: pd.DataFrame,
    factors: List[str],
    ycol: str,
    top_k: int = 3,
) -> List[Dict[str, Any]]:
    """⑫ Interaction — product of top factors (OOS half)"""
    if len(factors) < 2 or ycol not in df.columns:
        return []
    n = len(df)
    oos = df.iloc[int(n * 0.7) :].copy()
    y = oos[ycol].to_numpy(dtype=float)
    pairs = []
    use = factors[:top_k]
    for i in range(len(use)):
        for j in range(i + 1, len(use)):
            a, b = use[i], use[j]
            if a not in oos.columns or b not in oos.columns:
                continue
            inter = (oos[a].astype(float) * oos[b].astype(float)).to_numpy()
            m = np.isfinite(inter) & np.isfinite(y)
            ic = _spearman(inter[m], y[m])
            pairs.append({
                "interaction": f"{a}×{b}",
                "horizon": ycol,
                "n_oos": int(m.sum()),
                "rank_ic_oos": _round_or_none(ic),
            })
    pairs.sort(key=lambda r: abs(r["rank_ic_oos"] or 0.0), reverse=True)
    return pairs[:8]


def economic_edge_review(
    factor_or_ix: str,
    *,
    chain_key: Optional[str] = None,
    stats_ok: bool = False,
    behavioral_story: Optional[str] = None,
) -> Dict[str, Any]:
    """
    ⑬ Economic Edge Review（CSR-520）
    Strategy Design 前の「なぜ儲かるか」口頭試問。
    統計合格 ≠ 合理性合格。システムは経済PASSを出さない — ユーザー/研究ロックのみ。
    """
    # ユーザー正本: 単独/同族 spread は統計通っても合理性未確定（口頭試問落ち）
    # MM Inventory / LW の直交掛けは「検証中」— エッジ探索レーン
    locked_fail = {
        "spread_bp",
        "spread_bp×imbalance",
        "spread_bp×depth_imbalance",
    }
    edge_probe = {
        "spread_bp×inventory_proxy",
        "spread_bp×queue_position",
    }
    chain = BEHAVIORAL_CHAINS.get(chain_key or "", [])
    if factor_or_ix in edge_probe or factor_or_ix.endswith("×inventory_proxy") or (
        factor_or_ix.endswith("×queue_position")
    ):
        return {
            "gate": "⑬ Economic Edge Review",
            "subject": factor_or_ix,
            "status": "EER_NEED_MORE",
            "stats": "PROBE" if stats_ok else "UNKNOWN",
            "rationality": "検証中",
            "oral_exam": "エッジ探索中",
            "behavioral_chain": chain,
            "behavioral_story": behavioral_story,
            "predicts": "人間行動（MM在庫回避 / 流動性引き揚げ）",
            "reason": (
                "シグナル探索→エッジ探索。spread単独ではなく交互作用で情報量が増えれば "
                "Economic Edge を説明できる可能性。採用は保留。"
            ),
            "wire": "NO",
            "auto_apply": False,
        }
    if factor_or_ix in locked_fail or (
        factor_or_ix.startswith("spread_bp") and "inventory_proxy" not in factor_or_ix
        and "queue_position" not in factor_or_ix
    ):
        return {
            "gate": "⑬ Economic Edge Review",
            "subject": factor_or_ix,
            "status": "EER_FAIL",
            "stats": "PASS" if stats_ok else "UNKNOWN",
            "validation": "PASS" if stats_ok else "UNKNOWN",
            "stability": "PASS" if stats_ok else "UNKNOWN",
            "rationality": "未確定",
            "oral_exam": "落ち",
            "behavioral_chain": chain or BEHAVIORAL_CHAINS.get("spread", []),
            "predicts": "人間行動（流動性供給・回避）であって価格そのものではない — 要証明",
            "reason": (
                "統計学的には候補だが「なぜ儲かるか」の合理性口頭試問未突破。"
                "NOT ADOPTED · Strategy Design 禁止。"
            ),
            "next_hypotheses": ["mm_inventory_risk", "liquidity_withdrawal"],
            "wire": "NO",
            "auto_apply": False,
        }
    if not behavioral_story:
        return {
            "gate": "⑬ Economic Edge Review",
            "subject": factor_or_ix,
            "status": "EER_NEED_MORE",
            "rationality": "未記載",
            "reason": "行動チェーン（仮説）が未接続 — 人間行動の因果を先に書け",
            "behavioral_chain": chain,
            "wire": "NO",
        }
    return {
        "gate": "⑬ Economic Edge Review",
        "subject": factor_or_ix,
        "status": "EER_NEED_MORE",
        "rationality": "レビュー待ち",
        "behavioral_story": behavioral_story,
        "behavioral_chain": chain,
        "reason": "仮説記載あり · 人手 Economic Edge Review 待ち（システムPASS禁止）",
        "wire": "NO",
        "auto_apply": False,
    }


def evaluate_behavior_hypotheses(
    df: pd.DataFrame,
    horizon_sec: float = 30.0,
    is_frac: float = 0.70,
) -> List[Dict[str, Any]]:
    """
    CSR-520 P0/P1: MM Inventory Risk / Liquidity Withdrawal
    期待: spread×inventory_proxy / spread×queue が spread単独を超える
    """
    ycol = f"fwd_bp_{horizon_sec:g}s"
    out: List[Dict[str, Any]] = []
    if "spread_bp" not in df.columns or ycol not in df.columns:
        return [{"status": "MISSING", "reason": "spread or fwd 欠落"}]

    n = len(df)
    split = int(n * is_frac)
    y = df[ycol].to_numpy(dtype=float)
    sp = df["spread_bp"].astype(float).to_numpy()
    mo_sp = np.isfinite(sp[split:]) & np.isfinite(y[split:])
    ic_spread = _spearman(sp[split:][mo_sp], y[split:][mo_sp])

    probes = [
        ("inventory_proxy", "mm_inventory_risk", "MM_Inventory_Risk", "P0"),
        ("queue_position", "liquidity_withdrawal", "Liquidity_Withdrawal", "P1"),
    ]
    for leg, chain_key, lane, pri in probes:
        if leg not in df.columns:
            out.append({"lane": lane, "priority": pri, "status": "MISSING", "leg": leg})
            continue
        xb = df[leg].astype(float).to_numpy()
        prod = sp * xb
        mo = np.isfinite(prod[split:]) & np.isfinite(y[split:])
        ic_ix = _spearman(prod[split:][mo], y[split:][mo])
        mo_leg = np.isfinite(xb[split:]) & np.isfinite(y[split:])
        ic_leg = _spearman(xb[split:][mo_leg], y[split:][mo_leg])
        beats_spread = bool(
            np.isfinite(ic_ix) and np.isfinite(ic_spread) and abs(ic_ix) > abs(ic_spread) + 0.005
        )
        eer = economic_edge_review(
            f"spread_bp×{leg}",
            chain_key=chain_key,
            stats_ok=beats_spread,
            behavioral_story=ECONOMIC_HYPOTHESES.get(leg) or ORTHOGONAL_LEG_HYPOTHESES.get(leg),
        )
        if beats_spread:
            status = "HYP_INFO_GAIN · EER_PENDING"
        elif np.isfinite(ic_ix) and abs(ic_ix) >= 0.03:
            status = "HYP_STATS_OK · no_gain_vs_spread"
        else:
            status = "HYP_NEED_MORE"
        out.append({
            "lane": lane,
            "priority": pri,
            "interaction": f"spread_bp×{leg}",
            "hypothesis": PRIORITY_LANES[0]["hypothesis"] if pri == "P0" else PRIORITY_LANES[1]["hypothesis"],
            "behavioral_chain": BEHAVIORAL_CHAINS.get(chain_key, []),
            "n_oos": int(mo.sum()),
            "rank_ic_spread_oos": _round_or_none(ic_spread),
            "rank_ic_leg_oos": _round_or_none(ic_leg),
            "rank_ic_interaction_oos": _round_or_none(ic_ix),
            "beats_spread_alone": beats_spread,
            "economic_edge_review": eer,
            "status": status,
            "adoption": "NOT_ADOPTED",
            "note": "エッジ探索 · IC追いではない · WIRE=NO",
        })
    return out


def evaluate_interaction_factor(
    df: pd.DataFrame,
    a: str,
    b: str,
    horizon_sec: float = 30.0,
    is_frac: float = 0.70,
) -> Dict[str, Any]:
    """
    Interaction Test 本線（6段）:
    1 Rolling IC · 2 Regime IC · 3 Interaction IC · 4 ICIR · 5 Walk Forward · 6 Strategy Design
    """
    ycol = f"fwd_bp_{horizon_sec:g}s"
    name = f"{a}×{b}"
    if a not in df.columns or b not in df.columns or ycol not in df.columns:
        return {"interaction": name, "status": "MISSING", "horizon_sec": horizon_sec}

    x = (df[a].astype(float) * df[b].astype(float)).to_numpy()
    y = df[ycol].to_numpy(dtype=float)
    n = len(df)
    split = int(n * is_frac)
    m_is = np.isfinite(x[:split]) & np.isfinite(y[:split])
    m_oos = np.isfinite(x[split:]) & np.isfinite(y[split:])

    # 3. Interaction IC (IS/OOS)
    ic_is = _spearman(x[:split][m_is], y[:split][m_is])
    ic_oos = _spearman(x[split:][m_oos], y[split:][m_oos])

    # 1+4. Rolling IC / ICIR
    roll = rolling_ic(x, y, n_windows=8)
    # 2. Regime IC
    reg = regime_ic(x, y, df["regime"].to_numpy())
    # 5. Walk Forward
    wf = walk_forward_ic(x, y, n_folds=3)
    # auxiliaries
    dec = decile_stats(x[split:][m_oos], y[split:][m_oos])
    hit = hit_ratio(x[split:][m_oos], y[split:][m_oos])

    # vs single-leg OOS IC (比較)
    single = {}
    for leg in (a, b):
        xl = df[leg].to_numpy(dtype=float)
        mo = np.isfinite(xl[split:]) & np.isfinite(y[split:])
        single[leg] = _round_or_none(_spearman(xl[split:][mo], y[split:][mo]))

    beats_singles = bool(
        np.isfinite(ic_oos)
        and abs(ic_oos) > abs(single.get(a) or 0.0)
        and abs(ic_oos) > abs(single.get(b) or 0.0)
    )

    icir_v = roll.get("icir")
    wf_sign = wf.get("sign_stable_folds")
    # Interaction gate → ⑬ EER → ⑭ Strategy Design（EER未突破なら Design 禁止）
    status = "IX_NEED_MORE"
    reason = ""
    stats_ok = False
    if int(m_oos.sum()) < 300 or not np.isfinite(ic_oos):
        status, reason = "IX_NEED_MORE", "n不足 or IC nan"
    elif not (
        np.isfinite(ic_is) and np.sign(ic_is) == np.sign(ic_oos) and ic_oos != 0
    ):
        status, reason = "IX_REJECT", f"IS/OOS sign unstable IS={ic_is:.3f} OOS={ic_oos:.3f}"
    elif abs(ic_oos) < 0.03:
        status, reason = "IX_REJECT", f"|IC_oos|={abs(ic_oos):.3f}<0.03"
    elif icir_v is not None and abs(float(icir_v)) < 0.25:
        status, reason = "IX_NEED_MORE", f"|ICIR|={abs(float(icir_v)):.2f}<0.25"
    elif wf_sign is not None and float(wf_sign) < 0.5:
        status, reason = "IX_NEED_MORE", f"WF sign_stable={wf_sign}<0.5"
    elif not beats_singles:
        status, reason = "IX_NEED_MORE", "単独脚を |IC| で超えず（交互作用優位未確認）"
    else:
        stats_ok = True
        status, reason = "IX_STATS_OK", "統計ゲート通過 · Economic Edge Review へ"

    eer = economic_edge_review(
        name,
        chain_key="spread" if a == "spread_bp" else None,
        stats_ok=stats_ok,
        behavioral_story=(
            f"流動性状態({a})×方向圧力({b}): 人間の流動性供給/回避行動の交互作用"
        ),
    )
    if stats_ok and eer.get("status") == "EER_FAIL":
        status, reason = "IX_EER_FAIL", (
            "統計は立つが Economic Edge Review 口頭試問落ち · "
            "合理性未確定 · Strategy Design 禁止 · NOT ADOPTED"
        )
    elif stats_ok and eer.get("status") == "EER_NEED_MORE":
        status, reason = "IX_EER_PENDING", "統計OK · EER 人手レビュー待ち · Design 禁止"
    elif stats_ok and eer.get("status") == "EER_PASS":
        # システムは EER_PASS を自動発行しない想定 — 人手ロック時のみ
        status, reason = "IX_STRATEGY_DESIGN", (
            "EER通過（人手）→ Fusion mode/size 設計へ（ピン変更は別承認 · auto_apply OFF）"
        )

    # ⑭ Strategy Design（EER通過時のみ有効メモ · それ以外は blocked）
    strategy_design = {
        "wire": "NO",
        "enforce": 0,
        "blocked_by_eer": eer.get("status") != "EER_PASS",
        "fusion_role": "mm_mode / size_mult 調停のみ（Trend final_signal 非破壊）",
        "rule_sketch": [
            f"ix = z({a}) * z({b})  （または raw product → 標準化）",
            "wide_spread + adverse_imbalance → inventory_reduce or pause（size↓）",
            "wide_spread + favorable_imbalance → aggressive_mm 許可（幅は人手ピン）",
            "HardStop mid −5bp failsafe 維持 · Adverse は研究メモのみ",
        ],
        "inputs": [a, b, "regime", "inventory_pnl_bp"],
        "outputs": ["mm_mode", "quote_size_mult", "half_spread_mult"],
        "not_allowed": [
            "LIVE配線",
            "approved_weights自動更新",
            "単独spread_bpでの採用",
            "EER未突破でのDesign実行",
        ],
    }

    return {
        "interaction": name,
        "legs": [a, b],
        "horizon_sec": horizon_sec,
        "hypothesis": (
            f"流動性状態({a})×方向圧力({b}): Fusionどおり状態×圧力の交互作用が "
            f"単独スプレッドより予測力を持つ"
        ),
        "n_is": int(m_is.sum()),
        "n_oos": int(m_oos.sum()),
        "steps": {
            "1_rolling_ic": roll,
            "2_regime_ic": reg,
            "3_interaction_ic": {
                "rank_ic_is": _round_or_none(ic_is),
                "rank_ic_oos": _round_or_none(ic_oos),
                "vs_single_oos": single,
                "beats_singles": beats_singles,
            },
            "4_icir": icir_v,
            "5_walk_forward": wf,
            "6_economic_edge_review": eer,
            "7_strategy_design": strategy_design,
        },
        "decile_oos_top_minus_bot_bp": _round_or_none(float(dec.get("top_minus_bot", float("nan"))), 3),
        "hit_ratio_oos": hit,
        "status": status,
        "reason": reason,
    }


def _pearson_abs(a: np.ndarray, b: np.ndarray) -> float:
    m = np.isfinite(a) & np.isfinite(b)
    if int(m.sum()) < 30:
        return float("nan")
    aa, bb = a[m], b[m]
    if np.std(aa) < 1e-12 or np.std(bb) < 1e-12:
        return float("nan")
    return float(abs(np.corrcoef(aa, bb)[0, 1]))


def evaluate_orthogonal_interaction(
    df: pd.DataFrame,
    a: str,
    b: str,
    horizon_sec: float = 30.0,
    is_frac: float = 0.70,
) -> Dict[str, Any]:
    """
    Orthogonal Interaction（CSR-519）:
    0 Orthogonality · 1 Rolling · 2 Regime · 3 OX IC · 4 ICIR · 5 WF · 6 Strategy Design
    同族マイクロ（imb/depth）との |corr| が低い脚のみ「直交」として増分を判定。
    """
    ycol = f"fwd_bp_{horizon_sec:g}s"
    name = f"{a}×{b}"
    hyp = ORTHOGONAL_LEG_HYPOTHESES.get(b, "")
    if b not in df.columns or a not in df.columns:
        return {
            "interaction": name,
            "legs": [a, b],
            "horizon_sec": horizon_sec,
            "hypothesis": hyp,
            "status": "OX_MISSING",
            "reason": (
                f"脚 `{b}` がデータに無い — フィード追加待ち"
                if b not in df.columns
                else f"脚 `{a}` 欠落"
            ),
            "orthogonal": None,
        }
    if ycol not in df.columns:
        return {"interaction": name, "status": "OX_MISSING", "reason": f"{ycol} 欠落"}

    xa = df[a].astype(float).to_numpy()
    xb = df[b].astype(float).to_numpy()
    # 分散不足（例: funding 1点定数）→ 統計不能 = OX_MISSING（ユーザー判定）
    finite_b = xb[np.isfinite(xb)]
    if len(finite_b) < 30 or float(np.std(finite_b)) < 1e-12:
        return {
            "interaction": name,
            "legs": [a, b],
            "horizon_sec": horizon_sec,
            "hypothesis": hyp,
            "status": "OX_MISSING",
            "orthogonal": None,
            "reason": (
                f"統計不能: 脚 `{b}` 分散不足（n_unique≈1）— フィード基盤は配線済、履歴蓄積待ち"
            ),
            "n_unique_b": int(len(np.unique(np.round(finite_b, 12)))),
            "feed_wired": True if b == "funding_rate" else False,
        }
    # 0. Orthogonality vs same-family micro
    corr_vs = {}
    for ref in ORTHOGONAL_REF_FACTORS:
        if ref in df.columns:
            corr_vs[ref] = _round_or_none(_pearson_abs(xb, df[ref].astype(float).to_numpy()))
    max_corr = max((v for v in corr_vs.values() if v is not None), default=None)
    is_orthogonal = bool(max_corr is not None and max_corr < ORTHOGONAL_MAX_ABS_CORR)

    x = xa * xb
    y = df[ycol].to_numpy(dtype=float)
    n = len(df)
    split = int(n * is_frac)
    m_is = np.isfinite(x[:split]) & np.isfinite(y[:split])
    m_oos = np.isfinite(x[split:]) & np.isfinite(y[split:])

    ic_is = _spearman(x[:split][m_is], y[:split][m_is])
    ic_oos = _spearman(x[split:][m_oos], y[split:][m_oos])
    roll = rolling_ic(x, y, n_windows=8)
    reg = regime_ic(x, y, df["regime"].to_numpy())
    wf = walk_forward_ic(x, y, n_folds=3)
    dec = decile_stats(x[split:][m_oos], y[split:][m_oos])
    hit = hit_ratio(x[split:][m_oos], y[split:][m_oos])

    single: Dict[str, Optional[float]] = {}
    for leg, arr in ((a, xa), (b, xb)):
        mo = np.isfinite(arr[split:]) & np.isfinite(y[split:])
        single[leg] = _round_or_none(_spearman(arr[split:][mo], y[split:][mo]))
    # 同族 Interaction との比較（増分情報）
    same_family_ic = {}
    for sf in ("imbalance", "depth_imbalance"):
        if sf in df.columns:
            xp = xa * df[sf].astype(float).to_numpy()
            mo = np.isfinite(xp[split:]) & np.isfinite(y[split:])
            same_family_ic[f"{a}×{sf}"] = _round_or_none(
                _spearman(xp[split:][mo], y[split:][mo])
            )

    beats_singles = bool(
        np.isfinite(ic_oos)
        and abs(ic_oos) > abs(single.get(a) or 0.0)
        and abs(ic_oos) > abs(single.get(b) or 0.0)
    )
    # 同族IXの最良 |IC| を増分で超えるか
    best_sf = max((abs(v) for v in same_family_ic.values() if v is not None), default=0.0)
    beats_same_family = bool(np.isfinite(ic_oos) and abs(ic_oos) > best_sf + 0.01)

    icir_v = roll.get("icir")
    wf_sign = wf.get("sign_stable_folds")

    status = "OX_NEED_MORE"
    reason = ""
    stats_ok = False
    if not is_orthogonal:
        status, reason = "OX_NOT_ORTHOGONAL", (
            f"|corr| vs imb/depth max={max_corr} ≥ {ORTHOGONAL_MAX_ABS_CORR} "
            f"（同族マイクロと分離不足） corr={corr_vs}"
        )
    elif int(m_oos.sum()) < 300 or not np.isfinite(ic_oos):
        status, reason = "OX_NEED_MORE", "n不足 or IC nan"
    elif not (np.isfinite(ic_is) and np.sign(ic_is) == np.sign(ic_oos) and ic_oos != 0):
        status, reason = "OX_REJECT", f"IS/OOS sign unstable IS={ic_is:.3f} OOS={ic_oos:.3f}"
    elif abs(ic_oos) < 0.03:
        status, reason = "OX_REJECT", f"|IC_oos|={abs(ic_oos):.3f}<0.03"
    elif icir_v is not None and abs(float(icir_v)) < 0.25:
        status, reason = "OX_NEED_MORE", f"|ICIR|={abs(float(icir_v)):.2f}<0.25"
    elif wf_sign is not None and float(wf_sign) < 0.5:
        status, reason = "OX_NEED_MORE", f"WF sign_stable={wf_sign}<0.5"
    elif not beats_singles:
        status, reason = "OX_NEED_MORE", "単独脚を |IC| で超えず（増分情報未確認）"
    elif not beats_same_family:
        status, reason = "OX_NEED_MORE", (
            f"同族IX最良 |IC|={best_sf:.3f} を超えず（直交増分未確認）"
        )
    else:
        stats_ok = True
        status, reason = "OX_STATS_OK", "統計ゲート通過 · Economic Edge Review へ"

    chain_key = None
    if b == "inventory_proxy":
        chain_key = "mm_inventory_risk"
    elif b == "queue_position":
        chain_key = "liquidity_withdrawal"
    elif a == "spread_bp":
        chain_key = "spread"
    eer = economic_edge_review(
        name,
        chain_key=chain_key,
        stats_ok=stats_ok,
        behavioral_story=hyp or ORTHOGONAL_LEG_HYPOTHESES.get(b),
    )
    # inventory/queue は spread ロック対象外だが EER_PASS は人手のみ
    if name.startswith("spread_bp×") and b not in ("inventory_proxy", "queue_position"):
        pass  # economic_edge_review already EER_FAIL for spread_bp*
    if stats_ok and eer.get("status") == "EER_FAIL":
        status, reason = "OX_EER_FAIL", (
            "統計は立つが EER 口頭試問落ち · Strategy Design 禁止 · NOT ADOPTED"
        )
    elif stats_ok and eer.get("status") in ("EER_NEED_MORE",):
        # inventory/queue: 統計OKでも EER は人手 — Design 禁止
        if beats_singles:
            status, reason = "OX_EER_PENDING", (
                "spread単独より情報量増の兆し · Economic Edge Review 検証中 · Design 禁止"
            )
        else:
            status, reason = "OX_EER_PENDING", "EER 人手レビュー待ち · Design 禁止"
    elif stats_ok and eer.get("status") == "EER_PASS":
        status, reason = "OX_STRATEGY_DESIGN", "EER通過（人手）→ Strategy Design"

    strategy_design = {
        "wire": "NO",
        "enforce": 0,
        "blocked_by_eer": eer.get("status") != "EER_PASS",
        "fusion_role": "mm_mode / size_mult 調停のみ（Trend final_signal 非破壊）",
        "rule_sketch": [
            f"ox = z({a}) * z({b})",
            f"経済仮説: {hyp}",
            "wide_spread × 行動状態 → size/skew 変調（ピンは人手）",
            "HardStop mid −5bp 維持 · 単独spread採用禁止",
        ],
        "inputs": [a, b, "regime"],
        "outputs": ["mm_mode", "quote_size_mult", "half_spread_mult"],
        "not_allowed": [
            "LIVE配線",
            "approved_weights自動更新",
            "単独spread_bp採用",
            "EER未突破でのDesign実行",
        ],
        "proxy_note": (
            "queue_position は tip×micro proxy · 本命は実 queue rank"
            if b == "queue_position"
            else (
                "inventory_proxy は −累積約定符号 · 真の自社在庫ではない"
                if b == "inventory_proxy"
                else None
            )
        ),
    }

    return {
        "interaction": name,
        "legs": [a, b],
        "family": "orthogonal",
        "lane": (
            "MM_Inventory_Risk"
            if b == "inventory_proxy"
            else ("Liquidity_Withdrawal" if b == "queue_position" else "orthogonal")
        ),
        "horizon_sec": horizon_sec,
        "hypothesis": hyp,
        "n_is": int(m_is.sum()),
        "n_oos": int(m_oos.sum()),
        "orthogonal": is_orthogonal,
        "steps": {
            "0_orthogonality": {
                "corr_abs_vs": corr_vs,
                "max_abs_corr": max_corr,
                "threshold": ORTHOGONAL_MAX_ABS_CORR,
                "is_orthogonal": is_orthogonal,
            },
            "1_rolling_ic": roll,
            "2_regime_ic": reg,
            "3_orthogonal_interaction_ic": {
                "rank_ic_is": _round_or_none(ic_is),
                "rank_ic_oos": _round_or_none(ic_oos),
                "vs_single_oos": single,
                "vs_same_family_ix_oos": same_family_ic,
                "beats_singles": beats_singles,
                "beats_same_family_ix": beats_same_family,
            },
            "4_icir": icir_v,
            "5_walk_forward": wf,
            "6_economic_edge_review": eer,
            "7_strategy_design": strategy_design,
        },
        "decile_oos_top_minus_bot_bp": _round_or_none(float(dec.get("top_minus_bot", float("nan"))), 3),
        "hit_ratio_oos": hit,
        "status": status,
        "reason": reason,
    }


def adoption_gate(
    ic_is: float,
    ic_oos: float,
    n_oos: int,
    top_bot: float,
    mono: float,
    icir: float,
    hit: Optional[float],
    wf_sign: Optional[float],
    *,
    min_n: int = 400,
    min_abs_ic_oos: float = 0.03,
    min_mono: float = 0.55,
    min_icir: float = 0.35,
    min_hit: float = 0.52,
    min_wf_sign: float = 0.66,
) -> Tuple[str, str]:
    if n_oos < min_n or not np.isfinite(ic_oos) or not np.isfinite(ic_is):
        return "NEED_MORE", f"n_oos={n_oos}<{min_n} or IC nan"
    if abs(ic_oos) < min_abs_ic_oos:
        return "REJECT", f"|IC_oos|={abs(ic_oos):.3f}<{min_abs_ic_oos}"
    if np.sign(ic_is) != np.sign(ic_oos) or ic_is == 0 or ic_oos == 0:
        return "REJECT", f"IS/OOS sign unstable (IS={ic_is:.3f} OOS={ic_oos:.3f})"
    if abs(top_bot) < 0.15:
        return "NEED_MORE", f"decile spread {top_bot:.3f}bp 弱すぎ"
    if mono < min_mono:
        return "NEED_MORE", f"decile mono={mono:.2f}<{min_mono}"
    if np.isfinite(icir) and abs(icir) < min_icir:
        return "NEED_MORE", f"|ICIR|={abs(icir):.2f}<{min_icir}"
    if hit is not None and hit < min_hit:
        return "NEED_MORE", f"hit_ratio={hit:.2f}<{min_hit}"
    if wf_sign is not None and wf_sign < min_wf_sign:
        return "NEED_MORE", f"WF sign_stable={wf_sign:.2f}<{min_wf_sign}"
    return "CANDIDATE", (
        f"|IC_oos|={abs(ic_oos):.3f} ICIR={icir if np.isfinite(icir) else None} "
        f"hit={hit} WF={wf_sign} — 人手承認後のみ戦略化（auto_apply OFF）"
    )


def run_factor_ic_research(
    *,
    parquet_root: str = DEFAULT_PARQUET,
    out_root: str = DEFAULT_OUT,
    max_parts: int = 60,
    max_rows: int = 6000,
    horizons_sec: Optional[List[float]] = None,
    is_frac: float = 0.70,
    scatter_n: int = 400,
) -> Dict[str, Any]:
    horizons_sec = horizons_sec or [5.0, 30.0, 60.0]
    os.makedirs(out_root, exist_ok=True)
    daily_dir = os.path.join(out_root, "daily")
    os.makedirs(daily_dir, exist_ok=True)

    raw = load_recent_micro(parquet_root, max_parts=max_parts, max_rows=max_rows)
    if raw.empty or len(raw) < 200:
        report = {
            "date": _day_key(),
            "usable": "NEED_MORE",
            "reason": f"rows={len(raw)}<200",
            "pipeline_steps": PIPELINE_STEPS,
            "wire": "NO",
            "enforce": 0,
            "auto_apply": False,
            "factors": [],
        }
        _persist(out_root, daily_dir, report)
        return report

    # ②
    df = build_factors(raw)
    df = attach_funding_rate(df)  # CSR-519 · Bitflyer public REST cache as-of
    funding_meta = {
        "attached": bool("funding_rate" in df.columns and bool(df["funding_rate"].notna().any())),
        "n_finite": int(df["funding_rate"].notna().sum()) if "funding_rate" in df.columns else 0,
        "n_unique": (
            int(df["funding_rate"].round(12).nunique(dropna=True))
            if "funding_rate" in df.columns
            else 0
        ),
        "source": (
            str(df["funding_source"].iloc[0])
            if "funding_source" in df.columns and len(df)
            else None
        ),
        "cache_path": FUNDING_CACHE,
        "note": "巨大pulse tapeは読まない · 公開API+jsonl cache · n_unique=1 → OX_MISSING（統計不能）",
    }
    df = add_forward_returns(df, horizons_sec)
    n = len(df)
    split = int(n * is_frac)
    is_df = df.iloc[:split]
    oos_df = df.iloc[split:]

    results: List[Dict[str, Any]] = []
    for h in horizons_sec:
        ycol = f"fwd_bp_{h:g}s"
        for fac in CANDIDATE_FACTORS:
            if fac not in df.columns:
                continue
            x_all = df[fac].to_numpy(dtype=float)
            y_all = df[ycol].to_numpy(dtype=float)
            x_is = is_df[fac].to_numpy(dtype=float)
            y_is = is_df[ycol].to_numpy(dtype=float)
            x_oos = oos_df[fac].to_numpy(dtype=float)
            y_oos = oos_df[ycol].to_numpy(dtype=float)
            m_is = np.isfinite(x_is) & np.isfinite(y_is)
            m_oos = np.isfinite(x_oos) & np.isfinite(y_oos)

            # ④ Rank IC
            ic_is = _spearman(x_is[m_is], y_is[m_is])
            ic_oos = _spearman(x_oos[m_oos], y_oos[m_oos])
            # ⑤⑦ Rolling IC / ICIR (full sample light)
            roll = rolling_ic(x_all, y_all, n_windows=8)
            # ⑥ Decile (OOS)
            dec = decile_stats(x_oos[m_oos], y_oos[m_oos])
            # ⑧ Return dist (OOS)
            rdist = return_distribution(x_oos[m_oos], y_oos[m_oos])
            # ⑨ Hit
            hit = hit_ratio(x_oos[m_oos], y_oos[m_oos])
            # ⑩ Walk forward
            wf = walk_forward_ic(x_all, y_all, n_folds=3)
            # ⑪ Regime
            reg = regime_ic(
                x_all,
                y_all,
                df["regime"].to_numpy(),
            )

            icir_v = roll.get("icir")
            hit_v = hit.get("hit_ratio")
            wf_sign = wf.get("sign_stable_folds")
            adopt, reason = adoption_gate(
                ic_is,
                ic_oos,
                int(m_oos.sum()),
                float(dec.get("top_minus_bot") or float("nan")),
                float(dec.get("mono_score") or float("nan")),
                float(icir_v) if icir_v is not None else float("nan"),
                hit_v,
                wf_sign,
            )
            # CSR-518: 単独 spread_bp は採用せず Interaction Test へ昇格
            if adopt == "CANDIDATE" and fac in SINGLE_FACTOR_PROMOTE_TO_IX:
                adopt = "INTERACTION_TEST"
                reason = (
                    f"単独{fac}@{h}s は採用禁止 → Interaction Test 昇格 "
                    f"(spread_bp×imbalance / spread_bp×depth_imbalance · Fusion思想)"
                )
            results.append({
                "factor": fac,
                "horizon_sec": h,
                "hypothesis": ECONOMIC_HYPOTHESES.get(fac, ""),
                "n_is": int(m_is.sum()),
                "n_oos": int(m_oos.sum()),
                "rank_ic_is": _round_or_none(ic_is),
                "rank_ic_oos": _round_or_none(ic_oos),
                "ic_sign_stable": bool(
                    np.isfinite(ic_is) and np.isfinite(ic_oos) and np.sign(ic_is) == np.sign(ic_oos) and ic_oos != 0
                ),
                "icir": icir_v,
                "rolling_ic": roll,
                "decile_oos": dec.get("deciles"),
                "decile_oos_top_minus_bot_bp": _round_or_none(float(dec.get("top_minus_bot", float("nan"))), 3),
                "decile_oos_monotonic_score": _round_or_none(float(dec.get("mono_score", float("nan"))), 3),
                "return_distribution_oos": rdist,
                "hit_ratio_oos": hit,
                "walk_forward": wf,
                "regime_ic": reg,
                "adoption": adopt,
                "reason": reason,
            })

    ranked = sorted(results, key=lambda r: abs(r["rank_ic_oos"] or 0.0), reverse=True)

    # ③ scatter — INTERACTION_TEST / IX 優先
    scatter_payload: Dict[str, Any] = {}
    prefer = [r for r in ranked if r["adoption"] in ("INTERACTION_TEST", "CANDIDATE") and r.get("rank_ic_oos") is not None]
    best = prefer[0] if prefer else (ranked[0] if ranked else None)
    if best:
        fac = best["factor"]
        h = float(best["horizon_sec"])
        ycol = f"fwd_bp_{h:g}s"
        sub = oos_df[[fac, ycol]].dropna()
        if len(sub) > scatter_n:
            sub = sub.sample(scatter_n, random_state=42)
        scatter_payload = {
            "factor": fac,
            "horizon_sec": h,
            "adoption": best["adoption"],
            "rank_ic_oos": best["rank_ic_oos"],
            "x": [round(float(v), 5) for v in sub[fac].tolist()],
            "y_fwd_bp": [round(float(v), 4) for v in sub[ycol].tolist()],
            "n": int(len(sub)),
            "split": "OOS",
        }

    # 本命 Interaction Test（同族マイクロ · 6段）
    interaction_tests = [
        evaluate_interaction_factor(df, a, b, horizon_sec=30.0, is_frac=is_frac)
        for a, b in PRIMARY_INTERACTIONS
    ]
    # CSR-519: Orthogonal Interaction（異なる経済現象）
    orthogonal_tests = [
        evaluate_orthogonal_interaction(df, a, b, horizon_sec=30.0, is_frac=is_frac)
        for a, b in ORTHOGONAL_INTERACTIONS
    ]
    behavior_hypotheses = evaluate_behavior_hypotheses(df, horizon_sec=30.0, is_frac=is_frac)
    # 参考: 汎用ペア一覧
    top_facs = []
    for r in ranked:
        if r["horizon_sec"] == 30.0 and r["factor"] not in top_facs:
            top_facs.append(r["factor"])
        if len(top_facs) >= 4:
            break
    interactions = interaction_ic(df, top_facs, "fwd_bp_30s", top_k=3)

    n_cand = sum(1 for r in results if r["adoption"] == "CANDIDATE")
    n_ix = sum(1 for r in results if r["adoption"] == "INTERACTION_TEST")
    n_rej = sum(1 for r in results if r["adoption"] == "REJECT")
    n_more = sum(1 for r in results if r["adoption"] == "NEED_MORE")
    n_ix_design = sum(1 for t in interaction_tests if t.get("status") == "IX_STRATEGY_DESIGN")
    n_ox_design = sum(1 for t in orthogonal_tests if t.get("status") == "OX_STRATEGY_DESIGN")
    n_ox_more = sum(1 for t in orthogonal_tests if t.get("status") == "OX_NEED_MORE")
    n_ox_miss = sum(1 for t in orthogonal_tests if t.get("status") == "OX_MISSING")
    # CSR-520: ユーザースコアカード正。エッジ探索フェーズ。
    user_sd = USER_FUSION_SCORECARD.get("Strategy Design", "")
    p0 = next((h for h in behavior_hypotheses if h.get("priority") == "P0"), {})
    if user_sd == "NOT_READY":
        if p0.get("beats_spread_alone"):
            portfolio, usable = "EDGE_PROBE_P0_INFO_GAIN", "USEFUL"
        else:
            portfolio, usable = "EDGE_PROBE_MM_INVENTORY", "USEFUL"
    elif n_ox_design >= 1:
        portfolio, usable = "OX_STRATEGY_DESIGN", "USEFUL"
    elif n_ix_design >= 1:
        portfolio, usable = "IX_STRATEGY_DESIGN", "USEFUL"
    elif n_ox_more >= 1 or n_ox_miss >= 1:
        portfolio, usable = "ORTHOGONAL_INTERACTION_TEST", "USEFUL"
    elif n_ix >= 1 or any(t.get("status") == "IX_NEED_MORE" for t in interaction_tests):
        portfolio, usable = "INTERACTION_TEST", "USEFUL"
    elif n_cand >= 1:
        portfolio, usable = "HAS_CANDIDATE", "USEFUL"
    elif n_more >= n_rej:
        portfolio, usable = "NEED_MORE", "NEED_MORE"
    else:
        portfolio, usable = "NO_CANDIDATE", "NOT_USEFUL"

    report = {
        "date": _day_key(),
        "updated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST"),
        "mode": "edge_probe_mm_inventory",
        "csr": "CSR-520",
        "phase": "シグナル探索 → エッジ探索（⑬ Economic Edge Review 正式）",
        "user_fusion_scorecard": USER_FUSION_SCORECARD,
        "renaissance_aligned_gates": RENAISSANCE_ALIGNED_GATES,
        "strategy_design_unlock": STRATEGY_DESIGN_UNLOCK,
        "research_priorities": RESEARCH_PRIORITIES,
        "priority_lanes": PRIORITY_LANES,
        "behavioral_chains": BEHAVIORAL_CHAINS,
        "judgment_lock": {
            "spread_bp": "HAS_CANDIDATE · USEFUL · NOT ADOPTED（EER未突破）",
            "funding_rate": "OX_MISSING when n_unique=1（統計不能）· フィード配線は正資産",
            "queue_position": "proxy中 · 本命は実 queue rank",
            "p0": "MM Inventory Risk · spread×inventory_proxy",
            "p1": "Liquidity Withdrawal · spread×queue_position",
            "strategy_design": "統計PASS + Orthogonal PASS + Economic Edge PASS の3点セットでのみ解放",
            "achievement": "高IC発見 → なぜ存在するかを説明する段階（エッジ探索）",
        },
        "pipeline_steps": PIPELINE_STEPS,
        "interaction_test_steps": INTERACTION_TEST_STEPS,
        "orthogonal_test_steps": ORTHOGONAL_TEST_STEPS,
        "economic_hypotheses": ECONOMIC_HYPOTHESES,
        "orthogonal_leg_hypotheses": ORTHOGONAL_LEG_HYPOTHESES,
        "funding_feed": funding_meta,
        "wire": "NO",
        "enforce": 0,
        "auto_apply": False,
        "usable": usable,
        "portfolio_adoption": portfolio,
        "n_rows": n,
        "n_is": split,
        "n_oos": n - split,
        "max_parts": max_parts,
        "max_rows": max_rows,
        "horizons_sec": horizons_sec,
        "candidate_factors": CANDIDATE_FACTORS,
        "counts": {
            "CANDIDATE": n_cand,
            "INTERACTION_TEST": n_ix,
            "NEED_MORE": n_more,
            "REJECT": n_rej,
            "IX_STRATEGY_DESIGN": n_ix_design,
            "OX_STRATEGY_DESIGN": n_ox_design,
            "OX_NEED_MORE": n_ox_more,
            "OX_MISSING": n_ox_miss,
        },
        "ranked": ranked,
        "scatter_oos": scatter_payload,
        "interaction_tests": interaction_tests,
        "orthogonal_tests": orthogonal_tests,
        "behavior_hypotheses": behavior_hypotheses,
        "interactions": interactions,
        "adoption_policy": {
            "single_spread_bp": "採用禁止 · EER口頭試問未突破",
            "strategy_design_unlock": [
                "統計PASS",
                "Orthogonal PASS",
                "Economic Edge PASS",
            ],
            "economic_edge_review_required_before_strategy_design": True,
            "min_n_oos": 400,
            "min_abs_ic_oos": 0.03,
            "orthogonal_max_abs_corr_vs_imb_depth": ORTHOGONAL_MAX_ABS_CORR,
            "human_approval_required": True,
            "forbidden": [
                "経済PASS/FAIL（システム判定）",
                "LIVE配線",
                "approved_weights自動更新",
                "単独spread_bp採用",
                "ICが高いから採用",
                "EER未突破でのStrategy Design",
                "Orthogonal未PASSでのStrategy Design",
            ],
        },
        "next_steps": _next_steps(ranked, interaction_tests, orthogonal_tests, behavior_hypotheses),
        "note": (
            "エッジ探索段階。P0=MM Inventory Risk。spreadがEconomic Edgeに昇格するには "
            "行動主体の説明が必要。"
        ),
    }
    _persist(out_root, daily_dir, report)
    return report


def _next_steps(
    ranked: List[Dict[str, Any]],
    interaction_tests: List[Dict[str, Any]],
    orthogonal_tests: List[Dict[str, Any]],
    behavior_hypotheses: Optional[List[Dict[str, Any]]] = None,
) -> List[str]:
    out: List[str] = [
        f"{p['rank']}. {p['item']} — {p['why']}" for p in RESEARCH_PRIORITIES
    ]
    out.append(
        "Strategy Design 解放条件: 統計PASS + Orthogonal PASS + Economic Edge PASS（3点セット）"
    )
    for h in behavior_hypotheses or []:
        out.append(
            f"{h.get('priority')} {h.get('lane')}: {h.get('status')} "
            f"ix={h.get('rank_ic_interaction_oos')} vs sp={h.get('rank_ic_spread_oos')} "
            f"beats={h.get('beats_spread_alone')}"
        )
    return out[:12]


def _persist(out_root: str, daily_dir: str, report: Dict[str, Any]) -> None:
    day = report["date"]
    path = os.path.join(daily_dir, f"{day}.json")
    tmp = path + f".{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    state = {
        "updated_at": report.get("updated_at"),
        "today": day,
        "csr": report.get("csr"),
        "mode": report.get("mode"),
        "pipeline_steps": report.get("pipeline_steps"),
        "interaction_test_steps": report.get("interaction_test_steps"),
        "judgment_lock": report.get("judgment_lock"),
        "usable": report.get("usable"),
        "portfolio_adoption": report.get("portfolio_adoption"),
        "counts": report.get("counts"),
        "interaction_tests": [
            {
                "interaction": t.get("interaction"),
                "status": t.get("status"),
                "ic_oos": ((t.get("steps") or {}).get("3_interaction_ic") or {}).get("rank_ic_oos"),
                "icir": ((t.get("steps") or {}).get("4_icir")),
                "reason": t.get("reason"),
            }
            for t in (report.get("interaction_tests") or [])
        ],
        "orthogonal_tests": [
            {
                "interaction": t.get("interaction"),
                "status": t.get("status"),
                "orthogonal": t.get("orthogonal"),
                "ic_oos": (
                    ((t.get("steps") or {}).get("3_orthogonal_interaction_ic") or {}).get("rank_ic_oos")
                ),
                "reason": t.get("reason"),
            }
            for t in (report.get("orthogonal_tests") or [])
        ],
        "user_fusion_scorecard": report.get("user_fusion_scorecard"),
        "phase": report.get("phase"),
        "top3": [
            {
                "factor": r["factor"],
                "horizon_sec": r["horizon_sec"],
                "rank_ic_oos": r["rank_ic_oos"],
                "icir": r.get("icir"),
                "hit": (r.get("hit_ratio_oos") or {}).get("hit_ratio"),
                "adoption": r["adoption"],
            }
            for r in (report.get("ranked") or [])[:3]
        ],
        "next_steps": report.get("next_steps"),
        "wire": "NO",
        "enforce": 0,
        "auto_apply": False,
        "daily_path": path,
    }
    sp = os.path.join(out_root, "factor_ic_state.json")
    tmp2 = sp + ".tmp"
    with open(tmp2, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp2, sp)


if __name__ == "__main__":
    rep = run_factor_ic_research(max_parts=40, max_rows=4000)
    print(json.dumps({
        "date": rep["date"],
        "csr": rep.get("csr"),
        "phase": rep.get("phase"),
        "portfolio": rep.get("portfolio_adoption"),
        "scorecard": rep.get("user_fusion_scorecard"),
        "unlock": rep.get("strategy_design_unlock"),
        "priorities": rep.get("research_priorities"),
        "funding_feed": rep.get("funding_feed"),
        "behavior_hypotheses": [
            {
                "lane": h.get("lane"),
                "pri": h.get("priority"),
                "status": h.get("status"),
                "ic_sp": h.get("rank_ic_spread_oos"),
                "ic_ix": h.get("rank_ic_interaction_oos"),
                "beats": h.get("beats_spread_alone"),
            }
            for h in (rep.get("behavior_hypotheses") or [])
        ],
        "ox_p0p1_fund": [
            {
                "i": t.get("interaction"),
                "s": t.get("status"),
                "reason": (t.get("reason") or "")[:80],
            }
            for t in (rep.get("orthogonal_tests") or [])
            if (t.get("legs") or [None, None])[-1] in (
                "inventory_proxy", "queue_position", "funding_rate"
            )
        ],
        "next_steps": rep.get("next_steps"),
    }, indent=2, ensure_ascii=False))
