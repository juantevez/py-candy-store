import pytest
from fastapi.testclient import TestClient

from candy_store.main import create_app
from candy_store.shared.infrastructure.settings import Settings

pytestmark = pytest.mark.integration


def test_app_boots_with_real_wiring_and_background_tasks(migrated_database: str) -> None:
    app = create_app(Settings(database_url=migrated_database, outbox_poll_interval_seconds=0.05))

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
