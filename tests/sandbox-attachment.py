#!/usr/bin/env python3
"""sandbox 內 `sandbox-attachment` 的離線契約測試。

用 127.0.0.1 的本機 HTTP server 代替 OpenAB 的 presigned URL，用 PATH 上的假 `npm`
代替現裝的 officeparser。不需要 Docker、不連外網，也沒有真的轉檔；真的轉檔在
deployment host 用 sandbox image 人工驗（#28）。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "sandbox/sandbox-attachment"
VERSION = "8.3.0"
PDF = b"%PDF-1.4\n% fake pdf body\n"
OOXML = b"PK\x03\x04" + b"\0" * 64
failures: list[str] = []

FAKE_NPM = r"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
log = Path(os.environ["FAKE_NPM_LOG"])
args = sys.argv[1:]
source = next(a for a in args if a.startswith("/"))
log.write_text(json.dumps({"argv": args, "cwd": os.getcwd(), "bytes": Path(source).read_bytes().hex()}))
mode = os.environ.get("FAKE_NPM_MODE", "ok")
if mode == "fail":
    print("officeparser: Error: corrupt file", file=sys.stderr); sys.exit(1)
if mode == "empty":
    sys.exit(0)
print("# converted\n\nMARKER from " + Path(source).name)
"""


def check(name, condition, detail=""):
    if not condition:
        failures.append(f"{name}: {detail}" if detail else name)


class Files(BaseHTTPRequestHandler):
    routes: dict[str, tuple[int, bytes, dict]] = {}

    def log_message(self, *_):
        pass

    def do_GET(self):  # noqa: N802
        status, body, headers = self.routes.get(self.path.split("?")[0], (404, b"missing", {}))
        self.send_response(status)
        self.send_header("Content-Length", headers.get("Content-Length", str(len(body))))
        self.end_headers()
        self.wfile.write(body)


class Harness:
    def __init__(self, root: Path):
        self.root = root
        self.out, self.bin, self.cwd = root / "out", root / "bin", root / "cwd"
        for path in (self.out, self.bin, self.cwd):
            path.mkdir()
        npm = self.bin / "npm"
        npm.write_text(FAKE_NPM); npm.chmod(0o755)
        self.log = root / "npm.json"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Files)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown(); self.server.server_close()

    def serve(self, path, body, status=200, **headers):
        Files.routes[path] = (status, body, headers)
        return self.base + path + "?X-Amz-Signature=abc&X-Amz-Expires=3600"

    def run(self, *argv, mode="ok", version=VERSION):
        if self.log.exists():
            self.log.unlink()
        for entry in self.out.iterdir():
            entry.unlink()
        env = {"PATH": f"{self.bin}:{os.environ['PATH']}", "OUT_DIR": str(self.out),
               "FAKE_NPM_LOG": str(self.log), "FAKE_NPM_MODE": mode, "HOME": str(self.root)}
        if version is not None:
            env["OFFICEPARSER_VERSION"] = version
        proc = subprocess.run([str(CLI), *argv], cwd=self.cwd, env=env, capture_output=True, text=True, timeout=60)
        return proc.returncode, proc.stdout, proc.stderr

    def npm_call(self):
        return json.loads(self.log.read_text()) if self.log.exists() else None

    def outputs(self):
        return sorted(p.name for p in self.out.iterdir())


