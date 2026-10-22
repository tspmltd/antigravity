package main

import (
	"errors"
	"fmt"
	"math"
)

var (
	ErrEmptyOrderbook = errors.New("empty orderbook")
	ErrNeutralZone    = errors.New("neutral zone, no order placement")
)

// Level: 板の各価格帯と数量
type Level struct {
	Price  float64 `json:"price"`
	Volume float64 `json:"volume"`
}

// StructuralPricingConfig: 動的最適指値エンジンの設定パラメータ
type StructuralPricingConfig struct {
	ZScoreThreshold float64 // 発注トリガーZスコア (例: 2.0)
	ScanDepth       int     // スキャンする気配深度 (例: 3〜5段階)
	MinWallVolume   float64 // 真空とみなす最小ボリューム閾値 (例: 0.05 BTC)
	BaseLotSize     float64 // 基本発注ロット (例: 0.001)
	TickSize        float64 // 呼値 (例: 1.0 円)
}

func DefaultStructuralPricingConfig() StructuralPricingConfig {
	return StructuralPricingConfig{
		ZScoreThreshold: 2.0,
		ScanDepth:       5,
		MinWallVolume:   0.05,
		BaseLotSize:     0.001,
		TickSize:        1.0,
	}
}

// StructuralQuoteResult: 構造的指値決定の結果
type StructuralQuoteResult struct {
	TargetPrice  float64
	TargetSize   float64
	Side         Side
	AnchoredWall int     // アンカーされた板の深度 (0=最良, 1=2本目...)
	WallVolume   float64 // アンカーされた壁のボリューム
	IsVacuumSkip bool    // 最良気配真空を回避して内側にアンカーしたか
	Reason       string
}

// CalculateOptimalOrderPrice 板の構造とZスコアから、次に指すべき「必然の価格」を算出する（標準シグネチャ）
func CalculateOptimalOrderPrice(bids []Level, asks []Level, zScore float64) (float64, float64, error) {
	res, err := CalculateOptimalOrderPriceDetailed(bids, asks, zScore, DefaultStructuralPricingConfig())
	if err != nil {
		return 0, 0, err
	}
	return res.TargetPrice, res.TargetSize, nil
}

// CalculateOptimalOrderPriceDetailed 板の構造とZスコア/設定値から詳細な結果を算出する
func CalculateOptimalOrderPriceDetailed(
	bids []Level,
	asks []Level,
	zScore float64,
	cfg StructuralPricingConfig,
) (StructuralQuoteResult, error) {
	if len(bids) == 0 || len(asks) == 0 {
		return StructuralQuoteResult{}, ErrEmptyOrderbook
	}

	// 1. 買い方向を狙う場合 (Bullish Intent)
	if zScore > cfg.ZScoreThreshold {
		scanDepth := int(math.Min(float64(cfg.ScanDepth), float64(len(bids))))
		targetLevel := 0
		maxVolume := 0.0

		// 上位N段階をスキャンして最も厚いサポート（壁）を特定
		for i := 0; i < scanDepth; i++ {
			if bids[i].Volume > maxVolume {
				maxVolume = bids[i].Volume
				targetLevel = i
			}
		}

		optimalPrice := bids[targetLevel].Price
		isVacuumSkip := false

		// 最良気配が薄く（真空地帯）、背後（Level 1以上）に分厚い壁がある場合
		if targetLevel > 0 && bids[0].Volume < cfg.MinWallVolume {
			// Level 0 の薄板での毒食いを回避し、強固な大口壁の1ティック手前に指す
			optimalPrice = bids[targetLevel].Price + cfg.TickSize
			isVacuumSkip = true
		}

		return StructuralQuoteResult{
			TargetPrice:  optimalPrice,
			TargetSize:   cfg.BaseLotSize,
			Side:         Buy,
			AnchoredWall: targetLevel,
			WallVolume:   maxVolume,
			IsVacuumSkip: isVacuumSkip,
			Reason: fmt.Sprintf("BULL_WALL_ANCHOR (lvl=%d vol=%.4f vac_skip=%t)",
				targetLevel, maxVolume, isVacuumSkip),
		}, nil
	}

	// 2. 売り方向を狙う場合 (Bearish Intent)
	if zScore < -cfg.ZScoreThreshold {
		scanDepth := int(math.Min(float64(cfg.ScanDepth), float64(len(asks))))
		targetLevel := 0
		maxVolume := 0.0

		for i := 0; i < scanDepth; i++ {
			if asks[i].Volume > maxVolume {
				maxVolume = asks[i].Volume
				targetLevel = i
			}
		}

		optimalPrice := asks[targetLevel].Price
		isVacuumSkip := false

		// 最良売り気配が真空で、背後に厚い売り壁がある場合
		if targetLevel > 0 && asks[0].Volume < cfg.MinWallVolume {
			optimalPrice = asks[targetLevel].Price - cfg.TickSize
			isVacuumSkip = true
		}

		return StructuralQuoteResult{
			TargetPrice:  optimalPrice,
			TargetSize:   cfg.BaseLotSize,
			Side:         Sell,
			AnchoredWall: targetLevel,
			WallVolume:   maxVolume,
			IsVacuumSkip: isVacuumSkip,
			Reason: fmt.Sprintf("BEAR_WALL_ANCHOR (lvl=%d vol=%.4f vac_skip=%t)",
				targetLevel, maxVolume, isVacuumSkip),
		}, nil
	}

	return StructuralQuoteResult{}, ErrNeutralZone
}
