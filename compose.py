"""Container composition root for the independently delivered layers."""

from src.api import create_app
from src.config import load_settings
from src.domain import DatabaseStatus, Event, EventResult, HealthResult, HealthStatus
from src.repository import Repository, new_repository
from src.service import Service


class RuntimeService:
    """Adapt the API port to the service/repository contracts without changing them."""

    def __init__(self, service: Service, repository: Repository) -> None:
        self._service = service
        self._repository = repository

    def receive_event(
        self,
        event_id: str,
        raw_body: bytes,
        signature_header: str | None,
    ) -> EventResult:
        return self._service.receive_event(signature_header, event_id, raw_body)

    def get_event(self, event_id: str) -> Event | None:
        return self._repository.get_event(event_id)

    def check_health(self) -> HealthResult:
        try:
            self._repository.get_event("__healthcheck__")
        except Exception:
            return HealthResult(
                status=HealthStatus.DEGRADED,
                database=DatabaseStatus.ERROR,
            )
        return HealthResult(status=HealthStatus.OK, database=DatabaseStatus.OK)


settings = load_settings()
repository = new_repository(str(settings.database_path))
service = Service(settings.webhook_secret, repository)
app = create_app(RuntimeService(service, repository))


@app.on_event("shutdown")
def close_repository() -> None:
    repository.close()
