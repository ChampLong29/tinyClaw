"""WebSocket gateway server using JSON-RPC 2.0."""

from __future__ import annotations

import asyncio
import json
import time
import threading
from typing import Any

from tinyclaw.gateway.routing import (
    AgentManager, BindingTable, GatewayServer as GW,
    normalize_agent_id, build_session_key, resolve_route,
)


_event_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None


def get_event_loop() -> asyncio.AbstractEventLoop:
    """Get or create a shared event loop running in a daemon thread."""
    global _event_loop, _loop_thread
    if _event_loop is not None and _event_loop.is_running():
        return _event_loop
    _event_loop = asyncio.new_event_loop()

    def _run() -> None:
        asyncio.set_event_loop(_event_loop)
        _event_loop.run_forever()

    _loop_thread = threading.Thread(target=_run, daemon=True)
    _loop_thread.start()
    return _event_loop


def run_async(coro):
    """Run a coroutine in the shared event loop."""
    loop = get_event_loop()
    return asyncio.run_coroutine_threadsafe(coro, loop).result()


# ---------------------------------------------------------------------------
# Placeholder imports -- actual agent runner is injected at construction time
# ---------------------------------------------------------------------------

class GatewayServer:
    """WebSocket + JSON-RPC 2.0 gateway.

    Methods:
      - send: Route a message to an agent and get a response
      - bindings.set / bindings.list
      - agents.list / sessions.list / status

    Usage:
        gw = GatewayServer(agent_manager, binding_table, run_agent_fn)
        asyncio.run_coroutine_threadsafe(gw.start(), get_event_loop())
    """

    def __init__(
        self,
        mgr: AgentManager,
        bindings: BindingTable,
        run_agent_fn: Any = None,  # async (mgr, agent_id, sk, text) -> str
        host: str = "localhost",
        port: int = 8765,
    ) -> None:
        self._mgr = mgr
        self._bindings = bindings
        self._run_agent = run_agent_fn
        self._host = host
        self._port = port
        self._clients: set[Any] = set()
        self._start_time = time.monotonic()
        self._server: Any = None
        self._running = False

    async def start(self) -> None:
        """Start the WebSocket server."""
        try:
            import websockets
        except ImportError:
            print("GatewayServer requires websockets: pip install websockets")
            return
        self._start_time = time.monotonic()
        self._running = True
        self._server = await websockets.serve(
            self._handle, self._host, self._port
        )
        print(f"Gateway started ws://{self._host}:{self._port}")

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._running = False

    async def _handle(self, ws: Any, path: str = "") -> None:
        self._clients.add(ws)
        try:
            async for raw in ws:
                resp = await self._dispatch(raw)
                if resp:
                    await ws.send(json.dumps(resp))
        except Exception:
            pass
        finally:
            self._clients.discard(ws)

    def _typing_cb(self, agent_id: str, typing: bool) -> None:
        msg = json.dumps({
            "jsonrpc": "2.0", "method": "typing",
            "params": {"agent_id": agent_id, "typing": typing},
        })
        for ws in list(self._clients):
            try:
                asyncio.ensure_future(ws.send(msg))
            except Exception:
                self._clients.discard(ws)

    async def _dispatch(self, raw: str) -> dict | None:
        try:
            req = json.loads(raw)
        except json.JSONDecodeError:
            return {"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}, "id": None}

        rid = req.get("id")
        method = req.get("method", "")
        params = req.get("params", {})
        methods = {
            "send": self._m_send,
            "bindings.set": self._m_bind_set,
            "bindings.list": self._m_bind_list,
            "sessions.list": self._m_sessions,
            "agents.list": self._m_agents,
            "status": self._m_status,
        }
        handler = methods.get(method)
        if not handler:
            return {"jsonrpc": "2.0", "error": {"code": -32601, "message": f"Unknown: {method}"}, "id": rid}
        try:
            return {"jsonrpc": "2.0", "result": await handler(params), "id": rid}
        except Exception as exc:
            return {"jsonrpc": "2.0", "error": {"code": -32000, "message": str(exc)}, "id": rid}

    async def _m_send(self, p: dict) -> dict:
        text = p.get("text", "")
        if not text:
            raise ValueError("text is required")
        ch = p.get("channel", "websocket")
        pid = p.get("peer_id", "ws-client")
        if p.get("agent_id"):
            aid = normalize_agent_id(p["agent_id"])
            a = self._mgr.get_agent(aid)
            sk = build_session_key(
                aid, channel=ch, peer_id=pid,
                dm_scope=a.dm_scope if a else "per-peer",
            )
        else:
            aid, sk = resolve_route(self._bindings, self._mgr, ch, pid)

        if self._run_agent:
            reply = await self._run_agent(self._mgr, aid, sk, text)
        else:
            reply = "[no agent runner configured]"
        return {"agent_id": aid, "session_key": sk, "reply": reply}

    async def _m_bind_set(self, p: dict) -> dict:
        from tinyclaw.gateway.routing import Binding
        b = Binding(
            agent_id=normalize_agent_id(p.get("agent_id", "")),
            tier=int(p.get("tier", 5)),
            match_key=p.get("match_key", "default"),
            match_value=p.get("match_value", "*"),
            priority=int(p.get("priority", 0)),
        )
        self._bindings.add(b)
        return {"ok": True, "binding": b.display()}

    async def _m_bind_list(self, p: dict) -> list[dict]:
        return [{"agent_id": b.agent_id, "tier": b.tier, "match_key": b.match_key,
                 "match_value": b.match_value, "priority": b.priority}
                for b in self._bindings.list_all()]

    async def _m_sessions(self, p: dict) -> dict:
        return self._mgr.list_sessions(p.get("agent_id", ""))

    async def _m_agents(self, p: dict) -> list[dict]:
        return [{"id": a.id, "name": a.name, "model": a.effective_model,
                 "dm_scope": a.dm_scope, "personality": a.personality}
                for a in self._mgr.list_agents()]

    async def _m_status(self, p: dict) -> dict:
        return {
            "running": self._running,
            "uptime_seconds": round(time.monotonic() - self._start_time, 1),
            "connected_clients": len(self._clients),
            "agent_count": len(self._mgr.list_agents()),
            "binding_count": len(self._bindings.list_all()),
        }
