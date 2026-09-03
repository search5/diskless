# Diskless 서버 시스템 설계 계획서

## 1. 개요

### 1.1 목적
PXE 부팅 기반 디스크리스 클라이언트 시스템. 클라이언트는 로컬 디스크 없이 네트워크로 부팅하며, 서버가 제공하는 디스크 이미지를 iSCSI로 마운트해 OS를 구동한다.

### 1.2 범위
- 부팅 인프라: DHCP(Standalone/Proxy), TFTP, iPXE(sanboot)
- 스토리지: iSCSI 서버, 디스크 스냅샷/버전 관리
- 관리: Web UI
- 우선 지원 OS: Windows(iPXE sanboot로 iSCSI SAN 부팅), 리눅스는 향후 지원
- 전제: IP 할당용 DHCP 서버가 이미 있을 수도, 없을 수도 있음 → 3.1 두 모드 지원

### 1.3 기술 스택
| 영역 | 기술 |
|---|---|
| bootd — DHCP/Proxy DHCP/TFTP/iSCSI 세션 오케스트레이션 + 관리 웹 UI | Python 3, **Twisted 단일 프로세스**(reactor 하나) |
| Web UI 라우팅 | Klein (Twisted 위 Flask 스타일 라우팅) |
| WebSocket | autobahn (Twisted용) |
| 세션 쿠키/CSRF | itsdangerous (서명된 쿠키, Tornado의 secure_cookie/xsrf_cookies 대체) |
| 템플릿 엔진 | Jinja2 + MarkupSafe (자동 이스케이프) |
| 관리 메타데이터 저장소 | SQLite (WAL 모드 — reactor 스레드와 워커 스레드가 동시에 접근) |
| iSCSI 타겟 | 커널 LIO (targetcli/rtslib) |
| 블록 CoW | dm-snapshot (loop device 기반) |
| 권한 분리 | systemd `AmbientCapabilities` + 소켓 활성화 + configfs 그룹 권한 (컴파일 바이너리 불필요) |
| Rust/PyO3 | 현재 범위에서 미사용. 실측 성능 문제 발생 시에만 국지적으로 검토 |

---

## 2. 아키텍처 개요

```
[클라이언트 PXE 부팅]
    │
    ├── 모드 A: Standalone DHCP (기존 DHCP 서버 없음)
    │     └─▶ [본 시스템 DHCP, 포트 67/68] IP 할당 + PXE 옵션(60/66/67)
    │
    └── 모드 B: Proxy DHCP (기존 DHCP 서버 있음, 수정 안 함)
          ├─▶ [기존 DHCP 서버] IP만 할당
          └─▶ [본 시스템 Proxy DHCP, 포트 4011] PXE 옵션만 응답
    ▼
[TFTP] iPXE 바이너리 + 클라이언트별 동적 sanboot 스크립트 전달 (MAC 기준 조회)
    ▼
[iPXE, 자체 iSCSI initiator] sanboot iscsi:... ──로그인(initiator IQN)──▶ [iSCSI 타겟: 커널 LIO]
                                                              │
                                        ClientBinding 조회 → assigned_version 확인
                                                              │
                                  losetup(읽기전용) ── priv_helper.py(AmbientCapabilities) 경유
                                                              │
                                              dm-snapshot(세션별 CoW 오버레이)
                                                              │
                                          LIO가 LUN으로 export (ACL: initiator IQN)

[bootd, Twisted reactor 하나, 일반 유저]  DHCP(Standalone/Proxy)+TFTP 리스너 + ClientBinding 조회 + 세션 생성(rtslib)
                                        + Klein 웹 UI(이미지/버전/배정 관리) + autobahn WebSocket 대시보드
[priv_helper.py, CAP_SYS_ADMIN ambient 보유]  loop/dm-snapshot 조작 전담 — 유일하게 분리된 프로세스(root 아님)
[LIO/configfs, 그룹 권한]                    bootd가 rtslib로 직접 조작
[SQLite, WAL]                              ImageProfile / ImageVersion / ClientBinding / active_session
```

