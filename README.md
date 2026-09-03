# diskless

PXE + iSCSI 기반 디스크리스 부팅 서버.

- DHCP(Standalone/Proxy) + TFTP + iSCSI 세션 오케스트레이션 + 관리 웹 UI를 단일 Twisted 프로세스(`bootd`)로 실행
- 루트 권한이 필요한 loop/dm-snapshot/LIO 조작만 별도 프로세스(`priv_helper`, systemd `AmbientCapabilities`)로 분리
- 클라이언트별 base 이미지 + 버전 관리, dm-snapshot 기반 CoW 세션, 연결 종료 시 자동 세션 정리

설계 배경과 상세 아키텍처는 [DESIGN.md](DESIGN.md), 원 스펙은 [AGENTS.md](AGENTS.md) 참고.

## 개발

```bash
uv sync
uv run pytest
```
