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
from datetime import datetime, timezone
from os import getenv
from pathlib import Path
from uuid import UUID

import httpx2 as httpx
import keyring

from .formatting import email_fragment, email_html
from keyring.errors import PasswordDeleteError

CLIENT_ID_SETTING = "CORA_EMAIL_CLIENT_ID"
KEYRING_SERVICE = "cora"
CLIENT_ID_ACCOUNT = "email:client-id"
TENANT_SETTING = "CORA_EMAIL_TENANT"
TENANT_ACCOUNT = "email:tenant"
# The person's own address: mail from it that Microsoft verifies is treated like their Signal chat.
PERSON_ACCOUNT = "email:person"
# Her signature for new emails, plain text: Outlook's own isn't added to mail sent through Graph.
# Kept short, as the Windows credential store holds little.
SIGNATURE_ACCOUNT = "email:signature"
SIGNATURE_MAX_CHARS = 500
# One plain address: no display names, lists or line breaks.
ADDRESS = re.compile(r"^[^@\s<>,;\"]+@[^@\s<>,;\"]+\.[^@\s<>,;\"]+$")
# A work or school account signs in through its organisation's directory (a domain such as
# example.com, or the Directory (tenant) ID); without one, personal accounts (outlook.com).
LOGIN = "https://login.microsoftonline.com"
PERSONAL = "consumers"
DOMAIN = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$")
# Asked for once at sign-in, so sending later (E2) needs no second sign-in.
SCOPES = ["User.Read", "Mail.ReadWrite", "Mail.Send"]
# Her calendar (C1-C3), asked for at the same sign-in. Mail tokens don't need it, so a sign-in from
# before the calendar still reads and sends mail; only the calendar asks for one more sign-in.
CALENDAR_SCOPES = ["Calendars.ReadWrite"]
SIGN_IN_SCOPES = SCOPES + CALENDAR_SCOPES
GRAPH = "https://graph.microsoft.com/v1.0"
REQUEST_SECONDS = 20
PREVIEW_MAX_CHARS = 300
# A thread as she reads it: its latest messages, each cut to this much of its own new text.
THREAD_MESSAGES_MAX = 20
# Delivery failure reports, by their subject (Microsoft's own, and other mail servers' for late bounces).
BOUNCE_SUBJECT = re.compile(
    r"^\s*(undeliverable|undelivered mail|delivery status notification \(failure\)|mail delivery failed"
    r"|returned mail|delivery has failed|delivery failure|failure notice)", re.IGNORECASE,
)
BOUNCES_MAX = 50
PLAIN_TEXT = {"Prefer": 'outlook.body-content-type="text"'}
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


def access_token(app, scopes: list[str] = SCOPES) -> str:
    """A current access token from the saved sign-in, refreshed if needed. Blocking."""
    accounts = app.get_accounts()
    if not accounts:
        raise EmailNotReady("Cora's email isn't signed in yet; sign her in in Settings > Services > Email.")
    result = app.acquire_token_silent(scopes, account=accounts[0])
    if not result or "access_token" not in result:
        if scopes == CALENDAR_SCOPES:
            raise EmailNotReady(f"Cora's calendar needs her signed in once more to allow it; {SIGN_IN_HINT}.")
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

    def person_address(self) -> str:
        """The person's own address, or "" when they haven't given one."""
        return keyring.get_password(KEYRING_SERVICE, PERSON_ACCOUNT) or ""

    def save_person_address(self, value: str) -> str:
        """Their own address, lowercased; empty forgets it. ValueError for anything that isn't one address."""
        value = value.strip().lower()
        if not value:
            try:
                keyring.delete_password(KEYRING_SERVICE, PERSON_ACCOUNT)
            except PasswordDeleteError:
                pass  # nothing was saved
            return ""
        if len(value) > 320 or not ADDRESS.match(value):
            raise ValueError("That isn't one email address.")
        keyring.set_password(KEYRING_SERVICE, PERSON_ACCOUNT, value)
        return value

    def signature(self) -> str:
        """Her signature for new emails, or "" when there's none."""
        return keyring.get_password(KEYRING_SERVICE, SIGNATURE_ACCOUNT) or ""

    def save_signature(self, value: str) -> str:
        """Plain text, tidied; empty forgets it. ValueError when it's too long."""
        value = "\n".join(line.rstrip() for line in value.replace("\r\n", "\n").split("\n")).strip()
        if not value:
            try:
                keyring.delete_password(KEYRING_SERVICE, SIGNATURE_ACCOUNT)
            except PasswordDeleteError:
                pass  # nothing was saved
            return ""
        if len(value) > SIGNATURE_MAX_CHARS:
            raise ValueError(f"Keep the signature under {SIGNATURE_MAX_CHARS} characters.")
        keyring.set_password(KEYRING_SERVICE, SIGNATURE_ACCOUNT, value)
        return value

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

    def token(self, scopes: list[str] = SCOPES) -> str:
        return access_token(self.app(), scopes)

    def signed_in(self) -> bool:
        return self.client_id() is not None and bool(self.app().get_accounts())

    def start_sign_in(self) -> dict:
        """Starts a sign-in with a code; the flow holds user_code and verification_uri."""
        flow = self.app().initiate_device_flow(scopes=SIGN_IN_SCOPES)
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


