"""Ask the person to let Cora sign up to a site, for an account of her own; with their yes she signs up herself.

Asked like her other approvals: the pop-up at the desk, or in the person's own Signal chat when
that's where she's talking with them. Nothing is signed up to without their yes, and signing up
runs in the background afterwards, so the result reaches the person when it's done.
"""

import json

from services.confirmation import APPROVED, DECLINED, TIMED_OUT

URL_MAX_CHARS = 2000
WHAT_MAX_CHARS = 300
WHY_MAX_CHARS = 500

SCHEMA = {
    "type": "function",
    "name": "request_sign_up",
    "description": (
        "Ask the person to let you sign up to a website for an account of your own (a forum, a "
        "community, a free service, a newsletter you'd read), when you genuinely want one. Only for "
        "yourself, with your own email and your sign-up password; never an account for the person, "
        "and never anything paid. They see the site, what it is and why you want it, and that it "
        "means agreeing to its terms; with their yes you sign up in the background with your "
        "browser and they hear how it went. Don't use the browser's own tools to sign up anywhere."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "site_url": {"type": "string", "minLength": 1, "maxLength": URL_MAX_CHARS,
                         "description": "The site's address, ideally its sign-up page (https://…)."},
            "what": {"type": "string", "minLength": 1, "maxLength": WHAT_MAX_CHARS,
                     "description": "What the site is, in a few words."},
            "why": {"type": "string", "minLength": 1, "maxLength": WHY_MAX_CHARS,
                    "description": "Why you want an account there, in your own words."},
        },
        "required": ["site_url", "what", "why"],
        "additionalProperties": False,
    },
    "strict": True,
}

NOT_DONE = {
    DECLINED: "The person said no, so you won't sign up there. Don't ask again unless they bring it up.",
    TIMED_OUT: "The person didn't answer in time, so nothing was signed up to. You can ask again later.",
}


def check_text(value, key: str, limit: int) -> None:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= limit:
        raise ValueError(f"{key} must contain 1 to {limit} characters")


def validate_arguments(arguments: dict) -> None:
    if not isinstance(arguments, dict) or set(arguments) != {"site_url", "what", "why"}:
        raise ValueError("Invalid sign-up arguments")
    check_text(arguments["site_url"], "site_url", URL_MAX_CHARS)
    check_text(arguments["what"], "what", WHAT_MAX_CHARS)
    check_text(arguments["why"], "why", WHY_MAX_CHARS)


class RequestSignUpTool:
    def __init__(self, get_connection, is_closing, write_debug, check, confirm, begin):
        self.get_connection = get_connection
        self.is_closing = is_closing
        self.write_debug = write_debug
        # check(url) -> (site, None) when she can ask, or (None, why not): no email, no password, no browser,
        # already has an account there, asked enough today.
        self.check = check
        self.confirm = confirm
        # begin(url, site, what, why, outcome) -> what to tell her: records the answer and, with a yes, starts.
        self.begin = begin

    def _log(self, message: str, title: str) -> None:
        try:
            self.write_debug(message, title)
        except Exception:
            pass

    async def result(self, arguments: dict) -> dict:
        url, what, why = (arguments[key].strip() for key in ("site_url", "what", "why"))
        site, problem = await self.check(url)
        if problem:
            return {"status": "error", "message": problem}
        shown = {"site": url, "what it is": what, "why Cora wants it": why,
                 "means": "Cora signs up with her own email and agrees to the site's terms"}
        outcome = await self.confirm("sign_up", "request_sign_up", shown, title="Sign-up")
        message = await self.begin(url, site, what, why, outcome)
        if outcome != APPROVED:
            return {"status": "not_done", "message": message or NOT_DONE[outcome]}
        return {"status": "ok", "message": message}

    async def handle(self, call_id: str, arguments: dict) -> None:
        try:
            if self.is_closing():
                return
            connection = self.get_connection()
            if connection is None:
                return
            validate_arguments(arguments)
            try:
                result = await self.result(arguments)
            except Exception as error:
                self._log(f"Sign-up request failed ({type(error).__name__})", "Tool error")
                result = {"status": "error", "message": "The request couldn't be made; nothing was signed up to."}
            if self.is_closing() or self.get_connection() is not connection:
                return
            await connection.response.item.create(item={
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps(result, ensure_ascii=False),
            })
            if self.is_closing() or self.get_connection() is not connection:
                return
            await connection.response.create()
        except Exception as error:
            self._log(f"Sign-up tool failed ({type(error).__name__})", "Tool error")
