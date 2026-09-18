package main

import (
	"bufio"
	"encoding/json"
	"net"
	"os"
	"sync/atomic"
	"testing"
	"time"
)

func TestRingBuffer(t *testing.T) {
	rb := NewRingBuffer(10)
	now := 1000.0

	rb.Add(Trade{Ts: now - 0.05, Side: Buy, Size: 100, Price: 155.0})
	rb.Add(Trade{Ts: now - 0.02, Side: Buy, Size: 200, Price: 155.1})
	rb.Add(Trade{Ts: now - 0.01, Side: Sell, Size: 50, Price: 155.05})

	buyVol, sellVol, netFlow, imb, count := rb.Metrics(now, 0.1)
	if count != 3 {
		t.Fatalf("expected count 3, got %d", count)
	}
	if buyVol != 300.0 {
		t.Fatalf("expected buyVol 300, got %.1f", buyVol)
	}
	if sellVol != 50.0 {
		t.Fatalf("expected sellVol 50, got %.1f", sellVol)
	}
	if netFlow != 250.0 {
		t.Fatalf("expected netFlow 250, got %.1f", netFlow)
	}
	expectedImb := 250.0 / 350.0
	if imb < expectedImb-1e-5 || imb > expectedImb+1e-5 {
		t.Fatalf("expected imb %.4f, got %.4f", expectedImb, imb)
	}
}

func TestHawkesEngine(t *testing.T) {
	p := TokyoParams
	h := NewHawkesEngine(Tokyo, p)

	if h.LamBuy != p.MuBuy || h.LamSell != p.MuSell {
		t.Fatalf("initial intensities not equal to baseline")
	}

	h.Update(1.0, Buy)
	if h.LamBuy <= p.MuBuy {
		t.Fatalf("expected LamBuy to increase after Buy event")
	}

	mp := MicroPrice(155.00, 155.02, 20.0, 10.0)
	// LamBuy > LamSell -> microprice closer to ask
	if mp <= 155.01 {
		t.Fatalf("microprice should be skewed toward ask, got %.4f", mp)
	}
}

func TestPositionSizing(t *testing.T) {
	balance := 10_000_000.0 // 1,000万円
	budget := 0.005        // 5万円

	// USD/JPY
	sizeUSD := ComputeSize(balance, budget, 0.15, USDJPYProfile)
	if sizeUSD < USDJPYProfile.MinSize || sizeUSD > USDJPYProfile.MaxSize {
		t.Fatalf("USDJPY size out of bounds: %.1f", sizeUSD)
	}

	// 7203 (単元株制100株単位)
	sizeJP := ComputeSize(balance, budget, 20.0, JP7203Profile)
	if int(sizeJP)%100 != 0 {
		t.Fatalf("JP stock size must be multiple of 100, got %.1f", sizeJP)
	}
}

func TestExecutionGovernanceHalt(t *testing.T) {
	ee := NewExecutionEngine(100)

	ord := Order{
		ClientOrderId: "ord_test_1",
		Symbol:        "USDJPY",
		Side:          Buy,
		Type:          Limit,
		Price:         155.0,
		Size:          10000.0,
	}

	// 1. 通常発注 -> 成功
	if !ee.Submit(ord) {
		t.Fatalf("expected order to be submitted")
	}

	// 2. STOP 命令発令 -> 物理遮断
	ee.SetGovernance("STOP", true, 0.0, "TEST CRITICAL SHOCK")
	ord.ClientOrderId = "ord_test_2"
	if ee.Submit(ord) {
		t.Fatalf("expected order to be physically blocked by STOP")
	}

	sent, halted, _ := ee.GetStats()
	if sent != 1 || halted != 1 {
		t.Fatalf("expected sent=1, halted=1, got sent=%d, halted=%d", sent, halted)
	}

	// 3. REDUCE_50 -> サイズ半減
	ee.SetGovernance("REDUCE_50", false, 0.5, "TEST RISK OFF")
	ord.ClientOrderId = "ord_test_3"
	ord.Size = 10000.0
	if !ee.Submit(ord) {
		t.Fatalf("expected order to be submitted in REDUCE_50")
	}
}

func TestSupervisorPanicRecovery(t *testing.T) {
	s := NewSupervisor(3)
	var executed int64

	s.Supervise("panicking_worker", func() {
		cnt := atomic.AddInt64(&executed, 1)
		if cnt == 1 {
			panic("intentional test panic")
		}
	})

	// 少し待機してリカバリを確認
	time.Sleep(200 * time.Millisecond)

	finalExec := atomic.LoadInt64(&executed)
	if finalExec < 2 {
		t.Fatalf("expected worker to be restarted after panic, got %d executions", finalExec)
	}
	restarts := s.GetRestartCount("panicking_worker")
	if restarts < 1 {
		t.Fatalf("expected at least 1 restart counted, got %d", restarts)
	}
}

