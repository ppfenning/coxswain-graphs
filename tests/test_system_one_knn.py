import sys
import types

import pytest
import yaml

import runner.anthropic_runner as anthropic_runner
import runner.system_one_knn as knn
from harness.runners import build_runner
from runner.system_one import Choice, DecisionRunner, FastPathRunner, Noul, Score
from runner.system_one_knn import (
    Example,
    KnnDecider,
    UnsupportedQuestion,
    cosine,
    load_examples,
    pick,
    sentence_transformer_embedder,
)


def _state(name):
    return {"q": name}


def _stub(vectors, calls=None):
    """An embedder over a literal table from state name to vector."""

    def embed(texts):
        if calls is not None:
            calls.append(list(texts))
        return [vectors[text.removeprefix("q: ")] for text in texts]

    return embed


def _decider(rows, k, calls=None):
    """rows: (name, label, vector). The query is registered under the name `query` by the caller's table."""
    table = {name: vector for name, _, vector in rows} | {"query": [1.0, 0.0]}
    examples = [Example(_state(name), label) for name, label, _ in rows]
    return KnnDecider(examples, _stub(table, calls), k)


def test_cosine_of_literal_vectors() -> None:
    assert cosine([1, 0], [1, 0]) == 1.0
    assert cosine([1, 0], [0, 1]) == 0.0
    assert cosine([0, 0], [1, 0]) == 0.0


def test_nearest_labels_win_with_the_weighted_probabilities() -> None:
    decider = _decider(
        [("a", "yes", [1.0, 0.0]), ("b", "no", [0.6, 0.8]), ("c", "no", [0.0, 1.0])],
        k=2,
    )
    answer = decider.decide(Noul("q"), _state("query"))
    assert answer.value == "yes"
    assert answer.probabilities == pytest.approx({"yes": 1.0 / 1.6, "no": 0.6 / 1.6})
    assert answer.confidence == pytest.approx(0.625)


def test_a_three_to_one_vote_has_confidence_of_the_winning_share() -> None:
    rows = [(f"y{i}", "yes", [1.0, 0.0]) for i in range(3)] + [("n", "no", [1.0, 0.0])]
    answer = _decider(rows, k=4).decide(Noul("q"), _state("query"))
    assert answer.value == "yes"
    assert answer.probabilities == pytest.approx({"yes": 0.75, "no": 0.25})
    assert answer.confidence == pytest.approx(0.75)


def test_a_tie_goes_to_the_label_first_in_order() -> None:
    yes_first = [("a", "yes", [1.0, 0.0]), ("b", "no", [1.0, 0.0])]
    no_first = list(reversed(yes_first))
    assert [_decider(yes_first, k=2).decide(Noul("q"), _state("query")).value for _ in range(2)] == ["yes", "yes"]
    assert _decider(no_first, k=2).decide(Noul("q"), _state("query")).value == "no"
    assert pick({"x": 0.5, "y": 0.5}, ["y", "x"]) == "y"


def test_a_choice_ties_by_option_order_and_drops_labels_it_does_not_offer() -> None:
    rows = [("a", "other", [1.0, 0.0]), ("b", "x", [1.0, 0.0]), ("c", "y", [1.0, 0.0])]
    answer = _decider(rows, k=2).decide(Choice("q", ("y", "x")), _state("query"))
    assert answer.kind == "choice"
    assert answer.value == "y"
    assert answer.probabilities == pytest.approx({"y": 0.5, "x": 0.5})


def test_a_choice_probability_is_zero_for_an_option_no_neighbour_carries() -> None:
    rows = [("a", "x", [1.0, 0.0]), ("b", "y", [0.0, 1.0])]
    answer = _decider(rows, k=1).decide(Choice("q", ("x", "y", "z")), _state("query"))
    assert answer.probabilities == {"x": 1.0, "y": 0.0, "z": 0.0}


def test_a_choice_with_no_matching_example_is_unsupported() -> None:
    with pytest.raises(UnsupportedQuestion):
        _decider([("a", "x", [1.0, 0.0])], k=1).decide(Choice("q", ("y", "z")), _state("query"))


def test_all_zero_similarities_fall_back_to_equal_weights() -> None:
    rows = [("a", "yes", [0.0, 1.0]), ("b", "no", [-1.0, 0.0])]
    answer = _decider(rows, k=2).decide(Noul("q"), _state("query"))
    assert answer.probabilities == {"yes": 0.5, "no": 0.5}


def test_score_raises_unsupported_so_the_fast_path_falls_back() -> None:
    with pytest.raises(UnsupportedQuestion):
        _decider([("a", "yes", [1.0, 0.0])], k=1).decide(Score("q", ("lo", "hi")), _state("query"))
    assert issubclass(UnsupportedQuestion, Exception)


