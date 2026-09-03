# infra-github-to-notion

GitHub 조직의 저장소 중 **이름 규칙에 맞는 것만** 골라 Notion 데이터베이스에 자동 등록·갱신하는 AWS Lambda.

Notion 내장 GitHub 동기화는 이슈와 PR만 지원하고 저장소 목록 자체는 다루지 못한다. 이 스크립트는 GitHub REST API로 저장소 메타데이터를 직접 가져와 원하는 스키마로 카탈로그를 만든다.

외부 의존성이 없다. 표준 라이브러리만 쓰므로 배포 패키지는 `.py` 파일 하나다.

## 동작 방식

조직의 모든 저장소를 페이지네이션으로 수집한 뒤 이름 규칙으로 거르고, Notion DB에 업서트한다.

업서트 키는 저장소 이름이 아니라 **GitHub의 숫자 `id`** 다. 레포 이름이 바뀌어도 새 행이 생기지 않고 기존 행이 갱신된다.

매 실행마다 전체를 다시 쓰지 않는다. 동기화 대상 값들의 SHA-256 해시를 `Sync Signature` 프로퍼티에 저장해두고, 다음 실행에서 해시가 같으면 쓰기를 건너뛴다. 두 번째 실행부터는 실제로 바뀐 저장소만 API를 호출하며, Notion의 "최종 편집일시"도 불필요하게 갱신되지 않는다.

### 안전장치

| 상황 | 동작 |
|---|---|
| 매칭 규칙이 하나도 없음 | 실행 차단. 조직 전체가 등록되는 사고를 막는다 |
| 규칙에서 벗어난 저장소 | 기본적으로 **행을 보존**한다. `ARCHIVE_UNMATCHED=true` 로 자동 정리 선택 가능 |
| `Repo ID` 가 비어 있는 행 | 사람이 직접 만든 행으로 보고 건드리지 않는다 |
| Slack 알림 실패 | 원본 예외를 가리지 않는다. 알림 예외는 삼키고 동기화 오류를 그대로 올린다 |

## Notion DB 스키마

`init` 잡이 아래 16개 프로퍼티를 갖춘 DB를 만들어준다.

> DB 를 다른 페이지로 **이동**하면 integration 권한 상속이 끊겨 접근이 404 가 된다.
> 이동 후에는 새 위치에서 Connections 를 다시 추가해야 한다. DB **제목** 변경은 안전하지만,
> **프로퍼티 이름**은 바꾸면 안 된다 (`Repo ID` 는 업서트 키, `Sync Signature` 는 변경 감지용).
> 보기 싫은 컬럼은 삭제 대신 Notion 뷰에서 숨긴다.
>
> `.env` 를 고쳐도 람다에는 반영되지 않는다. 규칙이나 옵션을 바꿨으면 `./deploy.sh` 를 다시 돌려야
> 다음 자동 실행에 적용된다.

| 프로퍼티 | 타입 | 출처 |
|---|---|---|
| `Name` | title | 저장소 이름 |
| `Full Name` | rich_text | `org/repo` |
| `Repo ID` | number | GitHub 숫자 id (업서트 키) |
| `URL` | url | 저장소 링크 |
| `Description` | rich_text | 설명 (2000자 초과 시 절단) |
| `Language` | select | 주 언어 |
| `Visibility` | select | public / private |
| `Topics` | multi_select | 토픽 태그 |
| `Stars` | number | 스타 수 |
| `Open Issues` | number | 열린 이슈 수 |
| `Default Branch` | rich_text | 기본 브랜치 |
| `Pushed At` | date | 마지막 푸시 |
| `Created At` | date | 생성일 |
| `Last Committer` | select | 마지막 커밋 작성자 (GitHub login) |
| `Last Commit At` | date | 마지막 커밋 시각 |
| `Sync Signature` | rich_text | 변경 감지용 해시 (건드리지 말 것) |

프로퍼티 이름을 바꾸려면 `github_to_notion.py` 상단의 `P_*` 상수만 고치면 된다. `init` 잡이 같은 이름으로 DB를 만들기 때문에 양쪽이 함께 바뀐다.

## 사전 준비

### 1. Notion Integration

