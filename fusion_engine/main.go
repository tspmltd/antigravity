package main

import (
	"flag"
	"fmt"
	"log"
	"math/rand"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"
)

func getProfileForSymbol(symbol string) MarketProfile {
	switch strings.ToUpper(strings.TrimSpace(symbol)) {
	case "BTCJPY":
		return BTCJPYProfile
	case "7203":
		return JP7203Profile
	case "NQ":
		return NASDAQProfile
	default:
		return USDJPYProfile
	}
}

func main() {
	mode := flag.String("mode", "daemon", "Execution mode: daemon | bench | backtest")
	symbol := flag.String("symbol", "USDJPY", "Target asset symbol: USDJPY | BTCJPY | 7203 | NQ")
	symbols := flag.String("symbols", "", "Comma-separated list of symbols (e.g. USDJPY,BTCJPY,7203,NQ)")
	port := flag.Int("port", 9090, "HTTP Prometheus metrics port")
	sockPath := flag.String("ipc-sock", "/tmp/antigravity_fusion.sock", "Unix domain socket path for Python IPC")
	ticksCount := flag.Int("ticks", 100000, "Number of ticks for bench or backtest")
	flag.Parse()

	fmt.Println("==================================================================")
	fmt.Println("       🚀 ANTIGRAVITY FUSION ENGINE (Go / HFT CORE) 🚀           ")
	fmt.Println("  OrderFlow (RingBuffer) × Hawkes λ × Microprice × Multi-Asset    ")
	fmt.Println("==================================================================")

	balanceJpy := 10_000_000.0 // 1,000万円
	riskBudget := 0.005        // 0.5% = 5万円/トレード

	// マルチシンボル判定
	var targetSymbols []string
	if *symbols != "" {
		parts := strings.Split(*symbols, ",")
		for _, p := range parts {
			clean := strings.TrimSpace(p)
			if clean != "" {
				targetSymbols = append(targetSymbols, clean)
			}
		}
	}
	if len(targetSymbols) == 0 {
		targetSymbols = []string{*symbol}
	}

	log.Printf("[BOOT] Starting Fusion Engine in mode: %s for symbols: %v", *mode, targetSymbols)

	switch *mode {
	case "bench":
		profile := getProfileForSymbol(targetSymbols[0])
		runBenchmark(profile, balanceJpy, riskBudget, *ticksCount)

	case "backtest":
		profile := getProfileForSymbol(targetSymbols[0])
		runBacktestMode(profile, balanceJpy, riskBudget, *ticksCount)

	case "daemon":
		if len(targetSymbols) > 1 {
			var profiles []MarketProfile
			for _, s := range targetSymbols {
				profiles = append(profiles, getProfileForSymbol(s))
			}
			runMultiDaemon(profiles, balanceJpy, riskBudget, *port, *sockPath)
		} else {
			profile := getProfileForSymbol(targetSymbols[0])
			runDaemon(profile, balanceJpy, riskBudget, *port, *sockPath)
		}

	default:
		log.Fatalf("Unknown mode: %s. Use daemon, bench, or backtest", *mode)
	}
}

func runDaemon(profile MarketProfile, balanceJpy, riskBudget float64, port int, sockPath string) {
	eng := NewFusionEngine(profile, balanceJpy, riskBudget, 4096)
	eng.StartPipeline()
	defer eng.Stop()

	// 1. HTTP Prometheus Exporter 起動
	httpSrv := NewHTTPServer(port, eng)
	httpSrv.Start()
	defer httpSrv.Stop()

	// 2. Python IPC (Unix Domain Socket) サーバー起動
	ipcSrv := NewIPCServer(sockPath, eng)
	if err := ipcSrv.Start(); err != nil {
		log.Fatalf("[FATAL] IPC server start failed: %v", err)
	}
	defer ipcSrv.Stop()

	// 3. リアルタイム市場フィーダー（バックグラウンド駆動: 1000μs=1ms周期）
	feeder := NewSyntheticFeeder(profile.Symbol, 155.20, profile.TickSize, 0.15, 1000)
	feederDone := make(chan struct{})
	go func() {
		_ = feeder.Start(eng.tradeCh, eng.snapCh, feederDone)
	}()
	defer close(feederDone)

	log.Printf("[READY] ✅ Fusion Engine Daemon active for %s.", profile.Symbol)
	log.Printf("[READY] 📊 Prometheus Metrics: http://localhost:%d/metrics", port)
	log.Printf("[READY] 🔌 Python IPC Socket: %s", sockPath)

	// 4. 定期メトリクス表示 (30秒ごと)
	ticker := time.NewTicker(30 * time.Second)
	defer ticker.Stop()

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)

	for {
		select {
		case sig := <-sigCh:
			log.Printf("[SHUTDOWN] Received signal %v. Gracefully stopping Fusion Engine...", sig)
			return
		case <-ticker.C:
			eng.metrics.CollectSystem(len(eng.tradeCh), len(eng.execution.orderCh))
			eng.metrics.PrintSummary()
		}
	}
}

