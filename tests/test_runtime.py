"""Regression tests for HTTP fan-out, scheduling and task ownership."""

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from backend.agents.coordinator_core import do_spawn_swarm
from backend.agents.coordinator_loop import _auto_spawn_unsolved, _coordinator_worker
from backend.agents.swarm import ChallengeSwarm
from backend.config import Settings
from backend.cost_tracker import CostTracker
from backend.ctfd import CTFdClient
from backend.deps import CoordinatorDeps
from backend.poller import CTFdPoller
from backend.prompts import ChallengeMeta


@pytest.mark.asyncio
async def test_single_start_fetches_only_requested_detail() -> None:
    paths = []

    def handle(request):
        paths.append(request.url.path)
        if request.url.path.endswith('/challenges'):
            return httpx.Response(200, json={'data': [{'id': n, 'name': f'c{n}'} for n in range(52)]})
        return httpx.Response(200, json={'data': {'id': 7, 'name': 'c7'}})

    async with httpx.AsyncClient(base_url='https://ctfd.test', transport=httpx.MockTransport(handle)) as http:
        client = CTFdClient(token='test', _client=http)
        await client.fetch_challenge_stubs()
        paths.clear()
        assert (await client.fetch_challenge('c7'))['id'] == 7
        assert paths == ['/api/v1/challenges/7']


@pytest.mark.asyncio
async def test_full_fetch_is_parallel_and_bounded() -> None:
    active = peak = 0

    async def handle(request):
        nonlocal active, peak
        if request.url.path.endswith('/challenges'):
            return httpx.Response(200, json={'data': [{'id': n, 'name': f'c{n}'} for n in range(12)]})
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        return httpx.Response(200, json={'data': {'id': int(request.url.path.rsplit('/', 1)[1])}})

    async with httpx.AsyncClient(base_url='https://ctfd.test', transport=httpx.MockTransport(handle)) as http:
        results = await CTFdClient(token='test', _client=http).fetch_all_challenges()
    assert [r['id'] for r in results] == list(range(12))
    assert peak == 4


@pytest.mark.asyncio
async def test_parallel_spawn_respects_capacity(monkeypatch, tmp_path) -> None:
    ctfd = CTFdClient(token='test')
    details = AsyncMock(return_value={'name': 'alpha', 'id': 1})
    monkeypatch.setattr(ctfd, 'fetch_challenge', details)

    async def pull(challenge, output):
        await asyncio.sleep(0)  # expose the capacity race during network I/O
        path = tmp_path / challenge['name']
        path.mkdir(exist_ok=True)
        (path / 'metadata.yml').write_text('name: alpha\n')
        return str(path)

    monkeypatch.setattr(ctfd, 'pull_challenge', pull)
    block = asyncio.Event()
    monkeypatch.setattr(ChallengeSwarm, 'run', lambda self: block.wait())
    deps = CoordinatorDeps(ctfd=ctfd, cost_tracker=CostTracker(), settings=Settings(), max_concurrent_challenges=1)
    try:
        results = await asyncio.gather(do_spawn_swarm(deps, 'alpha'), do_spawn_swarm(deps, 'beta'))
        assert len(deps.swarms) == 1
        assert details.await_count == 1
        assert any('At capacity' in result for result in results)
    finally:
        for task in deps.swarm_tasks.values():
            task.cancel()
        await asyncio.gather(*deps.swarm_tasks.values(), return_exceptions=True)


@pytest.mark.asyncio
async def test_priority_and_no_automatic_retry(monkeypatch) -> None:
    ctfd = CTFdClient(token='test')
    deps = CoordinatorDeps(ctfd=ctfd, cost_tracker=CostTracker(), settings=Settings())
    poller = CTFdPoller(ctfd=ctfd)
    poller._known_challenges = {'alpha-hard', 'beta-easy'}
    poller.challenge_info = {'alpha-hard': {'solves': 1}, 'beta-easy': {'solves': 200}}
    order = []

    async def spawn(deps, name):
        order.append(name)
        return True

    monkeypatch.setattr('backend.agents.coordinator_loop._auto_spawn_one', spawn)
    attempted = set()
    await _auto_spawn_unsolved(deps, poller, attempted)
    await _auto_spawn_unsolved(deps, poller, attempted)
    assert order == ['beta-easy', 'alpha-hard']