bootd 프로세스는 systemd 소켓 활성화(6.3)로 시작되므로 **처음부터 끝까지 일반 유저**이며 root로 실행되는 구간 자체가 없다(포트 바인딩을 systemd가 대신 함). `priv_helper`만 `CAP_SYS_ADMIN`을 프로세스 수명 내내 들고 있어야 해서(클라이언트가 부팅할 때마다 반복적으로 필요, 한 번 쓰고 버릴 권한이 아님) 별도 프로세스로 분리한다 — DHCP/TFTP/HTTP 요청을 직접 파싱하는 넓은 코드가 그 권한을 오래 들고 있으면 취약점 하나의 파급 범위가 커지기 때문(6.2).

Web UI가 bootd와 같은 프로세스이므로 세션 상태는 메모리로 바로 공유되지만, `active_session` 테이블은 그래도 유지한다 — 세션 생성(TFTP 핸들러, 워커 스레드)과 대시보드 폴링(reactor 스레드)이 서로 다른 스레드에서 동작하므로 상태를 SQLite로 주고받는 편이 스레드 간 공유보다 단순하다.

---

## 3. 컴포넌트 설계

### 3.1 DHCP — Standalone / Proxy 두 모드

사이트마다 기존 DHCP 서버 유무가 다르므로 두 모드를 모두 구현하고 사이트별로 선택 운영한다. 두 모드 다 **bootd(Twisted) 프로세스 안의 리스너**로 동작한다(3.2 TFTP/3.3 iPXE와 한 프로세스).

**모드 A: Standalone DHCP**
- 포트 67/68에서 완전한 DHCP 서버 역할(DISCOVER/OFFER/REQUEST/ACK, lease pool) 직접 수행
- PXE vendor class(옵션 60=`PXEClient`) 확인 시 next-server(66)/boot filename(67) 동봉
- 포트 바인딩은 systemd 소켓 활성화(6.3)로 bootd에 fd를 넘김
- **응답 목적지(확정)**: RFC 2131 4.1에 따라 giaddr(릴레이 경유)가 있으면 relay:67로 유니캐스트, 없고 broadcast 플래그가 켜져 있으면 255.255.255.255:68로 브로드캐스트(소켓에 `SO_BROADCAST` 필요 — systemd 소켓 유닛에 `Broadcast=yes` 지정), 둘 다 아니면 받은 곳으로 회신(`dhcp/common.py: reply_destination`)
- **lease 만료(확정)**: `LeasePool`이 OFFER 단계(짧은 `offer_timeout_seconds`, 기본 30초 — REQUEST 없이 방치되면 회수)와 확정(commit, `lease_seconds` 보호) 두 단계로 타이머를 관리. 실시간 대기 대신 주입 가능한 `clock`으로 테스트(`twisted.internet.task.Clock`)

**모드 B: Proxy DHCP**
- IP 할당은 기존 DHCP 서버가 계속 담당, 수정/대체하지 않음
- 포트 4011에서 IP 없이 PXE 옵션(60/66/67)만 응답 — RFC 4578 PXE Proxy DHCP
- 특권 포트가 아니므로 bootd가 직접 bind, 별도 권한 상승 불필요
- 응답 목적지는 모드 A와 동일하게 `common.reply_destination`으로 결정(giaddr/broadcast 처리 공유)

두 모드는 vendor class 판별/옵션 생성 로직을 공유하며, 운영 모드는 Web UI에서 사이트별로 선택한다(Config로 bootd에 전달).

> DHCP Relay(RFC 1542, giaddr)는 "서로 다른 서브넷이라 브로드캐스트가 안 닿는 문제"를 푸는 별개 개념. 클라이언트가 다른 서브넷/VLAN에 있으면 `ip helper-address` 등으로 별도 릴레이 설정이 모드 A/B와 별개로 필요.

### 3.2 TFTP

- **역할 범위**: iPXE 바이너리와 클라이언트별 동적 sanboot 스크립트(수백 KB~수 MB)까지만 담당. 실제 OS 이미지는 iSCSI(3.4)로 서비스 — TFTP로 대용량을 나르지 않는다.
- RRQ(읽기)만 허용, WRQ(쓰기) 거부
- 블록 크기 협상(RFC 2348) 지원
- **bootd(Twisted) 프로세스의 리스너 중 하나**로 동작. 포트 69는 systemd 소켓 활성화(6.3)로 fd를 넘겨받음
- 동적 sanboot 스크립트 요청을 받으면 그 자리에서 iSCSI 세션 오케스트레이션(3.4)을 트리거 — 별도 프로세스로 안 쪼갬(사용자 요청)
- **재전송(확정)**: 보낸 패킷(OACK/DATA)마다 재전송 타이머를 걸고 ACK 오면 취소, `MAX_RETRANSMITS`(5회)까지 재시도 후 포기. DHCP와 동일하게 주입 가능한 `clock`으로 테스트

