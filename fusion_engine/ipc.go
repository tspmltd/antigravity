package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net"
	"os"
	"runtime"
	"sync"
	"time"
)

// IPCCommand: Python (Regime Orchestrator) から送られるガバナンス/状態問い合わせ命令
type IPCCommand struct {
	Command        string  `json:"command"` // "STOP", "REDUCE_50", "RESUME", "SET_MODE", "GET_STATE", "PING"
	TargetMode     string  `json:"target_mode,omitempty"`
	RiskMultiplier float64 `json:"risk_multiplier,omitempty"`
	Reason         string  `json:"reason,omitempty"`
	Symbol         string  `json:"symbol,omitempty"`
}

// IPCResponse: Go Fusion Engine から Python 側へ即時返却するテレメトリ
type IPCResponse struct {
	Status     string                 `json:"status"`
	Message    string                 `json:"message"`
	Governance GovernanceState        `json:"governance"`
	Metrics    map[string]interface{} `json:"metrics,omitempty"`
	Timestamp  float64                `json:"timestamp"`
}

// IPCServer: Unix Domain Socket を通じた超高速プロセス間通信サーバー
type IPCServer struct {
	sockPath string
	listener net.Listener
	engine   *FusionEngine
	multi    *MultiFusionEngine
	mu       sync.Mutex
	running  bool
	doneCh   chan struct{}
}

func NewIPCServer(sockPath string, engine *FusionEngine) *IPCServer {
	return &IPCServer{
		sockPath: sockPath,
		engine:   engine,
		doneCh:   make(chan struct{}),
	}
}

func NewMultiIPCServer(sockPath string, multi *MultiFusionEngine) *IPCServer {
	return &IPCServer{
		sockPath: sockPath,
		engine:   multi.PrimaryEngine(),
		multi:    multi,
		doneCh:   make(chan struct{}),
	}
}

// Start: UDSリスナーを起動し、Python司令塔からの命令を待受
func (s *IPCServer) Start() error {
	s.mu.Lock()
	defer s.mu.Unlock()

	// 古いソケットファイルが存在する場合は削除
	if err := os.RemoveAll(s.sockPath); err != nil {
		return fmt.Errorf("failed to unlink old socket: %w", err)
	}

	l, err := net.Listen("unix", s.sockPath)
	if err != nil {
		return fmt.Errorf("failed to listen on unix socket %s: %w", s.sockPath, err)
	}
	// パーミッションを0666に設定（同一ホストのPythonプロセスからの接続許可）
	if err := os.Chmod(s.sockPath, 0666); err != nil {
		log.Printf("[IPC] ⚠️ Warning chmod socket: %v", err)
	}

	s.listener = l
	s.running = true
	log.Printf("[IPC] 🚀 Unix Domain Socket Server listening at: %s", s.sockPath)

	go s.acceptLoop()
	return nil
}

func (s *IPCServer) acceptLoop() {
	for {
		conn, err := s.listener.Accept()
		if err != nil {
			select {
			case <-s.doneCh:
				return
			default:
				log.Printf("[IPC] Accept error: %v", err)
				continue
			}
		}

		go s.handleConnection(conn)
	}
}

func (s *IPCServer) handleConnection(conn net.Conn) {
	defer conn.Close()
	reader := bufio.NewReader(conn)

	for {
		_ = conn.SetReadDeadline(time.Now().Add(10 * time.Second))
		line, err := reader.ReadBytes('\n')
		if err != nil {
			if err != io.EOF {
				// 切断またはタイムアウト
			}
			return
		}

		var cmd IPCCommand
		if err := json.Unmarshal(line, &cmd); err != nil {
			resp := IPCResponse{
				Status:    "ERROR",
				Message:   fmt.Sprintf("Invalid JSON: %v", err),
				Timestamp: float64(time.Now().UnixNano()) / 1e9,
			}
			_ = sendIPCResponse(conn, resp)
			continue
		}

		resp := s.executeCommand(cmd)
		if err := sendIPCResponse(conn, resp); err != nil {
			return
		}
	}
}

