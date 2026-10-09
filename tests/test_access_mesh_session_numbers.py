"""Collision and reservation invariants for partition-tolerant Mesh numbers."""
from datetime import UTC, datetime, timedelta
import pytest
from terminal_mcp.storage.access_mesh import AccessMeshStore
from terminal_mcp.core.access_mesh_grants import AccessMeshError

T0 = datetime(2026, 10, 9, tzinfo=UTC)

@pytest.fixture
def registry(tmp_path):
    store = AccessMeshStore(tmp_path / 'numbers.sqlite3', local_node_id='a', trusted_issuers=frozenset({'a','b'}), proof_key=b'unit-test-mesh-secret-key-32bytes')
    return store.numbers

def test_reserve_conflict_proposal_expiry_and_release(registry):
    first = registry.reserve(number='0000', attempt_id='attempt-a', now=T0)
    assert first == {'ok': True, 'number': '0000'}
    assert registry.reserve(number='0000', attempt_id='attempt-a', now=T0)['ok']
    other = registry.reserve(number='0000', attempt_id='attempt-b', now=T0)
    assert other['ok'] is False and other['suggested_number'] != '0000'
    assert len(registry.reservations(now=T0)) == 2
    registry.release('attempt-b')
    assert len(registry.reservations(now=T0)) == 1
    assert registry.reserve(number='0000', attempt_id='attempt-c', now=T0 + timedelta(minutes=3))['ok']

def test_collision_winner_history_and_max_deadline(registry):
    a = dict(number='0001', issuer_id='a', slot_id='s1', logical_agent_id='la_a', started_at=T0, hard_expires_at=T0+timedelta(minutes=10))
    b = dict(number='0001', issuer_id='b', slot_id='s2', logical_agent_id='la_b', started_at=T0+timedelta(minutes=1), hard_expires_at=T0+timedelta(minutes=4))
    registry.register(**a)
    assert registry.register(**b)['collisions'] == 1
    assert registry.winner('0001')['logical_agent_id'] == 'la_b'
    assert registry.winner('0001')['hard_expires_at'] == registry.stamp(a['hard_expires_at'])
    registry.register(**b)
    assert len(registry.snapshot()) == 2
    assert len(registry.incidents()) == 1
    with pytest.raises(AccessMeshError, match='session_identity_conflict'):
        registry.register(**{**a, 'logical_agent_id':'different'})

def test_timestamp_tie_uses_stable_issuer_and_slot_order(registry):
    for issuer, slot in [('a','1'),('b','2')]:
        registry.register(number='1234', issuer_id=issuer, slot_id=slot, logical_agent_id='la_'+issuer, started_at=T0, hard_expires_at=T0+timedelta(minutes=5))
    assert registry.winner('1234')['logical_agent_id'] == 'la_b'

def test_invalid_number_and_untrusted_issuer(registry):
    with pytest.raises(AccessMeshError, match='invalid_session_number'):
        registry.reserve(number='12', attempt_id='a', now=T0)
    with pytest.raises(AccessMeshError, match='access_mesh_untrusted_peer'):
        registry.register(number='1111',issuer_id='outside',slot_id='s',logical_agent_id='x',started_at=T0,hard_expires_at=T0+timedelta(minutes=1))

