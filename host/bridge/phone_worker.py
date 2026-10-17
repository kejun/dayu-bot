"""One bounded official-client RPC. JSON parameters arrive on stdin, never a shell."""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys

from .ictrl import IctrlClient, IctrlError


def classify_rpc_error(method: str, params: dict, message: str) -> tuple[bool, str | None]:
    """Only known, explicit rejections prove that no action remains pending.

    iCTRL uses -32000 both for failed opens and for queued / timed-out actions.
    Never treat that code alone, or a generic 'not installed' substring, as safe.
    """
    certain = "rpc failed (401)" in message or "no token" in message
    reason = None
    try:
        error = json.loads(message)
        certain |= error.get("code") in (-32600, -32601, -32602)
        if (method == 'find_element' and error.get('code') == -32602
                and str(error.get('message', '')).startswith('No element matching label/id')):
            certain, reason = True, 'element_not_found'
        query = params.get('element_id')
        if method == 'find_element' and error.get('code') == -32000 and isinstance(query, str) and query:
            # These exact Runner replies are emitted AFTER locateElement has
            # returned without a hit. They are not the RPC watchdog's reply
            # that a queued call may still be running. Never classify by code
            # or a loose "accessibility" substring alone.
            prefix = r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+'s accessibility isn't answering right now"
            escaped = re.escape(query)
            patterns = (
                prefix + r", so '" + escaped + r"' can't be looked up\. Read the screen with screen_text and tap by coordinate; try find_element again in ~20s\.",
                prefix + r" \(the tree read failed after \d+s\), so '" + escaped + r"' couldn't be looked up\. Read the screen with screen_text and tap by coordinate; find_element works again once the app answers\.",
            )
            if any(re.fullmatch(pattern, str(error.get('message', ''))) for pattern in patterns):
                certain, reason = True, 'accessibility_unavailable'
        bid = params.get("bundle_id")
        rejected = (f"Could not open '{bid}'. The bundle_id is likely wrong or not installed. "
                    'Call resolve_app{text:"<app name>"} to look up the correct one.')
        if (method == "launch_app" and isinstance(bid, str) and bid
                and error.get("code") == -32000 and error.get("message") == rejected):
            certain, reason = True, "app_launch_rejected"
    except (ValueError, AttributeError):
        pass
    if any(word in message.lower() for word in ("timed out", "timeout", "超时")):
        certain, reason = False, None
    return not certain, reason


def main() -> int:
    host, client_path, method, timeout, cancel_file = sys.argv[1:]
    try:
        params = json.load(sys.stdin)
        if not isinstance(params, dict):
            raise ValueError("RPC params must be an object")
        client = IctrlClient(host, Path(client_path))
        original_post = client.mod._post
        def post_unless_cancelled(*args, **kwargs):
            if Path(cancel_file).exists():
                raise IctrlError('{"code": -32602, "message": "任务在派发手机动作前取消"}')
            return original_post(*args, **kwargs)
        client.mod._post = post_unless_cancelled
        result = client.call(method, params, float(timeout))
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
    except IctrlError as exc:
        message = str(exc)
        uncertain, reason = classify_rpc_error(method, params, message)
        print(json.dumps({"ok": False, "error": message[:500], "uncertain": uncertain,
                          "reason": reason}, ensure_ascii=False))
    except (ValueError, TypeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc), "uncertain": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
