"""Web research tools: DuckDuckGo search + page fetching.

Both are switchable off by policy (network.web_search / network.fetch_page),
in which case tools.configure also stops advertising them. fetch_page is
additionally bounded by network.allowed_domains and the standing refusal to
touch private addresses — see harness/policy.py.
"""

import re
from urllib.parse import urlparse

import requests

from .. import policy
from ..config import settings
from ..llm import truncate_middle


def _disabled(tool: str) -> str:
    return (f"[ERROR] {tool} is disabled by policy "
            f"'{policy.current.name}'. Work without it.")


def web_search(query: str, max_results: int = 5) -> str:
    """DuckDuckGo search — no API key needed. Returns numbered results with
    title, URL and snippet so the model can pick what to fetch_page next."""
    if not policy.current.network.web_search:
        return _disabled("web_search")
    try:
        from ddgs import DDGS  # pip install ddgs
    except ImportError:
        try:
            from duckduckgo_search import DDGS  # older package name
        except ImportError:
            return ("[ERROR] search needs the 'ddgs' package. "
                    "Install it with run_shell: pip install ddgs")
    max_results = max(1, min(int(max_results), 10))
    try:
        results = list(DDGS().text(query, max_results=max_results))
    except Exception as e:
        return f"[ERROR] search failed: {type(e).__name__}: {e}"
    if not results:
        return f"No results for: {query}"
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r.get('title', '')}\n"
                     f"   {r.get('href', '')}\n"
                     f"   {r.get('body', '')}")
    return "\n".join(lines)


_BLOCKED_HOSTS = ("localhost", "127.", "0.0.0.0", "10.", "192.168.", "169.254.", "172.")


def fetch_page(url: str) -> str:
    """Fetch a web page and return its readable text, capped at
    settings.page_text_max chars so one giant page can't blow the context window."""
    if not policy.current.network.fetch_page:
        return _disabled("fetch_page")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return "[ERROR] only http/https URLs are allowed"
    host = (parsed.hostname or "").lower()
    if policy.current.network.block_private_addresses and \
            any(host == b.rstrip(".") or host.startswith(b) for b in _BLOCKED_HOSTS):
        return "[ERROR] refusing to fetch local/private network addresses"
    if not policy.domain_allowed(host):
        allowed = policy.current.network.allowed_domains
        return (f"[ERROR] policy '{policy.current.name}' does not allow "
                f"fetching {host}. Allowed domains: "
                + (", ".join(allowed) if allowed else "(none)"))
    try:
        resp = requests.get(url, timeout=30, headers={
            "User-Agent": "Mozilla/5.0 (compatible; research-agent/1.0)"})
        resp.raise_for_status()
    except requests.RequestException as e:
        return f"[ERROR] fetch failed: {type(e).__name__}: {e}"

    html = resp.text
    text = None
    try:
        import trafilatura  # pip install trafilatura — best-quality extraction
        text = trafilatura.extract(html, url=url)
    except ImportError:
        pass
    if not text:
        # crude fallback: strip scripts/styles/tags, collapse whitespace
        html = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", html)
        text = re.sub(r"(?s)<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return f"[ERROR] no readable text extracted from {url}"
    return f"[{url}]\n" + truncate_middle(text, settings.page_text_max)
