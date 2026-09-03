"""커널 LIO(rtslib-fb) 래퍼. DESIGN.md 3.4/6.1.

target은 서버당 하나로 고정하고, 세션마다 LUN을 추가한 뒤 initiator ACL을
`MappedLUN`으로 그 LUN 하나만 보이게 제한한다 — 세션마다 별도 target을
만들지 않는다(3.4 "target은 고정, LUN+ACL만 클라이언트별로 늘리는 구조").

configfs 그룹 권한(diskless-priv, setgid)으로 일반 유저 프로세스가 직접
LUN/ACL을 등록한다 — 이 모듈은 capability를 요구하지 않는다.

**실제 환경 검증 필요**: 이 모듈은 rtslib_fb가 없는 개발 환경(macOS)에서는
가짜 모듈(tests/iscsi/fake_rtslib.py)로 "호출 구조"만 검증했다. 실제 커널이
이 rtslib 호출을 기대대로 처리하는지는 Linux + LIO 환경에서 확인 전까지 미검증.
"""

from __future__ import annotations

TPG_TAG = 1
MAPPED_LUN = 0  # initiator 입장에서는 항상 LUN 0 하나만 보임(세션당 디스크 1개)


def register_lun(
    target_wwn: str, initiator_iqn: str, lun_name: str, backing_dev: str, portal_ip: str, portal_port: int
) -> int:
    """공유 target 아래 LUN을 새로 만들고 반환한다. 이 initiator의 ACL은
    MappedLUN으로 그 LUN 하나만 보도록 제한한다.
    """
    from rtslib_fb import LUN, TPG, BlockStorageObject, MappedLUN, NetworkPortal, NodeACL, Target

    bs = BlockStorageObject(lun_name, dev=backing_dev)
    target = Target(fabric="iscsi", wwn=target_wwn)
    tpg = TPG(target, tag=TPG_TAG)
    tpg.enable = True
    NetworkPortal(tpg, portal_ip, portal_port)

    lun = LUN(tpg, storage_object=bs)
    acl = NodeACL(tpg, initiator_iqn)
    MappedLUN(acl, mapped_lun=MAPPED_LUN, tpg_lun=lun.lun)

    return lun.lun


def remove_lun(target_wwn: str, initiator_iqn: str, lun_name: str) -> None:
    """세션 종료 시 그 initiator의 ACL과 LUN/백스토어만 제거한다. target 자체는
    다른 세션이 계속 쓰므로 지우지 않는다.
    """
    from rtslib_fb import TPG, BlockStorageObject, Target

    target = Target(fabric="iscsi", wwn=target_wwn)
    tpg = TPG(target, tag=TPG_TAG)

    for acl in list(tpg.node_acls):
        if acl.node_wwn == initiator_iqn:
            acl.delete()

    for lun in list(tpg.luns):
        if lun.storage_object.name == lun_name:
            lun.delete()

    BlockStorageObject(lun_name).delete()


def is_session_active(target_wwn: str, initiator_iqn: str) -> bool:
    """이 initiator가 지금 실제로 iSCSI 로그인 상태인지 확인한다(세션 리퍼용,
    orchestration/session.py: reap_disconnected_sessions).

    rtslib_fb의 NodeACL은 실제 세션이 붙어 있으면 `.session`이 채워진다고 알려져
    있다 — 정확한 필드 구성/타입은 커널·rtslib 버전에 따라 다를 수 있어 이 판정
    자체는 다른 rtslib 호출과 마찬가지로 실제 Linux 환경에서 검증 전까지 미검증이다.
    """
    from rtslib_fb import TPG, Target

    target = Target(fabric="iscsi", wwn=target_wwn)
    tpg = TPG(target, tag=TPG_TAG)

    for acl in tpg.node_acls:
        if acl.node_wwn == initiator_iqn:
            return getattr(acl, "session", None) is not None
    return False
