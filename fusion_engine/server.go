package main

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"runtime"
	"sync"
	"time"
)

// HTTPServer: Prometheus メトリクス配信 & REST ヘルスチェックサーバー
type HTTPServer struct {
	port   int
	engine *FusionEngine
	multi  *MultiFusionEngine
	server *http.Server
	mu     sync.Mutex
}

func NewHTTPServer(port int, engine *FusionEngine) *HTTPServer {
	return &HTTPServer{
		port:   port,
		engine: engine,
	}
}

func NewMultiHTTPServer(port int, multi *MultiFusionEngine) *HTTPServer {
	return &HTTPServer{
		port:   port,
		engine: multi.PrimaryEngine(),
		multi:  multi,
	}
}

func (s *HTTPServer) Start() {
	mux := http.NewServeMux()
	mux.HandleFunc("/metrics", s.handleMetrics)
	mux.HandleFunc("/health", s.handleHealth)
	mux.HandleFunc("/status", s.handleHealth)
	mux.HandleFunc("/governance", s.handleGovernance)

	addr := fmt.Sprintf(":%d", s.port)
	s.server = &http.Server{
		Addr:         addr,
		Handler:      mux,
		ReadTimeout:  5 * time.Second,
		WriteTimeout: 5 * time.Second,
	}

	log.Printf("[METRICS] 📊 Prometheus Exporter listening on http://0.0.0.0:%d/metrics", s.port)
	go func() {
		if err := s.server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Printf("[METRICS] Server error: %v", err)
		}
	}()
}

func (s *HTTPServer) Stop() {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.server != nil {
		_ = s.server.Close()
	}
}

