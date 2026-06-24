#!/usr/bin/env bash
set -o errexit

case "${1}" in
  build)
    pip install -r requirements.txt
    (cd theme/static_src && npm install)
    python manage.py tailwind build
    python manage.py collectstatic --no-input
    python manage.py migrate
    ;;
  start)
    python -m gunicorn config.asgi:application -k uvicorn.workers.UvicornWorker
    ;;
  *)
    echo "usage: $0 {build|start}"
    exit 1
    ;;
esac
