#!/usr/bin/env python3
"""Slack channel／thread_ts／thread key／repo 名稱的格式 pattern 在各 CLI 之間逐字一致。

這幾支是分開裝進兩份 image 的獨立執行檔（bot：thread-workspace、sandbox-run、
slack-thread-artifact；sandbox：runner），沒有共用 module 可 import，所以各自寫一份、
由這支測試比對：同名 pattern 字面相同、key 正好是 channel-thread_ts，而且一律用
`fullmatch`（`re.match` 加 `$` 會放過結尾的換行）。只讀原始碼，不執行任何一支。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = ("agents/bin/thread-workspace", "agents/bin/sandbox-run", "agents/bin/slack-thread-artifact", "sandbox/runner")
NAMES = ("CHANNEL", "THREAD_TS", "KEY", "REPO")
DEFINITION = re.compile(r'^(' + "|".join(NAMES) + r')_PATTERN = re.compile\(r"([^"]*)"\)$', re.M)
failures: list[str] = []


def check(name, condition, detail=""):
    if not condition:
        failures.append(f"{name}: {detail}" if detail else name)


def main() -> int:
    found: dict[str, dict[str, str]] = {name: {} for name in NAMES}
    for rel in FILES:
        source = (ROOT / rel).read_text()
        for name, pattern in DEFINITION.findall(source):
            found[name][rel] = pattern
        for name in NAMES:
            loose = re.findall(rf"\b{name}_PATTERN\.(match|search)\(", source)
            check(f"{rel} uses fullmatch for {name}_PATTERN", not loose, str(loose))
    for name, by_file in found.items():
        check(f"{name}_PATTERN is defined somewhere", bool(by_file))
        check(f"{name}_PATTERN is identical in every file", len(set(by_file.values())) <= 1,
              "\n      " + "\n      ".join(f"{f}: {p}" for f, p in by_file.items()))
    channel = next(iter(found["CHANNEL"].values()), "")
    thread_ts = next(iter(found["THREAD_TS"].values()), "")
    for rel, key in found["KEY"].items():
        check(f"{rel} KEY_PATTERN is CHANNEL-THREAD_TS", key == channel.removesuffix("$") + "-" + thread_ts.removeprefix("^"), key)
    for rel in ("agents/bin/thread-workspace", "agents/bin/sandbox-run", "sandbox/runner"):
        check(f"{rel} defines KEY_PATTERN", rel in found["KEY"])
    if failures:
        print("format-patterns: failed", *failures, sep="\n  - ", file=sys.stderr)
        return 1
    print("format pattern checks passed (source text only).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
