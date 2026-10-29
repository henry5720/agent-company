#!/usr/bin/env python3
"""受限 Slack artifact uploader 的離線契約測試；不會開 socket。

來源 root 有三個（spec #21 §5、#24 決定 2、3）：drafts（bot 擁有，上傳成功就刪）、
thread workspace `work`（HTML prototype 與 diff `.md`，由 `thread-workspace cleanup`
7 天清，上傳後留著）、sandbox 的 `out`（bot 唯讀，由 sandbox runner 24 小時清）。
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
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qsl

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "agents/bin/slack-thread-artifact"
TOKEN = "test-token-not-a-secret"
CHANNEL = "C0123456789"
THREAD_TS = "1710000000.123456"
PNG_BYTES = b"\x89PNG\r\n\x1a\nmock"
failures: list[str] = []


def check(name, condition, detail=""):
    if not condition:
        failures.append(f"{name}: {detail}" if detail else name)


def load():
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_loader("slack_artifact", importlib.machinery.SourceFileLoader("slack_artifact", str(CLI)))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def headers_of(request):
    """urllib 會把 header key 轉成 `Content-type`；比較前一律轉小寫。"""
    return {key.lower(): value for key, value in request.headers.items()}


def form_body(request):
    """把 urlencoded body 解回 dict。非 ASCII 的 body 在這裡就會爆，那也是缺陷。"""
    return dict(parse_qsl(request.data.decode("ascii"), keep_blank_values=True))


class Response:
    def __init__(self, payload=b'{"ok": true}', status=200): self.payload, self.status = payload, status
    def read(self): return self.payload
    def __enter__(self): return self
    def __exit__(self, *_): return False


class Slack:
    """假 Slack：第一個請求回 ticket，其餘回 ok。`fail_at` 讓第 N 個請求炸掉。

    `reject_at` 讓第 N 個請求回 `ok: false`，用來驗 Slack 明確拒絕時的錯誤訊息。
    """

    def __init__(self, fail_at=None, reject_at=None, reject_error="invalid_arguments"):
        self.requests, self.fail_at = [], fail_at
        self.reject_at, self.reject_error = reject_at, reject_error

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        index = len(self.requests)
        if self.fail_at == index: raise OSError("mock failure")
        if self.reject_at == index: return Response(json.dumps({"ok": False, "error": self.reject_error}).encode())
        if index == 1: return Response(json.dumps({"ok": True, "upload_url": "https://uploads.slack.test/upload", "file_id": "F123"}).encode())
        return Response()


def run(cli, argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try: code = cli.main(argv)
        except SystemExit as exc: code = exc.code if isinstance(exc.code, int) else 1
    return code, out.getvalue(), err.getvalue()


def check_request_shapes(cli, root):
    """精確鎖住三個請求的 method、URL、headers 與 body。

    這是 `invalid_arguments` 那個 production bug 的迴歸測試：JSON body 送給
    `files.getUploadURLExternal` 會被 Slack 拒絕，所以這裡驗的是 form encoding
    本身，不只是「有打到那個 endpoint」。
    """
    artifact = cli.DRAFTS_ROOT / "orange-kitten.png"
    artifact.write_bytes(PNG_BYTES)
    fake = Slack(); cli.urlopen = fake
    code, _, err = run(cli, ["--file", str(artifact), "--channel", CHANNEL, "--thread-ts", THREAD_TS])
    check("happy path exits 0", code == 0, err)
    check("successful upload deletes draft", not artifact.exists())
    check("three fixed requests", len(fake.requests) == 3, str(len(fake.requests)))
    if len(fake.requests) != 3:
        return
    ticket, binary, complete = fake.requests

    # ---- 1. files.getUploadURLExternal：urlencoded filename/length
    check("ticket URL", ticket.full_url == "https://slack.com/api/files.getUploadURLExternal", ticket.full_url)
    check("ticket is POST", ticket.get_method() == "POST", ticket.get_method())
    ticket_headers = headers_of(ticket)
    check(
        "ticket sends form content type",
        ticket_headers.get("content-type") == "application/x-www-form-urlencoded",
        repr(ticket_headers.get("content-type")),
    )
    check("ticket never sends JSON content type", "json" not in (ticket_headers.get("content-type") or ""))
    check("ticket authorizes with bot token", ticket_headers.get("authorization") == f"Bearer {TOKEN}")
    check("ticket body is bytes", isinstance(ticket.data, bytes))
    ticket_body = form_body(ticket)
    check("ticket body has filename and length only", set(ticket_body) == {"filename", "length"}, str(sorted(ticket_body)))
    check("ticket filename is the draft name", ticket_body.get("filename") == "orange-kitten.png", str(ticket_body.get("filename")))
    check("ticket length is the byte count", ticket_body.get("length") == str(len(PNG_BYTES)), str(ticket_body.get("length")))
    check(
        "ticket body is not a JSON document",
        not ticket.data.lstrip().startswith(b"{"),
        ticket.data[:40].decode("ascii", "replace"),
    )

    # ---- 2. binary upload：Slack 給的 URL、mime、原始 bytes、沒有 token
    check("upload uses the returned URL", binary.full_url == "https://uploads.slack.test/upload", binary.full_url)
    check("upload is POST", binary.get_method() == "POST")
    binary_headers = headers_of(binary)
    check("upload sends the artifact mime", binary_headers.get("content-type") == "image/png", repr(binary_headers.get("content-type")))
    check("upload carries no bot token", "authorization" not in binary_headers, str(sorted(binary_headers)))
    check("upload sends the raw bytes", binary.data == PNG_BYTES)
    check("upload does not urlencode the body", b"filename=" not in binary.data)

    # ---- 3. files.completeUploadExternal：form，`files` 是 JSON 字串
    check("complete URL", complete.full_url == "https://slack.com/api/files.completeUploadExternal", complete.full_url)
    check("complete is POST", complete.get_method() == "POST")
    complete_headers = headers_of(complete)
    check(
        "complete sends form content type",
        complete_headers.get("content-type") == "application/x-www-form-urlencoded",
        repr(complete_headers.get("content-type")),
    )
    check("complete never sends JSON content type", "json" not in (complete_headers.get("content-type") or ""))
    check("complete authorizes with bot token", complete_headers.get("authorization") == f"Bearer {TOKEN}")
    complete_body = form_body(complete)
    check(
        "complete body has files, channel_id, thread_ts only",
        set(complete_body) == {"files", "channel_id", "thread_ts"},
        str(sorted(complete_body)),
    )
    check(
        "complete binds original channel/thread",
        complete_body.get("channel_id") == CHANNEL and complete_body.get("thread_ts") == THREAD_TS,
        f"{complete_body.get('channel_id')} {complete_body.get('thread_ts')}",
    )
    check(
        "complete body is not a JSON document",
        not complete.data.lstrip().startswith(b"{"),
        complete.data[:40].decode("ascii", "replace"),
    )
    files_field = complete_body.get("files", "")
    check("files field is a string in the form body", isinstance(files_field, str))
    try:
        parsed_files = json.loads(files_field)
    except json.JSONDecodeError as exc:
        check("files field is a JSON string", False, f"{exc}: {files_field!r}")
        return
    check(
        "files field decodes to one file object",
        isinstance(parsed_files, list) and len(parsed_files) == 1 and isinstance(parsed_files[0], dict),
        repr(parsed_files),
    )
    if isinstance(parsed_files, list) and parsed_files and isinstance(parsed_files[0], dict):
        entry = parsed_files[0]
        check("files entry carries the ticket file_id", entry.get("id") == "F123", repr(entry.get("id")))
        check("files entry titles with the draft name", entry.get("title") == "orange-kitten.png", repr(entry.get("title")))
        check("files entry has id and title only", set(entry) == {"id", "title"}, str(sorted(entry)))


def check_encoding_guard(cli):
    """非字串欄位必須當場失敗，而不是被 urlencode 轉成 Python repr 混過去。"""
    fake = Slack(); cli.urlopen = fake
    try:
        cli.slack_form("files.completeUploadExternal", {"files": [{"id": "F1"}]}, TOKEN)
    except cli.RuntimeFailure as exc:
        check("non-string field is refused", "must be encoded as a string" in str(exc), str(exc))
    except Exception as exc:  # noqa: BLE001 - 任何其他例外都算沒鎖住
        check("non-string field is refused", False, f"unexpected {type(exc).__name__}: {exc}")
    else:
        check("non-string field is refused", False, "slack_form accepted a list value")
    check("non-string field never reaches the network", not fake.requests)


def check_non_ascii_filename(cli):
    """UTF-8 檔名要 percent-encode 成 ASCII body，不能讓 urlencode 的輸出爆掉。"""
    artifact = cli.DRAFTS_ROOT / "橘貓.png"
    artifact.write_bytes(PNG_BYTES)
    fake = Slack(); cli.urlopen = fake
    code, _, err = run(cli, ["--file", str(artifact), "--channel", CHANNEL, "--thread-ts", THREAD_TS])
    check("utf-8 filename uploads", code == 0, err)
    if fake.requests:
        body = form_body(fake.requests[0])
        check("utf-8 filename round-trips", body.get("filename") == "橘貓.png", repr(body.get("filename")))


def check_rejection_errors(cli):
    """Slack 明確拒絕時：說出 method 與 error code，不吐 token、body 或 base64。"""
    for reject_at, method in ((1, "files.getUploadURLExternal"), (3, "files.completeUploadExternal")):
        candidate = cli.DRAFTS_ROOT / f"rejected-{reject_at}.md"
        candidate.write_text("# draft")
        fake = Slack(reject_at=reject_at); cli.urlopen = fake
        code, _, err = run(cli, ["--file", str(candidate), "--channel", CHANNEL, "--thread-ts", THREAD_TS])
        check(f"{method} rejection exits runtime", code == cli.EXIT_RUNTIME, err)
        check(f"{method} rejection keeps draft", candidate.exists())
        check(f"{method} rejection names the method", method in err, err)
        check(f"{method} rejection reports the error code", "invalid_arguments" in err, err)
        check(f"{method} rejection never leaks token", TOKEN not in err)
        check(f"{method} rejection never leaks the request body", "filename=" not in err and "channel_id=" not in err)
        check(f"{method} rejection never leaks the draft bytes", base64.b64encode(b"# draft").decode() not in err)


def check_transport_failures(cli):
    for fail_at in (1, 2, 3):
        candidate = cli.DRAFTS_ROOT / f"failure-{fail_at}.md"
        candidate.write_text("# draft")
        fake = Slack(fail_at); cli.urlopen = fake
        code, _, err = run(cli, ["--file", str(candidate), "--channel", CHANNEL, "--thread-ts", "1710000000.1"])
        check(f"failure {fail_at} exits runtime", code == cli.EXIT_RUNTIME, err)
        check(f"failure {fail_at} keeps draft", candidate.exists())
        check(f"failure {fail_at} never leaks token", TOKEN not in err)
        check(f"failure {fail_at} never leaks the request body", "filename=" not in err and "channel_id=" not in err)


def check_thread_workspace_and_out(cli, root):
    """work 與 out 底下的產物上傳得出去，而且上傳後留在原地。"""
    key = "C0123456789-1710000000.123456"
    repo = cli.WORK_ROOT / key / "repos" / "app"
    (repo / "prototype").mkdir(parents=True)
    shots = cli.OUT_ROOT / key
    shots.mkdir(parents=True)
    cases = (
        ("HTML prototype from work", repo / "prototype" / "checkout.html", b"<!doctype html><p>hi</p>", "text/html"),
        ("diff .md from work", repo / "change.md", b"```diff\n+x\n```\n", "text/markdown"),
        ("screenshot from out", shots / "variant-a.png", PNG_BYTES, "image/png"),
    )
    for label, path, data, mime in cases:
        path.write_bytes(data)
        fake = Slack(); cli.urlopen = fake
        code, out, err = run(cli, ["--file", str(path), "--channel", CHANNEL, "--thread-ts", THREAD_TS])
        check(f"{label} uploads", code == 0, err)
        check(f"{label} makes the three requests", len(fake.requests) == 3, str(len(fake.requests)))
        if len(fake.requests) == 3:
            check(f"{label} uploads the bytes", fake.requests[1].data == data)
            check(f"{label} uses its mime", headers_of(fake.requests[1]).get("content-type") == mime)
            complete = form_body(fake.requests[2])
            check(f"{label} lands in the thread", complete.get("channel_id") == CHANNEL and complete.get("thread_ts") == THREAD_TS)
        check(f"{label} is left in place after upload", path.exists())
        check(f"{label} reports no delete problem", not err.strip(), err)

    # 失敗也一樣留著（out 由 runner 清、work 由 thread-workspace cleanup 清）。
    failing = shots / "variant-b.png"; failing.write_bytes(PNG_BYTES)
    fake = Slack(fail_at=2); cli.urlopen = fake
    code, _, _ = run(cli, ["--file", str(failing), "--channel", CHANNEL, "--thread-ts", THREAD_TS])
    check("out upload failure exits runtime", code == cli.EXIT_RUNTIME)
    check("out upload failure keeps the file", failing.exists())

    # work 底下指出去的 symlink 不跟。
    outside = root / "secret.md"; outside.write_text("# secret")
    link = repo / "leak.md"; link.symlink_to(outside)
    fake = Slack(); cli.urlopen = fake
    code, _, _ = run(cli, ["--file", str(link), "--channel", CHANNEL, "--thread-ts", THREAD_TS])
    check("symlink in work is refused", code == cli.EXIT_INVALID_INPUT, str(code))
    check("symlink in work never reaches the network", not fake.requests)

    # 名字以 root 開頭但不是 root 底下（`work-evil`）不算。
    sibling = Path(str(cli.WORK_ROOT) + "-evil"); sibling.mkdir()
    (sibling / "x.md").write_text("# x")
    fake = Slack(); cli.urlopen = fake
    code, _, _ = run(cli, ["--file", str(sibling / "x.md"), "--channel", CHANNEL, "--thread-ts", THREAD_TS])
    check("sibling of work is refused", code == cli.EXIT_INVALID_INPUT, str(code))
    check("sibling of work never reaches the network", not fake.requests)


def check_rejected_inputs(cli, root):
    outside = root / "outside.png"; outside.write_bytes(b"\x89PNG\r\n\x1a\n")
    (cli.DRAFTS_ROOT / "bad.txt").write_text("x")
    keeper = cli.DRAFTS_ROOT / "failure-1.md"
    bad = [
        ("outside", ["--file", str(outside), "--channel", CHANNEL, "--thread-ts", "1710000000.1"]),
        ("wrong extension", ["--file", str(cli.DRAFTS_ROOT / "bad.txt"), "--channel", CHANNEL, "--thread-ts", "1710000000.1"]),
        ("bad channel", ["--file", str(keeper), "--channel", "https://evil", "--thread-ts", "1710000000.1"]),
        ("bad thread", ["--file", str(keeper), "--channel", CHANNEL, "--thread-ts", "evil"]),
        ("no url option", ["--file", str(keeper), "--channel", CHANNEL, "--thread-ts", "1710000000.1", "--url", "https://evil"]),
    ]
    for name, argv in bad:
        fake = Slack(); cli.urlopen = fake; code, _, _ = run(cli, argv)
        check(f"rejects {name}", code != 0)
        check(f"rejects {name} before network", not fake.requests)


def main():
    cli = load(); root = Path(tempfile.mkdtemp(prefix="slack-artifact-test-"))
    try:
        cli.DRAFTS_ROOT = root / "drafts"; cli.DRAFTS_ROOT.mkdir()
        cli.WORK_ROOT = root / "work"; cli.WORK_ROOT.mkdir()
        cli.OUT_ROOT = root / "out"; cli.OUT_ROOT.mkdir()
        os.environ["SLACK_BOT_TOKEN"] = TOKEN
        check_request_shapes(cli, root)
        check_encoding_guard(cli)
        check_non_ascii_filename(cli)
        check_rejection_errors(cli)
        check_transport_failures(cli)
        check_thread_workspace_and_out(cli, root)
        check_rejected_inputs(cli, root)
    finally: shutil.rmtree(root, ignore_errors=True)
    if failures:
        print("slack-thread-artifact: failed", *failures, sep="\n  - ", file=sys.stderr); return 1
    print("Slack artifact checks passed (mocked Slack; no real call was made)."); return 0


if __name__ == "__main__": sys.exit(main())