@dataclass(frozen=True)
class InboxMail:
    """An unread message in her inbox, with Microsoft's verdict on who really sent it."""
    id: str
    thread_id: str
    sender_name: str
    sender_address: str
    subject: str
    received_at: datetime
    text: str
    has_attachments: bool
    # Microsoft's own Authentication-Results header (the first, added on arrival), or "".
    authentication: str


def inbox_mail(message: dict) -> InboxMail:
    sender = (message.get("from") or {}).get("emailAddress") or {}
    headers = message.get("internetMessageHeaders") or []
    authentication = next((header.get("value") or "" for header in headers
                           if (header.get("name") or "").lower() == "authentication-results"), "")
    return InboxMail(
        message["id"], message.get("conversationId") or "", sender.get("name") or "",
        (sender.get("address") or "").lower(), message.get("subject") or "",
        parse_graph_time(message["receivedDateTime"]),
        ((message.get("uniqueBody") or {}).get("content") or "").strip()[:MESSAGE_TEXT_MAX_CHARS],
        bool(message.get("hasAttachments")), authentication,
    )


def verified_from(mail: InboxMail, address: str) -> bool:
    """From that address, and Microsoft found DMARC passing for its domain: the From line wasn't forged."""
    address = address.strip().lower()
    if not address or mail.sender_address != address:
        return False
    results = mail.authentication.lower()
    domain = re.search(r"header\.from=([^\s;]+)", results)
    return re.search(r"\bdmarc=pass\b", results) is not None and domain is not None \
        and domain.group(1) == address.rsplit("@", 1)[1]


def is_bounce(message) -> bool:
    """A delivery failure report, not someone's mail."""
    return bool(BOUNCE_SUBJECT.match(message.subject or ""))


@dataclass(frozen=True)
class Bounce:
    """A report that her mail couldn't be delivered: in its thread, or naming the address in its text."""
    id: str
    thread_id: str
    received_at: datetime
    text: str

    def names(self, address: str) -> bool:
        return re.search(rf"(?<![\w.+-]){re.escape(address)}(?![\w-]|\.\w)", self.text, re.IGNORECASE) is not None


def signed(text: str, signature: str, name: str = "Cora") -> str:
    """Her message with the signature below it. When the signature starts with her name, a last line
    that is only her name (Cora, or her full name, such as Cora Vale) is dropped, so she isn't signed twice."""
    if not signature:
        return text
    lines = text.rstrip().split("\n")
    last = lines[-1].strip(" -–—").lower()
    first = signature.strip().split("\n", 1)[0].strip().lower()
    if (len(lines) > 1 and last.startswith(name.lower()) and first.startswith(last)
            and signature.lower().startswith(name.lower())):
        lines.pop()
    return "\n".join(lines).rstrip() + "\n\n" + signature


BODY_TAG = re.compile(r"<body[^>]*>", re.IGNORECASE)


def reply_body(text: str, draft_body: dict) -> dict:
    """Her text, formatted (mail/formatting.py), above the quote in Outlook's reply draft."""
    quoted = draft_body.get("content") or ""
    if not quoted.strip():
        return {"contentType": "HTML", "content": email_html(text)}
    if draft_body.get("contentType", "").lower() != "html":
        quoted = "<div>" + html.escape(quoted).replace("\r\n", "\n").replace("\n", "<br>") + "</div>"
    mine = email_fragment(text) + "<br>"
    tag = BODY_TAG.search(quoted)
    content = quoted[:tag.end()] + mine + quoted[tag.end():] if tag else mine + quoted
    return {"contentType": "HTML", "content": content}