### 3.3 iPXE (SAN 부팅)

**확정**: iPXE가 **자체 iSCSI initiator로 직접 SAN 부팅**을 완결한다(`sanboot`). 네이티브 UEFI iSCSI Boot(펌웨어 내장 이니시에이터)에는 의존하지 않는다 — 벤더별 UEFI iSCSI 구현 편차/버그를 iPXE 하나로 우회하기 위함.

- **부트로더 바이너리**: BIOS는 `undionly.kpxe`, UEFI는 `ipxe.efi`(또는 `snponly.efi`) — TFTP로 전달(3.2), 클라이언트 아키텍처에 따라 DHCP가 boot filename(옵션 67)을 다르게 응답
- **동적 스크립트**: 클라이언트가 iPXE로 부팅되면 자신의 MAC으로 TFTP에 스크립트를 재요청 → bootd가 ClientBinding을 조회해 배정된 이미지의 iSCSI 타겟 정보로 스크립트를 동적 생성해 응답
- **sanboot 명령**: 스크립트는 `sanboot iscsi:<target-ip>::::<target-iqn>` 형태로 iSCSI LUN을 로컬 디스크처럼 노출한 뒤 그 디스크에서 부트로더(Windows bootmgr 등)를 체인로드
- Windows sanboot 시 필요한 iSCSI 관련 드라이버 주입 여부는 PoC 1단계(5장)에서 실제 검증

### 3.4 iSCSI 서버 (커널 LIO)

- **base 이미지**: 표준 raw 파일, 프로파일별로 다수 존재 가능(3.7), 항상 읽기 전용 보관
- **loop device**: 세션에 배정된 base 이미지를 `losetup`으로 블록화
- **dm-snapshot**: 세션별 CoW 오버레이. 일반 사용자 쓰기는 오버레이에만 반영, base 불변
- **LIO(rtslib)**: 스냅샷 디바이스를 LUN으로 export, initiator IQN 기준 ACL
- **오케스트레이션 트리거는 bootd**: 클라이언트가 TFTP로 sanboot 스크립트를 요청하는 시점에 ClientBinding 조회 → loop+snapshot 생성 → LUN/ACL을 미리 등록하고, 그 initiator IQN을 스크립트에 실어 보낸다(3.3) — iPXE가 실제 iSCSI 로그인을 하기 전에 LUN이 이미 준비되어 있음
- 관리자 쓰기 세션(readonly=False)은 Web UI(3.5, 같은 bootd 프로세스)에서 같은 세션 함수를 재사용해 트리거. 로그아웃 시 오버레이 폐기(일반 사용자) 또는 병합(관리자)
- 전 구성요소 Python 구현. loop/dm 조작만 6.2의 전용 헬퍼에 위임
- **클라이언트 세션 리퍼(확정)**: 클라이언트가 실제로 iSCSI 로그아웃/전원 종료해도 그걸 알려주는 이벤트가 없어서, loop/snapshot/LUN이 계속 남아있던 문제가 있었다. `bootd`가 30초 주기로 `active_session`(readonly=1만, 관리자 세션은 Web UI의 명시적 완료/취소로만 정리)을 훑어 `iscsi/lio.py: is_session_active()`로 실제 iSCSI 로그인이 살아있는지 확인하고, 끊긴 세션은 `end_session()`으로 정리한다(`orchestration/session.py: reap_disconnected_sessions`). 재구성에 필요한 정보(loop device 경로, 오버레이 파일 경로 등)를 `active_session` 테이블에 그대로 저장해두고 씀 — rtslib 호출이라 deferToThread로 돌림, `is_session_active`도 다른 rtslib 호출처럼 실제 커널 검증 전까지는 미검증

> 이미지가 여러 개로 늘어나도 LIO 계층 자체는 변경 없음(어떤 이미지를 loop로 열지는 오케스트레이션이 결정). 세션마다 별도 target 대신 target은 고정하고 LUN+ACL만 늘리는 구조 권장. 이미지 간 블록 dedup이 필요해지면 dm-snapshot 대신 ZFS/Btrfs로 백엔드만 교체(LIO는 유지).

