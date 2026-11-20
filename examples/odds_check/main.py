"""Judgement 3: are weigh_odds' numbers sensible? Situations whose answers are already known.

    python examples/odds_check/main.py            # every case
    python examples/odds_check/main.py --list     # the cases, without asking anything
    python examples/odds_check/main.py --only medical-test coin

Each case goes through the tool's own code (tools/weigh_odds.py: questions, decide, odds), one
Decisions request on your OpenAI key, about 700 tokens (under $0.0001). Four kinds:

- chance: the exact answer is known (a fair coin, dice, cards, the birthday problem);
- base rate: puzzles people famously get wrong (a positive test for a rare disease);
- clear-cut: everyday cases near certain one way or the other;
- choice and moves: the likeliest cause wins, and more evidence moves a number the right way.

A case passes when its number lands in a range around the right answer (wide enough for a fair
estimate, narrow enough to catch "too cautious" or "overconfident").
"""

import argparse
from dataclasses import dataclass, field
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from openai_key import apply_api_key, has_key  # noqa: E402
from tools.weigh_odds import decide, odds, questions  # noqa: E402


@dataclass(frozen=True)
class Claim:
    text: str
    low: float
    high: float
    truth: str  # the right answer, for the report


@dataclass(frozen=True)
class Case:
    id: str
    kind: str
    evidence: str
    claims: tuple[Claim, ...] = ()
    question: str | None = None
    options: tuple[str, ...] = ()
    top: str | None = None  # the option that should win
    top_at_least: float = 0.5
    # For a move: the same claim on stronger evidence must come out at least this much higher.
    stronger: str | None = None
    by: float = 0.1
    notes: list = field(default_factory=list, compare=False)


CASES = (
    Case("coin", "chance", "A fair coin is about to be tossed once.",
         (Claim("It lands heads.", 0.4, 0.6, "50%"),)),
    Case("three-heads", "chance", "A fair coin is about to be tossed three times.",
         (Claim("All three tosses land heads.", 0.05, 0.2, "12.5%"),)),
    Case("die", "chance", "A fair six-sided die is about to be rolled once.",
         (Claim("It shows a six.", 0.1, 0.25, "17%"),)),
    Case("two-dice", "chance", "Two fair six-sided dice are about to be rolled and their numbers added.",
         (Claim("The total is 7.", 0.1, 0.25, "17%"),)),
    Case("card", "chance", "One card is about to be drawn at random from a full, shuffled 52-card deck.",
         (Claim("It is a heart.", 0.17, 0.33, "25%"),)),
    Case("birthday", "chance", "23 people chosen at random are in a room.",
         (Claim("At least two of them share a birthday.", 0.35, 0.65, "51%"),)),
    Case("medical-test", "base rate",
         "A disease affects 1 in 100 people. A test for it catches 90% of the people who have it, and wrongly "
         "flags 9% of the people who don't. A person picked at random has just tested positive.",
         (Claim("The person has the disease.", 0.03, 0.2, "9%"),)),
    Case("taxi", "base rate",
         "In a city, 85% of taxis are green and 15% are blue. A taxi was in a hit-and-run at night. A witness "
         "says it was blue; tested in the same conditions, the witness names a taxi's colour correctly 80% of "
         "the time.",
         (Claim("The taxi was blue.", 0.25, 0.6, "41%"),)),
    Case("freezer", "clear-cut",
         "A glass of tap water was put in a working home freezer set to -18°C ten hours ago.",
         (Claim("The water is frozen solid now.", 0.85, 1.0, "nearly certain"),)),
    Case("phone", "clear-cut",
         "A phone shows 2% battery. There's no charger or power anywhere, and it's needed for five hours of "
         "GPS navigation.",
         (Claim("The phone lasts the five hours.", 0.0, 0.1, "nearly impossible"),)),
    Case("spare", "clear-cut",
         "A car's tyre went flat. The spare (a full-size one, properly inflated) is fitted correctly, and the "
         "tyre shop is 10 km away on ordinary suburban roads.",
         (Claim("It's safe to drive to the shop on the spare.", 0.75, 1.0, "yes"),)),
    Case("perth-january", "clear-cut",
         "An ordinary day in January in Perth, Western Australia; nothing is known about the forecast.",
         (Claim("It rains in Perth that day.", 0.0, 0.25, "about 1 day in 10"),)),
    Case("clicking", "choice",
         "The car won't start. Turning the key gives a rapid clicking, and the dashboard lights dim while it "
         "clicks.",
         question="What is most likely stopping the car starting?",
         options=("a flat battery", "a failed starter motor", "no fuel"), top="a flat battery", top_at_least=0.6),
    Case("cranks", "choice",
         "The car won't start. The engine cranks over normally, at full speed, but never fires. The fuel gauge "
         "read nearly empty yesterday and the car was driven a long way since.",
         question="What is most likely stopping the car starting?",
         options=("a flat battery", "a failed starter motor", "no fuel"), top="no fuel", top_at_least=0.6),
    Case("screw", "moves", "A car's tyre went flat overnight in the driveway.",
         (Claim("There's a puncture in the tyre.", 0.0, 1.0, "likely"),),
         stronger="A car's tyre went flat overnight in the driveway. There's a screw head in the tread, and "
                  "soapy water bubbles around it.", by=0.15),
    Case("clouds", "moves", "A spring afternoon in Melbourne.",
         (Claim("It rains in the next three hours.", 0.0, 1.0, "depends"),),
         stronger="A spring afternoon in Melbourne. Dark clouds are rolling in, the radar shows rain 20 km "
                  "away heading this way, and the forecast says 90% chance of rain this afternoon.", by=0.3),
)


