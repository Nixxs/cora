"""Cora's own Outlook mailbox through Microsoft Graph, set up in Settings > Services > Email.

Three things make it work: the Azure app's ID and, for a work or school account, her
organisation's directory (tenant) (neither secret; kept in the system credential store, with
CORA_EMAIL_CLIENT_ID and CORA_EMAIL_TENANT in .env as fallbacks), and her sign-in, kept by msal-extensions in a
file encrypted for this user (DPAPI on Windows), never in .env or the database: Windows'
credential store is too small for it. Blocking calls (the credential store, the sign-in cache,
refreshing a token) run off the event loop.
"""

import asyncio
from dataclasses import dataclass
import html
import re
from datetime import datetime
from os import getenv
from pathlib import Path
from uuid import UUID

import httpx2 as httpx
import keyring
from keyring.errors import PasswordDeleteError

CLIENT_ID_SETTING = "CORA_EMAIL_CLIENT_ID"
KEYRING_SERVICE = "cora"
CLIENT_ID_ACCOUNT = "email:client-id"
TENANT_SETTING = "CORA_EMAIL_TENANT"
TENANT_ACCOUNT = "email:tenant"
# A work or school account signs in through its organisation's directory (a domain such as
# example.com, or the Directory (tenant) ID); without one, personal accounts (outlook.com).
LOGIN = "https://login.microsoftonline.com"
PERSONAL = "consumers"
DOMAIN = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$")
# Asked for once at sign-in, so sending later (E2) needs no second sign-in.
SCOPES = ["User.Read", "Mail.ReadWrite", "Mail.Send"]
GRAPH = "https://graph.microsoft.com/v1.0"
REQUEST_SECONDS = 20
PREVIEW_MAX_CHARS = 300
# A thread as she reads it: its latest messages, each cut to this much of its own new text.
THREAD_MESSAGES_MAX = 20
MESSAGE_TEXT_MAX_CHARS = 4000
SIGN_IN_HINT = "sign her in again in Settings > Services > Email"

# EmailStatus.state
NO_APP, SIGNED_OUT, SIGNING_IN, READY, FAILED = "no app", "signed out", "signing in", "ready", "failed"


class EmailNotReady(Exception):
    """Not set up, not signed in, or the sign-in has lapsed; the message says which, never a token."""


class GraphError(Exception):
    """Microsoft refused a request; the message carries only its status and error code, never mail."""


def graph_error_code(response) -> str:
    try:
        return str(response.json()["error"]["code"])[:80]
    except Exception:
        return "no error code"


@dataclass(frozen=True)
class EmailStatus:
    state: str = NO_APP
    address: str | None = None
    # While signing in: the code to enter and where.
    code: str | None = None
    link: str | None = None
    reason: str | None = None


def sign_in_cache_path() -> Path:
    return Path(getenv("LOCALAPPDATA") or Path.home()) / "cora" / "email-sign-in.bin"


def access_token(app) -> str:
    """A current access token from the saved sign-in, refreshed if needed. Blocking."""
    accounts = app.get_accounts()
    if not accounts:
        raise EmailNotReady("Cora's email isn't signed in yet; sign her in in Settings > Services > Email.")
    result = app.acquire_token_silent(SCOPES, account=accounts[0])
    if not result or "access_token" not in result:
        raise EmailNotReady(f"Cora's email sign-in has lapsed; {SIGN_IN_HINT}.")
    return result["access_token"]


