#!/usr/bin/env python3
"""SLC Context Sync Hook for Hermes Agent.

Reads JSON from stdin (hook payload), calls SLC MCP update_context(),
returns context injection to stdout.

Usage:
  pre_api_request: loads fresh context from SLC
  post_api_request: saves summary back to SLC
"""
import json
import sys
import os
import urllib.request
import urllib.error

SLC_URL = os.environ.get("SLC_MCP_URL", "http://agent-sales-0:3000/mcp")
SEAT_ID = os.environ.get("SEAT_ID", "default")


def call_mcp(method, params=None):
    """Call SLC MCP via HTTP."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params or {}
    }
    try:
        req = urllib.request.Request(
            SLC_URL,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}


def load_context():
    """Load current context from SLC."""
    result = call_mcp("tools/call", {
        "name": "update_context",
        "arguments": {}
    })
    if "result" in result:
        content = result["result"].get("content", [])
        if content:
            return content[0].get("text", "")
    return ""


def save_context(summary, changes=None, next_steps=None):
    """Save context summary to SLC."""
    args = {"summary": summary}
    if changes:
        args["changes"] = changes
    if next_steps:
        args["next_steps"] = next_steps
    result = call_mcp("tools/call", {
        "name": "update_context",
        "arguments": args
    })
    return result


def main():
    try:
        payload = json.loads(sys.stdin.read())
    except Exception:
        payload = {}

    hook_event = payload.get("hook_event_name", "")

    if hook_event == "pre_api_request":
        # Load fresh context before LLM call
        context = load_context()
        if context:
            print(json.dumps({"context": context}))
        else:
            print(json.dumps({}))
        return

    if hook_event == "post_api_request":
        # Extract assistant response and save summary
        assistant_text = payload.get("extra", {}).get("assistant_text", "")
        if assistant_text and len(assistant_text) > 50:
            summary = assistant_text[:200] + "..." if len(assistant_text) > 200 else assistant_text
            save_context(summary=summary)
        print(json.dumps({}))
        return

    # Default: no-op
    print(json.dumps({}))


if __name__ == "__main__":
    main()