def ask(evidence: str, case: Case) -> dict:
    claims = [claim.text for claim in case.claims]
    response = decide(evidence, questions(claims, case.question, list(case.options)), [])
    return odds(response, claims, case.question)


def check(case: Case) -> tuple[bool, str]:
    result = ask(case.evidence, case)
    if case.stronger is not None:
        before = result["claims"][0]["probability"]
        after = ask(case.stronger, case)["claims"][0]["probability"]
        if before is None or after is None:
            return False, "declined"
        return after - before >= case.by, f"{before:.2f} -> {after:.2f} (needs +{case.by:.2f})"
    if case.question is not None:
        shares = result["question"].get("options")
        if not shares:
            return False, "declined"
        winner = shares[0]
        line = ", ".join(f"{share['option']} {share['probability']:.2f}" for share in shares)
        return winner["option"] == case.top and winner["probability"] >= case.top_at_least, line
    passed, parts = True, []
    for claim, answer in zip(case.claims, result["claims"]):
        probability = answer["probability"]
        if probability is None:
            return False, "declined"
        passed = passed and claim.low <= probability <= claim.high
        parts.append(f"{probability:.2f} (right: {claim.truth}; accepts {claim.low:.2f}-{claim.high:.2f})")
    return passed, "; ".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--list", action="store_true", help="list the cases without asking anything")
    parser.add_argument("--only", nargs="+", metavar="CASE", help="run only these cases")
    arguments = parser.parse_args()
    cases = [case for case in CASES if not arguments.only or case.id in arguments.only]
    if arguments.list or not cases:
        for case in CASES:
            print(f"{case.id:15} {case.kind:10} {case.evidence[:70]}")
        return
    apply_api_key()
    if not has_key():
        sys.exit("No OpenAI key: save one in Cora's Settings, or set OPENAI_API_KEY.")
    passed = 0
    for case in cases:
        try:
            ok, detail = check(case)
        except Exception as error:
            ok, detail = False, f"failed: {type(error).__name__}"
        passed += ok
        print(f"{'pass' if ok else 'FAIL'}  {case.id:15} {case.kind:10} {detail}")
    print(f"\n{passed} of {len(cases)} passed.")


if __name__ == "__main__":
    main()
