"""rtslib_fb 대역(fake). 실제 커널/configfs 없이 우리 코드가 rtslib API를
어떤 순서·구조로 호출하는지만 검증한다 — 커널이 그 호출을 어떻게 처리하는지는
검증 못 한다(macOS라 rtslib_fb 자체가 설치 안 됨. 실제 Linux 환경 검증 필요).

Target/TPG를 wwn/tag로 재사용(idempotent attach)하는 rtslib의 실제 동작을
레지스트리 딕셔너리로 흉내낸다.
"""

from __future__ import annotations

import sys
import types


class FakeStorageObject:
    registry: dict[str, "FakeStorageObject"] = {}

    def __init__(self, name, dev=None):
        self.name = name
        if dev is not None:
            self.dev = dev
        existing = FakeStorageObject.registry.get(name)
        if existing is not None:
            self.dev = getattr(existing, "dev", dev)
        FakeStorageObject.registry[name] = self

    def delete(self):
        FakeStorageObject.registry.pop(self.name, None)


class FakeLUN:
    def __init__(self, tpg, storage_object):
        self.tpg = tpg
        self.storage_object = storage_object
        self.lun = len(tpg.luns)
        tpg.luns.append(self)

    def delete(self):
        self.tpg.luns.remove(self)


class FakeMappedLUN:
    def __init__(self, node_acl, mapped_lun, tpg_lun):
        self.node_acl = node_acl
        self.mapped_lun = mapped_lun
        self.tpg_lun = tpg_lun
        node_acl.mapped_luns.append(self)


class FakeNodeACL:
    def __init__(self, tpg, node_wwn):
        self.tpg = tpg
        self.node_wwn = node_wwn
        self.mapped_luns: list[FakeMappedLUN] = []
        # rtslib_fb의 NodeACL은 실제 iSCSI 세션이 붙어 있으면 .session이 채워진다고
        # 알려져 있다(실 커널 필드 구성은 검증 필요, lio.py 주석 참고). 기본은 세션 없음.
        self.session: dict | None = None
        tpg.node_acls.append(self)

    def delete(self):
        self.tpg.node_acls.remove(self)


class FakeNetworkPortal:
    def __init__(self, tpg, ip, port):
        self.tpg = tpg
        self.ip = ip
        self.port = port
        if not any(p.ip == ip and p.port == port for p in tpg.portals):
            tpg.portals.append(self)


class FakeTPG:
    registry: dict[tuple[str, int], "FakeTPG"] = {}

    def __new__(cls, target, tag=1):
        key = (target.wwn, tag)
        if key in cls.registry:
            return cls.registry[key]
        self = super().__new__(cls)
        self.target = target
        self.tag = tag
        self.enable = False
        self.luns: list[FakeLUN] = []
        self.node_acls: list[FakeNodeACL] = []
        self.portals: list[FakeNetworkPortal] = []
        cls.registry[key] = self
        return self

    def __init__(self, target, tag=1):
        pass  # 상태 초기화는 __new__에서(재사용 시 덮어쓰지 않기 위해)


class FakeTarget:
    registry: dict[str, "FakeTarget"] = {}

    def __new__(cls, fabric, wwn):
        if wwn in cls.registry:
            return cls.registry[wwn]
        self = super().__new__(cls)
        self.fabric = fabric
        self.wwn = wwn
        cls.registry[wwn] = self
        return self

    def __init__(self, fabric, wwn):
        pass

    def delete(self):
        FakeTarget.registry.pop(self.wwn, None)


def install(monkeypatch) -> types.ModuleType:
    """sys.modules['rtslib_fb']에 가짜 모듈을 심고, 매 테스트마다 상태를 리셋한다."""
    FakeStorageObject.registry.clear()
    FakeTPG.registry.clear()
    FakeTarget.registry.clear()

    fake_module = types.ModuleType("rtslib_fb")
    fake_module.BlockStorageObject = FakeStorageObject
    fake_module.Target = FakeTarget
    fake_module.TPG = FakeTPG
    fake_module.LUN = FakeLUN
    fake_module.NodeACL = FakeNodeACL
    fake_module.MappedLUN = FakeMappedLUN
    fake_module.NetworkPortal = FakeNetworkPortal
    monkeypatch.setitem(sys.modules, "rtslib_fb", fake_module)
    return fake_module
