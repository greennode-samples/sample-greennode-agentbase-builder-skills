from __future__ import annotations

import unicodedata

import pytest
from greennode_agentbase import RequestContext
from langchain_core.messages import AIMessage

from app import service
from app.config import get_settings


def _kb(monkeypatch, tmp_path, files: dict[str, str | bytes]):
    """KNOWLEDGE_DIR = tmp_path with these files; returns the local_tools module (caches cleared)."""
    import app.tools.local_tools as lt

    for name, content in files.items():
        path = tmp_path / name
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
    monkeypatch.setenv("KNOWLEDGE_DIR", str(tmp_path))
    get_settings.cache_clear()
    lt._knowledge_index.cache_clear()
    return lt


@pytest.fixture(autouse=True)
def _clear_knowledge_cache():
    yield
    import app.tools.local_tools as lt

    lt._knowledge_index.cache_clear()


EN_DOC = (
    "# Annual leave\n\nFull-time employees get 12 days of annual leave per year.\n\n"
    "# Benefits\n\nHealth insurance and a plan for remote work."
)
VI_DOC = (
    "# Nghỉ phép năm\n\nNhân viên chính thức được nghỉ phép năm 12 ngày mỗi năm.\n\n"
    "# Bảo hiểm\n\nCông ty đóng bảo hiểm sức khỏe cho nhân viên."
)


def test_search_knowledge_cites_source(tmp_path, monkeypatch):
    lt = _kb(monkeypatch, tmp_path, {"annual-leave.md": EN_DOC})
    assert "search_knowledge" in [t.name for t in lt.get_local_tools()]
    out = lt.search_knowledge.invoke({"query": "how many days of annual leave"})
    assert out.startswith("[Source: annual-leave.md — Annual leave]")  # best passage first
    assert "12 days of annual leave" in out


def test_no_knowledge_dir_means_no_tool(monkeypatch, tmp_path):
    import app.tools.local_tools as lt

    monkeypatch.setenv("KNOWLEDGE_DIR", str(tmp_path / "missing"))
    get_settings.cache_clear()
    lt._knowledge_index.cache_clear()
    assert "search_knowledge" not in [t.name for t in lt.get_local_tools()]


def test_non_utf8_file_is_skipped_not_fatal(monkeypatch, tmp_path, caplog):
    """One UTF-16 / legacy-encoded file used to make EVERY chat request fail (collect_tools)."""
    legacy = "Nghỉ phép năm".encode("utf-16")
    lt = _kb(monkeypatch, tmp_path, {"ok.md": "# Leave\n\n12 days.", "legacy.txt": legacy})
    assert "search_knowledge" in [t.name for t in lt.get_local_tools()]
    assert "12 days" in lt.search_knowledge.invoke({"query": "leave days"})
    assert "legacy.txt skipped" in caplog.text and "UTF-8" in caplog.text


@pytest.mark.parametrize(
    "query",
    [
        "nghỉ phép năm được bao nhiêu ngày",  # with diacritics (NFC)
        "nghi phep nam duoc bao nhieu ngay",  # typed without diacritics
        unicodedata.normalize("NFD", "nghỉ phép năm"),  # combining marks (macOS input)
        "NGHỈ PHÉP",  # case
    ],
    ids=["nfc", "no-diacritics", "nfd-query", "upper"],
)
@pytest.mark.parametrize("doc_form", ["NFC", "NFD"])
def test_vietnamese_query_forms_match(monkeypatch, tmp_path, query, doc_form):
    doc = unicodedata.normalize(doc_form, VI_DOC)
    lt = _kb(monkeypatch, tmp_path, {"chinh-sach.md": doc})
    out = unicodedata.normalize("NFC", lt.search_knowledge.invoke({"query": query}))
    assert out.startswith("[Source: chinh-sach.md — Nghỉ phép năm]"), out
    assert "12 ngày" in out


def test_exact_diacritics_rank_above_folded_match(monkeypatch, tmp_path):
    lt = _kb(
        monkeypatch,
        tmp_path,
        {"a.md": "# Miền Nam\n\nVăn phòng phía nam.", "b.md": "# Lịch\n\nNghỉ lễ cuối năm."},
    )
    assert lt.search_knowledge.invoke({"query": "năm"}).startswith("[Source: b.md")
    assert lt.search_knowledge.invoke({"query": "nam"}).startswith("[Source: a.md")


