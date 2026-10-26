import logging
import httpx

logger = logging.getLogger(__name__)

class APIClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.client: httpx.AsyncClient | None = None

    async def start(self):
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=5.0),
            headers={"X-API-Key": self.api_key},
            follow_redirects=True,
        )


    async def lookup_id(self, record_id: str) -> dict:
        try:
            response = await self.client.get(
                f"{self.base_url}/id/{record_id}",
                headers={"X-API-Key": self.api_key},
            )

            if response.status_code == 200:
                return {
                    "status": "ok",
                    "data": response.json(),
                }

            if response.status_code == 404:
                return {"status": "not_found"}

            if response.status_code in (401, 403):
                return {"status": "auth_failed"}

            if response.status_code == 400:
                return {"status": "invalid"}

            return {
                "status": "error",
                "status_code": response.status_code,
            }

        except Exception as exc:
            name = type(exc).__name__

            if name in {
                "ConnectTimeout",
                "ReadTimeout",
                "WriteTimeout",
                "PoolTimeout",
            }:
                return {"status": "timeout"}

            if name in {"ConnectError", "NetworkError"}:
                return {"status": "unavailable"}

            return {"status": "error"}

    async def close(self):
        if self.client:
            await self.client.aclose()
            self.client = None

    async def lookup_mobile(self, mobile: str):
        if self.client is None:
            raise RuntimeError("API client not started")

        url = f"{self.base_url}/user/{mobile}"
        try:
            response = await self.client.get(url)
        except httpx.TimeoutException:
            return {"status": "timeout"}
        except httpx.RequestError as exc:
            logger.warning("API request failed: %s", exc.__class__.__name__)
            return {"status": "unavailable"}

        if response.status_code == 200:
            try:
                return {"status": "ok", "data": response.json()}
            except ValueError:
                logger.error("API returned invalid JSON")
                return {"status": "invalid_json"}

        if response.status_code == 404:
            return {"status": "not_found"}
        if response.status_code in (401, 403):
            logger.error("API authentication failed")
            return {"status": "auth_failed"}

        logger.warning("API returned status %s", response.status_code)
        return {"status": "error", "code": response.status_code}

    async def health(self):
        if self.client is None:
            return False
        try:
            response = await self.client.get(f"{self.base_url}/")
            return response.status_code == 200
        except httpx.RequestError:
            return False