[notion.so/profile/integrations](https://www.notion.so/profile/integrations) 에서 발급한다. 토큰은 `ntn_` 으로 시작하며 한 번만 표시된다.

필요한 Capabilities:

- **Content**: Read content, Update content, Insert content (3개 모두)
- **Comment**: 불필요
- **User**: `No user information` 으로 충분 (스키마에 people 타입이 없다)

> **가장 흔한 실패 지점.** Capabilities 를 다 켜도, 대상 페이지에서 `⋯` → **연결(Connections)** 로 integration 을 추가하지 않으면 API 가 `object_not_found` 를 돌려준다. 권한 문제처럼 보이지 않아 헷갈리기 쉽다.

### 2. GitHub Token

[github.com/settings/personal-access-tokens](https://github.com/settings/personal-access-tokens) 에서 fine-grained PAT 을 발급한다.

- **Resource owner 를 본인 계정이 아니라 대상 조직으로 지정**해야 한다
- **Repository access 는 `All repositories`** 로 한다. `Only select repositories` 로 하면 그 시점의 저장소만 보여서,
  나중에 만들어지는 저장소가 잡히지 않아 자동 등록의 목적이 무너진다
- 권한은 Repository permissions → **Metadata: Read-only** 하나면 충분하다 (코드 읽기/쓰기 불가)

Resource owner 를 조직으로 지정하면 토큰이 `pending` 상태로 발급되고 조직 관리자 승인이 필요하다 (본인이 조직 owner 면 자동 승인). **승인 전까지는 public 리소스만 읽힌다.** 즉 비공개 저장소가 통째로 빠진 카탈로그가 만들어지며, 규칙이 잘못된 것처럼 보이지만 실제로는 승인 문제다.

조직이 목록에 아예 안 보이면 그 조직이 fine-grained PAT 을 차단한 것이다.

## 환경 변수

`.env.example` 을 `.env` 로 복사해 채운다. `.env` 는 `.gitignore` 로 커밋에서 제외된다.

| 변수 | 필수 | 설명 |
|---|---|---|
| `GITHUB_TOKEN` | ✅ | fine-grained PAT (Metadata: Read-only) |
| `GITHUB_ORG` | ✅ | 조직 슬러그. `github.com/<여기>` |
| `NOTION_API_KEY` | ✅ | `ntn_` 로 시작하는 integration 토큰 |
| `REPO_NAME_PREFIX` | 둘 중 하나 | 콤마 구분 접두사. 예: `svc-,lambda-` |
| `REPO_NAME_REGEX` | 둘 중 하나 | 정규식. 설정되면 PREFIX 는 무시된다. 예: `^(svc\|lambda)-` |
| `NOTION_PARENT_PAGE_ID` | init 시 | DB 를 만들 부모 페이지. 생성 후엔 불필요 |
| `NOTION_DB_INLINE` | | 기본 `true`. 페이지 본문에 embed 되는 인라인 DB 로 만든다. `false` 면 전체 페이지형 |
| `NOTION_DB_TITLE` | | DB 제목. 비우면 `{조직명} Repositories` |
| `NOTION_REPO_DB_ID` | sync 시 | `init` 이 출력해주는 값 |
| `NOTION_REPO_DATA_SOURCE_ID` | | 비우면 DB 의 첫 data source 를 자동 선택 |
| `INCLUDE_FORKS` | | 기본 `true`. `false` 면 포크 제외 |
| `INCLUDE_ARCHIVED` | | 기본 `true`. `false` 면 아카이브 제외 |
| `ARCHIVE_UNMATCHED` | | 기본 `false`. `true` 면 규칙에서 벗어난 행을 보관 처리 |
| `DRY_RUN` | | `true` 면 쓰기 없이 계획만 출력 |
| `SLACK_BOT_TOKEN` | | 실패 알림용. `SLACK_CHANNEL` 과 함께 있어야 동작 |
| `SLACK_CHANNEL` | | 알림 채널 ID |

`GITHUB_ORG` 는 **이 저장소의 위치가 아니라 카탈로그로 만들 대상 조직**이다.

## 사용법

로컬 실행 시 `.env` 를 자동으로 읽는다. 실제 환경 변수가 있으면 그쪽이 우선한다.

```bash
# 0) 저장소 이름을 훑어보며 매칭 규칙을 정한다 (Notion 자격 증명 불필요)
python3 github_to_notion.py list

# 1) Notion DB 생성 (최초 1회) → 출력된 DB ID 를 .env 에 반영
python3 github_to_notion.py init

# 2) 어떤 저장소가 잡히는지 먼저 눈으로 확인
DRY_RUN=true python3 github_to_notion.py sync

# 3) 실제 동기화
python3 github_to_notion.py sync
```

### 매칭 규칙 정하기

`list` 는 조직의 저장소 이름을 정렬해 보여주고, 규칙이 설정돼 있으면 매칭되는 것에 `✓` 를 붙인다.
`GITHUB_TOKEN` 과 `GITHUB_ORG` 만 있으면 되므로 Notion DB 를 만들기 전에 규칙부터 확정할 수 있다.

```
acme 조직 저장소 5개

    docs-site
  ✓ lambda-notify  [private]
    legacy-tool  [fork, archived]
  ✓ svc-auth  [private]
  ✓ svc-billing  [private]

규칙 'svc-,lambda-' → 3/5개 매칭 (✓ 표시)
```

규칙을 바꿔가며 `list` 를 다시 돌려 원하는 집합이 나올 때까지 맞춘 뒤 `init` 으로 넘어가면 된다.
접두사로 충분하면 `REPO_NAME_PREFIX`(콤마 구분), 더 복잡한 조건이면 `REPO_NAME_REGEX` 를 쓴다.
정규식은 `re.search` 로 평가되므로 앞부분만 맞추려면 `^` 를 붙여야 한다.

## 배포

```bash
./deploy.sh           # 패키징 → IAM 역할 → 함수 생성/갱신 → EventBridge 스케줄
./deploy.sh package   # zip 만들기만
./deploy.sh invoke    # 지금 즉시 한 번 실행
./deploy.sh logs      # 최근 1시간 로그
```

| 항목 | 값 |
|---|---|
| 함수명 | `github-to-notion` (`FUNCTION_NAME` 으로 변경 가능) |
| 리전 | `ap-northeast-1` (`AWS_REGION` 으로 변경 가능) |
| 런타임 | `python3.12` |
| 타임아웃 / 메모리 | 300초 / 256MB |
| 스케줄 | `cron(0 0 * * ? *)` = 매일 09:00 KST |
| IAM 역할 | `github-to-notion-role` (`AWSLambdaBasicExecutionRole`) |

배포 스크립트는 자격 증명을 명령행 인자가 아니라 **권한 600 파일**로 전달해 `ps` 노출을 막고, 배포 후 삭제한다. `.env` 의 모든 값을 넘기지 않고 화이트리스트로 걸러서 `init` 전용인 `NOTION_PARENT_PAGE_ID` 와 `DRY_RUN` 은 람다에 올라가지 않는다. 필수값이나 매칭 규칙이 없으면 AWS 를 호출하기 전에 중단한다.

## 문제 해결

| 증상 | 원인 |
|---|---|
| `object_not_found` | 대상 페이지의 Connections 에 integration 이 추가되지 않음 |
| 비공개 저장소만 누락됨 | 토큰이 조직 관리자 **승인 대기(pending)** 중. 승인 전에는 public 리소스만 읽힌다 |
| 수집 개수가 0 | Resource owner 가 조직이 아니라 개인 계정으로 지정됨 |
| 나중에 만든 저장소가 안 잡힘 | 토큰의 Repository access 가 `Only select repositories` 로 되어 있음. `All repositories` 여야 한다 |
| 매칭 0개 | `REPO_NAME_PREFIX` / `REPO_NAME_REGEX` 확인. `DRY_RUN=true` 로 먼저 점검 |
| 행이 중복 생성됨 | `Sync Signature` 나 `Repo ID` 프로퍼티를 지웠는지 확인 |
| `[설정 필요] ...` | 해당 환경 변수가 비어 있음. 메시지가 발급처를 안내한다 |

## 라이선스

사내 인프라용 저장소.
