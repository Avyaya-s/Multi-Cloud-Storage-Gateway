import io
from collections import Counter

import pytest
from hypothesis import given
from hypothesis import strategies as st

from gateway.core import pipeline
from gateway.core.pipeline import FileContext
from gateway.placement.placer import Outcome, Placer, classify, plan
from tests.fakes import memory_registry

THREE = ["in-1", "in-2", "abroad"]
FOUR = THREE + ["extra"]


def one_segment():
    return next(pipeline.encode_stream(FileContext.new(), io.BytesIO(b"x" * 100), 64))


@given(n_providers=st.integers(0, 8), cap=st.integers(1, 3), offset=st.integers(0, 50))
def test_plan_never_exceeds_cap_and_fills_when_possible(n_providers: int, cap: int, offset: int) -> None:
    providers = [f"p{i}" for i in range(n_providers)]
    assignment = plan(6, providers, cap, offset)
    counts = Counter(p for p in assignment if p is not None)
    assert all(c <= cap for c in counts.values())
    assert len(assignment) == 6
    if n_providers * cap >= 6:
        assert None not in assignment


def test_plan_three_providers_two_each_rotating() -> None:
    assert plan(6, THREE, 2, 0) == ["in-1", "in-2", "abroad"] * 2
    assert plan(6, THREE, 2, 1) == ["in-2", "abroad", "in-1"] * 2


def test_plan_too_few_providers_leaves_gaps() -> None:
    assert plan(6, ["a", "b"], 2, 0) == ["a", "b", "a", "b", None, None]


@pytest.mark.parametrize(
    ("counts", "outcome"),
    [([6, 6], Outcome.ACTIVE), ([6, 5], Outcome.DEGRADED), ([5, 5], Outcome.DEGRADED),
     ([6, 4], Outcome.ROLLBACK), ([], Outcome.ROLLBACK)],
)
def test_classify(counts: list[int], outcome: Outcome) -> None:
    assert classify(counts) is outcome


async def test_all_ok() -> None:
    reg = memory_registry(*THREE)
    seg = one_segment()
    res = await Placer(reg, backoff_s=0).execute(seg, plan(6, THREE), THREE)
    assert len(res.stored) == 6 and not res.missing and not res.reroutes
    for sh in seg.shards:
        assert reg[res.stored[sh.index]].blobs[sh.object_key] == sh.data  # type: ignore[attr-defined]


async def test_transient_failure_is_retried() -> None:
    reg = memory_registry(*THREE)
    reg["in-2"].fail_next_puts = 2  # type: ignore[attr-defined]
    res = await Placer(reg, backoff_s=0).execute(one_segment(), plan(6, THREE), THREE)
    assert len(res.stored) == 6 and not res.reroutes


async def test_three_providers_one_down_has_no_reroute_target() -> None:
    reg = memory_registry(*THREE)
    reg["abroad"].down = True  # type: ignore[attr-defined]
    res = await Placer(reg, backoff_s=0).execute(one_segment(), plan(6, THREE), THREE)
    assert len(res.stored) == 4 and res.missing == {2, 5}
    assert res.failed_providers == {"abroad"} and not res.reroutes


async def test_three_providers_single_shard_failure_is_degraded() -> None:
    reg = memory_registry(*THREE)
    reg["abroad"].poison_first_key = True  # type: ignore[attr-defined]
    res = await Placer(reg, backoff_s=0).execute(one_segment(), plan(6, THREE), THREE)
    assert len(res.stored) == 5 and classify([5]) is Outcome.DEGRADED


async def test_four_providers_reroute_within_cap() -> None:
    reg = memory_registry(*FOUR)
    reg["extra"].down = True  # type: ignore[attr-defined]
    hooked: list[tuple[int, str]] = []

    async def hook(i: int, p: str) -> None:
        hooked.append((i, p))

    seg = one_segment()
    assignment = plan(6, FOUR)  # extra gets shard 3
    assert assignment[3] == "extra"
    res = await Placer(reg, backoff_s=0).execute(seg, assignment, FOUR, hook)
    assert len(res.stored) == 6 and "extra" not in res.stored.values()
    assert max(Counter(res.stored.values()).values()) <= 2
    assert [(r.shard_index, r.from_provider, r.to_provider) for r in res.reroutes] == [(3, "extra", "abroad")]
    assert hooked == [(3, "abroad")]


async def test_reroute_hook_runs_before_put() -> None:
    reg = memory_registry(*FOUR)
    reg["extra"].down = True  # type: ignore[attr-defined]
    seg = one_segment()
    seen_before: list[bool] = []

    async def hook(i: int, p: str) -> None:
        seen_before.append(seg.shards[i].object_key in reg[p].blobs)  # type: ignore[attr-defined]

    await Placer(reg, backoff_s=0).execute(seg, plan(6, FOUR), FOUR, hook)
    assert seen_before == [False]


async def test_delete_reports_failures() -> None:
    reg = memory_registry("a", "b")
    reg["a"].blobs["k1"] = b"1"  # type: ignore[attr-defined]
    reg["b"].down = True  # type: ignore[attr-defined]
    failed = await Placer(reg).delete([("a", "k1"), ("b", "k2"), ("gone", "k3")])
    assert failed == {("b", "k2"), ("gone", "k3")}
    assert reg["a"].blobs == {}  # type: ignore[attr-defined]
