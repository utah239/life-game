#!/bin/sh
set -eu

ROOT=/volume1/docker/life-game
STAGING="${HOME}/.life-game-deploy"
SERVICE=life-game-experiment
CONTAINER=life_game_experiment
DOCKER=/usr/local/bin/docker

for path in \
    "${STAGING}/root/game.py" \
    "${STAGING}/dashboard/server.py" \
    "${STAGING}/dashboard/aquarium_stream.py" \
    "${STAGING}/institutions/spatial.py" \
    "${STAGING}/docker-compose.yml"; do
    if [ ! -f "${path}" ]; then
        echo "staged runtime file is missing: ${path}" >&2
        exit 1
    fi
done

# checkpointを書いている瞬間を避けるため、旧コンテナを先に静止する。
if sudo test -f "${ROOT}/docker-compose.yml"; then
    (cd "${ROOT}" && sudo "${DOCKER}" compose stop "${SERVICE}")
fi

if sudo test -d "${ROOT}/app" \
        || sudo test -f "${ROOT}/state/aquarium_world.json"; then
    timestamp=$(date '+%Y%m%d-%H%M%S')
    backup_dir="${ROOT}/backups/${timestamp}"
    sudo mkdir -p "${backup_dir}"
    if sudo test -f "${ROOT}/docker-compose.yml"; then
        sudo cp -p "${ROOT}/docker-compose.yml" "${backup_dir}/"
    fi
    if sudo test -d "${ROOT}/app"; then
        # 大きいsweep reportは再配布可能なので除き、実行コードだけを退避する。
        sudo mkdir -p \
            "${backup_dir}/app/institutions" \
            "${backup_dir}/app/dashboard"
        sudo cp -p "${ROOT}/app/"*.py "${backup_dir}/app/"
        sudo cp -p "${ROOT}/app/institutions/"*.py \
            "${backup_dir}/app/institutions/"
        sudo cp -p "${ROOT}/app/dashboard/"*.py \
            "${ROOT}/app/dashboard/"*.html \
            "${backup_dir}/app/dashboard/"
    fi
    if sudo test -f "${ROOT}/state/aquarium_world.json"; then
        sudo cp -p "${ROOT}/state/aquarium_world.json" "${backup_dir}/"
    fi
    if sudo test -f "${ROOT}/state/aquarium.html"; then
        sudo cp -p "${ROOT}/state/aquarium.html" "${backup_dir}/"
    fi
    if sudo test -f "${ROOT}/state/aquarium_stream.json"; then
        sudo cp -p "${ROOT}/state/aquarium_stream.json" "${backup_dir}/"
    fi
    if sudo test -f "${ROOT}/state/aquarium_performance.json"; then
        sudo cp -p "${ROOT}/state/aquarium_performance.json" "${backup_dir}/"
    fi
    echo "Pre-deploy runtime/checkpoint backup: ${backup_dir}"
fi

sudo mkdir -p \
    "${ROOT}/app/institutions" \
    "${ROOT}/app/dashboard/reports" \
    "${ROOT}/state"
sudo cp "${STAGING}/root/"*.py "${ROOT}/app/"
sudo cp "${STAGING}/dashboard/"* "${ROOT}/app/dashboard/"
sudo cp "${STAGING}/institutions/"*.py "${ROOT}/app/institutions/"
sudo cp "${STAGING}/reports/"barter_sweep_*.html \
    "${ROOT}/app/dashboard/reports/"
sudo cp "${STAGING}/docker-compose.yml" "${ROOT}/docker-compose.yml"
sudo chown 65534:65534 "${ROOT}/state"
sudo chmod 700 "${ROOT}/state"
sudo find "${ROOT}/app" -type d -exec chmod 755 {} +
sudo find "${ROOT}/app" -type f -exec chmod 644 {} +

cd "${ROOT}"
sudo "${DOCKER}" compose config >/dev/null
# bind mount先のPython/HTMLだけを更新した場合も、既存コンテナを単に再利用せず
# 親Pythonプロセスを必ず新しいsourceから起動する。
sudo "${DOCKER}" compose up -d --force-recreate "${SERVICE}"

attempt=0
while [ "${attempt}" -lt 45 ]; do
    health=$(sudo "${DOCKER}" inspect \
        --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
        "${CONTAINER}" 2>/dev/null || true)
    if [ "${health}" = healthy ]; then
        sudo "${DOCKER}" compose ps "${SERVICE}"
        exit 0
    fi
    if [ "${health}" = unhealthy ]; then
        echo "container became unhealthy" >&2
        sudo "${DOCKER}" compose logs --tail 80 "${SERVICE}" >&2 || true
        exit 1
    fi
    attempt=$((attempt + 1))
    sleep 1
done

echo "container health check timed out" >&2
sudo "${DOCKER}" compose logs --tail 80 "${SERVICE}" >&2 || true
exit 1
