"""CLI entrypoint for AI Harness (core-harness).

Run autonomous coding loops locally with zero framework dependencies.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid

from core.evaluator import Evaluator
from core.loop import HarnessLoop
from core.memory import MemoryEngine
from core.model_client import MockModelClient, RawOpenAIClient
from core.models import ModelTurnResponse, ToolCallRequest
from core.permissions import GateConfigError, PermissionGate
from core.project_context import build_project_instructions, hide_runtime_dir_from_git
from core.state import StateStore
from core.telemetry import TelemetryLogger
from core.tools.base import ToolDispatcher
from core.tools.fs_tools import (
    CreateFileTool,
    ListDirTool,
    ReadFileTool,
    SafePathResolver,
    SearchDirectoryTool,
)
from core.tools.patcher import PatchFileTool
from core.tools.shell import ExecuteCommandTool
from planes.context.graph import CodeGraph, graph_tools
from planes.context.index import RepoIndex
from planes.context.tools import FindSymbolTool, MapRepositoryTool
from planes.verify.perf import PerfCheckTool
from planes.verify.review import ReviewChangeTool


def print_banner():
    banner = r"""
======================================================================
   ____ ___  ____  _____     _   _    _    ____  _   _ _____ ____ ____  
  / ___/ _ \|  _ \| ____|   | | | |  / \  |  _ \| \ | | ____/ ___/ ___| 
 | |  | | | | |_) |  _|     | |_| | / _ \ | |_) |  \| |  _| \___ \___ \ 
 | |__| |_| |  _ <| |___    |  _  |/ ___ \|  _ <| |\  | |___ ___) |__) |
  \____\___/|_| \_\_____|   |_| |_/_/   \_\_| \_\_| \_|_____|____/____/ 
       Deterministic Local Code Agent Runtime (Zero Frameworks)
