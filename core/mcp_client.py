"""Minimal MCP client over stdio, standard library only.

Keeps one server process alive for the session so each tool call costs
milliseconds instead of a process start. Speaks newline-delimited JSON-RPC 2.0
as MCP's stdio transport defines. A reader thread (not select) keeps it working
on Windows, where select() does not accept pipes.
"""

from __future__ import annotations

import atexit
import itertools
import json
import queue
import subprocess
import threading
from typing import Any, Dict, List, Optional

PROTOCOL_VERSION = "2025-06-18"


class MCPError(RuntimeError):
    """Raised when the server cannot be reached or a tool reports an error."""


class StdioMCPClient:
    def __init__(self, argv: List[str], cwd: Optional[str] = None, env: Optional[Dict[str, str]] = None,
                 start_timeout: float = 60.0):
        self.argv = argv
        self.cwd = cwd
        self.env = env
        self.start_timeout = start_timeout
        self._proc: Optional[subprocess.Popen] = None
        self._ids = itertools.count(1)
        self._pending: Dict[int, "queue.Queue[dict]"] = {}
        self._lock = threading.Lock()
        atexit.register(self.close)

    # ---------------------------------------------------------------- process
    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        if self.running:
            return
        try:
            self._proc = subprocess.Popen(
                self.argv, cwd=self.cwd, env=self.env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
        except OSError as e:
            raise MCPError(f"could not start {self.argv[0]}: {e}")
        threading.Thread(target=self._read_loop, args=(self._proc,), daemon=True).start()
        self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "agentic-harness", "version": "0.1"},
        }, timeout=self.start_timeout)
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        if proc.poll() is None:
            try:
                proc.stdin.close()
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
                proc.wait()
        for stream in (proc.stdin, proc.stdout):
            try:
                stream.close()
            except Exception:
                pass

    # --------------------------------------------------------------- protocol
    def _read_loop(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue  # servers may print non-protocol noise; ignore it
            waiter = self._pending.get(msg.get("id")) if isinstance(msg, dict) else None
            if waiter is not None:
                waiter.put(msg)
        # Process ended: wake every waiter so nobody blocks until timeout.
        for waiter in list(self._pending.values()):
            waiter.put({"error": {"message": "server exited"}})

    def _send(self, message: dict) -> None:
        try:
            self._proc.stdin.write(json.dumps(message) + "\n")
            self._proc.stdin.flush()
        except (OSError, ValueError, AttributeError) as e:
            raise MCPError(f"server pipe closed: {e}")

    def _request(self, method: str, params: dict, timeout: float) -> dict:
        request_id = next(self._ids)
        waiter: "queue.Queue[dict]" = queue.Queue()
        self._pending[request_id] = waiter
        try:
            with self._lock:
                self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            try:
                msg = waiter.get(timeout=timeout)
            except queue.Empty:
                raise MCPError(f"{method} timed out after {timeout:.0f}s")
        finally:
            self._pending.pop(request_id, None)
        if "error" in msg:
            raise MCPError(str(msg["error"].get("message", msg["error"])))
        return msg.get("result", {})

    # -------------------------------------------------------------------- API
    def call_tool(self, name: str, arguments: Dict[str, Any], timeout: float = 120.0) -> str:
        """Calls a tool and returns its text content. Restarts a dead server once."""
        for attempt in (1, 2):
            if not self.running:
                self.start()
            try:
                result = self._request("tools/call", {"name": name, "arguments": arguments}, timeout)
                break
            except MCPError as e:
                if attempt == 2 or self.running:
                    raise
                # The server died mid-call: start a fresh one and retry once.
                self.close()
        text = "\n".join(
            part.get("text", "") for part in result.get("content", []) if part.get("type") == "text"
        ).strip()
        if result.get("isError"):
            raise MCPError(text or f"{name} failed")
        return text
