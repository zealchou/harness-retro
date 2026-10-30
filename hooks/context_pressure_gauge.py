#!/usr/bin/env python3
"""Optional Claude Code hook that enqueues one retrospective at high context pressure."""
from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "retro2"))
import queue_adapter  # noqa: E402
import config  # noqa: E402


STATE_DIR = Path(os.environ.get("RETRO2_GAUGE_STATE_DIR", "/tmp/retro2-context-state"))
LOCK_DEADLINE_SECONDS = 0.025


def read_stdin_payload():
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def measure_usage(transcript_path):
    latest = 0
    path = Path(transcript_path)
    if not path.is_file():
        return latest
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            usage = json.loads(line).get("message", {}).get("usage", {})
        except (ValueError, AttributeError):
            continue
        latest = sum(
            value for key, value in usage.items()
            if key in {"input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"}
            and isinstance(value, int)
        )
    return latest


def context_window():
    try:
        return max(1, int(os.environ.get("RETRO2_CONTEXT_WINDOW", "800000")))
    except ValueError:
        return 800000


def enqueue_retro2_if_enabled(payload, ratio, launcher=None):
    if os.environ.get("RETRO2_QUEUE_ENABLED", "").lower() not in {"1", "true", "yes"}:
        return None
    queue_dir = Path(os.environ.get("RETRO2_QUEUE_DIR", config.QUEUE_DIR))
    return queue_adapter.observe_pressure_event(
        str(payload.get("session_id", "unknown")), ratio, queue_dir, launcher,
        transcript_path=payload.get("transcript_path"), repo_root=payload.get("cwd"),
    )


def _context(ratio, remaining):
    percent = round(ratio * 100)
    if ratio >= 0.88:
        return (
            f"context 已用約 {percent}%，接近上限。把當前工作收到一個乾淨斷點，寫好交接，"
            "然後告知維護者狀態已保存；這是進度通知，不是把決定丟回去。"
        )
    return (
        f"context 已用約 {percent}%，剩約 {remaining:,} token。接下來可以："
        "①先保存重要脈絡 ②把可獨立掃描的工作分離 ③縮短回覆與工具輸出。"
    )


def main():
    payload = read_stdin_payload()
    session_id = str(payload.get("session_id", "unknown"))
    state_root = Path(STATE_DIR)
    state_root.mkdir(parents=True, exist_ok=True)
    state_path = state_root / f"{session_id}.json"
    lock_path = Path(str(state_path) + ".lock")
    with lock_path.open("a+") as handle:
        deadline = time.monotonic() + LOCK_DEADLINE_SECONDS
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    return
                time.sleep(0.002)
        used = measure_usage(payload.get("transcript_path", ""))
        window = context_window()
        ratio = used / window
        enqueue_retro2_if_enabled(payload, ratio)
        if ratio < 0.75:
            return
        output = {
            "hookSpecificOutput": {
                "hookEventName": payload.get("hook_event_name", "Unknown"),
                "additionalContext": _context(ratio, max(0, window - used)),
            }
        }
        print(json.dumps(output))


if __name__ == "__main__":
    main()

