#!/usr/bin/env bash
# 为 Web 三池双探针并行开发创建 git worktree（在 grokImage 主仓根目录执行）。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE="${ROOT}/.."

declare -A WORKTREES=(
  ["grokImage-wt-web-core"]="feat/web-pool-core"
  ["grokImage-wt-web-probe"]="feat/web-probe-engine"
  ["grokImage-wt-web-selector"]="feat/web-selector-index"
  ["grokImage-wt-web-fe"]="feat/web-probe-ui"
  ["grokImage-wt-docs"]="docs/web-pool-plan"
)

INTEGRATION_BRANCH="${INTEGRATION_BRANCH:-codex/panda-safe-completion}"

cd "$ROOT"
git fetch --all --prune 2>/dev/null || true

for dir in "${!WORKTREES[@]}"; do
  branch="${WORKTREES[$dir]}"
  path="${BASE}/${dir}"
  if [[ -d "$path" ]]; then
    echo "skip: $path (exists)"
    continue
  fi
  if git show-ref --verify --quiet "refs/heads/$branch"; then
    git worktree add "$path" "$branch"
  else
    git worktree add -b "$branch" "$path" "$INTEGRATION_BRANCH"
  fi
  echo "created: $path -> $branch"
done

echo "主集成轨: $ROOT ($INTEGRATION_BRANCH)"
echo "合并顺序: web-pool-core -> web-probe-engine -> web-selector-index -> web-probe-ui"
