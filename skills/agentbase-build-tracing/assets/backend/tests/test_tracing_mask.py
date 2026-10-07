"""Trace masking: secrets never leave the process; usage numbers and amounts stay readable."""

from __future__ import annotations

import pytest

from app.observability import tracing


@pytest.fixture(autouse=True)
def _pii_on(monkeypatch):
    monkeypatch.setattr(tracing, "_mask_pii", True)


@pytest.mark.parametrize(
    "key",
    [
        "Authorization",
        "X-API-Key",
        "api-key",
        "refresh_token",
        "id_token",
        "client-secret",
        "LANGFUSE_SECRET_KEY",
        "private_key",
        "Cookie",
        "set-cookie",
        "X-GreenNode-AgentBase-Custom-Api-Key",
        "feedback_token",
        "accessToken",
        "refreshToken",
        "clientSecret",
    ],
)
def test_secret_keys_masked(key):
    assert tracing._mask(data={key: "s3cr3t-value"}) == {key: "***"}


@pytest.mark.parametrize(
    "key", ["max_tokens", "prompt_tokens", "tokens_before", "auth_mode", "model"]
)
def test_non_secret_keys_kept(key):
    assert tracing._mask(data={key: 1234}) == {key: 1234}


def test_secret_values_masked_in_text():
    text = (
        "Authorization: Bearer abc.def.ghi Basic dXNlcjpwYXNzMTIz key sk-proj-ABCDEFGHIJKLMNOPQRSTUV "
        "maas vn-FAKEKEY_aaaaBBBBccccDDDDeeeeFFFF0000"
    )
    out = tracing._mask(data=text)
    for leaked in ("abc.def.ghi", "dXNlcjpwYXNzMTIz", "sk-proj-ABCDEF", "vn-FAKEKEY"):
        assert leaked not in out


def test_pii_cards_and_ids_masked_amounts_kept():
    out = tracing._mask(
        data="card 4111 1111 1111 1111, CMND: 123456789, CCCD 012345678901, mail a@b.vn, "
        "amount 150000000 VND, order 1234567890123 (not a valid card)"
    )
    assert "4111" not in out and "123456789," not in out and "012345678901" not in out
    assert "a@b.vn" not in out
    assert "150000000" in out  # 9-digit amount without an ID keyword is kept
    assert "1234567890123" in out  # 13 digits failing Luhn is not a card


def test_email_user_id_hashed_for_trace():
    assert tracing.trace_user_id("alice@corp.vn").startswith("u-")
    assert tracing.trace_user_id("user-123") == "user-123"


@pytest.mark.parametrize("key", ["token_usage", "credential_type", "token_use", "expires_in"])
def test_metadata_keys_not_masked(key):
    assert tracing._mask(data={key: "v"}) == {key: "v"}


def test_prose_and_timestamps_not_masked():
    text = (
        "The basic idea is simple. Basic plan costs 100k. Basic Information here. "
        "Bearer of good news. ts=1728300000002"
    )
    assert tracing._mask(data=text) == text


# --------------------------------------------------------------------------- secrets INSIDE strings
@pytest.mark.parametrize(
    ("text", "leaked", "kept"),
    [
        (  # httpx error re-raised by an MCP tools/call (URL query secrets)
            "Server error '502 Bad Gateway' for url 'https://x/mcp?tavilyApiKey=SECRET-1&sig=abc'",
            ["SECRET-1", "sig=abc"],
            ["502 Bad Gateway", "https://x/mcp?tavilyApiKey=***&sig=***'"],
        ),
        (
            "GET https://idp.example/cb?code=c0de&access_token=AT-1&state=s1",
            ["AT-1"],
            ["state=s1"],
        ),
        (  # ToolMessage content = JSON in a string
            '{"access_token": "opaque-1", "refresh_token": "rt-2", "expires_in": 3600}',
            ["opaque-1", "rt-2"],
            ['"expires_in": 3600'],
        ),
        ("{'id_token': 'idt-3', 'scope': 'openid'}", ["idt-3"], ["'scope': 'openid'"]),
        ('{\\"access_token\\": \\"dbl-4\\", \\"user\\": \\"bob\\"}', ["dbl-4"], ["bob"]),
        (
            "password=hunter2 client_secret=abc123 max_tokens=1000 token_usage=5",
            ["hunter2", "abc123"],
            ["max_tokens=1000", "token_usage=5"],
        ),
        ("X-API-Key: k9secret\nAccept: json", ["k9secret"], ["Accept: json"]),
        ("url=https://x/?access_token=nested-5", ["nested-5"], ["url=https://x/"]),
        ("dial postgres://app:S3cret-6@db:5432/x failed", ["S3cret-6"], ["postgres://app:"]),
        (
            "X-Amz-Signature=deadbeef7&X-Amz-Date=20261007",
            ["deadbeef7"],
            ["X-Amz-Date=20261007"],
        ),
        (
            'Your OTP: 482913 cvv=999 {"pin": 1234, "passwd": "pw-8"}',
            ["482913", "999", "1234", "pw-8"],
            [],
        ),
    ],
)
def test_secrets_inside_text_masked(text, leaked, kept):
    out = tracing._mask(data=text)
    for s in leaked:
        assert s not in out, out
    for s in kept:
        assert s in out, out


