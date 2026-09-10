"""Base tool abstraction and registry.

Every tool implements `execute` and exposes a standard JSON Schema so the LLM
understands its parameters without proprietary framework layers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional

from core.models import ToolCallRequest, ToolExecutionResult


class BaseTool(ABC):
    """Abstract base class for all deterministic harness tools."""

    name: str
    description: str

    @abstractmethod
    def get_schema(self) -> Dict[str, Any]:
        """Returns the OpenAI/Anthropic compatible JSON Schema specification."""
        pass

    @abstractmethod
    def execute(self, parameters: Dict[str, Any]) -> ToolExecutionResult:
        """Executes the tool with the provided arguments."""
        pass


class ToolDispatcher:
    """Central registry and execution dispatcher for deterministic tools."""

    def __init__(self):
        self._tools: Dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        """Registers a tool in the dispatch catalog."""
        self._tools[tool.name] = tool

    def get_tool(self, name: str) -> Optional[BaseTool]:
        return self._tools.get(name)

    def list_schemas(self) -> List[Dict[str, Any]]:
        """Returns JSON Schema definitions for all registered tools."""
        return [tool.get_schema() for tool in self._tools.values()]

    def execute(self, request: ToolCallRequest) -> ToolExecutionResult:
        """Executes a tool call request, handling missing tools and unexpected errors safely."""
        tool = self._tools.get(request.tool_name)
        if not tool:
            return ToolExecutionResult(
                call_id=request.call_id,
                success=False,
                output=f"TOOL_NOT_FOUND: No tool registered with name '{request.tool_name}'. "
                f"Available tools: {list(self._tools.keys())}",
            )

        try:
            result = tool.execute(request.parameters)
            # Ensure call_id is properly populated
            result.call_id = request.call_id
            return result
        except Exception as e:
            return ToolExecutionResult(
                call_id=request.call_id,
                success=False,
                output=f"TOOL_EXECUTION_EXCEPTION: {type(e).__name__}: {str(e)}",
            )
