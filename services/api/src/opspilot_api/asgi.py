"""Uvicorn entrypoint: ``uvicorn opspilot_api.asgi:app``."""

from opspilot_api.main import create_app

app = create_app()
