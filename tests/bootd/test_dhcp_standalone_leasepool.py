import pytest

from diskless.bootd.dhcp_standalone import LeasePool, PoolExhaustedError


def test_offer_allocates_first_available_ip():
    pool = LeasePool("192.0.2.0/30")  # usable hosts: .1, .2
    assert pool.offer("aa:aa:aa:aa:aa:aa") == "192.0.2.1"


def test_offer_is_idempotent_for_same_mac():
    pool = LeasePool("192.0.2.0/30")
    first = pool.offer("aa:aa:aa:aa:aa:aa")
    second = pool.offer("aa:aa:aa:aa:aa:aa")
    assert first == second


def test_offer_gives_different_ip_to_different_mac():
    pool = LeasePool("192.0.2.0/30")
    ip1 = pool.offer("aa:aa:aa:aa:aa:aa")
    ip2 = pool.offer("bb:bb:bb:bb:bb:bb")
    assert ip1 != ip2


def test_pool_exhaustion_raises():
    pool = LeasePool("192.0.2.0/30")  # 호스트 2개뿐
    pool.offer("aa:aa:aa:aa:aa:aa")
    pool.offer("bb:bb:bb:bb:bb:bb")
    with pytest.raises(PoolExhaustedError):
        pool.offer("cc:cc:cc:cc:cc:cc")


def test_commit_finalizes_offered_ip():
    pool = LeasePool("192.0.2.0/30")
    offered = pool.offer("aa:aa:aa:aa:aa:aa")
    committed = pool.commit("aa:aa:aa:aa:aa:aa")
    assert committed == offered


def test_commit_without_prior_offer_allocates_one():
    pool = LeasePool("192.0.2.0/30")
    ip = pool.commit("aa:aa:aa:aa:aa:aa")
    assert ip == "192.0.2.1"


def test_release_frees_ip_for_reuse():
    pool = LeasePool("192.0.2.0/30")
    pool.offer("aa:aa:aa:aa:aa:aa")
    pool.offer("bb:bb:bb:bb:bb:bb")
    pool.release("aa:aa:aa:aa:aa:aa")
    # 이제 다시 하나 내줄 수 있어야 함(풀 소진 안 됨)
    ip = pool.offer("cc:cc:cc:cc:cc:cc")
    assert ip == "192.0.2.1"


# ---- 임대 만료(타이머) ----


def _fake_clock(start: float = 0.0):
    box = {"now": start}

    def clock() -> float:
        return box["now"]

    def advance(seconds: float) -> None:
        box["now"] += seconds

    return clock, advance


def test_uncommitted_offer_expires_after_offer_timeout():
    clock, advance = _fake_clock()
    pool = LeasePool("192.0.2.0/30", offer_timeout_seconds=30, clock=clock)
    pool.offer("aa:aa:aa:aa:aa:aa")
    pool.offer("bb:bb:bb:bb:bb:bb")  # 풀(호스트 2개) 소진

    advance(31)  # offer 타임아웃 경과, 둘 다 커밋 안 됨

    # 회수돼서 새 MAC에 재할당 가능해야 함
    ip = pool.offer("cc:cc:cc:cc:cc:cc")
    assert ip in ("192.0.2.1", "192.0.2.2")


def test_uncommitted_offer_not_yet_expired_still_blocks_pool():
    clock, advance = _fake_clock()
    pool = LeasePool("192.0.2.0/30", offer_timeout_seconds=30, clock=clock)
    pool.offer("aa:aa:aa:aa:aa:aa")
    pool.offer("bb:bb:bb:bb:bb:bb")

    advance(10)  # 아직 타임아웃 전

    with pytest.raises(PoolExhaustedError):
        pool.offer("cc:cc:cc:cc:cc:cc")


def test_committed_lease_survives_offer_timeout():
    clock, advance = _fake_clock()
    pool = LeasePool("192.0.2.0/30", lease_seconds=3600, offer_timeout_seconds=30, clock=clock)
    pool.offer("aa:aa:aa:aa:aa:aa")
    pool.commit("aa:aa:aa:aa:aa:aa")
    pool.offer("bb:bb:bb:bb:bb:bb")
    pool.commit("bb:bb:bb:bb:bb:bb")  # 풀을 채우는 두 MAC 다 확정해야 offer_timeout과 무관해짐

    advance(31)  # offer 타임아웃은 지났지만 둘 다 commit(정식 임대)했으니 안 풀려야 함

    with pytest.raises(PoolExhaustedError):
        pool.offer("cc:cc:cc:cc:cc:cc")


def test_committed_lease_expires_after_lease_seconds():
    clock, advance = _fake_clock()
    pool = LeasePool("192.0.2.0/30", lease_seconds=3600, clock=clock)
    pool.offer("aa:aa:aa:aa:aa:aa")
    pool.commit("aa:aa:aa:aa:aa:aa")
    pool.offer("bb:bb:bb:bb:bb:bb")

    advance(3601)

    ip = pool.offer("cc:cc:cc:cc:cc:cc")
    assert ip in ("192.0.2.1", "192.0.2.2")


def test_reoffer_refreshes_offer_timer():
    clock, advance = _fake_clock()
    pool = LeasePool("192.0.2.0/30", offer_timeout_seconds=30, clock=clock)
    pool.offer("aa:aa:aa:aa:aa:aa")
    pool.offer("bb:bb:bb:bb:bb:bb")

    advance(20)
    pool.offer("aa:aa:aa:aa:aa:aa")  # 재요청 -> 타이머 갱신(이제 만료는 t=50)
    pool.offer("bb:bb:bb:bb:bb:bb")  # bb도 갱신 — 이 테스트가 보려는 건 aa 쪽 갱신 로직이므로 bb는 죽지 않게 유지
    advance(20)  # now=40: 처음 offer로부터는 40초 지났지만 재요청 후로는 20초라 아직 안 죽어야 함

    with pytest.raises(PoolExhaustedError):
        pool.offer("cc:cc:cc:cc:cc:cc")
