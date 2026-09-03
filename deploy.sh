#!/usr/bin/env bash
# GitHub→Notion 동기화 람다 배포 스크립트
#
#   ./deploy.sh            전체 배포 (패키징 → 함수 생성/갱신 → 환경변수 → 스케줄)
#   ./deploy.sh package    zip 만들기만
#   ./deploy.sh invoke     지금 즉시 한 번 실행해보기
#   ./deploy.sh logs       최근 로그 보기
#
# 자격 증명은 .env 에서 읽어 파일로 전달한다 (명령행 인자로 넘기면 ps 에 노출됨).

set -euo pipefail
cd "$(dirname "$0")"

# 자격 증명이 담기는 임시 파일은 실패·중단 포함 어떤 경로로 끝나든 반드시 지운다.
trap 'rm -f build/env.json' EXIT INT TERM

FUNCTION_NAME="${FUNCTION_NAME:-github-to-notion}"
REGION="${AWS_REGION:-ap-northeast-1}"
RUNTIME="python3.12"
HANDLER="github_to_notion.lambda_handler"
TIMEOUT=300
MEMORY=256
ROLE_NAME="${FUNCTION_NAME}-role"
RULE_NAME="${FUNCTION_NAME}-daily"
# 00:00 UTC = 09:00 KST
SCHEDULE="cron(0 0 * * ? *)"

ZIP_FILE="build/${FUNCTION_NAME}.zip"

log() { printf '\033[1;34m▶\033[0m %s\n' "$*"; }

require_env_file() {
  [ -f .env ] || { echo "오류: .env 가 없습니다. .env.example 을 복사해 채우세요."; exit 1; }
}

# ── 패키징 ───────────────────────────────────────────────────
package() {
  log "패키징"
  rm -rf build && mkdir -p build
  # 외부 의존성이 없으므로 스크립트 하나만 넣는다.
  zip -j -q "$ZIP_FILE" github_to_notion.py
  log "생성: $ZIP_FILE ($(du -h "$ZIP_FILE" | cut -f1))"
}

# ── IAM 역할 ─────────────────────────────────────────────────
ensure_role() {
  if aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
    log "IAM 역할 이미 존재: $ROLE_NAME"
  else
    log "IAM 역할 생성: $ROLE_NAME"
    aws iam create-role --role-name "$ROLE_NAME" \
      --assume-role-policy-document '{
        "Version": "2012-10-17",
        "Statement": [{
          "Effect": "Allow",
          "Principal": {"Service": "lambda.amazonaws.com"},
          "Action": "sts:AssumeRole"
        }]
      }' >/dev/null
    aws iam attach-role-policy --role-name "$ROLE_NAME" \
      --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
    log "역할 전파 대기 (10초)"
    sleep 10
  fi
  ROLE_ARN=$(aws iam get-role --role-name "$ROLE_NAME" --query 'Role.Arn' --output text)
}

