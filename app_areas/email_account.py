"""Cora's mailbox: setup and sign-in: part of CoraApp (see app.py)."""

import asyncio

from textual import work

from mail.account import (
    FAILED as EMAIL_FAILED, NO_APP as EMAIL_NO_APP, READY as EMAIL_READY, SIGNED_OUT as EMAIL_SIGNED_OUT,
    SIGNING_IN as EMAIL_SIGNING_IN, EmailNotReady, EmailStatus, GraphError,
)


class EmailAccountMixin:
    """Cora's mailbox: setup and sign-in."""

    @work(group="email-check", exclusive=True, exit_on_error=False)
    async def check_email_account(self) -> None:
        """Whether her mailbox can be reached: at launch (so a lapsed sign-in shows) and after setting it up."""
        try:
            if await asyncio.to_thread(self.email_account.client_id) is None:
                self.email_status = EmailStatus(EMAIL_NO_APP)
                return
            if not await asyncio.to_thread(self.email_account.signed_in):
                self.email_status = EmailStatus(EMAIL_SIGNED_OUT)
                return
            address = await self.mailbox.address()
            self.email_person = await asyncio.to_thread(self.email_account.person_address)
            self.email_signature = await asyncio.to_thread(self.email_account.signature)
        except EmailNotReady as error:
            self.email_status = EmailStatus(EMAIL_SIGNED_OUT, reason=str(error))
            self.activity.write(str(error), "Email not ready")
            return
        except Exception as error:
            # Microsoft's own error code helps set things up; anything else, the type only.
            detail = str(error) if isinstance(error, GraphError) else f"({type(error).__name__})."
            reason = f"Cora's email couldn't be reached: {detail}"
            self.email_status = EmailStatus(EMAIL_FAILED, reason=reason)
            self.activity.write(reason, "Email error")
            return
        self.email_status = EmailStatus(EMAIL_READY, address=address)
        self.activity.write(f"Cora's email is ready: {address}", "Email ready")
        self.refresh_calendar()
        self.check_task_mail()
        self.ensure_mail_subscription()
        self.ensure_calendar_subscription()

    @work(group="email-setup", exit_on_error=False)
    async def save_email_app(self, client_id: str, tenant: str) -> None:
        """The app ID (kept if left blank) and her organisation's directory (blank: a personal account)."""
        try:
            if client_id.strip():
                await asyncio.to_thread(self.email_account.save_client_id, client_id)
            tenant = await asyncio.to_thread(self.email_account.save_tenant, tenant)
        except ValueError as error:
            self.notify(str(error), severity="warning")
            return
        except Exception as error:
            self.activity.write(f"Saving Cora's email app ID failed ({type(error).__name__}).", "Email error")
            self.notify("The app ID couldn't be saved. Check Activity.", severity="error")
            return
        if self.email_controls is not None:
            self.email_controls.has_client_id = True
            self.email_controls.tenant = tenant
        self.activity.write(
            f"Saved Cora's email setup ({f'work account at {tenant}' if tenant else 'personal account'}).", "Email",
        )
        self.check_email_account()

    @work(group="email-setup", exit_on_error=False)
    async def save_email_person(self, address: str) -> None:
        """The person's own address: verified mail from it is treated like their Signal chat. Empty forgets it."""
        try:
            address = await asyncio.to_thread(self.email_account.save_person_address, address)
        except ValueError as error:
            self.notify(str(error), severity="warning")
            return
        except Exception as error:
            self.activity.write(f"Saving your email address failed ({type(error).__name__}).", "Email error")
            self.notify("Your address couldn't be saved. Check Activity.", severity="error")
            return
        self.email_person = address
        if self.email_controls is not None:
            self.email_controls.person_address = address
        self.activity.write(
            f"Saved your email address: {address}." if address else "Forgot your email address.", "Email",
        )

    @work(group="email-setup", exit_on_error=False)
    async def save_email_signature(self, signature: str) -> None:
        """Her signature, added below each new email she sends (not replies). Empty forgets it."""
        try:
            signature = await asyncio.to_thread(self.email_account.save_signature, signature)
        except ValueError as error:
            self.notify(str(error), severity="warning")
            return
        except Exception as error:
            self.activity.write(f"Saving Cora's email signature failed ({type(error).__name__}).", "Email error")
            self.notify("The signature couldn't be saved. Check Activity.", severity="error")
            return
        self.email_signature = signature
        if self.email_controls is not None:
            self.email_controls.signature = signature
        self.activity.write("Saved Cora's email signature." if signature else "Forgot Cora's email signature.", "Email")

    @work(group="email-sign-in", exclusive=True, exit_on_error=False)
    async def sign_in_email(self) -> None:
        """Shows a code in Settings to enter at Microsoft's sign-in page, then waits for it to be used."""
        try:
            flow = await asyncio.to_thread(self.email_account.start_sign_in)
        except EmailNotReady as error:
            self.email_status = EmailStatus(EMAIL_FAILED, reason=str(error))
            return
        except Exception as error:
            self.email_status = EmailStatus(EMAIL_FAILED, reason=f"Starting the sign-in failed ({type(error).__name__}).")
            return
        self.email_status = EmailStatus(EMAIL_SIGNING_IN, code=flow["user_code"], link=flow["verification_uri"])
        try:
            who = await asyncio.to_thread(self.email_account.finish_sign_in, flow)
        except EmailNotReady as error:
            if not self.closing:
                self.email_status = EmailStatus(EMAIL_SIGNED_OUT, reason=str(error))
            return
        except Exception as error:
            self.email_status = EmailStatus(EMAIL_FAILED, reason=f"Signing in failed ({type(error).__name__}).")
            return
        if self.closing:
            return
        self.activity.write(f"Cora's email signed in{f' as {who}' if who else ''}.", "Email")
        self.check_email_account()

    @work(group="email-setup", exit_on_error=False)
    async def sign_out_email(self) -> None:
        # While she can still reach Microsoft: nothing should call her listener for a mailbox she's left.
        await self.drop_mail_subscription()
        await self.drop_calendar_subscription()
        try:
            await asyncio.to_thread(self.email_account.sign_out)
        except Exception as error:
            self.activity.write(f"Signing Cora out of her email failed ({type(error).__name__}).", "Email error")
            return
        self.email_status = EmailStatus(EMAIL_SIGNED_OUT)
        self.live.calendar_context = ""
        self.activity.write("Signed Cora out of her email.", "Email")
