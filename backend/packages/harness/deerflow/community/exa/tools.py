"""
Web search / fetch via Exa (2026-11-01, Brian: "Switch search to Exa").

Calls the Exa REST API directly with httpx (no exa-py dependency).
Key: EXA_API_KEY env var, or `api_key:` on the tool entry in config.yaml.
If no key is configured, web_search falls back to DuckDuckGo and web_fetch to a
plain HTTP GET, so the switch never leaves the agent without search.
"""

import html as _html
import json
import logging
import os
import re

import httpx
from langchain.tools import tool

from deerflow.config import get_app_config

logger = logging.getLogger(__name__)

EXA_URL = "https://api.exa.ai"
DEFAULT_MAX_RESULTS = 5
HIGHLIGHT_CHARS = 1500
FETCH_CHARS = 20000


def _extra(name: str) -> dict:
    cfg = get_app_config().get_tool_config(name)
    return dict(cfg.model_extra) if cfg is not None else {}


def _key(extra: dict) -> str | None:
    k = extra.get("api_key") or os.getenv("EXA_API_KEY")
    return k.strip() if isinstance(k, str) and k.strip() and not k.startswith("$") else None


def _post(path: str, key: str, payload: dict, timeout: float) -> dict:
    r = httpx.post(EXA_URL + path, headers={"x-api-key": key, "content-type": "application/json"}, json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()


@tool("web_search", parse_docstring=True)
def web_search_tool(query: str, max_results: int = 5) -> str:
    """Search the web for information. Use this tool to find current information, news, articles, and facts from the internet.

    Args:
        query: Search keywords describing what you want to find. Be specific for better results.
        max_results: Maximum number of results to return. Default is 5.
    """
    extra = _extra("web_search")
    key = _key(extra)
    if not key:
        logger.warning("[exa] no EXA_API_KEY; falling back to DuckDuckGo")
        from deerflow.community.ddg_search.tools import web_search_tool as ddg
        return ddg.invoke({"query": query, "max_results": max_results})
    try:
        n = int(max_results or extra.get("max_results", DEFAULT_MAX_RESULTS))
        data = _post("/search", key, {
            "query": query,
            "type": extra.get("search_type", "auto"),
            "numResults": max(1, min(n, 25)),
            "contents": {"highlights": {"maxCharacters": int(extra.get("contents_max_characters", HIGHLIGHT_CHARS))}},
        }, timeout=float(extra.get("timeout", 30)))
        results = [
            {
                "title": r.get("title") or "",
                "url": r.get("url") or "",
                "published": r.get("publishedDate") or "",
                "content": "\n".join(r.get("highlights") or []) or (r.get("text") or "")[:HIGHLIGHT_CHARS],
            }
            for r in data.get("results", [])
        ]
        return json.dumps({"query": query, "provider": "exa", "total_results": len(results), "results": results}, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error("[exa] search failed: %s", e)
        return f"Error: Exa search failed: {e}"


@tool("web_fetch", parse_docstring=True)
def web_fetch_tool(url: str) -> str:
    """Fetch the contents of a web page at a given URL.
    Only fetch EXACT URLs that have been provided directly by the user or have been returned in results from the web_search and web_fetch tools.
    This tool can NOT access content that requires authentication, such as private Google Docs or pages behind login walls.
    Do NOT add www. to URLs that do NOT have them.
    URLs must include the schema: https://example.com is a valid URL while example.com is an invalid URL.

    Args:
        url: The URL to fetch the contents of.
    """
    extra = _extra("web_fetch")
    key = _key(extra) or _key(_extra("web_search"))
    if key:
        try:
            data = _post("/contents", key, {"urls": [url], "text": {"maxCharacters": int(extra.get("max_characters", FETCH_CHARS))}, "livecrawl": "fallback"},
                         timeout=float(extra.get("timeout", 45)))
            res = data.get("results") or []
            if res and (res[0].get("text") or "").strip():
                return "# " + (res[0].get("title") or "") + "\n\n" + res[0]["text"]
            logger.warning("[exa] empty contents for %s; trying direct GET", url)
        except Exception as e:
            logger.warning("[exa] contents failed for %s: %s; trying direct GET", url, e)
    try:
        r = httpx.get(url, timeout=30, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
        t = r.text
        if "<html" in t[:3000].lower():
            t = re.sub(r"(?is)<(script|style|nav|footer|header).*?</\1>", " ", t)
            t = re.sub(r"(?s)<[^>]+>", " ", t)
            t = re.sub(r"\s+", " ", _html.unescape(t))
        return t[:FETCH_CHARS]
    except Exception as e:
        return f"Error: {e}"
