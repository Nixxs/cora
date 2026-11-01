"""Signal chats: receiving, answering you and her clients: part of CoraApp (see app.py)."""

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

from textual import work

from messaging.container import clear_attachments, delete_attachments
from messaging.images import read_images
from messaging.messages import (
    APPROVAL_SECONDS, SIGNAL_SORRY, approval_answer, approval_request_text, signal_content,
    signal_message_text,
)
from messaging.receiver import WITHHELD_IMAGE, IncomingMessage, SignalReceiver
from messaging.setup import LINKED as SIGNAL_LINKED
from messaging.signal_cli import signal_failure
from storage.client_store import Client
from services.confirmation import APPROVED, DECLINED, TIMED_OUT
from messaging.commitment import check_commitment
from backend_chat import SIGNAL_EXCLUDED, BackendRoute
from tools.pass_on import CLIENT_ONLY
from session_config import (
    BACKEND_MODEL, SIGNAL_CHAT_INSTRUCTIONS, client_chat_instructions, instruction_sections, stranger_reply,
)


# The most concerns she can raise at once about one client in a day; later chats still get a heads-up.
SIGNAL_CONCERNS_PER_DAY = 3


class SignalChatsMixin:
    """Signal chats: receiving, answering you and her clients."""

    async def listen_on_signal(self) -> None:
        """Learn known people's Signal IDs first: messages that waited arrive the moment listening starts."""
        await self.learn_signal_ids()
        if self.signal.status.state == SIGNAL_LINKED and (self.signal_receiver is None or self.signal_receiver.is_finished):
            # Nothing is being answered yet, so any attachment still stored is left over: none are kept.
            try:
                await clear_attachments()
            except Exception as error:
                self.activity.write(f"Clearing stored Signal attachments failed ({type(error).__name__}).", "Signal error")
        self.sync_signal_receiver()

    def sync_signal_receiver(self) -> None:
        """Listen while Cora's Signal account is linked, and not otherwise."""
        linked = self.signal.status.state == SIGNAL_LINKED and not self.closing
        running = self.signal_receiver is not None and not self.signal_receiver.is_finished
        account = self.signal.status.account
        if running and (not linked or self.signal_listening_as != account):
            self.signal_receiver.cancel()
            self.signal_receiver = None
            running = False
        if linked and not running:
            self.signal_listening_as = account
            receiver = SignalReceiver(self.signal.client, account, self.on_signal_message, self.activity.write)
            self.signal_listener = receiver
            # The method, not a coroutine: nothing is left un-awaited if it's cancelled before it starts.
            self.signal_receiver = self.run_worker(receiver.run, group="signal-receive", exit_on_error=False)
            self.activity.write("Listening for your Signal messages to Cora.", "Signal listening")

    def stop_signal_receiver(self) -> None:
        if self.signal_receiver is not None:
            self.signal_receiver.cancel()
            self.signal_receiver = None

    def on_signal_message(self, message: IncomingMessage) -> None:
        self.signal_last_message_at = asyncio.get_running_loop().time()
        # A first message may carry only their Signal ID until the request is accepted; known people's IDs are looked up.
        sender = message.sender or self.signal_ids.get(message.sender_id or "", "")
        known = sender and (sender == self.signal_settings.allowed_number or sender in self.signal_clients)
        if not known:
            # A stranger's attachments are never opened, and not kept either.
            self.discard_signal_attachments([message])
            self.answer_stranger(sender or message.sender_id)
            return
        if not message.sender:
            # Someone she knows wrote first: accept their request so her replies show normally.
            message = replace(message, sender=sender)
            self.accept_signal_request(sender)
        # She's waiting for the person's answer to a request: only their own next message answers it.
        pending = self.signal_approvals.get(sender)
        if pending is not None and not pending.done():
            answer = approval_answer(message.text)
            pending.set_result(answer is True)
            if answer is not None:
                self.discard_signal_attachments([message])
                self.conversation.show_signal_message(signal_message_text(message))
                return
            # Not a clear yes or no: the request is declined, and this is answered as a message.
        elif message.sender_id and message.sender_id not in self.signal_ids:
            # Number and ID together: saved, for when they hide their number later.
            self.run_worker(self.remember_signal_id(sender, message.sender_id), group="signal-ids")
        # One queue per sender: each is answered in order, and different people at the same time.
        self.signal_waiting.setdefault(sender, []).append(message)
        worker = self.signal_answering.get(sender)
        if worker is None or worker.is_finished:
            self.signal_answering[sender] = self.run_worker(
                self.answer_signal(sender), group="signal-chat", exit_on_error=False,
            )

    async def answer_signal(self, sender: str) -> None:
        """Answer one sender's messages in order; ones that arrive meanwhile are answered together next."""
        while self.signal_waiting.get(sender) and not self.closing:
            batch = self.signal_waiting.pop(sender)
            account = self.signal.status.account
            if not account:
                self.discard_signal_attachments(batch)
                return
            client = self.signal_clients.get(sender)
            if sender == self.signal_settings.allowed_number:
                batch = await self.read_signal_images(account, self.signal_settings.person_recipient, batch)
                await self.answer_primary_on_signal(account, self.signal_settings.person_recipient, batch)
            elif client is not None:
                batch = await self.read_signal_images(account, client.recipient, batch)
                await self.answer_client_on_signal(account, client, batch)
            else:
                self.discard_signal_attachments(batch)

    async def read_signal_images(self, account: str, recipient: str, batch: list[IncomingMessage]) -> list[IncomingMessage]:
        """Images from people she knows are read like pasted ones; then every attachment is deleted from signal-cli."""
        if not any(message.attachments for message in batch):
            return batch
        try:
            await self.signal.client.send_typing(account, recipient)
        except Exception:
            pass  # only a courtesy, while the images are read
        read = []
        try:
            for message in batch:
                seen, failed = await read_images(message, lambda attachment_id: self.signal.client.attachment(account, attachment_id))
                withheld = seen.count(WITHHELD_IMAGE)
                if len(seen) > withheld:
                    self.activity.write(f"Read {len(seen) - withheld} Signal image(s) for Cora.", "Image read")
                if withheld:
                    self.activity.write(f"Withheld {withheld} explicit, gory or abusive Signal image(s) from Cora.", "Image withheld")
                if failed:
                    # Not why: errors may echo the request.
                    self.activity.write(f"{failed} Signal image(s) couldn't be read; noted as not opened.", "Image look failed")
                read.append(replace(message, images_seen=seen))
        finally:
            await self.delete_signal_attachments(batch)
        return read

    def discard_signal_attachments(self, messages: list[IncomingMessage]) -> None:
        if any(message.attachments for message in messages):
            self.run_worker(self.delete_signal_attachments(messages), group="signal-attachments", exit_on_error=False)

    async def delete_signal_attachments(self, messages: list[IncomingMessage]) -> None:
        ids = [attachment.id for message in messages for attachment in message.attachments]
        if not ids:
            return
        try:
            await delete_attachments(ids)
        except Exception as error:
            # Cleared at the next launch instead.
            self.activity.write(f"Deleting Signal attachments failed ({type(error).__name__}).", "Signal error")

    async def send_signal_reply(self, account: str, recipient: str, write, review=None) -> str | None:
        """Show typing, write a reply and send it; on failure send a short apology and return None.

        review(text) -> what to send instead, such as a holding line for a reply the person didn't approve.
        """
        try:
            await self.signal.client.send_typing(account, recipient)
        except Exception:
            pass  # only a courtesy
        try:
            text = await write()
            if not text:
                raise ValueError("empty reply")
            if review is not None:
                text = await review(text)
            await self.signal.client.send(account, recipient, text)
            return text
        except Exception as error:
            self.activity.write(f"Signal reply failed ({signal_failure(error)}).", "Signal reply failed")
            try:
                await self.signal.client.send(account, recipient, SIGNAL_SORRY)
            except Exception:
                pass
            return None

    async def answer_primary_on_signal(self, account: str, person: str, batch: list[IncomingMessage]) -> None:
        """The person's own messages: the same conversation as the desktop, shown in its chat."""
        # Taken before these messages join the chat, so they aren't sent twice.
        history = self.conversation.get_reconnect_history()
        for message in batch:
            self.conversation.show_signal_message(signal_message_text(message))
        self.keep_conversation_open()
        self.activity.write(f"Answering {len(batch)} Signal message(s) from you.", "Signal reply")
        # Tools that change things are asked about in this chat, answered only by the person's reply.
        route = BackendRoute(
            excluded=SIGNAL_EXCLUDED,
            ask_approval=self.signal_approval_asker(account, person, self.signal_settings.allowed_number),
            approval_seconds=APPROVAL_SECONDS, tool_seconds=APPROVAL_SECONDS + 60,
        )
        text = await self.send_signal_reply(account, person, lambda: self.reply_in_writing(
            signal_content(batch, datetime.now(timezone.utc)), history, route, SIGNAL_CHAT_INSTRUCTIONS,
        ))
        if text is None or self.closing:
            return
        self.conversation.show_written_reply(text, label="Cora · Signal", channel="signal")
        self.activity.write(f"Signal reply sent: characters={len(text)}", "Signal reply sent")
        self.person_is_talking()
        said = "\n".join(signal_message_text(message) for message in batch)
        if self.pending_greeting == "launch":
            # Answered before she says hello, so the hello can follow on from it.
            self.signal_launch_exchanges.append((said, text))
        try:
            await self.live.note_signal_exchange(said, text)
        except Exception as error:
            self.activity.write(f"Signal note to voice Cora failed ({type(error).__name__}).", "Signal note failed")

    def signal_approval_asker(self, account: str, recipient: str, number: str, lapsed: list | None = None):
        """Ask the person in their Signal chat; the answer is their own next message (see on_signal_message).

        One request waits at a time, from their chat or a task's coordinator, so a YES answers the one
        they were shown. A request still unanswered when its time runs out is added to lapsed.
        """
        async def ask(request) -> bool:
            try:
                async with self.signal_approval_lock:
                    future = asyncio.get_running_loop().create_future()
                    self.signal_approvals[number] = future
                    try:
                        text = approval_request_text(request)
                        await self.signal.client.send(account, recipient, text)
                        self.conversation.show_written_reply(text, label="Cora · Signal", channel="signal")
                        return await future
                    finally:
                        if self.signal_approvals.get(number) is future:
                            del self.signal_approvals[number]
            except asyncio.CancelledError:
                if lapsed is not None:
                    lapsed.append(request)
                raise
        return ask

    async def answer_client_on_signal(self, account: str, client: Client, batch: list[IncomingMessage]) -> None:
        """Someone the person added: their own conversation, kept out of the person's chat."""
        try:
            conversation_id = await self.conversation_persistence.call_database(
                self.persistence.client_conversation, client.id,
            )
            history = await self.conversation_persistence.call_database(
                self.persistence.load_client_history, conversation_id,
            )
            for message in batch:
                await self.conversation_persistence.call_database(
                    self.persistence.save_client_message, conversation_id, "user", signal_message_text(message),
                )
        except Exception as error:
            self.activity.write(f"Saving {client.name}'s Signal message failed ({type(error).__name__}).",
                                "Signal reply failed")
            return
        try:
            await self.conversation_persistence.call_database(self.persistence.task_party_heard, client.id)
        except Exception as error:
            self.activity.write(f"Noting {client.name}'s reply on their tasks failed ({type(error).__name__}).", "Task")
        if conversation_id in self.signal_stopped:
            self.activity.write(f"Not answering {client.name}: Cora ended this chat.", "Signal client")
            return
        self.activity.write(f"Answering {client.name} on Signal.", "Signal client")
        text = await self.send_signal_reply(
            account, client.recipient, lambda: self.reply_to_client(client, batch, history),
            review=lambda text: self.review_client_reply(client, batch, text),
        )
        if text is None or self.closing:
            return
        try:
            await self.conversation_persistence.call_database(
                self.persistence.save_client_message, conversation_id, "assistant", text,
            )
        except Exception as error:
            self.activity.write(f"Saving the reply to {client.name} failed ({type(error).__name__}).", "Signal error")
        self.activity.write(f"Answered {client.name} on Signal.", "Signal client")

    async def review_client_reply(self, client: Client, batch: list[IncomingMessage], text: str) -> str:
        """A reply to a contact that would commit the person waits for their YES (T5)."""
        try:
            briefs = await self.conversation_persistence.call_database(self.persistence.load_task_briefs, client.id)
        except Exception:
            briefs = ""
        said = "\n".join(signal_message_text(message) for message in batch)
        reply, outcome, what = await self.hold_commitment(client.name, text, briefs, said)
        if outcome == TIMED_OUT:
            try:
                await self.tell_person(
                    f"I didn't hear back, so I didn't send {client.name} my reply that {what}; I told them "
                    "I'd check with you. What would you like me to tell them?"
                )
            except Exception:
                pass  # Activity has it
        return reply

    async def hold_commitment(self, who: str, text: str, brief: str, their_message: str,
                              sign_off: str = "") -> tuple[str, str | None, str]:
        """(what to send, the person's answer or None if nothing needed asking, what it commits them to).

        As written unless it commits the person; then only their YES sends it, and otherwise the
        contact gets a holding line.
        """
        try:
            models = await self.conversation_persistence.call_database(self.persistence.load_models)
            model = models["utility"]
        except Exception:
            model = BACKEND_MODEL
        check = await asyncio.to_thread(check_commitment, text, brief=brief, their_message=their_message, model=model)
        if not check.commits:
            return text, None, ""
        self.activity.write(f"A reply to {who} would commit you; asking you first.", "Commitment")
        outcome = await self.ask_person_approval(
            f"a reply to {who}", "that commits you", {"to": who, "commits you": check.what, "reply": text},
        )
        if outcome == APPROVED:
            return text, outcome, check.what
        name = self.live.person_name
        holding = f"Let me check with {name} and come back to you." if name else "Let me check and come back to you."
        self.activity.write(f"Held back a reply to {who} ({outcome}); sent a holding line.", "Commitment")
        return holding + sign_off, outcome, check.what

    async def ask_person_approval(self, title: str, tool: str, shown: dict) -> str:
        """The person's YES for something in the background: on Signal when it's linked, else the pop-up."""
        account, person = self.signal.status.account, self.signal_settings.person_recipient
        if self.signal.status.state != SIGNAL_LINKED or not account or not person:
            return await self.confirmations.ask("tasks", tool, shown, title=title)
        ask = self.signal_approval_asker(account, person, self.signal_settings.allowed_number)
        try:
            approved = await asyncio.wait_for(ask(SimpleNamespace(title=title, tool=tool, arguments=shown)),
                                              APPROVAL_SECONDS)
        except TimeoutError:
            return TIMED_OUT
        return APPROVED if approved else DECLINED

    async def reply_to_client(self, client: Client, batch: list[IncomingMessage], history: list[dict]) -> str:
        """Professional mode: her own identity and interests and what she remembers of them, none of the person's."""
        return await self.backend_chat.reply(
            content=signal_content(batch, datetime.now(timezone.utc)), history=history,
            sections=await self.client_sections(client), model=self.live.delegation_model,
            # Web search, memory search limited to what they may know, and passing things on to the
            # person; no services, nothing at the desk.
            route=BackendRoute(
                excluded=frozenset(self.tools.names()) - {"search_memory", *CLIENT_ONLY},
                memory_scope=(client.id, client.name),
            ),
            extra_instructions=client_chat_instructions(client.name, self.live.person_name, self.live.own_name),
        )

    async def client_sections(self, client: Client) -> dict[str, str]:
        """A client's chat context: her own self, what she remembers of them, her view of them and their briefs."""
        await self.conversation_persistence.load_session_context()
        try:
            # What she remembers from their earlier chats: theirs only, never the person's.
            remembered = await self.conversation_persistence.call_database(
                self.persistence.load_client_memory_context, client.id, client.name,
            )
        except Exception as error:
            self.activity.write(f"{client.name}'s memories didn't load ({type(error).__name__}).", "Signal client")
            remembered = ""
        try:
            # Her view of them, and only the names and one-line opinions of everyone else.
            people = await self.conversation_persistence.call_database(
                self.persistence.load_client_people_context, client.id,
            )
        except Exception as error:
            self.activity.write(f"Who {client.name} is didn't load ({type(error).__name__}).", "Signal client")
            people = ""
        try:
            # Only what she's asking them about, from each task's brief for them.
            briefs = await self.conversation_persistence.call_database(self.persistence.load_task_briefs, client.id)
        except Exception as error:
            self.activity.write(f"{client.name}'s task briefs didn't load ({type(error).__name__}).", "Signal client")
            briefs = ""
        return instruction_sections(
            remembered, interest_context=self.live.interest_context, identity_context=self.live.identity_context,
            self_image_context=self.live.self_image_context, client_people_context=people, task_briefs=briefs,
        )

    async def raise_client_concern(self, client_id, client_name: str, concern: str) -> str | None:
        """From a client's chat, at once: her own concern about them, not their words."""
        key = (client_id, datetime.now().date())
        if self.signal_concerns.get(key, 0) >= SIGNAL_CONCERNS_PER_DAY:
            return "You've already raised this with the person today; they'll also get your heads-up after the chat."
        try:
            await self.tell_person(f"Heads up about {client_name}, in our chat just now: {concern}")
        except Exception:
            return "The person can't be reached right now; they'll get your heads-up after the chat."
        self.signal_concerns[key] = self.signal_concerns.get(key, 0) + 1
        self.activity.write(f"Cora raised a concern about {client_name}.", "Heads-up")
        return None

    async def stop_replying_to_client(self, client_id, client_name: str) -> str | None:
        """She ends this chat: their further messages are saved but not answered until a new chat starts."""
        try:
            conversation_id = await self.conversation_persistence.call_database(
                self.persistence.client_conversation, client_id,
            )
        except Exception as error:
            self.activity.write(f"Ending the chat with {client_name} failed ({type(error).__name__}).", "Signal client")
            return "It couldn't be done just now."
        self.signal_stopped.add(conversation_id)
        self.activity.write(f"Cora stopped replying to {client_name} for the rest of this chat.", "Signal client")
        return None

    async def send_to_signal_contact(self, contact: Client, text: str) -> None:
        """Send from Cora's number and save it in that person's conversation, so she sees it when they reply."""
        account = self.signal.status.account
        if not account:
            raise RuntimeError("Signal is not linked")
        await self.signal.client.send(account, contact.recipient, text)
        try:
            conversation_id = await self.conversation_persistence.call_database(
                self.persistence.client_conversation, contact.id,
            )
            await self.conversation_persistence.call_database(
                self.persistence.save_client_message, conversation_id, "assistant", text,
            )
        except Exception as error:
            # Sent all the same; only her record of it is missing.
            self.activity.write(f"Saving the message to {contact.name} failed ({type(error).__name__}).", "Signal error")

    @work(group="signal-stranger")
    async def answer_stranger(self, recipient: str) -> None:
        """Someone nobody added gets one polite reply per run, and never a conversation.

        `recipient` is their number, or their Signal ID when Signal hasn't shared the number. Their
        message request is left unaccepted.
        """
        if not recipient or recipient in self.signal_introduced or not self.signal.status.account:
            return
        self.signal_introduced.add(recipient)
        self.activity.write("A Signal message from someone not added got the polite reply.", "Signal stranger")
        try:
            await self.signal.client.send(self.signal.status.account, recipient, stranger_reply(self.live.person_name))
        except Exception as error:
            self.activity.write(f"Signal reply failed ({signal_failure(error)}).", "Signal reply failed")
