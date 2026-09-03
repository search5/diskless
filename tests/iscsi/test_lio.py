"""DESIGN.md 3.4: "target은 고정, LUN+ACL만 클라이언트별로 늘리는 구조".
실제 rtslib_fb가 없는 환경(macOS)이라 가짜 모듈로 "우리 코드가 rtslib API를
올바른 구조로 호출하는지"만 검증한다 — 커널 동작 자체는 검증 못 한다.
"""

from diskless.iscsi import lio
from tests.iscsi.fake_rtslib import install

TARGET_WWN = "iqn.2026-09.local.diskless:server"


def test_register_lun_reuses_same_target_across_sessions(monkeypatch):
    fake = install(monkeypatch)

    lio.register_lun(TARGET_WWN, "iqn:client-a", "session-a", "/dev/mapper/session-a", "0.0.0.0", 3260)
    lio.register_lun(TARGET_WWN, "iqn:client-b", "session-b", "/dev/mapper/session-b", "0.0.0.0", 3260)

    assert len(fake.Target.registry) == 1


def test_register_lun_creates_separate_luns_per_session(monkeypatch):
    fake = install(monkeypatch)

    lio.register_lun(TARGET_WWN, "iqn:client-a", "session-a", "/dev/mapper/session-a", "0.0.0.0", 3260)
    lio.register_lun(TARGET_WWN, "iqn:client-b", "session-b", "/dev/mapper/session-b", "0.0.0.0", 3260)

    tpg = fake.TPG.registry[(TARGET_WWN, 1)]
    assert len(tpg.luns) == 2
    assert {lun.storage_object.name for lun in tpg.luns} == {"session-a", "session-b"}


def test_register_lun_restricts_acl_to_only_its_own_lun(monkeypatch):
    fake = install(monkeypatch)

    lun_a = lio.register_lun(TARGET_WWN, "iqn:client-a", "session-a", "/dev/mapper/session-a", "0.0.0.0", 3260)
    lun_b = lio.register_lun(TARGET_WWN, "iqn:client-b", "session-b", "/dev/mapper/session-b", "0.0.0.0", 3260)

    tpg = fake.TPG.registry[(TARGET_WWN, 1)]
    acl_a = next(a for a in tpg.node_acls if a.node_wwn == "iqn:client-a")
    tpg_luns_visible_to_a = {m.tpg_lun for m in acl_a.mapped_luns}

    assert tpg_luns_visible_to_a == {lun_a}
    assert lun_b not in tpg_luns_visible_to_a


def test_register_lun_creates_portal_only_once(monkeypatch):
    fake = install(monkeypatch)

    lio.register_lun(TARGET_WWN, "iqn:client-a", "session-a", "/dev/mapper/session-a", "0.0.0.0", 3260)
    lio.register_lun(TARGET_WWN, "iqn:client-b", "session-b", "/dev/mapper/session-b", "0.0.0.0", 3260)

    tpg = fake.TPG.registry[(TARGET_WWN, 1)]
    assert len(tpg.portals) == 1


def test_remove_lun_removes_only_that_session_and_keeps_target(monkeypatch):
    fake = install(monkeypatch)
    lio.register_lun(TARGET_WWN, "iqn:client-a", "session-a", "/dev/mapper/session-a", "0.0.0.0", 3260)
    lio.register_lun(TARGET_WWN, "iqn:client-b", "session-b", "/dev/mapper/session-b", "0.0.0.0", 3260)

    lio.remove_lun(TARGET_WWN, "iqn:client-a", "session-a")

    tpg = fake.TPG.registry[(TARGET_WWN, 1)]
    assert len(fake.Target.registry) == 1  # target 자체는 남아있음
    assert {lun.storage_object.name for lun in tpg.luns} == {"session-b"}
    assert {a.node_wwn for a in tpg.node_acls} == {"iqn:client-b"}
    assert "session-a" not in fake.BlockStorageObject.registry


# ---- is_session_active (세션 리퍼가 연결 끊김을 판단할 때 씀) ----


def test_is_session_active_true_when_acl_has_live_session(monkeypatch):
    fake = install(monkeypatch)
    lio.register_lun(TARGET_WWN, "iqn:client-a", "session-a", "/dev/mapper/session-a", "0.0.0.0", 3260)
    tpg = fake.TPG.registry[(TARGET_WWN, 1)]
    acl = next(a for a in tpg.node_acls if a.node_wwn == "iqn:client-a")
    acl.session = {"state": "LOGGED_IN"}  # 실제 세션이 붙어있는 상태를 흉내냄

    assert lio.is_session_active(TARGET_WWN, "iqn:client-a") is True


def test_is_session_active_false_when_acl_has_no_session(monkeypatch):
    install(monkeypatch)
    lio.register_lun(TARGET_WWN, "iqn:client-a", "session-a", "/dev/mapper/session-a", "0.0.0.0", 3260)
    # session은 기본값 None(로그아웃/연결 끊김 상태)

    assert lio.is_session_active(TARGET_WWN, "iqn:client-a") is False


def test_is_session_active_false_when_acl_does_not_exist(monkeypatch):
    install(monkeypatch)
    assert lio.is_session_active(TARGET_WWN, "iqn:never-registered") is False
