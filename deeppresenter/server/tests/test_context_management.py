from pathlib import Path
from types import SimpleNamespace

import pytest
from openai.types.chat.chat_completion_message import ChatCompletionMessage
from openai.types.chat.chat_completion_message_function_tool_call import (
    ChatCompletionMessageFunctionToolCall as ToolCall,
)

from deeppresenter.agents.agent import Agent
from deeppresenter.agents.design import Design
from deeppresenter.utils.config import (
    ContextBudgetConfig,
    ContextWindowExceededError,
    DeepPresenterConfig,
    Endpoint,
    LLM,
    estimate_chat_tokens,
)
from deeppresenter.utils.typings import ChatMessage, ContextLayer, InputRequest, Role


def test_default_context_budget_is_scaled_to_250k() -> None:
    budget = ContextBudgetConfig()

    assert budget.context_limit_tokens == 250_000
    assert budget.reserved_output_tokens == 12_500
    assert budget.context_safety_margin_tokens == 2_500
    assert budget.template_context_max_tokens == 30_000
    assert budget.template_overview_max_tokens == 7_500
    assert budget.template_reference_max_tokens == 7_500
    assert budget.template_search_result_max_tokens == 3_750
    assert budget.compaction_input_max_tokens == 15_000
    assert budget.compaction_summary_max_tokens == 2_500
    assert budget.input_token_budget == 235_000


class _AgentEnv:
    cutoff_len = 4096
    _server_tools: dict = {}
    _tools_dict: dict = {}


class _BudgetAgent(Agent):
    async def loop(self, req: InputRequest, *args, **kwargs):
        if False:
            yield req.instruction


def _completion(content: str, usage=None) -> SimpleNamespace:
    return SimpleNamespace(
        usage=usage,
        choices=[
            SimpleNamespace(
                message=ChatCompletionMessage(
                    role="assistant",
                    content=content,
                )
            )
        ],
    )


def _make_agent(
    tmp_path: Path,
    *,
    context_limit_tokens: int = 1_400,
    max_context_folds: int = 2,
    template_context_max_tokens: int = 300,
) -> _BudgetAgent:
    role_file = tmp_path / "role.yaml"
    role_file.write_text(
        """
system:
  zh: system
  en: system
instruction: "{{ prompt }}"
use_model: design_agent
toolset:
  include_tool_servers: []
  include_tools: []
""".strip(),
        encoding="utf-8",
    )
    model = {
        "model": "test-model",
        "api_key": "test-key",
        "base_url": "http://localhost.invalid/v1",
        "context_limit_tokens": context_limit_tokens,
        "reserved_output_tokens": 100,
        "context_safety_margin_tokens": 100,
        "fold_trigger_ratio": 0.5,
        "compaction_input_max_tokens": 300,
        "template_context_max_tokens": template_context_max_tokens,
        "image_token_estimate": 100,
    }
    config = DeepPresenterConfig(
        file_path=str(tmp_path / "config.yaml"),
        context_folding=True,
        max_context_folds=max_context_folds,
        research_agent=model,
        design_agent=model,
        long_context_model=model,
    )
    return _BudgetAgent(
        config,
        _AgentEnv(),
        tmp_path / "workspace",
        "zh",
        config_file=role_file,
    )


def _make_design(
    tmp_path: Path,
    *,
    overview_text: str = "# pinned template overview",
    context_limit_tokens: int = 4_000,
    template_context_max_tokens: int = 1_000,
) -> Design:
    role_file = tmp_path / "design-role.yaml"
    role_file.write_text(
        """
system:
  zh: system
  en: system
instruction: |
  {{ template_context_overview }}
use_model: design_agent
toolset:
  include_tool_servers: []
  include_tools: []
""".strip(),
        encoding="utf-8",
    )
    model = {
        "model": "test-model",
        "api_key": "test-key",
        "base_url": "http://localhost.invalid/v1",
        "context_limit_tokens": context_limit_tokens,
        "reserved_output_tokens": 100,
        "context_safety_margin_tokens": 100,
        "template_context_max_tokens": template_context_max_tokens,
    }
    config = DeepPresenterConfig(
        file_path=str(tmp_path / "config.yaml"),
        context_folding=False,
        research_agent=model,
        design_agent=model,
        long_context_model=model,
    )
    workspace = tmp_path / "design-workspace"
    overview = workspace / "template_context" / "overview.md"
    overview.parent.mkdir(parents=True, exist_ok=True)
    overview.write_text(overview_text, encoding="utf-8")
    return Design(
        config,
        _AgentEnv(),
        workspace,
        "zh",
        config_file=role_file,
    )


