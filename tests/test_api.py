"""Tests for cf_pages_batch_deploy.api"""

import httpx
import pytest
from pytest_httpx import HTTPXMock

from cf_pages_batch_scripts.api import CfApiClient

API_BASE = "https://api.cloudflare.com/client/v4"


@pytest.fixture
def client() -> CfApiClient:
    return CfApiClient(account_id="test_account", token="test_token")


class TestRequest:
    """Test _request method."""

    def test_success(self, client: CfApiClient, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            url=f"{API_BASE}/accounts/test_account/pages/projects",
            json={"success": True, "result": [{"name": "p1"}]},
        )
        data = client._request("GET", "/pages/projects")
        assert data is not None
        assert data["success"] is True
        assert data["result"][0]["name"] == "p1"

    def test_4xx_not_retried(self, client: CfApiClient, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            url=f"{API_BASE}/accounts/test_account/pages/projects",
            status_code=404,
            json={"success": False, "errors": [{"message": "Not found"}]},
        )
        data = client._request("GET", "/pages/projects")
        assert data is not None
        assert data["success"] is False

    def test_5xx_retried_then_fails(self, client: CfApiClient, httpx_mock: HTTPXMock):
        # Register 3 identical 500 responses (for initial + 2 retries)
        for _ in range(3):
            httpx_mock.add_response(
                url=f"{API_BASE}/accounts/test_account/pages/projects",
                status_code=500,
                json={"success": False},
            )
        data = client._request("GET", "/pages/projects")
        assert data is not None
        assert data["success"] is False

    def test_5xx_then_succeeds(self, client: CfApiClient, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            url=f"{API_BASE}/accounts/test_account/pages/projects",
            status_code=500,
            json={"success": False},
        )
        httpx_mock.add_response(
            url=f"{API_BASE}/accounts/test_account/pages/projects",
            status_code=500,
            json={"success": False},
        )
        httpx_mock.add_response(
            url=f"{API_BASE}/accounts/test_account/pages/projects",
            json={"success": True, "result": []},
        )
        data = client._request("GET", "/pages/projects")
        assert data is not None
        assert data["success"] is True

    def test_transport_error_retried(self, client: CfApiClient, httpx_mock: HTTPXMock):
        httpx_mock.add_exception(
            httpx.ConnectError("Connection refused"),
            url=f"{API_BASE}/accounts/test_account/pages/projects",
        )
        httpx_mock.add_exception(
            httpx.ConnectError("Connection refused"),
            url=f"{API_BASE}/accounts/test_account/pages/projects",
        )
        httpx_mock.add_response(
            url=f"{API_BASE}/accounts/test_account/pages/projects",
            json={"success": True, "result": []},
        )
        data = client._request("GET", "/pages/projects")
        assert data is not None
        assert data["success"] is True

    def test_programming_error_propagates(self, client: CfApiClient, httpx_mock: HTTPXMock):
        """Non-transient exceptions (e.g. JSON decode error) should propagate."""
        httpx_mock.add_response(
            url=f"{API_BASE}/accounts/test_account/pages/projects",
            content=b"not valid json",
        )
        with pytest.raises(ValueError):
            client._request("GET", "/pages/projects")


class TestPaginatedGet:
    def test_single_page(self, client: CfApiClient, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            url=f"{API_BASE}/accounts/test_account/storage/kv/namespaces?page=1&per_page=50",
            json={
                "success": True,
                "result": [{"id": "ns1", "title": "NS1"}],
                "result_info": {"total_pages": 1},
            },
        )
        results = client._paginated_get("/storage/kv/namespaces")
        assert len(results) == 1
        assert results[0]["title"] == "NS1"

    def test_multi_page(self, client: CfApiClient, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            url=f"{API_BASE}/accounts/test_account/storage/kv/namespaces?page=1&per_page=50",
            json={
                "success": True,
                "result": [{"id": "ns1"}],
                "result_info": {"total_pages": 2},
            },
        )
        httpx_mock.add_response(
            url=f"{API_BASE}/accounts/test_account/storage/kv/namespaces?page=2&per_page=50",
            json={
                "success": True,
                "result": [{"id": "ns2"}],
                "result_info": {"total_pages": 2},
            },
        )
        results = client._paginated_get("/storage/kv/namespaces")
        assert len(results) == 2

    def test_api_error(self, client: CfApiClient, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            url=f"{API_BASE}/accounts/test_account/storage/kv/namespaces?page=1&per_page=50",
            json={"success": False, "errors": [{"message": "Auth failed"}]},
        )
        results = client._paginated_get("/storage/kv/namespaces")
        assert results == []


