#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make_version.py — 릴리즈용 version.json 생성 (build.bat 마지막 단계에서 호출).

optimizer.py 의 VERSION 을 읽고 dist/PZ-Optimizer.exe 를 해시해서 dist/version.json 을 만든다.
GitHub 릴리즈에 exe 와 이 json 을 함께 올리면 PZ Optimizer 자동 업데이터가 이걸 보고 동작한다.

  · 릴리즈 태그는 VERSION 과 정확히 같아야 한다 (url 이 태그 경로로 만들어지므로)
  · notes 는 비어서 나오므로 릴리즈 올리기 전에 채울 것
  · min_supported 는 기본으로 넣지 않는다. 구버전 사용을 막아야 할 때만 손으로 추가한다
    (예: 포함된 GraalVM 에 보안 패치가 필요할 때) — 넣는 순간 그 미만 버전은 실행 즉시 업데이트 강제.

수동 실행:  py make_version.py
"""

import hashlib
import json
import pathlib
import re
import sys

REPO = "Project-PongDu/PZ-Optimizer"
EXE = pathlib.Path("dist/PZ-Optimizer.exe")
OUT = pathlib.Path("dist/version.json")


def read_version() -> str:
    src = pathlib.Path("optimizer.py").read_text(encoding="utf-8")
    m = re.search(r"""^VERSION\s*=\s*["'](.+?)["']""", src, re.M)
    if not m:
        raise SystemExit("[!] optimizer.py 에서 VERSION 을 찾지 못했습니다.")
    return m.group(1)


def sha256_of(p: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:                     # exe 가 수백 MB 라 통째로 읽지 않는다
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    if not EXE.exists():
        print("[!] %s 가 없습니다. 먼저 빌드하세요." % EXE)
        return 1
    version = read_version()
    digest = sha256_of(EXE)
    manifest = {
        "version": version,
        "url": "https://github.com/%s/releases/download/%s/PZ-Optimizer.exe" % (REPO, version),
        "sha256": digest,
        "notes": "",
    }
    OUT.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    print("  version = %s" % version)
    print("  sha256  = %s" % digest)
    print("  size    = %.1f MB" % (EXE.stat().st_size / 1048576.0))
    print("  wrote   = %s" % OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
