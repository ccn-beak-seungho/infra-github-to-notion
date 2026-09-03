"""
GitHub 조직 저장소 → Notion DB 동기화 Lambda

이름 패턴에 매칭되는 조직 저장소를 찾아 Notion 데이터베이스에 자동 등록/갱신한다.
의존성 없이 표준 라이브러리만 사용한다 (lambda_function.py와 동일한 방침).

EventBridge 입력의 job 값으로 동작 분기:
  {"job": "sync"} 또는 미지정 → 저장소 동기화 (기본)
  {"job": "init"}             → Notion DB를 스키마와 함께 새로 생성
  {"job": "list"}             → 저장소 이름 목록 출력 (규칙 정하기용, Notion 불필요)
"""

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ── .env 로더 (로컬 실행용) ──────────────────────────────────
# Lambda에서는 환경 변수가 이미 주입되므로 건너뛴다.
def _load_dotenv(path: str = "") -> None:
    if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        return
    # cron은 임의의 cwd에서 실행되므로 스크립트 파일 기준으로 찾는다.
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip("'\"")
            # 이미 설정된 실제 환경 변수가 우선한다.
            if key and value and key not in os.environ:
                os.environ[key] = value


_load_dotenv()


def _required(key: str, hint: str) -> str:
    """필수 환경 변수를 읽는다. 없으면 무엇을 어디서 발급하는지 알려준다."""
    value = os.environ.get(key, "").strip()
    if not value:
        raise SystemExit(f"[설정 필요] {key} 가 비어 있습니다. {hint}")
    return value


# ── 환경 변수 ────────────────────────────────────────────────
GITHUB_TOKEN = _required("GITHUB_TOKEN",
    "github.com/settings/personal-access-tokens 에서 Metadata: Read-only 권한으로 발급하세요.")
GITHUB_ORG   = _required("GITHUB_ORG",
    "GitHub 조직 슬러그입니다. 조직 페이지 URL의 github.com/<여기> 부분입니다.")

# list 잡은 Notion 을 쓰지 않으므로 import 시점에 강제하지 않고,
# 실제 호출 직전(notion_request)에 검증한다.
NOTION_API_KEY   = os.environ.get("NOTION_API_KEY", "").strip()
NOTION_REPO_DB_ID = os.environ.get("NOTION_REPO_DB_ID", "")

# 특정 data source를 직접 지정하고 싶으면 설정 (없으면 자동 선택)
NOTION_REPO_DATA_SOURCE_ID   = os.environ.get("NOTION_REPO_DATA_SOURCE_ID", "")
NOTION_REPO_DATA_SOURCE_NAME = os.environ.get("NOTION_REPO_DATA_SOURCE_NAME", "")

# init 잡으로 DB를 새로 만들 때 부모가 될 노션 페이지 ID
NOTION_PARENT_PAGE_ID = os.environ.get("NOTION_PARENT_PAGE_ID", "")

# ── 매칭 규칙 ────────────────────────────────────────────────
# REPO_NAME_REGEX 가 있으면 그것을 우선 사용하고, 없으면 REPO_NAME_PREFIX 목록을 쓴다.
#   REPO_NAME_REGEX  예: "^(svc|lambda)-.+"
#   REPO_NAME_PREFIX 예: "svc-,lambda-"
REPO_NAME_REGEX  = os.environ.get("REPO_NAME_REGEX", "")
REPO_NAME_PREFIX = [
    p.strip()
    for p in os.environ.get("REPO_NAME_PREFIX", "").split(",")
    if p.strip()
]

# 이름 패턴 외 추가 필터. 기본값은 '이름 패턴만 적용'이 되도록 모두 포함으로 둔다.
INCLUDE_FORKS    = os.environ.get("INCLUDE_FORKS", "true").lower() == "true"
INCLUDE_ARCHIVED = os.environ.get("INCLUDE_ARCHIVED", "true").lower() == "true"

# 규칙에 더 이상 맞지 않는 페이지를 노션에서 보관 처리할지 여부.
# 기본은 false (비파괴적). true로 두면 매칭이 풀린 레포의 페이지가 DB에서 사라진다.
ARCHIVE_UNMATCHED = os.environ.get("ARCHIVE_UNMATCHED", "false").lower() == "true"

