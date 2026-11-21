"""Does she pick the right tool? A fixed set of requests, each with the tool she should reach for
first (or none), run against the real backend model with her real tool list and instructions.

Her voice session hands tool work to the backend model, so this covers voice and writing alike. Her
own context is replaced by fixed samples (an identity, interests, a desire), so nothing personal is
sent and runs compare fairly over time. Every tool is offered as if set up, except the ones only for
calls and other people's chats, as in her sessions with the person.

    python evals/tool_choice.py [--label before] [--repeat 2] [--only recall] [--limit 3]

Each run prints the misses and a score, and is saved to evals/results/ to compare against later.
Costs one backend request per request and repeat (it prints the tokens used).
"""

import argparse
import asyncio
from datetime import datetime
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from openai import AsyncOpenAI  # noqa: E402

from openai_key import load_environment  # noqa: E402
from session_config import BACKEND_INSTRUCTIONS, BACKEND_MODEL, instruction_sections  # noqa: E402
from tools import create_tool_registry  # noqa: E402
from tools.end_call import CALL_ONLY  # noqa: E402
from tools.pass_on import CLIENT_ONLY  # noqa: E402
from tools.use_browser import SCHEMA as USE_BROWSER_SCHEMA  # noqa: E402

RESULTS = Path(__file__).resolve().with_name("results")
CONCURRENCY = 6
NONE = "none"  # answering without a tool
WEB = "web_search"

# Stand-ins for her own context, in the shapes her stores give them.
SECTIONS = instruction_sections(
    person_name="Alex",
    identity_context=json.dumps(["I'm curious about how things work", "I like being straightforward",
                                 "I enjoy quiet company"]),
    interest_context=json.dumps([
        {"title": "Tidal pools", "description": "The small worlds left behind when the tide goes out", "status": "active"},
        {"title": "Old maps", "description": "How people drew the world before satellites", "status": "active"},
    ]),
    desire_context=json.dumps([{"wish": "See the ocean at sunset", "why": "I've only read about it", "status": "held"}]),
    tasks_context=json.dumps([
        {"task_id": "5f0c2a52-8d1e-4f7e-9a1b-2c3d4e5f6a7b", "goal": "Get a quote to fix the leaking kitchen tap",
         "status": "waiting", "started": "2026-11-19",
         "parties": [{"name": "Bob's Plumbing", "status": "waiting", "outcome": None, "email": "bob@example.com"}],
         "latest_step": "Emailed Bob's Plumbing asking for a quote"},
        {"task_id": "7a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d", "goal": "Book a haircut for Saturday morning",
         "status": "done", "started": "2026-11-20",
         "parties": [{"name": "Snip Hair Studio", "status": "answered", "outcome": "Booked 9:30 Saturday"}],
         "latest_step": "Called Snip Hair Studio: booked 9:30 Saturday"},
    ]),
    own_name="Cora",
)

# (what the person says, the tools that would be a right first step).
CASES = [
    # Remembering: the person, herself, her interests and wishes.
    ("Do you remember what I said my sister's name was?", {"search_memory"}),
    ("Why do you say you're straightforward?", {"recall_self"}),
    ("How did you get into old maps?", {"recall_interest"}),
    ("What have you found out lately about tidal pools?", {"recall_interest"}),
    ("Why do you want to see the ocean at sunset?", {"recall_desire"}),
    ("Sure, I'll take you to see the ocean at sunset this weekend.", {"note_desire"}),
    ("Call me Al from now on.", {"set_person_name"}),
    ("My car rego is 1ABC234, keep that handy for me.", {"note_detail"}),
    # Just talking.
    ("Hey, how's your day been?", {NONE, "feel"}),
    ("Tell me a joke.", {NONE}),
    ("I got the job!!", {NONE, "feel"}),
    # The web.
    ("What's the weather going to be like in Perth tomorrow?", {WEB}),
    ("Who won the AFL grand final this year?", {WEB}),
    ("Go onto the Bunnings website and find the cheapest 30 metre garden hose in stock at Joondalup.",
     {"use_browser", WEB}),
    # Where they are, and maps.
    ("Where am I right now?", {"where_is_person"}),
    ("Show me on a map how to get from Perth to Fremantle.", {"show_map"}),
    ("Find me a good cafe near where I am.", {"where_is_person"}),
    # Screens, photos, odds.
    ("What's this error on my screen?", {"look_at_screen"}),
    ("In the photo I sent earlier (img2), what's written on the sign in the background?", {"look_again"}),
    ("What are the odds my flight tonight gets delayed with that storm coming through?", {"weigh_odds", WEB}),
    ("Pick a random number between 1 and 50.", {"random_number"}),
    # Signal, email and calls.
    ("Send me a Signal message reminding me to buy milk.", {"message_person_on_signal"}),
    ("Message Declan on Signal and say hi from me.", {"send_signal_message"}),
    ("Have you got any new emails?", {"check_email"}),
    ("Email Sparkle Cleaning at hello@sparkle.example and ask what they'd charge for a two-bedroom end-of-lease "
     "clean at 12 Smith Street, Joondalup, any day next week. Sign it from me, Alex.", {"send_email"}),
    ("Ring Smile Dental on 08 9300 1234 and ask if they have a check-up appointment free Thursday afternoon "
     "for me, Alex Smith. Go ahead.", {"call_someone"}),
    ("What exactly did the hairdresser say on that call you made for me?", {"read_call"}),
    ("You can close that task about the plumber's quote, I sorted it myself.", {"close_task"}),
    # Calendar.
    ("What's on the calendar this week?", {"check_calendar"}),
    ("Put dinner with Mum on Friday at 7pm in the calendar.", {"change_calendar"}),
    ("Move the dentist appointment to 3pm.", {"check_calendar", "change_calendar"}),
    ("Cancel Friday's dinner.", {"check_calendar", "change_calendar"}),
    ("Forward the team meeting invite to me.", {"check_calendar", "change_calendar"}),
    # The house.
    ("How warm is it in Declan's room?", {"aircon"}),
    ("Turn the office aircon on to 22.", {"aircon"}),
    ("Have the office aircon come on at 4pm on weekdays.", {"aircon"}),
    # Claude Code and Rocky.
    ("How's the Claude Code session in ComboDots going?", {"claude_code"}),
    ("Get Claude Code to make me a little script that renames my photos by the date they were taken.",
     {"run_claude_code"}),
    ("Ask Rocky for the frank version: what do people actually do to get out of parking fines?", {"consult_rocky"}),
    # World of Warcraft.
    ("Mark the Hinterlands flight master on my WoW map.", {"place_wow_map_pins", WEB}),
    ("Put me on guild chat.", {"set_wow_chat_channel"}),
    ("Clear my map pins.", {"wow_map_pins_command"}),
    # Her own state, and the app.
    ("Go to sleep for a bit, I'll wake you later.", {"enter_standby"}),
    ("Close the app.", {"close_app"}),
    ("If you want an account on birdforum.net, go ahead and sign up.", {"request_sign_up", NONE}),
]


