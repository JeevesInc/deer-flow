"""Regression tests for Mem0InjectionMiddleware.

The middleware previously inserted its SystemMessage at ``insert_idx = 1`` when
no SystemMessage was found in ``request.messages`` — but ``request.messages``
does not contain the static system_prompt (that lives in
``request.system_message`` and is prepended later by the factory). The
old fallback therefore produced ``[Human, System(mem0)]``, which became
``[System(static), Human, System(mem0)]`` after the factory prepended the
static prompt, tripping langchain_anthropic's "Received multiple non-consecutive
system messages." check.

These tests pin the invariant: injection must never leave a SystemMessage
anywhere but at index 0 of the resulting messages list.
"""

from unittest.mock import MagicMock, patch

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from deerflow.agents.middlewares.mem0_injection_middleware import Mem0InjectionMiddleware


def _make_request(messages):
    """Build a ModelRequest-shaped object the middleware can consume.

    We only exercise the ``messages`` attribute and ``override`` method, so a
    MagicMock is enough — pulling in the real ModelRequest needs a model
    instance and is unnecessary for this test.
    """
    req = MagicMock()
    req.messages = messages
    captured = {}

    def _override(**kwargs):
        captured.update(kwargs)
        new_req = MagicMock()
        new_req.messages = kwargs.get("messages", messages)
        new_req.override = _override
        new_req._captured = captured
        return new_req

    req.override = _override
    req._captured = captured
    return req


def _enabled_config():
    cfg = MagicMock()
    cfg.enabled = True
    cfg.injection_enabled = True
    return cfg


class TestMem0InjectionPlacement:
    """The injected SystemMessage must always end up at index 0."""

    @patch("deerflow.agents.memory.mem0_store.search_memories")
    @patch("deerflow.agents.middlewares.mem0_injection_middleware.get_memory_config")
    def test_no_existing_system_prepends_at_index_zero(self, mock_config, mock_search):
        """When request.messages has no SystemMessage, inject at index 0.

        Repro for the "multiple non-consecutive system messages" bug.
        """
        mock_config.return_value = _enabled_config()
        mock_search.return_value = [{"memory": "user prefers terse answers"}]

        mw = Mem0InjectionMiddleware(top_k=10)
        request = _make_request([HumanMessage(content="what did we talk about?")])

        out = mw._inject(request)

        assert out.messages[0].type == "system", (
            f"Expected SystemMessage at index 0, got {out.messages[0].type}. "
            "Anything else risks 'non-consecutive system messages' once the "
            "factory prepends the static system_message."
        )
        # No SystemMessage may appear past index 0
        for i, msg in enumerate(out.messages[1:], start=1):
            assert msg.type != "system", f"Stray SystemMessage at index {i}"

    @patch("deerflow.agents.memory.mem0_store.search_memories")
    @patch("deerflow.agents.middlewares.mem0_injection_middleware.get_memory_config")
    def test_existing_leading_system_is_merged_not_duplicated(self, mock_config, mock_search):
        """If a SystemMessage already leads the list, merge into it."""
        mock_config.return_value = _enabled_config()
        mock_search.return_value = [{"memory": "fact A"}]

        mw = Mem0InjectionMiddleware(top_k=10)
        existing = SystemMessage(content="pre-existing system prompt")
        request = _make_request([existing, HumanMessage(content="hi")])

        out = mw._inject(request)

        system_count = sum(1 for m in out.messages if m.type == "system")
        assert system_count == 1, f"Expected exactly one SystemMessage, got {system_count}"
        assert out.messages[0].type == "system"
        # Merged content should reference both the original and the injection
        merged = out.messages[0].content
        flat = merged if isinstance(merged, str) else " ".join(
            p.get("text", "") if isinstance(p, dict) else str(p) for p in merged
        )
        assert "pre-existing system prompt" in flat
        assert "fact A" in flat

    @patch("deerflow.agents.memory.mem0_store.search_memories")
    @patch("deerflow.agents.middlewares.mem0_injection_middleware.get_memory_config")
    def test_existing_leading_system_with_list_content_merges(self, mock_config, mock_search):
        """Leading SystemMessage with list-of-blocks content should also merge cleanly."""
        mock_config.return_value = _enabled_config()
        mock_search.return_value = [{"memory": "fact B"}]

        mw = Mem0InjectionMiddleware(top_k=10)
        existing = SystemMessage(content=[{"type": "text", "text": "block prompt"}])
        request = _make_request([existing, HumanMessage(content="hi")])

        out = mw._inject(request)

        system_count = sum(1 for m in out.messages if m.type == "system")
        assert system_count == 1
        # Content should remain a list with the original block plus the appended one
        content = out.messages[0].content
        assert isinstance(content, list)
        texts = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
        assert "block prompt" in texts
        assert "fact B" in texts

    @patch("deerflow.agents.memory.mem0_store.search_memories")
    @patch("deerflow.agents.middlewares.mem0_injection_middleware.get_memory_config")
    def test_human_assistant_history_does_not_get_system_inserted_mid_list(
        self, mock_config, mock_search
    ):
        """Multi-turn conversation: still no stray SystemMessage past index 0."""
        mock_config.return_value = _enabled_config()
        mock_search.return_value = [{"memory": "recall me"}]

        mw = Mem0InjectionMiddleware(top_k=10)
        request = _make_request([
            HumanMessage(content="first question"),
            AIMessage(content="first answer"),
            HumanMessage(content="follow-up"),
        ])

        out = mw._inject(request)

        # SystemMessage strictly at index 0
        assert out.messages[0].type == "system"
        for i, msg in enumerate(out.messages[1:], start=1):
            assert msg.type != "system", f"Stray SystemMessage at index {i}"
        # Conversation history preserved in order
        assert [m.type for m in out.messages[1:]] == ["human", "ai", "human"]


class TestMem0InjectionShortCircuits:
    @patch("deerflow.agents.middlewares.mem0_injection_middleware.get_memory_config")
    def test_disabled_config_returns_request_unchanged(self, mock_config):
        cfg = MagicMock()
        cfg.enabled = False
        cfg.injection_enabled = True
        mock_config.return_value = cfg

        mw = Mem0InjectionMiddleware()
        original = [HumanMessage(content="hi")]
        request = _make_request(original)
        out = mw._inject(request)
        # No override was called — same request returned
        assert out is request

    @patch("deerflow.agents.middlewares.mem0_injection_middleware.get_memory_config")
    def test_injection_disabled_returns_request_unchanged(self, mock_config):
        cfg = MagicMock()
        cfg.enabled = True
        cfg.injection_enabled = False
        mock_config.return_value = cfg

        mw = Mem0InjectionMiddleware()
        request = _make_request([HumanMessage(content="hi")])
        out = mw._inject(request)
        assert out is request

    @patch("deerflow.agents.memory.mem0_store.search_memories")
    @patch("deerflow.agents.middlewares.mem0_injection_middleware.get_memory_config")
    def test_empty_memory_search_returns_request_unchanged(self, mock_config, mock_search):
        mock_config.return_value = _enabled_config()
        mock_search.return_value = []

        mw = Mem0InjectionMiddleware()
        request = _make_request([HumanMessage(content="hi")])
        out = mw._inject(request)
        assert out is request

    @patch("deerflow.agents.middlewares.mem0_injection_middleware.get_memory_config")
    def test_no_human_message_returns_request_unchanged(self, mock_config):
        mock_config.return_value = _enabled_config()

        mw = Mem0InjectionMiddleware()
        # Only an AIMessage — no query to search on
        request = _make_request([AIMessage(content="ai only")])
        out = mw._inject(request)
        assert out is request
