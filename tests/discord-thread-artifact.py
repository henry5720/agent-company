#!/usr/bin/env python3
"""受限 Discord artifact uploader 的離線契約測試；不會開 socket。"""
from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
from email.parser import BytesParser
from email.policy import HTTP
from pathlib import Path
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "agents/bin/discord-thread-artifact"
TOKEN = "test-token-not-a-secret"
CHANNEL = "111111111111111111"
THREAD = "222222222222222222"
MESSAGE = "333333333333333333"
PNG_BYTES = b"\x89PNG\r\n\x1a\nmock"
failures: list[str] = []


def check(name, condition, detail=""):
    if not condition:
        failures.append(f"{name}: {detail}" if detail else name)


def load():
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_loader("discord_artifact", importlib.machinery.SourceFileLoader("discord_artifact", str(CLI)))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def headers_of(request):
    return {key.lower(): value for key, value in request.headers.items()}


def parts_of(request):
    """把 multipart body 拆回 {name: (filename, content_type, bytes)}。"""
    raw = b"Content-Type: " + headers_of(request)["content-type"].encode() + b"\r\n\r\n" + request.data
    message = BytesParser(policy=HTTP).parsebytes(raw)
    return {
        part.get_param("name", header="content-disposition"): (
            part.get_filename(), part.get_content_type(), part.get_payload(decode=True),
        )
        for part in message.iter_parts()
    }


class Response:
    def __init__(self, payload): self.payload, self.status = payload, 200
    def read(self): return self.payload
    def __enter__(self): return self
    def __exit__(self, *_): return False


class Discord:
    """假 Discord。GET channel 回 `channel_type`；POST 回帶一個附件的 message。

    `fail` 讓那個 method 丟 OSError；`reject` 讓它回 HTTP 400 加 Discord 的錯誤 body。
    """

    def __init__(self, channel_type=0, fail=None, reject=None):
        self.requests, self.channel_type, self.fail, self.reject = [], channel_type, fail, reject

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        method = request.get_method()
        if self.fail == method: raise OSError("mock failure")
        if self.reject == method:
            body = io.BytesIO(json.dumps({"code": 50083, "message": "Thread is archived"}).encode())
            raise HTTPError(request.full_url, 400, "Bad Request", {}, body)
        if method == "GET": return Response(json.dumps({"id": CHANNEL, "type": self.channel_type}).encode())
        return Response(json.dumps({"id": "9", "attachments": [{"id": "1"}]}).encode())


