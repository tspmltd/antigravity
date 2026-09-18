package main

import (
	"fmt"
	"sync"
	"sync/atomic"
)

// ExecRouter: 複数アセットの ExecutionEngine を一元管理・ルーティングするルーター
type ExecRouter struct {
	mu      sync.RWMutex
	engines map[string]*ExecutionEngine
}

func NewExecRouter() *ExecRouter {
	return &ExecRouter{
		engines: make(map[string]*ExecutionEngine),
	}
}

// Register: 銘柄ごとの ExecutionEngine を登録
func (r *ExecRouter) Register(symbol string, ee *ExecutionEngine) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.engines[symbol] = ee
}

// Submit: 指定銘柄の ExecutionEngine へ発注を投入
func (r *ExecRouter) Submit(symbol string, o Order) bool {
	r.mu.RLock()
	ee, exists := r.engines[symbol]
	r.mu.RUnlock()

	if !exists {
		fmt.Printf("[ROUTER] ❌ No ExecutionEngine found for symbol: %s\n", symbol)
		return false
	}
	return ee.Submit(o)
}

// SetGovernanceAll: 全アセットに一括でガバナンス命令を伝達 (STOP, REDUCE_50 等)
func (r *ExecRouter) SetGovernanceAll(targetMode string, isHalted bool, riskMultiplier float64, reason string) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	for _, ee := range r.engines {
		ee.SetGovernance(targetMode, isHalted, riskMultiplier, reason)
	}
}

// SetGovernance: 特定アセットのみにガバナンス命令を伝達
func (r *ExecRouter) SetGovernance(symbol string, targetMode string, isHalted bool, riskMultiplier float64, reason string) bool {
	r.mu.RLock()
	ee, exists := r.engines[symbol]
	r.mu.RUnlock()

	if !exists {
		return false
	}
	ee.SetGovernance(targetMode, isHalted, riskMultiplier, reason)
	return true
}

// GetStatsAll: 全アセット合計の発注統計を取得
func (r *ExecRouter) GetStatsAll() (sent, halted, dropped int64) {
	r.mu.RLock()
	defer r.mu.RUnlock()

	var totalSent, totalHalted, totalDropped int64
	for _, ee := range r.engines {
		s, h, d := ee.GetStats()
		totalSent += s
		totalHalted += h
		totalDropped += d
	}
	return totalSent, totalHalted, totalDropped
}

// MultiFusionEngine: 複数アセットの FusionEngine パイプラインを統括管理するコンテナ
type MultiFusionEngine struct {
	mu      sync.RWMutex
	engines map[string]*FusionEngine
	router  *ExecRouter
}

func NewMultiFusionEngine() *MultiFusionEngine {
	return &MultiFusionEngine{
		engines: make(map[string]*FusionEngine),
		router:  NewExecRouter(),
	}
}

// AddEngine: アセット別 FusionEngine を登録
func (m *MultiFusionEngine) AddEngine(fe *FusionEngine) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.engines[fe.Symbol] = fe
	m.router.Register(fe.Symbol, fe.execution)
}

// GetEngine: シンボル指定で FusionEngine を取得
func (m *MultiFusionEngine) GetEngine(symbol string) *FusionEngine {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.engines[symbol]
}

// GetAllEngines: 全ての FusionEngine を取得
func (m *MultiFusionEngine) GetAllEngines() map[string]*FusionEngine {
	m.mu.RLock()
	defer m.mu.RUnlock()
	copyMap := make(map[string]*FusionEngine, len(m.engines))
	for k, v := range m.engines {
		copyMap[k] = v
	}
	return copyMap
}

// StartAll: 全アセットのパイプラインを並列起動
func (m *MultiFusionEngine) StartAll() {
	m.mu.RLock()
	defer m.mu.RUnlock()
	for _, fe := range m.engines {
		fe.StartPipeline()
	}
}

// StopAll: 全アセットのパイプラインを安全停止
func (m *MultiFusionEngine) StopAll() {
	m.mu.RLock()
	defer m.mu.RUnlock()
	for _, fe := range m.engines {
		fe.Stop()
	}
}

// PrimaryEngine: デフォルトまたはプライマリアセット（1件目）を取得
func (m *MultiFusionEngine) PrimaryEngine() *FusionEngine {
	m.mu.RLock()
	defer m.mu.RUnlock()
	for _, fe := range m.engines {
		return fe
	}
	return nil
}

// TotalStats: 全アセットの統計集計
func (m *MultiFusionEngine) TotalStats() (totalPnL float64, totalSignals int64, totalHits int64) {
	m.mu.RLock()
	defer m.mu.RUnlock()
	for _, fe := range m.engines {
		fe.metrics.Accuracy.mu.RLock()
		totalPnL += fe.metrics.Accuracy.TotalPnL
		atomic.AddInt64(&totalSignals, int64(fe.metrics.Accuracy.SignalCount))
		atomic.AddInt64(&totalHits, int64(fe.metrics.Accuracy.HitCount))
		fe.metrics.Accuracy.mu.RUnlock()
	}
	return totalPnL, totalSignals, totalHits
}
