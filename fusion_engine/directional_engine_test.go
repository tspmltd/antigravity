package main

import (
	"testing"
)

func TestDirectionalEngine_WOBICalculation(t *testing.T) {
	cfg := DefaultDirectionalEngineConfig()
	engine := NewDirectionalEngine(cfg)

	// 1. 買い板優勢 (Bullish)
	bids := []Level{
		{Price: 10000000, Volume: 2.0},
		{Price: 9999990, Volume: 1.5},
	}
	asks := []Level{
		{Price: 10000010, Volume: 0.5},
		{Price: 10000020, Volume: 0.2},
	}

	wobiBull := engine.CalculateWOBI(bids, asks)
	if wobiBull <= 0.3 {
		t.Errorf("expected positive WOBI > 0.3, got %.4f", wobiBull)
	}

	// 2. 売り板優勢 (Bearish)
	wobiBear := engine.CalculateWOBI(asks, bids)
	if wobiBear >= -0.3 {
		t.Errorf("expected negative WOBI < -0.3, got %.4f", wobiBear)
	}
}

func TestDirectionalEngine_MicroPriceDislocation(t *testing.T) {
	cfg := DefaultDirectionalEngineConfig()
	engine := NewDirectionalEngine(cfg)

	// 仲値 = 10,000,005
	// Bid Volume (2.0) >> Ask Volume (0.5) なのでマイクロプライスは上方に偏る
	bids := []Level{{Price: 10000000, Volume: 2.0}}
	asks := []Level{{Price: 10000010, Volume: 0.5}}

	disloc, mid := engine.CalculateMicroPriceDislocation(bids, asks)
	if mid != 10000005.0 {
		t.Errorf("expected mid 10000005, got %.1f", mid)
	}
	if disloc <= 0.0 {
		t.Errorf("expected positive dislocation, got %.4f", disloc)
	}
}

func TestDirectionalEngine_SymmetricCancelPressure(t *testing.T) {
	cfg := DefaultDirectionalEngineConfig()
	engine := NewDirectionalEngine(cfg)

	// Tick 1: 初期の厚い板を登録
	snap1 := OrderBookSnapshot{
		Timestamp: 1000,
		Bids: []Level{
			{Price: 10000000, Volume: 1.0},
			{Price: 9999990, Volume: 2.0},
		},
		Asks: []Level{
			{Price: 10000010, Volume: 1.0},
			{Price: 10000020, Volume: 3.0}, // ここに大きな売り壁
		},
	}
	engine.UpdateAndEvaluate(snap1)

	// Tick 2: 売り壁 (10000020 の 3.0 BTC) が成行なしで突如キャンセル消失！
	snap2 := OrderBookSnapshot{
		Timestamp: 1001,
		Bids: []Level{
			{Price: 10000000, Volume: 1.0},
			{Price: 9999990, Volume: 2.0},
		},
		Asks: []Level{
			{Price: 10000010, Volume: 1.0}, // 10000020 の壁が消失
		},
	}
	rawScore2, _ := engine.UpdateAndEvaluate(snap2)

	// 売り壁が剥がされたため、cancelPresAsk が急増し、rawScore が大きくプラスに跳ねるべき
	if rawScore2 <= 0.1 {
		t.Errorf("expected positive score after ask cancel, got %.4f", rawScore2)
	}
}

func TestDirectionalEngine_ClosedLoopEvaluateAndPlace(t *testing.T) {
	cfg := DefaultDirectionalEngineConfig()
	cfg.WindowSize = 20
	engine := NewDirectionalEngine(cfg)
	ee := NewExecutionEngine(10)

	// 1. 平常時（中立板）を連続注入してローリング統計を安定化
	neutralSnap := OrderBookSnapshot{
		Timestamp: 1000,
		Bids:      []Level{{Price: 10000000, Volume: 1.0}},
		Asks:      []Level{{Price: 10000010, Volume: 1.0}},
	}
	for i := 0; i < 20; i++ {
		placed, _, _, _, err := engine.EvaluateAndPlace(ee, "FX_BTC_JPY", neutralSnap)
		if placed || err != ErrNeutralZone {
			t.Fatalf("expected neutral zone skip on tick %d, got placed=%t err=%v", i, placed, err)
		}
	}

	if ee.IsInFlight() {
		t.Fatalf("expected isInFlight=false during neutral period")
	}

	// 2. 突如として巨大な買い圧力（大口買い板＋薄い売り板）が到来！
	bullishSnap := OrderBookSnapshot{
		Timestamp: 1021,
		Bids: []Level{
			{Price: 10000000, Volume: 0.01}, // 真空
			{Price: 9999950, Volume: 5.00},  // 巨大サポート壁
		},
		Asks: []Level{
			{Price: 10000010, Volume: 0.10},
		},
	}

	placed, ord, raw, z, err := engine.EvaluateAndPlace(ee, "FX_BTC_JPY", bullishSnap)
	if !placed || err != nil || ord == nil {
		t.Fatalf("expected order placed on bullish spike, got placed=%t z=%.2f raw=%.2f err=%v", placed, z, raw, err)
	}

	// 真空（0.01）を回避して 9999950 + 1 = 9999951 に指されているか
	if ord.Price != 9999951.0 {
		t.Errorf("expected wall anchor price 9999951, got %.1f", ord.Price)
	}
	if !ee.IsInFlight() {
		t.Fatalf("expected in-flight lock active after placement")
	}

	// 3. インフライト中に再度シグナルが来ても、二重発注が物理遮断されること
	placed2, _, _, _, err2 := engine.EvaluateAndPlace(ee, "FX_BTC_JPY", bullishSnap)
	if placed2 || err2 == nil {
		t.Fatalf("expected double order blocked by in-flight lock, but got placed2=true")
	}

	// 4. パブリック約定テープで 9,999,951 円の売り約定を受信（自社指値にヒット）
	pubTrade := Trade{
		Ts:    1022.0,
		Side:  Sell,
		Size:  0.01,
		Price: 9999951.0,
	}
	matched, fill := ee.OnPublicTradeExecution(pubTrade)
	if !matched || fill == nil {
		t.Fatalf("expected public trade match, got matched=false")
	}

	// 5. 即時約定によりインフライト・ロックが解放されたこと
	if ee.IsInFlight() {
		t.Fatalf("expected in-flight lock released after public match")
	}
}
