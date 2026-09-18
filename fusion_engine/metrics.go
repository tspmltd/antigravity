package main

import (
	"fmt"
	"math"
	"runtime"
	"sync"
)

type LatencyMetrics struct {
	SignalToOrderUs []float64 // マイクロ秒 (μs)
	OrderToFillUs   []float64 // マイクロ秒 (μs)
	mu              sync.RWMutex
}

type AccuracyMetrics struct {
	SignalCount int
	HitCount    int
	TotalPnL    float64
	mu          sync.RWMutex
}

type RiskMetrics struct {
	AvgSlippage float64
	MaxSlippage float64
	MaxDrawdown float64
	PeakEquity  float64
	CurrentEquity float64
	mu          sync.RWMutex
}

type SystemMetrics struct {
	Goroutines  int
	OFDepth     int
	ExecDepth   int
	GCCount     uint32
	GCTimeTotal float64
	mu          sync.RWMutex
}

type FusionMetrics struct {
	Latency  *LatencyMetrics
	Accuracy *AccuracyMetrics
	Risk     *RiskMetrics
	System   *SystemMetrics
}

func NewFusionMetrics() *FusionMetrics {
	return &FusionMetrics{
		Latency:  &LatencyMetrics{SignalToOrderUs: make([]float64, 0, 10000), OrderToFillUs: make([]float64, 0, 10000)},
		Accuracy: &AccuracyMetrics{},
		Risk:     &RiskMetrics{},
		System:   &SystemMetrics{},
	}
}

func (m *FusionMetrics) RecordLatency(sigToOrdUs, ordToFillUs float64) {
	m.Latency.mu.Lock()
	defer m.Latency.mu.Unlock()
	m.Latency.SignalToOrderUs = append(m.Latency.SignalToOrderUs, sigToOrdUs)
	m.Latency.OrderToFillUs = append(m.Latency.OrderToFillUs, ordToFillUs)
}

func (m *FusionMetrics) RecordTrade(pnl float64, slippage float64, isHit bool) {
	m.Accuracy.mu.Lock()
	m.Accuracy.SignalCount++
	m.Accuracy.TotalPnL += pnl
	if isHit {
		m.Accuracy.HitCount++
	}
	m.Accuracy.mu.Unlock()

	m.Risk.mu.Lock()
	defer m.Risk.mu.Unlock()
	m.Risk.AvgSlippage = m.Risk.AvgSlippage*0.95 + slippage*0.05
	if math.Abs(slippage) > math.Abs(m.Risk.MaxSlippage) {
		m.Risk.MaxSlippage = slippage
	}
	m.Risk.CurrentEquity += pnl
	if m.Risk.CurrentEquity > m.Risk.PeakEquity {
		m.Risk.PeakEquity = m.Risk.CurrentEquity
	}
	dd := m.Risk.PeakEquity - m.Risk.CurrentEquity
	if dd > m.Risk.MaxDrawdown {
		m.Risk.MaxDrawdown = dd
	}
}

func (m *FusionMetrics) CollectSystem(ofLen, execLen int) {
	m.System.mu.Lock()
	defer m.System.mu.Unlock()
	m.System.Goroutines = runtime.NumGoroutine()
	m.System.OFDepth = ofLen
	m.System.ExecDepth = execLen

	var stats runtime.MemStats
	runtime.ReadMemStats(&stats)
	m.System.GCCount = stats.NumGC
	m.System.GCTimeTotal = float64(stats.PauseTotalNs) / 1e9
}

func (m *FusionMetrics) PrintSummary() {
	m.Latency.mu.RLock()
	avgS2O := calcMean(m.Latency.SignalToOrderUs)
	avgO2F := calcMean(m.Latency.OrderToFillUs)
	p99S2O := calcPercentile(m.Latency.SignalToOrderUs, 99)
	m.Latency.mu.RUnlock()

	m.Accuracy.mu.RLock()
	hitRate := 0.0
	if m.Accuracy.SignalCount > 0 {
		hitRate = float64(m.Accuracy.HitCount) / float64(m.Accuracy.SignalCount) * 100.0
	}
	totalPnL := m.Accuracy.TotalPnL
	sigCount := m.Accuracy.SignalCount
	m.Accuracy.mu.RUnlock()

	m.Risk.mu.RLock()
	avgSlip := m.Risk.AvgSlippage
	maxDD := m.Risk.MaxDrawdown
	m.Risk.mu.RUnlock()

	m.System.mu.RLock()
	routines := m.System.Goroutines
	gcCount := m.System.GCCount
	m.System.mu.RUnlock()

	fmt.Println("==================================================================")
	fmt.Println("        ⚡ FUSION ENGINE REAL-TIME PERFORMANCE METRICS ⚡         ")
	fmt.Println("==================================================================")
	fmt.Printf("⏱️  発注レイテンシ (Signal->Order) : 平均 %6.2f μs | p99 %6.2f μs\n", avgS2O, p99S2O)
	fmt.Printf("⏱️  約定レイテンシ (Order->Fill)   : 平均 %6.2f μs\n", avgO2F)
	fmt.Printf("🎯 シグナル的中率 (Hit Rate)      : %6.2f%% (%d / %d 回)\n", hitRate, int(hitRate*float64(sigCount)/100.0), sigCount)
	fmt.Printf("💰 累積損益 (Total PnL)           : %+10.1f 円\n", totalPnL)
	fmt.Printf("📉 リスク (AvgSlip / MaxDD)       : スリッページ %+.4f | 最大DD %.1f 円\n", avgSlip, maxDD)
	fmt.Printf("💻 システム健全性 (Goroutine/GC)  : %d Goroutines | GC %d 回\n", routines, gcCount)
	fmt.Println("------------------------------------------------------------------")
}

func calcMean(arr []float64) float64 {
	if len(arr) == 0 {
		return 0
	}
	sum := 0.0
	for _, v := range arr {
		sum += v
	}
	return sum / float64(len(arr))
}

func calcPercentile(arr []float64, p int) float64 {
	if len(arr) == 0 {
		return 0
	}
	// 簡易percentile
	idx := int(float64(len(arr)) * float64(p) / 100.0)
	if idx >= len(arr) {
		idx = len(arr) - 1
	}
	return arr[idx]
}
