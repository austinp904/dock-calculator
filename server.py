#!/usr/bin/env python3
"""
Shoreline Plastics Dock Calculator — Server
HTTP file server (8765) + WebSocket AI chat proxy (8766)
"""

import asyncio
import json
import os
import re
import signal
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
FEEDBACK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "feedback.txt")

SYSTEM_PROMPT = """\
You are an AI assistant embedded in Shoreline Plastics' Dock Cost Comparison calculator. \
Your job is to help the user (a company executive) understand and explore dock construction costs.

THE TOOL:
This is an interactive web-based cost comparison between a traditional PT wood dock and \
Shoreline Plastics' composite EcoPile monopile system with pre-assembled bents. \
The user can adjust all input values and see costs update in real time.

AVAILABLE STATE KEYS (you can change these):
- dockLength: Dock length in feet (60–500, default 300)
- dockWidth: Dock width in feet (default 5)
- woodPileSpacing: Feet between wood piles (default 10)
- compositePileSpacing: Feet between composite monopiles (default 20)
- woodPilePrice: Cost per 10"x16' PT wood pile (default $168)
- blackWrapPrice: Black pile wrap per pile (default $75)
- woodStringerPrice: Wood stringer 2x10x12' each (default $31)
- woodDeckingPrice: Wood decking per sq ft (default $3.08)
- wearDeckPrice: WearDeck composite decking per sq ft (default $9.25)
- woodHeaderPrice: Headers & blocking per bent, wood (default $41)
- woodHardwarePrice: Hardware per pile, wood (default $60)
- deckingScrews: Decking screws flat cost (default $1,200)
- ecopile16Price: 16" EcoPile per pile (default $648)
- ecoStringerPrice: Eco stringer 2x12x20' each (default $330)
- slpDeckingPrice: SLP decking per sq ft (default $5.75)
- compositeHeaderPrice: Headers & blocking per bent, composite (default $500)
- compositeHardwarePrice: Hardware per pile, composite (default $40)
- bentAssemblyPrice: Bent pre-assembly per bent (default $400)
- dealerProfitPct: Dealer profit margin % (default 20)
- laborPileInstall: Labor to install one pile (default $250)
- laborHeaders: Labor for headers/bolting per bent (default $100)
- laborStringerInstall: Labor for stringer install per set (default $50)
- laborDecking: Labor for decking per sq ft (default $8)
- laborBentPlacement: Labor to place one bent (default $200)
- weeklyLaborEquipment: Weekly labor & equipment cost (default $9,400)
- mobilization: Mobilization flat cost (default $3,000)
- contractorProfit: Contractor profit flat (default $20,000)
- compositeDeckMultiplier: Multiplier for composite deck upgrade on wood side (default 3)
- ecopileMultiplier: Multiplier for EcoPile upgrade on wood side (default 3)

HOW TO CHANGE VALUES:
To adjust calculator values, include one or more tags in your response like:
  [STATE dockLength=400]
  [STATE woodPilePrice=185]
The browser will parse these out, apply the changes, and recalculate automatically. \
The tags will be hidden from your displayed message.

RULES:
- Keep responses concise (2-4 sentences typical). This is a business tool, not a chatbot.
- When asked to change values, do it and briefly explain the impact.
- For feature requests or UI changes you cannot accomplish with state changes, say you've \
  noted it and Austin will implement it. These get logged to a feedback file.
- You can do math in your head to explain cost differences.
- Always be helpful, professional, and aware that the user is a company executive.
- When presenting numbers, use dollar formatting with commas.
"""

conversation_history = []


def log_exchange(user_msg, ai_msg):
    now = datetime.now().strftime("%Y-%m-%d %I:%M %p")
    with open(FEEDBACK_FILE, "a") as f:
        f.write(f"[{now}] User:\n{user_msg}\n\n")
        f.write(f"[{now}] Sonnet:\n{ai_msg}\n---\n\n")


async def handle_ws(websocket):
    client = anthropic.Anthropic()
    messages = []

    async for raw in websocket:
        try:
            data = json.loads(raw)
            user_msg = data.get("message", "").strip()
            state = data.get("state", {})

            if not user_msg:
                continue

            state_summary = ", ".join(f"{k}={v}" for k, v in state.items())
            content = f"[Current calculator state: {state_summary}]\n\nUser message: {user_msg}"

            messages.append({"role": "user", "content": content})

            response = client.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                messages=messages,
            )

            ai_text = response.content[0].text
            messages.append({"role": "assistant", "content": ai_text})

            # Keep conversation manageable
            if len(messages) > 20:
                messages = messages[-16:]

            state_changes = []
            for match in re.finditer(r"\[STATE\s+(\w+)=([\d.]+)\]", ai_text):
                state_changes.append({"key": match.group(1), "value": float(match.group(2))})

            display_text = re.sub(r"\[STATE\s+\w+=[\d.]+\]", "", ai_text).strip()

            await websocket.send(json.dumps({
                "type": "response",
                "message": display_text,
                "state_changes": state_changes,
            }))

            log_exchange(user_msg, ai_text)

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


def run_http(directory):
    handler = partial(SimpleHTTPRequestHandler, directory=directory)
    httpd = HTTPServer(("0.0.0.0", HTTP_PORT), handler)
    print(f"HTTP server on http://0.0.0.0:{HTTP_PORT}")
    httpd.serve_forever()


async def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: Set ANTHROPIC_API_KEY environment variable first.")
        sys.exit(1)

    serve_dir = os.path.dirname(os.path.abspath(__file__))

    http_thread = Thread(target=run_http, args=(serve_dir,), daemon=True)
    http_thread.start()

    print(f"WebSocket server on ws://0.0.0.0:{WS_PORT}")
    async with websockets.serve(handle_ws, "0.0.0.0", WS_PORT):
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down.")