@pytest.mark.asyncio
async def test_http_number_replication_converges_future_identity(tmp_path):
    """Independent partition-issued claims converge, originals stay immutable."""
    import httpx
    from test_access_mesh_replication_http import Routes, node, actor, T0
    from terminal_mcp.core.access_mesh_grants import SlotPolicy
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        first, first_rep = await node(tmp_path, 'firstbyte', 'bacloud', routes, client)
        second, second_rep = await node(tmp_path, 'bacloud', 'firstbyte', routes, client)
        original_first = await first.issue(actor(), code='0072', policy=SlotPolicy(240))
        original_second = await second.issue(actor(), code='0072', policy=SlotPolicy(600))
        await first_rep.tick()
        await second_rep.tick()
        winner_first = first.store.numbers.winner('0072')
        winner_second = second.store.numbers.winner('0072')
        assert winner_first == winner_second
        assert winner_first['logical_agent_id'] == original_first['logical_agent_id']
        assert len(first.store.numbers.incidents()) == 1
        assert len(first.store.numbers.snapshot()) == 2
        assert (await first.attach(actor(),session_number='0072')) == {'ok':True}
        assert (await second.attach(actor(),session_number='0072')) == {'ok':True}
        first_binding = first.store.attached_slot(first.connection_key(actor()))
        second_binding = second.store.attached_slot(second.connection_key(actor()))
        assert first_binding.logical_agent_id == second_binding.logical_agent_id
        assert first_binding.logical_agent_id == original_first['logical_agent_id']
        assert original_second['logical_agent_id'] != original_first['logical_agent_id']
        observed = first.store.observed_identity(first_binding, now=T0+timedelta(minutes=6))
        assert observed['session_lifecycle']['state'] == 'active'
        assert observed['hard_expires_at'] == first.store.numbers.stamp(T0+timedelta(minutes=10))

@pytest.mark.asyncio
async def test_online_peers_negotiate_distinct_numbers(tmp_path):
    import httpx
    from test_access_mesh_replication_http import Routes, node, actor
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        first, first_rep = await node(tmp_path, 'firstbyte', 'bacloud', routes, client)
        second, second_rep = await node(tmp_path, 'bacloud', 'firstbyte', routes, client)
        first.replication = first_rep
        second.replication = second_rep
        first_result = await first.issue(actor())
        second_result = await second.issue(actor())
        assert first_result['access_code'] != second_result['access_code']
        assert first.store.numbers.winner(first_result['access_code']) is not None
        assert second.store.numbers.winner(first_result['access_code']) is not None
        assert first.store.numbers.winner(second_result['access_code']) is not None

@pytest.mark.asyncio
async def test_concurrent_same_candidate_never_reuses_live_number(tmp_path):
    import asyncio
    import httpx
    from test_access_mesh_replication_http import Routes, node
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        first, first_rep = await node(tmp_path, 'firstbyte', 'bacloud', routes, client)
        second, second_rep = await node(tmp_path, 'bacloud', 'firstbyte', routes, client)
        first_number, second_number = await asyncio.gather(
            first_rep.negotiate_number(preferred='0456'),
            second_rep.negotiate_number(preferred='0456'),
        )
        assert first_number[0] != second_number[0]
        first.store.numbers.release(first_number[1])
        second.store.numbers.release(second_number[1])


def test_claims_survive_store_reopen(tmp_path):
    path = tmp_path / 'reopen.sqlite3'
    kwargs = dict(local_node_id='one',trusted_issuers=frozenset({'one'}),proof_key=b'unit-test-mesh-secret-key-32bytes')
    original = AccessMeshStore(path, **kwargs)
    original.numbers.register(number='0037',issuer_id='one',slot_id='slot',logical_agent_id='la_one',started_at=T0,hard_expires_at=T0+timedelta(minutes=23))
    reopened = AccessMeshStore(path, **kwargs)
    assert reopened.numbers.winner('0037')['logical_agent_id'] == 'la_one'
    assert reopened.numbers.winner('0037')['hard_expires_at'] == reopened.numbers.stamp(T0+timedelta(minutes=23))

@pytest.mark.asyncio
async def test_offline_peer_allows_local_issuance_before_30_second_budget(tmp_path):
    """Partitioned node finishes locally within the bounded negotiation window."""
    import time
    import httpx
    from test_access_mesh_replication_http import Routes, node, actor
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        local, replication = await node(tmp_path, 'firstbyte', 'bacloud', routes, client)
        local.replication = replication
        routes.offline.add('bacloud')
        started = time.monotonic()
        issued = await local.issue(actor())
        duration = time.monotonic() - started
        assert issued['ok'] is True
        assert duration < 30, duration
        assert local.store.numbers.winner(issued['access_code'])['logical_agent_id'] == issued['logical_agent_id']
