"""Dispatcher core: task lifecycle, reconciliation, and recovery."""

from .core import Dispatcher, JrBridgeUnavailableError

__all__ = ["Dispatcher", "JrBridgeUnavailableError"]
