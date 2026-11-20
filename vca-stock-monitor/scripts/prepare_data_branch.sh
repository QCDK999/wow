#!/usr/bin/env bash
# Check out the "monitor-data" branch (state.json + status.json) into ./data as a
# git worktree. Creates the branch (orphan, no code) on the very first run and
# verifies that this job can actually push to it – a monitor that cannot save
# its state would re-send notifications, so we fail loudly and early instead.
set -euo pipefail
BRANCH="${DATA_BRANCH:-monitor-data}"
DIR="${DATA_DIR:-data}"

git config --global user.name  "vca-monitor-bot"
git config --global user.email "41898282+github-actions[bot]@users.noreply.github.com"

remote_has_branch() { git ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1; }

fail_perm() {
  echo "::error::Cannot push to branch '$BRANCH'. Give the workflow write access: repo Settings → Actions → General →" \
       "Workflow permissions → 'Read and write permissions', and make sure '$BRANCH' is not a protected branch." >&2
  echo "$1" >&2
  exit 1
}

if remote_has_branch; then
  git fetch --quiet --depth=20 origin "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH"
  git worktree add --quiet -B "$BRANCH" "$DIR" "origin/$BRANCH"
  # Write-access check without changing anything (pushes the same commit).
  if ! out=$(git -C "$DIR" push --dry-run --porcelain origin "HEAD:refs/heads/$BRANCH" 2>&1); then
    fail_perm "$out"
  fi
else
  echo "Branch $BRANCH does not exist yet – creating it"
  git worktree add --quiet --detach "$DIR"
  (
    cd "$DIR"
    git checkout --quiet --orphan "$BRANCH"
    git rm -rf --quiet . >/dev/null 2>&1 || true
    git clean -fdq
    printf '# monitor-data\n\nWritten automatically by the VCA Stock Monitor workflow. Do not edit.\n' > README.md
    git add README.md
    git commit --quiet --no-gpg-sign -m "monitor: initialise data branch"
  )
  if ! out=$(git -C "$DIR" push --quiet origin "HEAD:refs/heads/$BRANCH" 2>&1); then
    if remote_has_branch; then
      echo "Another run created $BRANCH first – using it"
      git -C "$DIR" fetch --quiet origin "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH"
      git -C "$DIR" reset --quiet --hard "origin/$BRANCH"
    else
      fail_perm "$out"   # push failed and nobody else created it: permissions
    fi
  fi
fi
git -C "$DIR" branch --set-upstream-to="origin/$BRANCH" "$BRANCH" >/dev/null 2>&1 || true
echo "data branch ready at $(git -C "$DIR" rev-parse --short HEAD)"