# 실제 쓰기 없이 계획만 로그로 출력
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

# ── init 잡 DB 생성 옵션 ─────────────────────────────────────
# 인라인 DB(페이지 본문에 embed)로 만들지 여부. false 면 전체 페이지형.
NOTION_DB_INLINE = os.environ.get("NOTION_DB_INLINE", "true").lower() == "true"
# DB 제목. 비우면 "{조직명} Repositories".
NOTION_DB_TITLE  = os.environ.get("NOTION_DB_TITLE", "").strip()

# ── Slack 실패 알림 (선택) ───────────────────────────────────
# 둘 다 설정된 경우에만 알림을 보낸다. 없으면 조용히 건너뛴다.
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL   = os.environ.get("SLACK_CHANNEL", "")

# 2025-09-03 이상이어야 data_sources API 사용 가능
NOTION_VERSION = "2025-09-03"
GITHUB_API_VERSION = "2022-11-28"

HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "10"))

# Notion 쓰기 요청 사이 대기(초). 공식 권장치는 평균 초당 3회.
NOTION_WRITE_INTERVAL = float(os.environ.get("NOTION_WRITE_INTERVAL", "0.34"))

# ── Notion 프로퍼티명 ────────────────────────────────────────
# init 잡이 이 이름 그대로 DB를 만들기 때문에 여기만 고치면 양쪽이 함께 바뀐다.
# 기존 DB에 연결한다면 실제 프로퍼티명과 일치시켜야 한다.
P_NAME        = "Name"            # title
P_FULL_NAME   = "Full Name"       # rich_text
P_REPO_ID     = "Repo ID"         # number  (업서트 키)
P_URL         = "URL"             # url
P_DESC        = "Description"     # rich_text
P_LANGUAGE    = "Language"        # select
P_VISIBILITY  = "Visibility"      # select
P_TOPICS      = "Topics"          # multi_select
P_STARS       = "Stars"           # number
P_OPEN_ISSUES = "Open Issues"     # number
P_BRANCH      = "Default Branch"  # rich_text
P_PUSHED_AT   = "Pushed At"       # date
P_CREATED_AT  = "Created At"      # date
P_COMMITTER   = "Last Committer"   # select   (마지막 커밋 작성자)
P_COMMIT_AT   = "Last Commit At"   # date     (마지막 커밋 시각)
P_SIGNATURE   = "Sync Signature"  # rich_text (변경 감지용 해시)

# Notion rich_text 1개 블록의 최대 길이
RICH_TEXT_LIMIT = 2000


