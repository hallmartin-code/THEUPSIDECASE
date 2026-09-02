web: gunicorn web.server:app --bind 0.0.0.0:${PORT:-8020} --workers 1 --threads 8 --timeout 120 --graceful-timeout 30 --access-logfile - --error-logfile -
