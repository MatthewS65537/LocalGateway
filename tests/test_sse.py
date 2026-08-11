from localgateway.sse import SSEParser, StreamAccumulator, render_event, usage_from_dict


def test_split_events_across_chunks():
    full = b'data: {"choices":[{"delta":{"content":"He"}}]}\n\ndata: {"choices":[{"delta":{"content":"llo"}}]}\n\ndata: [DONE]\n\n'
    p = SSEParser()
    events = p.feed(full[:10]) + p.feed(full[10:])
    assert len(events) == 3
    assert events[0].data == '{"choices":[{"delta":{"content":"He"}}]}'
    assert events[2].done


def test_keepalive_comments_dropped():
    p = SSEParser()
    events = p.feed(b": keepalive\n\ndata: {\"a\":1}\n\n")
    assert len(events) == 1
    assert events[0].data == '{"a":1}'


def test_crlf_line_endings():
    p = SSEParser()
    events = p.feed(b'data: {"a":1}\r\n\r\ndata: [DONE]\r\n\r\n')
    assert len(events) == 2
    assert events[1].done


def test_multiline_data_joined():
    p = SSEParser()
    events = p.feed(b"data: line1\ndata: line2\n\n")
    assert events[0].data == "line1\nline2"


def test_data_strips_exactly_one_leading_space():
    """B2 regression: SSE spec (HTML5 §9.2.4) strips ONE leading U+0020 after
    the colon. The old lstrip(" ") stripped all leading spaces, corrupting
    payloads that legitimately begin with a space."""
    p = SSEParser()
    # Two spaces after "data:" — one is the field separator, the second is
    # part of the value and must be preserved.
    events = p.feed(b'data:  {"k":" v"}\n\n')
    assert events[0].data == ' {"k":" v"}', repr(events[0].data)
    # Single space is stripped.
    events = p.feed(b'data: {"a":1}\n\n')
    assert events[0].data == '{"a":1}'
    # No space stays no space.
    events = p.feed(b'data:{"a":1}\n\n')
    assert events[0].data == '{"a":1}'


def test_crlf_split_across_chunk_boundary():
    """CRLF normalisation must not falsely join events when the '\r' lands at
    the end of one chunk and the '\n' at the start of the next."""
    p = SSEParser()
    events = p.feed(b'data: {"a":1}\r') + p.feed(b'\n\r\ndata: [DONE]\r\n\r\n')
    assert len(events) == 2
    assert events[0].data == '{"a":1}'
    assert events[1].done


def test_flush_trailing_bytes():
    p = SSEParser()
    assert p.feed(b'data: {"a":1}') == []
    ev = p.flush()
    assert ev is not None and ev.data == '{"a":1}'
    assert p.flush() is None


def test_flush_captures_raw():
    """P7: flush() now captures raw so tail events are byte-faithful
    (previously they were re-serialized, losing original formatting)."""
    p = SSEParser()
    p.feed(b'data: {"x": 1}\n')  # no trailing blank line → stays in buffer
    ev = p.flush()
    assert ev is not None
    assert ev.raw is not None
    assert render_event(ev) == b'data: {"x": 1}\n\n'


def test_flush_raw_preserves_multiline_data():
    """P7: flushed events with multi-line data preserve original bytes."""
    p = SSEParser()
    p.feed(b'data: line1\ndata: line2\n')
    ev = p.flush()
    assert ev is not None
    assert ev.raw is not None
    assert render_event(ev) == b'data: line1\ndata: line2\n\n'


def test_render_roundtrip():
    p = SSEParser()
    ev = p.feed(b"data: [DONE]\n\n")[0]
    assert render_event(ev) == b"data: [DONE]\n\n"
    ev2 = p.feed(b'data: {"x":1}\n\n')[0]
    assert render_event(ev2) == b'data: {"x":1}\n\n'


def test_accumulator_usage_and_reasoning():
    acc = StreamAccumulator()
    acc.feed(b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n')
    assert acc.ttft_ms is not None
    acc.feed(
        b'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":25,'
        b'"completion_tokens_details":{"reasoning_tokens":8}}}\n\n'
    )
    acc.feed(b"data: [DONE]\n\n")
    assert (acc.usage.input_tokens, acc.usage.output_tokens, acc.usage.reasoning_tokens) == (10, 25, 8)
    assert acc.saw_done


def test_accumulator_ttft_requires_content():
    acc = StreamAccumulator()
    acc.feed(b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n')
    assert acc.ttft_ms is None
    acc.feed(b'data: {"choices":[{"delta":{"reasoning_content":"t"}}]}\n\n')
    assert acc.ttft_ms is not None


def test_usage_from_dict_tolerant():
    u = usage_from_dict({"prompt_tokens": 5})
    assert u.input_tokens == 5 and u.output_tokens is None
    assert usage_from_dict({}).input_tokens is None
    assert usage_from_dict("garbage").input_tokens is None
