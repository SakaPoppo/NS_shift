#!/usr/bin/env bash
set -o errexit
set -o nounset
set -o pipefail

case "${1:-}" in
  build)
    pip install -r requirements.txt
    npm --prefix theme/static_src ci
    python manage.py tailwind build
    python manage.py collectstatic --no-input
    python manage.py migrate --no-input
    ;;
  start)
    python -m gunicorn config.asgi:application \
      -k uvicorn.workers.UvicornWorker \
      --bind "0.0.0.0:${PORT:-10000}" \
      --workers "${WEB_CONCURRENCY:-1}" \
      --timeout "${GUNICORN_TIMEOUT:-120}"
    ;;
  *)
    echo "usage: $0 {build|start}"
    exit 1
    ;;
esac
