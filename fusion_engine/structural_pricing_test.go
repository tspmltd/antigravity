package main

import (
	"testing"
)

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
	res, err := CalculateOptimalOrderPrice(bids, asks, 2.5, cfg)
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
	res, err := CalculateOptimalOrderPrice(bids, asks, 2.8, cfg)
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
	res, err := CalculateOptimalOrderPrice(bids, asks, -3.2, cfg)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if res.Side != Sell {
		t.Errorf("expected side SELL, got %v", res.Side)
	}
	// Level 2 の壁の直前 (10,000,030 - 1.0 = 10,000,029)
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

	cfg := DefaultStructuralPricingConfig()
	_, err := CalculateOptimalOrderPrice(bids, asks, 0.5, cfg)
	if err != ErrNeutralZone {
		t.Errorf("expected ErrNeutralZone, got %v", err)
	}
}

func TestCalculateOptimalOrderPrice_EmptyBook(t *testing.T) {
	cfg := DefaultStructuralPricingConfig()
	_, err := CalculateOptimalOrderPrice(nil, nil, 3.0, cfg)
	if err != ErrEmptyOrderbook {
		t.Errorf("expected ErrEmptyOrderbook, got %v", err)
	}
}

func TestBuildStructuralOrder(t *testing.T) {
	ee := NewExecutionEngine(10)
	bids := []Level{
		{Price: 10000000.0, Volume: 1.5},
	}
	asks := []Level{
		{Price: 10000010.0, Volume: 1.5},
	}

	cfg := DefaultStructuralPricingConfig()
	ord, err := ee.BuildStructuralOrder("FX_BTC_JPY", bids, asks, 2.5, cfg)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if ord.Symbol != "FX_BTC_JPY" {
		t.Errorf("expected FX_BTC_JPY, got %s", ord.Symbol)
	}
	if ord.Side != Buy {
		t.Errorf("expected BUY, got %v", ord.Side)
	}
	if ord.Type != Limit {
		t.Errorf("expected Limit, got %v", ord.Type)
	}
	if ord.TimeInForce != "GTC" {
		t.Errorf("expected GTC, got %s", ord.TimeInForce)
	}
	if ord.Price != 10000000.0 {
		t.Errorf("expected 10000000, got %.1f", ord.Price)
	}
	if ord.Size != 0.001 {
		t.Errorf("expected 0.001, got %.4f", ord.Size)
	}
}