func TestIPCServerCommunication(t *testing.T) {
	sockPath := "/tmp/test_fusion_engine.sock"
	_ = os.Remove(sockPath)

	eng := NewFusionEngine(USDJPYProfile, 10_000_000, 0.005, 100)
	srv := NewIPCServer(sockPath, eng)
	if err := srv.Start(); err != nil {
		t.Fatalf("failed to start IPC server: %v", err)
	}
	defer srv.Stop()

	// クライアント接続
	conn, err := net.Dial("unix", sockPath)
	if err != nil {
		t.Fatalf("dial error: %v", err)
	}
	defer conn.Close()

	reader := bufio.NewReader(conn)

	// 1. PING 送信
	pingCmd := `{"command": "PING"}` + "\n"
	_, _ = conn.Write([]byte(pingCmd))
	line, err := reader.ReadBytes('\n')
	if err != nil {
		t.Fatalf("read error: %v", err)
	}

	var resp IPCResponse
	if err := json.Unmarshal(line, &resp); err != nil {
		t.Fatalf("unmarshal error: %v", err)
	}
	if resp.Status != "OK" {
		t.Fatalf("expected status OK, got %s", resp.Status)
	}

	// 2. STOP 送信
	stopCmd := `{"command": "STOP", "reason": "TEST STOP VIA UDS"}` + "\n"
	_, _ = conn.Write([]byte(stopCmd))
	line, err = reader.ReadBytes('\n')
	if err != nil {
		t.Fatalf("read stop response error: %v", err)
	}
	var stopResp IPCResponse
	_ = json.Unmarshal(line, &stopResp)
	if !stopResp.Governance.IsHalted {
		t.Fatalf("expected IsHalted to be true after STOP command")
	}
}

func TestExecRouterAndMultiAsset(t *testing.T) {
	multi := NewMultiFusionEngine()

	engUSD := NewFusionEngine(USDJPYProfile, 10_000_000, 0.005, 100)
	engBTC := NewFusionEngine(BTCJPYProfile, 10_000_000, 0.005, 100)
	engJP := NewFusionEngine(JP7203Profile, 10_000_000, 0.005, 100)

	multi.AddEngine(engUSD)
	multi.AddEngine(engBTC)
	multi.AddEngine(engJP)

	if len(multi.GetAllEngines()) != 3 {
		t.Fatalf("expected 3 engines, got %d", len(multi.GetAllEngines()))
	}

	// 1. 各アセットへのルーティング発注
	ordUSD := Order{ClientOrderId: "u1", Symbol: "USDJPY", Size: 10000}
	ordBTC := Order{ClientOrderId: "b1", Symbol: "BTCJPY", Size: 0.1}
	ordJP := Order{ClientOrderId: "j1", Symbol: "7203", Size: 100}
	ordUnknown := Order{ClientOrderId: "x1", Symbol: "UNKNOWN", Size: 1}

	if !multi.router.Submit("USDJPY", ordUSD) {
		t.Fatalf("failed to submit USDJPY order via router")
	}
	if !multi.router.Submit("BTCJPY", ordBTC) {
		t.Fatalf("failed to submit BTCJPY order via router")
	}
	if !multi.router.Submit("7203", ordJP) {
		t.Fatalf("failed to submit 7203 order via router")
	}
	if multi.router.Submit("UNKNOWN", ordUnknown) {
		t.Fatalf("expected UNKNOWN order to be rejected")
	}

	sent, halted, _ := multi.router.GetStatsAll()
	if sent != 3 || halted != 0 {
		t.Fatalf("expected sent=3, halted=0, got sent=%d, halted=%d", sent, halted)
	}

	// 2. 一括ガバナンス停止 (STOP ALL)
	multi.router.SetGovernanceAll("STOP", true, 0.0, "MACRO SHOCK TEST")

	ordUSD2 := Order{ClientOrderId: "u2", Symbol: "USDJPY", Size: 10000}
	ordBTC2 := Order{ClientOrderId: "b2", Symbol: "BTCJPY", Size: 0.1}

	if multi.router.Submit("USDJPY", ordUSD2) {
		t.Fatalf("expected USDJPY order to be halted")
	}
	if multi.router.Submit("BTCJPY", ordBTC2) {
		t.Fatalf("expected BTCJPY order to be halted")
	}

	_, haltedAfter, _ := multi.router.GetStatsAll()
	if haltedAfter != 2 {
		t.Fatalf("expected halted=2, got %d", haltedAfter)
	}

	// 3. 特定アセットのみ個別再開 (RESUME USDJPY ONLY)
	multi.router.SetGovernance("USDJPY", "HYBRID", false, 1.0, "USD REOPEN")
	ordUSD3 := Order{ClientOrderId: "u3", Symbol: "USDJPY", Size: 10000}
	if !multi.router.Submit("USDJPY", ordUSD3) {
		t.Fatalf("expected USDJPY order to succeed after individual resume")
	}
	// BTCは停止のまま
	ordBTC3 := Order{ClientOrderId: "b3", Symbol: "BTCJPY", Size: 0.1}
	if multi.router.Submit("BTCJPY", ordBTC3) {
		t.Fatalf("expected BTCJPY order to still be halted")
	}

	// 4. Multi IPC サーバー検証
	sockPath := "/tmp/test_multi_fusion_engine.sock"
	_ = os.Remove(sockPath)
	ipcSrv := NewMultiIPCServer(sockPath, multi)
	if err := ipcSrv.Start(); err != nil {
		t.Fatalf("failed to start multi IPC server: %v", err)
	}
	defer ipcSrv.Stop()

	conn, err := net.Dial("unix", sockPath)
	if err != nil {
		t.Fatalf("dial error: %v", err)
	}
	defer conn.Close()

	reader := bufio.NewReader(conn)
	pingCmd := `{"command": "PING"}` + "\n"
	_, _ = conn.Write([]byte(pingCmd))
	line, err := reader.ReadBytes('\n')
	if err != nil {
		t.Fatalf("read error: %v", err)
	}
	var resp IPCResponse
	_ = json.Unmarshal(line, &resp)
	if resp.Status != "OK" {
		t.Fatalf("expected OK, got %s", resp.Status)
	}
	if resp.Metrics["multi_orders_sent"] == nil {
		t.Fatalf("expected multi_orders_sent in metrics")
	}
}

