"""Eval runner + evaluators: exit codes (1 = gate only, 2 = could not run), dataset pushes, score hygiene."""

from __future__ import annotations

import math
import sys
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.messages import AIMessage
from langfuse import Evaluation
from langfuse.api import NotFoundError

from app.config import get_settings
from evals import evaluators, run_eval
from evals.evaluators import expected_tools, forbidden_tools, item_passed, no_error


def _ok_result(n: int = 1):
    passed = SimpleNamespace(evaluations=[no_error(output={"status": "success", "response": "x"})])
    return SimpleNamespace(item_results=[passed] * n, run_evaluations=[], format=lambda: "")


class _Client:
    """Langfuse stub: records calls; override attributes to inject failures."""

    def __init__(self, dataset_items: int = 1):
        self.calls: list[tuple[str, dict]] = []
        self.dataset = SimpleNamespace(
            items=[object()] * dataset_items, run_experiment=lambda **kw: _ok_result(dataset_items)
        )

    def auth_check(self):
        self.calls.append(("auth_check", {}))
        return True

    def create_dataset(self, **kw):
        self.calls.append(("create_dataset", kw))

    def create_dataset_item(self, **kw):
        self.calls.append(("create_dataset_item", kw))

    def get_dataset(self, name):
        self.calls.append(("get_dataset", {"name": name}))
        return self.dataset

    def run_experiment(self, **kw):
        self.calls.append(("run_experiment", kw))
        return _ok_result(len(kw["data"]))


@pytest.fixture
def langfuse(monkeypatch):
    def install(client):
        monkeypatch.setattr(run_eval.tracing, "init_tracing", lambda s: None)
        monkeypatch.setattr(run_eval.tracing, "get_client", lambda: client)
        monkeypatch.setattr(run_eval.tracing, "shutdown_tracing", lambda: None)
        return client

    return install


def _main(monkeypatch, *argv: str) -> int:
    monkeypatch.setattr(sys, "argv", ["run_eval", *argv])
    return run_eval.main()


@pytest.fixture
def jsonl(tmp_path):
    p = tmp_path / "d.jsonl"
    p.write_text(
        '{"id": "a", "input": {"message": "hi"}, "expected_output": "hello"}\n\n'
        '{"id": "b", "input": {"message": "time?"}}\n',
        encoding="utf-8",
    )
    return str(p)


# --- exit codes: 1 is reserved for the quality gate; anything that stops the eval from running is 2
def test_langfuse_without_data_or_dataset_is_config_error(monkeypatch, langfuse, capsys):
    client = langfuse(_Client())
    assert _main(monkeypatch) == run_eval.EXIT_CONFIG_ERROR  # was TypeError from load_jsonl(None)
    assert "--data" in capsys.readouterr().err
    assert not any(c[0] == "run_experiment" for c in client.calls)


@pytest.mark.parametrize("with_langfuse", [False, True])
def test_missing_jsonl_is_config_error(monkeypatch, langfuse, capsys, tmp_path, with_langfuse):
    langfuse(_Client() if with_langfuse else None)
    missing = str(tmp_path / "nope.jsonl")
    assert _main(monkeypatch, "--data", missing) == run_eval.EXIT_CONFIG_ERROR
    assert "nope.jsonl" in capsys.readouterr().err


def test_invalid_jsonl_line_is_config_error(monkeypatch, langfuse, capsys, tmp_path):
    langfuse(None)
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"id": "a", "input": {"message": "hi"}}\n{not json\n', encoding="utf-8")
    assert _main(monkeypatch, "--data", str(bad)) == run_eval.EXIT_CONFIG_ERROR
    assert "bad.jsonl:2" in capsys.readouterr().err


def test_unknown_dataset_is_config_error(monkeypatch, langfuse, capsys):
    client = _Client()

    def missing(name):
        raise NotFoundError(body={"message": "Dataset not found"})

    client.get_dataset = missing
    langfuse(client)
    assert _main(monkeypatch, "--dataset", "nope") == run_eval.EXIT_CONFIG_ERROR
    assert "'nope' not found" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["--dataset", "d"], ["--data", "evals/datasets/smoke.jsonl"]])
