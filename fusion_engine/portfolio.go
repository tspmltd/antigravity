package main

import "math"

// MarketProfile: 銘柄ごとのミクロ・マクロ特性定義
type MarketProfile struct {
	Symbol       string
	PipValue     float64 // 1pipまたは1ティックあたりの円建て価値
	MinSize      float64 // 最小発注ロット (株式なら100株、FXなら0.1ロット等)
	MaxSize      float64 // 最大許容ロット
	TickSize     float64 // 呼値・最小ティック
	Volatility   float64 // 想定年率ボラティリティ
	Liquidity    float64 // 板厚み・流動性スコア (0.0〜1.0)
	ExecutionLag float64 // 想定ネットワーク遅延 (秒)
}

var USDJPYProfile = MarketProfile{
	Symbol:       "USDJPY",
	PipValue:     1000.0, // 1ロット(10万通貨)あたり1pip = 1,000円
	MinSize:      10000.0,
	MaxSize:      5000000.0,
	TickSize:     0.001,
	Volatility:   0.08,
	Liquidity:    0.95,
	ExecutionLag: 0.002,
}

var BTCJPYProfile = MarketProfile{
	Symbol:       "BTCJPY",
	PipValue:     1.0,
	MinSize:      0.001,
	MaxSize:      1.0,
	TickSize:     1.0,
	Volatility:   0.55,
	Liquidity:    0.75,
	ExecutionLag: 0.005,
}

var JP7203Profile = MarketProfile{
	Symbol:       "7203", // トヨタ自動車
	PipValue:     100.0,  // 1単元(100株)あたり1円幅 = 100円
	MinSize:      100.0,
	MaxSize:      1000.0,
	TickSize:     1.0,
	Volatility:   0.22,
	Liquidity:    0.90,
	ExecutionLag: 0.003,
}

var NASDAQProfile = MarketProfile{
	Symbol:       "NQ",
	PipValue:     20.0,
	MinSize:      1.0,
	MaxSize:      10.0,
	TickSize:     0.25,
	Volatility:   0.18,
	Liquidity:    0.85,
	ExecutionLag: 0.003,
}

// RiskPerTrade: 1トレードあたりの許容損失額 (円)
func RiskPerTrade(balance, budget float64) float64 {
	return balance * budget
}

// PositionSize: ATRボラティリティスケーリングによる基本ロット算出
func PositionSize(risk, atr, pipValue float64) float64 {
	if atr <= 0 || pipValue <= 0 {
		return 0
	}
	return risk / (atr * pipValue)
}

// AdjustSize: スリッページおよび遅延実績による動的サイズ縮小
func AdjustSize(size, slip, latency float64) float64 {
	factor := 1.0

	if slip > 0.0001 {
		factor -= 0.2
	}
	if latency > 5e-3 {
		factor -= 0.2
	}

	if factor < 0.1 {
		factor = 0.1
	}
	return size * factor
}

// ComputeSize: 資金配分 × ボラティリティ × 銘柄制約の総合サイズ決定
func ComputeSize(balance, budget, atr float64, mp MarketProfile) float64 {
	risk := RiskPerTrade(balance, budget)
	size := PositionSize(risk, atr, mp.PipValue)

	// 銘柄固有の上下限クリップ
	if size < mp.MinSize {
		size = mp.MinSize
	}
	if size > mp.MaxSize {
		size = mp.MaxSize
	}

	// 呼値・単元株制への丸め
	if mp.MinSize >= 100.0 {
		size = math.Floor(size/100.0) * 100.0
	}

	return size
}