def check_formats(h):
    for name, body, kind in [("brief.pdf", PDF, "pdf"), ("spec.docx", OOXML, "docx"), ("quote.xlsx", OOXML, "xlsx"), ("deck.pptx", OOXML, "pptx")]:
        url = h.serve("/incoming/" + name, body)
        code, out, err = h.run(url, name)
        check(f"{kind}: exits 0", code == 0, err)
        target = h.out / f"{name}.md"
        check(f"{kind}: writes out/<name>.md", h.outputs() == [f"{name}.md"], str(h.outputs()))
        check(f"{kind}: output is the converter text", target.exists() and "MARKER" in target.read_text(), err)
        check(f"{kind}: prints the path for the agent", f"{target}" in out, out)
        check(f"{kind}: bot can read the output (0644 like other out files)", target.exists() and (target.stat().st_mode & 0o777) == 0o644,
              oct(target.stat().st_mode) if target.exists() else "missing")
        call = h.npm_call() or {"argv": []}
        argv = call["argv"]
        check(f"{kind}: installs the pinned officeparser on the fly",
              argv[:3] == ["exec", "--yes", f"--package=officeparser@{VERSION}"] and argv[3:5] == ["--", "officeparser"], str(argv))
        check(f"{kind}: asks for markdown", "--to=md" in argv, str(argv))
        check(f"{kind}: names the type instead of sniffing", f"--fileType={kind}" in argv, str(argv))
        check(f"{kind}: never fetches pdf.js/OCR assets from a CDN", "--allowRemoteAssets=false" in argv, str(argv))
        check(f"{kind}: converts the exact downloaded bytes", call.get("bytes") == body.hex())
        check(f"{kind}: download stays out of out/", not any(p.suffix != ".md" for p in h.out.iterdir()))


def check_names(h):
    url = h.serve("/incoming/q", OOXML)
    code, _, err = h.run(url, "報價 v2 (final).XLSX")
    check("unicode filename is kept readable", code == 0 and h.outputs() == ["報價_v2__final_.xlsx.md"], f"{h.outputs()} {err}")
    code, _, err = h.run(url, "../../etc/quote.xlsx")
    check("path in filename is reduced to its base name", code == 0 and h.outputs() == ["quote.xlsx.md"], f"{h.outputs()} {err}")


def check_rejections(h):
    pdf_url = h.serve("/incoming/brief.pdf", PDF)
    zip_url = h.serve("/incoming/a.zip", OOXML)
    cases = [
        ("unsupported extension", (zip_url, "archive.zip"), 2),
        ("no extension", (pdf_url, "brief"), 2),
        ("file url", ("file:///etc/passwd", "brief.pdf"), 2),
        ("missing filename", (pdf_url,), 2),
        ("content does not match extension", (h.serve("/incoming/fake.pdf", OOXML), "fake.pdf"), 3),
        ("http error (expired URL)", (h.serve("/incoming/gone.pdf", b"expired", status=403), "gone.pdf"), 3),
        ("over 50 MiB", (h.serve("/incoming/huge.pdf", PDF, **{"Content-Length": str(60 * 1024 * 1024)}), "huge.pdf"), 3),
    ]
    for name, argv, expected in cases:
        code, _, err = h.run(*argv)
        check(f"rejects {name} with exit {expected}", code == expected, f"{code} {err}")
        check(f"rejects {name} without output", h.outputs() == [], str(h.outputs()))
        check(f"rejects {name} with a message", "sandbox-attachment:" in err, err)
        if expected == 2:
            check(f"rejects {name} before converting", h.npm_call() is None)
    code, _, err = h.run(pdf_url, "brief.pdf", version=None)
    check("missing pinned version fails before any install", code == 3 and h.npm_call() is None, f"{code} {err}")


def check_converter_failures(h):
    url = h.serve("/incoming/brief.pdf", PDF)
    code, _, err = h.run(url, "brief.pdf", mode="fail")
    check("converter failure exits 3", code == 3, f"{code} {err}")
    check("converter failure leaves no partial output", h.outputs() == [], str(h.outputs()))
    check("converter error is shown", "corrupt file" in err, err)
    code, _, err = h.run(url, "brief.pdf", mode="empty")
    check("no extractable text exits 3", code == 3, f"{code} {err}")
    check("no extractable text explains OCR is unsupported", "OCR" in err, err)
    check("no extractable text leaves no output", h.outputs() == [], str(h.outputs()))


def main():
    root = Path(tempfile.mkdtemp(prefix="sandbox-attachment-test-"))
    h = Harness(root)
    try:
        check_formats(h)
        check_names(h)
        check_rejections(h)
        check_converter_failures(h)
    finally:
        h.close(); shutil.rmtree(root, ignore_errors=True)
    if failures:
        print("sandbox-attachment: failed", *failures, sep="\n  - ", file=sys.stderr); return 1
    print("sandbox-attachment checks passed (local HTTP server and fake npm; no real conversion)."); return 0


if __name__ == "__main__":
    sys.exit(main())
