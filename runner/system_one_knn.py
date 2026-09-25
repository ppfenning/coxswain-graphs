"""Local k-nearest-neighbour backend for the system-one fast path.

The examples file is JSON lines, one object per line with two keys: `state`, a mapping of
field name to text, and `label`, the non-empty string answer recorded for that state.
Blank lines are skipped. This is the contract the coxswain-tools extractor writes.

The core is pure: cosine similarity, a similarity-weighted vote over the k nearest, and a
confidence equal to the winning label's share. The embedder is injected. The default edge
factory imports sentence-transformers lazily, so this module imports without it.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from math import sqrt
from pathlib import Path

from runner.system_one import Answer, Choice, Noul, Question

__all__ = [
    "DEFAULT_EMBEDDING_MODEL",
    "Example",
    "KnnDecider",
    "UnsupportedQuestion",
    "cosine",
    "load_examples",
    "load_knn_decider",
    "neighbours",
    "pick",
    "sentence_transformer_embedder",
    "vote",
]

DEFAULT_EMBEDDING_MODEL = "all-MiniLM-L6-v2"
DEVICES = ("cpu", "cuda")

Vector = Sequence[float]
Embedder = Callable[[list[str]], list[Vector]]


class UnsupportedQuestion(TypeError):
    """The decider cannot answer this question kind. The fast path falls back to the LLM node."""


@dataclass(frozen=True)
class Example:
    state: Mapping[str, str]
    label: str


def _example(number: int, line: str) -> Example:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as error:
        raise ValueError(f"examples line {number}: not JSON ({error.msg})") from error
    if not isinstance(obj, dict):
        raise ValueError(f"examples line {number}: expected an object with state and label")
    state, label = obj.get("state"), obj.get("label")
    if not isinstance(state, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in state.items()):
        raise ValueError(f"examples line {number}: state must be a mapping of str to str")
    if not isinstance(label, str) or not label:
        raise ValueError(f"examples line {number}: label must be a non-empty string")
    return Example(state=state, label=label)


def load_examples(lines: Iterable[str]) -> list[Example]:
    """Parse the examples file. A bad line raises ValueError naming its 1-based number."""
    return [_example(number, line) for number, line in enumerate(lines, start=1) if line.strip()]


def cosine(a: Vector, b: Vector) -> float:
    """Cosine similarity; 0.0 when either vector has no length."""
    norm = sqrt(sum(x * x for x in a)) * sqrt(sum(y * y for y in b))
    return sum(x * y for x, y in zip(a, b)) / norm if norm else 0.0


def neighbours(query: Vector, vectors: Sequence[Vector], labels: Sequence[str], k: int) -> list[tuple[str, float]]:
    """The k most similar (label, similarity) pairs. Equal similarity keeps example order."""
    scored = [(label, cosine(query, vector)) for label, vector in zip(labels, vectors)]
    return sorted(scored, key=lambda pair: -pair[1])[:k]


def vote(pairs: Sequence[tuple[str, float]], order: Sequence[str]) -> dict[str, float]:
    """Probability per label in `order`, weighted by similarity below 0 clipped to 0. Equal weights when all are 0."""
    weights = [(label, max(similarity, 0.0)) for label, similarity in pairs]
    weights = weights if sum(w for _, w in weights) > 0 else [(label, 1.0) for label, _ in weights]
    total = sum(w for _, w in weights)
    return {label: sum(w for name, w in weights if name == label) / total for label in order}


def pick(probabilities: Mapping[str, float], order: Sequence[str]) -> str:
    """The most probable label. A tie goes to the label earlier in `order`."""
    return max(order, key=lambda label: (probabilities[label], -order.index(label)))


def _state_text(state: Mapping[str, str]) -> str:
    return "\n".join(f"{name}: {text}" for name, text in state.items())


_YES_NO = ("yes", "no")


class KnnDecider:
    """A DecisionRunner that votes among the k examples nearest the state. Examples are embedded once, here."""

    def __init__(self, examples: Sequence[Example], embed: Embedder, k: int) -> None:
        if not examples:
            raise ValueError("knn needs at least one example")
        if isinstance(k, bool) or not isinstance(k, int) or k < 1:
            raise ValueError(f"k must be an integer of at least 1, got {k!r}")
        self._embed = embed
        self._k = k
        self._labels = tuple(example.label for example in examples)
        self._vectors = tuple(tuple(v) for v in embed([_state_text(example.state) for example in examples]))

    def decide(self, question: Question, state: Mapping[str, str]) -> Answer:
        if isinstance(question, Noul):
            kind, order, keep = "noul", list(_YES_NO), set(_YES_NO)
        elif isinstance(question, Choice):
            kind, order, keep = "choice", list(question.options), set(question.options)
        else:
            raise UnsupportedQuestion(f"knn does not answer {type(question).__name__} questions")
        # A question is answered only from labels it offers (Noul offers yes and no); others are dropped before the k nearest.
        rows = [(label, vector) for label, vector in zip(self._labels, self._vectors) if label in keep]
        if not rows:
            raise UnsupportedQuestion("no example carries a label this question offers")
        query = self._embed([_state_text(state)])[0]
        pairs = neighbours(query, [v for _, v in rows], [label for label, _ in rows], self._k)
        probabilities = vote(pairs, order)
        value = pick(probabilities, order)
        return Answer(kind=kind, value=value, probabilities=probabilities, confidence=probabilities[value])


def sentence_transformer_embedder(model_name: str = DEFAULT_EMBEDDING_MODEL, device: str = "cpu") -> Embedder:
    """The default embedder, on `cpu` or `cuda`. sentence-transformers is imported here, not at module load."""
    if device not in DEVICES:
        raise ValueError(f"device must be one of {', '.join(DEVICES)}, got {device!r}")
    try:
        import sentence_transformers  # optional extra: system-one-local
    except ImportError as error:
        raise ImportError("sentence-transformers is not installed; install the system-one-local extra") from error
    model = sentence_transformers.SentenceTransformer(model_name, device=device)
    return lambda texts: [list(map(float, row)) for row in model.encode(texts)]


def load_knn_decider(
    path: str | Path,
    k: int,
    embed: Embedder | None = None,
    device: str = "cpu",
    model_name: str = DEFAULT_EMBEDDING_MODEL,
) -> KnnDecider:
    """The edge: read the examples file and build a decider over it."""
    examples = load_examples(Path(path).read_text(encoding="utf-8").splitlines())
    return KnnDecider(examples, embed or sentence_transformer_embedder(model_name, device), k)
