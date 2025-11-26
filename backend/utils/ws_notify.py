"""Simple WebSocket notifier registry for order updates."""
from __future__ import annotations

from typing import Dict, Set
import asyncio

try:
    from fastapi import WebSocket
except Exception:  # pragma: no cover - optional import for type hinting
    WebSocket = object  # type: ignore


_order_ws: Dict[int, Set[WebSocket]] = {}
_orders_summary_ws: Set[WebSocket] = set()
_lock = asyncio.Lock()


async def register(order_id: int, ws: WebSocket) -> None:
    async with _lock:
        _order_ws.setdefault(order_id, set()).add(ws)


async def unregister(order_id: int, ws: WebSocket) -> None:
    async with _lock:
        if order_id in _order_ws and ws in _order_ws[order_id]:
            _order_ws[order_id].remove(ws)
            if not _order_ws[order_id]:
                _order_ws.pop(order_id, None)


async def broadcast(order_id: int, payload: dict) -> None:
    """Broadcast a JSON-serialisable payload to all clients subscribed to order_id."""
    targets: Set[WebSocket] = set()
    async with _lock:
        targets = set(_order_ws.get(order_id, set()))
    if not targets:
        return
    dead: Set[WebSocket] = set()
    for ws in targets:
        try:
            await ws.send_json(payload)
        except Exception:
            dead.add(ws)
    if dead:
        async with _lock:
            for ws in dead:
                for oid, conns in list(_order_ws.items()):
                    if ws in conns:
                        conns.discard(ws)
                        if not conns:
                            _order_ws.pop(oid, None)


async def register_summary(ws: WebSocket) -> None:
    async with _lock:
        _orders_summary_ws.add(ws)


async def unregister_summary(ws: WebSocket) -> None:
    async with _lock:
        if ws in _orders_summary_ws:
            _orders_summary_ws.remove(ws)


async def broadcast_summary(payload: dict) -> None:
    """Broadcast a payload to all listeners of the orders summary channel."""
    targets: Set[WebSocket] = set()
    async with _lock:
        targets = set(_orders_summary_ws)
    if not targets:
        return
    dead: Set[WebSocket] = set()
    for ws in targets:
        try:
            await ws.send_json(payload)
        except Exception:
            dead.add(ws)
    if dead:
        async with _lock:
            for ws in dead:
                _orders_summary_ws.discard(ws)
