package main

import (
	"testing"
)

func TestCalculateOptimalOrderPrice_StandardSignature(t *testing.T) {
	bids := []Level{
		{Price: 10000000.0, Volume: 1.50}, // 厚い壁
		{Price: 9999990.0, Volume: 0.50},
	}
	asks := []Level{
		{Price: 10000010.0, Volume: 0.80},
	}

	price, size, err := CalculateOptimalOrderPrice(bids, asks, 2.5)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if price != 10000000.0 {
		t.Errorf("expected 10000000, got %.1f", price)
	}
	if size != 0.001 {
		t.Errorf("expected 0.001, got %.4f", size)
	}
}

func TestCalculateOptimalOrderPrice_BullishThickL0(t *testing.T) {
	bids := []Level{
		{Price: 10000000.0, Volume: 1.50}, // 厚い壁
		{Price: 9999990.0, Volume: 0.50},
		{Price: 9999980.0, Volume: 0.20},
	}
	asks := []Level{
		{Price: 10000010.0, Volume: 0.80},
		{Price: 10000020.0, Volume: 0.50},
	}

	cfg := DefaultStructuralPricingConfig()
	res, err := CalculateOptimalOrderPriceDetailed(bids, asks, 2.5, cfg)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if res.Side != Buy {
		t.Errorf("expected side BUY, got %v", res.Side)
	}
	if res.TargetPrice != 10000000.0 {
		t.Errorf("expected target price 10,000,000, got %.1f", res.TargetPrice)
	}
	if res.AnchoredWall != 0 {
		t.Errorf("expected anchored wall 0, got %d", res.AnchoredWall)
	}
	if res.IsVacuumSkip {
		t.Errorf("expected isVacuumSkip false, got true")
	}
}

func TestCalculateOptimalOrderPrice_BullishVacuumL0(t *testing.T) {
	// Level 0 はスカスカの真空 (0.01 BTC < 0.05 BTC)
	// Level 1 に 2.0 BTC の分厚いサポート壁
	bids := []Level{
		{Price: 10000000.0, Volume: 0.01}, // 真空
		{Price: 9999950.0, Volume: 2.00},  // 厚い壁
		{Price: 9999900.0, Volume: 0.50},
	}
	asks := []Level{
		{Price: 10000010.0, Volume: 0.50},
	}

	cfg := DefaultStructuralPricingConfig()
	res, err := CalculateOptimalOrderPriceDetailed(bids, asks, 2.8, cfg)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	// Level 1 の壁の直前 (9,999,950 + 1.0 = 9,999,951) に配置されるべき
	expectedPrice := 9999950.0 + cfg.TickSize
	if res.TargetPrice != expectedPrice {
		t.Errorf("expected price %.1f, got %.1f", expectedPrice, res.TargetPrice)
	}
	if !res.IsVacuumSkip {
		t.Errorf("expected isVacuumSkip true, got false")
	}
	if res.AnchoredWall != 1 {
		t.Errorf("expected anchored wall 1, got %d", res.AnchoredWall)
	}
}

func TestCalculateOptimalOrderPrice_BearishVacuumL0(t *testing.T) {
	// 売り方向: Level 0 が真空、Level 2 に巨大売り壁
	bids := []Level{
		{Price: 10000000.0, Volume: 0.50},
	}
	asks := []Level{
		{Price: 10000010.0, Volume: 0.02}, // 真空
		{Price: 10000020.0, Volume: 0.10},
		{Price: 10000030.0, Volume: 3.50}, // 巨大壁 (Level 2)
	}

	cfg := DefaultStructuralPricingConfig()
	res, err := CalculateOptimalOrderPriceDetailed(bids, asks, -3.2, cfg)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if res.Side != Sell {
		t.Errorf("expected side SELL, got %v", res.Side)
	}
	expectedPrice := 10000030.0 - cfg.TickSize
	if res.TargetPrice != expectedPrice {
		t.Errorf("expected price %.1f, got %.1f", expectedPrice, res.TargetPrice)
	}
	if !res.IsVacuumSkip {
		t.Errorf("expected isVacuumSkip true")
	}
	if res.AnchoredWall != 2 {
		t.Errorf("expected anchored wall 2, got %d", res.AnchoredWall)
	}
}

func TestCalculateOptimalOrderPrice_NeutralZone(t *testing.T) {
	bids := []Level{{Price: 10000000.0, Volume: 1.0}}
	asks := []Level{{Price: 10000010.0, Volume: 1.0}}

	_, _, err := CalculateOptimalOrderPrice(bids, asks, 0.5)
	if err != ErrNeutralZone {
		t.Errorf("expected ErrNeutralZone, got %v", err)
	}
}

func TestCalculateOptimalOrderPrice_EmptyBook(t *testing.T) {
	_, _, err := CalculateOptimalOrderPrice(nil, nil, 3.0)
	if err != ErrEmptyOrderbook {
		t.Errorf("expected ErrEmptyOrderbook, got %v", err)
	}
}

func TestTryPlaceOrderAndInFlightPublicMatch(t *testing.T) {
	ee := NewExecutionEngine(10)
	bids := []Level{
		{Price: 10000000.0, Volume: 2.0}, // 壁
	}
	asks := []Level{
		{Price: 10000010.0, Volume: 1.0},
	}

	// 1. 中立圏では発注されない
	placed, _, err := ee.TryPlaceOrder("FX_BTC_JPY", bids, asks, 0.5)
	if placed || err != ErrNeutralZone {
		t.Fatalf("expected neutral zone skip, got placed=%t err=%v", placed, err)
	}
	if ee.IsInFlight() {
		t.Fatalf("expected isInFlight=false")
	}

	// 2. 買いシグナル (Z=2.5) で発注成功 & インフライトロック発動
	placed, ord, err := ee.TryPlaceOrder("FX_BTC_JPY", bids, asks, 2.5)
	if !placed || err != nil || ord == nil {
		t.Fatalf("expected order placed, got placed=%t err=%v", placed, err)
	}
	if !ee.IsInFlight() {
		t.Fatalf("expected isInFlight=true after placement")
	}
	if ord.Price != 10000000.0 {
		t.Errorf("expected ord.Price 10000000, got %.1f", ord.Price)
	}

	// 3. インフライト中に再度発注を試みても、物理的に遮断される（二重発注防止！）
	placed2, _, err2 := ee.TryPlaceOrder("FX_BTC_JPY", bids, asks, 2.8)
	if placed2 || err2 == nil {
		t.Fatalf("expected in-flight double order rejection, but order was placed!")
	}

	// 4. パブリック約定テープで 10,000,000 円の売り約定を受信（自社指値にヒット）
	pubTrade := Trade{
		Ts:    1000.0,
		Side:  Sell,
		Size:  0.005,
		Price: 10000000.0,
	}
	matched, fill := ee.OnPublicTradeExecution(pubTrade)
	if !matched || fill == nil {
		t.Fatalf("expected instant public trade match, but got matched=false")
	}
	if fill.ExecPrice != 10000000.0 {
		t.Errorf("expected fill.ExecPrice 10000000, got %.1f", fill.ExecPrice)
	}

	// 5. 約定によりインフライト・ロックが即座に解放され、次の発注が可能になる
	if ee.IsInFlight() {
		t.Fatalf("expected isInFlight=false after fill report")
	}
}