def test_examples_are_embedded_once_and_each_decision_embeds_only_the_query() -> None:
    calls: list[list[str]] = []
    decider = _decider([("a", "yes", [1.0, 0.0]), ("b", "no", [0.0, 1.0])], k=1, calls=calls)
    assert calls == [["q: a", "q: b"]]
    decider.decide(Noul("q"), _state("query"))
    decider.decide(Noul("q"), _state("query"))
    assert calls[1:] == [["q: query"], ["q: query"]]


def test_a_decider_is_a_decision_runner() -> None:
    assert isinstance(_decider([("a", "yes", [1.0, 0.0])], k=1), DecisionRunner)


def test_construction_rejects_no_examples_and_a_bad_k() -> None:
    with pytest.raises(ValueError, match="at least one example"):
        KnnDecider([], _stub({}), 1)
    with pytest.raises(ValueError, match="k must be"):
        _decider([("a", "yes", [1.0, 0.0])], k=0)


def test_the_loader_reads_lines_and_skips_blanks() -> None:
    lines = ['{"state": {"q": "a"}, "label": "yes"}', "", '{"state": {}, "label": "no"}\n']
    assert load_examples(lines) == [Example({"q": "a"}, "yes"), Example({}, "no")]


def test_the_loader_rejects_a_line_missing_label_and_names_the_line() -> None:
    lines = ['{"state": {"q": "a"}, "label": "yes"}', '{"state": {"q": "b"}}']
    with pytest.raises(ValueError, match=r"line 2: label"):
        load_examples(lines)


def test_the_loader_rejects_bad_state_bad_json_and_non_objects() -> None:
    for line, message in [
        ('{"label": "yes"}', "state"),
        ('{"state": {"q": 1}, "label": "yes"}', "state"),
        ('{"state": {}, "label": ""}', "label"),
        ("not json", "not JSON"),
        ("[1]", "object"),
    ]:
        with pytest.raises(ValueError, match=message):
            load_examples([line])


def _fake_sentence_transformers(monkeypatch):
    seen = {}

    class Model:
        def __init__(self, name, device):
            seen["args"] = (name, device)

        def encode(self, texts):
            return [[1, 0] for _ in texts]

    monkeypatch.setitem(sys.modules, "sentence_transformers", types.SimpleNamespace(SentenceTransformer=Model))
    return seen


def test_the_default_embedder_imports_lazily_and_passes_the_device(monkeypatch) -> None:
    seen = _fake_sentence_transformers(monkeypatch)
    embed = sentence_transformer_embedder("m", "cuda")
    assert seen["args"] == ("m", "cuda")
    assert embed(["a", "b"]) == [[1.0, 0.0], [1.0, 0.0]]


def test_the_default_embedder_rejects_an_unknown_device_and_names_the_extra_when_missing(monkeypatch) -> None:
    with pytest.raises(ValueError, match="device must be one of cpu, cuda"):
        sentence_transformer_embedder("m", "tpu")
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    with pytest.raises(ImportError, match="system-one-local"):
        sentence_transformer_embedder("m", "cpu")


class _Real:
    def __init__(self, profile, **kwargs):
        self.profile = profile


def _profile(tmp_path, **over):
    examples = tmp_path / "examples.jsonl"
    examples.write_text('{"state": {"q": "a"}, "label": "yes"}\n{"state": {"q": "b"}, "label": "no"}\n')
    block = {
        "backend": "knn-local",
        "model": "knn-1",
        "examples": str(examples),
        "k": 1,
        "roles": {"handoff": {"mode": "shadow", "threshold": 0.9}},
    } | over
    block = {key: value for key, value in block.items() if value is not None}
    path = tmp_path / "p.yaml"
    body = {"profile": "p", "tiers": {"cheap": "haiku", "standard": "sonnet", "deep": "opus"}, "system_one": block}
    path.write_text(yaml.safe_dump(body), encoding="utf-8")
    return path


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(anthropic_runner, "AnthropicRunner", _Real)
    monkeypatch.setattr(knn, "sentence_transformer_embedder", lambda *a, **k: lambda texts: [[1.0, 0.0]] * len(texts))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def test_build_runner_wraps_the_real_runner_for_backend_knn_local_with_no_api_key(tmp_path, wired) -> None:
    runner = build_runner(scripted=None, provider_profile=_profile(tmp_path))
    assert isinstance(runner, FastPathRunner)
    assert isinstance(runner._decider, KnnDecider)


def test_build_runner_needs_the_examples_path_for_knn_local(tmp_path, wired) -> None:
    with pytest.raises(ValueError, match=r"system_one\.examples"):
        build_runner(scripted=None, provider_profile=_profile(tmp_path, examples=None))
