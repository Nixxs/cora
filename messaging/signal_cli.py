"""Cora's signal-cli service, reached over its local JSON-RPC API (one daemon, any linked accounts)."""

import base64
from itertools import count

import httpx2 as httpx


class SignalError(Exception):
    """signal-cli answered with an error."""


def signal_failure(error: Exception) -> str:
    """For Activity: signal-cli's own reason (it describes the failure, never message text), else the type."""
    if isinstance(error, SignalError) and str(error).strip():
        return f"SignalError: {str(error).strip()[:200]}"
    return type(error).__name__


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

    async def user_status(self, account: str, number: str) -> dict:
        result = await self.rpc("getUserStatus", {"account": account, "recipient": [number]})
        return result[0] if result else {}

    async def is_registered(self, account: str, number: str) -> bool:
        """Whether Signal can deliver to this number: false if it isn't on Signal, or hides its number."""
        return bool((await self.user_status(account, number)).get("isRegistered"))

    async def signal_id(self, account: str, number: str) -> str | None:
        """The Signal ID (uuid) behind a number, or None if it isn't on Signal or can't be found by number.

        A first message from someone often carries only this ID: Signal shares their number once
        the message request is accepted.
        """
        status = await self.user_status(account, number)
        return status.get("uuid") if status.get("isRegistered") else None

    async def update_profile(self, account: str, given_name: str, avatar_path: str | None = None) -> None:
        """Her Signal name, and picture from a file inside the container (avatar_path), seen by everyone she messages."""
        params = {"account": account, "givenName": given_name}
        if avatar_path:
            params["avatar"] = avatar_path
        await self.rpc("updateProfile", params, timeout=60)

    async def accept_request(self, account: str, recipient: str) -> None:
        """Accept a message request, so they see her as a contact and her replies show normally."""
        await self.rpc("sendMessageRequestResponse", {"account": account, "recipient": [recipient], "type": "accept"})

    async def send(self, account: str, recipient: str, message: str) -> None:
        await self.rpc("send", {"account": account, "recipient": [recipient], "message": message})

    async def attachment(self, account: str, attachment_id: str) -> bytes:
        """A received attachment's bytes, from signal-cli's stored copy (see container.delete_attachments)."""
        result = await self.rpc("getAttachment", {"account": account, "id": attachment_id}, timeout=60)
        return base64.b64decode(result["data"])

    async def send_typing(self, account: str, recipient: str, stop: bool = False) -> None:
        params = {"account": account, "recipient": [recipient]}
        if stop:
            params["stop"] = True
        await self.rpc("sendTyping", params)

    async def events(self, account: str, on_open=lambda: None):
        """Each event the daemon streams for one account, as its raw JSON text, until it drops."""
        async with self.http(None) as http:
            async with http.stream("GET", "/api/v1/events", params={"account": account}) as response:
                response.raise_for_status()
                on_open()
                data = []
                async for line in response.aiter_lines():
                    if line.startswith("data:"):
                        data.append(line[5:].lstrip())
                    elif not line and data:
                        # A blank line ends one server-sent event.
                        yield "\n".join(data)
                        data = []

    async def delete_account(self, number: str) -> None:
        """Forget an account here (its keys and messages); the phone keeps it."""
        await self.rpc("deleteLocalAccountData", {"account": number, "ignoreRegistered": True})
