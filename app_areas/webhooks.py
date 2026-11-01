"""Webhooks and the calendar subscription: part of CoraApp (see app.py)."""

import asyncio
from datetime import datetime, timezone

from textual import work

from mail.account import READY as EMAIL_READY, EmailNotReady, GraphError
from mail.webhook import WebhookServer, calendar_urls, webhook_app, webhook_secrets
from subconscious.event_bus import CalendarChangedEvent
from app_areas.shared import (
    MAIL_NOTIFY_SECONDS, MAIL_SUBSCRIPTION_LIFETIME, MAIL_SUBSCRIPTION_RENEW, WEBHOOK_STARTUP_SECONDS,
)


class WebhooksMixin:
    """Webhooks and the calendar subscription."""

    @work(group="calendar", exclusive=True, exit_on_error=False)
    async def refresh_calendar(self) -> None:
        """Her coming week into her context, at launch once email is ready and then hourly. Kept as it
        was when the calendar can't be read; a new reason why not is said once in Activity."""
        if self.closing or self.email_status.state != EMAIL_READY:
            return
        try:
            context = await self.calendar.coming_week(datetime.now(timezone.utc))
        except EmailNotReady as error:
            problem = str(error)
        except Exception as error:
            problem = f"Cora's calendar couldn't be read: {str(error) if isinstance(error, GraphError) else type(error).__name__}"
        else:
            self.live.calendar_context = context
            if self.calendar_problem:
                self.activity.write("Cora's calendar can be read again.", "Calendar")
            self.calendar_problem = ""
            return
        if problem != self.calendar_problem:
            self.activity.write(problem, "Calendar not read")
        self.calendar_problem = problem

    @work(group="webhook-setup", exit_on_error=False)
    async def set_webhooks_enabled(self, enabled: bool) -> None:
        try:
            await self.conversation_persistence.call_database(self.persistence.save_webhook_enabled, enabled)
        except Exception as error:
            self.activity.write(f"Saving webhooks {'on' if enabled else 'off'} failed ({type(error).__name__}).",
                                "Webhooks")
            self.notify("That couldn't be saved. Check Activity.", severity="error")
            return
        await self.apply_webhook_settings()

    @work(group="webhook-setup", exit_on_error=False)
    async def save_webhook_address(self, public_url: str, port: str) -> None:
        try:
            port_number = int(port.strip() or "0")
        except ValueError:
            self.notify("The local port must be a number.", severity="warning")
            return
        try:
            await self.conversation_persistence.call_database(
                self.persistence.save_webhook_address, public_url, port_number,
            )
        except ValueError as error:
            self.notify(str(error), severity="warning")
            return
        except Exception as error:
            self.activity.write(f"Saving the webhook address failed ({type(error).__name__}).", "Webhooks")
            self.notify("That couldn't be saved. Check Activity.", severity="error")
            return
        self.notify("Webhook address saved.")
        await self.apply_webhook_settings()

    @work(group="webhook-setup", exit_on_error=False)
    async def restart_webhooks(self, rotate: bool = False) -> None:
        await self.apply_webhook_settings(rotate)

    async def apply_webhook_settings(self, rotate: bool = False) -> None:
        """Load the settings and start, restart or stop the listener to match; then subscribe to her mail.

        Rotating makes new secrets, so Microsoft is given new URLs and the old ones stop working.
        """
        async with self.webhook_lock:
            try:
                self.webhook_settings = await self.conversation_persistence.call_database(
                    self.persistence.load_webhook_settings,
                )
            except Exception as error:
                self.activity.write(f"Webhook settings didn't load ({type(error).__name__}).", "Webhooks")
                return
            if self.webhook_controls is not None:
                self.webhook_controls.enabled = self.webhook_settings.enabled
                self.webhook_controls.public_url = self.webhook_settings.public_url or ""
                self.webhook_controls.port = self.webhook_settings.port
            await self.stop_webhooks()
            settings = self.webhook_settings
            if self.closing or not settings.enabled or not settings.public_url:
                return
            try:
                self.webhook_secrets = await asyncio.to_thread(webhook_secrets, rotate)
            except Exception as error:
                self.webhook_status = "Not listening: the system credential store isn't available."
                self.activity.write(f"Webhook secrets couldn't be loaded ({type(error).__name__}).", "Webhooks")
                return
            if rotate:
                self.activity.write("New webhook secrets made.", "Webhooks")
            server = WebhookServer(
                webhook_app(lambda: self.webhook_secrets, self.on_mail_webhook, self.on_mail_lifecycle,
                            self.on_calendar_webhook, self.on_calendar_lifecycle),
                settings.port,
            )
            try:
                server.open()
            except OSError as error:
                self.webhook_status = f"Not listening: port {settings.port} is in use or not allowed."
                self.activity.write(f"Webhooks couldn't listen on port {settings.port} ({error.strerror or 'in use'}).",
                                    "Webhooks")
                return
            self.webhook_server = server
            self.webhook_worker = self.run_worker(server.serve(), group="webhook", exit_on_error=False)
            self.webhook_status = f"Listening on 127.0.0.1:{settings.port}"
            self.activity.write(f"Listening for webhooks on 127.0.0.1:{settings.port}.", "Webhooks")
        self.ensure_mail_subscription()
        self.ensure_calendar_subscription()

    async def stop_webhooks(self) -> None:
        """Microsoft told to stop first, then the listener closed."""
        await self.drop_mail_subscription()
        await self.drop_calendar_subscription()
        server, worker = self.webhook_server, self.webhook_worker
        self.webhook_server = self.webhook_worker = None
        self.webhook_status = ""
        if server is None:
            return
        server.stop()
        if worker is not None:
            try:
                await asyncio.wait_for(worker.wait(), WEBHOOK_STARTUP_SECONDS)
            except Exception:
                worker.cancel()
        self.activity.write("Stopped listening for webhooks.", "Webhooks")

    def webhook_listening(self) -> bool:
        server = self.webhook_server
        return server is not None and server.server is not None and server.server.started

    def on_calendar_webhook(self) -> None:
        """Microsoft says her calendar changed: an event for whoever cares, carrying nothing from the request."""
        self.event_bus.publish(CalendarChangedEvent(source="graph"))

    def on_calendar_lifecycle(self, event: str) -> None:
        self.activity.write(f"Microsoft's calendar subscription: {event}.", "Webhooks")
        if event == "missed":
            self.event_bus.publish(CalendarChangedEvent(source="graph"))
        elif event == "subscriptionRemoved":
            self.calendar_subscription = None
            self.ensure_calendar_subscription()
        elif event == "reauthorizationRequired":
            self.ensure_calendar_subscription(renew=True)

    def on_calendar_changed(self) -> None:
        """A CalendarChangedEvent: her coming week read again, once, a moment after the first of a burst."""
        if self.closing or self.calendar_notify_timer is not None:
            return
        self.calendar_notify_timer = self.set_timer(MAIL_NOTIFY_SECONDS, self.calendar_notified)

    def calendar_notified(self) -> None:
        self.calendar_notify_timer = None
        self.refresh_calendar()

    def ensure_calendar_subscription(self, renew: bool = False) -> None:
        """Graph's subscription to changes in her calendar, made or renewed when due, as with mail."""
        if self.closing or self.webhook_server is None or self.email_status.state != EMAIL_READY:
            return
        if self.calendar_subscription_worker is not None and not self.calendar_subscription_worker.is_finished:
            return
        subscription = self.calendar_subscription
        if subscription is not None and not renew \
                and subscription.expires - datetime.now(timezone.utc) > MAIL_SUBSCRIPTION_RENEW:
            return
        self.calendar_subscription_worker = self.run_worker(
            self.subscribe_to_calendar(), group="calendar-subscription", exit_on_error=False,
        )

    async def subscribe_to_calendar(self) -> None:
        for _ in range(WEBHOOK_STARTUP_SECONDS * 10):
            if self.webhook_listening() or self.webhook_server is None:
                break
            await asyncio.sleep(0.1)
        server, settings, secrets_now = self.webhook_server, self.webhook_settings, self.webhook_secrets
        if server is None or not self.webhook_listening() or secrets_now is None or not settings.public_url:
            return
        expires = datetime.now(timezone.utc) + MAIL_SUBSCRIPTION_LIFETIME
        try:
            if self.calendar_subscription is not None:
                try:
                    self.calendar_subscription = await self.calendar.renew(self.calendar_subscription.id, expires)
                    self.calendar_subscription_shown("renewed")
                    return
                except GraphError:
                    self.calendar_subscription = None
            url, lifecycle = calendar_urls(settings.public_url, secrets_now[0])
            subscription = await self.calendar.subscribe_to_changes(url, lifecycle, secrets_now[1], expires)
        except Exception as error:
            detail = str(error) if isinstance(error, (GraphError, EmailNotReady)) else type(error).__name__
            self.calendar_subscription_status = f"Calendar: not subscribed ({detail}). Tried again in 10 minutes."
            self.activity.write(f"Subscribing to calendar changes failed ({detail}).", "Webhooks")
            return
        if self.webhook_server is not server or self.closing:
            try:
                await self.calendar.unsubscribe(subscription.id)
            except Exception:
                pass
            return
        self.calendar_subscription = subscription
        self.calendar_subscription_shown("made")

    def calendar_subscription_shown(self, what: str) -> None:
        until = self.calendar_subscription.expires.astimezone().strftime("%a %d %b %H:%M")
        self.calendar_subscription_status = f"Calendar: Microsoft tells Cora of changes straight away (until {until})."
        self.activity.write(f"Calendar subscription {what}, until {until}.", "Webhooks")

    async def drop_calendar_subscription(self) -> None:
        subscription, self.calendar_subscription = self.calendar_subscription, None
        self.calendar_subscription_status = ""
        if subscription is None:
            return
        try:
            await self.calendar.unsubscribe(subscription.id)
        except Exception as error:
            self.activity.write(f"Ending the calendar subscription failed ({type(error).__name__}).", "Webhooks")
            return
        self.activity.write("Calendar subscription ended.", "Webhooks")
