import json
import logging
import os
from pathlib import Path

from pydantic import BaseModel, Field


class GatewayConfig(BaseModel):
    """Configuration for the API Gateway."""

    host: str = Field(default="0.0.0.0", description="Host to bind the gateway server")
    port: int = Field(default=8001, description="Port to bind the gateway server")
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"], description="Allowed CORS origins")


_gateway_config: GatewayConfig | None = None


def get_gateway_config() -> GatewayConfig:
    """Get gateway config, loading from environment if available."""
    global _gateway_config
    if _gateway_config is None:
        cors_origins_str = os.getenv("CORS_ORIGINS", "http://localhost:3000")
        _gateway_config = GatewayConfig(
            host=os.getenv("GATEWAY_HOST", "0.0.0.0"),
            port=int(os.getenv("GATEWAY_PORT", "8001")),
            cors_origins=cors_origins_str.split(","),
        )
    return _gateway_config


_logger = logging.getLogger(__name__)


def save_extensions_config(mcp_servers: dict, skills: dict) -> None:
    """Write the combined MCP + skills config to extensions_config.json and reload the cache.

    Args:
        mcp_servers: dict of server_name → server config (model_dump() dicts).
        skills: dict of skill_name → {"enabled": bool}.
    """
    from deerflow.config.extensions_config import ExtensionsConfig, reload_extensions_config

    config_path = ExtensionsConfig.resolve_config_path()
    if config_path is None:
        config_path = Path.cwd().parent / "extensions_config.json"
        _logger.info("No existing extensions config found. Creating new config at: %s", config_path)

    config_data = {
        "mcpServers": mcp_servers,
        "skills": skills,
    }

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config_data, f, indent=2)

    _logger.info("Extensions configuration saved to: %s", config_path)
    reload_extensions_config()
