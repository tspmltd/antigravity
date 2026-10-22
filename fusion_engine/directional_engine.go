package main

import (
	"math"
	"sync"
)

// OrderBookSnapshot はインジェストされた板の完全状態
type OrderBookSnapshot struct {
	Timestamp int64
	Bids      []Level
	Asks      []Level
}

// DirectionalEngineConfig: 方向性予測エンジンの設定パラメータ
type DirectionalEngineConfig struct {
	DepthK     int     // スキャンする気配深度 (例: 5)
	Lambda     float64 // 深度減衰係数 (例: 0.5)
	Alpha      float64 // WOBI 重み (例: 1.0)
	Beta       float64 // 正規化マイクロプライス乖離 重み (例: 0.5)
	Gamma      float64 // キャンセル圧 重み (例: 0.3)
	WindowSize int     // Zスコア算出用のローリング窓長 (例: 100)
}

func DefaultDirectionalEngineConfig() DirectionalEngineConfig {
	return DirectionalEngineConfig{
		DepthK:     5,
		Lambda:     0.5,
		Alpha:      1.0,
		Beta:       0.5,
		Gamma:      0.3,
		WindowSize: 100,
	}
}

// DirectionalEngine は板の歪み・キャンセル圧・不均衡から価格方向性を先回り予測するコアエンジン
type DirectionalEngine struct {
	mu  sync.Mutex
	cfg DirectionalEngineConfig

	// ゼロアロケーション用：再利用可能な板マップ (clear(m) で使い回し)
	prevBids map[float64]float64
	currBids map[float64]float64
	prevAsks map[float64]float64
	currAsks map[float64]float64

	// O(1) 高速ローリングウィンドウ統計 (Welford / ローリング二乗和差分更新)
	windowSize   int
	scoreHistory []float64
	histIndex    int
	histCount    int
	rollingSum   float64
	rollingSumSq float64
}

func NewDirectionalEngine(cfg DirectionalEngineConfig) *DirectionalEngine {
	return &DirectionalEngine{
		cfg:          cfg,
		prevBids:     make(map[float64]float64, cfg.DepthK*2),
		currBids:     make(map[float64]float64, cfg.DepthK*2),
		prevAsks:     make(map[float64]float64, cfg.DepthK*2),
		currAsks:     make(map[float64]float64, cfg.DepthK*2),
		windowSize:   cfg.WindowSize,
		scoreHistory: make([]float64, cfg.WindowSize),
	}
}

// CalculateWOBI 深度加重板不バランス (Weighted Order Book Imbalance) の算出
func (e *DirectionalEngine) CalculateWOBI(bids, asks []Level) float64 {
	var weightedBidSum, weightedAskSum float64

	kBid := e.cfg.DepthK
	if len(bids) < kBid {
		kBid = len(bids)
	}
	for i := 0; i < kBid; i++ {
		w := math.Exp(-e.cfg.Lambda * float64(i))
		weightedBidSum += w * bids[i].Volume
	}

	kAsk := e.cfg.DepthK
	if len(asks) < kAsk {
		kAsk = len(asks)
	}
	for i := 0; i < kAsk; i++ {
		w := math.Exp(-e.cfg.Lambda * float64(i))
		weightedAskSum += w * asks[i].Volume
	}

	total := weightedBidSum + weightedAskSum
	if total <= 1e-12 {
		return 0.0
	}
	return (weightedBidSum - weightedAskSum) / total
}

// CalculateMicroPriceDislocation 深度加重マイクロプライスと仲値の正規化乖離幅 (Normalized Dislocation) の算出
func (e *DirectionalEngine) CalculateMicroPriceDislocation(bids, asks []Level) (float64, float64) {
	if len(bids) == 0 || len(asks) == 0 {
		return 0.0, 0.0
	}

	pBid := bids[0].Price
	pAsk := asks[0].Price
	midPrice := (pBid + pAsk) / 2.0
	spread := pAsk - pBid

	var wBidVol, wAskVol float64
	kBid := e.cfg.DepthK
	if len(bids) < kBid {
		kBid = len(bids)
	}
	for i := 0; i < kBid; i++ {
		w := math.Exp(-e.cfg.Lambda * float64(i))
		wBidVol += w * bids[i].Volume
	}

	kAsk := e.cfg.DepthK
	if len(asks) < kAsk {
		kAsk = len(asks)
	}
	for i := 0; i < kAsk; i++ {
		w := math.Exp(-e.cfg.Lambda * float64(i))
		wAskVol += w * asks[i].Volume
	}

	vTotal := wBidVol + wAskVol
	if vTotal <= 1e-12 || spread <= 1e-9 {
		return 0.0, midPrice
	}

	// 深度加重マイクロプライス
	microPrice := pBid + spread*(wBidVol/vTotal)
	// スプレッドで正規化 [-0.5, +0.5]
	normDislocation := (microPrice - midPrice) / spread

	return normDislocation, midPrice
}

