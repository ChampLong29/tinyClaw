import asyncio
from pathlib import Path

from tinyclaw.gateway import AgentConfig, AgentManager, Binding, BindingTable
from tinyclaw.gateway.server import GatewayServer


def test_gateway_returns_injected_versioned_session_key(tmp_path: Path):
    manager = AgentManager(tmp_path / "agents")
    manager.register(AgentConfig(id="main", name="Main"))
    bindings = BindingTable()
    bindings.add(Binding(agent_id="main", tier=5, match_key="default", match_value="*"))
    observed = {}

    def resolve_session(agent_id, channel, account_id, peer_id, platform_user_id):
        observed["route"] = (
            agent_id,
            channel,
            account_id,
            peer_id,
            platform_user_id,
        )
        return "agent:main:scope:per-account-channel-peer:v1:resolved"

    async def run_agent(_manager, agent_id, session_key, text):
        observed["run"] = (agent_id, session_key, text)
        return "done"

    gateway = GatewayServer(
        manager,
        bindings,
        run_agent_fn=run_agent,
        session_resolver_fn=resolve_session,
    )
    result = asyncio.run(
        gateway._m_send(
            {
                "text": "hello",
                "channel": "websocket",
                "account_id": "client-a",
                "peer_id": "thread-a",
                "platform_user_id": "user-a",
            }
        )
    )

    assert observed["route"] == (
        "main",
        "websocket",
        "client-a",
        "thread-a",
        "user-a",
    )
    assert observed["run"] == (
        "main",
        "agent:main:scope:per-account-channel-peer:v1:resolved",
        "hello",
    )
    assert result == {
        "agent_id": "main",
        "session_key": "agent:main:scope:per-account-channel-peer:v1:resolved",
        "reply": "done",
    }