### 3.5 Web UI

- Flask/Tornado 없이 **bootd(Twisted) 프로세스에 완전히 통합** — Klein으로 라우팅(Flask 스타일), 별도 프로세스로 안 쪼갬(사용자 요청)
- 템플릿: Jinja2 + MarkupSafe, `select_autoescape`로 자동 이스케이프 필수(XSS 방지)
- 세션/CSRF: itsdangerous로 서명된 쿠키 발급(Secure/HttpOnly) + 폼에 CSRF 토큰 동봉, 상태 변경 요청마다 검증(Klein엔 Tornado 같은 내장 기능이 없어 직접 구현)
- 실시간 대시보드: autobahn WebSocket(HTTP와 별도 TCP 포트) — 같은 프로세스라 세션 상태를 바로 조회 가능하지만, 세션 생성(워커 스레드)과 폴링(reactor 스레드)이 다른 스레드라 SQLite의 `active_session` 테이블을 경유
- 저장소: SQLite (ImageProfile/ImageVersion/ClientBinding/active_session 등)
- 인증: 로컬 admin 계정 + bcrypt 해시
- 화면: 대시보드(실시간 세션), 이미지 프로파일 관리(등록/삭제), 이미지 업데이트(관리자 쓰기 세션)/버전 삭제, 클라이언트-버전 배정, **DHCP 모드 설정**(`/settings` — 값은 `site_setting` 테이블에 저장되고 **다음 bootd 재시작부터** 적용됨, 포트를 systemd 소켓 활성화로 시작 시점에만 받기 때문에 즉시 반영은 불가), 클라이언트/ACL 등록
- 삭제 안전장치: 버전 삭제는 그 버전을 배정받은 클라이언트가 있으면 막음(애플리케이션에서 체크), 프로파일 삭제는 버전/클라이언트 배정이 남아있으면 FK 제약이 막아줌, 편집 중(락)인 프로파일도 삭제 불가
- "이미지 업데이트 시작"은 클라이언트 로그인과 **동일한 세션 생성 로직**을 `readonly=False` + 단독 점유 락으로 재사용(코드 중복 없음)
- **락 복구(확정)**: `_admin_sessions`(진행 중인 관리자 세션)는 bootd 프로세스 메모리에만 있어서, bootd가 재시작되면 DB의 락(`image_profile.locked_by`)만 남고 그걸 풀 방법이 없어지는 문제가 있었다. 그래서 (1) bootd 시작 시 `lock.release_all()`로 남아있는 락을 전부 고아 락으로 간주해 자동 해제하고(막 시작한 프로세스엔 살아있는 세션이 있을 수 없으므로 항상 안전), (2) 프로세스가 안 죽었는데 락만 안 풀리는 경우(브라우저를 닫고 안 돌아옴 등)를 위해 `/images/<id>/force-unlock` 수동 해제 버튼을 두되, 이 프로세스가 그 프로파일 세션을 메모리에 살아있다고 여기는 동안은 거부한다(동시 쓰기 방지).

### 3.6 디스크 스냅샷 / 버전 관리

- 버전 관리는 **ImageProfile 단위로 독립적**
- **저장 ≠ 배포**: 관리자가 새 버전을 만드는 것과 클라이언트가 그 버전을 쓰는 것은 별개 행위. 배포는 PC별 명시적 배정(3.7)으로만 이루어짐
- **관리자 세션(확정, 구현 단계에서 정정)**: dm-snapshot-merge는 쓰지 않는다 — 커널 merge는 origin 파일을 그 자리에서 변형시키므로 "기존 버전 보존"과 충돌한다. 대신 베이스 버전 파일을 복사(가능하면 reflink, 안 되면 일반 복사)해 새 파일을 만들고, 그 복사본을 직접 쓰기 가능한 loop로 마운트한다(스냅샷/오버레이 없음). 세션 종료 시 그 복사본 자체가 새 `ImageVersion`으로 등록된다(자동 활성 없음, 카탈로그 추가만) — 취소하면 복사본만 버림
- **디스크 용량 확장(선택)**: 관리자가 이미지 업데이트 시작 시 "추가 용량"을 지정하면(예: 베이스 10G + 추가 20G), 복사본 파일을 그 크기만큼 truncate로 늘려서 총 용량(30G) 전체가 쓰기 가능한 단일 파일이 된다. base.img와 별도 파일을 device-mapper로 이어붙이는 대신 파일 하나를 확장하는 방식을 택했다 — 원본은 여전히 복사본이라 보존되고, 이어붙이기+CoW 조합이 요구했을 병합 복잡도가 재도입되지 않는다. 늘어난 구간은 스파스(빈 공간)라 즉시 디스크를 소비하지 않으며, Windows 쪽에서는 diskpart/디스크 관리로 볼륨을 확장해야 실제로 그 공간을 쓸 수 있다.
- 일반 사용자 세션: CoW 오버레이, 세션 종료 시 폐기(휘발성)
- 롤백: `ClientBinding.assigned_version`을 이전 값으로 변경 — 파일은 계속 보관되므로 데이터 손실 없음

