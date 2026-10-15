"""上下文工程单测 —— 裁剪 / 截断 / 配对完整 / 超预算摘要。"""

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.services.context_engine import (
    SUMMARY_PREFIX,
    build_context_pack,
    estimate_tokens,
    truncate_tool_content,
)


def test_estimate_tokens_cjk_and_ascii():
    assert estimate_tokens("你好世界") == 4
    assert estimate_tokens("abcd") >= 1
    assert estimate_tokens("") == 0


def test_truncate_tool_content():
    text, did = truncate_tool_content("x" * 100, 50)
    assert did is True
    assert "truncated" in text
    assert len(text) < 100

    text2, did2 = truncate_tool_content("short", 50)
    assert did2 is False
    assert text2 == "short"


def test_passthrough_under_budget():
    msgs = [
        HumanMessage(content="读 README"),
        AIMessage(content="好的，我来读取"),
    ]
    pack = build_context_pack(msgs, max_tokens=6000, keep_recent=12, tool_max_chars=4000)
    assert pack.meta.mode == "passthrough"
    assert pack.meta.kept_messages == 2
    assert len(pack.messages) == 2
    assert pack.summary == ""


def test_tool_output_truncated_in_pack():
    huge = "A" * 8000
    msgs = [
        HumanMessage(content="读大文件"),
        AIMessage(
            content="",
            tool_calls=[{"id": "c1", "name": "read_file", "args": {"path": "big.py"}}],
        ),
        ToolMessage(content=huge, tool_call_id="c1", name="read_file"),
    ]
    pack = build_context_pack(msgs, max_tokens=20000, keep_recent=12, tool_max_chars=500)
    assert pack.meta.tool_truncated == 1
    tool = [m for m in pack.messages if isinstance(m, ToolMessage)][0]
    assert "truncated" in tool.content
    assert len(tool.content) < len(huge)


def test_trim_keeps_tool_call_pairs():
    msgs = []
    for i in range(8):
        msgs.append(HumanMessage(content=f"用户轮次 {i} " + ("详请" * 40)))
        msgs.append(
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": f"t{i}",
                        "name": "read_file",
                        "args": {"path": f"f{i}.py"},
                    }
                ],
            )
        )
        msgs.append(
            ToolMessage(
                content=("结果" * 200) + f" file-{i}",
                tool_call_id=f"t{i}",
                name="read_file",
            )
        )
        msgs.append(AIMessage(content=f"本轮结论 {i}"))

    pack = build_context_pack(
        msgs,
        max_tokens=800,
        keep_recent=6,
        tool_max_chars=2000,
    )
    assert pack.meta.mode in {"trim", "summarize+trim"}
    assert pack.meta.kept_messages < len(msgs)
    # 窗口内不应出现孤立 ToolMessage（前面没有带 tool_calls 的 AI）
    window = [m for m in pack.messages if not (
        isinstance(m, type(pack.messages[0])) and SUMMARY_PREFIX in str(getattr(m, "content", ""))
    )]
    # 去掉 summary system
    from langchain_core.messages import SystemMessage

    window = [m for m in pack.messages if not isinstance(m, SystemMessage)]
    for idx, m in enumerate(window):
        if isinstance(m, ToolMessage):
            assert idx > 0
            prev = window[idx - 1]
            # 允许连续多个 ToolMessage，向前找 AI
            j = idx - 1
            while j >= 0 and isinstance(window[j], ToolMessage):
                j -= 1
            assert j >= 0
            assert getattr(window[j], "tool_calls", None)


def test_summary_injected_when_over_budget():
    msgs = [HumanMessage(content=("很长的历史对话。" * 80) + f"#{i}") for i in range(20)]
    msgs += [AIMessage(content="最新回答，请保留")]
    pack = build_context_pack(
        msgs,
        max_tokens=400,
        keep_recent=4,
        tool_max_chars=1000,
    )
    assert pack.meta.summarized or pack.meta.dropped_messages > 0
    from langchain_core.messages import SystemMessage

    systems = [m for m in pack.messages if isinstance(m, SystemMessage)]
    if pack.meta.summarized:
        assert systems
        assert SUMMARY_PREFIX in systems[0].content
    assert any("最新回答" in str(getattr(m, "content", "")) for m in pack.messages)