class EmailAccount:
    """The app ID, her saved sign-in, and signing in and out. Every method blocks: call off the loop."""

    def __init__(self, cache_path: Path | None = None) -> None:
        self.cache_path = cache_path or sign_in_cache_path()
        self._app = None
        self._app_for: tuple[str, str] | None = None
        self._flow: dict | None = None

    def client_id(self) -> str | None:
        return keyring.get_password(KEYRING_SERVICE, CLIENT_ID_ACCOUNT) or getenv(CLIENT_ID_SETTING, "").strip() or None

    def save_client_id(self, value: str) -> str:
        """Saves the app's Application (client) ID, a GUID; raises ValueError for anything else."""
        try:
            value = str(UUID(value.strip()))
        except ValueError:
            raise ValueError("That isn't an Application (client) ID; it looks like 1a2b3c4d-....") from None
        keyring.set_password(KEYRING_SERVICE, CLIENT_ID_ACCOUNT, value)
        return value

    def tenant(self) -> str:
        """Her organisation's directory, or "" for a personal account."""
        return keyring.get_password(KEYRING_SERVICE, TENANT_ACCOUNT) or getenv(TENANT_SETTING, "").strip()

    def save_tenant(self, value: str) -> str:
        """A domain or Directory (tenant) ID; empty means a personal account. ValueError for anything else."""
        value = value.strip().lower()
        if not value:
            try:
                keyring.delete_password(KEYRING_SERVICE, TENANT_ACCOUNT)
            except PasswordDeleteError:
                pass  # nothing was saved
            return ""
        try:
            value = str(UUID(value))
        except ValueError:
            if not DOMAIN.match(value) or len(value) > 253:
                raise ValueError("That isn't a domain or a Directory (tenant) ID.") from None
        keyring.set_password(KEYRING_SERVICE, TENANT_ACCOUNT, value)
        return value

    def authority(self) -> str:
        return f"{LOGIN}/{self.tenant() or PERSONAL}"

    def app(self):
        """The sign-in client for the current app ID and directory, with its cache in the encrypted file."""
        app_client_id = self.client_id()
        if app_client_id is None:
            raise EmailNotReady("Cora's email isn't set up; add the app ID in Settings > Services > Email.")
        authority = self.authority()
        if self._app is None or self._app_for != (app_client_id, authority):
            import msal
            from msal_extensions import PersistedTokenCache, build_encrypted_persistence

            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache = PersistedTokenCache(build_encrypted_persistence(str(self.cache_path)))
            self._app = msal.PublicClientApplication(app_client_id, authority=authority, token_cache=cache)
            self._app_for = (app_client_id, authority)
        return self._app

    def token(self) -> str:
        return access_token(self.app())

    def signed_in(self) -> bool:
        return self.client_id() is not None and bool(self.app().get_accounts())

    def start_sign_in(self) -> dict:
        """Starts a sign-in with a code; the flow holds user_code and verification_uri."""
        flow = self.app().initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise EmailNotReady(f"Microsoft wouldn't start a sign-in: {flow.get('error_description') or flow.get('error')}")
        self._flow = flow
        return flow

    def finish_sign_in(self, flow: dict) -> str:
        """Waits until the code is used (or expires or is cancelled); returns who signed in."""
        try:
            result = self.app().acquire_token_by_device_flow(flow)
        finally:
            self._flow = None
        if "access_token" not in result:
            raise EmailNotReady(f"Sign-in didn't finish: {result.get('error_description') or result.get('error')}")
        claims = result.get("id_token_claims") or {}
        # Only the account that just signed in is kept, so an earlier one (such as someone else's
        # signing in by mistake) can never be the one she uses.
        signed_in = f"{claims.get('oid')}.{claims.get('tid')}"
        app = self.app()
        for account in app.get_accounts():
            if account.get("home_account_id") != signed_in:
                app.remove_account(account)
        return claims.get("preferred_username", "")

    def cancel_sign_in(self) -> None:
        """Stops a sign-in waiting for its code, within a few seconds (MSAL checks this between polls)."""
        if self._flow is not None:
            self._flow["expires_at"] = 0

    def sign_out(self) -> None:
        self.cancel_sign_in()
        if self.client_id() is None:
            return
        app = self.app()
        for account in app.get_accounts():
            app.remove_account(account)


@dataclass(frozen=True)
class MailSummary:
    id: str
    sender_name: str
    sender_address: str
    subject: str
    received_at: datetime
    preview: str
    unread: bool


def summary(message: dict) -> MailSummary:
    sender = (message.get("from") or {}).get("emailAddress") or {}
    return MailSummary(
        message["id"], sender.get("name") or "", sender.get("address") or "",
        message.get("subject") or "", datetime.fromisoformat(message["receivedDateTime"].replace("Z", "+00:00")),
        " ".join((message.get("bodyPreview") or "").split())[:PREVIEW_MAX_CHARS], not message.get("isRead", True),
    )


@dataclass(frozen=True)
class MailMessage:
    """One message of a thread: only its own new text (Microsoft's uniqueBody), without quoted replies."""
    id: str
    sender_name: str
    sender_address: str
    subject: str
    received_at: datetime
    text: str


def thread_message(message: dict) -> MailMessage:
    sender = (message.get("from") or {}).get("emailAddress") or {}
    text = ((message.get("uniqueBody") or {}).get("content") or "").strip()
    return MailMessage(
        message["id"], sender.get("name") or "", (sender.get("address") or "").lower(),
        message.get("subject") or "", datetime.fromisoformat(message["receivedDateTime"].replace("Z", "+00:00")),
        text[:MESSAGE_TEXT_MAX_CHARS],
    )


BODY_TAG = re.compile(r"<body[^>]*>", re.IGNORECASE)


def reply_body(text: str, draft_body: dict) -> dict:
    """Her plain text, escaped so none of it becomes markup, above the quote in Outlook's reply draft."""
    quoted = draft_body.get("content") or ""
    if draft_body.get("contentType", "").lower() != "html" or not quoted.strip():
        return {"contentType": "Text", "content": text}
    mine = "<div>" + html.escape(text).replace("\r\n", "\n").replace("\n", "<br>") + "</div><br>"
    tag = BODY_TAG.search(quoted)
    content = quoted[:tag.end()] + mine + quoted[tag.end():] if tag else mine + quoted
    return {"contentType": "HTML", "content": content}