def test_agent_history_round_trip_restores_conversation_context(
    tmp_path: Path,
) -> None:
    original = _make_agent(tmp_path, context_limit_tokens=4_000)
    original.chat_history.append(
        ChatMessage(role=Role.USER, content="第一版 Research 已完成")
    )
    history_dir = tmp_path / "history"
    original.save_history(history_dir, message_only=True)

    restored = _make_agent(tmp_path, context_limit_tokens=4_000)
    restored.load_history(history_dir / "_BudgetAgent-00-history.jsonl")
    restored.chat_history.append(
        ChatMessage(role=Role.USER, content="请删除重复章节并重试")
    )

    assert restored.chat_history[-2].text == "第一版 Research 已完成"
    assert restored.chat_history[-1].text == "请删除重复章节并重试"
    assert restored.chat_history[0].role == Role.SYSTEM


def test_agent_history_preserves_native_anthropic_thinking_blocks(
    tmp_path: Path,
) -> None:
    native_blocks = [
        {
            "type": "thinking",
            "thinking": "",
            "signature": "opaque-signature",
        },
        {
            "type": "redacted_thinking",
            "data": "opaque-redacted-data",
        },
        {"type": "text", "text": "继续"},
    ]
    original = _make_agent(tmp_path, context_limit_tokens=4_000)
    original.chat_history.append(
        ChatMessage(
            role=Role.ASSISTANT,
            content="继续",
            anthropic_content=native_blocks,
        )
    )
    history_dir = tmp_path / "history"
    original.save_history(history_dir, message_only=True)

    restored = _make_agent(tmp_path, context_limit_tokens=4_000)
    restored.load_history(history_dir / "_BudgetAgent-00-history.jsonl")

    assert restored.chat_history[-1].anthropic_content == native_blocks


@pytest.mark.asyncio
async def test_preflight_folds_before_sending_and_preserves_pinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list] = []

    async def fake_run(self, messages, **kwargs):
        calls.append(list(messages))
        return _completion("bounded summary", usage=None)

    monkeypatch.setattr(LLM, "run", fake_run)
    agent = _make_agent(tmp_path)
    template_message = ChatMessage(role=Role.USER, content="TEMPLATE-PIN")
    agent.add_context_message(
        template_message,
        ContextLayer.PINNED,
        template_context=True,
    )
    for index in range(12):
        agent.chat_history.append(
            ChatMessage(role=Role.USER, content=f"turn-{index}: " + "x" * 320)
        )

    estimated = await agent._preflight_context()

    assert calls, "preflight should compact before the normal model request"
    assert estimated <= int(agent.input_token_budget * agent.fold_trigger_ratio)
    assert any(message.id == template_message.id for message in agent.chat_history)
    assert estimate_chat_tokens(calls[0]) <= agent.llm.input_token_budget


@pytest.mark.asyncio
async def test_missing_usage_keeps_estimated_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_run(self, messages, **kwargs):
        return _completion("done", usage=None)

    monkeypatch.setattr(LLM, "run", fake_run)
    agent = _make_agent(tmp_path, context_limit_tokens=4_000)

    response = await agent.action(prompt="make slides")

    assert response.text == "done"
    assert agent.context_length > 0
    assert agent.cost.total == 0


@pytest.mark.asyncio
async def test_compaction_honors_fold_upper_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    call_count = 0

    async def fake_run(self, messages, **kwargs):
        nonlocal call_count
        call_count += 1
        return _completion("summary", usage=None)

    monkeypatch.setattr(LLM, "run", fake_run)
    agent = _make_agent(tmp_path, max_context_folds=1)
    for index in range(8):
        agent.chat_history.append(
            ChatMessage(role=Role.USER, content=f"turn-{index}: " + "x" * 100)
        )

    assert await agent.compact_history(target_tokens=500)
    assert not await agent.compact_history(target_tokens=500)
    assert agent.research_iter == 1
    assert call_count == 1


