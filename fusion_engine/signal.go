package main

type Signal int

const (
	Flat Signal = iota
	Long
	Short
)

func (s Signal) String() string {
	switch s {
	case Long:
		return "LONG"
	case Short:
		return "SHORT"
	default:
		return "FLAT"
	}
}

type SignalConfig struct {
	ImbShort  float64 // 0.1s 窓 インバランス閾値 (例: 0.6)
	FlowShort float64 // 0.1s 窓 NetFlow閾値 (通貨単位)
	ImbMid    float64 // 0.5s 窓 インバランス閾値 (例: 0.5)
	FlowMid   float64 // 0.5s 窓 NetFlow閾値
}

func DefaultSignalConfig() SignalConfig {
	return SignalConfig{
		ImbShort:  0.50,
		FlowShort: 100_000.0,
		ImbMid:    0.40,
		FlowMid:   300_000.0,
	}
}

func SignalConfigForProfile(p MarketProfile) SignalConfig {
	switch p.Symbol {
	case "BTCJPY":
		return SignalConfig{
			ImbShort:  0.50,
			FlowShort: 0.5,
			ImbMid:    0.40,
			FlowMid:   1.5,
		}
	case "7203":
		return SignalConfig{
			ImbShort:  0.50,
			FlowShort: 1000.0,
			ImbMid:    0.40,
			FlowMid:   3000.0,
		}
	default: // USDJPY / NQ
		return SignalConfig{
			ImbShort:  0.50,
			FlowShort: 100_000.0,
			ImbMid:    0.40,
			FlowMid:   250_000.0,
		}
	}
}

type SignalEngine struct {
	cfg SignalConfig
	rb  *RingBuffer
}

func NewSignalEngine(cfg SignalConfig, rb *RingBuffer) *SignalEngine {
	return &SignalEngine{
		cfg: cfg,
		rb:  rb,
	}
}

// Generate: Order Flow 複数窓 (0.1s & 0.5s) × ブレイクアウト判定
func (se *SignalEngine) Generate(now, price, recentHigh, recentLow float64) Signal {
	// 0.1秒 短期窓
	_, _, netS, imbS, _ := se.rb.Metrics(now, 0.1)
	// 0.5秒 中期窓
	_, _, netM, imbM, _ := se.rb.Metrics(now, 0.5)

	longCond := (imbS > se.cfg.ImbShort && netS > se.cfg.FlowShort &&
		imbM > se.cfg.ImbMid && netM > se.cfg.FlowMid &&
		price >= recentHigh)

	shortCond := (imbS < -se.cfg.ImbShort && netS < -se.cfg.FlowShort &&
		imbM < -se.cfg.ImbMid && netM < -se.cfg.FlowMid &&
		price <= recentLow)

	if longCond {
		return Long
	}
	if shortCond {
		return Short
	}
	return Flat
}

// GenerateEnhanced: Order Flow × Hawkes λ × Microprice 統合シグナル
func (se *SignalEngine) GenerateEnhanced(
	now, price, bid, ask, recentHigh, recentLow, lamBuy, lamSell float64,
) (Signal, float64) {
	baseSig := se.Generate(now, price, recentHigh, recentLow)
	mp := MicroPrice(bid, ask, lamBuy, lamSell)

	// MicropriceとHawkes強度が同方向の場合のみエントリー昇格 (確信度フィルタ)
	if baseSig == Long && mp > price && lamBuy > lamSell {
		return Long, mp
	}
	if baseSig == Short && mp < price && lamSell > lamBuy {
		return Short, mp
	}
	return Flat, mp
}
