"""Probe sharding (v1.6.0, setting ``probe_nodes`` > 1, core API >= 1.8):
N fake instances share one SQLite DB; ``ctx.my_shard`` is a small rendezvous
implementation over a shared, mutable membership list. Run inside
lyndrix-core-dev like the other suites.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import pathlib

_spec = importlib.util.spec_from_file_location(
    "_perf_base", pathlib.Path(__file__).with_name("test_perf_and_status.py")
)
_base = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_base)
svc = _base.svc  # pytest fixture
_monitor = _base._monitor


def _score(*parts):
    return int.from_bytes(hashlib.sha256("|".join(parts).encode()).digest()[:8], "big")


class _Cluster:
    def __init__(self, members):
        self.members = list(members)  # live instance ids


class _ShardCtx(_base._FakeCtx):
    def __init__(self, cluster, instance_id, settings, roles=("api",)):
        super().__init__()
        self.cluster = cluster
        self.instance_id = instance_id
        self.roles = list(roles)
        self._settings = settings
        self.wanted = []

    def get_all_settings(self):
        return dict(self._settings)

    def on_lease(self, name, *, acquired=None, lost=None, ttl=None):
        self.wanted.append(name)

    def my_shard(self, name, keys, count=None, roles=None):
        cands = sorted(self.cluster.members)
        n = len(cands) if count is None else max(1, min(int(count), len(cands)))
        chosen = sorted(cands, key=lambda i: _score(name, i), reverse=True)[:n]
        return [k for k in keys if max(chosen, key=lambda i: _score(name, str(k), i)) == self.instance_id]

    def is_worker(self, name, count=1, roles=None):
        cands = sorted(self.cluster.members)
        return self.instance_id in sorted(cands, key=lambda i: _score(name, i), reverse=True)[:count]


def _instances(base_svc, ids, settings):
    from app.controller.service import MonitoringService

    cluster = _Cluster(ids)
    out = []
    for iid in ids:
        s = MonitoringService(_ShardCtx(cluster, iid, settings))
        s._initial_probe_delay_seconds = 3600  # no real probes in tests
        out.append(s)
    return cluster, out


def _jobs(s):
    return {j.id.split(":", 1)[1] for j in s.scheduler.get_jobs() if j.id.startswith("monitor:")}


async def _settle(svcs, t=0.4):
    await asyncio.sleep(t)


def test_shards_are_disjoint_and_complete(svc):
    mids = {_monitor(svc, f"host:h{i}") for i in range(40)}

    async def run():
        cluster, svcs = _instances(svc, ["a", "b", "c"], {"probe_nodes": "3"})
        for s in svcs:
            await s.init_placement()
        await _settle(svcs)
        assert all(s.is_sharded and s.is_prober for s in svcs)
        assert all(s.ctx.wanted == [] for s in svcs)  # no lease in sharded mode
        shards = [s._shard for s in svcs]
        assert set().union(*shards) == mids
        assert sum(len(x) for x in shards) == len(mids)
        assert all(_jobs(s) == s._shard for s in svcs)
        assert all(s.shard_size() == len(s._shard) for s in svcs)
        for s in svcs:
            await s.stop_probing()

    asyncio.run(run())


def test_membership_change_moves_only_the_leavers_monitors(svc):
    for i in range(40):
        _monitor(svc, f"host:h{i}")

    async def run():
        cluster, svcs = _instances(svc, ["a", "b", "c"], {"probe_nodes": "3"})
        for s in svcs:
            await s.init_placement()
        await _settle(svcs)
        before = {s.ctx.instance_id: set(s._shard) for s in svcs}
        handles = {s.ctx.instance_id: {j.id: j for j in s.scheduler.get_jobs()} for s in svcs}
        # "c" leaves: survivors recompute on cluster:changed.
        cluster.members.remove("c")
        survivors = [s for s in svcs if s.ctx.instance_id != "c"]
        for s in survivors:
            s.on_cluster_changed({})
        await _settle(survivors)
        for s in survivors:
            iid = s.ctx.instance_id
            assert before[iid] <= s._shard           # kept everything it had
            assert s._shard - before[iid] <= before["c"]  # gained only c's monitors
            # retained jobs were not restarted
            for mid in before[iid]:
                assert s.scheduler.get_job(f"monitor:{mid}") is handles[iid][f"monitor:{mid}"]
        assert survivors[0]._shard | survivors[1]._shard == before["a"] | before["b"] | before["c"]
        for s in svcs:
            await s.stop_probing()

    asyncio.run(run())


def test_probe_nodes_one_keeps_the_lease_path(svc):
    _monitor(svc)

    async def run():
        cluster, svcs = _instances(svc, ["a", "b"], {"probe_nodes": "1"})
        for s in svcs:
            await s.init_placement()
        assert all(s.ctx.wanted == ["monitoring:prober"] for s in svcs)
        assert all(s._probe_mode == "lease" and s._shard is None and not s.is_prober for s in svcs)
        assert all(s.housekeeping_here() for s in svcs)

    asyncio.run(run())


def test_old_core_without_my_shard_falls_back_to_lease(svc):
    from app.controller.service import MonitoringService

    ctx = _base._FakeCtx()
    ctx.get_all_settings = lambda: {"probe_nodes": "3"}
    ctx.wanted = []
    ctx.on_lease = lambda name, **kw: ctx.wanted.append(name)
    s = MonitoringService(ctx)
    asyncio.run(s.init_placement())
    assert s._probe_mode == "lease" and ctx.wanted == ["monitoring:prober"]


def test_probe_all_and_quiesce_reach_every_worker(svc):
    for i in range(10):
        _monitor(svc, f"host:h{i}")

    async def run():
        cluster, svcs = _instances(svc, ["a", "b"], {"probe_nodes": "2"})
        for s in svcs:
            await s.init_placement()
        await _settle(svcs)
        a, b = svcs
        # probe-all on a → local flag; b receives the bridged event → its own sweep.
        a.request_full_probe_cluster()
        assert a._force_full_sweep is True
        b.on_monitors_changed({"reason": "probe_all", "_origin": "a"})
        assert b._force_full_sweep is True
        # each worker's sweep only covers its own shard
        for s in svcs:
            s._force_full_sweep = True
            assert set(await asyncio.to_thread(s._load_probe_candidates)) == s._shard
        # quiesce from a peer pauses every worker
        for s in svcs:
            s.on_quiesce({"on": True, "_origin": "x"})
        assert all(s.is_quiesced for s in svcs)
        for s in svcs:
            s.on_quiesce({"on": False, "_origin": "x"})
        assert not any(s.is_quiesced for s in svcs)
        # exactly one instance runs the global housekeeping
        assert sum(s.housekeeping_here() for s in svcs) == 1
        for s in svcs:
            await s.stop_probing()

    asyncio.run(run())


def test_non_owner_skips_probe(svc):
    mid = _monitor(svc)

    async def run():
        cluster, (a, b) = _instances(svc, ["a", "b"], {"probe_nodes": "2"})
        for s in (a, b):
            await s.init_placement()
        await _settle((a, b))
        other = b if mid in a._shard else a
        await other.run_scheduled_probe(mid)  # not owned → no probe, no write
        for s in (a, b):
            await s.stop_probing()

    asyncio.run(run())
    assert svc.get_monitor(mid)["latest_state"] == "UNKNOWN"


def test_settings_change_applies_probe_nodes_and_roles_at_runtime(svc):
    for i in range(20):
        _monitor(svc, f"host:h{i}")

    async def run():
        cluster, (a, b) = _instances(svc, ["a", "b"], {"probe_nodes": "2"})
        for s in (a, b):
            await s.init_placement()
        await _settle((a, b))
        assert a._shard and b._shard
        # probe_roles narrowed to "runner" — only b carries it now.
        b.ctx.roles = ["api", "runner"]
        cluster.members.remove("a")  # the core's role filter drops a from the candidates
        # (one service per process in production; here a's stop() would also
        # cancel b's "monitoring:*" tasks if both ran in the same instant)
        for s in (a, b):
            s.ctx._settings = {"probe_nodes": "2", "probe_roles": "runner"}
            await s.reload_settings()
            await _settle((a, b))
        assert a.is_prober is False
        assert len(b._shard) == 20
        # switching back to a single prober needs a reload: mode stays sharded
        b.ctx._settings = {"probe_nodes": "1", "probe_roles": "runner"}
        await b.reload_settings()
        assert b._probe_mode == "sharded"
        await b.stop_probing()

    asyncio.run(run())


def test_upsert_of_new_monitor_triggers_rebalance(svc):
    from app.model.models import MonitorType, MonitorUpsert

    _monitor(svc, "host:h0")

    async def run():
        cluster, (a, b) = _instances(svc, ["a", "b"], {"probe_nodes": "2"})
        for s in (a, b):
            await s.init_placement()
        await _settle((a, b))
        # Upsert lands on a; b learns via the bridged monitors_changed event.
        a.upsert_monitor(MonitorUpsert(monitor_id="host:new", name="host:new", monitor_type=MonitorType.PING,
                                       target="10.0.0.9", address="10.0.0.9", host_name="host:new"))
        b.on_monitors_changed({"reason": "upsert", "_origin": "a"})
        await _settle((a, b))
        owners = [s for s in (a, b) if "host:new" in s._shard]
        assert len(owners) == 1 and "host:new" in _jobs(owners[0])
        for s in (a, b):
            await s.stop_probing()

    asyncio.run(run())
