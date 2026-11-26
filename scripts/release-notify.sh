#!/usr/bin/env bash
# Sends one release notification (a JSON POST) for a deploy of .github/workflows/deploy.yml
# (docs/architecture.md, Section 19.3). The last step of each deploy job runs it, also when
# the deploy fails or is cancelled.
#
# The script never fails the deploy: it does not use `set -e`, every request has a time
# limit, and it always exits 0. It never prints the webhook URL or the response body.
#
# Inputs (environment variables):
#   RELEASE_WEBHOOK_URL   The webhook URL (CI secret VORFLUX_RELEASE_WEBHOOK_URL). If it is
#                         empty, the script does nothing.
#   DEPLOY_STATUS         The job status: success, failure, or cancelled.
#   DEPLOY_ENVIRONMENT    The GitHub environment, for example staging or production.
#   DEPLOY_SERVICE        The service name (default: sama).
#   DEPLOY_SHA            The full commit SHA that the job deploys.
#   DEPLOY_REF            The git ref of the commit, for example refs/heads/main.
#   DEPLOY_IMAGE_LABEL    The image label, for example main-<sha>. A tag on the commit wins.
#   GH_TOKEN              A token that can read deployments and contents (GITHUB_TOKEN).
#   RELEASE_NOTIFY_GIT_DIR  Optional. A clone with the history and the tags. The script uses
#                         it for the previous tag and the rollback check.
#   GITHUB_REPOSITORY, GITHUB_SERVER_URL, GITHUB_API_URL, GITHUB_RUN_ID, GITHUB_RUN_ATTEMPT,
#   GITHUB_ACTOR, GITHUB_TRIGGERING_ACTOR: set by GitHub Actions.
#
# previous_commit_sha is the commit of the last successful deployment to the same GitHub
# environment (Deployments API). If there is none, it is the commit of the tag before the
# deployed commit. The event is "rollback" when the deployed commit is an ancestor of it.

CURL_LIMITS=(--connect-timeout 5 --max-time 10)
MAX_DEPLOYMENTS=20

log() {
  echo "release-notify: $*"
}

is_sha() {
  [[ "${1:-}" =~ ^[0-9a-f]{40}$ ]]
}

is_number() {
  [[ "${1:-}" =~ ^[0-9]+$ ]]
}

# GET a path of the GitHub REST API. Prints the body, and fails on an HTTP error.
api_get() {
  local auth=()
  if [ -n "${GH_TOKEN:-}" ]; then
    auth=(-H "Authorization: Bearer ${GH_TOKEN}")
  fi
  curl -s --fail "${CURL_LIMITS[@]}" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "${auth[@]}" \
    "${API_URL}$1"
}

# The commit of the last successful deployment to the environment. The deployment of this
# job is still in progress, so it does not count. GitHub records the tip of main as the SHA
# of a deployment that a workflow_run or workflow_dispatch job makes, not the deployed
# commit. So the commit comes from the environment URL of the successful status, which ends
# with "#main-<sha>". If the last successful deployment has no such URL (a deploy from before
# the release notification), the lookup fails and the caller uses the previous tag.
previous_from_deployments() {
  local env_q deployments id statuses state sha
  env_q="$(jq -rn --arg v "$ENVIRONMENT" '$v|@uri')" || return 1
  deployments="$(api_get "/repos/${REPOSITORY}/deployments?environment=${env_q}&per_page=${MAX_DEPLOYMENTS}")" \
    || return 1
  while read -r id; do
    if ! is_number "$id"; then
      continue
    fi
    statuses="$(api_get "/repos/${REPOSITORY}/deployments/${id}/statuses?per_page=100")" \
      || return 1
    # A later successful deployment makes this one "inactive", so look at all statuses.
    state="$(jq -r 'if type == "array" and any(.[]; .state == "success")
      then "success" else "other" end' <<<"$statuses" 2>/dev/null)"
    if [ "$state" = "success" ]; then
      sha="$(jq -r '[.[] | select(.state == "success") | (.environment_url // "")
        | capture("#main-(?<sha>[0-9a-f]{40})$")? | .sha] | first // empty' \
        <<<"$statuses" 2>/dev/null)"
      if is_sha "$sha"; then
        echo "$sha"
        return 0
      fi
      return 1
    fi
  done < <(jq -r 'if type == "array" then .[].id else empty end' <<<"$deployments" 2>/dev/null)
  return 1
}

has_history() {
  [ -n "${GIT_DIR_OPT:-}" ] && git -C "$GIT_DIR_OPT" rev-parse --git-dir >/dev/null 2>&1
}

# The commit of the last tag before the deployed commit.
previous_from_tag() {
  local tag
  has_history || return 1
  tag="$(git -C "$GIT_DIR_OPT" describe --tags --abbrev=0 "${COMMIT_SHA}^" 2>/dev/null)" \
    || return 1
  git -C "$GIT_DIR_OPT" rev-list -n 1 "refs/tags/${tag}" 2>/dev/null
}

# A tag on the deployed commit, if there is one.
tag_of_commit() {
  has_history || return 1
  git -C "$GIT_DIR_OPT" tag --points-at "$COMMIT_SHA" --sort=-v:refname 2>/dev/null | head -n 1
}

