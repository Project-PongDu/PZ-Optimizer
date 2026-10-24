#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""collect_textures.py — 텍스쳐 최적화용 PNG 를 tex_conf/ 로 모은다 (build.bat 에서 빌드 전에 호출).

opt_conf 의 class 처럼 exe 에 묶어 배포하기 위해, 개발자 PC 의 Steam 워크샵에 받아둔
Every Texture Optimized 모드에서 PNG 만 복사한다. tex_conf/ 는 git 에 올리지 않는다(.gitignore).

    tex_conf/b41/...      ← 2812026801 "Every Texture Optimized - Performance mode"   <모드>/media/textures
    tex_conf/b42/...      ← 3119788162 "Every Texture Optimized Maximum Performance"   <모드>/42/media/textures
    tex_conf/info.json    {"built": <유닉스시각>, "b41": {...}, "b42": {...}}
                          built 가 바뀌면 옵티마이저가 '새 텍스쳐 있음 → 업데이트'로 표시한다.
                          (내용이 그대로면 built 도 그대로 둔다 — 쓸데없이 사용자 재적용을 유발하지 않도록)

수동 실행:  py collect_textures.py      (워크샵 폴더를 못 찾으면 --steam "E:\\Steam" 로 지정)
"""

import hashlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

PZ_APPID = "108600"
SOURCES = {
    "b41": ("2812026801", "media/textures"),
    "b42": ("3119788162", "42/media/textures"),
}
OUT = Path("tex_conf")


def steam_roots(extra):
    roots = [Path(extra)] if extra else []
    if os.name == "nt":
        try:
            import winreg
            for hive, key, val in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                                   (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath")):
                try:
                    with winreg.OpenKey(hive, key) as k:
                        roots.append(Path(str(winreg.QueryValueEx(k, val)[0])))
                except OSError:
                    pass
        except ImportError:
            pass
    libs = []
    for r in roots:
        libs.append(r)
        try:
            raw = (r / "steamapps" / "libraryfolders.vdf").read_text(encoding="utf-8", errors="replace")
            libs += [Path(m.replace("\\\\", "\\")) for m in re.findall(r'"path"\s+"((?:[^"\\]|\\.)*)"', raw)]
        except OSError:
            pass
    return libs


def find_source(libs, wid, sub):
    for lib in libs:
        mods = lib / "steamapps" / "workshop" / "content" / PZ_APPID / wid / "mods"
        if mods.is_dir():
            for m in sorted(mods.iterdir()):
                if (m / sub).is_dir():
                    return m / sub
    return None


def pngs(src):
    for dp, _dn, fn in os.walk(src):
        for f in sorted(fn):
            if f.lower().endswith(".png"):
                yield Path(dp) / f


def main():
    extra = sys.argv[sys.argv.index("--steam") + 1] if "--steam" in sys.argv else None
    libs = steam_roots(extra)
    old = {}
    try:
        old = json.loads((OUT / "info.json").read_text(encoding="utf-8"))
    except Exception:
        pass
    info = {}
    for build, (wid, sub) in SOURCES.items():
        src = find_source(libs, wid, sub)
        if src is None:
            print(f"[!] {build}: workshop item {wid} not found (subscribe to it in Steam, or pass --steam <Steam folder>)")
            return 1
        dst = OUT / build
        if dst.exists():
            shutil.rmtree(dst)
        h = hashlib.sha256()
        n = size = 0
        for f in pngs(src):
            rel = f.relative_to(src)
            t = dst / rel
            t.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, t)
            data = t.read_bytes()
            h.update(str(rel).replace("\\", "/").encode() + b"\0" + hashlib.sha256(data).digest())
            n += 1
            size += len(data)
        info[build] = {"workshop_id": wid, "files": n, "bytes": size, "digest": h.hexdigest()}
        print(f"  {build}: {n} files, {size / 1048576:.1f} MB  <- {src}")
    same = all(old.get(b, {}).get("digest") == info[b]["digest"] for b in SOURCES)
    info["built"] = old.get("built") if same and old.get("built") else int(time.time())
    (OUT / "info.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    print(f"  built = {info['built']}" + ("  (unchanged)" if same else "  (new textures)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
