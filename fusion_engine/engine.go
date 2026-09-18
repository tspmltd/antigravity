package main

import (
	"sync"
	"time"
)

type MarketSnapshot struct {
	Ts         float64
	Symbol     string
	Price      float64
	Bid        float64
	Ask        float64
	RecentHigh float64
	RecentLow  float64
	ATR        float64
}

// FusionEngine: 複数アセットを同一設計軸で統括する超低レイテンシHFT基盤
type FusionEngine struct {
	Symbol     string
	Profile    MarketProfile
	rb         *RingBuffer
	hawkes     *HawkesEngine
	signal     *SignalEngine
	execution  *ExecutionEngine
	metrics    *FusionMetrics
	balanceJpy float64
	riskBudget float64

	tradeCh chan Trade
	snapCh  chan MarketSnapshot
	doneCh  chan struct{}
	wg      sync.WaitGroup
}

func NewFusionEngine(profile MarketProfile, balanceJpy, riskBudget float64, bufSize int) *FusionEngine {
	rb := NewRingBuffer(bufSize)
	regime, params := RegimeForTime(time.Now())
	hawkes := NewHawkesEngine(regime, params)
	signal := NewSignalEngine(SignalConfigForProfile(profile), rb)
	execution := NewExecutionEngine(bufSize)
	metrics := NewFusionMetrics()

	return &FusionEngine{
		Symbol:     profile.Symbol,
		Profile:    profile,
		rb:         rb,
		hawkes:     hawkes,
		signal:     signal,
		execution:  execution,
		metrics:    metrics,
		balanceJpy: balanceJpy,
		riskBudget: riskBudget,
		tradeCh:    make(chan Trade, bufSize),
		snapCh:     make(chan MarketSnapshot, bufSize),
		doneCh:     make(chan struct{}),
	}
}

// StartPipeline: 独立Goroutine群による超高速並列イベントループ起動（Supervisor自律監視・Self-Healing）
func (fe *FusionEngine) StartPipeline() {
	fe.wg.Add(1)
	globalSupervisor.Supervise("trade_worker_"+fe.Symbol, func() {
		defer fe.wg.Done()
		for {
			select {
			case <-fe.doneCh:
				return
			case t := <-fe.tradeCh:
				fe.rb.Add(t)
				fe.hawkes.Update(t.Ts, t.Side)
			}
		}
	})

	fe.wg.Add(1)
	globalSupervisor.Supervise("signal_exec_worker_"+fe.Symbol, func() {
		defer fe.wg.Done()
		for {
			select {
			case <-fe.doneCh:
				return
			case snap := <-fe.snapCh:
				startTs := time.Now()

				// バックプレッシャー監視
				CheckBackpressure("tradeCh_"+fe.Symbol, len(fe.tradeCh), cap(fe.tradeCh))
				CheckBackpressure("snapCh_"+fe.Symbol, len(fe.snapCh), cap(fe.snapCh))

				// 1. シグナル生成 (Microprice × OrderFlow Imbalance)
				sig, mp := fe.signal.GenerateEnhanced(
					snap.Ts, snap.Price, snap.Bid, snap.Ask,
					snap.RecentHigh, snap.RecentLow,
					fe.hawkes.LamBuy, fe.hawkes.LamSell,
				)

				if sig != Flat {
					// 2. ポジションサイジング (Risk Budget × ATR × MarketProfile)
					size := ComputeSize(fe.balanceJpy, fe.riskBudget, snap.ATR, fe.Profile)

					// 3. 発注構築 & ノンブロッキング投入 (ガバナンス遮断判定内蔵)
					order := fe.execution.BuildOrder(
						snap.Symbol, sig, snap.Bid, snap.Ask, mp, size,
						(fe.hawkes.LamBuy-fe.hawkes.LamSell)/(fe.hawkes.LamBuy+fe.hawkes.LamSell+1e-9),
						fe.hawkes.LamBuy, fe.hawkes.LamSell,
					)
					sigToOrdUs := float64(time.Since(startTs).Nanoseconds()) / 1000.0

					submitted := fe.execution.Submit(order)
					if submitted {
						// 4. 即時約定シミュレーション & フィードバック
						fillTs := time.Now()
						ordToFillUs := float64(time.Since(fillTs).Nanoseconds()) / 1000.0
						fe.metrics.RecordLatency(sigToOrdUs, ordToFillUs)

						slip := (snap.Ask - order.Price)
						pnl := 150.0 // 仮想約定PnL
						fe.execution.tuner.UpdateFromFill(slip)
						fe.metrics.RecordTrade(pnl, slip, true)
					}
				}
			}
		}
	})
}

func (fe *FusionEngine) Stop() {
	close(fe.doneCh)
	fe.wg.Wait()
}

// ProcessTickFast: ベンチマーク用の極限インライン処理パス (ナノ秒計測用)
func (fe *FusionEngine) ProcessTickFast(t Trade, snap MarketSnapshot) (Signal, *Order) {
	// 1. Order Flow更新
	fe.rb.Add(t)

	// 2. Hawkes強度再帰更新 (O(1))
	fe.hawkes.Update(t.Ts, t.Side)

	// 3. シグナル生成
	sig, mp := fe.signal.GenerateEnhanced(
		snap.Ts, snap.Price, snap.Bid, snap.Ask,
		snap.RecentHigh, snap.RecentLow,
		fe.hawkes.LamBuy, fe.hawkes.LamSell,
	)

	if sig == Flat {
		return Flat, nil
	}

	// 4. ポジションサイジング
	size := ComputeSize(fe.balanceJpy, fe.riskBudget, snap.ATR, fe.Profile)

	// 5. 発注構築
	order := fe.execution.BuildOrder(
		snap.Symbol, sig, snap.Bid, snap.Ask, mp, size,
		(fe.hawkes.LamBuy-fe.hawkes.LamSell)/(fe.hawkes.LamBuy+fe.hawkes.LamSell+1e-9),
		fe.hawkes.LamBuy, fe.hawkes.LamSell,
	)

	return sig, &order
}
