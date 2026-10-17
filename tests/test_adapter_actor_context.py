"""Adapter actor identity comes only from verified admission and server trace."""

from test_application_boundary import Service, actor

from terminal_mcp.adapters.actor import actor_for
from terminal_mcp.core.persistent_admission import bind_admission_context, reset_admission_context
from terminal_mcp.trace import current_trace_id


def test_actor_factory_uses_verified_identity_and_server_trace():
    token = bind_admission_context(actor().admission())
    trace_token = current_trace_id.set("server-trace")
    try:
        service = Service()
        value = actor_for(service, transport="mcp", endpoint_role="executor", contract_version=1)
        assert value.principal_id == "owner"
        assert value.node_id == "node-a"
        assert value.transport == "mcp"
        assert value.request_id == "server-trace"
        assert value.endpoint_role == "executor"
        assert value.logical_agent_id is None
    finally:
        current_trace_id.reset(trace_token)
        reset_admission_context(token)
