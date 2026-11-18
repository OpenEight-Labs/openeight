from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import pytest
from contracts import Mutation
from runner import new_simulator
from simulator import BusinessError, canonical


def mutation(action="a", revision=0, operation="restart_replica", **args):
    return Mutation(
        action,
        "checkout",
        operation,
        revision,
        canonical(args or {"replica_id": "replica-1"}),
    )


def test_concurrent_identical_retries_have_one_effect():
    simulator = new_simulator()
    with ThreadPoolExecutor(8) as pool:
        receipts = list(pool.map(lambda _: simulator.apply(mutation()), range(20)))
    assert len(set(receipts)) == 1
    assert simulator.snapshot().revision == 1
    assert simulator.snapshot().restart_counts == (("replica-1", 1), ("replica-2", 0))
    assert simulator.effects() == (receipts[0],)


def test_conflicting_and_stale_calls_leave_state_unchanged():
    simulator = new_simulator()
    receipt = simulator.apply(mutation())
    for request, code in [
        (mutation(replica_id="replica-2"), "action_conflict"),
        (mutation("b"), "stale_revision"),
    ]:
        with pytest.raises(BusinessError, match=code):
            simulator.apply(request)
    assert simulator.effects() == (receipt,)
    assert simulator.snapshot().revision == 1


def test_competing_revisions_admit_only_one_effect():
    simulator = new_simulator()

    def attempt(i):
        try:
            return simulator.apply(mutation(str(i)))
        except BusinessError as exc:
            return exc.code

    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(attempt, range(8)))
    assert results.count("stale_revision") == 7
    assert len(simulator.effects()) == 1


def test_mutations_preserve_unrelated_state_and_surviving_counters():
    simulator = new_simulator()
    simulator.apply(mutation())
    simulator.apply(mutation("b", 1, "set_replica_count", replicas=3))
    assert simulator.snapshot().restart_counts == (
        ("replica-1", 1),
        ("replica-2", 0),
        ("replica-3", 0),
    )
    simulator.apply(mutation("c", 2, "rollback_release", target_release="v41"))
    assert simulator.snapshot().release == "v41"
    assert simulator.snapshot().replicas == 3
    simulator.apply(mutation("d", 3, "set_replica_count", replicas=1))
    assert simulator.snapshot().restart_counts == (("replica-1", 1),)


@pytest.mark.parametrize(
    "mutation_request",
    [
        mutation(revision=True),
        mutation(replica_id=""),
        mutation(operation="set_replica_count", replicas=True),
        mutation(operation="set_replica_count", replicas=11),
        replace(mutation(), arguments_json='{"replica_id":"replica-1","extra":0}'),
        replace(mutation(), arguments_json='{"replica_id":NaN}'),
        replace(mutation(), service_id="other"),
    ],
)
def test_invalid_requests_never_mutate(mutation_request):
    simulator = new_simulator()
    with pytest.raises((ValueError, BusinessError)):
        simulator.apply(mutation_request)
    assert simulator.effects() == ()
    assert simulator.snapshot().revision == 0