@pytest.mark.asyncio
async def test_provider_context_overflow_is_not_blindly_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def overflowing_call(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("maximum context length exceeded")

    monkeypatch.setattr(Endpoint, "call", overflowing_call)
    llm = LLM(
        model="test-model",
        api_key="test-key",
        base_url="http://localhost.invalid/v1",
        context_limit_tokens=2_000,
        reserved_output_tokens=100,
        context_safety_margin_tokens=100,
    )

    with pytest.raises(ContextWindowExceededError):
        await llm.run("small request")
    assert calls == 1


@pytest.mark.asyncio
async def test_tool_image_is_ephemeral_then_replaced_with_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    saw_image_in_request = False

    async def fake_run(self, messages, **kwargs):
        nonlocal saw_image_in_request
        saw_image_in_request = any(
            any(block.get("type") == "image_url" for block in message.content)
            for message in messages
        )
        return _completion("done", usage=None)

    monkeypatch.setattr(LLM, "run", fake_run)
    agent = _make_agent(tmp_path, context_limit_tokens=4_000)
    image_observation = ChatMessage(
        role=Role.TOOL,
        content=[
            {"type": "text", "text": "Image file: /tmp/slide.png"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,SECRET"},
            },
        ],
        tool_call_id="call-image",
    )

    async def fake_tool_execute(tool_call):
        return image_observation

    agent.agent_env.tool_execute = fake_tool_execute
    tool_call = ToolCall(
        id="call-image",
        type="function",
        function={"name": "inspect_slide", "arguments": "{}"},
    )
    await agent.execute([tool_call])
    assert image_observation.context_layer == ContextLayer.EPHEMERAL

    persisted_dir = tmp_path / "persisted"
    agent.save_history(persisted_dir, message_only=True)
    assert "SECRET" not in next(persisted_dir.iterdir()).read_text(encoding="utf-8")

    await agent.action(prompt="continue")

    assert saw_image_in_request
    assert image_observation.context_layer == ContextLayer.SUMMARIZABLE
    assert not image_observation.has_image
    assert "/tmp/slide.png" in image_observation.text


@pytest.mark.asyncio
@pytest.mark.parametrize("image_first", [False, True])
async def test_claude_tool_result_preserves_text_and_converts_image_in_any_order(
    image_first: bool,
) -> None:
    tool_call = ToolCall(
        id="call-template-reference",
        type="function",
        function={"name": "get_template_reference", "arguments": "{}"},
    )
    text_block = {"type": "text", "text": '{"slide_id":"s001"}'}
    image_block = {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,QUJD"},
    }
    observation = ChatMessage(
        role=Role.TOOL,
        content=(
            [image_block, text_block] if image_first else [text_block, image_block]
        ),
        from_tool=tool_call.function,
        tool_call_id=tool_call.id,
    )

    async def fake_tool_execute(call: ToolCall) -> ChatMessage:
        return observation

    agent = SimpleNamespace(
        name="Design",
        agent_env=SimpleNamespace(tool_execute=fake_tool_execute),
        model="claude-sonnet",
        chat_history=[],
        error_history=[],
        tools=[],
        context_warning=0,
        context_length=0,
        context_window=1_000,
        _update_context_estimate=lambda tools=None: 0,
        log_message=lambda message: None,
    )

    await Agent.execute(agent, [tool_call])

    assert any(
        block.get("type") == "text" and block.get("text") == '{"slide_id":"s001"}'
        for block in observation.content
    )
    converted_images = [
        block for block in observation.content if block.get("type") == "image"
    ]
    assert converted_images == [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": "QUJD",
            },
        }
    ]


