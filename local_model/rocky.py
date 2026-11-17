"""Rocky, Cora's consultant and friend: his persona, and his threads.

He's the local model (Settings > Local model), so what he's asked and what he says stay on this PC.
He keeps two threads: what Cora asks him (consult_rocky: only she sees his answer, and the person,
in the chat or from her), and what the person asks him directly ("@rocky …" at the desk or on the
web page: never given to Cora, so it never reaches OpenAI). In each, a follow-up carries on and a
new topic (or a long pause) starts afresh.
"""

import base64
from collections.abc import Callable
import io
import re
from time import monotonic

from PIL import Image

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
                f"frank answer. Their messages start \"{speaker(True, name)}\"; don't start yours with any "
                f"name. {CHARACTER} Talk to {name or 'them'} as a friend.")
    return (f"You are Rocky. Cora, an AI assistant, is your friend, and you're her consultant: she brings you "
            f"questions from {who}, an adult, when they want the frank answer. Her messages start "
            f"\"{speaker(False, name)}\"; don't start yours with any name. {CHARACTER} Talk to Cora as a "
            "friend.")


def speaker(direct: bool, name: str = "") -> str:
    """The label on each message to him: 'Nick:' asking directly, 'Cora (for Nick):' consulting."""
    if direct:
        return f"{name}:" if name else "The person:"
    return f"Cora (for {name}):" if name else "Cora (for the person):"


# The personas without a name, as most tests and the Rocky test runner see them.
PERSONA = persona(False)
DIRECT_PERSONA = persona(True)


def group_persona(name: str = "") -> str:
    """His system prompt in a group chat with the person and Cora: he answers first, she after."""
    who = name or "the person"
    return (f"You are Rocky, in a group chat with {who} and Cora, an AI assistant who works for "
            f"{name or 'them'}; you're her consultant and friend. {who[0].upper()}{who[1:]}'s messages start "
            f"\"{speaker(True, name)}\" and Cora's start \"Cora:\". You answer {who}'s latest message first and "
            "Cora replies after you, so answer it yourself rather than leaving it to her. Write only your own "
            "message, then stop: never write Cora's reply or anyone else's lines, and don't start yours with "
            f"any name. {CHARACTER} Talk to them both as friends.")


class GroupThread:
    """His side of a group chat: what he's sent is the conversation as shown (transcript() gives
    (who, said) oldest first, who being "person", "cora" or "rocky", ending with the question), so
    he sees Cora's replies too. Nothing of its own to keep (add does nothing)."""

    def __init__(self, transcript: Callable[[], list[tuple[str, str]]]) -> None:
        self.transcript = transcript

    def messages(self, question: str, new_topic: bool, name: str = "",
                 images: list[str] | None = None) -> list[dict]:
        """images: sent with the latest message (earlier ones show only as "[image]")."""
        person = speaker(True, name)
        messages = [{"role": "system", "content": group_persona(name)}]
        said = self.transcript() or [("person", question)]
        for who, text in said:
            if who == "rocky":
                messages.append({"role": "assistant", "content": text})
                continue
            line = f"{person if who == 'person' else 'Cora:'} {text}"
            if messages[-1]["role"] == "user":
                messages[-1]["content"] += f"\n\n{line}"
            else:
                messages.append({"role": "user", "content": line})
        if messages[-1]["role"] != "user":
            messages.append({"role": "user", "content": f"{person} {question}"})
        messages[-1] = with_images(messages[-1], images)
        return messages

    def add(self, question: str, answer: str, images: list[str] | None = None) -> None:
        pass


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


# His model sees images at about this size anyway (Gemma 4: 896 pixels a side); smaller is quicker.
IMAGE_SIDE = 1024
# What's asked when an image comes with no words.
IMAGE_QUESTION = "What do you make of this?"


def rocky_image(data: bytes) -> str:
    """An image (any format Pillow reads, such as a pasted PNG or a phone's JPEG) as his model takes
    it: JPEG at most IMAGE_SIDE a side, base64. Raises if it isn't an image. Never saved."""
    with Image.open(io.BytesIO(data)) as image:
        if image.width * image.height > 60_000_000:
            raise ValueError("Image has too many pixels")
        picture = image.convert("RGB")
    picture.thumbnail((IMAGE_SIDE, IMAGE_SIDE))
    buffer = io.BytesIO()
    picture.save(buffer, format="JPEG", quality=85)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def direct_question(text: str) -> str | None:
    """The question in a message to Rocky directly ("@rocky …"), "" for "@rocky" alone, or None
    for any other message."""
    found = DIRECT.match(text.strip())
    return None if found is None else text.strip()[found.end():].strip()


