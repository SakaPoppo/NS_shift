#!/bin/sh
set -eu

exec gunicorn config.wsgi:application \
  --bind "0.0.0.0:${PORT:-10000}" \
  --workers "${WEB_CONCURRENCY:-1}" \
  --access-logfile - \
  --error-logfile -
