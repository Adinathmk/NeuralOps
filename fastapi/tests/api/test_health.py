from httpx import AsyncClient


async def test_health_check_success(client: AsyncClient):
    """Test that the health endpoint is reachable and returns standard status."""
    response = await client.get("/health")

    # Assert status code is either 200 (OK) or 207 (Degraded) depending on local DB/Redis state
    assert response.status_code in (200, 207)

    data = response.json()
    assert "status" in data
    assert data["status"] in ("ok", "degraded")
    assert data["data"]["service"] == "neuralops-fastapi"