def test_json_string_stays_parseable_after_masking():
    import json

    out = tracing._mask(data='{"access_token": "opaque-1", "expires_in": 3600}')
    assert json.loads(out) == {"access_token": "***", "expires_in": 3600}


def test_secret_header_pairs_masked():
    data = {
        "headers": [["X-API-Key", "k9secret"], ("Accept", "json")],
        "auth": ("Authorization", "Bearer zzzzzzzz1"),
    }
    assert tracing._mask(data=data) == {
        "headers": [["X-API-Key", "***"], ["Accept", "json"]],
        "auth": ["Authorization", "***"],
    }


def test_langchain_messages_masked():
    """Objects given to step()/event()/update() reach `mask` raw; the SDK serializes them afterwards."""
    import dataclasses
    import json

    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    @dataclasses.dataclass
    class Payload:
        api_key: str
        note: str

    ai = AIMessage(
        "",
        tool_calls=[{"name": "login", "args": {"password": "pw-1"}, "id": "c1"}],
        usage_metadata={
            "input_tokens": 10,
            "output_tokens": 2,
            "total_tokens": 12,
            "input_token_details": {"cache_read": 4},
        },
    )
    state = {
        "messages": [
            HumanMessage("my password=hunter2"),
            ai,
            ToolMessage('{"access_token": "tm-1"}', tool_call_id="c1"),
        ],
        "extra": Payload(api_key="dc-1", note="ok"),
    }
    out = tracing._mask(data=state)
    blob = json.dumps(out)  # must be plain JSON now (no objects left for the SDK serializer)
    for s in ("hunter2", "pw-1", "tm-1", "dc-1"):
        assert s not in blob
    assert out["messages"][2]["type"] == "tool"
    assert out["messages"][1]["usage_metadata"]["input_token_details"] == {"cache_read": 4}
    assert out["extra"]["note"] == "ok"
    assert state["messages"][0].content == "my password=hunter2"  # graph state untouched


def test_prose_and_non_secret_keys_in_text_not_masked():
    text = (
        "spin=3 pinned: yes opinion=good shipping=fast tokenizer=cl100k max_completion_tokens=5 "
        "Note: see https://docs.example/page?lang=vi at 10:30"
    )
    assert tracing._mask(data=text) == text


# --------------------------------------------------------------------------- false positives / negatives
@pytest.mark.parametrize("key", ["input_token_details", "output_token_details"])
def test_usage_detail_keys_readable(key):
    assert tracing._mask(data={key: {"cache_read": 5}}) == {key: {"cache_read": 5}}


@pytest.mark.parametrize("key", ["otp", "pin", "cvv", "pin_code", "otpCode", "signature"])
def test_otp_pin_cvv_keys_masked(key):
    assert tracing._mask(data={key: "1234"}) == {key: "***"}


@pytest.mark.parametrize(
    "text",
    [
        "score 0.123456789 and prob 0.987654321",  # decimals are not phones
        "Mã đơn hàng 202410070001",  # 12-digit order code (not CCCD-shaped)
        "ref 123456789012",
        "invoice 0123456789",  # not a VN mobile prefix
    ],
)
def test_numbers_that_are_not_pii_kept(text):
    assert tracing._mask(data=text) == text


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("Số CMND của tôi là: 123456789", "123456789"),  # keyword 13 chars before the number
        (
            "Số căn cước công dân của tôi: 123456789012",
            "123456789012",
        ),  # any 12 digits after keyword
        ("CCCD 079203001234", "079203001234"),  # bare, CCCD-shaped
        ("gọi 0912.345.678", "345"),
        ("call +84 912 345 678", "345"),
        ("(+84) 912345678", "912345678"),
    ],
)
def test_ids_and_phones_masked(text, secret):
    assert secret not in tracing._mask(data=text)


def test_pin_masked_only_when_it_looks_like_a_code():
    """'pin' is also Vietnamese for battery: specs stay readable, PIN codes are masked."""
    kept = tracing._mask(data={"pin": "5000mAh", "battery_pin": "Li-ion"})
    assert kept == {"pin": "5000mAh", "battery_pin": "Li-ion"}
    assert "5000mAh" in tracing._mask(data="Dung lượng pin: 5000mAh, sạc nhanh 33W")
    assert tracing._mask(data={"pin": "1234", "pin_code": 987654}) == {
        "pin": "***",
        "pin_code": "***",
    }
    assert "4321" not in tracing._mask(data="pin=4321&user=a")
    assert tracing._mask(data=[["PIN", "0000"]]) == [["PIN", "***"]]


def test_email_regex_is_linear_on_long_tokens():
    """The old unbounded email regex was quadratic on long runs without '@' (40 KB ≈ 1.2 s per mask call)."""
    import time

    blob = "a1" * 20_000  # 40 KB, no '@'
    t0 = time.perf_counter()
    tracing._mask(data=blob)
    assert time.perf_counter() - t0 < 0.2
    assert "x.y@corp.vn" not in tracing._mask(data=blob + " contact x.y@corp.vn")
