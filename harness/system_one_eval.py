"""Score a system-one backend offline: `python -m harness.system_one_eval`.

The examples file is JSON lines. Each line is an object with `state`, a mapping of field name to
text, and `label`, the answer the LLM gave for that state. The tool asks the backend the role's
question for a sample of them and reports, per confidence threshold, how much the backend would
cover, how often it agrees with the label, and how often it says the costly answer where the
label does not. It reports and stops. It writes nothing.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from harness.runners import _decider, _Unavailable, build_runner
from runner.anthropic_runner import load_provider_profile
from runner.protocol import RunnerError
from runner.system_one import Choice, ConfigError, Noul, Question, parse_system_one_block
from runner.system_one_specs import question_for

__all__ = ["main", "score"]

THRESHOLDS = (0.5, 0.7, 0.8, 0.9, 0.95)

# The fast answer that costs something when the LLM disagreed. Mirrors tools stats_system_one.COSTLY_ANSWER.
COSTLY_ANSWER = {"handoff": "yes", "review_charter": "approve"}

Row = tuple[str | None, float, str]


def _share(part: int, whole: int) -> float:
    return part / whole if whole else 0.0


def score(rows: Sequence[Row], costly_answer: str, thresholds: Sequence[float] = THRESHOLDS) -> list[dict[str, Any]]:
    """One dict per threshold over `(predicted, confidence, label)` rows. `predicted` None means no answer.

    A row is covered when its confidence is at least the threshold. A costly row is a covered row
    that predicts `costly_answer` where the label differs. Both recalls are over every row whose label
    is not `costly_answer`: `minority_recall` counts a correct answer at any confidence, and
    `minority_recall_covered` counts only a correct answer the threshold covers.
    """
    minority = [(p, c, label) for p, c, label in rows if label != costly_answer]
    recall = _share(sum(p == label for p, _, label in minority), len(minority))

    def at(threshold: float) -> dict[str, Any]:
        covered = [(p, label) for p, c, label in rows if p is not None and c >= threshold]
        costly = sum(p == costly_answer and label != costly_answer for p, label in covered)
        minority_hits = sum(p == label and c >= threshold for p, c, label in minority)
        return {
            "threshold": threshold,
            "covered": len(covered),
            "coverage": _share(len(covered), len(rows)),
            "agreement": _share(sum(p == label for p, label in covered), len(covered)),
            "costly": costly,
            "costly_rate": _share(costly, len(covered)),
            "minority_recall": recall,
            "minority_recall_covered": _share(minority_hits, len(minority)),
        }

    return [at(t) for t in thresholds]


def _offered(question: Question) -> tuple[str, ...]:
    if isinstance(question, Noul):
        return ("yes", "no")
    return question.options if isinstance(question, Choice) else question.levels


def _example(path: Path, number: int, line: str) -> dict[str, Any]:
    try:
        example = json.loads(line)
    except json.JSONDecodeError as error:
        raise ValueError(f"{path}: example {number} is not JSON: {error}") from error
    state = example.get("state") if isinstance(example, Mapping) else None
    if not isinstance(state, Mapping) or not isinstance(example.get("label"), str):
        raise ValueError(f"{path}: example {number} needs a `state` mapping and a `label` string")
    return example


def _read_examples(path: Path) -> list[dict[str, Any]]:
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [_example(path, number, line) for number, line in enumerate(lines, 1)]


def _ask(decider: Any, question: Question, example: Mapping[str, Any]) -> tuple[Row, str | None]:
    """The backend's `(answer, confidence, label)` and no error, or an uncovered row and the error it raised."""
    try:
        answer = decider.decide(question, example["state"])
    except Exception as error:
        return (None, 0.0, example["label"]), f"{type(error).__name__}: {error}"
    return (answer.value, answer.confidence, example["label"]), None


def _table(results: Sequence[Mapping[str, Any]]) -> str:
    """The per-threshold table; no thresholds is a header and no rows."""
    head = (
        f"{'threshold':>9} {'covered':>7} {'coverage':>8} {'agreement':>9} {'costly':>6} {'costly_rate':>11}"
        f" {'minority_covered':>16}"
    )
    body = [
        f"{r['threshold']:>9} {r['covered']:>7} {r['coverage']:>8.3f} {r['agreement']:>9.3f}"
        f" {r['costly']:>6} {r['costly_rate']:>11.3f} {r['minority_recall_covered']:>16.3f}"
        for r in results
    ]
    tail = [f"minority_recall: {results[0]['minority_recall']:.3f}"] if results else []
    return "\n".join([head, *body, *tail])


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m harness.system_one_eval", description=__doc__.split("\n\n")[0])
    parser.add_argument("--provider-profile", required=True, metavar="PATH")
    parser.add_argument("--backend", required=True, metavar="NAME")
    parser.add_argument("--model", required=True, metavar="LABEL")
    parser.add_argument("--tier", default="cheap")
    parser.add_argument("--role", required=True, choices=sorted(COSTLY_ANSWER))
    parser.add_argument("--examples", required=True, metavar="FILE")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def _fail(message: str) -> int:
    print(f"system_one_eval: {message}", file=sys.stderr)
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    question = question_for(args.role)
    try:
        examples = _read_examples(Path(args.examples))
    except (OSError, ValueError) as error:
        return _fail(str(error))
    offered = _offered(question)
    eligible = [e for e in examples if e["label"] in offered]
    sample = random.Random(args.seed).sample(eligible, min(max(args.limit, 0), len(eligible)))

    try:
        profile = load_provider_profile(args.provider_profile)
        # A hosted backend names its key variable in the profile's own block; keep that, replace the rest.
        prior = profile.get("system_one")
        block = {
            **({"key_env": prior["key_env"]} if isinstance(prior, Mapping) and "key_env" in prior else {}),
            "backend": args.backend,
            "model": args.model,
            "tier": args.tier,
            "roles": {args.role: {"mode": "shadow", "threshold": 0.5}},
        }
        config = parse_system_one_block(block)
        if isinstance(config, ConfigError):
            return _fail(f"system_one: {config.error}")
        real = build_runner(scripted=None, provider_profile=args.provider_profile)
        decider = _decider(config, {**profile, "system_one": block}, real)
    except (RunnerError, _Unavailable, ValueError) as error:
        return _fail(str(error))
    except Exception as error:
        # A plugin factory or a lazily imported SDK can raise anything; setup failing is a message, not a traceback.
        return _fail(f"cannot build the backend: {type(error).__name__}: {error}")

    asked = [_ask(decider, question, e) for e in sample]
    rows = [row for row, _ in asked]
    errors = [error for _, error in asked if error is not None]
    results = score(rows, COSTLY_ANSWER[args.role])
    labels = dict(sorted(Counter(e["label"] for e in sample).items()))
    if args.as_json:
        payload = {"results": results, "labels": labels, "examples": len(examples), "errors": len(errors)}
        print(json.dumps({**payload, "first_error": errors[0] if errors else None}))
    else:
        print(_table(results))
        print("labels: " + " ".join(f"{label}={n}" for label, n in labels.items()))
        print(f"examples: {len(examples)} ({len(eligible)} eligible, {len(sample)} sampled)")
        print(f"errors: {len(errors)} of {len(sample)} sampled rows raised and count as uncovered")
    if errors:
        print(f"system_one_eval: {len(errors)} decider errors; first: {errors[0]}", file=sys.stderr)
    # Every row failing is a broken run, not a measurement of the backend.
    return 1 if sample and len(errors) == len(sample) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