### 3.7 클라이언트-이미지 매핑 및 버전 배정

```
ImageProfile
  - id, name, storage_dir

ImageVersion
  - profile_id, version_number, file_path, checksum, created_at, created_by

ClientBinding
  - client_mac (또는 initiator IQN)
  - image_profile_id
  - assigned_version   # 필수, 자동 default 없음
```

`ImageProfile`에는 "현재 버전" 자동 활성 개념이 없다. 어떤 클라이언트가 어떤 버전을 쓸지는 오직 `ClientBinding.assigned_version`으로 결정된다.

```python
def on_client_login(initiator_iqn):
    binding = lookup_client_binding(initiator_iqn)
    profile = get_image_profile(binding.image_profile_id)
    img_path = resolve_image_path(profile, binding.assigned_version)

    loop_dev = attach_loop(img_path, readonly=True)      # priv_helper 경유
    snap_dev = create_snapshot(loop_dev, name=f"session-{initiator_iqn}")
    register_lun(initiator_iqn, snap_dev)                 # rtslib
```

**적용 시점**: `assigned_version`은 매 부팅(iSCSI 로그인)마다 그 시점 값으로 조회된다.
- 이미 부팅된 세션은 로그인 시점 스냅샷에 고정 — 배정을 바꿔도 살아있는 세션엔 영향 없음
- 재부팅 시에만 새 배정 반영. 일반 사용자 쓰기는 원래 휘발성이므로 재부팅에 따른 추가 데이터 손실 위험 없음
- 단계적 롤아웃/즉시 롤백이 목적. `locked`/`pending_version` 같은 예약 적용 방식은 현재 범위 밖

---

## 4. 리스크 및 대응

| 리스크 | 대응 |
|---|---|
| LIO(target_core_mod) 커널 모듈 의존성 | 배포 대상 커널/배포판 사전 확인 |
| 동시 다수 클라이언트 부팅 시 loop/dm 확장성 | 대량 동시 부팅 부하 테스트 필요 |
| 관리자 쓰기 세션 중 동시 편집 충돌 | 프로파일 단위 단독 점유 락(`image_profile.locked_by`, `orchestration/lock.py`) — 잡혀 있으면 새 편집 시작 요청은 409 |
| 대용량 이미지 복사(reflink 미지원 파일시스템) 시 관리자 세션 시작 지연 | Btrfs/XFS 등 reflink 지원 파일시스템 사용 권장, PoC에서 실측 |
| iPXE sanboot로 Windows 부팅 시 드라이버/트릭 필요 여부 미검증 | PoC 1단계(5장)에서 실제 하드웨어로 검증 |

---

## 5. 로드맵 (PoC 단계)

1. loop + dm-snapshot + LIO 수동 구성 + iPXE sanboot → Windows 클라이언트 1대 iSCSI 부팅 성공 검증(이미지 1개)
2. bootd(Twisted) 오케스트레이션 계층 구현(세션 생명주기, `priv_helper.py` 연동)
3. Standalone/Proxy DHCP + TFTP(iPXE 바이너리·동적 sanboot 스크립트) 통합, 부팅 체인 자동화
4. 다중 이미지 프로파일 지원(ImageProfile/ClientBinding, 저장≠배포)
5. bootd에 통합된 Klein 관리 웹 UI(인증/SQLite/대시보드/버전 배정 화면)
6. 다중 클라이언트 동시 부팅 부하 테스트 및 안정화

