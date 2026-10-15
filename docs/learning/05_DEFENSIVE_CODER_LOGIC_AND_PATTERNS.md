# 05. Defensive Coder Logic & Reliability Patterns

> **Goal**: Study the core implementation patterns and defensive programming techniques that prevent code corruption, security holes, and runtime lockups.

---

## 1. Targeted Patching: Search & Replace vs. Whole File vs. Line Diffs

Why do commercial coding agents (Cursor, Claude Code, Aider) use **targeted search-and-replace blocks** instead of unified diffs or whole file rewrites?

| Mutation Strategy | Pros | Fatal Flaws |
| :--- | :--- | :--- |
| **Whole File Rewrite** | Simple to implement (`write_file`). | Burns tokens quadratically; drops unrelated code; strips comments; loses user changes. |
| **Line Number Diffs** (e.g. `replace line 40-52`) | Compact output. | **Fragile**: If an earlier turn added 2 lines at line 10, line 40 has shifted to 42. Edits land on the wrong code! |
| **Unified Diff (`patch -p0`)** | Standard Unix format. | LLMs frequently make off-by-one errors in hunk headers (`@@ -42,7 +42,8 @@`), causing patch rejects. |
| **Exact Block Search & Replace** | **Robust & Self-Validating**: LLMs are great at quoting exact code snippets. Context lines provide unique targeting. | **Winner**. Used in production. |

### The Targeted Patch Engine Implementation

```python
def apply_targeted_patch(filepath: str, search_block: str, replace_block: str) -> None:
    """
    Applies an exact search-and-replace block to a file.
    Enforces strict uniqueness checks.
    """
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # Invariant 1: Block must exist
    occurrences = content.count(search_block)
    if occurrences == 0:
        raise ValueError(
            f"Search block not found in {filepath}.\n"
            "Ensure indentation, comments, and line breaks match the source file exactly."
        )

    # Invariant 2: Block must be unique
    if occurrences > 1:
        raise ValueError(
            f"Ambiguous patch: Search block matched {occurrences} times in {filepath}.\n"
            "Include more surrounding lines of context to create a unique target."
        )

    # Perform replacement
    new_content = content.replace(search_block, replace_block, 1)

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(new_content)
```

---

## 2. AST Pre-Execution Syntax Guard

Never trust that model-generated Python code compiles. By validating the Abstract Syntax Tree (AST) before committing to disk, the harness prevents syntax corruption from propagating into subsequent turns.

```python
import ast
from typing import Optional, Tuple

def validate_python_code(code: str) -> Tuple[bool, Optional[str]]:
    """
    Parses code into an AST without executing it.
    Catches indentation errors, unclosed strings, and missing tokens.
    """
    try:
        ast.parse(code)
        return True, None
    except SyntaxError as e:
        return False, (
            f"AST SyntaxError on line {e.lineno}, column {e.offset}:\n"
            f"  {e.text.strip() if e.text else ''}\n"
            f"Error message: {e.msg}"
        )
```

If `validate_python_code` fails:
1. The harness restores the previous file version.
2. The harness formats a synthetic error message for the LLM.
3. The LLM receives the exact line number, column offset, and compiler hint to self-heal.

---

## 3. Path Traversal & Filesystem Jail

An agent must be strictly confined to the project workspace directory.

```python
import os

class SafePathResolver:
    def __init__(self, workspace_root: str):
        self.workspace_root = os.path.realpath(workspace_root)

    def resolve(self, requested_path: str) -> str:
        """
        Resolves a user or LLM provided path.
        Raises PermissionError if path attempts to escape the workspace root.
        """
        # Resolve symlinks and relative '../' elements
        full_path = os.path.realpath(os.path.join(self.workspace_root, requested_path))

        # Security check: workspace_root must be the common ancestor
        try:
            common = os.path.commonpath([self.workspace_root, full_path])
        except ValueError:
            # Different drive on Windows (e.g. C:\ vs D:\)
            raise PermissionError(f"Access Denied: Path {requested_path} is on a different drive.")

        if common != self.workspace_root:
            raise PermissionError(
                f"Access Denied: Path {requested_path} resolves outside workspace {self.workspace_root}."
            )

        return full_path
```

---

## 4. Subprocess Execution Without Deadlocks

When running arbitrary shell commands (`pytest`, `npm test`, `git status`), standard naive subprocess calls can freeze or hang indefinitely:

```python
import subprocess
from typing import Tuple

def execute_shell_command(command: str, cwd: str, timeout_seconds: int = 30) -> Tuple[int, str]:
    """
    Executes a command safely with:
    - Fixed working directory
    - Hard timeout enforcement
    - Unified stdout/stderr capture
    - Non-deadlocking pipe communication
    """
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # Merge stderr into stdout stream
            text=True,
            encoding="utf-8",
            errors="replace"
        )
        
        stdout, _ = proc.communicate(timeout=timeout_seconds)
        exit_code = proc.returncode
        return exit_code, stdout
        
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, _ = proc.communicate()
        return -1, f"COMMAND_TIMEOUT: Execution exceeded {timeout_seconds}s limit and was killed.\n{stdout}"
    except Exception as e:
        return -1, f"COMMAND_FAILED: {str(e)}"
```

---

## 5. Shadow Git Stash Rollback Checkpoint

Before an agent begins mutating files in a turn, the harness can record a shadow git tree. If an irrecoverable failure or user abort occurs, the harness rolls back in milliseconds:

```python
def create_shadow_checkpoint(workspace_root: str) -> Optional[str]:
    """Creates a temporary tree ref in git without affecting working index."""
    code, tree_id = execute_shell_command("git write-tree", cwd=workspace_root)
    if code == 0:
        return tree_id.strip()
    return None

def rollback_to_checkpoint(workspace_root: str, tree_id: str) -> bool:
    """Restores working directory to shadow tree."""
    code, _ = execute_shell_command(f"git read-tree {tree_id} && git checkout-index -a -f", cwd=workspace_root)
    return code == 0
```