@pytest.mark.asyncio
async def test_coordinator_batches_and_recovers_after_error() -> None:
    inbox = asyncio.Queue()
    inbox.put_nowait('one')
    inbox.put_nowait('two')
    recovered = asyncio.Event()
    received = []

    async def turn(message):
        received.append(message)
        if len(received) == 1:
            inbox.put_nowait('three')
            raise RuntimeError('temporary failure')
        recovered.set()

    worker = asyncio.create_task(_coordinator_worker(inbox, turn))
    try:
        await asyncio.wait_for(recovered.wait(), timeout=1)
        assert received == ['one\n\ntwo', 'three']
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.asyncio
async def test_parent_cancellation_joins_solvers(monkeypatch, tmp_path) -> None:
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def solver(self, spec):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(ChallengeSwarm, '_run_solver', solver)
    swarm = ChallengeSwarm(str(tmp_path), ChallengeMeta(name='test'), CTFdClient(), CostTracker(), Settings(), model_specs=['test/model'])
    task = asyncio.create_task(swarm.run())
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_poller_wait_propagates_cancellation() -> None:
    task = asyncio.create_task(CTFdPoller(CTFdClient()).get_event(timeout=60))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_kill_interrupts_active_solver(monkeypatch, tmp_path) -> None:
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def solver(self, spec):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(ChallengeSwarm, '_run_solver', solver)
    swarm = ChallengeSwarm(str(tmp_path), ChallengeMeta(name='test'), CTFdClient(), CostTracker(), Settings(), model_specs=['test/model'])
    task = asyncio.create_task(swarm.run())
    await asyncio.wait_for(started.wait(), timeout=1)
    swarm.kill()
    assert await asyncio.wait_for(task, timeout=1) is None
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_scheduler_continues_during_slow_coordinator(monkeypatch) -> None:
    from backend.agents.coordinator_loop import run_event_loop

    entered = asyncio.Event()
    progressed = asyncio.Event()
    calls = 0

    async def schedule(*args):
        nonlocal calls
        calls += 1
        if entered.is_set():
            progressed.set()

    async def turn(message):
        entered.set()
        await asyncio.Event().wait()

    async def next_event(*args, **kwargs):
        await asyncio.sleep(0)
        return None

    monkeypatch.setattr(CTFdPoller, 'start', AsyncMock())
    monkeypatch.setattr(CTFdPoller, 'stop', AsyncMock())
    monkeypatch.setattr(CTFdPoller, 'get_event', next_event)
    monkeypatch.setattr('backend.agents.coordinator_loop._start_msg_server', AsyncMock(return_value=None))
    monkeypatch.setattr('backend.agents.coordinator_loop._auto_spawn_unsolved', schedule)
    ctfd = CTFdClient(token='test')
    costs = CostTracker()
    deps = CoordinatorDeps(ctfd=ctfd, cost_tracker=costs, settings=Settings())
    task = asyncio.create_task(run_event_loop(deps, ctfd, costs, turn))
    try:
        await asyncio.wait_for(progressed.wait(), timeout=1)
        assert entered.is_set() and calls >= 2
    finally:
        task.cancel()
        await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
async def test_transient_submission_error_can_retry(monkeypatch, tmp_path) -> None:
    submit = AsyncMock(side_effect=[('submit_flag error: timeout', False), ('CORRECT accepted', True)])
    monkeypatch.setattr('backend.tools.core.do_submit_flag', submit)
    swarm = ChallengeSwarm(str(tmp_path), ChallengeMeta(name='test'), CTFdClient(), CostTracker(), Settings())
    assert not (await swarm.try_submit_flag('CTF{candidate}', 'model'))[1]
    assert (await swarm.try_submit_flag('CTF{candidate}', 'model'))[1]
    assert submit.await_count == 2


@pytest.mark.asyncio
async def test_incorrect_flag_still_deduplicated(monkeypatch, tmp_path) -> None:
    submit = AsyncMock(return_value=('INCORRECT rejected', False))
    monkeypatch.setattr('backend.tools.core.do_submit_flag', submit)
    swarm = ChallengeSwarm(str(tmp_path), ChallengeMeta(name='test'), CTFdClient(), CostTracker(), Settings())
    await swarm.try_submit_flag('CTF{wrong}', 'model-one')
    display, confirmed = await swarm.try_submit_flag('CTF{wrong}', 'model-two')
    assert not confirmed and 'already tried' in display
    assert submit.await_count == 1
