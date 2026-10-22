package main

import (
	"fmt"
	"math"
	"sync"
	"sync/atomic"
	"time"
)

type OrderType int

const (
	Limit OrderType = iota
	Market
)

type Order struct {
	ClientOrderId string
	Symbol        string
	Side          Side
	Type          OrderType
	Price         float64
	Size          float64
	TimeInForce   string // "IOC", "FOK", "GTC"
	TsSent        float64
}

type FillReport struct {
	ClientOrderId string
	Symbol        string
	Side          Side
	ExpectedPrice float64
	ExecPrice     float64
	ExecSize      float64
	Slippage      float64
	LatencySec    float64
	PnL           float64
	Ts            float64
}

// GovernanceState: Python 司令塔 (Regime Orchestrator) からトップダウン発令されるガバナンス状態
type GovernanceState struct {
	TargetMode     string  `json:"target_mode"`     // "HFT", "TREND", "HYBRID", "REDUCE_50", "STOP"
	IsHalted       bool    `json:"is_halted"`       // true: 物理的遮断（即時発注禁止）
	RiskMultiplier float64 `json:"risk_multiplier"` // 1.0 (通常), 0.5 (REDUCE_50), 0.0 (STOP)
	Reason         string  `json:"reason"`
	UpdatedAt      float64 `json:"updated_at"`
}

// OnlineTuner: 約定スリッページ・リジェクト実績から動的に閾値を微調整する自己学習機構
type OnlineTuner struct {
	mu             sync.RWMutex
	Aggressiveness float64 // 0.0 (保守的) 〜 1.0 (積極的)
	RejectCount    int
	TotalFills     int
}

func NewOnlineTuner() *OnlineTuner {
	return &OnlineTuner{
		Aggressiveness: 0.5,
	}
}

func (ot *OnlineTuner) UpdateFromFill(slip float64) {
	ot.mu.Lock()
	defer ot.mu.Unlock()
	ot.TotalFills++
	// スリッページが大きい場合は攻撃度を落とし、キューを少し内側に配置
	if math.Abs(slip) > 0.001 {
		ot.Aggressiveness = math.Max(0.1, ot.Aggressiveness*0.95)
	} else {
		ot.Aggressiveness = math.Min(0.9, ot.Aggressiveness*1.02)
	}
}

func (ot *OnlineTuner) UpdateFromReject() {
	ot.mu.Lock()
	defer ot.mu.Unlock()
	ot.RejectCount++
	// リジェクトが多い場合はより保守的なIOC/FOK条件に後退
	ot.Aggressiveness = math.Max(0.1, ot.Aggressiveness*0.90)
}

// ExecutionEngine: ノンブロッキング発注・キューポジション管理・ガバナンス遮断機構
type ExecutionEngine struct {
	orderCh       chan Order
	fillCh        chan FillReport
	tuner         *OnlineTuner
	activeOrders  map[string]Order
	mu            sync.Mutex
	govMu         sync.RWMutex
	governance    GovernanceState
	running       bool
	ordersSent    int64
	ordersHalted  int64
	ordersDropped int64

	// In-Flight Guard: 二重発注の物理遮断 ＆ パブリック即時約定照合
	flightMu          sync.Mutex
	isInFlight        bool
	activeFlightOrder *Order
}

func NewExecutionEngine(bufSize int) *ExecutionEngine {
	return &ExecutionEngine{
		orderCh:      make(chan Order, bufSize),
		fillCh:       make(chan FillReport, bufSize),
		tuner:        NewOnlineTuner(),
		activeOrders: make(map[string]Order),
		governance: GovernanceState{
			TargetMode:     "HYBRID",
			IsHalted:       false,
			RiskMultiplier: 1.0,
			Reason:         "INITIALIZED",
			UpdatedAt:      float64(time.Now().UnixNano()) / 1e9,
		},
		running: true,
	}
}

