#!/usr/bin/env bash
set -euo pipefail

: "${EXPECTED_COMMIT:?EXPECTED_COMMIT must be the full pushed commit hash}"

if [[ ! "$EXPECTED_COMMIT" =~ ^[0-9a-f]{40}$ ]]; then
  echo "EXPECTED_COMMIT must be a full 40-character lowercase commit hash" >&2
  exit 2
fi

repo_dir="$(git rev-parse --show-toplevel)"
cd "$repo_dir"

if [[ -n "$(git status --porcelain --untracked-files=normal)" ]]; then
  echo "refusing deployment from a dirty worktree" >&2
  exit 3
fi

git fetch --prune origin main
remote_commit="$(git rev-parse origin/main)"
if [[ "$remote_commit" != "$EXPECTED_COMMIT" ]]; then
  echo "origin/main does not match EXPECTED_COMMIT" >&2
  exit 4
fi

current_commit="$(git rev-parse HEAD)"
if ! git merge-base --is-ancestor "$current_commit" "$EXPECTED_COMMIT"; then
  echo "deployment is not a fast-forward from the current commit" >&2
  exit 5
fi

git merge --ff-only "$EXPECTED_COMMIT"
if [[ "$(git rev-parse HEAD)" != "$EXPECTED_COMMIT" ]]; then
  echo "checked-out commit does not match EXPECTED_COMMIT" >&2
  exit 6
fi

venv_dir="${FOLIANT_VENV:-$repo_dir/venv2}"
python_bin="$venv_dir/bin/python"
if [[ ! -x "$python_bin" ]]; then
  echo "configured Foliant Python is not executable" >&2
  exit 7
fi

"$python_bin" -m pip install -r requirements.txt
"$python_bin" -m compileall -q \
  -x '(^|[\\/])(venv2?|\.git|node_modules)([\\/]|$)' .
find scripts -type f -name '*.sh' -exec bash -n {} +

if [[ "${DEPLOY_RESTART:-false}" != "true" ]]; then
  echo "validated commit $EXPECTED_COMMIT; restart skipped"
  exit 0
fi

supervisorctl restart stock-webui
supervisorctl restart stock-jobs-hub
supervisorctl status stock-webui stock-jobs-hub
echo "deployed commit $EXPECTED_COMMIT"
