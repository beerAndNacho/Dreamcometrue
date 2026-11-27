# Macro Invest 4.0: Dreamcometrue 웹 배포

대상 저장소: https://github.com/beerAndNacho/Dreamcometrue.git

3.0은 지정 저장소 main에 반영된 상태입니다. 4.0 업데이트는 별도 브랜치에서 검증 후 병합하세요. 실제 Render 배포 성공 여부는 별도 확인해야 합니다. 기존 파일을 강제로 덮어쓰거나 force push하지 마세요.

## 1. GitHub 반영

- GitHub 계정에서 해당 저장소 쓰기 권한을 확인합니다.
- 기존 저장소를 clone하고 현재 브랜치와 파일을 확인합니다. 빈 저장소라면 이 ZIP 내부 프로젝트의 파일들을 루트에 복사할 수 있습니다. 기존 프로젝트가 있다면 충돌하지 않는 `macro-invest/` 하위 디렉터리에 파일을 복사하고 새 브랜치에서 PR을 만드세요.
- `.env`, `data/`, 키 파일, 로컬 데이터베이스, 비밀번호 파일은 커밋하지 않습니다. `.env.example`만 예제입니다.
- GitHub Desktop 또는 로그인된 git CLI로 변경사항을 검토하고 커밋 및 push합니다. 배포할 브랜치를 확인합니다.

## 2. Render 배포

공식 설정 문서: https://render.com/docs/blueprint-spec

