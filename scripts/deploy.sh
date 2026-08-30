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

previous_commit="$(git rev-parse HEAD)"
if ! git merge-base --is-ancestor "$previous_commit" "$EXPECTED_COMMIT"; then
  echo "deployment is not a fast-forward from the current commit" >&2
  exit 5
fi

git merge --ff-only "$EXPECTED_COMMIT"
if [[ "$(git rev-parse HEAD)" != "$EXPECTED_COMMIT" ]]; then
  echo "checked-out commit does not match EXPECTED_COMMIT" >&2
  exit 6
fi

release_root="${FOLIANT_RELEASE_ROOT:-}"
if [[ -n "$release_root" ]]; then
  release_dir="$release_root/$EXPECTED_COMMIT"
  venv_dir="$release_dir/venv"
  mkdir -p "$release_dir"
  if [[ ! -f "$release_dir/.source-ready" ]]; then
    git archive "$EXPECTED_COMMIT" | tar -x -C "$release_dir"
    printf '%s\n' "$EXPECTED_COMMIT" > "$release_dir/.release-revision"
    touch "$release_dir/.source-ready"
  fi
  if [[ ! -x "$venv_dir/bin/python" ]]; then
    python3 -m venv "$venv_dir"
  fi
  # Supervisor and operational scripts consistently address ``venv2``. Keep
  # that stable release-local entry point while the controlled builder owns
  # the actual ``venv`` directory.
  ln -sfn venv "$release_dir/venv2"
  validation_dir="$release_dir"
else
  venv_dir="${FOLIANT_VENV:-$repo_dir/venv2}"
  validation_dir="$repo_dir"
fi
python_bin="$venv_dir/bin/python"
if [[ ! -x "$python_bin" ]]; then
  echo "configured Foliant Python is not executable" >&2
  exit 7
fi

platform_wheel="${SHADOW_PLATFORM_WHEEL:?SHADOW_PLATFORM_WHEEL is required}"
platform_wheel_sha256="${SHADOW_PLATFORM_WHEEL_SHA256:?SHADOW_PLATFORM_WHEEL_SHA256 is required}"
if [[ ! -r "$platform_wheel" ]]; then
  echo "configured Shadow Platform wheel is not readable" >&2
  exit 7
fi
actual_platform_wheel_sha256="$(sha256sum "$platform_wheel" | awk '{print $1}')"
if [[ "$actual_platform_wheel_sha256" != "$platform_wheel_sha256" ]]; then
  echo "Shadow Platform wheel SHA-256 mismatch" >&2
  exit 7
fi
"$python_bin" -m pip install "$platform_wheel"
"$python_bin" -m pip install -r "$validation_dir/requirements.txt"
# Validation runs inside the freshly created release, so its test runner cannot
# depend on whatever happened to be installed in a previous production venv.
"$python_bin" -m pip install 'pytest>=8,<10'
(
  cd "$validation_dir"
  "$python_bin" -m compileall -q \
    -x '(^|[\\/])(venv2?|\.venv|\.git|node_modules)([\\/]|$)' .
  find scripts -type f -name '*.sh' -exec bash -n {} +
  if [[ -n "${FOLIANT_TEST_TARGETS:-}" ]]; then
    read -r -a test_targets <<< "$FOLIANT_TEST_TARGETS"
    "$python_bin" -m pytest -q "${test_targets[@]}"
  elif [[ "${FOLIANT_SKIP_TESTS:-false}" == "true" ]]; then
    echo "pytest skipped by explicit deployment policy"
  else
    "$python_bin" -m pytest -q
  fi
)

(cd "$validation_dir" && bash scripts/migrate.sh)

if [[ "${DEPLOY_RESTART:-false}" != "true" ]]; then
  echo "validated commit $EXPECTED_COMMIT; restart skipped"
  exit 0
fi

health_base="${FOLIANT_HEALTH_BASE_URL:?FOLIANT_HEALTH_BASE_URL is required when restarting}"
if [[ -z "$release_root" ]]; then
  echo "safe restart requires FOLIANT_RELEASE_ROOT and a commit-specific venv" >&2
  exit 9
fi
app_link="${FOLIANT_CURRENT_LINK:?FOLIANT_CURRENT_LINK is required for release activation}"
if [[ -e "$app_link" && ! -L "$app_link" ]]; then
  echo "FOLIANT_CURRENT_LINK must be a symlink managed by this deploy script" >&2
  exit 10
fi
shared_env="${FOLIANT_SHARED_ENV:?FOLIANT_SHARED_ENV is required for release activation}"
if [[ ! -r "$shared_env" ]]; then
  echo "configured shared runtime environment is not readable" >&2
  exit 10
fi
ln -sfn "$shared_env" "$release_dir/.env"
previous_link="$(readlink "$app_link" 2>/dev/null || true)"
ln -sfn "$release_dir" "$app_link"

rolled_back=false
rollback() {
  if [[ "$rolled_back" == "true" ]]; then
    return
  fi
  rolled_back=true
  echo "deployment verification failed; restoring previous release" >&2
  if [[ -n "${previous_link:-}" ]]; then
    ln -sfn "$previous_link" "$app_link"
  fi
  supervisorctl restart stock-webui stock-jobs-hub >/dev/null 2>&1 || true
}
trap rollback ERR

supervisorctl restart stock-webui stock-jobs-hub
supervisorctl status stock-webui stock-jobs-hub
curl --fail --silent --show-error "$health_base/healthz" >/dev/null
ready_headers=()
ready_bearer_file="${FOLIANT_READY_BEARER_FILE:?FOLIANT_READY_BEARER_FILE is required}"
if [[ ! -r "$ready_bearer_file" ]]; then
  echo "configured readiness Bearer file is not readable" >&2
  false
fi
ready_headers=(-H "Authorization: Bearer $(<"$ready_bearer_file")")
ready_payload="$(curl --fail --silent --show-error "${ready_headers[@]}" "$health_base/readyz")"
if [[ "$ready_payload" != *"$EXPECTED_COMMIT"* ]]; then
  echo "runtime revision does not match EXPECTED_COMMIT" >&2
  false
fi
# Research readiness may legitimately be degraded while a fresh bootstrap is in
# progress, but both protected contracts must respond with valid JSON semantics.
for path in data-readyz selection-readyz; do
  payload="$(curl --silent --show-error "${ready_headers[@]}" "$health_base/$path")"
  if [[ "$payload" != *'"ready"'* || "$payload" != *'"checks"'* ]]; then
    echo "$path did not return a readiness contract" >&2
    false
  fi
done
trap - ERR
echo "deployed commit $EXPECTED_COMMIT"