@pytest.mark.asyncio
async def test_design_marks_template_prompt_before_first_model_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent_messages: list[dict[str, object]] = []

    async def fake_run(self, messages, **kwargs):
        sent_messages.extend(
            {
                "text": message.text,
                "extra_info": dict(message.extra_info),
            }
            for message in messages
        )
        return _completion("first design turn", usage=None)

    monkeypatch.setattr(LLM, "run", fake_run)
    design = _make_design(tmp_path)
    request = InputRequest(
        instruction="Create slides",
        template_id="brand_deck",
        template_revision_id="rev_001",
        template_context_path=str(design.workspace / "template_context"),
    )
    stream = design.loop(request, "manuscript.md")

    await anext(stream)
    await stream.aclose()

    template_prompts = [
        item
        for item in sent_messages
        if "# pinned template overview" in str(item["text"])
    ]
    assert len(template_prompts) == 1
    metadata = template_prompts[0]["extra_info"]
    assert metadata == {
        "context_layer": ContextLayer.PINNED.value,
        "template_context": True,
    }


@pytest.mark.asyncio
async def test_design_never_silently_slices_pinned_template_overview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "TEMPLATE-OVERVIEW-END"
    overview = "# template\n" + "a" * 10_500 + marker
    sent_text = ""

    async def fake_run(self, messages, **kwargs):
        nonlocal sent_text
        sent_text = "\n".join(message.text for message in messages)
        return _completion("first design turn", usage=None)

    monkeypatch.setattr(LLM, "run", fake_run)
    design = _make_design(
        tmp_path,
        overview_text=overview,
        context_limit_tokens=30_000,
        template_context_max_tokens=20_000,
    )
    request = InputRequest(
        instruction="Create slides",
        template_id="brand_deck",
        template_revision_id="rev_001",
        template_context_path=str(design.workspace / "template_context"),
    )
    stream = design.loop(request, "manuscript.md")

    await anext(stream)
    await stream.aclose()

    assert marker in sent_text


def _template_reference_image(index: int) -> ChatMessage:
    message = ChatMessage(
        role=Role.TOOL,
        content=[
            {"type": "text", "text": f"reference page {index}"},
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/webp;base64,IMG{index}"},
            },
        ],
        tool_call_id=f"call-ref-{index}",
    )
    message.set_context_layer(ContextLayer.EPHEMERAL, template_context=True)
    return message


def test_excess_template_images_are_evicted_oldest_first(tmp_path: Path) -> None:
    agent = _make_agent(tmp_path, template_context_max_tokens=2_000)
    assert agent.max_template_reference_images == 2
    references = [_template_reference_image(i) for i in range(3)]
    agent.chat_history.extend(references)

    # Preflight validation must degrade gracefully instead of raising.
    agent._validate_template_context()

    surviving = [msg for msg in references if msg.is_template_context]
    assert len(surviving) == 2
    # The two most recently fetched pages keep their image bytes.
    assert surviving == references[1:]
    assert all(msg.has_image for msg in surviving)
    # The oldest reference is downgraded to a text placeholder.
    oldest = references[0]
    assert not oldest.has_image
    assert not oldest.is_template_context
    assert oldest.context_layer == ContextLayer.SUMMARIZABLE
    assert "reference page 0" in oldest.text
    assert (
        sum(Agent._count_images(msg) for msg in references)
        == agent.max_template_reference_images
    )


def test_eviction_runs_before_the_token_budget_check(tmp_path: Path) -> None:
    """Over-fetching references must not fail as misleading token overflow."""

    # Six images at image_token_estimate=100 exceed the 300-token template
    # budget before eviction, but fit once the cap is enforced.
    agent = _make_agent(tmp_path)
    references = [_template_reference_image(i) for i in range(6)]
    agent.chat_history.extend(references)

    agent._validate_template_context()

    assert sum(Agent._count_images(msg) for msg in references) == 2
    assert [msg for msg in references if msg.is_template_context] == references[4:]


def test_oversized_template_context_still_fails_hard(tmp_path: Path) -> None:
    agent = _make_agent(tmp_path)
    huge = ChatMessage(
        role=Role.USER,
        content=[{"type": "text", "text": "token " * 5_000}],
    )
    huge.set_context_layer(ContextLayer.PINNED, template_context=True)
    agent.chat_history.append(huge)

    with pytest.raises(ContextWindowExceededError, match="template_context_max_tokens"):
        agent._validate_template_context()