def without_label(text: str, name: str = "") -> str:
    """His reply without a speaker label copied from the messages he's sent ("Nick: …",
    "Rocky: …", "Cora (for Nick): …"). Only those names, so "Short answer: no" stays."""
    names = ["Rocky", "Cora", "The person", "Them"] + ([re.escape(name)] if name else [])
    label = re.compile(rf"^\s*(?:{'|'.join(names)})(?: \([^)\n]*\))?\s*:\s*", re.IGNORECASE)
    return label.sub("", text, count=1)


def speakers(name: str = "") -> list[str]:
    """The names whose turns he must never write: everyone in his messages, himself included."""
    return ["Cora", "Rocky", "The person"] + ([name] if name else [])


def stop_at(name: str = "") -> list[str]:
    """Where Ollama stops him: a new line starting someone's turn, as in a group chat's messages."""
    return [f"\n{who}:" for who in speakers(name)] + [f"\n**{who}:" for who in speakers(name)]


# The line Cora adds under his answers (rocky_browsing.looked_line), which he isn't to write himself.
LOOKED_LINE = re.compile(r"(?m)^[ \t]*[*_]*(?:From memory:|He searched|He read)[^\n]*\n?")


def own_turn(text: str, name: str = "") -> str:
    """His reply alone: cut where he starts writing someone else's turn ("Cora: …" on a line of its
    own, bold or not), without any line of Cora's about what he looked up."""
    names = "|".join(re.escape(who) for who in speakers(name))
    other = re.compile(rf"(?m)^[ \t]*[*_]*(?:{names})[*_]*(?: \([^)\n]*\))?[ \t]*[*_]*:", re.IGNORECASE)
    found = other.search(text, 1)
    if found:
        text = text[:found.start()]
    return LOOKED_LINE.sub("", text)


def shown_so_far(reply: str, name: str = "") -> str:
    """His answer as it streams in, without any thinking (finished blocks, or one still open), a
    copied speaker label, or anyone else's turn."""
    shown = THINKING.sub("", reply)
    return own_turn(without_label(shown.split("<think>", 1)[0], name), name).lstrip()


def answer_text(reply: str, name: str = "") -> str:
    """What he said, without any thinking a reasoning model wrote first, a copied speaker label, or
    anyone else's turn."""
    answer = own_turn(without_label(THINKING.sub("", reply), name), name).strip()
    return answer if len(answer) <= ANSWER_MAX_CHARS else answer[:ANSWER_MAX_CHARS - 1].rstrip() + "…"


class RockyThread:
    """What he's been asked lately (by Cora, or by the person directly) and what he said, for the
    next question's context."""

    def __init__(self, clock: Callable[[], float] = monotonic, direct: bool = False) -> None:
        self.clock = clock
        # Whose thread: the person's own (@rocky), or Cora's for them.
        self.direct = direct
        self.exchanges: list[tuple[str, str]] = []
        # The images sent with each exchange's question (rocky_image's base64), in step with
        # exchanges, so a follow-up about one still has it. Kept in memory only.
        self.pictures: list[list[str]] = []
        self.last_asked = 0.0

    def messages(self, question: str, new_topic: bool, name: str = "",
                 images: list[str] | None = None) -> list[dict]:
        """The conversation to send him: his persona, the thread so far (unless it's a new topic or
        has gone quiet too long), and the question, each labelled with who asked (name: the
        person's, if known), with any images sent alongside."""
        if new_topic or self.clock() - self.last_asked > THREAD_IDLE_SECONDS:
            self.exchanges, self.pictures = [], []
        label = speaker(self.direct, name)
        messages = [{"role": "system", "content": persona(self.direct, name)}]
        for (asked, said), pictures in zip(self.exchanges, self.pictures):
            messages += [with_images({"role": "user", "content": f"{label} {asked}"}, pictures),
                         {"role": "assistant", "content": said}]
        return messages + [with_images({"role": "user", "content": f"{label} {question}"}, images)]

    def add(self, question: str, answer: str, images: list[str] | None = None) -> None:
        self.exchanges = (self.exchanges + [(question, answer)])[-THREAD_EXCHANGES:]
        self.pictures = (self.pictures + [list(images or [])])[-THREAD_EXCHANGES:]
        self.last_asked = self.clock()


def with_images(message: dict, images: list[str] | None) -> dict:
    """A message to him with images (Ollama's "images", base64), if there are any."""
    return {**message, "images": list(images)} if images else message
