# GitHub 배포 소스 패키지

ZIP의 내용물을 저장소 루트에 올리세요. GitHub 업로드/커밋/서버 배포는 자동 수행하지 않았습니다.

- 현재 수정 소스 전체, requirements, 설정 예제와 deployment 설치 자료 포함.
- 운영 DB, pb_data, .env, API 키, 로그, 가상환경, 테스트 백업 제외.
- 기존 저장소에 이미 추적된 pb_data/data.db 등은 새 ZIP을 올리는 것만으로 제거되지 않습니다. GitHub 저장소에서 계정 DB와 인증값을 제거하고 이력 노출도 점검하세요. 서버의 운영 DB는 삭제하지 마세요.
- deployment/cloud-config-debian12.yaml은 새 Debian 12 amd64 서버 전용입니다. GitHub main에 이 ZIP 내용을 반영한 뒤 사용하세요.
- 새 서버는 빈 PocketBase DB이므로 SSH 터널로 새 관리자 계정을 만들고 .env를 맞춰야 합니다. 헷지 사용 시 kimp 스키마도 필요합니다.
- 기존 서버는 deployment/적용안내.md의 fix_alerts.py 경로를 따르세요. cloud-config를 재실행하거나 서버 DB를 덮어쓰지 마세요.
- 새 알림 플래그 6개가 True여야 하며, 확인되지 않은 손익은 미확정으로 표시됩니다.
- 검증: Windows Python 3.12에서 기존 239 + 보안 15, Golden 123, 시간/설치 3 통과. Linux 설치 및 512MB 운영 성능 승인은 미완료입니다.
- 소스 설정의 공개 기본 DB 인증값은 운영 인증값으로 사용하지 마세요. 새 설치 템플릿은 빈 관리자 인증값으로 시작하고 기동 전 필수 설정을 검사합니다.