// SetGovernance: Python司令塔 (Regime Orchestrator) からのガバナンス通達を反映
func (ee *ExecutionEngine) SetGovernance(targetMode string, isHalted bool, riskMultiplier float64, reason string) {
	ee.govMu.Lock()
	defer ee.govMu.Unlock()

	ee.governance = GovernanceState{
		TargetMode:     targetMode,
		IsHalted:       isHalted,
		RiskMultiplier: riskMultiplier,
		Reason:         reason,
		UpdatedAt:      float64(time.Now().UnixNano()) / 1e9,
	}
}

// GetGovernance: 現在のガバナンス状態を取得
func (ee *ExecutionEngine) GetGovernance() GovernanceState {
	ee.govMu.RLock()
	defer ee.govMu.RUnlock()
	return ee.governance
}

// Submit: 発注投入。IsHalted時は即座に0nsで物理遮断
func (ee *ExecutionEngine) Submit(o Order) bool {
	ee.govMu.RLock()
	gov := ee.governance
	ee.govMu.RUnlock()

	// 1. ガバナンス遮断判定 (STOP 命令最優先)
	if gov.IsHalted || gov.TargetMode == "STOP" {
		atomic.AddInt64(&ee.ordersHalted, 1)
		return false
	}

	// 2. リスク乗数の適用 (REDUCE_50 など)
	if gov.RiskMultiplier < 1.0 && gov.RiskMultiplier > 0.0 {
		o.Size *= gov.RiskMultiplier
	}

	// 3. ノンブロッキング送信
	select {
	case ee.orderCh <- o:
		atomic.AddInt64(&ee.ordersSent, 1)
		return true
	default:
		atomic.AddInt64(&ee.ordersDropped, 1)
		fmt.Printf("[EXEC] ⚠️ Order channel full! Dropping order %s\n", o.ClientOrderId)
		return false
	}
}

// GetStats: 発注統計
func (ee *ExecutionEngine) GetStats() (sent, halted, dropped int64) {
	return atomic.LoadInt64(&ee.ordersSent),
		atomic.LoadInt64(&ee.ordersHalted),
		atomic.LoadInt64(&ee.ordersDropped)
}

// BuildOrder: Microprice × OrderFlow Imbalance × Hawkes強度 による攻めの指値/IOC発注構築
func (ee *ExecutionEngine) BuildOrder(
	symbol string,
	sig Signal,
	bid, ask, mp, size float64,
	flowImb float64,
	lamBuy, lamSell float64,
) Order {
	now := float64(time.Now().UnixNano()) / 1e9
	orderId := fmt.Sprintf("ord_%s_%d", symbol, time.Now().UnixNano()%1000000)

	ee.tuner.mu.RLock()
	aggr := ee.tuner.Aggressiveness
	ee.tuner.mu.RUnlock()

	var side Side
	var price float64

	if sig == Long {
		side = Buy
		// 攻めのキューポジション: フローと強度が強い時はスプレッド内側に指値をねじ込む
		if lamBuy > lamSell && flowImb > 0.4 {
			price = bid + (ask-bid)*0.4*aggr
		} else {
			price = bid
		}
	} else {
		side = Sell
		if lamSell > lamBuy && flowImb < -0.4 {
			price = ask - (ask-bid)*0.4*aggr
		} else {
			price = ask
		}
	}

	return Order{
		ClientOrderId: orderId,
		Symbol:        symbol,
		Side:          side,
		Type:          Limit,
		Price:         price,
		Size:          size,
		TimeInForce:   "IOC", // 即時約定・残キャンセルで在庫リスクを回避
		TsSent:        now,
	}
}