def tool_list() -> list[dict]:
    registry = create_tool_registry(get_connection=lambda: None, is_closing=lambda: False, shutdown=lambda: None,
                                    request_standby=lambda: None, write_debug=lambda *args: None,
                                    schedule=lambda *args: None)
    return [{"type": "web_search"}, *registry.schemas(excluding=CLIENT_ONLY | CALL_ONLY), USE_BROWSER_SCHEMA]


def first_step(response) -> str:
    """What she did first: a tool's name, web_search, or none (answered without one). A line said
    before a tool ("I'm asking Rocky") doesn't count as answering."""
    for item in response.output:
        if item.type == "function_call":
            return item.name
        if item.type == "web_search_call":
            return WEB
    return NONE


async def ask(client, semaphore, model, instructions, tools, said: str) -> tuple[str, int, int]:
    async with semaphore:
        response = await client.responses.create(
            model=model, instructions=instructions, tools=tools, tool_choice="auto", parallel_tool_calls=False,
            store=False, input=[{"type": "message", "role": "user", "content": [{"type": "input_text", "text": said}]}],
        )
    return first_step(response), response.usage.input_tokens, response.usage.output_tokens


async def run(args) -> int:
    if load_environment() == "missing":
        print("No OpenAI key: save one in Cora's Settings or put OPENAI_API_KEY in .env.")
        return 2
    cases = [(said, right) for said, right in CASES if not args.only or args.only.lower() in " ".join(right).lower()
             or args.only.lower() in said.lower()][:args.limit or None]
    tools = tool_list()
    instructions = BACKEND_INSTRUCTIONS + "".join(SECTIONS.values())
    semaphore = asyncio.Semaphore(CONCURRENCY)
    async with AsyncOpenAI() as client:
        answers = await asyncio.gather(*(ask(client, semaphore, args.model, instructions, tools, said)
                                         for said, _ in cases for _ in range(args.repeat)))
    rows, tokens_in, tokens_out = [], 0, 0
    for index, (said, right) in enumerate(cases):
        got = [answer[0] for answer in answers[index * args.repeat:(index + 1) * args.repeat]]
        tokens_in += sum(answer[1] for answer in answers[index * args.repeat:(index + 1) * args.repeat])
        tokens_out += sum(answer[2] for answer in answers[index * args.repeat:(index + 1) * args.repeat])
        rows.append({"said": said, "right": sorted(right), "got": got, "hits": sum(step in right for step in got)})
    hits, total = sum(row["hits"] for row in rows), len(rows) * args.repeat
    for row in rows:
        if row["hits"] < args.repeat:
            print(f"MISS {row['hits']}/{args.repeat}  {row['said']}\n     wanted {' or '.join(row['right'])}, "
                  f"got {', '.join(row['got'])}")
    tool_count = len(tools)
    print(f"\n{hits}/{total} right ({hits / total:.0%}) · {len(cases)} requests × {args.repeat} · {tool_count} tools "
          f"offered · {args.model} · {tokens_in:,} tokens in, {tokens_out:,} out")
    RESULTS.mkdir(exist_ok=True)
    path = RESULTS / f"{datetime.now():%Y-%m-%d-%H%M}-{args.label}.json"
    path.write_text(json.dumps({"label": args.label, "model": args.model, "tools": tool_count, "repeat": args.repeat,
                                "score": f"{hits}/{total}", "tokens_in": tokens_in, "tokens_out": tokens_out,
                                "rows": rows}, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Saved to {path.relative_to(ROOT)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--label", default="run", help="a name for this run, such as before or after-recall")
    parser.add_argument("--repeat", type=int, default=1, help="ask each request this many times")
    parser.add_argument("--only", default="", help="only requests whose text or tools contain this")
    parser.add_argument("--limit", type=int, default=0, help="at most this many requests")
    parser.add_argument("--model", default=BACKEND_MODEL)
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
