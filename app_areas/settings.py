"""The Settings screen and model choices: part of CoraApp (see app.py)."""

import asyncio

from textual import work

from ui.model_settings import EmailControls, ModelSettingsScreen, ServiceControls, SignalControls, WebhookControls
from services.catalogue import SERVICES
from services.credentials import get_token


class SettingsMixin:
    """The Settings screen and model choices."""

    @work(group="model-settings", exclusive=True)
    async def open_model_settings(self) -> None:
        try:
            models = await self.conversation_persistence.call_database(self.persistence.load_models)
            enabled = await self.conversation_persistence.call_database(self.persistence.load_enabled_services)
            self.service_tool_choices = await self.conversation_persistence.call_database(
                self.persistence.load_service_tools,
            )
            self.signal_settings = await self.conversation_persistence.call_database(
                self.persistence.load_signal_settings,
            )
            self.signal_clients = {
                client.number: client
                for client in await self.conversation_persistence.call_database(self.persistence.load_clients)
            }
            has_token = {
                service_id for service_id, service in SERVICES.items()
                if service.needs_token and await asyncio.to_thread(get_token, service_id)
            }
            has_email_app = await asyncio.to_thread(self.email_account.client_id) is not None
            email_tenant = await asyncio.to_thread(self.email_account.tenant)
            self.email_person = await asyncio.to_thread(self.email_account.person_address)
            self.email_signature = await asyncio.to_thread(self.email_account.signature)
        except Exception as error:
            self.activity.write(f"Model settings load failed ({type(error).__name__}).", "Model settings error")
            self.notify("Could not load model settings. Check Activity.", severity="error")
            return
        if not self.closing:
            self.push_screen(ModelSettingsScreen(
                models, self.save_model_setting, self.screen_looking, self.set_screen_looking,
                services=ServiceControls(
                    status=self.services.status, set_enabled=self.set_service_enabled,
                    save_token=self.save_service_token, forget_token=self.forget_service_token,
                    set_tool_enabled=self.set_service_tool, enabled=set(enabled), has_token=has_token,
                    tools_on={service_id: set(tools) for service_id, tools in self.service_tool_choices.items()},
                ),
                signal=SignalControls(
                    status=lambda: self.signal.status, set_enabled=self.set_signal_enabled,
                    save_number=self.save_signal_number, link=self.link_signal,
                    clients=lambda: sorted(self.signal_clients.values(), key=lambda client: client.name.lower()),
                    add_client=self.add_signal_client, remove_client=self.remove_signal_client,
                    enabled=self.signal_settings.enabled, person_number=self.signal_settings.allowed_number,
                ),
                email=self.make_email_controls(has_email_app, email_tenant),
                webhooks=self.make_webhook_controls(),
            ), callback=self.settings_closed)

    def make_webhook_controls(self) -> WebhookControls:
        self.webhook_controls = WebhookControls(
            status=lambda: (
                self.webhook_status,
                "\n".join(filter(None, (self.mail_subscription_status, self.calendar_subscription_status))),
                self.mail_subscription is not None,
            ),
            set_enabled=self.set_webhooks_enabled, save_address=self.save_webhook_address,
            rotate=lambda: self.restart_webhooks(rotate=True), enabled=self.webhook_settings.enabled,
            public_url=self.webhook_settings.public_url or "", port=self.webhook_settings.port,
        )
        return self.webhook_controls

    def make_email_controls(self, has_client_id: bool, tenant: str = "") -> EmailControls:
        self.email_controls = EmailControls(
            status=lambda: self.email_status, save_app=self.save_email_app,
            sign_in=self.sign_in_email, sign_out=self.sign_out_email, has_client_id=has_client_id, tenant=tenant,
            person_address=self.email_person, save_person=self.save_email_person,
            signature=self.email_signature, save_signature=self.save_email_signature,
        )
        return self.email_controls

    def settings_closed(self, _result=None) -> None:
        # Tool changes reach Cora at a connect, so reconnect once now rather than after every switch.
        if self.service_tools_changed and self.live.ready and not self.standby and not self.closing:
            self.activity.write("Reconnecting so Cora has the service tools you changed.", "Service tools changed")
            self.action_reconnect()

    def set_screen_looking(self, enabled: bool) -> None:
        self.screen_looking = enabled
        self.activity.write(
            "Cora can look at the screen." if enabled else "Cora cannot look at the screen.",
            "Screen looking on" if enabled else "Screen looking off",
        )

    @work(group="model-settings-save")
    async def save_model_setting(self, area: str, model: str) -> None:
        try:
            await self.conversation_persistence.call_database(self.persistence.save_model, area, model)
        except Exception as error:
            self.activity.write(f"Model setting save failed ({type(error).__name__}).", "Model settings error")
            self.notify("Could not save the model setting. Check Activity.", severity="error")
            return
        self.activity.write(f"The {area} model is now {model}.", "Model settings saved")
        # Live's session config holds the delegation model; restart Live so it applies now.
        # In standby or while disconnected, the next connect loads it anyway.
        if area == "delegation" and self.live.ready and not self.standby:
            self.action_reconnect()