// BuildStructuralOrder: 板の深層構造（Level）× Zスコアに基づく構造的最適指値の構築
func (ee *ExecutionEngine) BuildStructuralOrder(
	symbol string,
	bids, asks []Level,
	zScore float64,
	cfg StructuralPricingConfig,
) (*Order, error) {
	quote, err := CalculateOptimalOrderPriceDetailed(bids, asks, zScore, cfg)
	if err != nil {
		return nil, err
	}

	now := float64(time.Now().UnixNano()) / 1e9
	orderId := fmt.Sprintf("str_ord_%s_%d", symbol, time.Now().UnixNano()%1000000)

	ee.govMu.RLock()
	riskMul := ee.governance.RiskMultiplier
	ee.govMu.RUnlock()

	size := quote.TargetSize
	if riskMul < 1.0 && riskMul > 0.0 {
		size *= riskMul
	}

	order := &Order{
		ClientOrderId: orderId,
		Symbol:        symbol,
		Side:          quote.Side,
		Type:          Limit,
		Price:         quote.TargetPrice,
		Size:          size,
		TimeInForce:   "GTC", // 壁にアンカーする指値
		TsSent:        now,
	}
	return order, nil
}

// IsInFlight: 現在未確定のインフライト注文が存在するか判定
func (ee *ExecutionEngine) IsInFlight() bool {
	ee.flightMu.Lock()
	defer ee.flightMu.Unlock()
	return ee.isInFlight
}

// TryPlaceOrder: 最適価格が計算された時のみ発注し、インフライト・ガードで多重発注を完全ロック
func (ee *ExecutionEngine) TryPlaceOrder(
	symbol string,
	bids, asks []Level,
	zScore float64,
) (bool, *Order, error) {
	ee.flightMu.Lock()
	if ee.isInFlight {
		ee.flightMu.Unlock()
		return false, nil, fmt.Errorf("in-flight lock active: order already outstanding")
	}

	price, size, err := CalculateOptimalOrderPrice(bids, asks, zScore)
	if err != nil {
		ee.flightMu.Unlock()
		return false, nil, err
	}

	side := Buy
	if zScore < 0 {
		side = Sell
	}

	ee.govMu.RLock()
	riskMul := ee.governance.RiskMultiplier
	ee.govMu.RUnlock()
	if riskMul < 1.0 && riskMul > 0.0 {
		size *= riskMul
	}

	orderId := fmt.Sprintf("str_%s_%d", symbol, time.Now().UnixNano()%1000000)
	order := &Order{
		ClientOrderId: orderId,
		Symbol:        symbol,
		Side:          side,
		Type:          Limit,
		Price:         price,
		Size:          size,
		TimeInForce:   "GTC",
		TsSent:        float64(time.Now().UnixNano()) / 1e9,
	}

	// 発注投入と同時にインフライト・ガードを起動
	submitted := ee.Submit(*order)
	if submitted {
		ee.isInFlight = true
		ee.activeFlightOrder = order
		ee.flightMu.Unlock()
		return true, order, nil
	}

	ee.flightMu.Unlock()
	return false, nil, fmt.Errorf("submission rejected by governance or queue full")
}

// OnPublicTradeExecution: パブリック約定テープを受信した際に自社指値を照合し、即時約定確定・ロック解除
func (ee *ExecutionEngine) OnPublicTradeExecution(t Trade) (bool, *FillReport) {
	ee.flightMu.Lock()
	defer ee.flightMu.Unlock()

	if !ee.isInFlight || ee.activeFlightOrder == nil {
		return false, nil
	}

	ord := ee.activeFlightOrder
	isMatched := false
	if ord.Side == Buy && t.Side == Sell && t.Price <= ord.Price {
		isMatched = true
	} else if ord.Side == Sell && t.Side == Buy && t.Price >= ord.Price {
		isMatched = true
	}

	if isMatched {
		fill := &FillReport{
			ClientOrderId: ord.ClientOrderId,
			Symbol:        ord.Symbol,
			Side:          ord.Side,
			ExpectedPrice: ord.Price,
			ExecPrice:     t.Price,
			ExecSize:      ord.Size,
			Slippage:      t.Price - ord.Price,
			LatencySec:    (float64(time.Now().UnixNano()) / 1e9) - ord.TsSent,
			Ts:            float64(time.Now().UnixNano()) / 1e9,
		}

		// インフライト・ロックを即時解放！
		ee.isInFlight = false
		ee.activeFlightOrder = nil
		return true, fill
	}

	return false, nil
}
