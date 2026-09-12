#!/usr/bin/env bash
# Runs on the cloudbox codespace at every start (devcontainer postStartCommand
# fetches this from the scipio repo so the runner is updatable without touching
# cloudbox). Installs slyci, triggers scipio's .slyci/workflows pipeline, exits;
# the codespace idle-timeout shuts the box down afterwards.
set -u
# /tmp is wiped every boot — a wake that died before slyci wrote its own log
# left no trace at all (8/25: four cron wakes, zero evidence). Persist.
mkdir -p "$HOME/.slyci"
exec >>"$HOME/.slyci/runner.log" 2>&1
echo "=== scipio runner $(date -u +%FT%TZ) (boot $(uptime -s 2>/dev/null)) ==="

export PATH="$HOME/.local/bin:$PATH"
export GH_TOKEN="${GH_TOKEN:-${GITHUB_TOKEN:-}}"
[ -n "$GH_TOKEN" ] || { echo "no GH_TOKEN — set the Codespaces user secret"; exit 1; }

# heartbeat visible from anywhere: a commit status on main's head the moment
# the box wakes, before anything that can fail. "pending" that never turns
# into slyci success = the runner died between here and the pipeline.
HEAD_SHA=$(curl -fsS -H "Authorization: token $GH_TOKEN" https://api.github.com/repos/AssiamahS/scipio/commits/main 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["sha"])' 2>/dev/null || true)
[ -n "$HEAD_SHA" ] && curl -fsS -o /dev/null -X POST -H "Authorization: token $GH_TOKEN" \
  "https://api.github.com/repos/AssiamahS/scipio/statuses/$HEAD_SHA" \
  -d "{\"state\":\"pending\",\"context\":\"cloudbox/wake\",\"description\":\"box woke $(date -u +%H:%MZ), runner starting\"}" || true

# git auth for private clones + log pushes (gh CLI reads GH_TOKEN by itself).
# NOT a credential helper: the codespace ships a system helper that answers
# first with its cloudbox-scoped token and 403s on every other repo — the
# url rewrite below bypasses helpers entirely.
git config --global url."https://x-access-token:${GH_TOKEN}@github.com/".insteadOf "https://github.com/"
git config --global user.name "$(git config --global user.name || echo AssiamahS)"
git config --global user.email "$(git config --global user.email || echo <git-email>)"

command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh

if [ ! -d "$HOME/githubactions" ]; then
  git clone -q https://github.com/AssiamahS/githubactions "$HOME/githubactions"
else
  # fetch+reset, not pull: npm rewrites the lockfile and blocks a rebase
  git -C "$HOME/githubactions" fetch -q origin main && git -C "$HOME/githubactions" reset -q --hard origin/main
fi
(cd "$HOME/githubactions" && npm install --no-fund --no-audit >/dev/null)

SLYCI="node $HOME/githubactions/bin/slyci.js"
$SLYCI add AssiamahS/scipio main || true
$SLYCI trigger AssiamahS/scipio
RC=$?

# 9/5: the codespace idle timeout is 240 min, so one boot can carry several
# hourly slices instead of one. Re-trigger at :02 past each hour while the
# apply window (12:00-20:59 UTC) is open; process_queue.py owns the per-day
# caps, so an extra trigger outside its window is a cheap no-op. The box is
# reaped ~4h after boot; the :20 backstop wake in AssiamahS/githubactions
# boots it again for the afternoon half.
STATE="$HOME/.slyci/work/AssiamahS__scipio/auto-apply/batch_state.json"
day_done() { python3 -c "import json,sys,datetime; d=json.load(open('$STATE')); sys.exit(0 if d.get('last_batch')==datetime.datetime.utcnow().date().isoformat() else 1)" 2>/dev/null; }
while :; do
  h=$(date -u +%H); h=${h#0}
  if day_done; then echo "=== day complete, no more slices ==="; break; fi
  [ "$h" -ge 20 ] && break
  [ "$h" -lt 12 ] && break
  now=$(date -u +%s); next=$(( (now/3600+1)*3600 + 120 ))
  echo "=== next slice at $(date -u -d @$next +%H:%MZ 2>/dev/null || date -u -r $next +%H:%MZ) ==="
  sleep $((next-now))
  h=$(date -u +%H); h=${h#0}
  [ "$h" -ge 20 ] && break
  git -C "$HOME/githubactions" fetch -q origin main && git -C "$HOME/githubactions" reset -q --hard origin/main
  $SLYCI trigger AssiamahS/scipio
  RC=$?
done
# Free-tier core-hours: stop the box the moment the day's work is done
# instead of idling to the timeout. 403 here just means the token lacks the
# codespace scope; the idle timeout still reaps it.
if [ -n "${CODESPACE_NAME:-}" ]; then
  echo "=== self-stop $CODESPACE_NAME $(date -u +%H:%MZ) ==="
  sleep 20
  curl -fsS -o /dev/null -X POST -H "Authorization: token $GH_TOKEN" \
    "https://api.github.com/user/codespaces/$CODESPACE_NAME/stop" || echo "self-stop refused (idle timeout will reap)"
fi
[ -n "$HEAD_SHA" ] && curl -fsS -o /dev/null -X POST -H "Authorization: token $GH_TOKEN" \
  "https://api.github.com/repos/AssiamahS/scipio/statuses/$HEAD_SHA" \
  -d "{\"state\":\"$([ $RC -eq 0 ] && echo success || echo failure)\",\"context\":\"cloudbox/wake\",\"description\":\"runner finished $(date -u +%H:%MZ) exit $RC\"}" || true
echo "=== runner done $(date -u +%FT%TZ) (exit $RC) ==="
