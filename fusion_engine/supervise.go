package main

import (
	"log"
	"runtime/debug"
	"sync"
	"sync/atomic"
	"time"
)

// Supervisor: Goroutineの死活監視・自動再起動（Self-Healing）とバックプレッシャー検知
type Supervisor struct {
	restarts   map[string]*int64
	mu         sync.RWMutex
	maxRetries int
}

var globalSupervisor = NewSupervisor(10)

func NewSupervisor(maxRetries int) *Supervisor {
	return &Supervisor{
		restarts:   make(map[string]*int64),
		maxRetries: maxRetries,
	}
}

// Supervise: パニックを捕捉し、クラッシュを防いで自動リカバリするセーフガード実行
func (s *Supervisor) Supervise(name string, fn func()) {
	s.mu.Lock()
	counter, exists := s.restarts[name]
	if !exists {
		var c int64
		counter = &c
		s.restarts[name] = counter
	}
	s.mu.Unlock()

	go func() {
		defer func() {
			if r := recover(); r != nil {
				cnt := atomic.AddInt64(counter, 1)
				log.Printf("[SUPERVISOR] ⚠️ CRITICAL: Goroutine [%s] panicked: %v (Restart count: %d)\nStack:\n%s",
					name, r, cnt, string(debug.Stack()))

				if int(cnt) <= s.maxRetries {
					time.Sleep(100 * time.Millisecond) // 再起動スロットリング
					log.Printf("[SUPERVISOR] 🔄 Re-spawning Goroutine [%s]...", name)
					s.Supervise(name, fn)
				} else {
					log.Printf("[SUPERVISOR] 🛑 Goroutine [%s] exceeded max retries (%d). Halting worker.", name, s.maxRetries)
				}
			}
		}()

		fn()
	}()
}

// GetRestartCount: 対象ワーカーの再起動回数取得
func (s *Supervisor) GetRestartCount(name string) int64 {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if c, ok := s.restarts[name]; ok {
		return atomic.LoadInt64(c)
	}
	return 0
}

// CheckBackpressure: チャネル詰まり（Backpressure）の監視 (80%超で警告)
func CheckBackpressure(name string, chLen, chCap int) float64 {
	if chCap <= 0 {
		return 0.0
	}
	ratio := float64(chLen) / float64(chCap)
	if ratio >= 0.8 {
		log.Printf("[BACKPRESSURE] ⚠️ Channel [%s] congestion warning: %d/%d (%.1f%%)",
			name, chLen, chCap, ratio*100)
	}
	return ratio
}
