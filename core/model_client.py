"""Raw Model Gateway with zero third-party dependencies.

Communicates via standard library urllib.request or provides scripted deterministic
mocks for simulation and automated testing.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from core.models import ModelTurnResponse, ToolCallRequest


class BaseModelClient(ABC):
    """Abstract interface for model inference providers."""

    @abstractmethod
    def generate_turn(
        self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None
    ) -> ModelTurnResponse:
        """Executes a single inference turn given compiled context and tool schemas."""
        pass


class MockModelClient(BaseModelClient):
    """Scripted deterministic model client for offline testing and verification."""

    def __init__(self, responses: Optional[List[ModelTurnResponse]] = None):
        self.responses: List[ModelTurnResponse] = responses or []
        self.call_history: List[List[Dict[str, Any]]] = []
        self._current_index = 0

    def add_response(self, response: ModelTurnResponse) -> None:
        self.responses.append(response)

    def generate_turn(
        self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None
    ) -> ModelTurnResponse:
        self.call_history.append(messages)
        if self._current_index < len(self.responses):
            resp = self.responses[self._current_index]
            self._current_index += 1
            return resp
        return ModelTurnResponse(
            text="Mock agent completed execution: no more scripted turns.",
            prompt_tokens=50,
            completion_tokens=20,
            total_tokens=70,
        )


class RawOpenAIClient(BaseModelClient):
    """Zero-framework HTTP client for OpenAI-compatible inference endpoints."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model_name: str = "gpt-4o-mini",
        timeout: int = 60,
    ):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        self.model_name = model_name
        self.timeout = timeout

    def generate_turn(
        self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None
    ) -> ModelTurnResponse:
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        payload: Dict[str, Any] = {
            "model": self.model_name,
            "messages": messages,
            "temperature": 0.0,
        }

        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        data_bytes = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data_bytes, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                result_json = json.loads(resp.read().decode("utf-8"))

            choice = result_json.get("choices", [{}])[0]
            message = choice.get("message", {})
            usage = result_json.get("usage", {})

            # Extract tool calls if emitted
            raw_tool_calls = message.get("tool_calls", [])
            parsed_calls: List[ToolCallRequest] = []
            for tc in raw_tool_calls:
                fn = tc.get("function", {})
                raw_args = fn.get("arguments", "{}")
                try:
                    params = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except Exception:
                    params = {"raw_args": raw_args}

                parsed_calls.append(
                    ToolCallRequest(
                        call_id=tc.get("id", f"call_{len(parsed_calls)}"),
                        tool_name=fn.get("name", ""),
                        parameters=params,
                    )
                )

            return ModelTurnResponse(
                text=message.get("content"),
                tool_calls=parsed_calls,
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                total_tokens=usage.get("total_tokens", 0),
            )

        except urllib.error.HTTPError as e:
            err_msg = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Model API HTTP {e.code} Error: {err_msg}")
        except Exception as e:
            raise RuntimeError(f"Model API Connection Failure: {type(e).__name__}: {str(e)}")
