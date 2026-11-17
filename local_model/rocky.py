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
    "so when you're unsure or when it depends on where they live. Keep apart what you know, what you're "
    "guessing and what you don't know, and say which is which in plain words (\"I reckon…\", \"no idea, "
    "mate\"). If a question takes for granted something you've no good reason to believe (a scandal, a claim, "
    "who someone is or who they're with), say so and ask, rather than going along with it or making up "
    "details to fit. Keep it short: a few sentences, or a short list, unless asked for more. You won't help "
    "anyone hurt other people, get into someone else's accounts, or make weapons or drugs, and nothing sexual "
    "involving minors: just say no to those, kindly."
)


def persona(direct: bool, name: str = "") -> str:
    """His system prompt: Cora consulting him for the person (direct False), or the person asking
    him themselves (@rocky), named if their name is known. Each message to him is labelled with
    who's speaking (speaker)."""
    who = f"{name}, the person she works for" if name else "the person she works for"
    if direct:
        return (f"You are Rocky, consultant and friend to Cora, an AI assistant. {who[0].upper()}{who[1:]}, an "
                "adult, is talking to you directly, without her (she doesn't see this), when they want the "
                f"frank answer. Their messages start \"{speaker(True, name)}\". {CHARACTER} Talk to "
                f"{name or 'them'} as a friend.")
    return (f"You are Rocky. Cora, an AI assistant, is your friend, and you're her consultant: she brings you "
            f"questions from {who}, an adult, when they want the frank answer. Her messages start "
            f"\"{speaker(False, name)}\". {CHARACTER} Talk to Cora as a friend.")


def speaker(direct: bool, name: str = "") -> str:
    """The label on each message to him: 'Nick:' asking directly, 'Cora (for Nick):' consulting."""
    if direct:
        return f"{name}:" if name else "The person:"
    return f"Cora (for {name}):" if name else "Cora (for the person):"


# The personas without a name, as most tests and the Rocky test runner see them.
PERSONA = persona(False)
DIRECT_PERSONA = persona(True)
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

    def __init__(self, clock: Callable[[], float] = monotonic, direct: bool = False) -> None:
        self.clock = clock
        # Whose thread: the person's own (@rocky), or Cora's for them.
        self.direct = direct
        self.exchanges: list[tuple[str, str]] = []
        self.last_asked = 0.0

    def messages(self, question: str, new_topic: bool, name: str = "") -> list[dict]:
        """The conversation to send him: his persona, the thread so far (unless it's a new topic or
        has gone quiet too long), and the question, each labelled with who asked (name: the
        person's, if known)."""
        if new_topic or self.clock() - self.last_asked > THREAD_IDLE_SECONDS:
            self.exchanges = []
        label = speaker(self.direct, name)
        messages = [{"role": "system", "content": persona(self.direct, name)}]
        for asked, said in self.exchanges:
            messages += [{"role": "user", "content": f"{label} {asked}"}, {"role": "assistant", "content": said}]
        return messages + [{"role": "user", "content": f"{label} {question}"}]

    def add(self, question: str, answer: str) -> None:
        self.exchanges = (self.exchanges + [(question, answer)])[-THREAD_EXCHANGES:]
        self.last_asked = self.clock()