class TestAddQueryParam:
    def test_path_without_params(self):
        result = CfApiClient._add_query_param("/path", "page", 1)
        assert result == "/path?page=1"

    def test_path_with_params(self):
        result = CfApiClient._add_query_param("/path?existing=1", "page", 2)
        assert result == "/path?existing=1&page=2"

    def test_multiple_calls(self):
        result = CfApiClient._add_query_param(
            CfApiClient._add_query_param("/path", "page", 1), "per_page", 50
        )
        assert result == "/path?page=1&per_page=50"


class TestApiMethods:
    def test_list_projects(self, client: CfApiClient, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            url=f"{API_BASE}/accounts/test_account/pages/projects",
            json={"success": True, "result": [{"name": "p1"}]},
        )
        projects = client.list_projects()
        assert len(projects) == 1
        assert projects[0]["name"] == "p1"

    def test_list_projects_empty(self, client: CfApiClient, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            url=f"{API_BASE}/accounts/test_account/pages/projects",
            json={"success": False},
        )
        assert client.list_projects() == []

    def test_get_project_found(self, client: CfApiClient, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            url=f"{API_BASE}/accounts/test_account/pages/projects/my-proj",
            json={"success": True, "result": {"name": "my-proj"}},
        )
        proj = client.get_project("my-proj")
        assert proj is not None
        assert proj["name"] == "my-proj"

    def test_get_project_not_found(self, client: CfApiClient, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            url=f"{API_BASE}/accounts/test_account/pages/projects/ghost",
            json={"success": False},
        )
        assert client.get_project("ghost") is None

    def test_create_project(self, client: CfApiClient, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            method="POST",
            url=f"{API_BASE}/accounts/test_account/pages/projects",
            json={"success": True, "result": {"name": "new-proj"}},
        )
        result = client.create_project("new-proj")
        assert result is not None
        assert result["success"] is True

    def test_delete_project(self, client: CfApiClient, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            method="DELETE",
            url=f"{API_BASE}/accounts/test_account/pages/projects/to-del",
            json={"success": True},
        )
        result = client.delete_project("to-del")
        assert result is not None

    def test_patch_project_config_success(self, client: CfApiClient, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            method="PATCH",
            url=f"{API_BASE}/accounts/test_account/pages/projects/p",
            json={"success": True},
        )
        assert client.patch_project_config("p", {"production": {"env_vars": {"K": {"value": "v"}}}}) is True

    def test_patch_project_config_failure(self, client: CfApiClient, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            method="PATCH",
            url=f"{API_BASE}/accounts/test_account/pages/projects/p",
            json={"success": False},
        )
        assert client.patch_project_config("p", {}) is False

    def test_add_domain(self, client: CfApiClient, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            method="POST",
            url=f"{API_BASE}/accounts/test_account/pages/projects/p/domains",
            json={"success": True},
        )
        result = client.add_domain("p", "example.com")
        assert result is not None

    def test_delete_domain(self, client: CfApiClient, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            method="DELETE",
            url=f"{API_BASE}/accounts/test_account/pages/projects/p/domains/example.com",
            json={"success": True},
        )
        result = client.delete_domain("p", "example.com")
        assert result is not None

    def test_ensure_kv_namespace_creates(self, client: CfApiClient, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            url=f"{API_BASE}/accounts/test_account/storage/kv/namespaces?page=1&per_page=50",
            json={"success": True, "result": [], "result_info": {"total_pages": 1}},
        )
        httpx_mock.add_response(
            method="POST",
            url=f"{API_BASE}/accounts/test_account/storage/kv/namespaces",
            json={"success": True, "result": {"id": "ns-new"}},
        )
        ns_id = client.ensure_kv_namespace("new-ns")
        assert ns_id == "ns-new"

    def test_ensure_kv_namespace_finds_existing(self, client: CfApiClient, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            url=f"{API_BASE}/accounts/test_account/storage/kv/namespaces?page=1&per_page=50",
            json={
                "success": True,
                "result": [{"id": "ns-exist", "title": "my-ns"}],
                "result_info": {"total_pages": 1},
            },
        )
        ns_id = client.ensure_kv_namespace("my-ns")
        assert ns_id == "ns-exist"

    def test_ensure_kv_namespace_empty_title(self, client: CfApiClient):
        assert client.ensure_kv_namespace("") is None


class TestContextManager:
    def test_with_statement_closes_client(self):
        with CfApiClient(account_id="a", token="t") as client:
            assert client._client is not None
        # After exit, the client should be closed
        with pytest.raises(RuntimeError):
            client._client.get("https://example.com")
