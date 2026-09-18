package main

import (
	"math"
	"time"
)

// HawkesParams: 2次元Hawkes過程 (Buy/Sell) のパラメータセット
type HawkesParams struct {
	MuBuy, MuSell     float64
	AlphaBB, AlphaBS  float64
	AlphaSB, AlphaSS  float64
	BetaBB, BetaBS    float64
	BetaSB, BetaSS    float64
}

type Regime string

const (
	Tokyo  Regime = "Tokyo"
	London Regime = "London"
	NY     Regime = "NY"
)

// USD/JPY 専用パラメータセット (実務感覚値: BR 0.4〜0.8)
var TokyoParams = HawkesParams{
	MuBuy: 12.0, MuSell: 12.0,
	AlphaBB: 3.0, AlphaBS: 1.0,
	AlphaSB: 1.0, AlphaSS: 3.0,
	BetaBB: 5.0, BetaBS: 5.0,
	BetaSB: 5.0, BetaSS: 5.0,
}

var LondonParams = HawkesParams{
	MuBuy: 18.0, MuSell: 18.0,
	AlphaBB: 6.0, AlphaBS: 2.0,
	AlphaSB: 2.0, AlphaSS: 6.0,
	BetaBB: 3.0, BetaBS: 3.0,
	BetaSB: 3.0, BetaSS: 3.0,
}

var NYParams = HawkesParams{
	MuBuy: 22.0, MuSell: 22.0,
	AlphaBB: 8.0, AlphaBS: 3.0,
	AlphaSB: 3.0, AlphaSS: 8.0,
	BetaBB: 4.0, BetaBS: 4.0,
	BetaSB: 4.0, BetaSS: 4.0,
}

func RegimeForTime(t time.Time) (Regime, HawkesParams) {
	hour := t.In(time.FixedZone("JST", 9*3600)).Hour()
	switch {
	case hour >= 9 && hour < 15:
		return Tokyo, TokyoParams
	case hour >= 16 && hour < 23:
		return London, LondonParams
	default:
		return NY, NYParams
	}
}

// HawkesEngine: 過去イベント総和を保持せず、指数カーネル減衰をO(1)で再帰更新
type HawkesEngine struct {
	p       HawkesParams
	regime  Regime
	LamBuy  float64
	LamSell float64
	LastTs  float64
}

func NewHawkesEngine(regime Regime, p HawkesParams) *HawkesEngine {
	return &HawkesEngine{
		p:       p,
		regime:  regime,
		LamBuy:  p.MuBuy,
		LamSell: p.MuSell,
		LastTs:  0.0,
	}
}

func (h *HawkesEngine) SetParams(regime Regime, p HawkesParams) {
	h.regime = regime
	h.p = p
}

// Update: O(1) での強度減衰 & ジャンプ加算
func (h *HawkesEngine) Update(ts float64, side Side) {
	dt := ts - h.LastTs
	if dt < 0 {
		dt = 0
	}

	// 1. 指数減衰: λ(t) = μ + (λ(t-) - μ) * exp(-β * dt)
	h.LamBuy = h.p.MuBuy + (h.LamBuy - h.p.MuBuy)*math.Exp(-h.p.BetaBB*dt)
	h.LamSell = h.p.MuSell + (h.LamSell - h.p.MuSell)*math.Exp(-h.p.BetaSS*dt)

	// 2. イベントジャンプ加算 (+α)
	if side == Buy {
		h.LamBuy += h.p.AlphaBB
		h.LamSell += h.p.AlphaSB
	} else {
		h.LamBuy += h.p.AlphaBS
		h.LamSell += h.p.AlphaSS
	}

	h.LastTs = ts
}

// MicroPrice: λ_buy, λ_sell で重み付けした超短期期待価格
func MicroPrice(bid, ask, lamBuy, lamSell float64) float64 {
	return (lamSell*bid + lamBuy*ask) / (lamBuy + lamSell + 1e-9)
}