func (s *HTTPServer) handleMetrics(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "text/plain; version=0.0.4")

	engines := make(map[string]*FusionEngine)
	if s.multi != nil {
		engines = s.multi.GetAllEngines()
	} else if s.engine != nil {
		engines[s.engine.Symbol] = s.engine
	}

	fmt.Fprintf(w, "# HELP fusion_latency_sig_to_ord_us Latency from signal generation to order submission in us\n")
	fmt.Fprintf(w, "# TYPE fusion_latency_sig_to_ord_us gauge\n")
	for sym, fe := range engines {
		fe.metrics.Latency.mu.RLock()
		avgS2O := calcMean(fe.metrics.Latency.SignalToOrderUs)
		fe.metrics.Latency.mu.RUnlock()
		fmt.Fprintf(w, "fusion_latency_sig_to_ord_us{symbol=\"%s\"} %.2f\n", sym, avgS2O)
	}
	fmt.Fprintf(w, "\n")

	fmt.Fprintf(w, "# HELP fusion_latency_ord_to_fill_us Latency from order to fill report in us\n")
	fmt.Fprintf(w, "# TYPE fusion_latency_ord_to_fill_us gauge\n")
	for sym, fe := range engines {
		fe.metrics.Latency.mu.RLock()
		avgO2F := calcMean(fe.metrics.Latency.OrderToFillUs)
		fe.metrics.Latency.mu.RUnlock()
		fmt.Fprintf(w, "fusion_latency_ord_to_fill_us{symbol=\"%s\"} %.2f\n", sym, avgO2F)
	}
	fmt.Fprintf(w, "\n")

	fmt.Fprintf(w, "# HELP fusion_pnl_total_jpy Accumulated profit and loss in JPY\n")
	fmt.Fprintf(w, "# TYPE fusion_pnl_total_jpy gauge\n")
	for sym, fe := range engines {
		fe.metrics.Accuracy.mu.RLock()
		pnl := fe.metrics.Accuracy.TotalPnL
		fe.metrics.Accuracy.mu.RUnlock()
		fmt.Fprintf(w, "fusion_pnl_total_jpy{symbol=\"%s\"} %.2f\n", sym, pnl)
	}
	fmt.Fprintf(w, "\n")

	fmt.Fprintf(w, "# HELP fusion_hit_rate_pct Signal accuracy hit rate percentage\n")
	fmt.Fprintf(w, "# TYPE fusion_hit_rate_pct gauge\n")
	for sym, fe := range engines {
		fe.metrics.Accuracy.mu.RLock()
		hitRate := 0.0
		if fe.metrics.Accuracy.SignalCount > 0 {
			hitRate = float64(fe.metrics.Accuracy.HitCount) / float64(fe.metrics.Accuracy.SignalCount) * 100.0
		}
		fe.metrics.Accuracy.mu.RUnlock()
		fmt.Fprintf(w, "fusion_hit_rate_pct{symbol=\"%s\"} %.2f\n", sym, hitRate)
	}
	fmt.Fprintf(w, "\n")

	fmt.Fprintf(w, "# HELP fusion_signals_total Total signals generated\n")
	fmt.Fprintf(w, "# TYPE fusion_signals_total counter\n")
	for sym, fe := range engines {
		fe.metrics.Accuracy.mu.RLock()
		sigCount := fe.metrics.Accuracy.SignalCount
		fe.metrics.Accuracy.mu.RUnlock()
		fmt.Fprintf(w, "fusion_signals_total{symbol=\"%s\"} %d\n", sym, sigCount)
	}
	fmt.Fprintf(w, "\n")

	fmt.Fprintf(w, "# HELP fusion_hawkes_lambda_buy Hawkes buy intensity\n")
	fmt.Fprintf(w, "# TYPE fusion_hawkes_lambda_buy gauge\n")
	for sym, fe := range engines {
		fmt.Fprintf(w, "fusion_hawkes_lambda_buy{symbol=\"%s\"} %.4f\n", sym, fe.hawkes.LamBuy)
	}
	fmt.Fprintf(w, "\n")

	fmt.Fprintf(w, "# HELP fusion_hawkes_lambda_sell Hawkes sell intensity\n")
	fmt.Fprintf(w, "# TYPE fusion_hawkes_lambda_sell gauge\n")
	for sym, fe := range engines {
		fmt.Fprintf(w, "fusion_hawkes_lambda_sell{symbol=\"%s\"} %.4f\n", sym, fe.hawkes.LamSell)
	}
	fmt.Fprintf(w, "\n")

	fmt.Fprintf(w, "# HELP fusion_governance_halted 1 if trading physically halted, 0 otherwise\n")
	fmt.Fprintf(w, "# TYPE fusion_governance_halted gauge\n")
	for sym, fe := range engines {
		gov := fe.execution.GetGovernance()
		haltVal := 0.0
		if gov.IsHalted {
			haltVal = 1.0
		}
		fmt.Fprintf(w, "fusion_governance_halted{symbol=\"%s\"} %.0f\n", sym, haltVal)
	}
	fmt.Fprintf(w, "\n")

	fmt.Fprintf(w, "# HELP fusion_governance_risk_multiplier Current risk multiplier\n")
	fmt.Fprintf(w, "# TYPE fusion_governance_risk_multiplier gauge\n")
	for sym, fe := range engines {
		gov := fe.execution.GetGovernance()
		fmt.Fprintf(w, "fusion_governance_risk_multiplier{symbol=\"%s\"} %.2f\n", sym, gov.RiskMultiplier)
	}
	fmt.Fprintf(w, "\n")

	fmt.Fprintf(w, "# HELP fusion_orders_sent Total orders submitted\n")
	fmt.Fprintf(w, "# TYPE fusion_orders_sent counter\n")
	for sym, fe := range engines {
		sent, _, _ := fe.execution.GetStats()
		fmt.Fprintf(w, "fusion_orders_sent{symbol=\"%s\"} %d\n", sym, sent)
	}
	fmt.Fprintf(w, "\n")

	fmt.Fprintf(w, "# HELP fusion_orders_halted Total orders blocked by governance halt\n")
	fmt.Fprintf(w, "# TYPE fusion_orders_halted counter\n")
	for sym, fe := range engines {
		_, halted, _ := fe.execution.GetStats()
		fmt.Fprintf(w, "fusion_orders_halted{symbol=\"%s\"} %d\n", sym, halted)
	}
	fmt.Fprintf(w, "\n")

	fmt.Fprintf(w, "# HELP fusion_goroutines Total goroutines running\n")
	fmt.Fprintf(w, "# TYPE fusion_goroutines gauge\n")
	fmt.Fprintf(w, "fusion_goroutines %d\n\n", runtime.NumGoroutine())
}

