import pytest

from ove.domain.errors import OveError


def test_idempotent_submit_and_conflict(service):
    repo = service.repository
    first = repo.submit("render", {"x": 1}, "key", "hash")
    assert repo.submit("render", {"x": 1}, "key", "hash")["id"] == first["id"]
    with pytest.raises(OveError):
        repo.submit("render", {"x": 2}, "key", "different")


def test_cancellation_and_restart_recovery(service):
    repo = service.repository
    first = repo.submit("render", {}, "a", "a")
    repo.claim()
    assert repo.cancel(first["id"])["state"] == "cancel_requested"
    assert repo.recover() == 1
    assert repo.job(first["id"])["state"] == "cancelled"
    second = repo.submit("render", {}, "b", "b")
    repo.claim()
    repo.recover()
    assert repo.job(second["id"])["state"] == "failed"
    assert repo.job(second["id"])["error"]["code"] == "worker_interrupted"


def test_cancel_wins_completion_race(service):
    repo = service.repository
    job = repo.submit("render", {}, "c", "c")
    repo.claim()
    repo.cancel(job["id"])
    repo.transition(job["id"], "succeeded", result={"artifact_id": "x"})
    assert repo.job(job["id"])["state"] == "cancelled"
    assert repo.job(job["id"])["result"] is None


def test_terminal_status_cannot_be_overwritten(service):
    repo = service.repository
    job = repo.submit("render", {}, "d", "d")
    repo.claim()
    repo.transition(job["id"], "failed")
    with pytest.raises(OveError):
        repo.transition(job["id"], "succeeded")


def test_queue_claim_is_unique_under_concurrent_callers(service):
    from concurrent.futures import ThreadPoolExecutor

    repo = service.repository
    jobs = [repo.submit("render", {}, f"key-{i}", str(i)) for i in range(10)]
    with ThreadPoolExecutor(max_workers=5) as pool:
        claims = list(pool.map(lambda _: repo.claim(), range(12)))
    claimed = [job["id"] for job in claims if job]
    assert len(claimed) == len(set(claimed)) == len(jobs)
