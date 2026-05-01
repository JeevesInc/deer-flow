"""Memory API router for retrieving and managing memory data.

Profile sections are in memory.json, long-term facts are in mem0.
"""

import logging
from fastapi import APIRouter
from pydantic import BaseModel, Field

from deerflow.agents.memory.updater import get_memory_data, reload_memory_data
from deerflow.config.memory_config import get_memory_config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["memory"])


class ContextSection(BaseModel):
    summary: str = Field(default="", description="Summary content")
    updatedAt: str = Field(default="", description="Last update timestamp")


class UserContext(BaseModel):
    workContext: ContextSection = Field(default_factory=ContextSection)
    personalContext: ContextSection = Field(default_factory=ContextSection)
    topOfMind: ContextSection = Field(default_factory=ContextSection)


class HistoryContext(BaseModel):
    recentMonths: ContextSection = Field(default_factory=ContextSection)
    earlierContext: ContextSection = Field(default_factory=ContextSection)
    longTermBackground: ContextSection = Field(default_factory=ContextSection)


class Mem0Memory(BaseModel):
    id: str = Field(default="", description="Memory ID")
    memory: str = Field(default="", description="Memory text")
    score: float = Field(default=0.0, description="Relevance score")


class MemoryResponse(BaseModel):
    version: str = Field(default="2.0", description="Memory schema version")
    lastUpdated: str = Field(default="", description="Last update timestamp")
    user: UserContext = Field(default_factory=UserContext)
    history: HistoryContext = Field(default_factory=HistoryContext)
    mem0_count: int = Field(default=0, description="Number of memories in mem0")
    # Legacy: facts may still exist during migration
    facts: list = Field(default_factory=list)


class MemoryConfigResponse(BaseModel):
    enabled: bool = Field(..., description="Whether memory is enabled")
    storage_path: str = Field(..., description="Path to memory storage file")
    debounce_seconds: int = Field(..., description="Debounce time for memory updates")
    max_facts: int = Field(..., description="Maximum number of facts (legacy)")
    fact_confidence_threshold: float = Field(..., description="Minimum confidence threshold (legacy)")
    injection_enabled: bool = Field(..., description="Whether memory injection is enabled")
    max_injection_tokens: int = Field(..., description="Maximum tokens for profile injection")
    mem0_enabled: bool = Field(default=True, description="Whether mem0 long-term memory is active")


class MemoryStatusResponse(BaseModel):
    config: MemoryConfigResponse
    data: MemoryResponse


def _get_mem0_count() -> int:
    """Get the number of memories stored in mem0."""
    try:
        from deerflow.agents.memory.mem0_store import get_all_memories
        memories = get_all_memories()
        return len(memories)
    except Exception as e:
        logger.warning("Failed to get mem0 count: %s", e)
        return -1


@router.get("/memory", response_model=MemoryResponse, summary="Get Memory Data")
async def get_memory() -> MemoryResponse:
    memory_data = get_memory_data()
    return MemoryResponse(
        version=memory_data.get("version", "2.0"),
        lastUpdated=memory_data.get("lastUpdated", ""),
        user=UserContext(**memory_data.get("user", {})),
        history=HistoryContext(**memory_data.get("history", {})),
        mem0_count=_get_mem0_count(),
        facts=memory_data.get("facts", []),
    )


@router.post("/memory/reload", response_model=MemoryResponse, summary="Reload Memory Data")
async def reload_memory() -> MemoryResponse:
    memory_data = reload_memory_data()
    return MemoryResponse(
        version=memory_data.get("version", "2.0"),
        lastUpdated=memory_data.get("lastUpdated", ""),
        user=UserContext(**memory_data.get("user", {})),
        history=HistoryContext(**memory_data.get("history", {})),
        mem0_count=_get_mem0_count(),
        facts=memory_data.get("facts", []),
    )


@router.get("/memory/config", response_model=MemoryConfigResponse, summary="Get Memory Configuration")
async def get_memory_config_endpoint() -> MemoryConfigResponse:
    config = get_memory_config()
    return MemoryConfigResponse(
        enabled=config.enabled,
        storage_path=config.storage_path,
        debounce_seconds=config.debounce_seconds,
        max_facts=config.max_facts,
        fact_confidence_threshold=config.fact_confidence_threshold,
        injection_enabled=config.injection_enabled,
        max_injection_tokens=config.max_injection_tokens,
        mem0_enabled=True,
    )


@router.get("/memory/status", response_model=MemoryStatusResponse, summary="Get Memory Status")
async def get_memory_status() -> MemoryStatusResponse:
    config = get_memory_config()
    memory_data = get_memory_data()

    return MemoryStatusResponse(
        config=MemoryConfigResponse(
            enabled=config.enabled,
            storage_path=config.storage_path,
            debounce_seconds=config.debounce_seconds,
            max_facts=config.max_facts,
            fact_confidence_threshold=config.fact_confidence_threshold,
            injection_enabled=config.injection_enabled,
            max_injection_tokens=config.max_injection_tokens,
            mem0_enabled=True,
        ),
        data=MemoryResponse(
            version=memory_data.get("version", "2.0"),
            lastUpdated=memory_data.get("lastUpdated", ""),
            user=UserContext(**memory_data.get("user", {})),
            history=HistoryContext(**memory_data.get("history", {})),
            mem0_count=_get_mem0_count(),
            facts=memory_data.get("facts", []),
        ),
    )


@router.get("/memory/mem0", summary="Search mem0 memories")
async def search_mem0(q: str = "", limit: int = 20):
    """Search or list mem0 memories. If q is empty, returns all."""
    try:
        if q:
            from deerflow.agents.memory.mem0_store import search_memories
            results = search_memories(q, top_k=limit)
        else:
            from deerflow.agents.memory.mem0_store import get_all_memories
            results = get_all_memories()
        return {"results": results, "count": len(results)}
    except Exception as e:
        logger.error("mem0 search failed: %s", e)
        return {"results": [], "count": 0, "error": str(e)}