def test_whole_words_not_substrings(monkeypatch, tmp_path):
    """Substring counting ranked "Benefits" first for "an" (pl-an, an-d, an-nual)."""
    lt = _kb(monkeypatch, tmp_path, {"policy.md": EN_DOC})
    assert lt.search_knowledge.invoke({"query": "an"}).startswith("NO_MATCH")
    out = lt.search_knowledge.invoke({"query": "remote work plan"})
    assert out.startswith("[Source: policy.md — Benefits]")


def test_query_in_another_language_tells_the_llm_what_to_do(monkeypatch, tmp_path):
    """Documented behavior: keyword search cannot match a Vietnamese query against English documents ⇒
    the tool says so, names the documents' language, and asks for ONE retry before saying unavailable."""
    lt = _kb(monkeypatch, tmp_path, {"policy.md": EN_DOC})
    out = lt.search_knowledge.invoke({"query": "nghỉ phép năm được bao nhiêu ngày"})
    assert out.startswith("NO_MATCH") and "written in English" in out and "retry ONCE" in out
    assert "not available" in out
    tool = next(t for t in lt.get_local_tools() if t.name == "search_knowledge")
    assert "write the query in English" in tool.description  # the LLM is told before calling
    (tmp_path / "vi").mkdir()
    lt = _kb(monkeypatch, tmp_path / "vi", {"chinh-sach.md": VI_DOC})
    tool = next(t for t in lt.get_local_tools() if t.name == "search_knowledge")
    assert "write the query in Vietnamese" in tool.description


async def test_forwarded_claims_reach_tools_only_allowlisted(fake_llm, monkeypatch):
    import app.memory.short_term as st

    seen = {}
    orig = st.thread_config

    def spy(settings, **kw):
        cfg = orig(settings, **kw)
        seen.update(cfg["configurable"])
        return cfg

    monkeypatch.setattr("app.service.thread_config", spy)
    monkeypatch.setenv("AUTH_FORWARD_CLAIMS", '["employee_id"]')
    get_settings.cache_clear()
    fake_llm(AIMessage("ok"))
    import app.service as sv

    run = sv._Run(
        sv.Principal(user_id="alice", claims={"employee_id": "E01", "secret": "x"}),
        "claims-1",
        "chat",
        {"messages": []},
        "x",
    )
    await run._prepare()
    assert seen["user_claims"] == {"employee_id": "E01"}


def test_pii_masked_in_traces():
    from app.observability import tracing

    out = tracing._mask(data="Contact a.b@vng.com.vn or 0912 345 678, CCCD 012345678901")
    assert "vng.com.vn" not in out and "345 678" not in out and "012345678901" not in out


async def test_max_tool_rounds_gives_friendly_error(fake_llm, monkeypatch):
    import pytest
    from greennode_agentbase.exceptions import GreenNodeRequestError

    monkeypatch.setenv("MAX_TOOL_ROUNDS", "1")
    get_settings.cache_clear()
    loop = AIMessage("", tool_calls=[{"name": "get_current_time", "args": {}, "id": "t"}])
    fake_llm(*[loop] * 20)
    with pytest.raises(GreenNodeRequestError) as e:
        await service.handle({"message": "loop"}, RequestContext(session_id="loop-1", user_id="u"))
    assert e.value.status_code == 422


def test_forbidden_tools_evaluator():
    from evals.evaluators import forbidden_tools

    meta = {"forbidden_tools": ["create_leave_request"]}
    assert forbidden_tools(output={"tools_used": []}, metadata=meta).value == 1.0
    assert (
        forbidden_tools(output={"tools_used": ["create_leave_request"]}, metadata=meta).value == 0.0
    )
    interrupted = {
        "status": "interrupted",
        "interrupt": {"tool_calls": [{"name": "create_leave_request"}]},
    }
    assert forbidden_tools(output=interrupted, metadata=meta).value == 0.0


def test_get_current_time_defaults_to_app_timezone_and_rejects_unknown_zones(monkeypatch):
    """Empty timezone ⇒ APP_TIMEZONE; a non-IANA name (the model asked for "Asia/Hanoi" on a real run) ⇒ a
    readable answer instead of a tool exception."""
    from app.config import get_settings
    from app.tools.local_tools import get_current_time

    monkeypatch.setenv("APP_TIMEZONE", "Asia/Ho_Chi_Minh")
    get_settings.cache_clear()
    assert get_current_time.invoke({}).endswith("+07:00")
    assert get_current_time.invoke({"timezone": "Europe/Paris"})[-6:] in ("+01:00", "+02:00")
    assert "Unknown time zone 'Asia/Hanoi'" in get_current_time.invoke({"timezone": "Asia/Hanoi"})
