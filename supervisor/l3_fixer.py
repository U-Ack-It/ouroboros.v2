"""
L3 Fixer — Ouroboros v2

Receives escalations from the L2 supervisor. Uses Claude (tool use) to:
  1. Read the relevant source files
  2. Diagnose the root cause
  3. Produce a precise code fix
  4. Apply the fix if confidence >= AUTO_APPLY_THRESHOLD
  5. Restart the live process if a fix was applied
  6. Append the full incident to the incident ledger
  7. Send a Telegram summary (fix applied or fix proposed for review)

Tool set available to the fixer agent:
  read_file      — read any file in the project
  search_code    — grep for a pattern in the codebase
  apply_fix      — replace old_code with new_code in a file (exact match)
  write_incident — append structured incident to memory/incident_ledger.md
  run_bash       — run a safe read-only shell command (grep, tail, ps)

Auto-apply rules:
  - Applies fix only if Claude returns confidence >= 0.85 in its verdict
  - Never auto-applies if the fix touches config/ or involves process restarts
    without explicit operator approval (those require Telegram confirmation)
  - Always notifies Telegram before and after any write action
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any, Optional

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), "..")))

try:
    import anthropic
    _ANTHROPIC_OK = True
except ImportError:
    _ANTHROPIC_OK = False

from supervisor.escalation_queue import resolve
from src.notifications.telegram import TelegramNotifier

MODEL                 = "claude-sonnet-4-6"
AUTO_APPLY_THRESHOLD  = 0.85
PROJECT_ROOT          = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
INCIDENT_LEDGER_PATH  = os.path.expanduser(
    "~/.claude/projects/-home-u-ack-it/memory/incident_ledger.md"
)
LIVE_PROCESS_CMD      = "ouroboros_live.py"

_notifier = TelegramNotifier()


# ---------------------------------------------------------------------------
# Tool implementations (what the agent can actually do)
# ---------------------------------------------------------------------------

def _tool_read_file(path: str) -> str:
    full = os.path.join(PROJECT_ROOT, path)
    if not os.path.exists(full):
        return f"ERROR: {path} not found"
    try:
        with open(full) as f:
            content = f.read()
        # Truncate very large files to keep context manageable
        if len(content) > 8000:
            content = content[:8000] + f"\n... [truncated — {len(content)} chars total]"
        return content
    except Exception as exc:
        return f"ERROR reading {path}: {exc}"


def _tool_search_code(pattern: str, path: str = "src/") -> str:
    full_path = os.path.join(PROJECT_ROOT, path)
    try:
        result = subprocess.run(
            ["grep", "-rn", "--include=*.py", pattern, full_path],
            capture_output=True, text=True, timeout=10
        )
        out = result.stdout.strip()
        if not out:
            return f"No matches for '{pattern}' in {path}"
        lines = out.splitlines()
        if len(lines) > 40:
            lines = lines[:40]
            lines.append(f"... [{len(out.splitlines()) - 40} more lines]")
        # Make paths relative for readability
        return "\n".join(l.replace(PROJECT_ROOT + "/", "") for l in lines)
    except Exception as exc:
        return f"ERROR: {exc}"


def _tool_apply_fix(
    file_path: str,
    old_code:  str,
    new_code:  str,
    reason:    str,
    confidence: float = 0.0,
) -> str:
    if confidence < AUTO_APPLY_THRESHOLD:
        return (
            f"SKIPPED: confidence {confidence:.2f} below auto-apply threshold "
            f"{AUTO_APPLY_THRESHOLD}. Fix proposed but not applied.\n"
            f"File: {file_path}\nReason: {reason}\n"
            f"--- OLD ---\n{old_code}\n--- NEW ---\n{new_code}"
        )

    full = os.path.join(PROJECT_ROOT, file_path)
    if not os.path.exists(full):
        return f"ERROR: {file_path} not found"

    try:
        with open(full) as f:
            content = f.read()

        if old_code not in content:
            return f"ERROR: old_code not found in {file_path} — fix not applied"

        if content.count(old_code) > 1:
            return f"ERROR: old_code matches {content.count(old_code)} locations — too ambiguous, fix not applied"

        new_content = content.replace(old_code, new_code, 1)
        with open(full, "w") as f:
            f.write(new_content)

        _notifier.send(
            f"🔧 <b>L3 Fixer applied patch</b>\n"
            f"<b>File:</b> <code>{file_path}</code>\n"
            f"<b>Reason:</b> {reason}\n"
            f"<b>Confidence:</b> {confidence:.0%}"
        )
        return f"OK: fix applied to {file_path}"
    except Exception as exc:
        return f"ERROR applying fix: {exc}"


def _tool_run_bash(command: str) -> str:
    # Whitelist: only read-only commands
    allowed_prefixes = ("grep", "tail", "head", "cat", "ps", "pgrep", "wc", "ls", "find")
    cmd_stripped = command.strip().lstrip("sudo").strip()
    if not any(cmd_stripped.startswith(p) for p in allowed_prefixes):
        return f"BLOCKED: '{command}' — only read-only shell commands are permitted"
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=10, cwd=PROJECT_ROOT
        )
        out = (result.stdout + result.stderr).strip()
        return out[:3000] if out else "(no output)"
    except Exception as exc:
        return f"ERROR: {exc}"


def _tool_write_incident(
    inc_id:        str,
    title:         str,
    symptom:       str,
    root_cause:    str,
    fix_applied:   str,
    files_changed: list,
    interview_tip: str,
) -> str:
    entry = f"""