# ── .env → 람다 환경변수 JSON ────────────────────────────────
# 람다에 실제로 필요한 키만 화이트리스트로 추린다.
# NOTION_PARENT_PAGE_ID 는 init 잡 전용이라 제외, DRY_RUN 도 제외.
build_env_json() {
  # umask 077: 리다이렉트로 파일이 만들어지는 순간부터 600 이 되도록 한다.
  # (chmod 를 나중에 걸면 그 사이 짧게 644 로 존재한다)
  umask 077
  python3 - << 'PYEOF' > build/env.json
import json, os
KEYS = [
    "GITHUB_TOKEN", "GITHUB_ORG",
    "REPO_NAME_PREFIX", "REPO_NAME_REGEX",
    "NOTION_API_KEY", "NOTION_REPO_DB_ID", "NOTION_REPO_DATA_SOURCE_ID",
    "INCLUDE_FORKS", "INCLUDE_ARCHIVED", "ARCHIVE_UNMATCHED",
    "SLACK_BOT_TOKEN", "SLACK_CHANNEL",
]
env = {}
with open(".env", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip("'\"")
        if k in KEYS and v:
            env[k] = v

missing = [k for k in ("GITHUB_TOKEN", "GITHUB_ORG", "NOTION_API_KEY") if k not in env]
if missing:
    raise SystemExit(f"오류: .env 에 다음 값이 비어 있습니다: {', '.join(missing)}")
if not (env.get("REPO_NAME_PREFIX") or env.get("REPO_NAME_REGEX")):
    raise SystemExit("오류: REPO_NAME_PREFIX 또는 REPO_NAME_REGEX 중 하나는 있어야 합니다.")

print(json.dumps({"Variables": env}))
PYEOF
}

# ── 함수 생성/갱신 ───────────────────────────────────────────
deploy_function() {
  if aws lambda get-function --function-name "$FUNCTION_NAME" --region "$REGION" >/dev/null 2>&1; then
    log "코드 갱신"
    aws lambda update-function-code --function-name "$FUNCTION_NAME" \
      --zip-file "fileb://$ZIP_FILE" --region "$REGION" >/dev/null
    aws lambda wait function-updated --function-name "$FUNCTION_NAME" --region "$REGION"

    log "설정 갱신 (환경변수 포함)"
    aws lambda update-function-configuration --function-name "$FUNCTION_NAME" \
      --timeout "$TIMEOUT" --memory-size "$MEMORY" --runtime "$RUNTIME" --handler "$HANDLER" \
      --environment "file://build/env.json" --region "$REGION" >/dev/null
    aws lambda wait function-updated --function-name "$FUNCTION_NAME" --region "$REGION"
  else
    log "함수 신규 생성: $FUNCTION_NAME"
    aws lambda create-function --function-name "$FUNCTION_NAME" \
      --runtime "$RUNTIME" --handler "$HANDLER" --role "$ROLE_ARN" \
      --timeout "$TIMEOUT" --memory-size "$MEMORY" \
      --zip-file "fileb://$ZIP_FILE" \
      --environment "file://build/env.json" --region "$REGION" >/dev/null
    aws lambda wait function-active --function-name "$FUNCTION_NAME" --region "$REGION"
  fi
}

# ── EventBridge 스케줄 ───────────────────────────────────────
setup_schedule() {
  log "스케줄 설정: $SCHEDULE (09:00 KST)"
  aws events put-rule --name "$RULE_NAME" \
    --schedule-expression "$SCHEDULE" --state ENABLED --region "$REGION" >/dev/null

  local fn_arn rule_arn
  fn_arn=$(aws lambda get-function --function-name "$FUNCTION_NAME" \
    --query 'Configuration.FunctionArn' --output text --region "$REGION")
  rule_arn=$(aws events describe-rule --name "$RULE_NAME" \
    --query 'Arn' --output text --region "$REGION")

  # 중복 호출 시 이미 존재 오류가 나므로 무시한다.
  aws lambda add-permission --function-name "$FUNCTION_NAME" \
    --statement-id "${RULE_NAME}-invoke" \
    --action lambda:InvokeFunction --principal events.amazonaws.com \
    --source-arn "$rule_arn" --region "$REGION" >/dev/null 2>&1 || true

  # 축약 문법(Id=..,Arn=..)은 값 안의 JSON 을 파싱하지 못한다.
  # Input 은 "JSON 을 담은 문자열" 이라 이스케이프가 까다로우므로 python 에 맡기고
  # 파일로 넘긴다.
  python3 -c "import json,sys; print(json.dumps([{'Id':'1','Arn':sys.argv[1],'Input':json.dumps({'job':'sync'})}]))" \
    "$fn_arn" > build/targets.json

  aws events put-targets --rule "$RULE_NAME" --region "$REGION" \
    --targets "file://build/targets.json" >/dev/null
}

# ── 서브커맨드 ───────────────────────────────────────────────
case "${1:-deploy}" in
  package)
    package
    ;;
  invoke)
    log "즉시 실행"
    aws lambda invoke --function-name "$FUNCTION_NAME" \
      --payload '{"job":"sync"}' --cli-binary-format raw-in-base64-out \
      --region "$REGION" build/response.json >/dev/null
    echo "--- 응답 ---"; cat build/response.json; echo
    ;;
  logs)
    aws logs tail "/aws/lambda/$FUNCTION_NAME" --since 1h --region "$REGION"
    ;;
  deploy)
    require_env_file
    package
    mkdir -p build
    build_env_json
    ensure_role
    deploy_function
    setup_schedule
    log "완료. 즉시 테스트: ./deploy.sh invoke"
    ;;
  *)
    echo "사용법: $0 [deploy|package|invoke|logs]"; exit 1
    ;;
esac
