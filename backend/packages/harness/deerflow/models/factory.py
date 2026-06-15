import logging

from langchain.chat_models import BaseChatModel

from deerflow.config import get_app_config, get_tracing_config, is_tracing_enabled
from deerflow.reflection import resolve_class

logger = logging.getLogger(__name__)


def create_chat_model(name: str | None = None, thinking_enabled: bool = False, _attach_fallbacks: bool = True, **kwargs) -> BaseChatModel:
    """Create a chat model instance from the config.

    Args:
        name: The name of the model to create. If None, the first model in the config will be used.

    Returns:
        A chat model instance.
    """
    config = get_app_config()
    if name is None:
        name = config.models[0].name
    model_config = config.get_model_config(name)
    if model_config is None:
        raise ValueError(f"Model {name} not found in config") from None
    model_class = resolve_class(model_config.use, BaseChatModel)
    model_settings_from_config = model_config.model_dump(
        exclude_none=True,
        exclude={
            "use",
            "name",
            "fallback",
            "display_name",
            "description",
            "supports_thinking",
            "supports_reasoning_effort",
            "when_thinking_enabled",
            "thinking",
            "supports_vision",
        },
    )
    # Compute effective when_thinking_enabled by merging in the `thinking` shortcut field.
    # The `thinking` shortcut is equivalent to setting when_thinking_enabled["thinking"].
    has_thinking_settings = (model_config.when_thinking_enabled is not None) or (model_config.thinking is not None)
    effective_wte: dict = dict(model_config.when_thinking_enabled) if model_config.when_thinking_enabled else {}
    if model_config.thinking is not None:
        merged_thinking = {**(effective_wte.get("thinking") or {}), **model_config.thinking}
        effective_wte = {**effective_wte, "thinking": merged_thinking}
    if thinking_enabled and has_thinking_settings:
        if not model_config.supports_thinking:
            raise ValueError(f"Model {name} does not support thinking. Set `supports_thinking` to true in the `config.yaml` to enable thinking.") from None
        if effective_wte:
            model_settings_from_config.update(effective_wte)
    if not thinking_enabled and has_thinking_settings:
        if effective_wte.get("extra_body", {}).get("thinking", {}).get("type"):
            # OpenAI-compatible gateway: thinking is nested under extra_body
            kwargs.update({"extra_body": {"thinking": {"type": "disabled"}}})
            kwargs.update({"reasoning_effort": "minimal"})
        elif effective_wte.get("thinking", {}).get("type") == "adaptive":
            # Adaptive-only models (e.g. Fable 5) reject an explicit
            # {"type": "disabled"} with a 400 — omitting the param entirely is
            # the supported way to run without thinking.
            kwargs.pop("thinking", None)
        elif effective_wte.get("thinking", {}).get("type"):
            # Native langchain_anthropic: thinking is a direct constructor parameter
            kwargs.update({"thinking": {"type": "disabled"}})
    if not model_config.supports_reasoning_effort and "reasoning_effort" in kwargs:
        del kwargs["reasoning_effort"]

    # For Codex Responses API models: map thinking mode to reasoning_effort
    from deerflow.models.openai_codex_provider import CodexChatModel

    if issubclass(model_class, CodexChatModel):
        # The ChatGPT Codex endpoint currently rejects max_tokens/max_output_tokens.
        model_settings_from_config.pop("max_tokens", None)

        # Use explicit reasoning_effort from frontend if provided (low/medium/high)
        explicit_effort = kwargs.pop("reasoning_effort", None)
        if not thinking_enabled:
            model_settings_from_config["reasoning_effort"] = "none"
        elif explicit_effort and explicit_effort in ("low", "medium", "high", "xhigh"):
            model_settings_from_config["reasoning_effort"] = explicit_effort
        elif "reasoning_effort" not in model_settings_from_config:
            model_settings_from_config["reasoning_effort"] = "medium"

    model_instance = model_class(**kwargs, **model_settings_from_config)

    if is_tracing_enabled():
        try:
            from langchain_core.tracers.langchain import LangChainTracer

            tracing_config = get_tracing_config()
            tracer = LangChainTracer(
                project_name=tracing_config.project,
            )
            existing_callbacks = model_instance.callbacks or []
            model_instance.callbacks = [*existing_callbacks, tracer]
            logger.debug(f"LangSmith tracing attached to model '{name}' (project='{tracing_config.project}')")
        except Exception as e:
            logger.warning(f"Failed to attach LangSmith tracing to model '{name}': {e}")

    # Wrap with a step-down fallback chain if configured. Fallback models are
    # built WITHOUT their own fallbacks (_attach_fallbacks=False) to keep the
    # chain flat and prevent infinite recursion when models reference each other.
    if _attach_fallbacks and model_config.fallback:
        fallback_names = (
            [model_config.fallback]
            if isinstance(model_config.fallback, str)
            else list(model_config.fallback)
        )
        fallback_models = []
        for fb_name in fallback_names:
            if fb_name == name:
                continue
            try:
                fallback_models.append(
                    create_chat_model(
                        name=fb_name,
                        thinking_enabled=thinking_enabled,
                        _attach_fallbacks=False,
                        **kwargs,
                    )
                )
            except Exception as e:
                logger.warning(f"Could not build fallback model '{fb_name}' for '{name}': {e}")
        if fallback_models:
            from deerflow.models.fallback import FallbackChatModel

            logger.info(
                f"Model '{name}' configured with fallback chain: "
                f"{[name] + fallback_names}"
            )
            return FallbackChatModel(primary=model_instance, fallbacks=fallback_models)
    return model_instance
