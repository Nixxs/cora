"""Setting up Cora's own Signal account: the container, linking her phone, and her status.

It is Cora's account, on her own phone and number. Linking makes this app another device
on whichever account scans the code, so scanning with the person's own phone would make
Cora them; a link to the person's number is undone straight away.
"""

import asyncio
from dataclasses import dataclass

OFF, STARTING, NOT_LINKED, LINKING, LINKED, FAILED = (
    "off", "starting", "not linked", "linking", "linked", "failed",
)
DEVICE_NAME = "Cora"
# How long to wait for the daemon to answer after starting, and for a phone to scan the code.
READY_SECONDS = 90
LINK_SECONDS = 300


@dataclass(frozen=True)
class SignalStatus:
    state: str = OFF
    account: str | None = None
    link_uri: str | None = None
    reason: str | None = None


def error_text(error: Exception) -> str:
    message = str(error).strip()
    return f"{type(error).__name__}: {message[:200]}" if message else type(error).__name__


class SignalSetup:
    def __init__(self, client, start_container, stop_container, save_account, write_activity,
                 ready_seconds: float = READY_SECONDS, link_seconds: float = LINK_SECONDS,
                 poll_seconds: float = 1) -> None:
        self.client = client
        self.start_container = start_container
        self.stop_container = stop_container
        # Saves Cora's number, or None when it is no longer linked.
        self.save_account = save_account
        self.write_activity = write_activity
        self.ready_seconds = ready_seconds
        self.link_seconds = link_seconds
        self.poll_seconds = poll_seconds
        self.status = SignalStatus()
        # Bumped by every start and stop, so a slow step that finishes later can't undo a newer one.
        self.run = 0

    def set_status(self, run: int, status: SignalStatus) -> bool:
        if run != self.run:
            return False
        self.status = status
        return True

    async def start(self, account: str | None) -> None:
        """Start the container and find out whether Cora's saved account is still linked there."""
        self.run += 1
        run = self.run
        self.status = SignalStatus(STARTING)
        try:
            await self.start_container()
            await self.wait_ready()
            linked = await self.client.accounts()
        except Exception as error:
            if self.set_status(run, SignalStatus(FAILED, reason=error_text(error))):
                self.write_activity(f"Signal failed to start ({error_text(error)}).", "Signal failed")
            return
        if account and account in linked:
            if self.set_status(run, SignalStatus(LINKED, account=account)):
                self.write_activity(f"Signal ready as Cora's number {account}.", "Signal ready")
            return
        if account:
            # Unlinked from her phone, or the container's data was lost: set up again.
            await self.save_account(None)
            self.write_activity("Cora's Signal account is no longer linked; link her phone again.", "Signal unlinked")
        self.set_status(run, SignalStatus(NOT_LINKED))

    async def wait_ready(self) -> None:
        deadline = asyncio.get_running_loop().time() + self.ready_seconds
        while not await self.client.check():
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("signal-cli did not answer")
            await asyncio.sleep(self.poll_seconds)

    async def stop(self) -> None:
        self.run += 1
        run = self.run
        self.status = SignalStatus(OFF)
        try:
            await self.stop_container()
        except Exception as error:
            self.write_activity(f"Signal container stop failed ({error_text(error)}).", "Signal error")
        self.set_status(run, SignalStatus(OFF))

    async def link(self, person_number: str) -> None:
        """Show a code for Cora's phone to scan, and wait for it."""
        if self.status.state != NOT_LINKED:
            return
        run = self.run
        try:
            uri = await self.client.start_link()
        except Exception as error:
            self.set_status(run, SignalStatus(NOT_LINKED, reason=f"Couldn't start linking ({error_text(error)})."))
            return
        if not self.set_status(run, SignalStatus(LINKING, link_uri=uri)):
            return
        try:
            number = await self.client.finish_link(uri, DEVICE_NAME, timeout=self.link_seconds)
        except Exception:
            self.set_status(run, SignalStatus(NOT_LINKED, reason="The code wasn't scanned in time; try again."))
            return
        if number == person_number:
            # This is the person's own account: undo it, so Cora never holds their messages.
            try:
                await self.client.delete_account(number)
            except Exception as error:
                self.write_activity(f"Removing the wrong link failed ({error_text(error)}).", "Signal error")
            self.write_activity(
                "That code was scanned with your own phone, so it linked your account; it was removed. "
                "Also remove 'Cora' from your phone's Linked devices.", "Wrong phone linked",
            )
            self.set_status(run, SignalStatus(NOT_LINKED, reason=(
                "That was your own phone, so it was undone. Scan with Cora's phone, and remove "
                "'Cora' from Linked devices on yours."
            )))
            return
        await self.save_account(number)
        if self.set_status(run, SignalStatus(LINKED, account=number)):
            self.write_activity(f"Signal linked as Cora's number {number}.", "Signal linked")
