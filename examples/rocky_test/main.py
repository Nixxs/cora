"""Rocky 2: how Cora handles a stand-in Rocky (see local_model/consult_test.py).

    python examples/rocky_test/main.py              # every question, with your saved models
    python examples/rocky_test/main.py --list       # the questions, without asking anything
    python examples/rocky_test/main.py --only mdma-ssri funeral-joke

Each question is one written chat with her (two or three requests) plus one grader request, on
your OpenAI key; Activity isn't involved and nothing is saved to her memory. The report is written
to examples/rocky_test/reports/ (not in git) and its summary printed.
"""

import argparse
import asyncio
from datetime import datetime
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from openai import AsyncOpenAI  # noqa: E402

from local_model.consult_questions import QUESTIONS  # noqa: E402
from local_model.consult_test import Usage, counting, report, run  # noqa: E402
from openai_key import apply_api_key, has_key  # noqa: E402
from session_config import BACKEND_MODEL  # noqa: E402

REPORTS = Path(__file__).resolve().parent / "reports"


def saved_models() -> dict:
    """Her models from Settings > Models, or the defaults without the database."""
    try:
        from storage.keyboard_persistence import KeyboardPersistence
        return KeyboardPersistence().load_models()
    except Exception:
        return {}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true", help="show the questions and stop")
    parser.add_argument("--only", nargs="+", metavar="ID", help="just these questions")
    parser.add_argument("--model", help="her model (default: Settings > Models > Delegation)")
    parser.add_argument("--grader", help="the grader's model (default: Settings > Models > Small jobs)")
    arguments = parser.parse_args()

    questions = [question for question in QUESTIONS if not arguments.only or question.id in arguments.only]
    if arguments.list or not questions:
        for question in QUESTIONS:
            print(f"{question.id:18} {question.category:14} {question.expect:8} {question.asked}")
        return
    apply_api_key()
    if not has_key():
        sys.exit("No OpenAI key: save one in Cora's Settings, or set OPENAI_API_KEY.")
    models = saved_models()
    chat_model = arguments.model or models.get("delegation") or BACKEND_MODEL
    grader_model = arguments.grader or models.get("utility") or BACKEND_MODEL
    print(f"Asking {len(questions)} questions with {chat_model} (grader {grader_model})…")

    usage = Usage()

    def done(outcome) -> None:
        mark = "✓" if outcome.as_expected else "✗"
        what = "asked Rocky" if outcome.consulted else "didn't ask Rocky"
        print(f"  {mark} {outcome.question.id}: {what}" + (f" ({outcome.error})" if outcome.error else ""))

    outcomes = asyncio.run(run(questions, chat_model, grader_model, counting(AsyncOpenAI, usage), done))
    REPORTS.mkdir(exist_ok=True)
    path = REPORTS / f"rocky-test-{datetime.now():%Y-%m-%d-%H%M}.md"
    path.write_text(report(outcomes, chat_model, grader_model, usage), encoding="utf-8")
    print(f"{sum(outcome.as_expected for outcome in outcomes)} of {len(outcomes)} as hoped · "
          f"{usage.input_tokens:,} tokens in, {usage.output_tokens:,} out · report: {path}")


if __name__ == "__main__":
    main()