# ── Notion API 헬퍼 ──────────────────────────────────────────
def notion_request(path: str, payload: dict = None, method: str = "POST") -> dict:
    if not NOTION_API_KEY:
        raise SystemExit(
            "[설정 필요] NOTION_API_KEY 가 비어 있습니다. "
            "notion.so/profile/integrations 에서 발급하세요 (ntn_ 로 시작)."
        )
    url = f"https://api.notion.com/v1/{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization":  f"Bearer {NOTION_API_KEY}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type":   "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as res:
            return json.loads(res.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise RuntimeError(f"Notion API {e.code} 오류: {body}") from e


# ── Slack 알림 헬퍼 ──────────────────────────────────────────
def slack_notify(text: str) -> None:
    """실패 알림 전송. 알림 실패가 본 작업을 가리지 않도록 예외를 삼킨다."""
    if not (SLACK_BOT_TOKEN and SLACK_CHANNEL):
        return
    try:
        data = json.dumps({"channel": SLACK_CHANNEL, "text": text}).encode()
        req = urllib.request.Request(
            "https://slack.com/api/chat.postMessage", data=data, method="POST",
            headers={
                "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
                "Content-Type":  "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as res:
            result = json.loads(res.read())
        if not result.get("ok"):
            print(f"Slack 알림 실패: {result.get('error')}")
    except Exception as e:
        print(f"Slack 알림 예외: {e}")


# ── GitHub API 헬퍼 ──────────────────────────────────────────
def github_request(path: str, params: dict = None) -> tuple:
    """(응답 JSON, 응답 헤더)를 반환한다."""
    url = f"https://api.github.com/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url, method="GET",
        headers={
            "Authorization":        f"Bearer {GITHUB_TOKEN}",
            "Accept":               "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "User-Agent":           "notion-repo-sync",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as res:
            return json.loads(res.read()), dict(res.headers)
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            message = json.loads(body).get("message", body)
        except json.JSONDecodeError:
            message = body

        remaining = e.headers.get("X-RateLimit-Remaining")

        # rate limit 은 remaining 이 0 일 때만이다. 403 은 조직 정책·권한 등
        # 원인이 다양하므로 GitHub 이 준 메시지를 그대로 보여준다.
        if e.code == 429 or (e.code == 403 and remaining == "0"):
            reset = e.headers.get("X-RateLimit-Reset", "")
            when = ""
            if reset.isdigit():
                when = datetime.fromtimestamp(int(reset), timezone.utc).isoformat()
            detail = f"해제 시각(UTC): {when}" if when else "잠시 후 다시 시도하세요."
            raise RuntimeError(f"GitHub API rate limit 초과. {detail}") from e

        if e.code == 403:
            raise RuntimeError(
                f"GitHub API 403 (권한/정책 거부, rate limit 아님 remaining={remaining})\n"
                f"  → {message}"
            ) from e

        if e.code == 404:
            raise RuntimeError(
                f"GitHub API 404: {message}\n"
                f"  → GITHUB_ORG='{GITHUB_ORG}' 가 조직이 맞는지 확인하세요. "
                f"개인 계정이면 이 엔드포인트로는 조회되지 않습니다."
            ) from e

        if e.code == 401:
            raise RuntimeError(
                f"GitHub API 401: {message}\n"
                f"  → GITHUB_TOKEN 이 잘못됐거나 만료됐습니다."
            ) from e

        raise RuntimeError(f"GitHub API {e.code} 오류: {message}") from e


# ── Data Source ID 해석 ──────────────────────────────────────
def resolve_data_source_id() -> str:
    if NOTION_REPO_DATA_SOURCE_ID:
        return NOTION_REPO_DATA_SOURCE_ID

    if not NOTION_REPO_DB_ID:
        raise RuntimeError(
            "NOTION_REPO_DB_ID가 비어 있습니다. "
            'DB를 아직 안 만들었다면 {"job": "init"} 으로 먼저 생성하세요.'
        )

    db = notion_request(f"databases/{NOTION_REPO_DB_ID}", method="GET")
    sources = db.get("data_sources", [])

    if not sources:
        raise RuntimeError("이 데이터베이스에 data_sources가 없습니다. DB ID를 확인하세요.")

    print("발견된 data sources:")
    for s in sources:
        print(f"  - name='{s.get('name')}'  id={s.get('id')}")

    if NOTION_REPO_DATA_SOURCE_NAME:
        for s in sources:
            if s.get("name") == NOTION_REPO_DATA_SOURCE_NAME:
                return s["id"]
        raise RuntimeError(
            f"'{NOTION_REPO_DATA_SOURCE_NAME}' 이름의 data source를 찾지 못했습니다."
        )

    return sources[0]["id"]


# ── 저장소 수집 ──────────────────────────────────────────────
def fetch_org_repos() -> list:
    """조직의 모든 저장소를 페이지네이션으로 수집한다."""
    repos, page = [], 1

    while True:
        batch, _ = github_request(
            f"orgs/{GITHUB_ORG}/repos",
            {"per_page": 100, "page": page, "type": "all", "sort": "full_name"},
        )
        if not batch:
            break

        repos += batch

        if len(batch) < 100:
            break
        page += 1

        # 방어적 상한. 조직 레포가 5000개를 넘으면 설계를 다시 봐야 한다.
        if page > 50:
            print("경고: 페이지 상한(50)에 도달해 수집을 중단합니다.")
            break

    return repos


def fetch_last_commit(full_name: str) -> dict:
    """
    최신 커밋 1건을 조회한다.

    committer 가 아니라 author 를 쓴다. GitHub UI 에서 만든 머지 커밋은
    committer 가 web-flow(GitHub 봇)로 기록되어 실제 사람을 알 수 없다.
    GitHub 계정이 연결돼 있으면 login, 아니면 커밋에 적힌 이름을 쓴다.
    """
    try:
        data, _ = github_request(f"repos/{full_name}/commits", {"per_page": 1})
    except RuntimeError as e:
        # 빈 저장소(409)나 접근 불가는 치명적이지 않으므로 건너뛴다.
        print(f"  경고: {full_name} 커밋 조회 실패 — {e}")
        return {}

    if not data:
        return {}

    c = data[0]
    commit = c.get("commit", {})
    author = c.get("author") or {}
    who = author.get("login") or commit.get("author", {}).get("name") or ""
    return {"committer": who, "date": commit.get("author", {}).get("date")}


def build_matcher():
    """이름 패턴 매칭 함수를 만든다. 규칙이 하나도 없으면 즉시 실패시킨다."""
    if REPO_NAME_REGEX:
        pattern = re.compile(REPO_NAME_REGEX)
        return lambda name: bool(pattern.search(name))

    if REPO_NAME_PREFIX:
        return lambda name: any(name.startswith(p) for p in REPO_NAME_PREFIX)

    raise RuntimeError(
        "매칭 규칙이 없습니다. REPO_NAME_REGEX 또는 REPO_NAME_PREFIX 중 하나를 설정하세요. "
        "규칙이 없으면 조직 전체가 등록되어 사고가 납니다."
    )


def match_repos(repos: list) -> list:
    matches = build_matcher()
    selected = []

    for r in repos:
        if not matches(r["name"]):
            continue
        if not INCLUDE_FORKS and r.get("fork"):
            continue
        if not INCLUDE_ARCHIVED and r.get("archived"):
            continue
        selected.append(r)

    return selected


# ── Notion 프로퍼티 변환 ─────────────────────────────────────
def _rich_text(value: str) -> list:
    if not value:
        return []
    return [{"type": "text", "text": {"content": value[:RICH_TEXT_LIMIT]}}]


def _select(value: str):
    if not value:
        return None
    return {"name": value[:100]}


def _date(value: str):
    if not value:
        return None
    return {"start": value}


def build_properties(repo: dict, last_commit: dict = None) -> dict:
    """GitHub 저장소 객체를 Notion 프로퍼티로 변환한다."""
    topics = repo.get("topics") or []
    last_commit = last_commit or {}

    return {
        P_COMMITTER:   {"select": _select(last_commit.get("committer"))},
        P_COMMIT_AT:   {"date": _date(last_commit.get("date"))},
        P_NAME:        {"title": [{"type": "text", "text": {"content": repo["name"][:RICH_TEXT_LIMIT]}}]},
        P_FULL_NAME:   {"rich_text": _rich_text(repo.get("full_name", ""))},
        P_REPO_ID:     {"number": repo["id"]},
        P_URL:         {"url": repo.get("html_url") or None},
        P_DESC:        {"rich_text": _rich_text(repo.get("description") or "")},
        P_LANGUAGE:    {"select": _select(repo.get("language"))},
        P_VISIBILITY:  {"select": _select(repo.get("visibility") or ("private" if repo.get("private") else "public"))},
        P_TOPICS:      {"multi_select": [{"name": t[:100]} for t in topics[:100]]},
        P_STARS:       {"number": repo.get("stargazers_count", 0)},
        P_OPEN_ISSUES: {"number": repo.get("open_issues_count", 0)},
        P_BRANCH:      {"rich_text": _rich_text(repo.get("default_branch") or "")},
        P_PUSHED_AT:   {"date": _date(repo.get("pushed_at"))},
        P_CREATED_AT:  {"date": _date(repo.get("created_at"))},
    }


def signature_of(props: dict) -> str:
    """Synced At을 제외한 값들의 해시. 변경 없는 페이지 쓰기를 건너뛰는 데 쓴다."""
    payload = json.dumps(props, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


# ── 기존 페이지 인덱싱 ───────────────────────────────────────
def fetch_existing_pages(data_source_id: str) -> dict:
    """Repo ID → {page_id, signature} 로 인덱싱한다."""
    index, cursor = {}, None

    while True:
        payload = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor

        result = notion_request(f"data_sources/{data_source_id}/query", payload)

        for page in result.get("results", []):
            props = page.get("properties", {})
            repo_id = (props.get(P_REPO_ID) or {}).get("number")
            if repo_id is None:
                # Repo ID가 없는 행은 사람이 직접 만든 것으로 보고 건드리지 않는다.
                continue

            sig_blocks = (props.get(P_SIGNATURE) or {}).get("rich_text") or []
            signature = sig_blocks[0]["plain_text"] if sig_blocks else ""

            index[int(repo_id)] = {"page_id": page["id"], "signature": signature}

        if not result.get("has_more"):
            break
        cursor = result.get("next_cursor")

    return index


# ── 업서트 ───────────────────────────────────────────────────
def sync_repos(data_source_id: str, repos: list) -> dict:
    existing = fetch_existing_pages(data_source_id)

    stats = {"created": 0, "updated": 0, "skipped": 0, "archived": 0}
    seen_ids = set()

    for repo in repos:
        repo_id = repo["id"]
        seen_ids.add(repo_id)

        props = build_properties(repo, fetch_last_commit(repo["full_name"]))
        signature = signature_of(props)

        current = existing.get(repo_id)

        if current and current["signature"] == signature:
            stats["skipped"] += 1
            continue

        props[P_SIGNATURE] = {"rich_text": _rich_text(signature)}

        if current:
            print(f"  갱신: {repo['full_name']}")
            if not DRY_RUN:
                notion_request(
                    f"pages/{current['page_id']}",
                    {"properties": props},
                    method="PATCH",
                )
                time.sleep(NOTION_WRITE_INTERVAL)
            stats["updated"] += 1
        else:
            print(f"  등록: {repo['full_name']}")
            if not DRY_RUN:
                notion_request(
                    "pages",
                    {
                        "parent": {"type": "data_source_id", "data_source_id": data_source_id},
                        "properties": props,
                    },
                )
                time.sleep(NOTION_WRITE_INTERVAL)
            stats["created"] += 1

    if ARCHIVE_UNMATCHED:
        for repo_id, info in existing.items():
            if repo_id in seen_ids:
                continue
            print(f"  보관: repo_id={repo_id}")
            if not DRY_RUN:
                notion_request(
                    f"pages/{info['page_id']}",
                    {"archived": True},
                    method="PATCH",
                )
                time.sleep(NOTION_WRITE_INTERVAL)
            stats["archived"] += 1

    return stats


# ── DB 생성 (init 잡) ────────────────────────────────────────
def init_database() -> dict:
    """스키마를 갖춘 Notion 데이터베이스를 새로 만든다."""
    if not NOTION_PARENT_PAGE_ID:
        raise RuntimeError(
            "NOTION_PARENT_PAGE_ID가 필요합니다. "
            "DB를 만들 노션 페이지를 integration에 공유한 뒤 그 페이지 ID를 넣으세요."
        )

    schema = {
        P_NAME:        {"type": "title",        "title": {}},
        P_FULL_NAME:   {"type": "rich_text",    "rich_text": {}},
        P_REPO_ID:     {"type": "number",       "number": {}},
        P_URL:         {"type": "url",          "url": {}},
        P_DESC:        {"type": "rich_text",    "rich_text": {}},
        P_LANGUAGE:    {"type": "select",       "select": {}},
        P_VISIBILITY:  {"type": "select",       "select": {}},
        P_TOPICS:      {"type": "multi_select", "multi_select": {}},
        P_STARS:       {"type": "number",       "number": {}},
        P_OPEN_ISSUES: {"type": "number",       "number": {}},
        P_BRANCH:      {"type": "rich_text",    "rich_text": {}},
        P_PUSHED_AT:   {"type": "date",         "date": {}},
        P_CREATED_AT:  {"type": "date",         "date": {}},
        P_COMMITTER:   {"type": "select",       "select": {}},
        P_COMMIT_AT:   {"type": "date",         "date": {}},
        P_SIGNATURE:   {"type": "rich_text",    "rich_text": {}},
    }

    title = NOTION_DB_TITLE or f"{GITHUB_ORG} Repositories"

    db = notion_request("databases", {
        "parent": {"type": "page_id", "page_id": NOTION_PARENT_PAGE_ID},
        "title": [{"type": "text", "text": {"content": title}}],
        "icon": {"type": "emoji", "emoji": "📦"},
        "is_inline": NOTION_DB_INLINE,
        "initial_data_source": {"properties": schema},
    })

    db_id = db["id"]
    ds = db.get("data_sources", [])
    ds_id = ds[0]["id"] if ds else ""

    kind = "인라인" if NOTION_DB_INLINE else "전체 페이지형"
    print(f"데이터베이스를 만들었습니다 ({kind}, 제목='{title}').")
    print("아래 값을 환경 변수에 넣으세요:")
    print(f"  NOTION_REPO_DB_ID={db_id}")
    if ds_id:
        print(f"  NOTION_REPO_DATA_SOURCE_ID={ds_id}")

    return {"database_id": db_id, "data_source_id": ds_id}


# ── 잡 실행 ──────────────────────────────────────────────────
def run_sync() -> dict:
    all_repos = fetch_org_repos()
    print(f"{GITHUB_ORG} 조직 저장소 {len(all_repos)}개 수집")

    selected = match_repos(all_repos)
    rule = REPO_NAME_REGEX or ",".join(REPO_NAME_PREFIX)
    print(f"규칙 '{rule}' 매칭 {len(selected)}개")

    if DRY_RUN:
        print("DRY_RUN=true — 실제 쓰기는 하지 않습니다.")

    ds_id = resolve_data_source_id()
    stats = sync_repos(ds_id, selected)

    print(
        f"완료: 등록 {stats['created']} / 갱신 {stats['updated']} / "
        f"변경없음 {stats['skipped']} / 보관 {stats['archived']}"
    )
    return stats


def run_list() -> dict:
    """저장소 이름을 훑어보며 매칭 규칙을 정하기 위한 잡. Notion 을 건드리지 않는다."""
    repos = fetch_org_repos()
    print(f"{GITHUB_ORG} 조직 저장소 {len(repos)}개\n")

    has_rule = bool(REPO_NAME_REGEX or REPO_NAME_PREFIX)
    matches = build_matcher() if has_rule else (lambda name: False)

    matched = 0
    for r in sorted(repos, key=lambda x: x["name"]):
        hit = matches(r["name"])
        matched += hit
        mark = "✓" if hit else " "
        flags = []
        if r.get("private"):  flags.append("private")
        if r.get("fork"):     flags.append("fork")
        if r.get("archived"): flags.append("archived")
        suffix = f"  [{', '.join(flags)}]" if flags else ""
        print(f"  {mark} {r['name']}{suffix}")

    if has_rule:
        rule = REPO_NAME_REGEX or ",".join(REPO_NAME_PREFIX)
        print(f"\n규칙 '{rule}' → {matched}/{len(repos)}개 매칭 (✓ 표시)")
    else:
        print("\n규칙이 설정되지 않아 매칭 표시를 생략했습니다.")
        print("REPO_NAME_PREFIX 또는 REPO_NAME_REGEX 를 넣고 다시 실행하면 ✓ 로 확인됩니다.")

    return {"total": len(repos), "matched": matched}


def lambda_handler(event, context):
    job = (event or {}).get("job", "sync")
    try:
        if job == "init":
            return init_database()
        if job == "list":
            return run_list()
        return run_sync()
    except Exception as e:
        slack_notify(f"❌ GitHub→Notion 동기화 오류 [{GITHUB_ORG}/{job}]: {e}")
        raise


if __name__ == "__main__":
    import sys
    print(json.dumps(
        lambda_handler({"job": sys.argv[1] if len(sys.argv) > 1 else "sync"}, None),
        ensure_ascii=False,
        indent=2,
    ))
