"""Flask front end for the upside-case analyzer.

A package rather than a loose script so `gunicorn web.server:app` runs from the
repository root, where the pipeline module `app.py` is importable by its own
name. Serving it as `--chdir web app:app` would put `web/` first on the path and
shadow that module with this one.
"""
