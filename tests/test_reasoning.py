import json

from localgateway.config import ProviderConfig
from localgateway.reasoning import (
    flush_stream_chunk,
    normalize_delta,
    normalize_response_body,
    transform_stream_chunk,
)
from localgateway.sse import SSEParser


def _provider():
    return ProviderConfig(id="p", base_url="http://x")


def test_dual_emit_reasoning_content_to_reasoning():
    d = {"role": "assistant", "reasoning_content": "let me think"}
    assert normalize_delta(d)
    assert d["reasoning"] == "let me think"
    assert d["reasoning_content"] == "let me think"


def test_dual_emit_reasoning_to_reasoning_content():
    d = {"reasoning": "hmm"}
    assert normalize_delta(d)
    assert d["reasoning_content"] == "hmm"


def test_thinking_blocks_mapped_to_both():
    d = {"thinking": [{"type": "thinking", "thinking": "a"}, {"type": "thinking", "thinking": "b"}]}
    assert normalize_delta(d)
    assert d["reasoning_content"] == "ab"
    assert d["reasoning"] == "ab"


def test_no_reasoning_untouched():
    d = {"content": "hi"}
    assert not normalize_delta(d)
    assert "reasoning" not in d and "reasoning_content" not in d


def test_response_body_normalized():
    body = json.dumps({
        "choices": [{"message": {"role": "assistant", "content": "ans", "reasoning_content": "think"}}]
    }).encode()
    out = json.loads(normalize_response_body(_provider(), body))
    assert out["choices"][0]["message"]["reasoning"] == "think"


def test_response_body_without_reasoning_byte_identical():
    body = b'{"choices":[{"message":{"content":"hi"}}]}'
    assert normalize_response_body(_provider(), body) == body


def test_stream_transform_dual_emits_and_buffers():
    parser = SSEParser()
    out1 = transform_stream_chunk(
        _provider(),
        b'data: {"choices":[{"delta":{"reasoning_content":"t1"}}]}\n\ndata: {"choices":[{"delt',
        parser,
    )
    ev1 = json.loads(out1.decode().strip().removeprefix("data: "))
    assert ev1["choices"][0]["delta"]["reasoning"] == "t1"

    out2 = transform_stream_chunk(_provider(), b'a":{"content":"hi"}}]}\n\n', parser)
    ev2 = json.loads(out2.decode().strip().removeprefix("data: "))
    assert ev2["choices"][0]["delta"]["content"] == "hi"
    assert "reasoning" not in ev2["choices"][0]["delta"]
    assert flush_stream_chunk(_provider(), parser) == b""


def test_stream_flush_renders_trailing_event():
    parser = SSEParser()
    transform_stream_chunk(_provider(), b'data: {"choices":[{"delta":{"reasoning":"z"}}]}', parser)
    tail = flush_stream_chunk(_provider(), parser)
    ev = json.loads(tail.decode().strip().removeprefix("data: "))
    assert ev["choices"][0]["delta"]["reasoning_content"] == "z"