# Succeeds when the deployed commit is an ancestor of the previous one (a rollback).
is_rollback() {
  local rc status
  [ "$COMMIT_SHA" != "$PREVIOUS_SHA" ] || return 1
  if has_history; then
    git -C "$GIT_DIR_OPT" merge-base --is-ancestor "$COMMIT_SHA" "$PREVIOUS_SHA" 2>/dev/null
    rc=$?
    # 0: ancestor, 1: not an ancestor. Other codes: a commit is not in the clone.
    if [ "$rc" -le 1 ]; then
      return "$rc"
    fi
  fi
  status="$(api_get "/repos/${REPOSITORY}/compare/${PREVIOUS_SHA}...${COMMIT_SHA}?per_page=1" \
    | jq -r '.status // empty' 2>/dev/null)"
  [ "$status" = "behind" ]
}

build_payload() {
  jq -cn \
    --arg event "$EVENT" \
    --arg status "$STATUS" \
    --arg environment "$ENVIRONMENT" \
    --arg service "$SERVICE" \
    --arg repository "$REPOSITORY" \
    --arg version "$VERSION" \
    --arg ref "$REF" \
    --arg commit_sha "$COMMIT_SHA" \
    --arg previous_commit_sha "$PREVIOUS_SHA" \
    --arg compare_url "$COMPARE_URL" \
    --arg run_url "$RUN_URL" \
    --arg run_id "$RUN_ID" \
    --arg run_attempt "$RUN_ATTEMPT" \
    --arg actor "$ACTOR" \
    --arg deployed_at "$DEPLOYED_AT" \
    'def opt: if . == "" then null else . end;
     def num: if test("^[0-9]+$") then tonumber else null end;
     {
       event: $event,
       status: $status,
       environment: $environment,
       service: $service,
       repository: $repository,
       version: ($version | opt),
       ref: ($ref | opt),
       commit_sha: ($commit_sha | opt),
       previous_commit_sha: ($previous_commit_sha | opt),
       compare_url: ($compare_url | opt),
       run_url: $run_url,
       run_id: ($run_id | num),
       run_attempt: ($run_attempt | num),
       actor: ($actor | opt),
       deployed_at: $deployed_at
     }'
}

main() {
  if [ -z "${RELEASE_WEBHOOK_URL:-}" ]; then
    log "VORFLUX_RELEASE_WEBHOOK_URL is not set. No notification."
    return 0
  fi
  if ! command -v jq >/dev/null 2>&1 || ! command -v curl >/dev/null 2>&1; then
    log "jq or curl is missing. No notification."
    return 0
  fi

  SERVER_URL="${GITHUB_SERVER_URL:-https://github.com}"
  API_URL="${GITHUB_API_URL:-https://api.github.com}"
  REPOSITORY="${GITHUB_REPOSITORY:-}"
  ENVIRONMENT="${DEPLOY_ENVIRONMENT:-}"
  SERVICE="${DEPLOY_SERVICE:-sama}"
  REF="${DEPLOY_REF:-}"
  RUN_ID="${GITHUB_RUN_ID:-}"
  RUN_ATTEMPT="${GITHUB_RUN_ATTEMPT:-}"
  ACTOR="${GITHUB_TRIGGERING_ACTOR:-${GITHUB_ACTOR:-}}"
  GIT_DIR_OPT="${RELEASE_NOTIFY_GIT_DIR:-}"
  RUN_URL="${SERVER_URL}/${REPOSITORY}/actions/runs/${RUN_ID}"
  DEPLOYED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

  case "${DEPLOY_STATUS:-}" in
    success) STATUS="succeeded" ;;
    cancelled) STATUS="cancelled" ;;
    *) STATUS="failed" ;;
  esac

  # A bad SHA (for example a wrong workflow input) is not sent and not looked up.
  COMMIT_SHA=""
  if is_sha "${DEPLOY_SHA:-}"; then
    COMMIT_SHA="$DEPLOY_SHA"
  else
    log "The deployed commit is not a full SHA. commit_sha is null."
  fi

  VERSION="${DEPLOY_IMAGE_LABEL:-}"
  PREVIOUS_SHA=""
  local source="none" tag
  if [ -n "$COMMIT_SHA" ]; then
    tag="$(tag_of_commit)" && [ -n "$tag" ] && VERSION="$tag"
    if PREVIOUS_SHA="$(previous_from_deployments)" && is_sha "$PREVIOUS_SHA"; then
      source="deployments"
    elif PREVIOUS_SHA="$(previous_from_tag)" && is_sha "$PREVIOUS_SHA"; then
      source="tag"
    else
      PREVIOUS_SHA=""
    fi
  fi
  log "previous_commit_sha source: ${source}."

  EVENT="deploy"
  COMPARE_URL=""
  if [ -n "$PREVIOUS_SHA" ]; then
    if is_rollback; then
      EVENT="rollback"
      # For a rollback, the link shows the commits that are no longer live.
      COMPARE_URL="${SERVER_URL}/${REPOSITORY}/compare/${COMMIT_SHA}...${PREVIOUS_SHA}"
    else
      COMPARE_URL="${SERVER_URL}/${REPOSITORY}/compare/${PREVIOUS_SHA}...${COMMIT_SHA}"
    fi
  fi

  local payload code
  if ! payload="$(build_payload)"; then
    log "Could not build the payload. No notification."
    return 0
  fi

  # One POST, no retries. Only the HTTP code goes to the log.
  code="$(curl -s -o /dev/null -w '%{http_code}' "${CURL_LIMITS[@]}" \
    -X POST -H "Content-Type: application/json" \
    --data-binary "$payload" \
    "$RELEASE_WEBHOOK_URL" 2>/dev/null)"
  case "$code" in
    2??) log "Sent ${EVENT} ${STATUS} for ${ENVIRONMENT} (HTTP ${code})." ;;
    *) log "The webhook did not accept the notification (HTTP ${code:-none}). The deploy is not affected." ;;
  esac
  return 0
}

main "$@" || true
exit 0