루트에 프로젝트를 배치한 경우 Render Dashboard(https://dashboard.render.com/)에서 New > Blueprint로 연결한 GitHub 저장소와 브랜치를 선택합니다. `render.yaml`을 확인하고 비용을 검토한 뒤 생성합니다. **이 파일은 유료 컴퓨트와 1GB 영구 디스크를 요청합니다.** 요금제 및 비용은 결제 전 직접 확인하세요. 이 파일은 실제 클라우드에서 실행 검증되지 않았습니다.

하위 디렉터리에 배치했다면 Blueprint 파일 위치를 지정하고, 서비스 정의에 `rootDir: macro-invest`를 추가합니다. 또는 New > Web Service에서 GitHub 저장소, Docker 런타임, Root Directory `macro-invest`, 배포 브랜치를 선택하고 아래 환경 설정과 디스크를 수동으로 구성합니다. 기존 서비스 이름과 충돌하지 않는 이름을 선택하세요.

- 런타임: Docker / 단일 인스턴스 / Singapore 권장
- HTTP health check: `/healthz`
- 영구 디스크: `/app/data`, 1GB 이상
- `HOSTED_MODE=1`, `DEMO_MODE=0`, `DATABASE_URL=sqlite:////app/data/invest.db`, `SETTINGS_DIR=/app/data/settings`
- `ADMIN_PASSWORD`: Render에서 생성한 24자 이상의 랜덤 비밀번호. 비밀값으로만 등록합니다.
- `AUTO_COLLECT=0`, `COLLECT_HOUR_KST=8`, `TZ=Asia/Seoul`
- 기본 도메인은 `RENDER_EXTERNAL_HOSTNAME` 자동 환경변수를 사용합니다. 사용자 도메인을 연결했다면 `PUBLIC_ORIGIN=https://실제도메인`을 지정하세요(단일 호스트, 포트/경로 없이).
- 이 버전은 Render의 HTTPS 진입점과 단일 프로세스를 전제로 합니다. 직접 HTTP 포트를 공인 인터넷에 노출하지 마세요.
- 웹 서비스 및 영구 디스크 비용: https://render.com/pricing . 기존 Docker Compose PostgreSQL 구성을 유지했지만, 개인용 Render 구성에는 비용 절감을 위해 영구 디스크 SQLite를 선택했습니다. PostgreSQL 사용 시 별도 관리형 DB를 생성하고 `DATABASE_URL`에 내부 연결 문자열을 입력할 수 있습니다(영구 디스크는 API 키 저장에 계속 필요).

## 3. 접속 및 최초 수집

Render에서 발급된 **실제 HTTPS 주소**를 확인합니다. 주소가 발급되기 전에는 접속할 수 없습니다.

- `/`: 사용자 대시보드. 브라우저 Basic 인증으로 로그인합니다.
- `/admin`: 관리자 페이지. 동일 계정으로 로그인합니다.
- `/healthz`: 비인증 상태 점검(데이터베이스 연결 여부만 반환).
- 계정: `admin`. 비밀번호는 Render의 Environment에서 확인합니다. 채팅이나 저장소에 공개하지 마세요.
- 관리자에서 API 키 및 KOSIS 항목을 등록하고 연결 검사, 수동 수집을 실행합니다. 필요할 경우 자동 수집을 활성화합니다.
- 인증되지 않은 외부 사용자는 대시보드/API/정적 파일에 접근할 수 없습니다. HTTPS 및 높은 엔트로피의 비밀번호를 반드시 사용하세요. 다중 사용자 권한 분리, 별도 MFA 및 침투 테스트는 제공하지 않습니다.
- 영구 디스크의 데이터베이스, 설정 암호문, 암호화 키를 함께 보호하고 정기적으로 백업하세요. 암호화 키 분실 시 설정을 복구할 수 없습니다. 디스크/서버 접근 권한을 제한하세요.

## 4. 업그레이드 및 검증

실제 GitHub push, Render 리소스 생성, 외부 도메인 발급 및 실데이터 API 연결은 수행되지 않았습니다. 기존 테스트 및 hosted mode 인증 검사를 실행했으며 아래 VALIDATION.md에 기록했습니다.

3.0은 기존 영구 디스크를 유지합니다. 배포 전 DB/설정 키를 백업하고 README의 KOSIS 출처 이력 초기화 절차를 확인하세요. GitHub Actions CI가 Python 3.11/3.12에서 실행되도록 구성했으나 실제 GitHub Actions는 실행하지 않았습니다.


## 4. 3.x → 4.0 업그레이드

- 기존 서비스/DB 경로, 설정 디렉터리, Docker 볼륨을 유지합니다. 먼저 백업을 수행합니다. SQLite는 서비스 정지 후 WAL과 일관성을 고려해 DB를 백업하고, PostgreSQL은 pg_dump 등 공식 도구로 백업합니다. 설정 디렉터리의 settings.key/settings.enc도 함께 보호하여 백업합니다.
- 4.0 코드를 받은 뒤 동일 환경변수 및 단일 worker로 재시작/재배포합니다. 새 일별 스냅샷 테이블은 시작 시 생성되며 기존 테이블 삭제가 필요 없습니다. 초기 비교 내역이 비어 있는 것은 정상입니다.
- `/healthz`, 로그인, 관리자 상태, 실 API 연결 검사와 전체 수집을 실행합니다. 대시보드의 38개 지표 상태 및 브리핑, 보유종목 저장본을 확인합니다. 최신 데이터 확인 전 투자 모형을 사용하지 않습니다.
- 실제 배포 자동화가 연결된 경우 main 병합 시 배포가 시작될 수 있습니다. 배포 브랜치, CI 결과, 요금 및 백업을 확인한 뒤 병합하세요.


## 4.1 배포
4.0 업데이트와 동일한 백업/DB 경로 유지 정책입니다. 신규 `kr_market_cache`, `kr_market_watch` 테이블은 시작 시 자동 생성되므로 기존 파일이나 볼륨을 삭제하지 마세요. `KIS_APP_KEY`, `KIS_APP_SECRET`, `DART_API_KEY`는 선택형 환경 변수 또는 관리자 화면에서 설정합니다. 공개 저장소에 키/토큰/개인 관심종목/DB 파일을 올리지 마세요. 실전 KIS API 연결 및 OpenDART 기업목록·공시 확인, KRX 대조는 실제 배포에서 수행해야 합니다. 기존 4.0 배포 브랜치에 반영하기 전 백업과 CI 확인을 권장합니다.