======================================================================
"""
    print(banner)


def setup_harness(
    workspace_root: str,
    max_turns: int = 25,
    max_token_budget: int = 200_000,
    max_spend_usd: float = 0.50,
    auto_approve: bool = False,
    mock_mode: bool = False,
    model_name: str = "gpt-4o-mini",
) -> HarnessLoop:
    """Configures and wires all subsystems together."""
    workspace = os.path.realpath(workspace_root)
    os.makedirs(os.path.join(workspace, ".harness"), exist_ok=True)
    hide_runtime_dir_from_git(workspace)

    session_id = f"session_{uuid.uuid4().hex[:8]}"
    state_store = StateStore.create_new(
        session_id=session_id,
        max_turns=max_turns,
        max_token_budget=max_token_budget,
        max_spend_usd=max_spend_usd,
        storage_path=os.path.join(workspace, ".harness", "session.json"),
    )

    resolver = SafePathResolver(workspace)
    tools = ToolDispatcher()
    tools.register(ReadFileTool(resolver))
    tools.register(CreateFileTool(resolver))
    tools.register(ListDirTool(resolver))
    tools.register(SearchDirectoryTool(resolver))
    tools.register(PatchFileTool(resolver))
    repo_index = RepoIndex(workspace)
    tools.register(MapRepositoryTool(repo_index))
    tools.register(FindSymbolTool(repo_index))
    # Optional: call-graph tools when codebase-memory-mcp is installed. One
    # server is shared by the graph tools and review_change.
    code_graph = CodeGraph(workspace)
    for tool in graph_tools(workspace, code_graph):
        tools.register(tool)
    tools.register(ReviewChangeTool(workspace, code_graph if code_graph.available else None))
    tools.register(PerfCheckTool(workspace))
    tools.register(
        ExecuteCommandTool(resolver, raw_log_dir=os.path.join(workspace, ".harness", "raw"))
    )

    permissions = PermissionGate(workspace, auto_approve_all=auto_approve)
    evaluator = Evaluator(resolver)
    memory = MemoryEngine(project_instructions=build_project_instructions(workspace))
    telemetry = TelemetryLogger(os.path.join(workspace, ".harness", "events.jsonl"))

    if mock_mode:
        # Default mock script for testing CLI without API keys
        mock = MockModelClient()
        mock.add_response(
            ModelTurnResponse(
                text="Listing workspace files...",
                tool_calls=[
                    ToolCallRequest(
                        call_id="call_mock_1",
                        tool_name="list_dir",
                        parameters={"directory_path": "."},
                    )
                ],
                prompt_tokens=50,
                completion_tokens=20,
            )
        )
        mock.add_response(
            ModelTurnResponse(
                text="Workspace inspected successfully. Ready for tasks.",
                tool_calls=[],
                prompt_tokens=80,
                completion_tokens=15,
            )
        )
        client = mock
    else:
        client = RawOpenAIClient(model_name=model_name)

    return HarnessLoop(
        state_store=state_store,
        memory=memory,
        model_client=client,
        permissions=permissions,
        tools=tools,
        evaluator=evaluator,
        telemetry=telemetry,
    )


def main():
    print_banner()

    parser = argparse.ArgumentParser(description="Deterministic AI Code Harness Runtime")
    parser.add_argument("--workspace", default=".", help="Target workspace root directory")
    parser.add_argument("--prompt", help="Initial task prompt")
    parser.add_argument("--max-turns", type=int, default=25, help="Maximum execution turns")
    parser.add_argument("--token-budget", type=int, default=200_000, help="Maximum token budget")
    parser.add_argument("--max-spend", type=float, default=0.50, help="Maximum dollar spend ceiling")
    parser.add_argument("--auto-approve", action="store_true", help="Auto-approve file writes AND shell commands (only the denylist still applies)")
    parser.add_argument("--mock", action="store_true", help="Run with deterministic mock client")
    parser.add_argument("--model", default="gpt-4o-mini", help="Model name for OpenAI-compatible endpoint")

    args = parser.parse_args()

    prompt = args.prompt
    if not prompt:
        try:
            prompt = input("Enter your task prompt: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting.")
            sys.exit(0)

    if not prompt:
        print("Empty prompt provided. Exiting.")
        sys.exit(1)

    print(f"\n[INIT] Target Workspace: {os.path.realpath(args.workspace)}")
    print(f"[INIT] Model Mode: {'MOCK SIMULATION' if args.mock else args.model}")
    print(f"[INIT] Hard Ceiling: max {args.max_turns} turns | budget ${args.max_spend:.2f}\n")

    try:
        loop = setup_harness(
            workspace_root=args.workspace,
            max_turns=args.max_turns,
            max_token_budget=args.token_budget,
            max_spend_usd=args.max_spend,
            auto_approve=args.auto_approve,
            mock_mode=args.mock,
            model_name=args.model,
        )
    except GateConfigError as e:
        # Fail closed: never run with gates we could not read.
        print(f"[REFUSED] {e}\nFix the file or remove it, then run again.")
        sys.exit(2)

    final_state = loop.run(prompt)

    print("\n" + "=" * 70)
    print("SESSION SUMMARY")
    print(f"Status:            {'TERMINATED' if final_state.is_terminal else 'ACTIVE'}")
    print(f"Reason:            {final_state.termination_reason}")
    print(f"Turns Completed:   {final_state.current_turn} / {final_state.max_turns}")
    print(f"Tokens Consumed:   {final_state.tokens_consumed:,} / {final_state.max_token_budget:,}")
    print(f"Estimated Spend:   ${final_state.spend_usd:.4f} / ${final_state.max_spend_usd:.2f}")
    print(f"Modified Files:    {final_state.dirty_files if final_state.dirty_files else 'None'}")
    print(f"Event Log:         {os.path.join(args.workspace, '.harness', 'events.jsonl')}")
    print("=" * 70)


if __name__ == "__main__":
    main()
