import time
from urllib.parse import quote, urlencode

import httpx

CF_API_BASE = "https://api.cloudflare.com/client/v4"


class CfApiClient:
    """Cloudflare REST API client with retry logic."""

    def __init__(self, account_id: str, token: str, timeout: int = 30):
        self.account_id = account_id
        self.token = token
        self.last_error: str | None = None
        self._client = httpx.Client(
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )

    def _request(self, method: str, path: str, body: dict | None = None) -> dict | None:
        """Make an API request with retry logic.

        Retries on transient errors (5xx, 429, network timeouts/errors)
        up to 3 attempts with 2/4/8s backoff.
        4xx errors and programming errors are NOT retried and are returned / raised.
        """
        url = f"{CF_API_BASE}/accounts/{self.account_id}{path}"
        return self._request_url(method, url, body)

    def _request_url(self, method: str, url: str, body: dict | None = None) -> dict | None:
        """Make a request to an absolute Cloudflare API URL with retry logic."""
        backoff = [2, 4, 8]
        self.last_error = None

        for attempt in range(3):
            try:
                resp = self._client.request(method, url, json=body)
                data = resp.json()

                if resp.status_code >= 400:
                    is_transient = resp.status_code >= 500 or resp.status_code == 429
                    if is_transient and attempt < 2:
                        time.sleep(backoff[attempt])
                        continue
                    self.last_error = self._format_api_errors(resp.status_code, data)
                    return data

                return data

            except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as exc:
                if attempt < 2:
                    time.sleep(backoff[attempt])
                    continue
                self.last_error = f"Cloudflare API 网络请求失败：{exc}"
                return None

        return None

    @staticmethod
    def _add_query_param(path: str, param: str, value: object) -> str:
        """Safely append a query parameter to a path, preserving any existing params."""
        sep = "&" if "?" in path else "?"
        return f"{path}{sep}{param}={value}"

    @staticmethod
    def _format_api_errors(status_code: int, data: dict) -> str:
        errors = data.get("errors", [])
        details = "; ".join(
            f"[{error.get('code')}]: {error.get('message')}"
            for error in errors
            if isinstance(error, dict)
        )
        return f"Cloudflare API {status_code} {details}".rstrip()

    def _paginated_get(self, path: str) -> list[dict]:
        """Fetch all pages of a paginated GET endpoint."""
        results: list[dict] = []
        page = 1
        while True:
            url = self._add_query_param(self._add_query_param(path, "page", page), "per_page", 50)
            data = self._request("GET", url)
            if not data or not data.get("success"):
                break
            results.extend(data.get("result", []))
            result_info = data.get("result_info", {})
            total_pages = result_info.get("total_pages", 1)
            if page >= total_pages:
                break
            page += 1
        return results

    def list_projects(self) -> list[dict]:
        data = self._request("GET", "/pages/projects")
        if data and data.get("success"):
            return data.get("result", [])
        return []

    def get_project(self, name: str) -> dict | None:
        data = self._request("GET", f"/pages/projects/{quote(name, safe='')}")
        if data and data.get("success"):
            return data["result"]
        return None

    def create_project(self, name: str, branch: str = "main") -> dict | None:
        return self._request("POST", "/pages/projects", {
            "name": name,
            "production_branch": branch,
        })

    def delete_project(self, name: str) -> dict | None:
        return self._request("DELETE", f"/pages/projects/{quote(name, safe='')}")

    def patch_project_config(self, name: str, deployment_configs: dict) -> bool:
        data = self._request("PATCH", f"/pages/projects/{quote(name, safe='')}", {
            "deployment_configs": deployment_configs,
        })
        return data is not None and data.get("success", False)

    def list_deployments(self, project_name: str) -> list[dict]:
        data = self._request("GET", f"/pages/projects/{project_name}/deployments")
        if data and data.get("success"):
            return data.get("result", [])
        return []

    def delete_deployment(self, project_name: str, deployment_id: str) -> dict | None:
        return self._request("DELETE", f"/pages/projects/{project_name}/deployments/{deployment_id}")

    def add_domain(self, project_name: str, domain: str) -> dict | None:
        project_path = quote(project_name, safe="")
        return self._request("POST", f"/pages/projects/{project_path}/domains", {
            "name": domain,
        })

    def list_domains(self, project_name: str) -> list[dict] | None:
        project_path = quote(project_name, safe="")
        data = self._request("GET", f"/pages/projects/{project_path}/domains")
        if data and data.get("success"):
            return data.get("result", [])
        return None

    def delete_domain(self, project_name: str, domain: str) -> dict | None:
        project_path = quote(project_name, safe="")
        domain_path = quote(domain, safe="")
        return self._request("DELETE", f"/pages/projects/{project_path}/domains/{domain_path}")

    def list_dns_records(self, zone_id: str, content: str) -> list[dict] | None:
        results: list[dict] = []
        page = 1
        while True:
            query = urlencode({"page": page, "per_page": 100, "content": content})
            url = f"{CF_API_BASE}/zones/{zone_id}/dns_records?{query}"
            data = self._request_url("GET", url)
            if not data or not data.get("success"):
                if data and not self.last_error:
                    self.last_error = self._format_api_errors(200, data)
                return None
            results.extend(data.get("result", []))
            total_pages = data.get("result_info", {}).get("total_pages", 1)
            if page >= total_pages:
                return results
            page += 1

    def create_dns_record(self, zone_id: str, record: dict) -> dict | None:
        url = f"{CF_API_BASE}/zones/{zone_id}/dns_records"
        return self._request_url("POST", url, record)

    def update_dns_record(self, zone_id: str, record_id: str, record: dict) -> dict | None:
        url = f"{CF_API_BASE}/zones/{zone_id}/dns_records/{record_id}"
        return self._request_url("PUT", url, record)

    def delete_dns_record(self, zone_id: str, record_id: str) -> dict | None:
        url = f"{CF_API_BASE}/zones/{zone_id}/dns_records/{record_id}"
        return self._request_url("DELETE", url)

    def list_kv_namespaces(self) -> list[dict]:
        return self._paginated_get("/storage/kv/namespaces")

    def create_kv_namespace(self, title: str) -> dict | None:
        return self._request("POST", "/storage/kv/namespaces", {
            "title": title,
        })

    def delete_kv_namespace(self, namespace_id: str) -> dict | None:
        return self._request("DELETE", f"/storage/kv/namespaces/{namespace_id}")

    def ensure_kv_namespace(self, title: str) -> str | None:
        """Find KV namespace by title, or create if not exists. Returns namespace ID."""
        if not title:
            return None
        namespaces = self.list_kv_namespaces()
        for ns in namespaces:
            if ns.get("title") == title:
                return ns.get("id")
        result = self.create_kv_namespace(title)
        if result and result.get("success"):
            return result["result"].get("id")
        return None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def close(self):
        self._client.close()