def test_unreachable_langfuse_is_config_error(monkeypatch, langfuse, capsys, argv):
    client = _Client()

    def down(*a, **k):
        raise httpx.ConnectError("[Errno 61] Connection refused")

    client.auth_check = client.get_dataset = down
    langfuse(client)
    assert _main(monkeypatch, *argv) == run_eval.EXIT_CONFIG_ERROR
    assert "Langfuse" in capsys.readouterr().err
    # fail fast: no (paid) agent/judge calls when results can't be recorded anyway
    assert not any(c[0] == "run_experiment" for c in client.calls)


def test_unexpected_runner_crash_is_not_reported_as_gate_failure(monkeypatch, langfuse, jsonl):
    client = _Client()

    def crash(**kw):
        raise RuntimeError("boom")

    client.run_experiment = crash
    langfuse(client)
    assert _main(monkeypatch, "--data", jsonl) == run_eval.EXIT_CONFIG_ERROR


def test_gate_pass_and_fail_exit_codes(monkeypatch, langfuse, jsonl):
    langfuse(_Client())
    assert _main(monkeypatch, "--data", jsonl, "--min-pass-rate", "0.8") == 0
    client = _Client(dataset_items=3)
    client.dataset.run_experiment = lambda **kw: _ok_result(1)  # 1 of 3 passed
    langfuse(client)
    assert (
        _main(monkeypatch, "--dataset", "d", "--min-pass-rate", "0.5") == run_eval.EXIT_GATE_FAILED
    )


# --- --push: Langfuse item ids are global across datasets ⇒ namespace them by dataset
def test_push_namespaces_item_ids_by_dataset(monkeypatch, langfuse, jsonl):
    client = langfuse(_Client(dataset_items=2))
    assert _main(monkeypatch, "--data", jsonl, "--push", "--dataset", "proj-smoke") == 0
    ids = [kw["id"] for name, kw in client.calls if name == "create_dataset_item"]
    assert ids == ["proj-smoke:a", "proj-smoke:b"]
    assert all(
        kw["dataset_name"] == "proj-smoke" for n, kw in client.calls if n == "create_dataset_item"
    )


def test_push_requires_data_and_dataset(monkeypatch, langfuse):
    langfuse(_Client())
    assert _main(monkeypatch, "--push", "--dataset", "d") == run_eval.EXIT_CONFIG_ERROR


# --- score hygiene: NaN must never pass, empty aggregates must not be recorded as 0
@pytest.mark.parametrize("raw", ['{"score": NaN}', '{"score": Infinity}', '{"score": -Infinity}'])
async def test_judge_non_finite_score_is_zero(fake_llm, raw):
    fake_llm(AIMessage(raw))
    ev = await evaluators.llm_judge_correctness(
        input={"message": "q"}, output={"response": "a"}, expected_output="a"
    )
    assert ev.value == 0.0


def test_item_with_nan_score_fails():
    ok = no_error(output={"status": "success", "response": "a"})
    assert not item_passed([ok, Evaluation(name="custom", value=math.nan)])
    assert not item_passed([ok, Evaluation(name="custom", value=math.inf)])
    assert item_passed([ok, Evaluation(name="custom", value=0.9)])


def test_avg_is_skipped_when_no_item_has_the_score():
    items = [SimpleNamespace(evaluations=[no_error(output={"status": "success", "response": "a"})])]
    assert evaluators.avg("correctness")(item_results=items) is None
    scored = [
        SimpleNamespace(evaluations=[Evaluation(name="correctness", value=v)]) for v in (1, 0)
    ]
    assert evaluators.avg("correctness")(item_results=scored).value == 0.5


# --- --hitl reject: a rejected tool call is NOT "used" (expected_tools fails, forbidden_tools passes)
@pytest.mark.parametrize(("action", "used"), [("approve", ["remember"]), ("reject", [])])
async def test_hitl_reject_does_not_count_as_tool_used(fake_llm, monkeypatch, action, used):
    monkeypatch.setenv("HITL_TOOLS", '["remember"]')
    get_settings.cache_clear()
    fake_llm(
        AIMessage("", tool_calls=[{"name": "remember", "args": {"fact": "tea"}, "id": "c1"}]),
        AIMessage("Done."),
    )
    out = await run_eval.make_task(action)(item={"input": {"message": "remember I like tea"}})
    assert out["status"] == "success" and out["tools_used"] == used
    meta = {"expected_tools": ["remember"], "forbidden_tools": ["remember"]}
    assert expected_tools(output=out, metadata=meta).value == (1.0 if used else 0.0)
    assert forbidden_tools(output=out, metadata=meta).value == (0.0 if used else 1.0)