// calculateSideCancelPressure ゼロアロケーションによる片側キャンセル圧の算出
func (e *DirectionalEngine) calculateSideCancelPressure(
	levels []Level,
	currMap, prevMap map[float64]float64,
) float64 {
	clear(currMap)
	var prevTotalVolume float64
	var totalCancelDrop float64

	k := e.cfg.DepthK
	if len(levels) < k {
		k = len(levels)
	}
	for i := 0; i < k; i++ {
		currMap[levels[i].Price] = levels[i].Volume
	}

	for p, prevVol := range prevMap {
		prevTotalVolume += prevVol
		currVol, exists := currMap[p]
		if !exists {
			totalCancelDrop += prevVol
		} else if currVol < prevVol {
			totalCancelDrop += (prevVol - currVol)
		}
	}

	// 次回のために現在の板を前回の板へスワップコピー
	clear(prevMap)
	for p, v := range currMap {
		prevMap[p] = v
	}

	if prevTotalVolume <= 1e-12 {
		return 0.0
	}
	return totalCancelDrop / prevTotalVolume
}

// UpdateAndEvaluate 毎ティックのメイン処理：特徴量統合と O(1) Zスコア算出
func (e *DirectionalEngine) UpdateAndEvaluate(snapshot OrderBookSnapshot) (float64, float64) {
	e.mu.Lock()
	defer e.mu.Unlock()

	// 1. 各種ミクロ構造指標の計算
	wobi := e.CalculateWOBI(snapshot.Bids, snapshot.Asks)
	normMicroDisloc, _ := e.CalculateMicroPriceDislocation(snapshot.Bids, snapshot.Asks)

	// 2. 買い板・売り板のキャンセル圧（完全対称計算）
	cancelPresBid := e.calculateSideCancelPressure(snapshot.Bids, e.currBids, e.prevBids)
	cancelPresAsk := e.calculateSideCancelPressure(snapshot.Asks, e.currAsks, e.prevAsks)

	// 3. 総合生スコア (Raw Score) の合成
	rawScore := (e.cfg.Alpha * wobi) + (e.cfg.Beta * normMicroDisloc) + (e.cfg.Gamma * (cancelPresAsk - cancelPresBid))

	// 4. O(1) ローリング統計差分更新
	oldVal := e.scoreHistory[e.histIndex]
	e.scoreHistory[e.histIndex] = rawScore
	e.histIndex = (e.histIndex + 1) % e.windowSize

	if e.histCount < e.windowSize {
		e.histCount++
		e.rollingSum += rawScore
		e.rollingSumSq += rawScore * rawScore
	} else {
		e.rollingSum += rawScore - oldVal
		e.rollingSumSq += (rawScore * rawScore) - (oldVal * oldVal)
	}

	// 5. Zスコア算出 (O(1))
	n := float64(e.histCount)
	mean := e.rollingSum / n
	variance := (e.rollingSumSq / n) - (mean * mean)

	var zScore float64
	if variance > 1e-9 {
		std := math.Sqrt(variance)
		zScore = (rawScore - mean) / std
	}

	return rawScore, zScore
}

// EvaluateAndPlace 板スナップショットからZスコアを算出し、特異点検知時に TryPlaceOrder を直接実行
func (e *DirectionalEngine) EvaluateAndPlace(
	ee *ExecutionEngine,
	symbol string,
	snapshot OrderBookSnapshot,
) (placed bool, ord *Order, rawScore, zScore float64, err error) {
	rawScore, zScore = e.UpdateAndEvaluate(snapshot)

	// 構造的指値決定エンジン経由でインフライト・ガード付き発注を実行
	placed, ord, err = ee.TryPlaceOrder(symbol, snapshot.Bids, snapshot.Asks, zScore)
	return placed, ord, rawScore, zScore, err
}
