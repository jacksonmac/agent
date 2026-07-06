"""Docker MCP Toolkit integration.

Connects to the Docker MCP gateway (`docker mcp gateway run`), which fronts
every MCP server enabled in Docker Desktop's MCP Toolkit over a single
stdio JSON-RPC 2.0 connection. Enable with --mcp; the gateway's tools are
discovered at startup and merged into the normal tool registry.
"""

import json
import subprocess
import time
from typing import Optional

MCP_TOOL_TIMEOUT = 300  # seconds per MCP request (containers can be slow to cold-start)


class MCPGateway:
    """Minimal synchronous MCP client over stdio (newline-delimited JSON-RPC).

    No SDK dependency: the MCP stdio transport is just JSON-RPC 2.0 messages,
    one per line, on the subprocess's stdin/stdout.
    """

    def __init__(self, command: list[str], timeout: int = MCP_TOOL_TIMEOUT):
        self.timeout = timeout
        try:
            self.proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,  # the gateway logs a lot on stderr
                text=True,
                bufsize=1,
            )
        except FileNotFoundError:
            raise RuntimeError(
                f"could not run {command[0]!r} — is Docker installed and on PATH?")
        self._id = 0
        self._initialize()

    # -- wire protocol -------------------------------------------------

    def _send(self, msg: dict) -> None:
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def _read_message(self, deadline: float) -> dict:
        """Read the next JSON-RPC message, skipping any non-JSON noise."""
        import select
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError("MCP gateway did not respond in time")
            # select() works on pipes on POSIX; on Windows fall back to blocking
            try:
                ready, _, _ = select.select([self.proc.stdout], [], [], remaining)
                if not ready:
                    raise TimeoutError("MCP gateway did not respond in time")
            except (OSError, ValueError):
                pass  # non-selectable (e.g. Windows) — just block on readline
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError(
                    "MCP gateway closed its output — is Docker Desktop running "
                    "with the MCP Toolkit enabled?")
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue  # stray log line, ignore

    def _request(self, method: str, params: Optional[dict] = None) -> dict:
        self._id += 1
        req_id = self._id
        self._send({"jsonrpc": "2.0", "id": req_id, "method": method,
                    "params": params or {}})
        deadline = time.time() + self.timeout
        while True:
            msg = self._read_message(deadline)
            if msg.get("id") != req_id:
                continue  # notification or unrelated message — skip it
            if "error" in msg:
                err = msg["error"]
                raise RuntimeError(f"{method}: {err.get('message', err)}")
            return msg.get("result", {})

    # -- MCP lifecycle ---------------------------------------------------

    def _initialize(self) -> None:
        result = self._request("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "agent.py", "version": "1.0"},
        })
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        info = result.get("serverInfo", {})
        print(f"[mcp] connected to {info.get('name', 'gateway')} "
              f"{info.get('version', '')}".rstrip())

    def list_tools(self) -> list[dict]:
        found, cursor = [], None
        while True:
            result = self._request("tools/list", {"cursor": cursor} if cursor else {})
            found.extend(result.get("tools", []))
            cursor = result.get("nextCursor")
            if not cursor:
                return found

    def call_tool(self, name: str, arguments: dict) -> str:
        result = self._request("tools/call", {"name": name,
                                              "arguments": arguments or {}})
        parts = []
        for block in result.get("content", []):
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
            else:
                parts.append(f"[{block.get('type', 'unknown')} content omitted]")
        text = "\n".join(p for p in parts if p) or "[no content returned]"
        if result.get("isError"):
            text = "[ERROR] " + text
        return text

    def close(self) -> None:
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


mcp_gateway: Optional[MCPGateway] = None


def _mcp_tool_proxy(tool_name: str):
    def proxy(**kwargs):
        return mcp_gateway.call_tool(tool_name, kwargs)
    proxy.__name__ = f"mcp_{tool_name}"
    return proxy


def setup_mcp_tools(registry: dict, schemas: list,
                    profile: Optional[str] = None) -> list[str]:
    """Start the Docker MCP gateway, discover its tools, and register them
    into the given registry + schema list. Returns the list of tool names added."""
    global mcp_gateway
    command = ["docker", "mcp", "gateway", "run"]
    if profile:
        command += ["--profile", profile]
    print(f"[mcp] starting gateway: {' '.join(command)}")
    mcp_gateway = MCPGateway(command)

    added = []
    for t in mcp_gateway.list_tools():
        name = t.get("name")
        if not name:
            continue
        if name in registry:
            print(f"[mcp] skipping tool '{name}' — name clashes with a built-in tool")
            continue
        schema = t.get("inputSchema") or {"type": "object", "properties": {}}
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        registry[name] = _mcp_tool_proxy(name)
        schemas.append({
            "type": "function",
            "function": {
                "name": name,
                "description": t.get("description", "") or f"MCP tool {name}",
                "parameters": schema,
            },
        })
        added.append(name)
    print(f"[mcp] registered {len(added)} tool(s): {', '.join(added) or '(none)'}")
    return added