func (s *HTTPServer) handleHealth(w http.ResponseWriter, r *http.Request) {
	engines := make(map[string]*FusionEngine)
	if s.multi != nil {
		engines = s.multi.GetAllEngines()
	} else if s.engine != nil {
		engines[s.engine.Symbol] = s.engine
	}

	assetDetails := make(map[string]interface{})
	allHalted := true
	var totalPnL float64
	var totalSent, totalHalted, totalDropped int64

	for sym, fe := range engines {
		gov := fe.execution.GetGovernance()
		if !gov.IsHalted {
			allHalted = false
		}
		sent, halted, dropped := fe.execution.GetStats()
		totalSent += sent
		totalHalted += halted
		totalDropped += dropped

		fe.metrics.Accuracy.mu.RLock()
		pnl := fe.metrics.Accuracy.TotalPnL
		sigCount := fe.metrics.Accuracy.SignalCount
		hitCount := fe.metrics.Accuracy.HitCount
		fe.metrics.Accuracy.mu.RUnlock()
		totalPnL += pnl

		hitRate := 0.0
		if sigCount > 0 {
			hitRate = float64(hitCount) / float64(sigCount) * 100.0
		}

		assetDetails[sym] = map[string]interface{}{
			"governance":     gov,
			"total_pnl_jpy":  pnl,
			"hit_rate_pct":   hitRate,
			"signals_count":  sigCount,
			"orders_sent":    sent,
			"orders_halted":  halted,
			"orders_dropped": dropped,
			"lam_buy":        fe.hawkes.LamBuy,
			"lam_sell":       fe.hawkes.LamSell,
		}
	}

	overallStatus := "healthy"
	if allHalted && len(engines) > 0 {
		overallStatus = "halted"
	}

	resp := map[string]interface{}{
		"status":         overallStatus,
		"total_assets":   len(engines),
		"total_pnl_jpy":  totalPnL,
		"orders_sent":    totalSent,
		"orders_halted":  totalHalted,
		"orders_dropped": totalDropped,
		"goroutines":     runtime.NumGoroutine(),
		"assets":         assetDetails,
		"timestamp":      float64(time.Now().UnixNano()) / 1e9,
	}

	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(resp)
}

func (s *HTTPServer) handleGovernance(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method Not Allowed", http.StatusMethodNotAllowed)
		return
	}

	var req struct {
		Symbol         string  `json:"symbol"`
		Command        string  `json:"command"`
		TargetMode     string  `json:"target_mode"`
		RiskMultiplier float64 `json:"risk_multiplier"`
		Reason         string  `json:"reason"`
	}

	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}

	applyGov := func(mode string, halted bool, risk float64, reason string) {
		if s.multi != nil && req.Symbol == "" {
			s.multi.router.SetGovernanceAll(mode, halted, risk, reason)
		} else if s.multi != nil && req.Symbol != "" {
			s.multi.router.SetGovernance(req.Symbol, mode, halted, risk, reason)
		} else if s.engine != nil {
			s.engine.execution.SetGovernance(mode, halted, risk, reason)
		}
	}

	switch req.Command {
	case "STOP":
		applyGov("STOP", true, 0.0, req.Reason)
	case "REDUCE_50":
		applyGov("REDUCE_50", false, 0.5, req.Reason)
	case "RESUME":
		risk := req.RiskMultiplier
		if risk <= 0 {
			risk = 1.0
		}
		mode := req.TargetMode
		if mode == "" {
			mode = "HYBRID"
		}
		applyGov(mode, false, risk, req.Reason)
	default:
		http.Error(w, "Unknown command", http.StatusBadRequest)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]interface{}{
		"status":  "OK",
		"message": fmt.Sprintf("Command %s applied", req.Command),
	})
}
