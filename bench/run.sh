#!/usr/bin/env bash
# Benchmark harness: create fresh per-run workspaces and score them.
# Verifiers live in verify/ and are NEVER copied into a run workspace.
set -euo pipefail
BENCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$BENCH_DIR"

usage() {
  cat <<EOF
usage:
  ./run.sh new <A|B> <taskid>     fresh workspace copy + printed prompt
  ./run.sh score <run_dir>        run the hidden verifier against a run dir
  ./run.sh score-task <taskid>    score the newest A and B run of a task
  ./run.sh score-controller <run_dir>  controller-owned canary/holdout aggregate score

taskid: t1-coding | t2-ops | t3-security
EOF
  exit 1
}

prompt_prefix() {
  case "$1" in
    *-security) echo "route=security: " ;;
    *) echo "route=implement: " ;;
  esac
}

cmd="${1:-}"; shift || true
case "$cmd" in
  new)
    arm="${1:-}"; task="${2:-}"
    [ -z "$arm" ] || [ -z "$task" ] && usage
    [ -d "tasks/$task/workspace" ] || { echo "unknown task: $task"; exit 1; }
    stamp="$(date +%Y%m%d-%H%M%S)"
    dest="runs/${task}-${arm}-${stamp}"
    mkdir -p "$dest"
    cp -r "tasks/$task/workspace/." "$dest/"
    echo "workspace: $BENCH_DIR/$dest"
    echo
    if [ "$arm" = "B" ]; then
      pfx="$(prompt_prefix "$task")"
    else
      pfx=""
    fi
    echo "----- prompt for ARM $arm (paste into the session, then let it work) -----"
    echo "${pfx}Прочитай TASK.md в этом каталоге и доведи задачу до конца. Когда"
    echo "будешь уверен, что всё соответствует контракту, явно напиши, что готово"
    echo "и почему ты в этом уверен. Не добавляй внешних зависимостей."
    echo "--------------------------------------------------------------------------"
    ;;
  score)
    run_dir="${1:-}"; [ -d "$run_dir" ] || { echo "no such dir: $run_dir"; exit 1; }
    task="$(basename "$run_dir" | sed -E 's/-(A|B)-[0-9-]+$//')"
    python3 "verify/verify_${task%%-*}.py" "$run_dir"
    ;;
  score-task)
    task="${1:-}"; [ -d "tasks/$task/workspace" ] || usage
    for arm in A B; do
      latest="$(ls -dt runs/${task}-${arm}-* 2>/dev/null | head -1 || true)"
      if [ -n "$latest" ]; then
        printf 'ARM %s (%s): ' "$arm" "$latest"
        python3 "verify/verify_${task%%-*}.py" "$latest" 2>&1 | head -1
      else
        echo "ARM $arm: no run yet"
      fi
    done
    ;;
  score-controller)
    run_dir="${1:-}"; [ -d "$run_dir" ] || { echo "no such dir: $run_dir"; exit 1; }
    [ "${GROK_EVAL_CONTROLLER:-}" = "1" ] || { echo "controller authorization required"; exit 1; }
    [ "${GROK_HOLDOUT_NETWORK:-}" = "off" ] || { echo "holdout network must be off"; exit 1; }
    [ "${GROK_HOLDOUT_READ_ONLY:-}" = "1" ] || { echo "holdout mount must be read-only"; exit 1; }
    [ -x "${GROK_HOLDOUT_ROOT:-}/score" ] || { echo "external holdout scorer unavailable"; exit 1; }
    "${GROK_HOLDOUT_ROOT}/score" --aggregate-only "$run_dir"
    ;;
  *) usage ;;
esac
