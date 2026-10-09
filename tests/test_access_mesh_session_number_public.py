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
        ended = call(client, 'access', 'session', {'action':'end'}, request_id=73)
        assert ended == {'ok':True}


def test_mcp_envelope_has_remaining_time_on_success_and_error(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app, base_url='https://terminal.example') as client:
        before = rpc(client,'access','tools/call', {'name':'session','arguments':{'action':'end'}})
        assert before.status_code == 200
        assert re.fullmatch(r'\d{2}:\d{2}', before.json()['result']['_meta']['remaining_time'])
        started = rpc(client,'access','tools/call',
                      {'name':'session','arguments':{'action':'start'}}, request_id=77)
        assert started.status_code == 200
        timer = started.json()['result']['_meta']['remaining_time']
        assert re.fullmatch(r'\d{2}:\d{2}', timer)
        assert int(timer.split(':')[0]) >= 19
        failed = rpc(client,'executor','tools/call',
                     {'name':'session','arguments':{'session_number':'9999'}},request_id=78)
        assert re.fullmatch(r'\d{2}:\d{2}', failed.json()['result']['_meta']['remaining_time'])
        invalid = failed.json()['result']['structuredContent']
        assert invalid['error']['code'] in {'invalid_session_number','access_mesh_slot_not_found'}


def test_observe_only_named_attached_agents_and_four_fields(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app, base_url='https://terminal.example') as client:
        assert call(client, 'coordinator', 'agent_observe', {}, meta=False) == {
            'ok': True, 'agents': [],
        }
        started = call(client, 'access', 'session', {'action':'start'}, request_id=401)
        assert call(client, 'coordinator','session',
                    {'session_number':started['session_number']}) == {'ok':True}
        result = call(client, 'coordinator', 'agent_observe', {})
        assert result['ok'] and len(result['agents']) == 1
        view = result['agents'][0]
        assert set(view) == {'public_name','last_server','session_duration','last_activity'}
        assert view['public_name'] and view['last_server'] == 'firstbyte'
        assert view['session_duration'] > 0 and view['last_activity']
        assert 'session_number' not in str(view)