@dataclass(frozen=True)
class SentMail:
    id: str
    # Microsoft's conversationId: replies in the same thread share it.
    thread_id: str


class Mailbox:
    """Her address, her latest inbox mail, and sending as her (the caller has the person approve first)."""

    def __init__(self, get_token, client_factory=httpx.AsyncClient) -> None:
        # get_token() -> an access token, blocking; raises EmailNotReady.
        self.get_token = get_token
        self.client_factory = client_factory

    async def _request(self, method: str, path: str, params: dict | None = None, body: dict | None = None,
                       headers: dict | None = None) -> dict:
        token = await asyncio.to_thread(self.get_token)
        async with self.client_factory(timeout=REQUEST_SECONDS) as client:
            response = await client.request(method, f"{GRAPH}{path}", params=params, json=body,
                                            headers={"Authorization": f"Bearer {token}", **(headers or {})})
        if response.status_code >= 400:
            code = graph_error_code(response)
            if response.status_code == 401:
                raise EmailNotReady(f"Microsoft refused Cora's email sign-in ({code}); {SIGN_IN_HINT}.")
            raise GraphError(f"Microsoft said HTTP {response.status_code} ({code}).")
        # Sending answers 202 with no body.
        return response.json() if response.status_code not in (202, 204) else {}

    async def _get(self, path: str, params: dict | None = None) -> dict:
        return await self._request("GET", path, params)

    async def send(self, address: str, name: str | None, subject: str, text: str) -> SentMail:
        """A new plain-text email from her: saved as a draft first, so its thread is known, then sent."""
        recipient = {"address": address} | ({"name": name} if name else {})
        draft = await self._request("POST", "/me/messages", body={
            "subject": subject, "body": {"contentType": "Text", "content": text},
            "toRecipients": [{"emailAddress": recipient}],
        })
        await self._request("POST", f"/me/messages/{draft['id']}/send")
        return SentMail(draft["id"], draft["conversationId"])

    async def reply(self, message_id: str, address: str, name: str | None, text: str) -> SentMail:
        """A reply in that message's thread, to the one address given (never reply-all): her text on
        top of the earlier mail Outlook quotes below, as anyone's reply looks."""
        draft = await self._request("POST", f"/me/messages/{message_id}/createReply")
        recipient = {"address": address} | ({"name": name} if name else {})
        await self._request("PATCH", f"/me/messages/{draft['id']}", body={
            "body": reply_body(text, draft.get("body") or {}), "toRecipients": [{"emailAddress": recipient}],
            "ccRecipients": [], "bccRecipients": [],
        })
        await self._request("POST", f"/me/messages/{draft['id']}/send")
        return SentMail(draft["id"], draft["conversationId"])

    async def thread(self, thread_id: str) -> list[MailMessage]:
        """A thread's latest messages, either way, oldest first; drafts left out. Nothing is marked read."""
        quoted = thread_id.replace("'", "''")
        page = await self._request("GET", "/me/messages", params={
            "$filter": f"conversationId eq '{quoted}'", "$top": "50",
            "$select": "id,from,subject,receivedDateTime,uniqueBody,isDraft",
        }, headers={"Prefer": 'outlook.body-content-type="text"'})
        messages = [message for message in page.get("value", []) if not message.get("isDraft")]
        return sorted(map(thread_message, messages), key=lambda message: message.received_at)[-THREAD_MESSAGES_MAX:]

    async def reply_in_thread(self, thread_id: str, address: str, name: str | None, text: str) -> SentMail:
        """A reply to them in an existing thread, after its latest message; LookupError if it's gone."""
        messages = await self.thread(thread_id)
        if not messages:
            raise LookupError("That email thread is gone")
        return await self.reply(messages[-1].id, address, name, text)

    async def mark_read(self, message_id: str) -> None:
        await self._request("PATCH", f"/me/messages/{message_id}", body={"isRead": True})

    async def address(self) -> str:
        me = await self._get("/me", {"$select": "mail,userPrincipalName"})
        return me.get("mail") or me.get("userPrincipalName") or ""

    async def recent(self, limit: int = 10, unread_only: bool = False) -> list[MailSummary]:
        """Her newest inbox mail, newest first; nothing is marked read."""
        params = {
            "$top": str(limit), "$orderby": "receivedDateTime desc",
            "$select": "id,from,subject,receivedDateTime,bodyPreview,isRead",
        }
        if unread_only:
            params["$filter"] = "isRead eq false"
        page = await self._get("/me/mailFolders/inbox/messages", params)
        return [summary(message) for message in page.get("value", [])]