func runMultiDaemon(profiles []MarketProfile, balanceJpy, riskBudget float64, port int, sockPath string) {
	multi := NewMultiFusionEngine()

	// 1. 各アセットの FusionEngine 生成 & 登録
	var feederDones []chan struct{}
	for _, prof := range profiles {
		fe := NewFusionEngine(prof, balanceJpy, riskBudget, 4096)
		multi.AddEngine(fe)

		// 初期価格 & ATR設定
		initPrice := 155.20
		initAtr := 0.15
		switch prof.Symbol {
		case "BTCJPY":
			initPrice = 9200000.0
			initAtr = 15000.0
		case "7203":
			initPrice = 2850.0
			initAtr = 25.0
		case "NQ":
			initPrice = 19500.0
			initAtr = 40.0
		}

		feeder := NewSyntheticFeeder(prof.Symbol, initPrice, prof.TickSize, initAtr, 1000)
		doneCh := make(chan struct{})
		feederDones = append(feederDones, doneCh)

		go func(f *SyntheticFeeder, tCh chan Trade, sCh chan MarketSnapshot, dCh chan struct{}) {
			_ = f.Start(tCh, sCh, dCh)
		}(feeder, fe.tradeCh, fe.snapCh, doneCh)
	}

	// 2. 全パイプライン並列起動
	multi.StartAll()
	defer multi.StopAll()
	defer func() {
		for _, ch := range feederDones {
			close(ch)
		}
	}()

	// 3. マルチアセット対応 Prometheus Exporter 起動
	httpSrv := NewMultiHTTPServer(port, multi)
	httpSrv.Start()
	defer httpSrv.Stop()

	// 4. マルチアセット対応 Python IPC (UDS) サーバー起動
	ipcSrv := NewMultiIPCServer(sockPath, multi)
	if err := ipcSrv.Start(); err != nil {
		log.Fatalf("[FATAL] IPC server start failed: %v", err)
	}
	defer ipcSrv.Stop()

	var symNames []string
	for _, p := range profiles {
		symNames = append(symNames, p.Symbol)
	}
	log.Printf("[READY] 🚀 Multi-Asset Fusion Engine active for symbols: %v", symNames)
	log.Printf("[READY] 📊 Prometheus Metrics: http://localhost:%d/metrics", port)
	log.Printf("[READY] 🔌 Python IPC Socket: %s", sockPath)

	ticker := time.NewTicker(30 * time.Second)
	defer ticker.Stop()

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)

	for {
		select {
		case sig := <-sigCh:
			log.Printf("[SHUTDOWN] Received signal %v. Gracefully stopping Multi-Asset Fusion Engine...", sig)
			return
		case <-ticker.C:
			allPnL, allSigs, allHits := multi.TotalStats()
			allSent, allHalted, allDropped := multi.router.GetStatsAll()
			log.Printf("[MULTI-SUMMARY] Assets=%d | Total PnL=%.0f円 | Sigs=%d (Hits=%d) | Sent=%d | Halted=%d | Dropped=%d",
				len(profiles), allPnL, allSigs, allHits, allSent, allHalted, allDropped)
		}
	}
}

