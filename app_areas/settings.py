"""The Settings screen and model choices: part of CoraApp (see app.py)."""

import asyncio

from textual import work

import openai_key
from ui.model_settings import (
    EmailControls, GeneralControls, ModelSettingsScreen, ServiceControls, SignalControls, WebhookControls,
)
from services.catalogue import SERVICES
from services.credentials import get_token
from sign_ups import password as passwords


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
            voice = await self.conversation_persistence.call_database(self.persistence.load_voice)
            has_sign_up_password = bool(await asyncio.to_thread(passwords.sign_up_password))
            self.live.own_name = await asyncio.to_thread(passwords.sign_up_name)
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
                    set_client_calls=self.set_client_may_call,
                    enabled=self.signal_settings.enabled, person_number=self.signal_settings.allowed_number,
                ),
                email=self.make_email_controls(has_email_app, email_tenant, has_sign_up_password),
                webhooks=self.make_webhook_controls(),
                web=self.make_web_controls(),
                phone=self.make_phone_controls(),
                general=GeneralControls(
                    key_status=await asyncio.to_thread(self.key_status), save_key=self.save_openai_key,
                    voice=voice, save_voice=self.save_voice_setting,
                ),
            ), callback=self.settings_closed)

    def make_webhook_controls(self) -> WebhookControls:
        self.webhook_controls = WebhookControls(
            status=lambda: (
                # The listener may be running only for the web interface.
                self.webhook_status if self.webhook_settings.enabled else "",
                "\n".join(filter(None, (self.mail_subscription_status, self.calendar_subscription_status))),
                self.mail_subscription is not None,
            ),
            set_enabled=self.set_webhooks_enabled, save_address=self.save_webhook_address,
            rotate=lambda: self.restart_webhooks(rotate=True), enabled=self.webhook_settings.enabled,
            public_url=self.webhook_settings.public_url or "", port=self.webhook_settings.port,
        )
        return self.webhook_controls

    def make_email_controls(self, has_client_id: bool, tenant: str = "",
                            has_sign_up_password: bool = False) -> EmailControls:
        self.email_controls = EmailControls(
            status=lambda: self.email_status, save_app=self.save_email_app,
            sign_in=self.sign_in_email, sign_out=self.sign_out_email, has_client_id=has_client_id, tenant=tenant,
            person_address=self.email_person, save_person=self.save_email_person,
            signature=self.email_signature, save_signature=self.save_email_signature,
            has_sign_up_password=has_sign_up_password, save_sign_up_password=self.save_sign_up_password,
            sign_up_name=self.live.own_name, save_sign_up_name=self.save_sign_up_name,
        )
        return self.email_controls

    def settings_closed(self, _result=None) -> None:
        # A pairing code lives only while Settings is open.
        self.web_pairing.cancel()
        # Tool and voice changes reach Cora at a connect, so reconnect once now rather than after every change.
        voice_changed, self.voice_changed = self.voice_changed, False
        if (self.service_tools_changed or voice_changed) and self.live.ready and not self.standby and not self.closing:
            what = "the service tools you changed" if self.service_tools_changed else "her new voice"
            self.activity.write(f"Reconnecting so Cora has {what}.", "Settings changed")
            self.action_reconnect()

    @staticmethod
    def key_status() -> str:
        """What Settings shows of the OpenAI key in use, never the key itself."""
        if not openai_key.has_key():
            return "No key yet: Cora can't connect until one is saved."
        where = "saved in Cora" if openai_key.saved_key() else "from .env"
        return f"In use: {where} ({openai_key.masked()})."

    async def check_and_save_openai_key(self, key: str) -> str | None:
        """Check a key with OpenAI and save it: None when saved, else why not (never the key)."""
        problem = await asyncio.to_thread(openai_key.check_key, key)
        if problem is None:
            try:
                await asyncio.to_thread(openai_key.save_key, key)
            except Exception as error:
                self.activity.write(f"OpenAI key save failed ({type(error).__name__}).", "OpenAI key")
                return "The key works but couldn't be saved to the credential store; see Activity."
            self.activity.write(f"OpenAI key saved ({openai_key.masked()}).", "OpenAI key")
        return problem

    @work(group="openai-key", exclusive=True)
    async def save_openai_key(self, key: str) -> None:
        problem = await self.check_and_save_openai_key(key)
        if isinstance(self.screen, ModelSettingsScreen):
            self.screen.show_key_status(problem or await asyncio.to_thread(self.key_status))
        if problem is not None:
            self.notify(problem, severity="error")
            return
        self.notify("Key saved.")
        self.start_with_new_key()

    def api_key_given(self, saved: bool | None) -> None:
        """The first-start pop-up closed: start with the key, or say where to add one later."""
        if saved:
            self.notify("Key saved. Starting Cora.")
            self.start_with_new_key()
        else:
            self.notify("Cora can't start without an OpenAI API key; add one in Settings > General.",
                        severity="warning", timeout=15)

    def start_with_new_key(self) -> None:
        """A new key: connect if Cora never started for want of one, or reconnect if she's disconnected.
        A session already running keeps going; later calls use the new key."""
        if self.closing:
            return
        if self.connection_worker is None:
            self.connection_worker = self.connect_to_cora()
        elif not self.live.ready and not self.standby:
            self.action_reconnect()

    @work(group="model-settings-save")
    async def save_voice_setting(self, voice: str) -> None:
        try:
            await self.conversation_persistence.call_database(self.persistence.save_voice, voice)
        except Exception as error:
            self.activity.write(f"Voice setting save failed ({type(error).__name__}).", "Voice settings error")
            self.notify("Could not save her voice. Check Activity.", severity="error")
            return
        self.activity.write(f"Cora's voice is now {voice}.", "Voice saved")
        # A session keeps the voice it began with; closing Settings reconnects with the new one.
        self.voice_changed = True

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
