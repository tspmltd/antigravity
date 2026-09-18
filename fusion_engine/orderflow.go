package main

import (
	"sync"
)

type Side int

const (
	Buy Side = iota
	Sell
)

func (s Side) String() string {
	if s == Buy {
		return "BUY"
	}
	return "SELL"
}

type Trade struct {
	Ts    float64 // Unix Timestamp (秒, 小数点以下マイクロ秒/ナノ秒)
	Side  Side    // Buy / Sell
	Size  float64 // 出来高 (ロット, 株数, BTC等)
	Price float64 // 約定価格
}

// RingBuffer: メモリ再確保・GCゼロの固定長循環バッファ
type RingBuffer struct {
	buf      []Trade
	size     int
	writeIdx int
	mu       sync.RWMutex
}

func NewRingBuffer(n int) *RingBuffer {
	return &RingBuffer{
		buf:      make([]Trade, n),
		size:     n,
		writeIdx: 0,
	}
}

// Add: O(1) で最新Tradeを上書き格納
func (r *RingBuffer) Add(t Trade) {
	r.mu.Lock()
	r.buf[r.writeIdx] = t
	r.writeIdx = (r.writeIdx + 1) % r.size
	r.mu.Unlock()
}

// Metrics: 指定時間窓 (windowSec) 内の Buy/Sell 出来高、NetFlow、Imbalance を高速逆走査
func (r *RingBuffer) Metrics(now, windowSec float64) (buyVol, sellVol, netFlow, imbalance float64, count int) {
	r.mu.RLock()
	defer r.mu.RUnlock()

	cutoff := now - windowSec
	idx := (r.writeIdx - 1 + r.size) % r.size

	for i := 0; i < r.size; i++ {
		e := r.buf[idx]
		if e.Ts == 0 || e.Ts < cutoff {
			break
		}
		if e.Side == Buy {
			buyVol += e.Size
		} else {
			sellVol += e.Size
		}
		count++
		idx = (idx - 1 + r.size) % r.size
	}

	netFlow = buyVol - sellVol
	denom := buyVol + sellVol
	if denom > 0 {
		imbalance = netFlow / denom
	}
	return
}
