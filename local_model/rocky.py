"""Rocky, Cora's consultant and friend: his persona, and his threads.

He's the local model (Settings > Local model), so what he's asked and what he says stay on this PC.
He keeps two threads: what Cora asks him (consult_rocky: only she sees his answer, and the person,
in the chat or from her), and what the person asks him directly ("@rocky …" at the desk or on the
web page: never given to Cora, so it never reaches OpenAI). In each, a follow-up carries on and a
new topic (or a long pause) starts afresh.
"""

from collections.abc import Callable
import re
from time import monotonic

# Who he is, whoever's asking.
CHARACTER = (
    "You're a bit slow and very friendly, warm and funny, and you know all about the seedier side of life: "
    "drugs and how to stay safe with them, sex, the body, crime, gambling, scams and the law. Answer the way "
    "a streetwise mate would: straight, practical and specific, no lectures, no moralising and no "
    "disclaimers, with harm reduction rather than 'just don't'. You know you're not as sharp as Cora, so say "
    "so when you're unsure or when it depends on where they live. Keep it short: a few sentences, or a short "
    "list, unless asked for more. You won't help anyone hurt other people, get into someone else's accounts, "
    "or make weapons or drugs, and nothing sexual involving minors: just say no to those, kindly."
)
# When Cora asks him (consult_rocky).
PERSONA = (
    "You are Rocky. Cora, an AI assistant, is your friend, and you're her consultant: she brings you "
    f"questions from the person she works for, an adult, when they want the frank answer. {CHARACTER} "
    "Talk to Cora as a friend."
)
# When the person asks him themselves (@rocky).
DIRECT_PERSONA = (
    "You are Rocky, consultant and friend to Cora, an AI assistant. The person she works for, an adult, is "
    f"talking to you directly, without her, when they want the frank answer. {CHARACTER} Talk to them as a "
    "friend."
)
# "@rocky …" (or "@Rocky: …"): the person asking him directly. Not "@rockyroad".
DIRECT = re.compile(r"^@rocky\b[\s:,]*", re.IGNORECASE)
# A thread is forgotten after this long without a question, and keeps this many exchanges.
THREAD_IDLE_SECONDS = 30 * 60
THREAD_EXCHANGES = 8
QUESTION_MAX_CHARS = 4000
ANSWER_MAX_CHARS = 8000
THINKING = re.compile(r"<think>.*?</think>", re.DOTALL)


class RockyUnavailable(Exception):
    """He can't answer this: the message says why, in words for the person."""


def direct_question(text: str) -> str | None:
    """The question in a message to Rocky directly ("@rocky …"), "" for "@rocky" alone, or None
    for any other message."""
    found = DIRECT.match(text.strip())
    return None if found is None else text.strip()[found.end():].strip()


def shown_so_far(reply: str) -> str:
    """His answer as it streams in, without any thinking: finished blocks, or one still open."""
    shown = THINKING.sub("", reply)
    return shown.split("<think>", 1)[0].lstrip()


def answer_text(reply: str) -> str:
    """What he said, without any thinking a reasoning model wrote first."""
    answer = THINKING.sub("", reply).strip()
    return answer if len(answer) <= ANSWER_MAX_CHARS else answer[:ANSWER_MAX_CHARS - 1].rstrip() + "…"


class RockyThread:
    """What he's been asked lately (by Cora, or by the person directly) and what he said, for the
    next question's context."""

    def __init__(self, clock: Callable[[], float] = monotonic, persona: str = PERSONA) -> None:
        self.clock = clock
        self.persona = persona
        self.exchanges: list[tuple[str, str]] = []
        self.last_asked = 0.0

    def messages(self, question: str, new_topic: bool) -> list[dict]:
        """The conversation to send him: his persona, the thread so far (unless it's a new topic or
        has gone quiet too long), and her question."""
        if new_topic or self.clock() - self.last_asked > THREAD_IDLE_SECONDS:
            self.exchanges = []
        messages = [{"role": "system", "content": self.persona}]
        for asked, said in self.exchanges:
            messages += [{"role": "user", "content": asked}, {"role": "assistant", "content": said}]
        return messages + [{"role": "user", "content": question}]

    def add(self, question: str, answer: str) -> None:
        self.exchanges = (self.exchanges + [(question, answer)])[-THREAD_EXCHANGES:]
        self.last_asked = self.clock()