def run(cli, argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try: code = cli.main(argv)
        except SystemExit as exc: code = exc.code if isinstance(exc.code, int) else 1
    return code, out.getvalue(), err.getvalue()


def draft(cli, name, data=PNG_BYTES):
    path = cli.DRAFTS_ROOT / name
    path.write_bytes(data)
    return path


def check_thread_upload(cli):
    """在 thread 裡：只有一個 POST 到 thread，帶 bot token 與檔案 bytes。"""
    artifact = draft(cli, "orange-kitten.png")
    fake = Discord(); cli.urlopen = fake
    code, _, err = run(cli, ["--file", str(artifact), "--channel", CHANNEL, "--thread", THREAD, "--message", MESSAGE])
    check("thread upload exits 0", code == 0, err)
    check("thread upload deletes draft", not artifact.exists())
    check("thread upload is one request", len(fake.requests) == 1, str(len(fake.requests)))
    if len(fake.requests) != 1:
        return
    post = fake.requests[0]
    check("posts to the thread", post.full_url == f"https://discord.com/api/v10/channels/{THREAD}/messages", post.full_url)
    check("authorizes as a bot", headers_of(post).get("authorization") == f"Bot {TOKEN}")
    check("carries the file bytes", parts_of(post)["files[0]"][2] == PNG_BYTES)

def check_first_message_in_channel(cli):
    """頻道第一則：沒有 thread_id，查到不是 DM，送到 OpenAB 從訊息開的 thread（= message ID）。"""
    artifact = draft(cli, "first.md", b"# draft")
    fake = Discord(channel_type=0); cli.urlopen = fake
    code, _, err = run(cli, ["--file", str(artifact), "--channel", CHANNEL, "--message", MESSAGE])
    check("first message exits 0", code == 0, err)
    urls = [r.full_url for r in fake.requests]
    check("first message checks the channel then posts to the message thread", urls == [
        f"https://discord.com/api/v10/channels/{CHANNEL}",
        f"https://discord.com/api/v10/channels/{MESSAGE}/messages",
    ], repr(urls))


def check_dm(cli):
    artifact = draft(cli, "dm.png")
    fake = Discord(channel_type=1); cli.urlopen = fake
    code, _, err = run(cli, ["--file", str(artifact), "--channel", CHANNEL, "--message", MESSAGE])
    check("dm upload exits 0", code == 0, err)
    check("dm upload posts back to the DM", fake.requests[-1].full_url == f"https://discord.com/api/v10/channels/{CHANNEL}/messages",
          fake.requests[-1].full_url)
    for channel_type, want in ((1, "dm"), (3, "dm"), (0, "channel"), (11, "channel")):
        fake = Discord(channel_type=channel_type); cli.urlopen = fake
        code, out, err = run(cli, ["--check-dm", "--channel", CHANNEL])
        check(f"--check-dm type {channel_type}", code == 0 and out.strip() == want, f"{code} {out!r} {err}")
        check(f"--check-dm type {channel_type} only reads", [r.get_method() for r in fake.requests] == ["GET"])


def check_missing_message(cli):
    artifact = draft(cli, "orphan.md", b"# draft")
    fake = Discord(channel_type=0); cli.urlopen = fake
    code, _, err = run(cli, ["--file", str(artifact), "--channel", CHANNEL])
    check("no message outside thread/DM is input error", code == cli.EXIT_INVALID_INPUT, err)
    check("no message never posts", all(r.get_method() == "GET" for r in fake.requests))
    check("no message keeps draft", artifact.exists())


def check_failures(cli):
    for kind, fake in (("rejected", Discord(reject="POST")), ("transport", Discord(fail="POST")),
                       ("dm lookup", Discord(fail="GET"))):
        artifact = draft(cli, f"{kind.replace(' ', '-')}.md", b"# draft")
        cli.urlopen = fake
        argv = ["--file", str(artifact), "--channel", CHANNEL, "--message", MESSAGE]
        if kind != "dm lookup":
            argv += ["--thread", THREAD]
        code, _, err = run(cli, argv)
        check(f"{kind} exits runtime", code == cli.EXIT_RUNTIME, err)
        check(f"{kind} keeps draft", artifact.exists())
        check(f"{kind} never leaks token", TOKEN not in err)
        check(f"{kind} never leaks the body", "payload_json" not in err and "# draft" not in err)
    fake = Discord(reject="POST"); cli.urlopen = fake
    artifact = draft(cli, "archived.md", b"# draft")
    _, _, err = run(cli, ["--file", str(artifact), "--channel", CHANNEL, "--thread", THREAD])
    check("rejection reports Discord's code", "50083" in err and "Thread is archived" in err, err)


def check_rejected_inputs(cli, root):
    outside = root / "outside.png"; outside.write_bytes(PNG_BYTES)
    (cli.DRAFTS_ROOT / "bad.txt").write_text("x")
    keeper = draft(cli, "keeper.md", b"# draft")
    big = draft(cli, "big.png", PNG_BYTES + b"\0" * cli.MAX_ARTIFACT_BYTES)
    bad = [
        ("outside", ["--file", str(outside), "--channel", CHANNEL, "--thread", THREAD]),
        ("wrong extension", ["--file", str(cli.DRAFTS_ROOT / "bad.txt"), "--channel", CHANNEL, "--thread", THREAD]),
        ("over discord limit", ["--file", str(big), "--channel", CHANNEL, "--thread", THREAD]),
        ("bad channel", ["--file", str(keeper), "--channel", "https://evil", "--thread", THREAD]),
        ("bad thread", ["--file", str(keeper), "--channel", CHANNEL, "--thread", "../evil"]),
        ("bad message", ["--file", str(keeper), "--channel", CHANNEL, "--message", "C0123456789"]),
        ("no file", ["--channel", CHANNEL, "--thread", THREAD]),
        ("no url option", ["--file", str(keeper), "--channel", CHANNEL, "--thread", THREAD, "--url", "https://evil"]),
    ]
    for name, argv in bad:
        fake = Discord(); cli.urlopen = fake; code, _, _ = run(cli, argv)
        check(f"rejects {name}", code != 0)
        check(f"rejects {name} before network", not fake.requests)
    check("rejected inputs keep the draft", keeper.exists())


def main():
    cli = load(); root = Path(tempfile.mkdtemp(prefix="discord-artifact-test-"))
    try:
        cli.DRAFTS_ROOT = root / "drafts"; cli.DRAFTS_ROOT.mkdir()
        os.environ["DISCORD_BOT_TOKEN"] = TOKEN
        check_thread_upload(cli)
        check_first_message_in_channel(cli)
        check_dm(cli)
        check_missing_message(cli)
        check_failures(cli)
        check_rejected_inputs(cli, root)
    finally: shutil.rmtree(root, ignore_errors=True)
    if failures:
        print("discord-thread-artifact: failed", *failures, sep="\n  - ", file=sys.stderr); return 1
    print("Discord artifact checks passed (mocked Discord; no real call was made)."); return 0


if __name__ == "__main__": sys.exit(main())
