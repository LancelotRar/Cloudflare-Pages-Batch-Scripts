import time

import httpx

CF_API_BASE = "https://api.cloudflare.com/client/v4"


class CfApiClient:
    """Cloudflare REST API client with retry logic."""

    def __init__(self, account_id: str, token: str):
        self.account_id = account_id
        self.token = token
        self._client = httpx.Client(
            base_url=CF_API_BASE,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=30,
        )

    def _request(self, method: str, path: str, body: dict | None = None) -> dict | None:
        """Make an API request with retry logic.
        
        Retries on 5xx and network errors (up to 3 attempts with 2/4/8s backoff).
        4xx errors are not retried.
        """
        backoff = [2, 4, 8]
        url = f"/accounts/{self.account_id}{path}"
        
        for attempt in range(3):
            try:
                resp = self._client.request(method, url, json=body)
                data = resp.json()
                
                if resp.status_code >= 400:
                    is_transient = resp.status_code >= 500 or resp.status_code == 429
                    if is_transient and attempt < 2:
                        time.sleep(backoff[attempt])
                        continue
                    return data
                
                return data
                
            except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError):
                if attempt < 2:
                    time.sleep(backoff[attempt])
                    continue
                return None
            except Exception:
                if attempt < 2:
                    time.sleep(backoff[attempt])
                    continue
                return None
        
        return None

    def _paginated_get(self, path: str) -> list[dict]:
        """Fetch all pages of a paginated GET endpoint."""
        results: list[dict] = []
        page = 1
        while True:
            data = self._request("GET", f"{path}?page={page}&per_page=50")
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
        return self._paginated_get("/pages/projects")

    def get_project(self, name: str) -> dict | None:
        data = self._request("GET", f"/pages/projects/{name}")
        if data and data.get("success"):
            return data["result"]
        return None

    def create_project(self, name: str, branch: str = "main") -> dict | None:
        return self._request("POST", "/pages/projects", {
            "name": name,
            "production_branch": branch,
        })

    def delete_project(self, name: str) -> dict | None:
        return self._request("DELETE", f"/pages/projects/{name}")

    def patch_project_config(self, name: str, deployment_configs: dict) -> bool:
        data = self._request("PATCH", f"/pages/projects/{name}", {
            "deployment_configs": deployment_configs,
        })
        return data is not None and data.get("success", False)

    def list_deployments(self, project_name: str) -> list[dict]:
        return self._paginated_get(f"/pages/projects/{project_name}/deployments")

    def delete_deployment(self, project_name: str, deployment_id: str) -> dict | None:
        return self._request("DELETE", f"/pages/projects/{project_name}/deployments/{deployment_id}")

    def add_domain(self, project_name: str, domain: str) -> dict | None:
        return self._request("POST", f"/pages/projects/{project_name}/domains", {
            "name": domain,
        })

    def delete_domain(self, project_name: str, domain: str) -> dict | None:
        return self._request("DELETE", f"/pages/projects/{project_name}/domains/{domain}")

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

    def close(self):
        self._client.close()