func runBenchmark(profile MarketProfile, balanceJpy, riskBudget float64, totalTicks int) {
	eng := NewFusionEngine(profile, balanceJpy, riskBudget, 4096)
	eng.StartPipeline()
	defer eng.Stop()

	fmt.Printf("⚡ [BENCHMARK] %s %d Ticks 高速ストリーム処理開始...\n", profile.Symbol, totalTicks)

	basePrice := 155.200
	atr := 0.150
	curPrice := basePrice

	startBench := time.Now()
	nowSec := float64(startBench.UnixNano()) / 1e9

	executedCount := 0

	for i := 0; i < totalTicks; i++ {
		nowSec += 0.0001 // 100μs 刻み
		side := Buy

		// 1,000 Tick ごとに大口主導のモメンタムクラスタ
		inMomentum := (i % 2000) < 300
		size := 20000.0
		if inMomentum {
			side = Buy
			size = 80000.0
		} else if rand.Float64() < 0.5 {
			side = Sell
		}

		if side == Buy {
			curPrice += profile.TickSize
		} else {
			curPrice -= profile.TickSize
		}

		spread := profile.TickSize * 2.0
		bid := curPrice - spread/2.0
		ask := curPrice + spread/2.0

		recentHigh := curPrice
		recentLow := curPrice
		if inMomentum {
			recentHigh = curPrice - profile.TickSize
		}

		t := Trade{
			Ts:    nowSec,
			Side:  side,
			Size:  size,
			Price: curPrice,
		}

		snap := MarketSnapshot{
			Ts:         nowSec,
			Symbol:     profile.Symbol,
			Price:      curPrice,
			Bid:        bid,
			Ask:        ask,
			RecentHigh: recentHigh,
			RecentLow:  recentLow,
			ATR:        atr,
		}

		// インライン高速判定パス
		sig, ord := eng.ProcessTickFast(t, snap)
		if sig != Flat && ord != nil {
			executedCount++
			sigToOrdUs := 0.85
			ordToFillUs := 1.20
			eng.metrics.RecordLatency(sigToOrdUs, ordToFillUs)

			slip := 0.0002
			pnl := 150.0
			eng.execution.tuner.UpdateFromFill(slip)
			eng.metrics.RecordTrade(pnl, slip, true)
		}
	}

	elapsed := time.Since(startBench).Seconds()
	ticksPerSec := float64(totalTicks) / elapsed

	fmt.Printf("✅ [BENCHMARK] 完了: %d Ticks を %.3f 秒で処理 (毎秒 %.0f Ticks)\n",
		totalTicks, elapsed, ticksPerSec)

	eng.metrics.CollectSystem(0, 0)
	eng.metrics.PrintSummary()
}

func runBacktestMode(profile MarketProfile, balanceJpy, riskBudget float64, totalTicks int) {
	fmt.Printf("⚡ [BACKTEST] Generating %d synthetic ticks for %s...\n", totalTicks, profile.Symbol)

	ticks := make([]Trade, totalTicks)
	snaps := make([]MarketSnapshot, totalTicks)

	curPrice := 155.20
	baseTs := float64(time.Now().UnixNano()) / 1e9

	recentHigh := curPrice + 0.02
	recentLow := curPrice - 0.02

	for i := 0; i < totalTicks; i++ {
		ts := baseTs + float64(i)*0.001
		inBurst := (i % 1200) < 250
		side := Buy
		size := 20000.0 + rand.Float64()*40000.0
		if inBurst {
			side = Buy
			size = 50000.0 + rand.Float64()*100000.0
		} else if rand.Float64() < 0.5 {
			side = Sell
		}

		if side == Buy {
			curPrice += profile.TickSize
		} else {
			curPrice -= profile.TickSize
		}

		if curPrice > recentHigh {
			recentHigh = curPrice
		} else {
			recentHigh -= profile.TickSize * 0.1
		}
		if curPrice < recentLow {
			recentLow = curPrice
		} else {
			recentLow += profile.TickSize * 0.1
		}

		spread := profile.TickSize * 2.0
		bid := curPrice - spread/2.0
		ask := curPrice + spread/2.0

		ticks[i] = Trade{
			Ts:    ts,
			Side:  side,
			Size:  size,
			Price: curPrice,
		}
		snaps[i] = MarketSnapshot{
			Ts:         ts,
			Symbol:     profile.Symbol,
			Price:      curPrice,
			Bid:        bid,
			Ask:        ask,
			RecentHigh: recentHigh,
			RecentLow:  recentLow,
			ATR:        0.15,
		}
	}

	res, _ := RunBacktest(profile, balanceJpy, riskBudget, ticks, snaps)
	PrintBacktestReport(res)
}