**테스트 환경**: 1~5단계는 물리 PC/네트워크 없이 QEMU/KVM(libvirt) 또는 VirtualBox로 검증 가능 — 서버 VM 1대 + Windows 클라이언트 VM을 격리된 가상 네트워크로 연결. iSCSI(LIO)만 먼저 `iscsiadm`으로, PXE→iPXE 체인은 별도로 검증한 뒤 합치는 순서 권장. 물리 하드웨어는 벤더별 NIC/UEFI PXE ROM 편차를 확인하는 최종 검증(6단계 전후)에만 필요.

---

## 6. 구현 참고

> 실제 코드가 `src/diskless/`에 TDD로 작성되기 시작한 뒤로는, 아래 코드 조각은 "개념 설명용"이며
> 정확한 최신 시그니처는 소스(`priv_helper/server.py`, `orchestration/session.py`, `bootd/`)를
> 참고한다. 테스트는 `tests/`에 있으며 `uv run pytest`로 실행한다(VM/네트워크 불필요, subprocess와
> rtslib 호출은 전부 모킹). `src/diskless/` 전체 소스 파일에 대응하는 테스트가 있다 —
> `bootd/main.py`는 `reactor.adoptDatagramPort`/`listenUDP`/`listenTCP`를 모킹해 배선 로직을,
> `bootd/websocket.py`는 등록/해제/브로드캐스트를, `bootd/systemd_sockets.py`는
> `LISTEN_FDNAMES` 파싱을 검증한다 — 셋 다 "우리 코드가 무엇을 호출하는지"까지고,
> 실제 커널/소켓 동작은 여전히 LIO(rtslib)와 마찬가지로 Linux 환경에서만 검증 가능하다.

### 6.1 LIO 수동 구성

```bash
sudo modprobe target_core_mod iscsi_target_mod
sudo apt install targetcli-fb

# base.img → loop(읽기전용) → dm-snapshot → LIO export
sudo losetup -f --show -r base.img                 # → /dev/loop0
truncate -s 1G overlay.cow
sudo losetup -f --show overlay.cow                  # → /dev/loop1
sudo dmsetup create session1-snap --table \
  "0 $(blockdev --getsz /dev/loop0) snapshot /dev/loop0 /dev/loop1 P 8"
                                                      # → /dev/mapper/session1-snap
```

```
# targetcli
/backstores/block create name=session1 dev=/dev/mapper/session1-snap
/iscsi create iqn.2026-09.local.diskless:session1
/iscsi/iqn.../tpg1/luns create /backstores/block/session1
/iscsi/iqn.../tpg1/acls create iqn.<클라이언트-iqn>
/iscsi/iqn.../tpg1/portals create <서버IP> 3260
saveconfig
```

Python(`rtslib-fb`)으로 동일하게 제어 — **target은 서버당 하나로 고정하고(`iqn...:server`),
세션마다 그 밑에 LUN만 추가한 뒤 ACL을 `MappedLUN`으로 그 LUN 하나만 보이게 제한한다**
(세션마다 별도 target을 만들지 않음 — `src/diskless/iscsi/lio.py` 참고, `tests/iscsi/`에
가짜 rtslib_fb로 호출 구조를 검증해뒀다. 실제 커널 동작은 Linux 환경에서 별도 확인 필요):

```python
from rtslib_fb import BlockStorageObject, Target, TPG, LUN, NodeACL, MappedLUN, NetworkPortal

bs = BlockStorageObject("session1", dev="/dev/mapper/session1-snap")
target = Target(fabric="iscsi", wwn="iqn.2026-09.local.diskless:server")  # 고정 target
tpg = TPG(target, tag=1)
tpg.enable = True
NetworkPortal(tpg, "0.0.0.0", 3260)

lun = LUN(tpg, storage_object=bs)
acl = NodeACL(tpg, "iqn.<클라이언트-iqn>")
MappedLUN(acl, mapped_lun=0, tpg_lun=lun.lun)  # 이 initiator에게는 이 LUN 하나만 보임
```

### 6.2 권한 확보: systemd AmbientCapabilities

`priv_helper.py`만 `CAP_SYS_ADMIN`을 ambient로 보유, bootd(Twisted) 프로세스는 capability 없음.

