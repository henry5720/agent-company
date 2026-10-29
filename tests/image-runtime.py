#!/usr/bin/env python3
"""agents/bin/company-image 的 mockable 測試。

不連網、不需要 Docker：`urlopen` 被換成一個會把
`Request` 收下來的假物件，所以測得到「送出去的請求長什麼樣」。輸出目錄與允許的
來源目錄也被指到 tmpdir，production 那份因此不必為了測試多開任何覆蓋參數。

這份測的是 CLI 契約：請求 shape、兩種回應形狀（一般 JSON body 與
`text/event-stream` 的 SSE）、PNG 輸出位置、以及不合法輸入一定被擋。它不能證明公司
gateway 真的接受這個 shape，也不能證明它這次會回哪一種 —— 那要經 host proxy
實打一次。

key 不在 container 裡（spec #21 §3）：host Caddy proxy 補上 Authorization，所以 CLI
不讀 key、不送 Authorization header；proxy 走 bridge 上的 plain http。
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
CLI = ROOT / "agents/bin/company-image"

# 放在 env 裡但 CLI 不該碰的 key：用來證明它不會被讀進請求或 stderr。
FAKE_KEY = "test-key-not-a-real-secret"
# production 的值（config/host-network.env）：bridge 上的 host proxy，plain http。
FAKE_BASE_URL = "http://10.231.0.1:18080"

# 「這個參數沒給」跟「給了 None」是兩件事：None 代表 gateway 漏掉 Content-Type。
UNSET = object()

failures: list[str] = []


def load_cli():
    # agents/bin/ 是要 COPY 進 image 的東西，不要在裡面留下 __pycache__。
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_loader(
        "company_image", importlib.machinery.SourceFileLoader("company_image", str(CLI))
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def png_bytes(width: int = 1, height: int = 1) -> bytes:
    """最小的合法 PNG，用來當假 gateway 的回傳與 edit 的來源圖。"""

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


class FakeResponse:
    """夠像 urlopen 回傳值的東西：有 Content-Type、能 read、能逐行 readline。"""

    def __init__(self, payload: bytes, content_type: str | None = "application/json"):
        self._stream = io.BytesIO(payload)
        self.headers = {} if content_type is None else {"Content-Type": content_type}
        # 宣告 SSE 的回應必須逐行讀完，不是整份 read() 進記憶體再切。
        self.reads = 0
        self.readlines = 0

    def getheader(self, name: str, default: str = "") -> str:
        for key, value in self.headers.items():
            if key.lower() == name.lower():
                return value
        return default

    def read(self, amount: int = -1) -> bytes:
        self.reads += 1
        return self._stream.read(amount if amount is not None and amount >= 0 else -1)

    def readline(self, limit: int = -1) -> bytes:
        self.readlines += 1
        return self._stream.readline(limit)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def sse_event(data, name: str | None = None) -> str:
    """一段 SSE 區塊。data 給 dict 就序列化，給字串就照原樣送（測 malformed 用）。"""
    text = data if isinstance(data, str) else json.dumps(data)
    lines = "".join(f"data: {line}\n" for line in text.split("\n"))
    return (f"event: {name}\n" if name else "") + lines + "\n"


def partial_event(encoded: str, item_id: str = "ig_1", index: int = 0, seq: int = 0) -> dict:
    """實測回來的那種 partial_image 事件：base64 在 top-level。"""
    return {
        "type": "response.image_generation_call.partial_image",
        "item_id": item_id,
        "output_index": index,
        "partial_image_index": seq,
        "partial_image_b64": encoded,
        "output_format": "png",
        "size": "1024x1024",
    }


def completed_event(encoded: str, item_id: str = "ig_1") -> dict:
    """收尾事件：整份 response 帶 image_generation_call 的 result。"""
    return {
        "type": "response.completed",
        "response": {
            "output": [
                {"type": "reasoning", "summary": []},
                {"type": "image_generation_call", "id": item_id, "result": encoded},
            ]
        },
    }


def b64(image: bytes) -> str:
    return base64.b64encode(image).decode()


class FakeGateway:
    """收下 Request 並回一個固定的回應（JSON payload 或 SSE body）。"""

    def __init__(
        self,
        payload=None,
        image: bytes | None = None,
        sse: str | None = None,
        raw: bytes | None = None,
        content_type=UNSET,
    ):
        self.requests = []
        self.timeouts = []
        self.responses = []
        if sse is not None:
            self.raw = sse.encode("utf-8")
            # content_type=None 是「gateway 漏掉這個 header」那個案例。
            self.content_type = (
                "text/event-stream; charset=utf-8" if content_type is UNSET else content_type
            )
            return
        self.content_type = "application/json" if content_type is UNSET else content_type
        if raw is not None:
            self.raw = raw
            return
        if payload is None:
            encoded = b64(image if image is not None else png_bytes())
            payload = {
                "output": [
                    {"type": "reasoning", "summary": []},
                    {"type": "image_generation_call", "result": encoded},
                ]
            }
        self.payload = payload
        self.raw = json.dumps(payload).encode()

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        self.timeouts.append(timeout)
        response = FakeResponse(self.raw, self.content_type)
        self.responses.append(response)
        return response

    @property
    def body(self) -> dict:
        return json.loads(self.requests[-1].data.decode())


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        return
    failures.append(f"{name}{': ' + detail if detail else ''}")


def run(cli, argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main(argv)
        except SystemExit as exc:  # argparse rejects unknown modes and sizes itself
            code = exc.code if isinstance(exc.code, int) else 1
    return code, out.getvalue(), err.getvalue()


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def main() -> int:
    cli = load_cli()
    workdir = Path(tempfile.mkdtemp(prefix="company-image-test-"))
    try:
        drafts = workdir / "drafts"
        staging = workdir / "staging"
        outside = workdir / "outside"
        for directory in (drafts, staging, outside):
            directory.mkdir()

        # spec #21 §11：edit 的來源可以直接是 thread workspace 與 sandbox 產物（不必先 cp 到
        # /tmp）；唯讀 snapshot 仍不行。用 production 的 root 清單、在碰檔案系統之前就被擋。
        os.environ["COMPANY_GATEWAY_BASE_URL"] = FAKE_BASE_URL
        code, _, err = run(cli, ["edit", "--prompt", "x", "--name", "n", "--source", "/home/node/code/app/logo.png"])
        check("snapshot source rejected as invalid input", code == 2, f"code={code} stderr={err.strip()}")
        for root in ("/home/node/drafts", "/home/node/work", "/home/node/out", "/home/node/.openab", "/tmp"):
            check(f"allowed source roots include {root}", root in err, err.strip())
        check("snapshot is not an allowed source root", "/home/node/code" not in err.split("one of:", 1)[-1], err.strip())

        cli.DRAFTS_ROOT = drafts
        cli.SOURCE_ROOTS = (drafts, staging)
        os.environ["COMPANY_GATEWAY_API_KEY"] = FAKE_KEY

        # ------------------------------------------------ generate happy path
        gateway = FakeGateway()
        cli.urlopen = gateway
        code, out, err = run(
            cli, ["generate", "--prompt", "  a red square  ", "--name", "hero"]
        )
        check("generate exits 0", code == 0, f"code={code} stderr={err.strip()}")
        check("generate calls the gateway once", len(gateway.requests) == 1)

        if gateway.requests:
            request = gateway.requests[0]
            check(
                "endpoint is base URL + /responses",
                request.full_url == "http://10.231.0.1:18080/responses",
                request.full_url,
            )
            check("method is POST", request.get_method() == "POST")
            check(
                "no Authorization header (the host proxy adds the key)",
                request.get_header("Authorization") is None,
            )
            check(
                "Content-Type is JSON",
                request.get_header("Content-type") == "application/json",
            )
            # User-Agent 固定，host 告警靠它從 Caddy access log 數生圖請求（scripts/alerts.py）。
            check(
                "User-Agent marks the request as company-image",
                request.get_header("User-agent") == "work-agent-company-image",
                str(request.get_header("User-agent")),
            )
            check(
                "only the two fixed headers are sent (no Authorization; the proxy adds it)",
                {k.lower() for k in request.headers} == {"content-type", "user-agent"},
                str(sorted(request.headers)),
            )
            check("timeout is set", gateway.timeouts[0] == cli.REQUEST_TIMEOUT_SECONDS)

            body = gateway.body
            check("model is the pinned one", body["model"] == cli.MODEL, body["model"])
            check(
                "image_generation tool is requested",
                body["tools"] == [
                    {
                        "type": "image_generation",
                        "size": "1024x1024",
                        "output_format": "png",
                    }
                ],
                json.dumps(body["tools"]),
            )
            check(
                "the tool is forced",
                body["tool_choice"] == {"type": "image_generation"},
            )
            content = body["input"][0]["content"]
            check(
                "prompt is sent trimmed as input_text",
                content == [{"type": "input_text", "text": "a red square"}],
                json.dumps(content),
            )
            check("generate sends no input_image", all(c["type"] != "input_image" for c in content))

        written = drafts / today() / "hero.png"
        check("PNG lands in drafts", written.is_file(), str(written))
        if written.is_file():
            check("output is a real PNG", written.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))
        check("stdout is exactly the output path", out.strip() == str(written), out.strip())

        # --------------------------------------------------- name collision
        gateway = FakeGateway()
        cli.urlopen = gateway
        code, out, _ = run(cli, ["generate", "--prompt", "again", "--name", "hero"])
        check("second run exits 0", code == 0)
        check(
            "a colliding name does not overwrite",
            out.strip() == str(drafts / today() / "hero-2.png"),
            out.strip(),
        )

        # ---------------------------------------------------- edit happy path
        source = staging / "shot.png"
        source_bytes = png_bytes(2, 2)
        source.write_bytes(source_bytes)
        gateway = FakeGateway()
        cli.urlopen = gateway
        code, out, err = run(
            cli,
            [
                "edit",
                "--prompt",
                "make it blue",
                "--name",
                "edited",
                "--size",
                "1536x1024",
                "--source",
                str(source),
            ],
        )
        check("edit exits 0", code == 0, f"code={code} stderr={err.strip()}")
        if gateway.requests:
            content = gateway.body["input"][0]["content"]
            check("edit sends text first", content[0]["type"] == "input_text")
            check("edit attaches one input_image", len(content) == 2 and content[1]["type"] == "input_image")
            if len(content) == 2:
                expected = "data:image/png;base64," + base64.b64encode(source_bytes).decode()
                check("input_image is the source as a data URL", content[1]["image_url"] == expected)
            check(
                "size is passed through from the allowlist",
                gateway.body["tools"][0]["size"] == "1536x1024",
            )
        check("edit writes a PNG", (drafts / today() / "edited.png").is_file())

        # ------------------------------------------------------ SSE responses
        # 公司 gateway 實測回 `200 text/event-stream`，圖在
        # `response.image_generation_call.partial_image` 事件的 top-level
        # `partial_image_b64`。JSON body 也還要能吃，所以兩種都測。
        png_a, png_b, png_c = png_bytes(1, 1), png_bytes(2, 2), png_bytes(3, 3)

        def run_sse(name: str, blocks, content_type=UNSET):
            gateway = FakeGateway(sse="".join(blocks), content_type=content_type)
            cli.urlopen = gateway
            code, out, err = run(cli, ["generate", "--prompt", "x", "--name", name])
            return gateway, code, out.strip(), err.strip()

        def written_bytes(path: str) -> bytes:
            return Path(path).read_bytes() if path and Path(path).is_file() else b""

        created = sse_event({"type": "response.created", "response": {"id": "resp_1"}})
        in_progress = sse_event(
            {"type": "response.image_generation_call.in_progress", "item_id": "ig_1"}
        )

        gateway, code, out, err = run_sse(
            "streamed",
            [created, ": keepalive\n\n", in_progress, sse_event(partial_event(b64(png_a)))],
        )
        check("SSE generate exits 0", code == 0, f"code={code} stderr={err}")
        check("SSE calls the gateway once", len(gateway.requests) == 1)
        check(
            "SSE request is the same shape as before",
            bool(gateway.requests)
            and gateway.requests[0].full_url.endswith("/responses")
            and gateway.body["tool_choice"] == {"type": "image_generation"},
        )
        check(
            "a declared SSE response is read line by line, not buffered whole",
            bool(gateway.responses)
            and gateway.responses[0].reads == 0
            and gateway.responses[0].readlines > 1,
            f"reads={gateway.responses[0].reads if gateway.responses else '-'} "
            f"readlines={gateway.responses[0].readlines if gateway.responses else '-'}",
        )
        check("SSE writes the streamed PNG", written_bytes(out) == png_a, out)
        check("SSE stdout is only the path", out == str(drafts / today() / "streamed.png"), out)
        check("SSE never prints the base64", b64(png_a) not in out and b64(png_a) not in err)

        # partial_image_b64 是一張完整的圖，不是 chunk：同一個 output 後到的取代先到的。
        _, code, out, err = run_sse(
            "multi-partial",
            [
                sse_event(partial_event(b64(png_a), seq=0)),
                sse_event(partial_event(b64(png_b), seq=1)),
            ],
        )
        check("multiple partials exit 0", code == 0, f"code={code} stderr={err}")
        check("the last partial wins", written_bytes(out) == png_b, out)

        _, code, out, err = run_sse(
            "with-final",
            [sse_event(partial_event(b64(png_a))), sse_event(completed_event(b64(png_c)))],
        )
        check("partial then completed exits 0", code == 0, f"code={code} stderr={err}")
        check("the completed payload beats the partial", written_bytes(out) == png_c, out)

        _, code, out, err = run_sse(
            "late-partial",
            [sse_event(completed_event(b64(png_c))), sse_event(partial_event(b64(png_a), seq=9))],
        )
        check("a partial after the final exits 0", code == 0, f"code={code} stderr={err}")
        check(
            "a partial arriving after the final does not replace it",
            written_bytes(out) == png_c,
            out,
        )

        _, code, out, err = run_sse(
            "two-outputs",
            [
                sse_event(partial_event(b64(png_b), item_id="ig_2", index=1)),
                sse_event(partial_event(b64(png_a), item_id="ig_1", index=0)),
                sse_event(completed_event(b64(png_b), item_id="ig_2")),
            ],
        )
        check("two image outputs exit 0", code == 0, f"code={code} stderr={err}")
        check("the first output seen is the one written", written_bytes(out) == png_b, out)

        # 收尾的 result 可能在 event 的 top-level，也可能包在 item 裡。
        for label, name, event in (
            (
                "top-level result",
                "final-flat",
                {
                    "type": "response.image_generation_call.completed",
                    "item_id": "ig_1",
                    "output_index": 0,
                    "result": b64(png_c),
                },
            ),
            (
                "result inside item",
                "final-item",
                {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": {
                        "type": "image_generation_call",
                        "id": "ig_1",
                        "result": b64(png_c),
                    },
                },
            ),
        ):
            _, code, out, err = run_sse(
                name, [sse_event(partial_event(b64(png_a))), sse_event(event)]
            )
            check(f"a final event exits 0: {label}", code == 0, f"code={code} stderr={err}")
            check(f"the final result beats the partial: {label}", written_bytes(out) == png_c, out)

        # 一個 event 的 data 可以拆成好幾行，接起來才是 JSON。
        _, code, out, err = run_sse(
            "multiline-data",
            [sse_event(json.dumps(partial_event(b64(png_a)), indent=1))],
        )
        check("multi-line data exits 0", code == 0, f"code={code} stderr={err}")
        check("multi-line data writes the PNG", written_bytes(out) == png_a, out)

        _, code, out, err = run_sse(
            "noisy",
            [
                ": ping\n\n",
                "id: 1\nretry: 500\n\n",
                "\n\n",
                sse_event(partial_event(b64(png_a))),
                "data: [DONE]\n\n",
            ],
        )
        check("keepalives and non-data fields are ignored", code == 0, f"code={code} stderr={err}")
        check("noisy stream still writes the PNG", written_bytes(out) == png_a, out)

        # 最後一個 event 沒有以空行收尾。
        _, code, out, err = run_sse(
            "unterminated",
            [f"event: partial\ndata: {json.dumps(partial_event(b64(png_a)))}\n"],
        )
        check("an unterminated last event still parses", code == 0, f"code={code} stderr={err}")
        check("unterminated stream writes the PNG", written_bytes(out) == png_a, out)

        # base64 被折行過（有實作會這樣送），解碼前要把空白去掉。
        folded = b64(png_a)
        folded = folded[:20] + "\n" + folded[20:40] + "\r\n" + folded[40:]
        _, code, out, err = run_sse("folded", [sse_event(partial_event(folded))])
        check("folded base64 exits 0", code == 0, f"code={code} stderr={err}")
        check("folded base64 decodes to the PNG", written_bytes(out) == png_a, out)

        # 錯誤說明會被截斷，整個 event 不會被倒進 stderr。
        _, code, out, err = run_sse(
            "streamfail",
            [sse_event({"type": "error", "error": {"message": "boom " * 2000}})],
        )
        check("a long error message still exits 3", code == cli.EXIT_RUNTIME, f"exit={code}")
        check(
            "the error detail is truncated",
            len(err) < cli.MAX_ERROR_DETAIL_CHARS + 120,
            f"len={len(err)}",
        )

        # gateway stream 但漏掉 Content-Type：body 的形狀就足以判斷。
        _, code, out, err = run_sse(
            "sniffed", [sse_event(partial_event(b64(png_a)))], content_type=None
        )
        check("SSE without a Content-Type still parses", code == 0, f"code={code} stderr={err}")
        check("sniffed stream writes the PNG", written_bytes(out) == png_a, out)

        # ------------------------------------------------- SSE that carries no image
        bad_streams = [
            ("empty stream", [""], ""),
            ("only keepalives", [": ping\n\n", ": ping\n\n"], ""),
            ("only [DONE]", ["data: [DONE]\n\n"], ""),
            (
                "an error event",
                [sse_event({"type": "error", "error": {"message": "image_generation disabled"}})],
                "image_generation disabled",
            ),
            (
                "a failed response",
                [
                    sse_event({"type": "response.in_progress"}),
                    sse_event(
                        {
                            "type": "response.failed",
                            "response": {"error": {"message": "content_policy"}},
                        }
                    ),
                ],
                "content_policy",
            ),
            (
                "completed without an image",
                [sse_event({"type": "response.completed", "response": {"output": [{"type": "message"}]}})],
                "",
            ),
            (
                "an image_generation_call with an empty result",
                [sse_event(partial_event(""))],
                "",
            ),
            ("malformed data", ['data: {"type": "response.completed"\n\n'], "not JSON"),
            (
                "data that is not an object",
                ["data: [1, 2, 3]\n\n"],
                "",
            ),
        ]
        for label, blocks, expected in bad_streams:
            _, code, out, err = run_sse("streamfail", blocks)
            check(f"SSE without an image exits 3: {label}", code == cli.EXIT_RUNTIME, f"exit={code}")
            check(
                f"SSE without an image writes nothing: {label}",
                not (drafts / today() / "streamfail.png").exists(),
            )
            check(f"the key never leaks into stderr: {label}", FAKE_KEY not in err)
            if expected:
                check(f"the failure says why: {label}", expected in err, err)

        # base64 進來但不是圖、或根本不是 base64：訊息不能把 payload 倒出來。
        for label, encoded, forbidden in (
            ("not base64", "not-base64-@@@@", "not-base64-@@@@"),
            ("not a PNG", b64(b"GIF89a nope"), b64(b"GIF89a nope")),
        ):
            _, code, out, err = run_sse("streambad", [sse_event(partial_event(encoded))])
            check(f"SSE bad image exits 3: {label}", code == cli.EXIT_RUNTIME, f"exit={code}")
            check(
                f"SSE bad image writes nothing: {label}",
                not (drafts / today() / "streambad.png").exists(),
            )
            check(f"the payload is not echoed: {label}", forbidden not in err and forbidden not in out)

        # ------------------------------------------------------------ size caps
        caps = [
            ("base64 over the cap", "MAX_IMAGE_B64_CHARS", 16),
            ("a line over the cap", "MAX_SSE_LINE_CHARS", 32),
            ("a body over the cap", "MAX_SSE_BODY_CHARS", 40),
        ]
        for label, attribute, limit in caps:
            saved_limit = getattr(cli, attribute)
            setattr(cli, attribute, limit)
            _, code, out, err = run_sse("capped", [sse_event(partial_event(b64(png_a)))])
            setattr(cli, attribute, saved_limit)
            check(f"SSE cap is enforced: {label}", code == cli.EXIT_RUNTIME, f"exit={code}")
            check(
                f"SSE cap writes nothing: {label}",
                not (drafts / today() / "capped.png").exists(),
            )
            check(f"the cap failure mentions a limit: {label}", str(limit) in err, err)

        saved_limit = cli.MAX_JSON_BODY_CHARS
        cli.MAX_JSON_BODY_CHARS = 8
        cli.urlopen = FakeGateway()
        code, _, err = run(cli, ["generate", "--prompt", "x", "--name", "jsoncap"])
        cli.MAX_JSON_BODY_CHARS = saved_limit
        check("an oversized JSON body exits 3", code == cli.EXIT_RUNTIME, f"exit={code}")
        check("an oversized JSON body writes nothing", not (drafts / today() / "jsoncap.png").exists())

        # ------------------------------------------------ JSON stays supported
        cli.urlopen = FakeGateway(
            raw=json.dumps(
                {"output": [{"type": "image_generation_call", "result": b64(png_a)}]}
            ).encode(),
            content_type="application/json; charset=utf-8",
        )
        code, out, err = run(cli, ["generate", "--prompt", "x", "--name", "jsoncharset"])
        check("a JSON body with charset still works", code == 0, f"code={code} stderr={err}")
        check(
            "the JSON path writes the PNG",
            written_bytes(out.strip()) == png_a,
            out.strip(),
        )

        cli.urlopen = FakeGateway(raw=b"[]")
        code, _, err = run(cli, ["generate", "--prompt", "x", "--name", "jsonlist"])
        check("a JSON body that is not an object exits 3", code == cli.EXIT_RUNTIME, f"exit={code}")
        check("a non-object JSON body writes nothing", not (drafts / today() / "jsonlist.png").exists())

        cli.urlopen = FakeGateway(raw=b"<html>nope</html>", content_type="text/html")
        code, _, err = run(cli, ["generate", "--prompt", "x", "--name", "notjson"])
        check("a non-JSON body still exits 3", code == cli.EXIT_RUNTIME, f"exit={code}")
        check("the non-JSON failure says so", "not JSON" in err, err)

        # ------------------------------------------------- media type detection
        class HeadersOnly:
            """只有 .headers、沒有 getheader 的回應物件。"""

            def __init__(self, value):
                self.headers = {"Content-Type": value}

        check(
            "the media type is read from headers when getheader is missing",
            cli.response_media_type(HeadersOnly("text/event-stream")) == "text/event-stream",
        )
        check(
            "charset and case are stripped from the media type",
            cli.response_media_type(FakeResponse(b"", "TEXT/Event-Stream; charset=utf-8"))
            == "text/event-stream",
        )
        check(
            "a missing Content-Type reads as empty",
            cli.response_media_type(FakeResponse(b"", None)) == "",
        )

        # ------------------------------------------------------ bad input
        rejected = [
            (
                "unknown mode",
                ["upscale", "--prompt", "x", "--name", "a"],
            ),
            (
                "size outside the allowlist",
                ["generate", "--prompt", "x", "--name", "a", "--size", "4096x4096"],
            ),
            (
                "name with a path separator",
                ["generate", "--prompt", "x", "--name", "../escape"],
            ),
            (
                "name with an extension",
                ["generate", "--prompt", "x", "--name", "a.png"],
            ),
            (
                "empty prompt",
                ["generate", "--prompt", "   ", "--name", "a"],
            ),
            (
                "prompt over the cap",
                ["generate", "--prompt", "x" * (cli.MAX_PROMPT_CHARS + 1), "--name", "a"],
            ),
            (
                "generate does not take --source",
                ["generate", "--prompt", "x", "--name", "a", "--source", str(source)],
            ),
            (
                "edit without --source",
                ["edit", "--prompt", "x", "--name", "a"],
            ),
            (
                "source outside the allowed roots",
                ["edit", "--prompt", "x", "--name", "a", "--source", str(outside / "x.png")],
            ),
            (
                "relative source",
                ["edit", "--prompt", "x", "--name", "a", "--source", "shot.png"],
            ),
            (
                "there is no --url flag",
                ["generate", "--prompt", "x", "--name", "a", "--url", "https://evil.invalid"],
            ),
            (
                "there is no --header flag",
                ["generate", "--prompt", "x", "--name", "a", "--header", "X: 1"],
            ),
            (
                "there is no --model flag",
                ["generate", "--prompt", "x", "--name", "a", "--model", "other"],
            ),
            (
                "there is no --output flag",
                ["generate", "--prompt", "x", "--name", "a", "--output", "/etc/x.png"],
            ),
        ]
        (outside / "x.png").write_bytes(png_bytes())
        for label, argv in rejected:
            gateway = FakeGateway()
            cli.urlopen = gateway
            before = sorted(p.name for p in (drafts / today()).glob("*"))
            code, out, err = run(cli, argv)
            check(f"rejects: {label}", code != 0, f"exit={code}")
            check(f"rejects without calling the gateway: {label}", not gateway.requests)
            after = sorted(p.name for p in (drafts / today()).glob("*"))
            check(f"rejects without writing a file: {label}", before == after)

        # A file that exists in an allowed root but is not an image.
        not_an_image = staging / "notes.txt"
        not_an_image.write_text("plain text")
        gateway = FakeGateway()
        cli.urlopen = gateway
        code, _, _ = run(
            cli, ["edit", "--prompt", "x", "--name", "a", "--source", str(not_an_image)]
        )
        check("rejects a non-image source", code == cli.EXIT_INVALID_INPUT, f"exit={code}")
        check("non-image source never reaches the gateway", not gateway.requests)

        # A symlink in an allowed root that points outside it.
        escape = staging / "escape.png"
        escape.symlink_to(outside / "x.png")
        gateway = FakeGateway()
        cli.urlopen = gateway
        code, _, _ = run(cli, ["edit", "--prompt", "x", "--name", "a", "--source", str(escape)])
        check("rejects a symlink that escapes the allowed roots", code == cli.EXIT_INVALID_INPUT)
        check("escaping symlink never reaches the gateway", not gateway.requests)

        # ------------------------------------------------------ bad gateway
        for label, payload in (
            ("no image_generation_call", {"output": [{"type": "message"}]}),
            ("no output array", {"error": "nope"}),
            ("result is not base64", {"output": [{"type": "image_generation_call", "result": "!!!"}]}),
            (
                "result is not a PNG",
                {
                    "output": [
                        {
                            "type": "image_generation_call",
                            "result": base64.b64encode(b"GIF89a nope").decode(),
                        }
                    ]
                },
            ),
        ):
            cli.urlopen = FakeGateway(payload=payload)
            code, _, err = run(cli, ["generate", "--prompt", "x", "--name", "bad"])
            check(f"gateway failure exits 3: {label}", code == cli.EXIT_RUNTIME, f"exit={code}")
            check(f"gateway failure writes nothing: {label}", not (drafts / today() / "bad.png").exists())
            check(f"the key never leaks into stderr: {label}", FAKE_KEY not in err)

        # ------------------------------------------------------ bad env
        cli.urlopen = FakeGateway()
        for label, env, expected in (
            ("missing base URL", {"COMPANY_GATEWAY_BASE_URL": ""}, cli.EXIT_RUNTIME),
            (
                "non-http base URL",
                {"COMPANY_GATEWAY_BASE_URL": "file:///etc/passwd"},
                cli.EXIT_RUNTIME,
            ),
        ):
            saved = {k: os.environ.get(k) for k in env}
            os.environ.update(env)
            code, _, err = run(cli, ["generate", "--prompt", "x", "--name", "envcase"])
            check(f"bad env exits 3: {label}", code == expected, f"exit={code}")
            check(f"bad env writes nothing: {label}", not (drafts / today() / "envcase.png").exists())
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        # The key lives only in the host proxy: with no key in the env at all the
        # CLI still calls the proxy, and never sends an Authorization header.
        saved_key = os.environ.pop("COMPANY_GATEWAY_API_KEY", None)
        gateway = FakeGateway()
        cli.urlopen = gateway
        code, _, err = run(cli, ["generate", "--prompt", "x", "--name", "nokey"])
        check("no key in env still generates", code == 0, f"code={code} stderr={err.strip()}")
        check(
            "no key in env sends no Authorization header",
            bool(gateway.requests) and gateway.requests[0].get_header("Authorization") is None,
        )
        if saved_key is not None:
            os.environ["COMPANY_GATEWAY_API_KEY"] = saved_key
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    if failures:
        print(f"image-runtime: {len(failures)} check(s) failed", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print("Image runtime checks passed (mocked gateway; no real call was made).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
