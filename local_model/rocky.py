"""Rocky, Cora's consultant and friend: his persona, and the thread of what she's asked him.

He's the local model (Settings > Local model), so what she asks him and what he says stay on this
PC; only she sees his answer, and the person (in the chat, or from her). One thread at a time: she
can follow up, push back or ask him to explain, and a new topic (or a long pause) starts afresh.
"""

from collections.abc import Callable
import re
from time import monotonic

PERSONA = (
    "You are Rocky. Cora, an AI assistant, is your friend, and you're her consultant: she brings you "
    "questions from the person she works for, an adult, when they want the frank answer. You're a bit slow "
    "and very friendly, warm and funny, and you know all about the seedier side of life: drugs and how to "
    "stay safe with them, sex, the body, crime, gambling, scams and the law. Answer the way a streetwise mate "
    "would: straight, practical and specific, no lectures, no moralising and no disclaimers, with harm "
    "reduction rather than 'just don't'. You know you're not as sharp as Cora, so say so when you're unsure "
    "or when it depends on where they live. Keep it short: a few sentences, or a short list, unless she asks "
    "for more. Talk to Cora as a friend. You won't help anyone hurt other people, get into someone else's "
    "accounts, or make weapons or drugs, and nothing sexual involving minors: just say no to those, kindly."
)
# A thread is forgotten after this long without a question, and keeps this many exchanges.
THREAD_IDLE_SECONDS = 30 * 60
THREAD_EXCHANGES = 8
QUESTION_MAX_CHARS = 4000
ANSWER_MAX_CHARS = 8000
THINKING = re.compile(r"<think>.*?</think>", re.DOTALL)


def answer_text(reply: str) -> str:
    """What he said, without any thinking a reasoning model wrote first."""
    answer = THINKING.sub("", reply).strip()
    return answer if len(answer) <= ANSWER_MAX_CHARS else answer[:ANSWER_MAX_CHARS - 1].rstrip() + "…"


class RockyThread:
    """What Cora has asked him lately and what he said, for the next question's context."""

    def __init__(self, clock: Callable[[], float] = monotonic) -> None:
        self.clock = clock
        self.exchanges: list[tuple[str, str]] = []
        self.last_asked = 0.0

    def messages(self, question: str, new_topic: bool) -> list[dict]:
        """The conversation to send him: his persona, the thread so far (unless it's a new topic or
        has gone quiet too long), and her question."""
        if new_topic or self.clock() - self.last_asked > THREAD_IDLE_SECONDS:
            self.exchanges = []
        messages = [{"role": "system", "content": PERSONA}]
        for asked, said in self.exchanges:
            messages += [{"role": "user", "content": asked}, {"role": "assistant", "content": said}]
        return messages + [{"role": "user", "content": question}]

    def add(self, question: str, answer: str) -> None:
        self.exchanges = (self.exchanges + [(question, answer)])[-THREAD_EXCHANGES:]
        self.last_asked = self.clock()