func (s *IPCServer) executeCommand(cmd IPCCommand) IPCResponse {
	now := float64(time.Now().UnixNano()) / 1e9

	// 対象エンジンの決定 (特定シンボルまたはプライマリ)
	targetEngine := s.engine
	if s.multi != nil && cmd.Symbol != "" {
		if eng := s.multi.GetEngine(cmd.Symbol); eng != nil {
			targetEngine = eng
		}
	}
	exec := targetEngine.execution

	applyGov := func(mode string, halted bool, risk float64, reason string) {
		if s.multi != nil && cmd.Symbol == "" {
			// 全アセット一括ガバナンス適用
			s.multi.router.SetGovernanceAll(mode, halted, risk, reason)
		} else {
			exec.SetGovernance(mode, halted, risk, reason)
		}
	}

	switch cmd.Command {
	case "STOP":
		reason := cmd.Reason
		if reason == "" {
			reason = "CRITICAL EMERGENCY STOP INGESTED VIA IPC"
		}
		applyGov("STOP", true, 0.0, reason)
		log.Printf("[IPC] 🛑 [CRITICAL] STOP applied! (Symbol=%s, Reason=%s)", cmd.Symbol, reason)

	case "REDUCE_50":
		reason := cmd.Reason
		if reason == "" {
			reason = "RISK_OFF REDUCE_50 VIA IPC"
		}
		applyGov("REDUCE_50", false, 0.5, reason)
		log.Printf("[IPC] ⚠️ REDUCE_50 applied! (Symbol=%s, Reason=%s)", cmd.Symbol, reason)

	case "RESUME":
		mode := cmd.TargetMode
		if mode == "" {
			mode = "HYBRID"
		}
		risk := cmd.RiskMultiplier
		if risk <= 0.0 {
			risk = 1.0
		}
		applyGov(mode, false, risk, "RESUMED VIA IPC: "+cmd.Reason)
		log.Printf("[IPC] ✅ RESUME applied! (Symbol=%s, Mode=%s, Risk=%.2f)", cmd.Symbol, mode, risk)

	case "SET_MODE":
		mode := cmd.TargetMode
		if mode == "" {
			mode = "HYBRID"
		}
		halted := (mode == "STOP")
		risk := cmd.RiskMultiplier
		if risk <= 0.0 {
			risk = 1.0
		}
		applyGov(mode, halted, risk, cmd.Reason)
		log.Printf("[IPC] ⚙️ Mode set: (Symbol=%s, Mode=%s, Halted=%v, Risk=%.2f)", cmd.Symbol, mode, halted, risk)

	case "PING", "GET_STATE":
		// 情報取得のみ
	default:
		return IPCResponse{
			Status:     "ERROR",
			Message:    fmt.Sprintf("Unknown command: %s", cmd.Command),
			Governance: exec.GetGovernance(),
			Timestamp:  now,
		}
	}

	sent, halted, dropped := exec.GetStats()
	gov := exec.GetGovernance()

	targetEngine.metrics.Accuracy.mu.RLock()
	totalPnL := targetEngine.metrics.Accuracy.TotalPnL
	sigCount := targetEngine.metrics.Accuracy.SignalCount
	hitCount := targetEngine.metrics.Accuracy.HitCount
	targetEngine.metrics.Accuracy.mu.RUnlock()

	hitRate := 0.0
	if sigCount > 0 {
		hitRate = float64(hitCount) / float64(sigCount) * 100.0
	}

	metrics := map[string]interface{}{
		"symbol":         targetEngine.Symbol,
		"total_pnl_jpy":  totalPnL,
		"hit_rate_pct":   hitRate,
		"orders_sent":    sent,
		"orders_halted":  halted,
		"orders_dropped": dropped,
		"lam_buy":        targetEngine.hawkes.LamBuy,
		"lam_sell":       targetEngine.hawkes.LamSell,
		"goroutines":     runtime.NumGoroutine(),
		"restart_counts": globalSupervisor.GetRestartCount("hawkes_pipeline"),
	}

	if s.multi != nil {
		allPnL, allSigs, allHits := s.multi.TotalStats()
		allSent, allHalted, allDropped := s.multi.router.GetStatsAll()
		metrics["multi_total_pnl_jpy"] = allPnL
		metrics["multi_orders_sent"] = allSent
		metrics["multi_orders_halted"] = allHalted
		metrics["multi_orders_dropped"] = allDropped
		metrics["multi_total_signals"] = allSigs
		metrics["multi_total_hits"] = allHits
	}

	return IPCResponse{
		Status:     "OK",
		Message:    fmt.Sprintf("Command %s processed successfully", cmd.Command),
		Governance: gov,
		Metrics:    metrics,
		Timestamp:  now,
	}
}

func sendIPCResponse(conn net.Conn, resp IPCResponse) error {
	data, err := json.Marshal(resp)
	if err != nil {
		return err
	}
	data = append(data, '\n')
	_, err = conn.Write(data)
	return err
}

func (s *IPCServer) Stop() {
	s.mu.Lock()
	defer s.mu.Unlock()
	if !s.running {
		return
	}
	s.running = false
	close(s.doneCh)
	if s.listener != nil {
		s.listener.Close()
	}
	_ = os.RemoveAll(s.sockPath)
	log.Printf("[IPC] 🛑 Unix Domain Socket Server stopped")
}
