"""Regression tests for the mem0 LLM config (2026-07-02).

claude-sonnet-5 rejects `temperature` with a 400 ("`temperature` is
deprecated for this model"). mem0's BaseLlmConfig defaults temperature=0.1
and its Anthropic wrapper sends it unless it is explicitly None — so every
fact extraction silently failed. build_mem0_config must pin temperature to
None, and mem0's wrapper must actually honor that.
"""

from deerflow.agents.memory.mem0_store import build_mem0_config


def test_llm_config_pins_temperature_none():
    config = build_mem0_config("test-key", "localhost", 6333)
    llm_config = config["llm"]["config"]
    assert "temperature" in llm_config, "temperature must be explicitly set"
    assert llm_config["temperature"] is None, (
        "temperature must be None — mem0 defaults it to 0.1 otherwise, and "
        "claude-sonnet-5 400s on any temperature value"
    )


def test_mem0_anthropic_wrapper_omits_none_temperature():
    """mem0's Anthropic wrapper must not send temperature when it's None.

    Guards against a mem0 upgrade changing the is-None check that the fix
    relies on.
    """
    from mem0.configs.llms.anthropic import AnthropicConfig
    from mem0.llms.anthropic import AnthropicLLM

    llm_cfg = build_mem0_config("test-key", "localhost", 6333)["llm"]["config"]
    llm = AnthropicLLM(AnthropicConfig(**llm_cfg))
    params = llm._get_common_params()
    assert "temperature" not in params
    assert params.get("max_tokens") == 4096


def test_config_shape_unchanged():
    config = build_mem0_config("test-key", "qhost", 7777)
    assert config["llm"]["provider"] == "anthropic"
    assert config["llm"]["config"]["model"] == "claude-sonnet-5"
    assert config["vector_store"]["provider"] == "qdrant"
    assert config["vector_store"]["config"]["host"] == "qhost"
    assert config["vector_store"]["config"]["port"] == 7777
    assert config["embedder"]["config"]["embedding_dims"] == 384