```python
# priv_helper.py — 화이트리스트된 작업만 노출, Unix 소켓 RPC
import socket, json, subprocess, os

SOCK_PATH = "/run/diskless/priv-helper.sock"

def attach_loop(args):
    cmd = ["losetup", "-f", "--show"] + (["-r"] if args.get("readonly") else []) + [args["img_path"]]
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()

def detach_loop(args):
    subprocess.run(["losetup", "-d", args["dev"]], check=True)

def create_snapshot(args):
    subprocess.run(["dmsetup", "create", args["name"], "--table", args["table"]], check=True)

ALLOWED_OPS = {"attach_loop": attach_loop, "detach_loop": detach_loop, "create_snapshot": create_snapshot}

def serve():
    if os.path.exists(SOCK_PATH):
        os.unlink(SOCK_PATH)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK_PATH)
    os.chmod(SOCK_PATH, 0o660)
    srv.listen(8)
    while True:
        conn, _ = srv.accept()
        try:
            req = json.loads(conn.recv(65536))
            op = req["op"]
            if op not in ALLOWED_OPS:
                raise ValueError(f"disallowed op: {op}")
            conn.sendall(json.dumps({"result": ALLOWED_OPS[op](req.get("args", {}))}).encode())
        except Exception as e:
            conn.sendall(json.dumps({"error": str(e)}).encode())
        finally:
            conn.close()
```

```ini
# /etc/systemd/system/diskless-priv-helper.service
[Service]
User=diskless-priv
AmbientCapabilities=CAP_SYS_ADMIN
CapabilityBoundingSet=CAP_SYS_ADMIN
NoNewPrivileges=true
ExecStart=/usr/bin/python3 /opt/diskless/priv_helper.py
Restart=on-failure
```

메인 앱 측 호출:

```python
def call_priv_helper(op, **args):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(SOCK_PATH)
        s.sendall(json.dumps({"op": op, "args": args}).encode())
        resp = json.loads(s.recv(65536))
    if "error" in resp:
        raise RuntimeError(resp["error"])
    return resp["result"]
```

LIO(configfs)는 capability 대신 그룹 권한으로 처리:

```bash
sudo chgrp -R diskless-priv /sys/kernel/config/target
sudo chmod -R g+ws /sys/kernel/config/target
```

### 6.3 포트 바인딩: systemd 소켓 활성화

TFTP(69), Standalone DHCP(67/68)는 컴파일 바이너리가 없어 setcap 대상이 없으므로, systemd가 root로 미리 bind해둔 소켓 fd를 넘겨받는 방식을 쓴다. 프로세스는 시작부터 끝까지 일반 유저.

```ini
# /etc/systemd/system/diskless-tftp.socket
[Socket]
ListenDatagram=69
Accept=no
[Install]
WantedBy=sockets.target
```

```ini
# /etc/systemd/system/diskless-tftp.service
[Unit]
Requires=diskless-tftp.socket
[Service]
ExecStart=/usr/bin/python3 /opt/diskless/tftp_server.py
User=diskless
```

```python
import socket
sock = socket.socket(fileno=3)  # systemd LISTEN_FDS 규약, bind() 호출 없음
```

Standalone DHCP(67/68)도 `ListenDatagram=67`로 동일 적용.

### 6.4 권한 확보 방식 요약

| 구성요소 | 방식 |
|---|---|
| loop/dm-snapshot 조작 | `priv_helper.py` + `AmbientCapabilities=CAP_SYS_ADMIN` |
| LIO(rtslib) 조작 | configfs 그룹 권한(setgid) |
| Standalone DHCP(67/68) | systemd 소켓 활성화 |
| Proxy DHCP(4011) | 특권 포트 아님, 상승 불필요 |
| TFTP(69) | systemd 소켓 활성화 |
| bootd 프로세스 전체(DHCP/TFTP/iSCSI/Web UI) | 완전히 일반 유저, root로 실행되는 구간 자체가 없음 |

### 6.5 최초 배포 시 필요한 절차

- **관리자 계정 부트스트랩**: `admin_user` 테이블은 스키마만 있고 자동으로 채워지지 않는다 — 배포 후 `diskless-create-admin <username>` (비밀번호는 프롬프트 또는 `--password`)으로 최초 계정을 만든다. 같은 username으로 다시 실행하면 비밀번호 갱신(upsert).
- iPXE 바이너리(`undionly.kpxe`, `ipxe.efi`)는 이 저장소에 포함되어 있지 않다 — `tftp_root`에 별도로 받아 둬야 한다.