INBOX_RESOURCE = "me/mailFolders('inbox')/messages"


def graph_time(at: datetime) -> str:
    return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_graph_time(text: str) -> datetime:
    # Graph may give seven decimal places, more than Python reads.
    text = text.replace("Z", "+00:00")
    if "." in text:
        whole, rest = text.split(".", 1)
        digits = re.match(r"\d*", rest).group()
        text = f"{whole}.{digits[:6]}{rest[len(digits):]}"
    return datetime.fromisoformat(text)


@dataclass(frozen=True)
class MailSubscription:
    """Graph's subscription to new mail in her inbox, and when it lapses unless renewed."""
    id: str
    expires: datetime


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
        """A new email from her, formatted from her Markdown: saved as a draft first, so its thread is
        known, then sent."""
        recipient = {"address": address} | ({"name": name} if name else {})
        draft = await self._request("POST", "/me/messages", body={
            "subject": subject, "body": {"contentType": "HTML", "content": email_html(text)},
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
        }, headers=PLAIN_TEXT)
        messages = [message for message in page.get("value", []) if not message.get("isDraft")]
        return sorted(map(thread_message, messages), key=lambda message: message.received_at)[-THREAD_MESSAGES_MAX:]

    async def reply_in_thread(self, thread_id: str, address: str, name: str | None, text: str) -> SentMail:
        """A reply to them in an existing thread, after its latest message; LookupError if it's gone."""
        messages = await self.thread(thread_id)
        if not messages:
            raise LookupError("That email thread is gone")
        return await self.reply(messages[-1].id, address, name, text)

    async def bounces(self, since: datetime) -> list[Bounce]:
        """Delivery failure reports in her inbox received since then, newest first. Nothing is marked read."""
        page = await self._request("GET", "/me/mailFolders/inbox/messages", params={
            "$filter": f"receivedDateTime ge {since.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
            "$orderby": "receivedDateTime desc", "$top": str(BOUNCES_MAX),
            "$select": "id,conversationId,subject,receivedDateTime,body",
        }, headers=PLAIN_TEXT)
        return [
            Bounce(message["id"], message.get("conversationId") or "",
                   datetime.fromisoformat(message["receivedDateTime"].replace("Z", "+00:00")),
                   (message.get("body") or {}).get("content") or "")
            for message in page.get("value", []) if BOUNCE_SUBJECT.match(message.get("subject") or "")
        ]

    async def unread(self, since: datetime) -> list[InboxMail]:
        """Unread inbox mail received since then, oldest first, with each one's authentication results."""
        page = await self._request("GET", "/me/mailFolders/inbox/messages", params={
            "$filter": f"receivedDateTime ge {graph_time(since)} and isRead eq false",
            "$orderby": "receivedDateTime asc", "$top": str(BOUNCES_MAX),
            "$select": "id,conversationId,from,subject,receivedDateTime,uniqueBody,hasAttachments,"
                       "internetMessageHeaders",
        }, headers=PLAIN_TEXT)
        return [inbox_mail(message) for message in page.get("value", [])]

    async def subscribe(self, url: str, lifecycle_url: str, client_state: str, expires: datetime,
                        resource: str = INBOX_RESOURCE, change_type: str = "created") -> MailSubscription:
        """Graph calls `url` when mail lands in her inbox (or on changes to another resource, such as
        her calendar's events); any earlier subscription to it goes first.

        Graph checks the URL answers its handshake before this returns, so the listener must be up.
        """
        page = await self._get("/subscriptions")
        for old in page.get("value", []):
            if old.get("notificationUrl") == url and old.get("id"):
                await self.unsubscribe(old["id"])
        made = await self._request("POST", "/subscriptions", body={
            "changeType": change_type, "resource": resource, "notificationUrl": url,
            "lifecycleNotificationUrl": lifecycle_url, "clientState": client_state,
            "expirationDateTime": graph_time(expires),
        })
        return MailSubscription(made["id"], parse_graph_time(made["expirationDateTime"]))

    async def renew(self, subscription_id: str, expires: datetime) -> MailSubscription:
        made = await self._request("PATCH", f"/subscriptions/{subscription_id}",
                                   body={"expirationDateTime": graph_time(expires)})
        return MailSubscription(subscription_id, parse_graph_time(made["expirationDateTime"]))

    async def unsubscribe(self, subscription_id: str) -> None:
        await self._request("DELETE", f"/subscriptions/{subscription_id}")

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
