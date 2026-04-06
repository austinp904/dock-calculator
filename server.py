#!/usr/bin/env python3
"""
Shoreline Plastics Dock Calculator — Server
HTTP file server (8765) + WebSocket AI chat with code-editing tools (8766)
"""

import asyncio
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from threading import Thread

import anthropic
import websockets

ANTHROPIC_MODEL = "claude-sonnet-4-20250514"
HTTP_PORT = 8765
WS_PORT = 8766
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_PATH = os.path.join(PROJECT_DIR, "index.html")
SPREADSHEET_REF = os.path.join(PROJECT_DIR, "spreadsheet-reference.txt")
FEEDBACK_FILE = os.path.join(PROJECT_DIR, "feedback.txt")

# All connected WebSocket clients (for broadcasting reload)
connected_clients = set()

TOOLS = [
    {
        "name": "read_file",
        "description": "Read the current source code of index.html. Always call this before making edits so you understand the current structure.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "edit_file",
        "description": (
            "Edit index.html by finding exact text and replacing it. "
            "The old_text must match EXACTLY (including whitespace/indentation). "
            "Make small, focused edits — one logical change per call. "
            "You can call this multiple times for multi-part changes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "old_text": {
                    "type": "string",
                    "description": "Exact text to find in index.html (must match precisely)",
                },
                "new_text": {
                    "type": "string",
                    "description": "Replacement text",
                },
            },
            "required": ["old_text", "new_text"],
        },
    },
    {
        "name": "git_log",
        "description": "View recent git commit history to understand what changes have been made.",
        "input_schema": {
            "type": "object",
            "properties": {
                "count": {
                    "type": "integer",
                    "description": "Number of recent commits to show (default 10)",
                },
            },
            "required": [],
        },
    },
    {
        "name": "read_spreadsheet",
        "description": (
            "Read the original Excel spreadsheet ('comparison Composite vs wood substructure.xlsx') "
            "that the executive uses. This is the ORIGINAL reference document — call this when the user "
            "asks about 'my spreadsheet', 'my sheet', 'the Excel file', or references data/formulas "
            "from the original comparison. It contains two sheets: 'Comparison' (the main cost model) "
            "and 'Labor & equipment' (crew rates and weekly costs)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "revert_last_commit",
        "description": "Revert the most recent commit, undoing the last change. Use when something broke.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]

SYSTEM_PROMPT = """\
You are an AI developer embedded in Shoreline Plastics' Dock Cost Comparison web tool. \
You help a company executive modify and improve the calculator by editing its source code directly.

THE TOOL:
A single-page web app (index.html) that compares traditional PT wood dock construction costs \
against Shoreline Plastics' composite EcoPile monopile system. It has a dashboard view, \
spreadsheet view, settings panel, bar charts, and PDF export.

ORIGINAL SPREADSHEET:
The executive built this calculator based on an Excel spreadsheet called \
"comparison Composite vs wood substructure.xlsx". If the user mentions "my spreadsheet", \
"my sheet", "the Excel file", "the original numbers", or references specific rows/formulas \
from their comparison document, use the read_spreadsheet tool to look up the original data. \
This is their source of truth — they may want to compare the web tool's numbers against it \
or ask how specific calculations were derived. The spreadsheet has two sheets:
  - "Comparison" — the main cost model with wood vs. composite side-by-side
  - "Labor & equipment" — crew rates, insurance, equipment costs, daily/weekly totals

YOUR CAPABILITIES:
You can read and edit the source code of index.html using the provided tools. \
Every edit you make is automatically committed to git with your description, so changes are tracked and reversible. \
You can also read the original Excel spreadsheet to answer questions about the source data.

WORKFLOW:
1. When the user asks for a change, ALWAYS call read_file first to see the current code.
2. Plan your edits carefully — match existing code style and indentation exactly.
3. Use edit_file to make changes. Use multiple calls for multi-part edits.
4. After your edits, the page auto-reloads in the user's browser.
5. If something breaks, use revert_last_commit to undo.
6. When the user asks about "the spreadsheet" or original data, call read_spreadsheet.

RULES:
- Keep the existing design language (navy/teal theme, Plus Jakarta Sans font, card-based layout).
- Make small, safe changes. Don't rewrite large sections unnecessarily.
- Match indentation exactly — the file uses 2-space indentation for HTML and 4-space for JS.
- When adding CSS, add it in the appropriate section (look for the section comments like /* ─── HEADER ─── */).
- When adding JS, add it near related functions.
- Test your string matches carefully — if old_text isn't found exactly, the edit will fail.
- Be conversational: explain what you're doing and why, like a colleague pair-programming.
- If the request is ambiguous, ask clarifying questions before editing.
- Keep responses concise.

AVAILABLE STATE KEYS (for calculator value changes, you can also just edit the DEFAULTS object):
dockLength, dockWidth, woodPileSpacing, compositePileSpacing, woodPilePrice, blackWrapPrice, \
woodStringerPrice, woodDeckingPrice, wearDeckPrice, woodHeaderPrice, woodHardwarePrice, \
deckingScrews, ecopile16Price, ecoStringerPrice, slpDeckingPrice, compositeHeaderPrice, \
compositeHardwarePrice, bentAssemblyPrice, dealerProfitPct, laborPileInstall, laborHeaders, \
laborStringerInstall, laborDecking, laborBentPlacement, weeklyLaborEquipment, mobilization, \
contractorProfit, compositeDeckMultiplier, ecopileMultiplier
"""


def git_commit(message):
    """Stage all changes and commit."""
    try:
        subprocess.run(["git", "add", "-A"], cwd=PROJECT_DIR, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", message],
            cwd=PROJECT_DIR, capture_output=True, check=True,
        )
        return True
    except subprocess.CalledProcessError as e:
        return False


def git_push():
    """Push to origin (best-effort, non-blocking)."""
    try:
        subprocess.run(["git", "push"], cwd=PROJECT_DIR, capture_output=True, timeout=10)
    except Exception:
        pass


def handle_tool(name, input_data):
    """Execute a tool call and return the result string."""
    if name == "read_file":
        with open(INDEX_PATH, "r") as f:
            content = f.read()
        lines = content.split("\n")
        numbered = "\n".join(f"{i+1:5d}| {line}" for i, line in enumerate(lines))
        return f"index.html ({len(lines)} lines):\n{numbered}"

    elif name == "edit_file":
        old_text = input_data["old_text"]
        new_text = input_data["new_text"]

        with open(INDEX_PATH, "r") as f:
            content = f.read()

        if old_text not in content:
            return (
                "ERROR: old_text not found in index.html. "
                "Make sure it matches exactly, including whitespace and indentation. "
                "Call read_file to see the current contents."
            )

        count = content.count(old_text)
        if count > 1:
            return (
                f"ERROR: old_text found {count} times. "
                "Make your old_text more specific (include more surrounding context) "
                "so it matches exactly once."
            )

        new_content = content.replace(old_text, new_text, 1)
        with open(INDEX_PATH, "w") as f:
            f.write(new_content)

        return "Edit applied successfully."

    elif name == "read_spreadsheet":
        try:
            with open(SPREADSHEET_REF, "r") as f:
                return f.read()
        except FileNotFoundError:
            return "ERROR: spreadsheet-reference.txt not found."

    elif name == "git_log":
        count = input_data.get("count", 10)
        result = subprocess.run(
            ["git", "log", f"-{count}", "--oneline", "--format=%h %s (%cr)"],
            cwd=PROJECT_DIR, capture_output=True, text=True,
        )
        return result.stdout or "No commits found."

    elif name == "revert_last_commit":
        result = subprocess.run(
            ["git", "revert", "HEAD", "--no-edit"],
            cwd=PROJECT_DIR, capture_output=True, text=True,
        )
        if result.returncode == 0:
            git_push()
            return "Last commit reverted successfully. The page will reload."
        else:
            return f"Revert failed: {result.stderr}"

    return f"Unknown tool: {name}"


def log_exchange(user_msg, ai_msg):
    now = datetime.now().strftime("%Y-%m-%d %I:%M %p")
    with open(FEEDBACK_FILE, "a") as f:
        f.write(f"[{now}] User:\n{user_msg}\n\n")
        f.write(f"[{now}] Sonnet:\n{ai_msg}\n---\n\n")


async def broadcast_reload():
    """Tell all connected browsers to reload the page."""
    msg = json.dumps({"type": "reload"})
    for ws in list(connected_clients):
        try:
            await ws.send(msg)
        except Exception:
            connected_clients.discard(ws)


async def handle_ws(websocket):
    connected_clients.add(websocket)
    client = anthropic.Anthropic()
    messages = []

    try:
        async for raw in websocket:
            try:
                data = json.loads(raw)
                user_msg = data.get("message", "").strip()
                state = data.get("state", {})

                if not user_msg:
                    continue

                state_summary = ", ".join(f"{k}={v}" for k, v in state.items())
                content = f"[Current calculator state: {state_summary}]\n\nUser: {user_msg}"

                messages.append({"role": "user", "content": content})

                edits_made = False
                edit_descriptions = []
                final_text = ""

                # Tool-use loop
                while True:
                    response = client.messages.create(
                        model=ANTHROPIC_MODEL,
                        max_tokens=4096,
                        system=SYSTEM_PROMPT,
                        tools=TOOLS,
                        messages=messages,
                    )

                    # Collect text blocks and tool calls
                    text_parts = []
                    tool_calls = []
                    for block in response.content:
                        if block.type == "text":
                            text_parts.append(block.text)
                        elif block.type == "tool_use":
                            tool_calls.append(block)

                    if text_parts:
                        final_text = "\n".join(text_parts)

                    if not tool_calls:
                        messages.append({"role": "assistant", "content": response.content})
                        break

                    # Process tool calls
                    messages.append({"role": "assistant", "content": response.content})
                    tool_results = []
                    for tc in tool_calls:
                        result = handle_tool(tc.name, tc.input)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tc.id,
                            "content": result,
                        })
                        if tc.name == "edit_file" and "successfully" in result:
                            edits_made = True
                            desc = tc.input.get("description", user_msg)
                            edit_descriptions.append(desc)

                        # Send progress to browser
                        if tc.name == "read_file":
                            await websocket.send(json.dumps({
                                "type": "progress",
                                "message": "Reading current source code...",
                            }))
                        elif tc.name == "read_spreadsheet":
                            await websocket.send(json.dumps({
                                "type": "progress",
                                "message": "Checking original spreadsheet...",
                            }))
                        elif tc.name == "edit_file":
                            status = "Applied edit" if "successfully" in result else "Edit failed"
                            await websocket.send(json.dumps({
                                "type": "progress",
                                "message": f"{status}",
                            }))
                        elif tc.name == "revert_last_commit":
                            await websocket.send(json.dumps({
                                "type": "progress",
                                "message": "Reverted last change",
                            }))

                    messages.append({"role": "user", "content": tool_results})

                    if response.stop_reason == "end_turn":
                        break

                # Commit and reload if edits were made
                if edits_made:
                    commit_msg = f"AI edit: {user_msg[:80]}"
                    if edit_descriptions:
                        commit_msg += "\n\n" + "\n".join(f"- {d}" for d in edit_descriptions)
                    git_commit(commit_msg)
                    git_push()
                    await broadcast_reload()

                # Strip any [STATE ...] tags from display text
                display_text = re.sub(r"\[STATE\s+\w+=[\d.]+\]", "", final_text).strip()

                # Parse state changes from text
                state_changes = []
                for match in re.finditer(r"\[STATE\s+(\w+)=([\d.]+)\]", final_text):
                    state_changes.append({"key": match.group(1), "value": float(match.group(2))})

                await websocket.send(json.dumps({
                    "type": "response",
                    "message": display_text or "Changes applied.",
                    "state_changes": state_changes,
                    "edits_made": edits_made,
                }))

                log_exchange(user_msg, final_text)

                # Keep conversation manageable
                if len(messages) > 30:
                    messages = messages[-20:]

            except anthropic.APIError as e:
                await websocket.send(json.dumps({
                    "type": "error",
                    "message": f"API error: {e.message}",
                }))
            except Exception as e:
                await websocket.send(json.dumps({
                    "type": "error",
                    "message": f"Server error: {str(e)}",
                }))
    finally:
        connected_clients.discard(websocket)


def run_http(directory):
    handler = partial(SimpleHTTPRequestHandler, directory=directory)
    httpd = HTTPServer(("0.0.0.0", HTTP_PORT), handler)
    print(f"HTTP server on http://0.0.0.0:{HTTP_PORT}", flush=True)
    httpd.serve_forever()


async def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: Set ANTHROPIC_API_KEY environment variable first.")
        sys.exit(1)

    http_thread = Thread(target=run_http, args=(PROJECT_DIR,), daemon=True)
    http_thread.start()

    print(f"WebSocket server on ws://0.0.0.0:{WS_PORT}", flush=True)
    print(f"Project: {PROJECT_DIR}", flush=True)
    print("Sonnet can now read/edit index.html with git tracking.", flush=True)

    async with websockets.serve(handle_ws, "0.0.0.0", WS_PORT):
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down.")
