"""
Antigravity - High-Frequency Trading (HFT) Engine for Cryptocurrency Derivative Markets.
"""

__version__ = "1.0.0"
__author__ = "Antigravity Dev Team"

from .config.settings import Settings
from .ws_engine.stream import WebSocketTickStream
from .ws_engine.flow_analyzer import FlowAnalyzer
from .risk_guard.circuit_breaker import PeakDrawdownCircuitBreaker
from .risk_guard.performance import PerformanceTracker
from .risk_guard.notifier import DiscordNotifier
from .strategies.base import BaseTickStrategy
from .strategies.micro_trend import MicroTrendTickStrategy
from .strategies.inventory_mm import InventorySkewTickMMStrategy
from .strategies.order_flow_scalp import OrderFlowScalpTickStrategy
from .strategies.ema_trend import EmaTrendTickStrategy
from .runtime.client import BitflyerClient
from .runtime.runner import AntigravityRunner

__all__ = [
    "Settings",
    "WebSocketTickStream",
    "FlowAnalyzer",
    "PeakDrawdownCircuitBreaker",
    "PerformanceTracker",
    "DiscordNotifier",
    "BaseTickStrategy",
    "MicroTrendTickStrategy",
    "InventorySkewTickMMStrategy",
    "OrderFlowScalpTickStrategy",
    "EmaTrendTickStrategy",
    "BitflyerClient",
    "AntigravityRunner",
]