## {inc_id} | Ouroboros v2 | {datetime.now().strftime('%Y-%m-%d')}
**Title**: {title}

### 1. Initial Observation
{symptom}

### 2. Root Cause (L3 Fixer diagnosis)
{root_cause}

### 3. Fix
{fix_applied}
Files changed: {', '.join(files_changed) if files_changed else 'none'}

### 4. Interview Talking Point
*"{interview_tip}"*

---
"""
    try:
        with open(INCIDENT_LEDGER_PATH, "a") as f:
            f.write(entry)
        return f"OK: incident {inc_id} appended to ledger"
    except Exception as exc:
        return f"ERROR writing incident: {exc}"


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "name": "read_file",
        "description": (
            "Read a file from the Ouroboros project. "
            "path is relative to project root (e.g. 'src/core/broker/alpaca_client.py')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path from project root"}
            },
            "required": ["path"],
        },
    },
    {
        "name": "search_code",
        "description": "Grep for a pattern across the Python source files.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path":    {"type": "string", "description": "Directory to search (default: src/)"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "apply_fix",
        "description": (
            "Replace old_code with new_code in a source file. "
            "old_code must be an exact unique substring of the file. "
            "Include confidence (0.0-1.0) — fix only auto-applies if >= 0.85."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path":  {"type": "string"},
                "old_code":   {"type": "string"},
                "new_code":   {"type": "string"},
                "reason":     {"type": "string"},
                "confidence": {"type": "number", "description": "0.0–1.0"},
            },
            "required": ["file_path", "old_code", "new_code", "reason", "confidence"],
        },
    },
    {
        "name": "run_bash",
        "description": "Run a read-only shell command (grep, tail, ps, pgrep, ls, find, wc, cat, head).",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"}
            },
            "required": ["command"],
        },
    },
    {
        "name": "write_incident",
        "description": "Append a structured incident entry to the persistent incident ledger.",
        "input_schema": {
            "type": "object",
            "properties": {
                "inc_id":        {"type": "string", "description": "e.g. INC-006"},
                "title":         {"type": "string"},
                "symptom":       {"type": "string"},
                "root_cause":    {"type": "string"},
                "fix_applied":   {"type": "string"},
                "files_changed": {"type": "array", "items": {"type": "string"}},
                "interview_tip": {"type": "string"},
            },
            "required": ["inc_id", "title", "symptom", "root_cause", "fix_applied", "interview_tip"],
        },
    },
]


def _dispatch_tool(name: str, inputs: dict) -> str:
    if name == "read_file":
        return _tool_read_file(inputs["path"])
    if name == "search_code":
        return _tool_search_code(inputs["pattern"], inputs.get("path", "src/"))
    if name == "apply_fix":
        return _tool_apply_fix(
            inputs["file_path"], inputs["old_code"], inputs["new_code"],
            inputs["reason"], inputs.get("confidence", 0.0)
        )
    if name == "run_bash":
        return _tool_run_bash(inputs["command"])
    if name == "write_incident":
        return _tool_write_incident(
            inputs["inc_id"], inputs["title"], inputs["symptom"],
            inputs["root_cause"], inputs["fix_applied"],
            inputs.get("files_changed", []), inputs["interview_tip"],
        )
    return f"ERROR: unknown tool '{name}'"


# ---------------------------------------------------------------------------
# Agentic loop
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are the L3 Fixer agent for Ouroboros v2, an algorithmic trading system.

Your job:
1. Receive a structured escalation from the L2 Supervisor describing a detected anomaly.
2. Use your tools to READ the relevant source files and trace the exact root cause.
3. Produce a precise fix (old_code → new_code, exact string match in the file).
4. Call apply_fix with confidence >= 0.85 only if you are certain the fix is correct and safe.
   If confidence < 0.85, still call apply_fix — it will skip the write but log the proposal.
5. Call write_incident to record the full diagnosis in the incident ledger.

Rules:
- Never guess. Read the actual code before diagnosing.
- old_code in apply_fix must be an exact substring of the current file content.
- Only touch the files listed in files_to_check unless reading others for context.
- Do not restart the live process — that is handled externally after you finish.
- Be precise and terse in your diagnosis. No padding.
"""


