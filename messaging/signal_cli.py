"""Cora's signal-cli service, reached over its local JSON-RPC API (one daemon, any linked accounts)."""

from itertools import count

import httpx2 as httpx


class SignalError(Exception):
    """signal-cli answered with an error."""


class SignalClient:
    def __init__(self, base_url: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.transport = transport
        self.ids = count(1)

    def http(self, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base_url, timeout=timeout, transport=self.transport)

    async def check(self) -> bool:
        """Whether the daemon is up and answering."""
        try:
            async with self.http(5) as http:
                return (await http.get("/api/v1/check")).status_code == 200
        except httpx.HTTPError:
            return False

    async def rpc(self, method: str, params: dict | None = None, timeout: float = 30):
        request = {"jsonrpc": "2.0", "id": str(next(self.ids)), "method": method}
        if params:
            request["params"] = params
        async with self.http(timeout) as http:
            response = await http.post("/api/v1/rpc", json=request)
            response.raise_for_status()
            body = response.json()
        if "error" in body:
            raise SignalError((body["error"] or {}).get("message") or f"{method} failed")
        return body.get("result")

    async def accounts(self) -> list[str]:
        return [account["number"] for account in await self.rpc("listAccounts") or [] if account.get("number")]

    async def start_link(self) -> str:
        """A link address for a phone to scan, as a QR code, from Signal's linked devices."""
        return (await self.rpc("startLink"))["deviceLinkUri"]

    async def finish_link(self, uri: str, device_name: str, timeout: float) -> str:
        """Wait until the phone scans the code; returns the linked account's number."""
        result = await self.rpc("finishLink", {"deviceLinkUri": uri, "deviceName": device_name}, timeout=timeout)
        return result["number"]

    async def delete_account(self, number: str) -> None:
        """Forget an account here (its keys and messages); the phone keeps it."""
        await self.rpc("deleteLocalAccountData", {"account": number, "ignoreRegistered": True})
