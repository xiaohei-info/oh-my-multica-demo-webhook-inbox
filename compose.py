"""Container composition root.

Wires the real FastAPI app from `WEBHOOK_SECRET` and `DATABASE_PATH`,
failing startup clearly when WEBHOOK_SECRET is missing. Re-exposes `app` for
`uvicorn compose:app`.
"""

from src.api import create_app
from src.config import load_settings
from src.repository import new_repository
from src.service import Service

settings = load_settings()
repository = new_repository(str(settings.database_path))
service = Service(settings.webhook_secret, repository)
app = create_app(service)
