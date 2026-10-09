"""Regression tests for the minimal public Access/attach protocol."""
import re
from fastapi.testclient import TestClient
from terminal_mcp.app import create_app
from test_access_mesh_mcp_runtime import settings, call, rpc


def test_start_and_attach_share_minimal_number_only(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app, base_url='https://terminal.example') as client:
        started = call(client, 'access', 'session', {'action':'start'}, request_id=70)
        assert set(started) == {'ok','session_number'}
        assert re.fullmatch(r'[0-9]{4}', started['session_number'])
        repeated = call(client, 'access', 'session', {'action':'start'}, request_id=70)
        assert repeated == started
        attached = call(client, 'executor', 'session', {'session_number': started['session_number']}, request_id=71)
        assert attached == {'ok':True}
        again = call(client, 'executor', 'session', {'session_number': started['session_number']}, request_id=72)
        assert again == {'ok':True}
        tools = rpc(client, 'executor', 'tools/list').json()['result']['tools']
        session = next(tool for tool in tools if tool['name'] == 'session')
        assert session['inputSchema']['required'] == ['session_number']
        assert 'issuer_node_id' not in session['inputSchema']['properties']
        assert 'access_code' not in session['inputSchema']['properties']
