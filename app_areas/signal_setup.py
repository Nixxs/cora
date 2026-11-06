"""Cora's Signal account: linking, settings and the people she may talk with: part of CoraApp (see app.py)."""

from dataclasses import replace

from textual import work

from messaging.setup import LINKED as SIGNAL_LINKED
from messaging.signal_cli import signal_failure
from storage.client_store import Client
from storage.signal_settings_store import normalise_number


class SignalSetupMixin:
    """Cora's Signal account: linking, settings and the people she may talk with."""

    @work(group="signal")
    async def start_signal_if_enabled(self) -> None:
        try:
            self.signal_settings = await self.conversation_persistence.call_database(
                self.persistence.load_signal_settings,
            )
        except Exception as error:
            self.activity.write(f"Signal settings load failed ({type(error).__name__}).", "Signal error")
            return
        if self.signal_settings.enabled and not self.closing:
            await self.load_signal_clients()
            await self.signal.start(self.signal_settings.account)
            await self.listen_on_signal()

    async def load_signal_clients(self) -> None:
        try:
            clients = await self.conversation_persistence.call_database(self.persistence.load_clients)
        except Exception as error:
            self.activity.write(f"Signal people load failed ({type(error).__name__}).", "Signal error")
            return
        self.signal_clients = {client.number: client for client in clients}

    @work(group="signal-save")
    async def add_signal_client(self, name: str, number: str) -> None:
        try:
            number = normalise_number(number)
            if number == self.signal_settings.allowed_number:
                raise ValueError("That's your own number: you're the person she works with, not a client")
            if number == self.signal.status.account or number == self.signal_settings.account:
                raise ValueError("That's Cora's own number")
            client = await self.conversation_persistence.call_database(self.persistence.add_client, name, number)
        except ValueError as error:
            self.notify(str(error), severity="warning")
            return
        except Exception as error:
            self.activity.write(f"Adding a Signal person failed ({type(error).__name__}).", "Signal error")
            self.notify("Could not add them. Check Activity.", severity="error")
            return
        self.signal_clients[client.number] = client
        # Someone added after getting the stranger reply is answered from now on.
        self.signal_introduced.discard(client.number)
        self.activity.write(f"{client.name} added: Cora may talk with them on Signal.", "Signal person added")
        # Added either way (they may join later), but say now if messages can't reach them.
        if self.signal.status.state == SIGNAL_LINKED and (problem := await self.signal_contact_unreachable(client)):
            self.notify(f"{client.name}'s number isn't on Signal, or is hidden. Check the number.", severity="warning")
            self.activity.write(problem, "Signal number not found")
        await self.learn_signal_ids()

    @work(group="signal-save")
    async def set_client_may_call(self, number: str, may_call: bool) -> None:
        """Whether this person may reach her by phone (Settings › Signal, their Calls switch)."""
        client = self.signal_clients.get(number)
        if client is None:
            return
        try:
            await self.conversation_persistence.call_database(self.persistence.save_client_may_call, client.id, may_call)
        except Exception as error:
            self.activity.write(f"Saving {client.name}'s calls setting failed ({type(error).__name__}).", "Signal error")
            self.notify("That couldn't be saved. Check Activity.", severity="error")
            return
        self.signal_clients[number] = replace(client, may_call=may_call)
        self.activity.write(f"{client.name} {'may now call Cora' if may_call else 'can no longer call Cora'}.",
                            "Phone calls")

    @work(group="signal-save")
    async def remove_signal_client(self, number: str) -> None:
        client = self.signal_clients.get(number)
        if client is None:
            return
        try:
            await self.conversation_persistence.call_database(self.persistence.remove_client, client.id)
        except Exception as error:
            self.activity.write(f"Removing a Signal person failed ({type(error).__name__}).", "Signal error")
            self.notify("Could not remove them. Check Activity.", severity="error")
            return
        self.signal_clients.pop(number, None)
        self.activity.write(f"{client.name} removed; their past chats are kept.", "Signal person removed")

    @work(group="signal")
    async def set_signal_enabled(self, enabled: bool) -> None:
        try:
            await self.conversation_persistence.call_database(self.persistence.save_signal_enabled, enabled)
        except Exception as error:
            self.activity.write(f"Signal setting save failed ({type(error).__name__}).", "Signal error")
            self.notify("Could not save the Signal setting. Check Activity.", severity="error")
            return
        self.signal_settings = replace(self.signal_settings, enabled=enabled)
        if enabled:
            await self.signal.start(self.signal_settings.account)
        else:
            await self.signal.stop()
            self.activity.write("Signal is off; Cora doesn't use it.", "Signal off")
        await self.listen_on_signal()

    @work(group="signal-save")
    async def save_signal_number(self, number: str) -> None:
        try:
            await self.conversation_persistence.call_database(self.persistence.save_signal_allowed_number, number)
        except ValueError as error:
            self.notify(str(error), severity="warning")
            return
        except Exception as error:
            self.activity.write(f"Your Signal number save failed ({type(error).__name__}).", "Signal error")
            self.notify("Could not save your number. Check Activity.", severity="error")
            return
        if number != self.signal_settings.allowed_number:
            # A new number may be a new account: its ID is learned again.
            self.signal_settings = replace(self.signal_settings, allowed_number=number, allowed_signal_id=None)
        self.activity.write("Your number saved: Cora answers you as herself on Signal.", "Signal number saved")
        await self.learn_signal_ids()

    async def save_signal_account(self, account: str | None) -> None:
        await self.conversation_persistence.call_database(self.persistence.save_signal_account, account)
        self.signal_settings = replace(self.signal_settings, account=account)

    @work(group="signal-link")
    async def link_signal(self) -> None:
        if not self.signal_settings.allowed_number:
            self.notify("Save your own number first.", severity="warning")
            return
        await self.signal.link(self.signal_settings.allowed_number)
        await self.listen_on_signal()

    def find_signal_contact(self, name: str) -> tuple[Client | None, str | None]:
        """Someone the person added, by name (or first name if only one matches), for Cora to message."""
        if self.signal.status.state != SIGNAL_LINKED:
            return None, "Signal isn't set up and linked, so nothing can be sent."
        contacts = list(self.signal_clients.values())
        if not contacts:
            return None, "Nobody has been added for you to message yet (Settings > Services > Signal)."
        wanted = " ".join(name.split()).casefold()
        matches = [contact for contact in contacts if contact.name.casefold() == wanted] or [
            contact for contact in contacts if contact.name.casefold().split()[0] == wanted.split()[0]
        ]
        if len(matches) == 1:
            return matches[0], None
        names = ", ".join(sorted(contact.name for contact in contacts))
        if matches:
            return None, f"More than one contact matches; ask which one: {names}."
        return None, f"There's no contact by that name. You can message: {names}."

    async def signal_contact_unreachable(self, contact: Client) -> str | None:
        """Why Signal can't deliver to them, or None. A failed check doesn't block: the send will say."""
        if contact.signal_id:
            return None  # known by ID, which works even if they hide their number
        try:
            if await self.signal.client.is_registered(self.signal.status.account, contact.number):
                return None
        except Exception:
            return None
        return (
            f"{contact.name}'s number isn't on Signal, or they've set Signal to hide their number, so "
            "nothing can be sent. Ask the person to check the number in Settings, or ask "
            f"{contact.name} to allow being found by number."
        )

    @work(group="signal-accept")
    async def accept_signal_request(self, number: str) -> None:
        if number in self.signal_accepted or not self.signal.status.account:
            return
        self.signal_accepted.add(number)
        try:
            await self.signal.client.accept_request(self.signal.status.account, number)
            self.activity.write("Accepted a Signal message request from someone you added.", "Signal request accepted")
        except Exception as error:
            self.signal_accepted.discard(number)
            self.activity.write(f"Accepting a Signal request failed ({signal_failure(error)}).", "Signal error")

    async def learn_signal_ids(self) -> None:
        """Know the Signal IDs of the person and everyone they added, so their messages are recognised.

        Saved IDs come first: they keep working when someone hides their number. Anyone without one
        is looked up by number, and what's found is saved.
        """
        person = self.signal_settings
        if person.allowed_number and person.allowed_signal_id:
            self.signal_ids[person.allowed_signal_id] = person.allowed_number
        for client in self.signal_clients.values():
            if client.signal_id:
                self.signal_ids[client.signal_id] = client.number
        account = self.signal.status.account
        if self.signal.status.state != SIGNAL_LINKED or not account:
            return
        for number in filter(None, [person.allowed_number, *self.signal_clients]):
            if number in self.signal_ids.values():
                continue
            try:
                signal_id = await self.signal.client.signal_id(account, number)
            except Exception:
                continue  # looked up again next time
            if signal_id:
                await self.remember_signal_id(number, signal_id)

    async def remember_signal_id(self, number: str, signal_id: str) -> None:
        """Save someone's Signal ID, so they're still recognised if they later hide their number."""
        self.signal_ids[signal_id] = number
        try:
            if number == self.signal_settings.allowed_number:
                await self.conversation_persistence.call_database(self.persistence.save_signal_person_id, signal_id)
                self.signal_settings = replace(self.signal_settings, allowed_signal_id=signal_id)
            elif (client := self.signal_clients.get(number)) is not None:
                await self.conversation_persistence.call_database(
                    self.persistence.save_client_signal_id, client.id, signal_id,
                )
                self.signal_clients[number] = replace(client, signal_id=signal_id)
        except Exception as error:
            self.activity.write(f"Saving a Signal ID failed ({type(error).__name__}).", "Signal error")