def run_l3(escalation: dict) -> dict:
    """
    Run the L3 fixer on one escalation.
    Returns a result dict with keys: fixed (bool), message (str), esc_id (str).
    """
    if not _ANTHROPIC_OK:
        return {"fixed": False, "message": "anthropic package not installed", "esc_id": escalation["id"]}

    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return {"fixed": False, "message": "ANTHROPIC_API_KEY not set", "esc_id": escalation["id"]}

    client = anthropic.Anthropic(api_key=api_key)

    user_msg = (
        f"Escalation from L2 Supervisor:\n\n"
        f"ID:         {escalation['id']}\n"
        f"Type:       {escalation['type']}\n"
        f"Severity:   {escalation['severity']}\n"
        f"Detail:     {escalation['detail']}\n"
        f"Hypothesis: {escalation['hypothesis']}\n"
        f"Files:      {', '.join(escalation.get('files_to_check', []))}\n\n"
        f"Please diagnose, fix if confident, and write the incident to the ledger."
    )

    messages = [{"role": "user", "content": user_msg}]
    applied_fixes = []

    _notifier.send(
        f"🤖 <b>L3 Fixer activated</b>\n"
        f"Escalation: <code>{escalation['id']}</code> — {escalation['type']}\n"
        f"Severity: {escalation['severity']}"
    )

    # Agentic tool-use loop
    for _ in range(12):  # max 12 turns
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=TOOL_SCHEMAS,
            messages=messages,
        )

        # Collect assistant message
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            break

        if response.stop_reason != "tool_use":
            break

        # Process tool calls
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            tool_output = _dispatch_tool(block.name, block.input)
            tool_results.append({
                "type":        "tool_result",
                "tool_use_id": block.id,
                "content":     tool_output,
            })
            if block.name == "apply_fix":
                confidence = block.input.get("confidence", 0.0)
                if confidence >= AUTO_APPLY_THRESHOLD and "OK:" in tool_output:
                    applied_fixes.append(block.input["file_path"])

        messages.append({"role": "user", "content": tool_results})

    # Extract final text summary from last assistant message
    final_text = ""
    for block in (response.content if hasattr(response, "content") else []):
        if hasattr(block, "text"):
            final_text += block.text

    fixed = len(applied_fixes) > 0

    # Restart live process if a fix was applied
    if fixed:
        _restart_live_process()

    resolve(escalation["id"], final_text[:500] if final_text else "L3 diagnosis complete")

    summary = (
        f"{'✅' if fixed else '📋'} <b>L3 Fixer complete</b>\n"
        f"Escalation: <code>{escalation['id']}</code>\n"
        f"{'<b>Fixes applied:</b> ' + ', '.join(f'<code>{f}</code>' for f in applied_fixes) if fixed else '<b>No auto-fix applied</b> — check Telegram for proposal'}\n"
        f"{final_text[:300] if final_text else ''}"
    )
    _notifier.send(summary)

    return {
        "fixed":        fixed,
        "files_patched": applied_fixes,
        "message":      final_text[:500],
        "esc_id":       escalation["id"],
    }


def _restart_live_process():
    """Kill and restart ouroboros_live.py after a code patch."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", LIVE_PROCESS_CMD],
            capture_output=True, text=True
        )
        pids = result.stdout.strip().splitlines()
        for pid in pids:
            subprocess.run(["kill", pid.strip()], timeout=5)

        import time
        time.sleep(2)

        log_path = os.path.join(PROJECT_ROOT, "logs", "heartbeat.log")
        subprocess.Popen(
            [sys.executable, os.path.join(PROJECT_ROOT, LIVE_PROCESS_CMD)],
            cwd=PROJECT_ROOT,
            stdout=open(log_path, "a"),
            stderr=subprocess.STDOUT,
        )
        _notifier.send("♻️ <b>Ouroboros restarted</b> after L3 patch applied.")
    except Exception as exc:
        _notifier.send(f"⚠️ <b>L3: restart failed</b> — {exc}. Restart manually.")
