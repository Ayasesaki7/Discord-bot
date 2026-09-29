from __future__ import annotations

import json
import sys
import time


failed_once_sessions: set[str] = set()
wedged_sessions: set[str] = set()


def send(payload: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


for raw_line in sys.stdin:
    request = json.loads(raw_line)
    request_id = request["id"]
    method = request["method"]
    params = request.get("params") or {}
    if method == "initialize":
        send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "serverInfo": {
                        "name": "deepseek-harness-sdk-runtime",
                        "version": "test",
                    }
                },
            }
        )
        continue
    if method == "session/prompt":
        session_id = params["sessionId"]
        text = params["contentBlocks"][0]["text"]
        if text == "fail-once-until-cancel":
            if session_id not in failed_once_sessions:
                failed_once_sessions.add(session_id)
                wedged_sessions.add(session_id)
            if session_id in wedged_sessions:
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {
                            "code": -32001,
                            "message": "simulated wedged session",
                        },
                    }
                )
                continue
        reply = f"echo:{text}"
        send({"jsonrpc": "2.0", "id": request_id, "result": {"messageId": "m-1"}})
        send({"method": "session.status", "params": {"sessionId": session_id, "status": "running"}})
        if text == "hang-until-cancelled":
            continue
        if text == "idle-before-turn-end":
            send({"method": "session.status", "params": {"sessionId": session_id, "status": "idle"}})
            time.sleep(0.05)
        if text == "tool-loop-usage":
            send(
                {
                    "method": "session.event",
                    "params": {
                        "sessionId": session_id,
                        "event": {
                            "type": "assistant/message",
                            "data": {
                                "message": {
                                    "content": [
                                        {
                                            "type": "tool-call",
                                            "id": "call-1",
                                            "name": "test_tool",
                                            "arguments": "{}",
                                        }
                                    ]
                                },
                                "usage": {
                                    "inputTokens": 30,
                                    "cacheReadTokens": 70,
                                    "outputTokens": 50,
                                },
                            },
                        },
                    },
                }
            )
        send(
            {
                "method": "session.event",
                "params": {
                    "sessionId": session_id,
                    "event": {
                        "type": "assistant/chunk",
                        "data": {"chunk": {"type": "text-delta", "text": reply}},
                    },
                },
            }
        )
        send(
            {
                "method": "session.event",
                "params": {
                    "sessionId": session_id,
                    "event": {
                        "type": "assistant/message",
                        "data": {
                            "message": {
                                "content": [{"type": "text", "text": reply}]
                            },
                            "usage": (
                                {
                                    "inputTokens": 12,
                                    "cacheReadTokens": 20,
                                    "cacheWriteTokens": 3,
                                    "outputTokens": 4,
                                }
                                if text == "cached-usage"
                                else (
                                    {
                                        "inputTokens": 0,
                                        "cacheReadTokens": 120,
                                        "outputTokens": 10,
                                    }
                                    if text == "tool-loop-usage"
                                    else (
                                        {"inputTokens": 12, "outputTokens": 0}
                                        if text == "missing-output-usage"
                                        else {"inputTokens": 12, "outputTokens": 4}
                                    )
                                )
                            ),
                        },
                    },
                },
            }
        )
        send(
            {
                "method": "session.event",
                "params": {
                    "sessionId": session_id,
                    "event": {
                        "type": "turn/end",
                        "data": {"reason": {"kind": "completed"}},
                    },
                },
            }
        )
        send({"method": "session.status", "params": {"sessionId": session_id, "status": "idle"}})
        continue
    if method == "session/cancel":
        wedged_sessions.discard(params["sessionId"])
        send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"sessionId": params["sessionId"], "cancelled": True},
            }
        )
        continue
    if method == "shutdown":
        send({"jsonrpc": "2.0", "id": request_id, "result": {}})
        break
