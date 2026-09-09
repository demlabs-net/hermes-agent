from agent.transports.chat_completions import ChatCompletionsTransport


def _tool_image(call_id: str, label: str) -> dict:
    return {
        "role": "tool",
        "name": "vision_analyze",
        "tool_call_id": call_id,
        "content": [
            {"type": "text", "text": label},
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{call_id}"},
            },
        ],
    }


def test_strict_provider_promotes_tool_image_after_complete_result_block():
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "a", "type": "function", "function": {"name": "vision_analyze", "arguments": "{}"}},
                {"id": "b", "type": "function", "function": {"name": "vision_analyze", "arguments": "{}"}},
            ],
        },
        _tool_image("a", "first image"),
        _tool_image("b", "second image"),
    ]

    result = ChatCompletionsTransport().convert_messages(
        messages,
        model="ornith-1.5-9b",
        supports_vision_tool_messages=False,
    )

    assert [message["role"] for message in result] == [
        "assistant", "tool", "tool", "user"
    ]
    assert result[1]["content"] == "first image"
    assert result[2]["content"] == "second image"
    assert [part["type"] for part in result[3]["content"]] == [
        "text", "image_url", "image_url"
    ]
    assert isinstance(messages[1]["content"], list)


def test_strict_provider_merges_promoted_image_into_following_user_turn():
    messages = [
        _tool_image("a", "image summary"),
        {"role": "user", "content": "continue"},
    ]

    result = ChatCompletionsTransport().convert_messages(
        messages,
        supports_vision_tool_messages=False,
    )

    assert [message["role"] for message in result] == ["tool", "user"]
    assert [part["type"] for part in result[1]["content"]] == [
        "text", "image_url", "text"
    ]
    assert result[1]["content"][-1]["text"] == "continue"


def test_capable_provider_keeps_multimodal_tool_result_unchanged():
    messages = [_tool_image("a", "native")]

    result = ChatCompletionsTransport().convert_messages(
        messages,
        supports_vision_tool_messages=True,
    )

    assert isinstance(result[0]["content"], list)
    assert result[0]["content"] == messages[0]["content"]
    assert result[0]["tool_call_id"] == "a"
    # The upstream transport now strips the internal-only tool name on every
    # provider while preserving native multimodal content.
    assert "name" not in result[0]


def test_custom_provider_defaults_to_strict_tool_content():
    from providers import get_provider_profile

    profile = get_provider_profile("custom")
    assert profile is not None
    assert profile.supports_vision_tool_messages is False

    kwargs = ChatCompletionsTransport().build_kwargs(
        model="ornith-1.5-9b",
        messages=[_tool_image("a", "native local image")],
        provider_profile=profile,
        supports_vision_tool_messages=profile.supports_vision_tool_messages,
    )
    assert [message["role"] for message in kwargs["messages"]] == ["tool", "user"]
