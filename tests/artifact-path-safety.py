#!/usr/bin/env python3
"""company-image 的路徑安全測試：symlink 逃逸與 check/use race。

每個 case 都自己種好攻擊佈局（父目錄被換成 symlink、檔名被換成 symlink、檔案在
檢查之後被換掉），所以失敗是可重現的，不是「看起來有處理」。不開 socket、不需要
Docker、不需要真的 key：`urlopen` 換成假物件。

這裡驗的是「不會逃出固定 root」，不驗 gateway 真的接受請求。
"""

from __future__ import annotations

import base64
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import struct
import sys
import tempfile
import zlib
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMAGE_CLI = ROOT / "agents/bin/company-image"
FAKE_KEY = "test-key-not-a-real-secret"
FAKE_BASE_URL = "https://gateway.example.invalid/v1"

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        failures.append(f"{name}: {detail}" if detail else name)


def content(path: Path) -> bytes | None:
    """讀檔內容給斷言用。檔案不在就回 None，讓失敗變成一筆記錄而不是 traceback。"""
    try:
        return path.read_bytes()
    except OSError:
        return None


def load(path: Path, name: str):
    # agents/bin/ 會被 COPY 進 image，不要在裡面留下 __pycache__。
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_loader(
        name, importlib.machinery.SourceFileLoader(name, str(path))
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def png_bytes(width: int = 1, height: int = 1) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def run(cli, argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main(argv)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
    return code, out.getvalue(), err.getvalue()


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class Response:
    def __init__(
        self,
        payload: bytes = b'{"ok": true}',
        status: int = 200,
        content_type: str = "application/json",
    ):
        self.payload, self.status = payload, status
        self.headers = {"Content-Type": content_type}
        self._stream = io.BytesIO(payload)

    def getheader(self, name: str, default: str = "") -> str:
        return self.headers.get(name, default)

    def read(self, amount: int = -1) -> bytes:
        return self._stream.read(amount if amount is not None and amount >= 0 else -1)

    def readline(self, limit: int = -1) -> bytes:
        return self._stream.readline(limit)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class Gateway:
    """假 gateway，永遠回一張最小的合法 PNG。

    `stream=True` 回的是實測那種 SSE。寫檔那一段兩種回應共用，所以路徑安全的 case
    也要能用 SSE 跑一次 —— parser 換了不代表 dirfd 與 O_EXCL 那條路可以鬆掉。
    """

    def __init__(self, image: bytes | None = None, stream: bool = False):
        self.requests: list = []
        self.image = image if image is not None else png_bytes()
        self.stream = stream

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        encoded = base64.b64encode(self.image).decode()
        if self.stream:
            event = json.dumps(
                {
                    "type": "response.image_generation_call.partial_image",
                    "item_id": "ig_1",
                    "output_index": 0,
                    "partial_image_index": 0,
                    "partial_image_b64": encoded,
                    "output_format": "png",
                }
            )
            body = f": keepalive\n\ndata: {event}\n\n"
            return Response(body.encode(), content_type="text/event-stream")
        payload = {
            "output": [{"type": "image_generation_call", "result": encoded}]
        }
        return Response(json.dumps(payload).encode())


# ------------------------------------------------------------ company-image cases


def image_cases(cli, workdir: Path) -> None:
    os.environ["COMPANY_GATEWAY_BASE_URL"] = FAKE_BASE_URL
    os.environ["COMPANY_GATEWAY_API_KEY"] = FAKE_KEY

    def fresh() -> tuple[Path, Path, Path]:
        base = Path(tempfile.mkdtemp(prefix="image-case-", dir=workdir))
        drafts, staging, outside = base / "drafts", base / "staging", base / "outside"
        for directory in (drafts, staging, outside):
            directory.mkdir()
        cli.DRAFTS_ROOT = drafts
        cli.SOURCE_ROOTS = (drafts, staging)
        return drafts, staging, outside

    def generate(argv: list[str], stream: bool = False) -> tuple[int, str, str, Gateway]:
        gateway = Gateway(stream=stream)
        cli.urlopen = gateway
        code, out, err = run(cli, argv)
        return code, out, err, gateway

    # 1. --source 的父目錄是 symlink：逐段 O_NOFOLLOW 必須擋下來。
    drafts, staging, outside = fresh()
    (outside / "shots").mkdir()
    private = outside / "shots" / "private.png"
    private.write_bytes(png_bytes(2, 2))
    (staging / "shots").symlink_to(outside / "shots")
    code, _, _, gateway = generate(
        ["edit", "--prompt", "x", "--name", "a", "--source", str(staging / "shots" / "private.png")]
    )
    check("source under a symlinked parent is rejected", code == cli.EXIT_INVALID_INPUT, f"exit={code}")
    check("source under a symlinked parent never reaches the gateway", not gateway.requests)

    # 2. --source 用 `..` 走出允許的 root。
    drafts, staging, outside = fresh()
    (outside / "private.png").write_bytes(png_bytes())
    code, _, _, gateway = generate(
        [
            "edit",
            "--prompt",
            "x",
            "--name",
            "a",
            "--source",
            f"{staging}/../outside/private.png",
        ]
    )
    check("dot-dot source is rejected", code == cli.EXIT_INVALID_INPUT, f"exit={code}")
    check("dot-dot source never reaches the gateway", not gateway.requests)

    # 3. 日期目錄被事先種成 symlink：不寫進去，也不改寫 symlink 目標裡的東西。
    drafts, staging, outside = fresh()
    (outside / "loot").mkdir()
    (drafts / today()).symlink_to(outside / "loot")
    code, _, err, gateway = generate(["generate", "--prompt", "x", "--name", "hero"])
    check("symlinked day directory is refused", code == cli.EXIT_RUNTIME, f"exit={code}")
    check("symlinked day directory is refused before the gateway call", not gateway.requests)
    check(
        "symlinked day directory stays empty",
        not list((outside / "loot").iterdir()),
        str(list((outside / "loot").iterdir())),
    )
    check("the key never leaks into stderr", FAKE_KEY not in err)

    # 4. drafts root 自己是 symlink。
    drafts, staging, outside = fresh()
    shutil.rmtree(drafts)
    drafts.symlink_to(outside)
    code, _, _, gateway = generate(["generate", "--prompt", "x", "--name", "hero"])
    check("symlinked drafts root is refused", code == cli.EXIT_RUNTIME, f"exit={code}")
    check("symlinked drafts root is refused before the gateway call", not gateway.requests)

    # 5. 目標檔名被事先種成指向 root 以外的 symlink：O_EXCL 讓它換名字，不寫穿。
    drafts, staging, outside = fresh()
    victim = outside / "victim.png"
    victim.write_bytes(b"do not overwrite me")
    day = drafts / today()
    day.mkdir()
    (day / "hero.png").symlink_to(victim)
    code, out, err, gateway = generate(["generate", "--prompt", "x", "--name", "hero"])
    check("a symlinked target still succeeds", code == 0, err.strip())
    check(
        "a symlinked target is written past, not through",
        out.strip() == str(day / "hero-2.png"),
        out.strip(),
    )
    check(
        "a symlinked target does not overwrite its victim",
        content(victim) == b"do not overwrite me",
        str(content(victim)),
    )
    check("the planted symlink is left as-is", (day / "hero.png").is_symlink())

    # 6. 正常寫入：內容完整，而且不留暫存檔。
    drafts, staging, outside = fresh()
    code, out, err, gateway = generate(["generate", "--prompt", "x", "--name", "clean"])
    check("clean generate exits 0", code == 0, err.strip())
    written = drafts / today() / "clean.png"
    check("clean generate writes the PNG", written.is_file(), str(written))
    check("the written PNG is complete", content(written) == gateway.image)
    leftovers = sorted(p.name for p in (drafts / today()).glob("*") if p.name != "clean.png")
    check("no temporary file is left behind", not leftovers, str(leftovers))

    # 7. gateway 失敗時連檔名都不該被佔住。
    drafts, staging, outside = fresh()

    def broken(request, timeout=None):
        return Response(json.dumps({"output": [{"type": "message"}]}).encode())

    cli.urlopen = broken
    code, _, err = run(cli, ["generate", "--prompt", "x", "--name", "ghost"])
    check("a bad gateway response exits 3", code == cli.EXIT_RUNTIME, f"exit={code}")
    remaining = sorted(p.name for p in (drafts / today()).glob("*"))
    check("a bad gateway response reserves no name", not remaining, str(remaining))

    # 8. SSE 回應走的是同一條寫檔路徑：日期目錄仍然是 dirfd 握著的那一個，種好的
    #    symlink 仍然寫不穿，也仍然不留暫存檔。
    drafts, staging, outside = fresh()
    victim = outside / "victim.png"
    victim.write_bytes(b"do not overwrite me")
    day = drafts / today()
    day.mkdir()
    (day / "streamed.png").symlink_to(victim)
    code, out, err, gateway = generate(
        ["generate", "--prompt", "x", "--name", "streamed"], stream=True
    )
    check("a streamed response exits 0", code == 0, err.strip())
    check(
        "a streamed response is written past a planted symlink",
        out.strip() == str(day / "streamed-2.png"),
        out.strip(),
    )
    check(
        "a streamed response does not overwrite the symlink victim",
        content(victim) == b"do not overwrite me",
        str(content(victim)),
    )
    check("the planted symlink survives the streamed write", (day / "streamed.png").is_symlink())
    check(
        "the streamed PNG is complete",
        content(day / "streamed-2.png") == gateway.image,
    )
    leftovers = sorted(
        p.name for p in day.glob("*") if p.name not in ("streamed.png", "streamed-2.png")
    )
    check("a streamed response leaves no temporary file", not leftovers, str(leftovers))

    # 9. 日期目錄是 symlink 時，SSE 也一樣在呼叫 gateway 之前就被擋下來。
    drafts, staging, outside = fresh()
    (outside / "loot").mkdir()
    (drafts / today()).symlink_to(outside / "loot")
    code, _, err, gateway = generate(
        ["generate", "--prompt", "x", "--name", "streamed"], stream=True
    )
    check("SSE does not change the day-directory check", code == cli.EXIT_RUNTIME, f"exit={code}")
    check("SSE still refuses before the gateway call", not gateway.requests)
    check(
        "SSE writes nothing into the symlink target",
        not list((outside / "loot").iterdir()),
        str(list((outside / "loot").iterdir())),
    )


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="artifact-path-safety-"))
    try:
        image_cases(load(IMAGE_CLI, "company_image"), workdir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    if failures:
        print(f"artifact-path-safety: {len(failures)} check(s) failed", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print("Artifact path-safety checks passed (mocked gateway; no real call was made).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
