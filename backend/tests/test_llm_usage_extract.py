"""usage / actual_model 提取：不得伪造。"""

from __future__ import annotations

from types import SimpleNamespace

from app.services.llm_usage.extract import classify_error, extract_from_response, merge_chunk_usage
import asyncio


def test_extract_usage_metadata():
    resp = SimpleNamespace(
        usage_metadata={
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "input_token_details": {"cache_read": 3},
        },
        response_metadata={"model_name": "qwen-plus-real"},
    )
    u = extract_from_response(resp, requested_model="qwen-plus")
    assert u.usage_source == "provider_metadata"
    assert u.input_tokens == 10
    assert u.output_tokens == 5
    assert u.cached_input_tokens == 3
    assert u.actual_model == "qwen-plus-real"


def test_extract_missing_usage_is_unavailable_not_zero():
    resp = SimpleNamespace(usage_metadata=None, response_metadata={"model_name": "x"})
    u = extract_from_response(resp)
    assert u.usage_source == "unavailable"
    assert u.input_tokens is None
    assert u.output_tokens is None
    assert u.total_tokens is None
    assert u.actual_model == "x"


def test_actual_model_not_filled_from_requested():
    resp = SimpleNamespace(usage_metadata={"input_tokens": 1, "output_tokens": 1}, response_metadata={})
    u = extract_from_response(resp, requested_model="configured-model")
    assert u.actual_model is None


def test_token_usage_fallback():
    resp = SimpleNamespace(
        usage_metadata=None,
        response_metadata={
            "model": "deepseek-chat",
            "token_usage": {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9},
        },
    )
    u = extract_from_response(resp)
    assert u.input_tokens == 7
    assert u.output_tokens == 2
    assert u.actual_model == "deepseek-chat"


def test_merge_chunk_usage_stream():
    acc = extract_from_response(None)
    c1 = SimpleNamespace(content="hi", usage_metadata=None, response_metadata={})
    acc = merge_chunk_usage(acc, c1)
    assert acc.usage_source == "unavailable"
    c2 = SimpleNamespace(
        content="",
        usage_metadata={"input_tokens": 4, "output_tokens": 8, "total_tokens": 12},
        response_metadata={"model_name": "stream-model"},
    )
    acc = merge_chunk_usage(acc, c2)
    assert acc.usage_source == "provider_metadata"
    assert acc.total_tokens == 12
    assert acc.actual_model == "stream-model"


def test_classify_timeout_and_cancel():
    assert classify_error(asyncio.TimeoutError())[0] == "timeout"
    assert classify_error(asyncio.CancelledError())[0] == "cancelled"
    assert classify_error(RuntimeError("boom"))[0] == "error"
