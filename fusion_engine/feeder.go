package main

import (
	"bufio"
	"fmt"
	"math/rand"
	"net"
	"os"
	"strconv"
	"strings"
	"syscall"
	"time"
)

// TickFeeder: 市場データ（約定・気配スナップショット）供給インターフェース
type TickFeeder interface {
	Start(tradeCh chan<- Trade, snapCh chan<- MarketSnapshot, doneCh <-chan struct{}) error
}

// OptimizeTCPConn: 極限低遅延のためのTCPソケット最適化 (Nagle解除・KeepAlive・バッファ最適化)
func OptimizeTCPConn(conn net.Conn) error {
	tcpConn, ok := conn.(*net.TCPConn)
	if !ok {
		return fmt.Errorf("not a TCP connection")
	}

	// 1. Nagleアルゴリズムの完全無効化 (TCP_NODELAY: パケットをため込まず即座に送出)
	if err := tcpConn.SetNoDelay(true); err != nil {
		return fmt.Errorf("set TCP_NODELAY failed: %w", err)
	}

	// 2. KeepAlive有効化
	if err := tcpConn.SetKeepAlive(true); err != nil {
		return fmt.Errorf("set SO_KEEPALIVE failed: %w", err)
	}
	if err := tcpConn.SetKeepAlivePeriod(30 * time.Second); err != nil {
		return fmt.Errorf("set KeepAlivePeriod failed: %w", err)
	}

	// 3. 低レイテンシ用バッファサイズ調整 (64KB)
	_ = tcpConn.SetReadBuffer(64 * 1024)
	_ = tcpConn.SetWriteBuffer(64 * 1024)

	// 4. Linux OS固有の低遅延ソケットオプション (TCP_QUICKACK等)
	rawConn, err := tcpConn.SyscallConn()
	if err == nil {
		_ = rawConn.Control(func(fd uintptr) {
			// TCP_QUICKACK (12 on Linux)
			_ = syscall.SetsockoptInt(int(fd), syscall.IPPROTO_TCP, 12, 1)
		})
	}

	return nil
}

// SyntheticFeeder: 高頻度取引向けリアルタイム疑似市場データジェネレータ
type SyntheticFeeder struct {
	Symbol     string
	BasePrice  float64
	TickSize   float64
	ATR        float64
	BurstFreq  int
	IntervalUs int
}

func NewSyntheticFeeder(symbol string, basePrice, tickSize, atr float64, intervalUs int) *SyntheticFeeder {
	return &SyntheticFeeder{
		Symbol:     symbol,
		BasePrice:  basePrice,
		TickSize:   tickSize,
		ATR:        atr,
		BurstFreq:  500,
		IntervalUs: intervalUs,
	}
}

func (sf *SyntheticFeeder) Start(tradeCh chan<- Trade, snapCh chan<- MarketSnapshot, doneCh <-chan struct{}) error {
	curPrice := sf.BasePrice
	recentHigh := curPrice + sf.ATR*0.5
	recentLow := curPrice - sf.ATR*0.5
	ticker := time.NewTicker(time.Duration(sf.IntervalUs) * time.Microsecond)
	defer ticker.Stop()

	tickCount := 0

	for {
		select {
		case <-doneCh:
			return nil
		case <-ticker.C:
			tickCount++
			now := float64(time.Now().UnixNano()) / 1e9

			// クラスタリング（突発的モメンタム）シミュレーション
			inBurst := (tickCount % sf.BurstFreq) < (sf.BurstFreq / 5)
			side := Buy
			delta := sf.TickSize
			if !inBurst {
				if rand.Float64() < 0.5 {
					side = Sell
					delta = -sf.TickSize
				}
			}

			curPrice += delta
			if curPrice > recentHigh {
				recentHigh = curPrice
			}
			if curPrice < recentLow {
				recentLow = curPrice
			}

			spread := sf.TickSize * 2.0
			bid := curPrice - spread/2.0
			ask := curPrice + spread/2.0

			size := 10000.0 + rand.Float64()*50000.0

			t := Trade{
				Ts:    now,
				Side:  side,
				Size:  size,
				Price: curPrice,
			}

			snap := MarketSnapshot{
				Ts:         now,
				Symbol:     sf.Symbol,
				Price:      curPrice,
				Bid:        bid,
				Ask:        ask,
				RecentHigh: recentHigh,
				RecentLow:  recentLow,
				ATR:        sf.ATR,
			}

			// ノンブロッキング送信
			select {
			case tradeCh <- t:
			default:
			}

			select {
			case snapCh <- snap:
			default:
			}
		}
	}
}

// CSVReplayFeeder: ヒストリカルTickデータ（CSV）再生ジェネレータ
type CSVReplayFeeder struct {
	FilePath string
	Symbol   string
}

func NewCSVReplayFeeder(filePath, symbol string) *CSVReplayFeeder {
	return &CSVReplayFeeder{
		FilePath: filePath,
		Symbol:   symbol,
	}
}

func (cf *CSVReplayFeeder) Start(tradeCh chan<- Trade, snapCh chan<- MarketSnapshot, doneCh <-chan struct{}) error {
	file, err := os.Open(cf.FilePath)
	if err != nil {
		return fmt.Errorf("open csv failed: %w", err)
	}
	defer file.Close()

	scanner := bufio.NewScanner(file)
	first := true
	for scanner.Scan() {
		select {
		case <-doneCh:
			return nil
		default:
		}

		line := scanner.Text()
		if first {
			first = false
			if strings.HasPrefix(line, "ts") || strings.HasPrefix(line, "time") {
				continue
			}
		}

		parts := strings.Split(line, ",")
		if len(parts) < 6 {
			continue
		}

		// フォーマット: ts,side,price,size,bid,ask
		ts, _ := strconv.ParseFloat(parts[0], 64)
		sideStr := strings.ToUpper(strings.TrimSpace(parts[1]))
		side := Buy
		if sideStr == "SELL" || sideStr == "S" || sideStr == "1" {
			side = Sell
		}
		price, _ := strconv.ParseFloat(parts[2], 64)
		size, _ := strconv.ParseFloat(parts[3], 64)
		bid, _ := strconv.ParseFloat(parts[4], 64)
		ask, _ := strconv.ParseFloat(parts[5], 64)

		t := Trade{
			Ts:    ts,
			Side:  side,
			Size:  size,
			Price: price,
		}

		snap := MarketSnapshot{
			Ts:         ts,
			Symbol:     cf.Symbol,
			Price:      price,
			Bid:        bid,
			Ask:        ask,
			RecentHigh: price * 1.001,
			RecentLow:  price * 0.999,
			ATR:        (ask - bid) * 5.0,
		}

		tradeCh <- t
		snapCh <- snap
	}

	return scanner.Err()
}
