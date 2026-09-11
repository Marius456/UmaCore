#!/usr/bin/env bash
set -Eeuo pipefail

branch="${1:-}"
repo_dir="${HOME}/UmaCore"
container_name="para-bot-container"
rollback_name="${container_name}-rollback"
image_name="para-bot"
health_timeout_seconds=60

if [[ -z "$branch" || ! "$branch" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]]; then
    echo "Invalid or missing branch name." >&2
    exit 2
fi

for command_name in git docker flock; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "Required command is unavailable: $command_name" >&2
        exit 1
    fi
done

if [[ ! -d "$repo_dir/.git" ]]; then
    echo "Git repository not found at $repo_dir" >&2
    exit 1
fi

cd "$repo_dir"

# Keep concurrent invocations from manipulating the same checkout/container.
exec 9>".git/umacore-deploy.lock"
if ! flock -n 9; then
    echo "Another UmaCore deployment is already running." >&2
    exit 1
fi

if [[ -n "$(git status --porcelain --untracked-files=normal)" ]]; then
    echo "The server checkout has local changes. Deployment aborted:" >&2
    git status --short >&2
    exit 1
fi

echo "Fetching origin..."
git fetch --prune origin

remote_ref="refs/remotes/origin/${branch}"
if ! git show-ref --verify --quiet "$remote_ref"; then
    echo "Remote branch does not exist: origin/$branch" >&2
    exit 1
fi

# A detached deployment commit is safe to replace if it is still represented
# by an origin ref. Otherwise it may be a server-only commit that must be kept.
origin_refs_containing_head="$(git branch --remotes --contains HEAD --format='%(refname)')"
if [[ "$origin_refs_containing_head" != *refs/remotes/origin/* ]]; then
    echo "HEAD is not contained in any origin branch; it may be a local-only commit." >&2
    echo "Deployment aborted to avoid discarding it." >&2
    exit 1
fi

if [[ ! -f .env ]]; then
    echo "Required environment file is missing: $repo_dir/.env" >&2
    exit 1
fi

if docker container inspect "$rollback_name" >/dev/null 2>&1; then
    echo "Rollback container already exists: $rollback_name" >&2
    echo "Resolve it manually before deploying again." >&2
    exit 1
fi

git checkout --detach "$remote_ref"
commit="$(git rev-parse --short=12 HEAD)"
commit_image="${image_name}:${commit}"

echo "Building $commit_image while the current bot remains online..."
docker build --memory="800m" --cpu-shares=512 -t "$commit_image" .

had_previous_container=false
cutover_started=false

rollback() {
    local exit_code=$?
    trap - ERR INT TERM

    if [[ "$cutover_started" == true ]]; then
        echo "New deployment failed; collecting logs and rolling back..." >&2
        docker logs --tail 200 "$container_name" >&2 || true
        docker rm -f "$container_name" >/dev/null 2>&1 || true

        if [[ "$had_previous_container" == true ]]; then
            if docker rename "$rollback_name" "$container_name" \
                && docker start "$container_name" >/dev/null; then
                echo "Previous container restored successfully." >&2
            else
                echo "Automatic rollback failed; manual recovery is required." >&2
            fi
        else
            echo "No previous container was available to restore." >&2
        fi
    fi

    exit "$exit_code"
}
trap rollback ERR INT TERM

if docker container inspect "$container_name" >/dev/null 2>&1; then
    had_previous_container=true
    echo "Stopping the current container..."
    docker stop "$container_name" >/dev/null
    if ! docker rename "$container_name" "$rollback_name"; then
        echo "Could not prepare the rollback container; restarting the current container." >&2
        docker start "$container_name" >/dev/null || true
        exit 1
    fi
fi

cutover_started=true
echo "Starting $container_name from $commit_image..."
docker run -d \
    --name "$container_name" \
    --env-file .env \
    --restart always \
    "$commit_image" >/dev/null

echo "Waiting for the internal health endpoint..."
healthy=false
deadline=$((SECONDS + health_timeout_seconds))
while (( SECONDS < deadline )); do
    if docker exec "$container_name" python -c \
        'import os, urllib.request; port=os.getenv("BOT_API_PORT", "7890"); urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2).read()' \
        >/dev/null 2>&1; then
        healthy=true
        break
    fi
    sleep 2
done

if [[ "$healthy" != true ]]; then
    echo "Container did not become healthy within ${health_timeout_seconds} seconds." >&2
    false
fi

docker tag "$commit_image" "${image_name}:latest"
if [[ "$had_previous_container" == true ]]; then
    docker rm "$rollback_name" >/dev/null
fi

cutover_started=false
trap - ERR INT TERM

echo "Deployment successful: origin/$branch at $commit"
docker ps --filter "name=^/${container_name}$" --format 'Container: {{.Names}} | Image: {{.Image}} | Status: {{.Status}}'
