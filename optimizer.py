#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
optimizer.py  —  PZ Optimizer : Project Zomboid 설치 폴더 최적화 도구

퐁듀 런처(gui.py)에 들어 있던 '게임 최적화' 기능을 별도 exe 로 분리한 것.
런처는 방송마다 켜는 상시 프로그램이고 이쪽은 설치/패치 후 1회 실행하는 관리자 도구라,
GraalVM(수백 MB)을 묶어도 런처 시작 속도·자동 업데이트 용량에 영향이 없다.

항목 (체크 = 적용 / 해제 = 원본 복원, 자동 적용 없음)
    chunk / world / netzombie / zcount : 좀비·청크 연산 패치 class 교체 (B41 전용)
    heap   : JVM 힙 = 전체 RAM 의 절반 (ProjectZomboid64.json + .bat, B41·B42 공통)
    graal  : jre64 → Oracle GraalVM 교체 + JVM 인자 2개 추가 (Java 25 = B42 전용)
               -XX:+AlwaysPreTouch                        시작 시 힙 페이지를 미리 확보
               -Djdk.graal.CompilerConfiguration=enterprise  Graal JIT 최적화 등급 고정

백업 (게임 폴더 puppet_opt_backup/)
    · class / json / bat : 최초 1회 복사 (원본 보존). json/bat 은 '원본 힙 값'을 읽는 기준으로만
      쓰고, 복원은 인자 단위로 한다 — heap 과 graal 이 같은 json 을 건드리므로 파일째 덮으면
      다른 항목의 인자까지 날아간다.
    · jre64 : 복사하지 않고 puppet_opt_backup/jre64 로 '이름만 이동' (같은 드라이브라 즉시 끝남).
    · pongdu_opt_state.json : 적용된 항목의 지문(class sha256, 힙 크기, GraalVM 버전·인자).
      퐁듀 런처가 이 파일만 읽어서 'PZ 업데이트로 최적화가 풀렸는지'를 표시한다.

자동 업데이트: 런처와 같은 방식. GitHub Releases 의 version.json 을 보고 새 버전이면
    다이얼로그 → [지금 업데이트] 시 같은 폴더에 .new 로 받아 sha256 검증 후 자기 교체·재실행.

빌드: build.bat (PyInstaller --onefile --uac-admin → 항상 관리자 권한으로 실행)
      → make_version.py 가 dist/version.json 생성 (릴리스에 exe 와 함께 첨부)
"""

import hashlib
import json
import os
import re
import shutil
import stat
import sys
import threading
import time
import zipfile
from collections import namedtuple
from pathlib import Path

from PyQt5.QtCore import Qt, QObject, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import (
    QApplication, QWidget, QLabel, QLineEdit, QPushButton, QVBoxLayout, QHBoxLayout,
    QFileDialog, QFrame, QCheckBox, QMessageBox, QScrollArea, QProgressBar, QDialog, QTextEdit,
)


VERSION = "v1.3.0"
APP_TITLE = "PZ Optimizer"
PAGE_TITLE = "프로젝트 좀보이드 최적화 툴"

# ── 자동 업데이트 (런처와 같은 구조) ──────────────────────────────────────────
# 릴리즈마다 asset 두 개: PZ-Optimizer.exe + version.json (build.bat 의 make_version.py 가 생성)
# version.json: {"version", "url", "sha256", "notes", "min_supported"(선택)}
UPDATE_MANIFEST_URL = "https://github.com/Project-PongDu/PZ-Optimizer/releases/latest/download/version.json"
UPDATE_TIMEOUT      = 10.0    # 매니페스트 조회 한도 (초). 실패는 조용히 무시
UPDATE_DL_TIMEOUT   = 60.0    # exe 다운로드 소켓 타임아웃 (초)
# 다운로드 허용 호스트. 리다이렉트를 따라간 뒤 최종 URL 도 이 목록으로 재검증한다.
UPDATE_ALLOWED_HOSTS = ("github.com", "objects.githubusercontent.com",
                        "release-assets.githubusercontent.com")
IS_FROZEN = bool(getattr(sys, "frozen", False))

OPT_DIRNAME = "opt_conf"                        # 패치 class 묶음 (exe 에 --add-data 로 포함)
GRAAL_ZIP_NAME = "graalvm-jdk-25_windows-x64_bin.zip"   # Oracle 공식 zip 원본 그대로 포함
OPT_BACKUP_DIRNAME = "puppet_opt_backup"        # 런처 시절과 같은 이름 — 기존 백업 그대로 이어씀
STATE_FILE = "pongdu_opt_state.json"            # 퐁듀 런처가 읽는 적용 상태 기록
PZ_APPID = "108600"
ICON_FILE = "optimizer.ico"

GRAAL_VMARGS = ["-XX:+AlwaysPreTouch", "-Djdk.graal.CompilerConfiguration=enterprise"]
JRE_TMP_DIRNAME = "jre64.__pzopt_tmp"           # 압축 해제 임시 폴더 (끝나면 jre64 로 이름 변경)
DISK_MARGIN_MB = 512                            # 압축 해제 크기 외 여유 공간
PRETOUCH_WARN_RAM_MB = 32 * 1024                # 이 미만이면 AlwaysPreTouch 경고 문구 표시


def resource_path(rel):
    """exe(PyInstaller)로 묶였을 때든 그냥 실행이든 리소스 파일 경로를 찾는다."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


# ── 설정 (%APPDATA%\PZ-Optimizer\config.json) ────────────────────────────────
# 퐁듀 런처도 이 파일의 pz_dir 을 읽어 '최적화 상태'를 표시할 폴더를 정한다.
def config_path() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "PZ-Optimizer" / "config.json"


def load_config() -> dict:
    try:
        d = json.loads(config_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_config(cfg: dict):
    try:
        p = config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


# ── 시스템 정보 ───────────────────────────────────────────────────────────────
def total_ram_mb() -> int:
    """전체 물리 RAM (MB). Windows는 GlobalMemoryStatusEx, 개발용(posix)은 sysconf."""
    if os.name == "nt":
        import ctypes
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]
        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return int(st.ullTotalPhys // (1024 * 1024))
        return 0
    try:
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // (1024 * 1024))
    except (ValueError, OSError, AttributeError):
        return 0


def half_ram_mb() -> int:
    """할당할 힙 크기 = 전체 RAM의 절반 (256MB 단위 절사, 최소 2048)."""
    total = total_ram_mb()
    if total <= 0:
        return 0
    return max(2048, (total // 2 // 256) * 256)


def is_admin() -> bool:
    if os.name != "nt":
        return True
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def pz_running() -> bool:
    """실제 프로세스 목록으로 PZ 실행 여부 확인 (실행 중이면 jvm.dll·class 가 잠겨 교체 불가)."""
    KEY = "projectzomboid"
    try:                                         # psutil 있으면 우선 (의존성 아님)
        import psutil  # pyright: ignore[reportMissingModuleSource]
        for p in psutil.process_iter(["name"]):
            if KEY in (p.info.get("name") or "").lower():
                return True
        return False
    except Exception:
        pass
    import subprocess
    if os.name == "nt":
        try:
            CREATE_NO_WINDOW = 0x08000000        # noconsole exe 에서 콘솔창 안 뜨게
            out = subprocess.run(["tasklist"], capture_output=True, text=True,
                                 creationflags=CREATE_NO_WINDOW).stdout.lower()
            return KEY in out
        except Exception:
            return False
    try:                                         # 개발용 폴백 (mac/linux)
        out = subprocess.run(["pgrep", "-fil", KEY], capture_output=True, text=True).stdout.lower()
        return KEY in out
    except Exception:
        return False


# ── PZ 설치 폴더 탐색 ─────────────────────────────────────────────────────────
def _steam_roots():
    """Steam 루트 후보들: 레지스트리(HKCU 우선) → 드라이브 스캔. 중복 제거."""
    roots = []
    if os.name == "nt":
        try:
            import winreg
            for hive, key, val in (
                (winreg.HKEY_CURRENT_USER,  r"Software\Valve\Steam",              "SteamPath"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam",  "InstallPath"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam",              "InstallPath"),
            ):
                try:
                    with winreg.OpenKey(hive, key) as k:
                        v, _ = winreg.QueryValueEx(k, val)
                    p = Path(str(v))
                    if p.exists():
                        roots.append(p)
                except OSError:
                    pass
        except ImportError:
            pass
    for drive in ("C:", "D:", "E:", "F:", "G:", "H:"):
        for sub in ("Steam", "SteamLibrary",
                    "Program Files (x86)\\Steam", "Program Files\\Steam"):
            p = Path(drive + "\\") / sub
            if p.exists():
                roots.append(p)
    seen, out = set(), []
    for r in roots:
        key = str(r).lower()
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


_VDF_PATH_RE = re.compile(r'"path"\s+"((?:[^"\\]|\\.)*)"')


def _libraries_from(steam_root: Path):
    """steamapps/libraryfolders.vdf 파싱 → 이 Steam이 아는 모든 라이브러리 폴더."""
    libs = [steam_root]
    vdf = steam_root / "steamapps" / "libraryfolders.vdf"
    try:
        raw = vdf.read_text(encoding="utf-8", errors="replace")
        for m in _VDF_PATH_RE.finditer(raw):
            libs.append(Path(m.group(1).replace("\\\\", "\\")))
    except OSError:
        pass
    return libs


def find_pz_dir():
    """PZ 설치 폴더 탐색. appmanifest_108600.acf가 있는 라이브러리 우선(별도 SteamLibrary 지원),
       없으면 폴더 존재만으로 폴백. 못 찾으면 None."""
    roots = _steam_roots()
    for root in roots:
        for lib in _libraries_from(root):
            steamapps = lib / "steamapps"
            if (steamapps / f"appmanifest_{PZ_APPID}.acf").exists():
                g = steamapps / "common" / "ProjectZomboid"
                if g.exists():
                    return g
    for root in roots:
        g = root / "steamapps" / "common" / "ProjectZomboid"
        if g.exists():
            return g
    return None


def is_pz_dir(p) -> bool:
    """최소 조건: ProjectZomboid64.json 이 있어야 우리가 아는 PZ 설치 폴더."""
    try:
        return p is not None and (Path(p) / "ProjectZomboid64.json").exists()
    except OSError:
        return False


def initial_game_dir():
    """마지막으로 고른 폴더 우선, 없으면 자동 탐색."""
    saved = load_config().get("pz_dir", "")
    if saved and is_pz_dir(saved):
        return Path(saved)
    return find_pz_dir()


# ── JRE 판별 (jre64/release) ──────────────────────────────────────────────────
def parse_release(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        k, sep, v = line.partition("=")
        if sep:
            out[k.strip()] = v.strip().strip('"')
    return out


def read_release(jre_dir: Path) -> dict:
    try:
        return parse_release((jre_dir / "release").read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return {}


def java_major(rel: dict) -> int:
    m = re.match(r"(\d+)", rel.get("JAVA_VERSION", ""))
    return int(m.group(1)) if m else 0


def is_graal_release(rel: dict) -> bool:
    return bool(rel.get("GRAALVM_VERSION"))


def jre_state(game_dir: Path) -> str:
    """'graal' | 'vanilla' | 'mixed' | 'missing'
       mixed = Steam 무결성 검사/업데이트가 Graal 위에 원본 파일을 덮어써서 섞인 상태.
               release 는 원본으로 돌아왔는데 Graal 전용 파일이 남아 있으면 이걸로 본다."""
    j = game_dir / "jre64"
    if not j.is_dir():
        return "missing"
    if is_graal_release(read_release(j)):
        return "graal"
    if (j / "GRAALVM-README.md").exists() or (j / "bin" / "jvmcicompiler.dll").exists():
        return "mixed"
    return "vanilla"


def vanilla_release(game_dir: Path) -> dict:
    """게임 원래 JRE 의 release. jre64 가 Graal 이거나 섞였으면 백업본을 본다."""
    if jre_state(game_dir) == "vanilla":
        return read_release(game_dir / "jre64")
    bk = read_release(game_dir / OPT_BACKUP_DIRNAME / "jre64")
    return bk or read_release(game_dir / "jre64")


BUILD_BY_JAVA = {17: "b41", 25: "b42"}      # 번들 JRE 의 Java 메이저 버전 → PZ 빌드


def detect_build(game_dir: Path):
    """'b41' | 'b42' | None(알 수 없음). jre64 가 Graal 로 바뀌어 있어도 원본(백업) 기준."""
    return BUILD_BY_JAVA.get(java_major(vanilla_release(game_dir)))


def build_label(game_dir: Path) -> str:
    rel = vanilla_release(game_dir)
    major = java_major(rel)
    name = {"b41": "B41", "b42": "B42"}.get(BUILD_BY_JAVA.get(major), "알 수 없는 빌드")
    jre = {"graal": "GraalVM " + read_release(game_dir / "jre64").get("GRAALVM_VERSION", ""),
           "vanilla": "원본", "mixed": "섞임 (재적용 필요)", "missing": "없음"}[jre_state(game_dir)]
    return f"감지된 빌드: {name} (Java {major or '?'})  ·  현재 JRE: {jre}"


# ── 번들된 GraalVM zip ────────────────────────────────────────────────────────
_GRAAL_INFO = None


def graal_zip_path():
    p = Path(resource_path(GRAAL_ZIP_NAME))
    return p if p.is_file() else None


def graal_zip_info():
    """(최상위 폴더명, release dict, 압축 해제 크기 bytes) | None. 결과는 캐시."""
    global _GRAAL_INFO
    if _GRAAL_INFO is not None:
        return _GRAAL_INFO or None
    _GRAAL_INFO = ()
    zp = graal_zip_path()
    if zp is None:
        return None
    try:
        with zipfile.ZipFile(zp) as z:
            infos = z.infolist()
            tops = {i.filename.split("/", 1)[0] for i in infos}
            if len(tops) != 1:
                return None
            top = tops.pop()
            rel = parse_release(z.read(f"{top}/release").decode("utf-8", "replace"))
            size = sum(i.file_size for i in infos)
    except (OSError, KeyError, zipfile.BadZipFile):
        return None
    if not is_graal_release(rel):
        return None
    _GRAAL_INFO = (top, rel, size)
    return _GRAAL_INFO


# ── 항목 정의 ─────────────────────────────────────────────────────────────────
# key   : 설정/상태 기록용 식별자
# kind  : "class" = opt_conf 패치본으로 교체 / "heap" = 힙 인자 치환 / "graal" = JRE 교체 + 인자
# label : 체크박스 표시명
# files : 게임 폴더 기준 상대경로. class/heap 은 백업 대상이기도 하다 (그룹 내 전부 함께 움직임).
#         IsoWorld$* 는 IsoWorld 내부 클래스라 버전이 서로 맞아야 해서 한 그룹으로 묶는다.
# builds: 화면에 보여줄 빌드. 선택한 설치본의 빌드에 해당하는 항목만 목록에 나온다.
OptGroup = namedtuple("OptGroup", "key kind label files builds")

OPT_GROUPS = [
    OptGroup("chunk", "class", "청크 로딩 최적화",
             ["zombie/iso/IsoChunkMap.class"], ("b41",)),
    OptGroup("world", "class", "월드 업데이트 / 좀비 정렬 연산 최적화",
             ["zombie/iso/IsoWorld.class",
              "zombie/iso/IsoWorld$CompDistToPlayer.class",
              "zombie/iso/IsoWorld$CompScoreToPlayer.class",
              "zombie/iso/IsoWorld$Frame.class",
              "zombie/iso/IsoWorld$MetaCell.class",
              "zombie/iso/IsoWorld$s_performance.class"], ("b41",)),
    OptGroup("netzombie", "class", "좀비 네트워크 패킷 최적화",
             ["zombie/popman/NetworkZombiePacker.class"], ("b41",)),
    OptGroup("zcount", "class", "좀비 개체수 연산 최적화",
             ["zombie/popman/ZombieCountOptimiser.class"], ("b41",)),
    OptGroup("heap", "heap", "JVM 힙 메모리 = 전체 RAM 의 절반",
             ["ProjectZomboid64.json", "ProjectZomboid64.bat"], ("b41", "b42")),
    OptGroup("graal", "graal", "JVM 교체: Oracle GraalVM + AlwaysPreTouch",
             ["jre64/  →  Oracle GraalVM (원본은 puppet_opt_backup/jre64 로 이동)",
              "ProjectZomboid64.json / .bat  +  " + "  ".join(GRAAL_VMARGS)], ("b42",)),
]
OPT_GROUP_BY_KEY = {g.key: g for g in OPT_GROUPS}

_XMX_RE = re.compile(r"-Xmx\d+[mMgG]")
_XMS_RE = re.compile(r"-Xms\d+[mMgG]")
_BAT_JAVA_RE = re.compile(r'(jre64\\bin\\java\.exe")', re.I)


def opt_conf_dir():
    """패치 리소스 폴더(opt_conf). 빌드에 안 들어갔으면 None → class 항목 비활성."""
    p = Path(resource_path(OPT_DIRNAME))
    return p if p.is_dir() else None


# ── 파일 헬퍼 ─────────────────────────────────────────────────────────────────
def _backup_once(game_dir: Path, rel):
    """원본을 puppet_opt_backup/에 백업. 이미 백업본이 있으면 건드리지 않음(최초 원본 보존)."""
    src = game_dir / rel
    dst = game_dir / OPT_BACKUP_DIRNAME / rel
    if src.exists() and not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def _rmtree(p: Path):
    """읽기 전용 속성 파일까지 지우는 rmtree."""
    def _force(func, path, _exc):
        os.chmod(path, stat.S_IWRITE)
        func(path)
    if sys.version_info >= (3, 12):
        shutil.rmtree(p, onexc=_force)
    else:
        shutil.rmtree(p, onerror=_force)


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_json(p: Path):
    """(data, indent). 원본 들여쓰기(탭/공백)를 유지해 다시 쓰기 위함."""
    raw = p.read_text(encoding="utf-8")
    return json.loads(raw), ("\t" if "\n\t" in raw else 2)


def _save_json(p: Path, data, indent):
    p.write_text(json.dumps(data, indent=indent, ensure_ascii=False) + "\n", encoding="utf-8")


def _json_vmargs(game_dir: Path):
    try:
        data, _ = _load_json(game_dir / "ProjectZomboid64.json")
        return [str(a) for a in data.get("vmArgs", [])]
    except Exception:
        return []


def _json_heap_mb(game_dir) -> int:
    """ProjectZomboid64.json 의 -Xmx 값(MB). 못 읽으면 0."""
    for a in _json_vmargs(game_dir):
        m = re.match(r"-Xmx(\d+)m$", a, re.I)
        if m:
            return int(m.group(1))
    return 0


def _is_heap_arg(a) -> bool:
    return str(a).startswith("-Xms") or str(a).startswith("-Xmx")


def _replace_heap_args(args, new):
    """기존 -Xms/-Xmx 자리에 new 를 끼워 넣는다 (없으면 맨 앞). 다른 인자·순서는 그대로 →
       적용 후 복원하면 원본 json 과 순서까지 같아진다."""
    idx = next((i for i, a in enumerate(args) if _is_heap_arg(a)), 0)
    rest = [a for a in args if not _is_heap_arg(a)]
    idx = min(idx, len(rest))
    return rest[:idx] + list(new) + rest[idx:]


def _edit_json_vmargs(game_dir: Path, fn):
    """vmArgs 를 fn(list)->list 로 바꿔 저장 (나머지 키·들여쓰기 보존)."""
    p = game_dir / "ProjectZomboid64.json"
    data, indent = _load_json(p)
    data["vmArgs"] = fn([a for a in data.get("vmArgs", [])])
    _save_json(p, data, indent)


def _bat_java_lines(raw: str):
    return [ln for ln in raw.splitlines() if _BAT_JAVA_RE.search(ln)]


def _bat_add_args(raw: str, args) -> str:
    out = []
    for ln in raw.splitlines(keepends=True):
        if _BAT_JAVA_RE.search(ln):
            missing = [a for a in args if a not in ln.split()]
            if missing:
                ln = _BAT_JAVA_RE.sub(lambda m: m.group(1) + " " + " ".join(missing), ln, count=1)
        out.append(ln)
    return "".join(out)


def _bat_remove_args(raw: str, args) -> str:
    for a in args:
        raw = re.sub(r"[ \t]+" + re.escape(a) + r"(?=[ \t\r\n])", "", raw)
    return raw


# ── 항목 단위 조회/적용/해제 ──────────────────────────────────────────────────
def opt_group_available(g, game_dir):
    """이 항목을 이 설치 폴더에 적용할 수 있는지. 반환: (가능여부, 불가사유)"""
    if game_dir is None:
        return False, "PZ 설치 폴더 없음"
    if g.kind == "class":
        conf = opt_conf_dir()
        if conf is None:
            return False, "패치 리소스(opt_conf) 없음"
        for rel in g.files:
            fname = rel.rsplit("/", 1)[-1]
            if not (conf / fname).exists():
                return False, f"패치 파일 누락: {fname}"
            # 패치본이 이미 덮여 있어도 파일 자체는 존재해야 한다. 없으면 구조가 다른 빌드(B42 등).
            if not (game_dir / rel).exists():
                return False, "이 설치본엔 해당 없음"
        return True, ""
    if not (game_dir / "ProjectZomboid64.json").exists():
        return False, "ProjectZomboid64.json 없음"
    if g.kind == "heap":
        if half_ram_mb() <= 0:
            return False, "RAM 크기를 알 수 없음"
        return True, ""
    # graal
    info = graal_zip_info()
    if info is None:
        return False, "GraalVM zip 이 빌드에 포함되지 않음"
    want = java_major(info[1])
    have = java_major(vanilla_release(game_dir))
    if have != want:
        return False, f"Java {want} 빌드(B42) 전용 — 이 설치본은 Java {have or '?'}"
    return True, ""


def _graal_vmargs_ok(game_dir) -> bool:
    args = _json_vmargs(game_dir)
    if not all(a in args for a in GRAAL_VMARGS):
        return False
    bpath = game_dir / "ProjectZomboid64.bat"
    if bpath.exists():
        try:
            raw = bpath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        for ln in _bat_java_lines(raw):
            if not all(a in ln.split() for a in GRAAL_VMARGS):
                return False
    return True


def opt_group_applied(g, game_dir) -> bool:
    """이 항목이 현재 '완전히 패치된 상태'인지. 부분 적용은 False (적용 누르면 다시 맞춰진다)."""
    if game_dir is None:
        return False
    if g.kind == "class":
        conf = opt_conf_dir()
        if conf is None:
            return False
        for rel in g.files:
            fname = rel.rsplit("/", 1)[-1]
            src, dst = conf / fname, game_dir / rel
            try:
                if not dst.exists() or dst.read_bytes() != src.read_bytes():
                    return False
            except OSError:
                return False
        return True
    if g.kind == "heap":
        mb = half_ram_mb()
        if mb <= 0 or _json_heap_mb(game_dir) != mb:
            return False
        bpath = game_dir / "ProjectZomboid64.bat"
        if bpath.exists():
            try:
                raw = bpath.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return False
            # bat 안의 -Xmx 는 여러 줄에 나온다. 하나라도 다른 값이면 미적용으로 본다
            found = _XMX_RE.findall(raw)
            if not found or any(x.lower() != f"-xmx{mb}m" for x in found):
                return False
        return True
    # graal
    info = graal_zip_info()
    if info is None or jre_state(game_dir) != "graal":
        return False
    cur = read_release(game_dir / "jre64").get("GRAALVM_VERSION")
    return cur == info[1].get("GRAALVM_VERSION") and _graal_vmargs_ok(game_dir)


def opt_group_touched(g, game_dir) -> bool:
    """원본에서 조금이라도 바뀌어 있는가 (해제 시 복원 대상 판정용).
       graal 은 JRE 만 바뀌고 인자는 없는 식의 반쪽 상태가 있을 수 있어 따로 본다."""
    if g.kind != "graal":
        return opt_group_applied(g, game_dir)
    if jre_state(game_dir) in ("graal", "mixed"):
        return True
    args = _json_vmargs(game_dir)
    return any(a in args for a in GRAAL_VMARGS)


def _apply_heap(game_dir: Path):
    mb = half_ram_mb()
    for rel in OPT_GROUP_BY_KEY["heap"].files:
        _backup_once(game_dir, Path(rel))     # 원본 힙 값을 기억하는 기준본
    _edit_json_vmargs(game_dir, lambda a: _replace_heap_args(a, [f"-Xms{mb}m", f"-Xmx{mb}m"]))
    # bat (직접 실행용): 바닐라 bat 는 java 실행 라인이 2개(-Xmx 고정)라 전부 치환해야 한다.
    bpath = game_dir / "ProjectZomboid64.bat"
    if bpath.exists():
        raw = bpath.read_text(encoding="utf-8", errors="replace")
        raw = _XMX_RE.sub(f"-Xmx{mb}m", raw)
        raw = _XMS_RE.sub(f"-Xms{mb}m", raw)
        bpath.write_text(raw, encoding="utf-8")


def _restore_heap(game_dir: Path):
    """백업 원본에서 힙 인자만 읽어와 되돌린다 (graal 인자 등 다른 인자는 보존)."""
    bdir = game_dir / OPT_BACKUP_DIRNAME
    bjson = bdir / "ProjectZomboid64.json"
    if not bjson.exists():
        raise RuntimeError("힙 설정 백업이 없음 — Steam '게임 파일 무결성 검사'로 복원해 주세요")
    bdata, _ = _load_json(bjson)
    orig = [a for a in bdata.get("vmArgs", []) if _is_heap_arg(a)]
    _edit_json_vmargs(game_dir, lambda a: _replace_heap_args(a, orig))
    bpath, bbat = game_dir / "ProjectZomboid64.bat", bdir / "ProjectZomboid64.bat"
    if bpath.exists() and bbat.exists():
        raw = bpath.read_text(encoding="utf-8", errors="replace")
        braw = bbat.read_text(encoding="utf-8", errors="replace")
        bx, bs = _XMX_RE.search(braw), _XMS_RE.search(braw)
        if bx:
            raw = _XMX_RE.sub(bx.group(0), raw)
        raw = _XMS_RE.sub(bs.group(0), raw) if bs else re.sub(r"[ \t]+-Xms\d+[mMgG]", "", raw)
        bpath.write_text(raw, encoding="utf-8")


def _extract_graal(game_dir: Path, progress):
    """번들 zip → game_dir/jre64.__pzopt_tmp (최상위 폴더 한 단계 벗겨서). 반환: 임시 폴더."""
    top, _rel, size = graal_zip_info()
    tmp = game_dir / JRE_TMP_DIRNAME
    if tmp.exists():
        _rmtree(tmp)
    need = size + DISK_MARGIN_MB * 1024 * 1024
    free = shutil.disk_usage(game_dir).free
    if free < need:
        raise RuntimeError(f"디스크 공간 부족 — {need // 1048576:,} MB 필요, {free // 1048576:,} MB 남음")
    tmp.mkdir()
    root = tmp.resolve()
    prefix = top + "/"
    done = 0
    try:
        with zipfile.ZipFile(graal_zip_path()) as z:
            for i in z.infolist():
                if i.is_dir() or not i.filename.startswith(prefix):
                    continue
                rel = i.filename[len(prefix):]
                dst = (tmp / rel).resolve()
                if root not in dst.parents:                # zip 경로 탈출 방지
                    raise RuntimeError(f"잘못된 zip 경로: {i.filename}")
                dst.parent.mkdir(parents=True, exist_ok=True)
                with z.open(i) as src, open(dst, "wb") as out:
                    shutil.copyfileobj(src, out, 1 << 20)
                done += i.file_size
                progress(f"GraalVM 압축 해제 중… {done // 1048576:,} / {size // 1048576:,} MB",
                         int(done * 100 / max(size, 1)))
    except BaseException:
        _rmtree(tmp)
        raise
    if not is_graal_release(read_release(tmp)):
        _rmtree(tmp)
        raise RuntimeError("압축 해제 결과 검증 실패 (release 파일 없음)")
    return tmp


def _apply_graal(game_dir: Path, progress):
    info = graal_zip_info()
    jre = game_dir / "jre64"
    bdir = game_dir / OPT_BACKUP_DIRNAME
    bjre = bdir / "jre64"
    st = jre_state(game_dir)
    cur_ver = read_release(jre).get("GRAALVM_VERSION")
    if not (st == "graal" and cur_ver == info[1].get("GRAALVM_VERSION")):
        if st != "vanilla" and not bjre.exists():
            raise RuntimeError("원본 jre64 백업이 없음 — Steam '게임 파일 무결성 검사'로 "
                               "원본을 받은 뒤 다시 시도해 주세요")
        # 먼저 다 풀어놓고 나서 바꿔치기 → 중간에 실패해도 jre64 는 그대로 남는다
        tmp = _extract_graal(game_dir, progress)
        progress("JRE 교체 중…", 100)
        if st == "vanilla":
            # 지금 원본이 기존 백업보다 최신이다 (Steam 이 PZ 업데이트로 갈아끼웠을 수 있음)
            if bjre.exists():
                _rmtree(bjre)
            bdir.mkdir(parents=True, exist_ok=True)
            os.replace(jre, bjre)                          # 같은 드라이브 → 이름만 이동
        elif jre.exists():
            _rmtree(jre)                                   # 섞인/다른 버전 Graal → 버림
        os.replace(tmp, jre)
    _edit_json_vmargs(game_dir, lambda a: a + [x for x in GRAAL_VMARGS if x not in a])
    bpath = game_dir / "ProjectZomboid64.bat"
    if bpath.exists():
        raw = bpath.read_text(encoding="utf-8", errors="replace")
        bpath.write_text(_bat_add_args(raw, GRAAL_VMARGS), encoding="utf-8")


def _restore_graal(game_dir: Path, progress):
    _edit_json_vmargs(game_dir, lambda a: [x for x in a if x not in GRAAL_VMARGS])
    bpath = game_dir / "ProjectZomboid64.bat"
    if bpath.exists():
        raw = bpath.read_text(encoding="utf-8", errors="replace")
        bpath.write_text(_bat_remove_args(raw, GRAAL_VMARGS), encoding="utf-8")
    if jre_state(game_dir) == "vanilla":
        return
    jre, bjre = game_dir / "jre64", game_dir / OPT_BACKUP_DIRNAME / "jre64"
    if not bjre.exists():
        raise RuntimeError("원본 jre64 백업이 없음 — Steam '게임 파일 무결성 검사'로 복원해 주세요")
    progress("원본 JRE 복원 중…", 100)
    if jre.exists():
        _rmtree(jre)
    os.replace(bjre, jre)


def apply_opt_group(g, game_dir, progress):
    """항목 적용. 권한 문제는 PermissionError 그대로 던짐."""
    if g.kind == "class":
        conf = opt_conf_dir()
        for rel in g.files:
            _backup_once(game_dir, Path(rel))
        for rel in g.files:
            fname = rel.rsplit("/", 1)[-1]
            src, dst = conf / fname, game_dir / rel
            shutil.copy2(src, dst)
            if dst.read_bytes() != src.read_bytes():
                raise RuntimeError(f"복사 검증 실패: {fname}")
    elif g.kind == "heap":
        _apply_heap(game_dir)
    else:
        _apply_graal(game_dir, progress)


def restore_opt_group(g, game_dir, progress):
    """원본으로 되돌린다. 백업이 없으면 RuntimeError."""
    if g.kind == "heap":
        _restore_heap(game_dir)
        return
    if g.kind == "graal":
        _restore_graal(game_dir, progress)
        return
    bdir = game_dir / OPT_BACKUP_DIRNAME
    n = 0
    for rel in g.files:
        src = bdir / rel
        if src.exists():
            dst = game_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            n += 1
    if n == 0:
        raise RuntimeError(f"'{g.label}' 백업이 없음 — Steam '게임 파일 무결성 검사'로 복원해 주세요")


def apply_opt_selection(game_dir, keys, progress=lambda t, p: None):
    """체크된 항목은 적용, 체크 해제된 항목은 원본 복원. 반환: (적용수, 복원수).
       적용 불가 항목은 건드리지 않는다. 끝나면 상태 기록을 갱신한다.
       순서: heap 이 graal 보다 먼저 (둘 다 json 을 고치지만 서로의 인자는 보존한다)."""
    keys = set(keys or [])
    applied = restored = 0
    try:
        for g in OPT_GROUPS:
            ok, _ = opt_group_available(g, game_dir)
            if not ok:
                continue
            if g.key in keys:
                if not opt_group_applied(g, game_dir):
                    progress(f"적용 중: {g.label}", 0)
                    apply_opt_group(g, game_dir, progress)
                    applied += 1
            elif opt_group_touched(g, game_dir):
                progress(f"복원 중: {g.label}", 0)
                restore_opt_group(g, game_dir, progress)
                restored += 1
    finally:
        write_state(game_dir)
    return applied, restored


# ── 상태 기록 (퐁듀 런처가 읽음) ──────────────────────────────────────────────
# 런처는 그룹 key 를 몰라도 된다 — 레코드에 든 필드(files / xmx_mb / graalvm_version)만 보고
# 현재 디스크와 대조한다. 그래서 여기서 항목을 추가해도 런처 수정이 필요 없다.
def write_state(game_dir: Path):
    groups = {}
    for g in OPT_GROUPS:
        ok, _ = opt_group_available(g, game_dir)
        if not ok or not opt_group_applied(g, game_dir):
            continue
        if g.kind == "class":
            groups[g.key] = {"files": {rel: _sha256(game_dir / rel) for rel in g.files}}
        elif g.kind == "heap":
            groups[g.key] = {"xmx_mb": _json_heap_mb(game_dir)}
        else:
            groups[g.key] = {
                "graalvm_version": read_release(game_dir / "jre64").get("GRAALVM_VERSION", ""),
                "vmargs": list(GRAAL_VMARGS),
            }
    bdir = game_dir / OPT_BACKUP_DIRNAME
    if not groups and not bdir.exists():
        return
    try:
        bdir.mkdir(parents=True, exist_ok=True)
        data = {"tool": APP_TITLE, "version": VERSION, "time": int(time.time()), "groups": groups}
        (bdir / STATE_FILE).write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    except OSError:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
#  ZombieBuddy 적용/해제 (ZombieBuddyInstaller.exe 의 Install/Uninstall 과 같은 작업)
#
#  ZombieBuddyInstaller.exe (Go, zed-0xff) 가 하는 일을 그대로 옮겼다:
#    적용  1) 구독한 ZB 워크샵 폴더 mods/ZombieBuddy/libs/ 의 zbNative.dll, ZombieBuddy.jar → 게임 폴더
#          2) ProjectZomboid64.json vmArgs 맨 앞에 -agentlib:zbNative
#          3) ProjectZomboid64.bat 의 SET _JAVA_OPTIONS= 값 앞에 -agentlib:zbNative (줄이 없으면 맨 위에 추가)
#          4) Steam userdata/*/config/localconfig.vdf 의 PZ(108600) LaunchOptions 에 -agentlib:zbNative --
#    해제  위 1~4 를 되돌림 (+ ZB 가 자체 업데이트로 남긴 ZombieBuddy.jar.new 삭제)
#
#  설치기와 다르게 한 부분 (호환 범위 안에서 더 안전하게):
#    · Steam 실행 옵션은 '덮어쓰기'가 아니라 '앞에 끼워넣기'. 설치기는 기존 옵션을 통째로
#      "-agentlib:zbNative --" 로 바꿔 사용자가 넣어둔 -Xmx, -debug 등이 사라진다.
#      기존 옵션에 "--" 가 없으면 전부 게임 인자이므로 "-agentlib:zbNative -- <기존>" 으로 넣는다.
#    · Steam 실행 옵션은 '선택한 폴더가 Steam 정식 설치본일 때만' 건드린다. 복사본 폴더(예: B41 보관용)
#      에 적용하면서 Steam 옵션을 넣으면, dll 이 없는 정식 설치본이 "agent library 없음"으로 안 켜진다.
#    · localconfig.vdf 는 수정 전에 %APPDATA%\PZ-Optimizer\backup\ 에 사본을 남긴다.
#
#  최신본 반영:
#    · 복사 원본은 항상 '지금 구독 중인 워크샵 폴더' → Steam 이 ZB 모드를 업데이트하면 그 파일이 쓰인다.
#      게임 폴더 사본이 워크샵과 다르면 '업데이트 필요'로 표시하고 버튼이 [업데이트]가 된다.
#    · 설치기 소스(installer/installer.go)가 모드에 같이 들어 있어서, 실행 인자·복사 파일 목록·설치기 버전은
#      그 소스에서 매번 읽는다 (설치기가 바뀌면 같이 따라감). 못 읽으면 아래 기본값.
#    · 설치기의 주(major) 버전이 아래 ZB_KNOWN_INSTALLER_MAJOR 와 다르면 동작 방식이 바뀌었을 수 있다는
#      경고를 띄운다 → PZ Optimizer 업데이트로 맞춘다.
# ═══════════════════════════════════════════════════════════════════════════════
ZB_WORKSHOP_ID = "3619862853"
ZB_DEFAULT_LAUNCH_ARG = "-agentlib:zbNative"
ZB_DEFAULT_CORE_FILES = ["zbNative.dll", "ZombieBuddy.jar"]
ZB_EXTRA_REMOVE_FILES = ["ZombieBuddy.jar.new"]       # ZB 자체 업데이트 임시 파일 (해제 시 함께 삭제)
ZB_KNOWN_INSTALLER_MAJOR = 4                           # 이 동작을 옮겨온 설치기 버전 (v4.1)


def zb_mod_dir():
    """구독한 ZombieBuddy 워크샵 폴더(.../3619862853/mods/ZombieBuddy). 없으면 None."""
    for root in _steam_roots():
        for lib in _libraries_from(root):
            d = lib / "steamapps" / "workshop" / "content" / PZ_APPID / ZB_WORKSHOP_ID / "mods" / "ZombieBuddy"
            if (d / "libs").is_dir():
                return d
    return None


_ZB_SPEC_CACHE = {}


def zb_spec(mod_dir):
    """설치기 소스에서 읽은 {launch_arg, core_files, installer_version, mod_version}."""
    key = str(mod_dir)
    if key in _ZB_SPEC_CACHE:
        return _ZB_SPEC_CACHE[key]
    spec = {"launch_arg": ZB_DEFAULT_LAUNCH_ARG, "core_files": list(ZB_DEFAULT_CORE_FILES),
            "installer_version": "", "mod_version": ""}
    if mod_dir is not None:
        try:
            src = (Path(mod_dir) / "installer" / "installer.go").read_text(encoding="utf-8", errors="replace")
            m = re.search(r'ZB_LAUNCH_ARG\s*=\s*"([^"]+)"', src)
            if m:
                spec["launch_arg"] = m.group(1)
            m = re.search(r'INSTALLER_VERSION\s*=\s*"([^"]+)"', src)
            if m:
                spec["installer_version"] = m.group(1)
            m = re.search(r'func copyCoreFiles\(.*?files\s*:=\s*\[\]string\{([^}]*)\}', src, re.S)
            if m:
                files = re.findall(r'"([^"]+)"', m.group(1))
                if files:
                    spec["core_files"] = files
        except OSError:
            pass
        try:
            spec["mod_version"] = (Path(mod_dir) / "java" / "VERSION").read_text(encoding="utf-8").strip()
        except OSError:
            pass
    _ZB_SPEC_CACHE[key] = spec
    return spec


def zb_installer_mismatch(spec) -> bool:
    v = parse_version(spec.get("installer_version"))
    return bool(v) and v[0] != ZB_KNOWN_INSTALLER_MAJOR


def _is_zb_opt(tok, arg) -> bool:
    return tok == arg or tok.startswith(arg + "=")


def steam_running() -> bool:
    if os.name != "nt":
        return False
    import subprocess
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq steam.exe", "/NH"],
                             capture_output=True, text=True, creationflags=0x08000000).stdout.lower()
        return "steam.exe" in out
    except Exception:
        return False


def steam_main_path():
    """Steam 본체 경로 (userdata 가 있는 곳). HKCU SteamPath 우선."""
    for r in _steam_roots():
        if (r / "userdata").is_dir():
            return r
    return None


def is_steam_install(game_dir) -> bool:
    """선택한 폴더가 Steam 이 실행하는 정식 설치본인가 (Steam 실행 옵션을 건드려도 되는가)."""
    official = find_pz_dir()
    try:
        return official is not None and Path(game_dir).resolve() == Path(official).resolve()
    except OSError:
        return False


# ── Steam localconfig.vdf (텍스트 VDF) — 위치 정보를 가진 최소 파서 ──────────────
_VDF_TOKEN_RE = re.compile(r'"((?:[^"\\]|\\.)*)"|([{}])|(//[^\n]*)')


def _vdf_unescape(s):
    return s.replace('\\"', '"').replace("\\\\", "\\")


def _vdf_escape(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _vdf_parse(text):
    """{key_lower: node} 트리. node = {"key", "span"(값 따옴표 포함 범위) | "open","close", "children"}"""
    toks = [(m.start(), m.end(), m.group(1), m.group(2)) for m in _VDF_TOKEN_RE.finditer(text) if not m.group(3)]
    pos = 0

    def block(close_expected):
        nonlocal pos
        children = []
        while pos < len(toks):
            st, en, q, br = toks[pos]
            if br == "}":
                pos += 1
                return children, st
            if q is None:                     # 예상 못 한 '{' — 건너뜀
                pos += 1
                continue
            key = _vdf_unescape(q)
            pos += 1
            if pos >= len(toks):
                break
            st2, en2, q2, br2 = toks[pos]
            if br2 == "{":
                pos += 1
                kids, close = block(True)
                children.append({"key": key, "open": st2, "close": close, "children": kids})
            elif q2 is not None:
                pos += 1
                children.append({"key": key, "value": _vdf_unescape(q2), "span": (st2, en2)})
        return children, len(text)

    root, _ = block(False)
    return root


def _vdf_find(children, *path):
    cur = children
    node = None
    for k in path:
        node = next((c for c in cur if c["key"].lower() == k.lower()), None)
        if node is None or "children" not in node:
            return None
        cur = node["children"]
    return node


_VDF_APPS_PATH = ("UserLocalConfigStore", "Software", "Valve", "Steam", "apps")


def vdf_get_launch_options(text):
    """(LaunchOptions 문자열 | None, apps 블록 존재 여부)"""
    tree = _vdf_parse(text)
    app = _vdf_find(tree, *_VDF_APPS_PATH, PZ_APPID)
    if app is None:
        return None, _vdf_find(tree, *_VDF_APPS_PATH) is not None
    lo = next((c for c in app["children"] if c["key"].lower() == "launchoptions" and "value" in c), None)
    return (lo["value"] if lo else ""), True


def vdf_set_launch_options(text, new_opts):
    """PZ 앱 블록의 LaunchOptions 를 new_opts 로 (없으면 추가, 앱 블록이 없으면 생성)."""
    tree = _vdf_parse(text)
    apps = _vdf_find(tree, *_VDF_APPS_PATH)
    if apps is None:
        raise RuntimeError("localconfig.vdf 에 apps 블록이 없음")
    val = '"' + _vdf_escape(new_opts) + '"'
    app = next((c for c in apps["children"] if c["key"].lower() == PZ_APPID and "children" in c), None)
    if app is not None:
        lo = next((c for c in app["children"] if c["key"].lower() == "launchoptions" and "value" in c), None)
        if lo is not None:
            a, b = lo["span"]
            return text[:a] + val + text[b:]
        # 닫는 괄호 앞에 새 줄로 추가 (닫는 괄호 줄의 들여쓰기 + 탭)
        close = app["close"]
        ls = text.rfind("\n", 0, close) + 1
        indent = text[ls:close]
        return text[:ls] + indent + '\t"LaunchOptions"\t\t' + val + "\n" + text[ls:]
    close = apps["close"]
    ls = text.rfind("\n", 0, close) + 1
    ind = text[ls:close] + "\t"
    block = (f'{ind}"{PZ_APPID}"\n{ind}{{\n{ind}\t"LaunchOptions"\t\t{val}\n{ind}}}\n')
    return text[:ls] + block + text[ls:]


def launch_opts_add(opts, arg):
    """'-agentlib:zbNative' 를 JVM 인자 구역(-- 앞)에 넣는다. 이미 있으면 그대로."""
    toks = opts.split()
    if any(_is_zb_opt(t, arg) for t in toks):
        return opts, False
    if "--" in toks:
        return " ".join([arg] + toks), True
    return " ".join([arg, "--"] + toks), True      # -- 가 없으면 기존 것은 전부 게임 인자


def launch_opts_remove(opts, arg):
    toks = opts.split()
    kept = [t for t in toks if not _is_zb_opt(t, arg)]
    if len(kept) == len(toks):
        return opts, False
    if kept and kept[0] == "--":                     # 우리가 붙인 '--' 만 남았으면 같이 제거
        kept = kept[1:]
    return " ".join(kept), True


def _steam_localconfigs():
    sp = steam_main_path()
    if sp is None:
        return []
    out = []
    for d in sorted((sp / "userdata").iterdir()):
        p = d / "config" / "localconfig.vdf"
        if d.is_dir() and p.is_file():
            out.append(p)
    return out


def _backup_localconfig(p: Path):
    base = os.environ.get("APPDATA") or str(Path.home())
    bdir = Path(base) / "PZ-Optimizer" / "backup"
    bdir.mkdir(parents=True, exist_ok=True)
    uid = p.parent.parent.name
    shutil.copy2(p, bdir / f"localconfig_{uid}_{time.strftime('%Y%m%d_%H%M%S')}.vdf")


def _steam_opts_state(arg):
    """[(경로, 현재 옵션 | None)] — PZ 항목이 없으면 None."""
    out = []
    for p in _steam_localconfigs():
        try:
            opts, _ = vdf_get_launch_options(p.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            opts = None
        out.append((p, opts))
    return out


# ── bat 의 SET _JAVA_OPTIONS= 처리 (설치기와 동일 규칙) ───────────────────────────
_JAVA_OPTS_RE = re.compile(r"^([ \t]*)SET _JAVA_OPTIONS=(.*)$", re.I)


def _bat_zb_has(raw, arg):
    for ln in raw.splitlines():
        m = _JAVA_OPTS_RE.match(ln)
        if m:
            return any(_is_zb_opt(t, arg) for t in m.group(2).split())
    return False


def _bat_zb_edit(raw, arg, install):
    nl = "\r\n" if "\r\n" in raw else "\n"
    lines = raw.replace("\r\n", "\n").split("\n")
    for i, ln in enumerate(lines):
        m = _JAVA_OPTS_RE.match(ln)
        if not m:
            continue
        toks = m.group(2).split()
        if install:
            if any(_is_zb_opt(t, arg) for t in toks):
                return raw, False
            toks = [arg] + toks
        else:
            kept = [t for t in toks if not _is_zb_opt(t, arg)]
            if len(kept) == len(toks):
                return raw, False
            toks = kept
        lines[i] = m.group(1) + "SET _JAVA_OPTIONS=" + " ".join(toks)
        return nl.join(lines), True
    if install:
        return nl.join(["SET _JAVA_OPTIONS=" + arg] + lines), True
    return raw, False


# ── 상태 / 적용 / 해제 ────────────────────────────────────────────────────────
def zb_status(game_dir):
    """dict: state('none'|'partial'|'applied'|'outdated'|'nomod'), parts, notes, spec, steam_ok"""
    mod = zb_mod_dir()
    spec = zb_spec(mod)
    arg = spec["launch_arg"]
    gd = Path(game_dir)
    parts = {}
    parts["files"] = all((gd / f).is_file() for f in spec["core_files"])
    parts["json"] = any(_is_zb_opt(a, arg) for a in _json_vmargs(gd))
    bat = gd / "ProjectZomboid64.bat"
    if bat.exists():
        try:
            parts["bat"] = _bat_zb_has(bat.read_text(encoding="utf-8", errors="replace"), arg)
        except OSError:
            parts["bat"] = False
    steam_ok = is_steam_install(gd) and bool(_steam_localconfigs())
    if steam_ok:
        st = _steam_opts_state(arg)
        parts["steam"] = any(o is not None and any(_is_zb_opt(t, arg) for t in o.split()) for _, o in st)
    any_on = any(parts.values())
    all_on = all(parts.values())
    state = "applied" if all_on else ("partial" if any_on else "none")
    outdated = False
    if parts["files"] and mod is not None:
        for f in spec["core_files"]:
            src = mod / "libs" / f
            if not src.is_file():
                continue
            cur = gd / f
            new = gd / (f + ".new")
            h = _sha256(src)
            if _sha256(cur) != h and not (new.is_file() and _sha256(new) == h):
                outdated = True
    if state == "applied" and outdated:
        state = "outdated"
    if mod is None and state == "none":
        state = "nomod"
    return {"state": state, "parts": parts, "spec": spec, "mod": mod, "steam_ok": steam_ok}


def zb_apply(game_dir):
    """설치기 Install 과 같은 작업. 반환: 바뀐 항목 설명 리스트."""
    gd = Path(game_dir)
    mod = zb_mod_dir()
    if mod is None:
        raise RuntimeError("ZombieBuddy 모드를 찾을 수 없음 — 스팀 창작마당에서 ZombieBuddy 를 구독해 주세요\n"
                           "https://steamcommunity.com/sharedfiles/filedetails/?id=" + ZB_WORKSHOP_ID)
    spec = zb_spec(mod)
    arg = spec["launch_arg"]
    use_steam = is_steam_install(gd) and bool(_steam_localconfigs())
    if use_steam and steam_running():
        raise RuntimeError("Steam 이 실행 중입니다 — 실행 옵션은 Steam 이 꺼져 있을 때만 바꿀 수 있습니다.\n"
                           "트레이의 Steam 아이콘 우클릭 → [나가기] 로 완전히 종료한 뒤 다시 눌러 주세요.")
    done = []
    for f in spec["core_files"]:
        src = mod / "libs" / f
        if not src.is_file():
            raise RuntimeError(f"ZombieBuddy 모드 폴더에 {f} 가 없음: {src}")
        shutil.copy2(src, gd / f)
    done.append("zbNative.dll · ZombieBuddy.jar 복사 (모드 " + (spec["mod_version"] or "?") + ")")
    changed = []
    _edit_json_vmargs(gd, lambda a: a if any(_is_zb_opt(x, arg) for x in a)
                      else (changed.append(1) or [arg] + a))
    if changed:
        done.append(f"ProjectZomboid64.json 에 {arg} 추가")
    bat = gd / "ProjectZomboid64.bat"
    if bat.exists():
        raw = bat.read_text(encoding="utf-8", errors="replace")
        new, ch = _bat_zb_edit(raw, arg, True)
        if ch:
            bat.write_text(new, encoding="utf-8")
            done.append(f"ProjectZomboid64.bat _JAVA_OPTIONS 에 {arg} 추가")
    if use_steam:
        for p in _steam_localconfigs():
            text = p.read_text(encoding="utf-8", errors="replace")
            cur, _ = vdf_get_launch_options(text)
            new_opts, ch = launch_opts_add(cur or "", arg)
            if ch:
                _backup_localconfig(p)
                p.write_text(vdf_set_launch_options(text, new_opts), encoding="utf-8")
                done.append(f"Steam 실행 옵션 ({p.parent.parent.name}): {new_opts}")
    return done


def zb_remove(game_dir):
    """설치기 Uninstall 과 같은 작업."""
    gd = Path(game_dir)
    spec = zb_spec(zb_mod_dir())
    arg = spec["launch_arg"]
    use_steam = is_steam_install(gd) and bool(_steam_localconfigs())
    if use_steam and steam_running():
        st = _steam_opts_state(arg)
        if any(o and any(_is_zb_opt(t, arg) for t in o.split()) for _, o in st):
            raise RuntimeError("Steam 이 실행 중입니다 — 실행 옵션은 Steam 이 꺼져 있을 때만 바꿀 수 있습니다.\n"
                               "트레이의 Steam 아이콘 우클릭 → [나가기] 로 완전히 종료한 뒤 다시 눌러 주세요.")
    done = []
    changed = []
    _edit_json_vmargs(gd, lambda a: (changed.append(1) or [x for x in a if not _is_zb_opt(x, arg)])
                      if any(_is_zb_opt(x, arg) for x in a) else a)
    if changed:
        done.append(f"ProjectZomboid64.json 에서 {arg} 제거")
    bat = gd / "ProjectZomboid64.bat"
    if bat.exists():
        raw = bat.read_text(encoding="utf-8", errors="replace")
        new, ch = _bat_zb_edit(raw, arg, False)
        if ch:
            bat.write_text(new, encoding="utf-8")
            done.append(f"ProjectZomboid64.bat _JAVA_OPTIONS 에서 {arg} 제거")
    if use_steam:
        for p in _steam_localconfigs():
            text = p.read_text(encoding="utf-8", errors="replace")
            cur, _ = vdf_get_launch_options(text)
            if not cur:
                continue
            new_opts, ch = launch_opts_remove(cur, arg)
            if ch:
                _backup_localconfig(p)
                p.write_text(vdf_set_launch_options(text, new_opts), encoding="utf-8")
                done.append(f"Steam 실행 옵션 ({p.parent.parent.name}): '{new_opts}'")
    for f in spec["core_files"] + ZB_EXTRA_REMOVE_FILES:
        fp = gd / f
        if fp.exists():
            fp.unlink()
            done.append(f"{f} 삭제")
    return done


# ═══════════════════════════════════════════════════════════════════════════════
#  UI
# ═══════════════════════════════════════════════════════════════════════════════
DARK_QSS = """
QWidget { background:#23252b; color:#e8e8ea; font-family:'Malgun Gothic','맑은 고딕',sans-serif; font-size:13px; }
QLineEdit { background:#1b1d22; border:1px solid rgba(255,255,255,0.12); border-radius:8px; padding:7px 10px; color:#e8e8ea; }
QPushButton { background:#2b2e36; border:1px solid rgba(255,255,255,0.15); border-radius:8px; padding:7px 14px; color:#e8e8ea; }
QPushButton:hover { background:#343843; }
QPushButton:disabled { color:#5f5e5a; }
QPushButton#start { background:#1d9e75; color:#04342c; border:none; font-weight:bold; padding:10px 20px; }
QPushButton#start:hover { background:#22b384; }
QPushButton#start:disabled { background:#2b2e36; color:#5f5e5a; border:1px solid rgba(255,255,255,0.12); }
QPushButton#link  { background:transparent; border:none; color:#85b7eb; padding:2px; }
QCheckBox { color:#cfd0d4; font-size:13px; }
QCheckBox:disabled { color:#5f5e5a; }
QLabel#muted { color:#9a9ca3; font-size:12px; }
QLabel#hint  { color:#6f7178; font-size:11px; }
QLabel#warn  { color:#ef9f27; font-size:11px; }
QLabel#info  { color:#cfd0d4; font-size:13px; }
QLabel#zbstate { color:#cfd0d4; font-size:13px; }
QFrame#box   { background:#1b1d22; border:1px solid rgba(255,255,255,0.08); border-radius:8px; }
QFrame#box QLabel { background:transparent; }
QPushButton#zb { background:#2b2e36; border:1px solid #1d9e75; color:#5dcaa5; font-weight:bold; padding:7px 14px; }
QPushButton#zb:hover { background:#343843; }
QPushButton#zb:disabled { border:1px solid rgba(255,255,255,0.12); color:#5f5e5a; }
QLabel#sect  { font-size:15px; font-weight:bold; color:#e8e8ea; }
QLabel#linkok { color:#5dcaa5; font-weight:bold; }
QLabel#err   { font-size:13px; font-weight:bold; color:#e24b4a; }
QTextEdit { background:#15171b; border:1px solid rgba(255,255,255,0.08); border-radius:8px; color:#b8bac0; font-size:12px; }
QProgressBar { background:#1b1d22; border:1px solid rgba(255,255,255,0.10); border-radius:5px; min-height:10px; max-height:10px; }
QProgressBar::chunk { background:#1d9e75; border-radius:4px; }
"""


class ApplyWorker(QThread):
    """파일 작업은 백그라운드에서 (GraalVM 압축 해제가 수십 초 걸릴 수 있음)."""
    progress = pyqtSignal(str, int)
    done = pyqtSignal(int, int)
    failed = pyqtSignal(str)

    def __init__(self, game_dir, keys):
        super().__init__()
        self.game_dir, self.keys = game_dir, keys

    def run(self):
        try:
            a, r = apply_opt_selection(self.game_dir, self.keys,
                                       lambda t, p: self.progress.emit(t, p))
            self.done.emit(a, r)
        except PermissionError as e:
            self.failed.emit("쓰기 권한이 없습니다 — 관리자 권한으로 실행해 주세요.\n\n" + str(e))
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {e}")


class OptimizerWindow(QWidget):
    """자동 적용 없음. 창을 열면 디스크의 현재 상태를 읽어 체크박스를 맞춰주고
       ([적용됨]=체크 / [미적용]=해제), [적용]을 눌렀을 때만 파일을 건드린다."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_TITLE}  {VERSION}")
        ico = resource_path(ICON_FILE)
        if os.path.exists(ico):
            self.setWindowIcon(QIcon(ico))
        self.setFixedSize(680, 720)
        self.game_dir = initial_game_dir()
        self.boxes = {}
        self.rows = {}                 # key -> 항목 묶음 위젯 (빌드에 따라 표시/숨김)
        self.worker = None
        self._pending_update = None    # 적용 작업 중에 새 버전이 확인되면 끝난 뒤 띄운다
        self._build()
        self.setStyleSheet(DARK_QSS)
        self._reload_state()

    def _lbl(self, t, name):
        l = QLabel(t); l.setObjectName(name); l.setWordWrap(True); return l

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18); root.setSpacing(10)

        root.addWidget(self._lbl(PAGE_TITLE, "sect"))
        root.addWidget(self._lbl(
            "체크한 항목만 게임 파일에 적용됩니다. 원본은 게임 폴더의 "
            f"{OPT_BACKUP_DIRNAME}/ 에 백업되며, 체크를 해제하면 원본으로 되돌립니다.\n"
            "PZ 업데이트나 '게임 파일 무결성 검사' 후에는 최적화가 풀리므로 다시 실행해 주세요.",
            "muted"))

        drow = QHBoxLayout()
        drow.addWidget(self._lbl("PZ 설치 폴더", "muted"))
        self.dir_input = QLineEdit(); self.dir_input.setReadOnly(True)
        drow.addWidget(self.dir_input, 1)
        self.pick_btn = QPushButton("폴더 선택"); self.pick_btn.setObjectName("link")
        self.pick_btn.clicked.connect(self._choose_dir)
        drow.addWidget(self.pick_btn)
        root.addLayout(drow)

        self.info = self._lbl("", "info")          # 감지된 빌드 / 설정된 힙 (크게)
        root.addWidget(self.info)
        self.status = self._lbl("", "warn")        # 관리자 권한·PZ 실행 중 등 경고
        root.addWidget(self.status)

        # ZombieBuddy 적용/해제 토글 (빌드 무관)
        self.zb_box = QFrame(); self.zb_box.setObjectName("box")
        zh = QHBoxLayout(self.zb_box); zh.setContentsMargins(12, 10, 12, 10); zh.setSpacing(10)
        zv = QVBoxLayout(); zv.setSpacing(2)
        self.zb_state = self._lbl("", "zbstate")
        self.zb_detail = self._lbl("", "hint")
        zv.addWidget(self.zb_state); zv.addWidget(self.zb_detail)
        zh.addLayout(zv, 1)
        self.zb_btn = QPushButton("ZombieBuddy 적용"); self.zb_btn.setObjectName("zb")
        self.zb_btn.clicked.connect(self._zb_toggle)
        zh.addWidget(self.zb_btn)
        root.addWidget(self.zb_box)

        host = QWidget()
        lv = QVBoxLayout(host); lv.setContentsMargins(2, 2, 2, 2); lv.setSpacing(2)
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame); scroll.setWidget(host)
        root.addWidget(scroll, 1)
        for g in OPT_GROUPS:
            row = QWidget()
            rv = QVBoxLayout(row); rv.setContentsMargins(0, 0, 0, 6); rv.setSpacing(2)
            cb = QCheckBox(g.label)
            self.boxes[g.key] = cb
            rv.addWidget(cb)
            rv.addWidget(self._lbl("     " + "\n     ".join(g.files), "hint"))
            if g.kind == "graal":
                self.graal_note = self._lbl("", "warn")
                rv.addWidget(self.graal_note)
            self.rows[g.key] = row
            lv.addWidget(row)
        self.empty_hint = self._lbl("PZ 설치 폴더를 지정하면 그 빌드에 맞는 항목이 표시됩니다.", "hint")
        lv.addWidget(self.empty_hint)
        lv.addStretch(1)

        self.prog_text = self._lbl("", "muted")
        self.prog = QProgressBar(); self.prog.setTextVisible(False); self.prog.setRange(0, 100)
        self.prog_text.hide(); self.prog.hide()
        root.addWidget(self.prog_text); root.addWidget(self.prog)

        brow = QHBoxLayout()
        self.all_btn = QPushButton("전체 선택"); self.all_btn.setObjectName("link")
        self.all_btn.clicked.connect(lambda: self._set_all(True))
        self.none_btn = QPushButton("전체 해제"); self.none_btn.setObjectName("link")
        self.none_btn.clicked.connect(lambda: self._set_all(False))
        brow.addWidget(self.all_btn); brow.addWidget(self.none_btn); brow.addStretch(1)
        self.apply_btn = QPushButton("적용"); self.apply_btn.setObjectName("start")
        self.apply_btn.clicked.connect(self._apply)
        brow.addWidget(self.apply_btn)
        self.close_btn = QPushButton("닫기"); self.close_btn.clicked.connect(self.close)
        brow.addWidget(self.close_btn)
        root.addLayout(brow)

    # --- 상태 ---
    def _set_all(self, on):
        for cb in self.boxes.values():
            if cb.isEnabled():
                cb.setChecked(on)

    def _reload_state(self):
        """디스크 현재 상태 → 체크박스/활성화/상태문구 반영."""
        gd = self.game_dir
        self.dir_input.setText(str(gd) if gd else "")
        valid = gd is not None and is_pz_dir(gd)
        build = detect_build(gd) if valid else None
        for g in OPT_GROUPS:
            # 설치본 빌드에 해당하는 항목만 보인다. 빌드를 판별 못 하면(새 Java 버전 등) 전부 보여
            # 비활성 사유로 판단하게 한다. 폴더가 없으면 아무것도 안 보인다.
            self.rows[g.key].setVisible(valid and (build is None or build in g.builds))
            cb = self.boxes[g.key]
            ok, why = opt_group_available(g, gd if valid else None)
            cb.setEnabled(ok)
            cb.setChecked(bool(ok and opt_group_applied(g, gd)))
            cb.setText(g.label if ok else f"{g.label}  —  {why}")

        info, msgs = [], []
        if gd is None:
            msgs.append("PZ 설치 폴더를 자동으로 찾지 못했습니다 — 폴더를 직접 지정해 주세요.")
        elif not valid:
            msgs.append("이 폴더에 ProjectZomboid64.json 이 없습니다 — PZ 설치 폴더가 맞는지 확인해 주세요.")
        else:
            info.append(build_label(gd))
            cur = _json_heap_mb(gd)
            mb = half_ram_mb()
            line = f"설정된 힙 크기: {cur:,} MB" if cur else "설정된 힙 크기: 알 수 없음"
            if mb > 0 and cur != mb:
                line += f"   (힙 항목 적용 시 {mb:,} MB — 전체 RAM {total_ram_mb():,} MB 의 절반)"
            info.append(line)
        self.info.setText("\n".join(info))
        self.info.setVisible(bool(info))
        if not is_admin():
            msgs.append("관리자 권한이 아닙니다 — Program Files 아래 설치본은 적용이 실패할 수 있습니다.")
        if pz_running():
            msgs.append("Project Zomboid 실행 중 — 게임을 종료해야 파일을 바꿀 수 있습니다.")
        self.status.setText("\n".join(msgs))
        self.status.setVisible(bool(msgs))
        self._zb_refresh(gd if valid else None)

        notes = []
        info = graal_zip_info()
        if info is not None:
            notes.append(f"포함된 GraalVM {info[1].get('GRAALVM_VERSION', '?')}"
                         f" · 디스크 약 {info[2] // 1048576:,} MB 사용")
        if 0 < total_ram_mb() < PRETOUCH_WARN_RAM_MB:
            notes.append("RAM 32GB 미만 — AlwaysPreTouch 는 시작하자마자 힙 전체를 실제 메모리로 "
                         "점유하므로 방송 프로그램과 함께 쓰면 버벅일 수 있습니다.")
        self.graal_note.setText("     " + "\n     ".join(notes) if notes else "")
        self.empty_hint.setVisible(not valid)
        self.apply_btn.setEnabled(valid)

    # --- ZombieBuddy ---
    def _zb_refresh(self, gd):
        self.zb_box.setVisible(gd is not None)
        if gd is None:
            return
        st = zb_status(gd)
        self._zb_st = st
        spec, parts = st["spec"], st["parts"]
        ver = spec["mod_version"] or "?"
        names = {"files": "파일", "json": "json", "bat": "bat", "steam": "Steam 실행 옵션"}
        state = st["state"]
        if state == "applied":
            self.zb_state.setText(f"ZombieBuddy 적용됨  ·  모드 {ver}")
            self.zb_btn.setText("ZombieBuddy 적용 해제")
        elif state == "outdated":
            self.zb_state.setText(f"ZombieBuddy 적용됨 — 모드가 업데이트됨 ({ver})")
            self.zb_btn.setText("ZombieBuddy 업데이트")
        elif state == "partial":
            on = ", ".join(names[k] for k, v in parts.items() if v)
            self.zb_state.setText(f"ZombieBuddy 일부만 적용됨 ({on})")
            self.zb_btn.setText("ZombieBuddy 적용")
        elif state == "nomod":
            self.zb_state.setText("ZombieBuddy 모드를 구독하지 않음")
            self.zb_btn.setText("ZombieBuddy 적용")
        else:
            self.zb_state.setText(f"ZombieBuddy 미적용  ·  모드 {ver}")
            self.zb_btn.setText("ZombieBuddy 적용")
        detail = []
        if not st["steam_ok"]:
            detail.append("Steam 정식 설치 폴더가 아니라 Steam 실행 옵션은 건드리지 않습니다 (json·bat 만 적용).")
        if zb_installer_mismatch(spec):
            detail.append(f"⚠ ZombieBuddy 설치기가 v{spec['installer_version']} 로 바뀌었습니다 — "
                          "설치 방식이 달라졌을 수 있으니 PZ Optimizer 업데이트를 확인하세요.")
        self.zb_detail.setText("\n".join(detail))
        self.zb_detail.setVisible(bool(detail))
        self.zb_btn.setEnabled(state != "nomod" or st["mod"] is not None)

    def _zb_toggle(self):
        gd = self.game_dir
        if gd is None or self.worker is not None:
            return
        if pz_running():
            QMessageBox.information(self, APP_TITLE, "Project Zomboid가 실행 중입니다. 게임을 먼저 종료해 주세요.")
            return
        st = zb_status(gd)
        removing = st["state"] == "applied"
        try:
            done = zb_remove(gd) if removing else zb_apply(gd)
        except PermissionError as e:
            QMessageBox.warning(self, APP_TITLE, f"쓰기 권한이 없습니다 — 관리자 권한으로 실행해 주세요.\n\n{e}")
            return
        except Exception as e:
            QMessageBox.warning(self, APP_TITLE, f"실패: {e}")
            self._reload_state()
            return
        self._reload_state()
        title = "ZombieBuddy 적용 해제 완료" if removing else "ZombieBuddy 적용 완료"
        QMessageBox.information(self, APP_TITLE, title + "\n\n" + ("\n".join("· " + d for d in done)
                                                                   if done else "변경할 것이 없습니다."))

    def _choose_dir(self):
        start = str(self.game_dir) if self.game_dir else str(Path.home())
        d = QFileDialog.getExistingDirectory(
            self, "Project Zomboid 설치 폴더 선택 (ProjectZomboid64.json 이 있는 폴더)", start)
        if not d:
            return
        p = Path(d)
        if not is_pz_dir(p):
            QMessageBox.warning(self, APP_TITLE,
                                "선택한 폴더에 ProjectZomboid64.json 이 없습니다.\n"
                                "…/steamapps/common/ProjectZomboid 폴더를 선택해 주세요.")
            return
        self.game_dir = p
        cfg = load_config(); cfg["pz_dir"] = str(p); save_config(cfg)
        self._reload_state()

    # --- 적용 ---
    def _set_busy(self, busy):
        for w in (self.apply_btn, self.close_btn, self.pick_btn, self.all_btn, self.none_btn, self.zb_btn):
            w.setEnabled(not busy)
        for cb in self.boxes.values():
            cb.setAttribute(Qt.WA_TransparentForMouseEvents, busy)
        self.prog_text.setVisible(busy); self.prog.setVisible(busy)

    def _apply(self):
        if self.game_dir is None or self.worker is not None:
            return
        if pz_running():
            QMessageBox.information(self, APP_TITLE,
                                    "Project Zomboid가 실행 중이라 파일을 바꿀 수 없습니다.\n"
                                    "게임을 먼저 종료해 주세요.")
            return
        keys = [k for k, cb in self.boxes.items() if cb.isEnabled() and cb.isChecked()]
        cfg = load_config(); cfg["pz_dir"] = str(self.game_dir); save_config(cfg)
        self._set_busy(True)
        self.prog.setValue(0); self.prog_text.setText("준비 중…")
        self.worker = ApplyWorker(self.game_dir, keys)
        self.worker.progress.connect(self._on_progress)
        self.worker.done.connect(self._on_done)
        self.worker.failed.connect(self._on_failed)
        self.worker.start()

    def _on_progress(self, text, pct):
        self.prog_text.setText(text); self.prog.setValue(pct)

    def _finish(self):
        if self.worker is not None:
            self.worker.wait()
            self.worker = None
        self._set_busy(False)
        self._reload_state()
        if self._pending_update is not None:
            man, self._pending_update = self._pending_update, None
            QTimer.singleShot(300, lambda: self.show_update(man))

    def show_update(self, man):
        """새 버전 다이얼로그. 파일 교체 작업 중이면(재시작이 작업을 끊으므로) 끝난 뒤로 미룬다."""
        if self.worker is not None:
            self._pending_update = man
            return
        UpdateDialog(man, self).exec_()

    def _on_done(self, applied, restored):
        self._finish()
        if applied or restored:
            QMessageBox.information(self, APP_TITLE, f"완료!\n\n적용 {applied}개 · 원본 복원 {restored}개")
        else:
            QMessageBox.information(self, APP_TITLE, "변경할 항목이 없습니다.")

    def _on_failed(self, msg):
        self._finish()
        QMessageBox.warning(self, APP_TITLE, f"실패: {msg}")

    def closeEvent(self, e):
        if self.worker is not None:          # 압축 해제/교체 도중 종료 방지
            e.ignore()
            return
        super().closeEvent(e)


# ═══════════════════════════════════════════════════════════════════════════════
#  자동 업데이트 (퐁듀 런처와 같은 방식)
#   실행 중인 exe 는 "삭제"는 막히지만 "이름 변경"은 허용된다:
#     1) 새 exe 를 PZ-Optimizer.exe.new 로 받는다 (같은 폴더 — 볼륨이 달라지면 rename 이 복사가 됨)
#     2) sha256 검증
#     3) PZ-Optimizer.exe → .old  (실행 중이어도 성공)
#     4) .new → PZ-Optimizer.exe
#     5) 새 exe 를 띄우고 현재 프로세스 종료 (이미 관리자 권한이라 UAC 재확인 없음)
#     6) 새 프로세스가 시작할 때 .old 삭제
#   3~4 사이에서 실패하면 즉시 롤백한다.
# ═══════════════════════════════════════════════════════════════════════════════
def parse_version(s):
    """'v1.2.3' → (1, 2, 3). 비교 불가능한 값이면 None."""
    nums = re.findall(r"\d+", str(s or ""))
    if not nums:
        return None
    return tuple(int(n) for n in nums[:4])


def _update_url_ok(url) -> bool:
    """https + 허용 호스트인지. 리다이렉트 최종 URL 도 이걸로 다시 검사한다."""
    from urllib.parse import urlparse
    try:
        p = urlparse(url or "")
    except ValueError:
        return False
    return p.scheme == "https" and (p.hostname or "") in UPDATE_ALLOWED_HOSTS


def cleanup_old_exe():
    """이전 업데이트가 남긴 .old 제거. 실패해도 무시(다음 실행 때 다시 시도)."""
    if not IS_FROZEN:
        return
    old = sys.executable + ".old"
    for _ in range(10):
        try:
            os.remove(old)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.3)   # 직전 프로세스가 아직 완전히 안 죽었을 수 있다


def _fresh_instance_env():
    """재시작용 환경변수. onefile 이 남긴 임시 압축해제 폴더(_MEIxxxx) 정보를 물려주면
       새 exe 가 곧 지워질 구 폴더를 재사용하려다 Qt 플러그인을 못 찾는다 → 비운다."""
    env = os.environ.copy()
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"      # PyInstaller 6.9+ 공식 방식
    meipass = getattr(sys, "_MEIPASS", None)
    for k in list(env):
        if k.startswith("_PYI_") or k == "_MEIPASS2":
            env.pop(k, None)
        elif meipass and k.upper().startswith("QT_") and meipass.lower() in env[k].lower():
            env.pop(k, None)
    return env


class UpdateWorker(QObject):
    """매니페스트 조회 + exe 다운로드/교체를 백그라운드 스레드에서 수행.
       조회 실패는 조용히 무시 — 오프라인이어도 도구는 그대로 써야 하므로."""

    found     = pyqtSignal(dict)        # 새 버전 있음 → 매니페스트 dict
    progress  = pyqtSignal(int, int)    # 받은 바이트, 전체 바이트(모르면 0)
    stage     = pyqtSignal(str)         # 다운로드 이후 단계 문구
    finished  = pyqtSignal(str)         # "" = 성공(재실행 대기), 그 외 = 에러 메시지

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cancel = False

    def cancel(self):
        self._cancel = True

    # --- 조회 ---
    def check_async(self):
        threading.Thread(target=self._check, daemon=True).start()

    def _check(self):
        try:
            man = self._fetch_manifest()
        except Exception as e:
            print("[update] check failed: %s: %s" % (type(e).__name__, e))
            return
        cur = parse_version(VERSION)
        new = parse_version(man.get("version"))
        if not cur or not new or new <= cur:
            return
        if not _update_url_ok(man.get("url")) or not man.get("sha256"):
            print("[update] manifest rejected (bad url or missing sha256)")
            return
        floor = parse_version(man.get("min_supported"))
        man["_forced"] = bool(floor and cur < floor)
        self.found.emit(man)

    def _fetch_manifest(self):
        import urllib.request
        req = urllib.request.Request(
            UPDATE_MANIFEST_URL,
            headers={"User-Agent": "PZOptimizer/" + VERSION, "Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=UPDATE_TIMEOUT) as r:
            raw = r.read(64 * 1024)
        return json.loads(raw.decode("utf-8"))

    # --- 적용 ---
    def apply_async(self, man):
        threading.Thread(target=self._apply, args=(man,), daemon=True).start()

    def _apply(self, man):
        if not IS_FROZEN:
            self.finished.emit("개발 모드(python optimizer.py)에서는 자동 업데이트를 쓸 수 없습니다.")
            return
        exe = sys.executable
        tmp = exe + ".new"
        old = exe + ".old"
        try:
            self._download(man["url"], tmp, man["sha256"])
        except Exception as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            if self._cancel:
                self.finished.emit("취소됨")
            else:
                self.finished.emit("다운로드 실패: %s: %s" % (type(e).__name__, e))
            return

        # 교체 — 백신 실시간 감시가 방금 받은 exe(수백 MB)를 검사하며 잠깐 잠그면
        # os.replace 가 수 초~수십 초 블로킹될 수 있어 단계를 명시한다.
        self.stage.emit("설치 중… (백신 검사 중이면 잠시 걸릴 수 있습니다)")
        try:
            try:
                os.remove(old)
            except OSError:
                pass
            os.replace(exe, old)          # 실행 중이어도 rename 은 허용됨
        except OSError as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            self.finished.emit(
                "기존 파일을 교체할 수 없습니다 (%s).\n"
                "프로그램을 바탕화면 등 쓰기 가능한 폴더로 옮긴 뒤 다시 시도하세요." % e)
            return
        try:
            os.replace(tmp, exe)
        except OSError as e:
            os.replace(old, exe)          # 롤백 — exe 가 사라진 채로 끝나면 안 된다
            self.finished.emit("교체 실패, 원래 버전으로 되돌렸습니다: %s" % e)
            return
        self.finished.emit("")

    def _download(self, url, dest, want_sha):
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "PZOptimizer/" + VERSION})
        h = hashlib.sha256()
        got = 0
        with urllib.request.urlopen(req, timeout=UPDATE_DL_TIMEOUT) as r:
            final = getattr(r, "url", None) or url          # 리다이렉트 최종 URL 재검증
            if not _update_url_ok(final):
                raise ValueError("허용되지 않은 다운로드 위치: %s" % final)
            try:
                total = int(r.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                total = 0
            self.progress.emit(0, total)
            with open(dest, "wb") as f:
                while True:
                    if self._cancel:
                        raise IOError("취소됨")
                    chunk = r.read(1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    got += len(chunk)
                    self.progress.emit(got, total)
        if total and got != total:
            raise IOError("전송이 중간에 끊겼습니다 (%d/%d bytes)" % (got, total))
        if h.hexdigest().lower() != str(want_sha).strip().lower():
            raise ValueError("해시 불일치 — 파일이 손상됐거나 변조됐습니다")


class UpdateDialog(QDialog):
    """새 버전 안내 → [지금 업데이트] 시 그 자리에서 받아 교체하고 재실행."""

    def __init__(self, man, parent=None):
        super().__init__(parent)
        self.man = man
        self.forced = bool(man.get("_forced"))
        self.worker = UpdateWorker(self)
        self.worker.progress.connect(self._on_progress)
        self.worker.stage.connect(self._on_stage)
        self.worker.finished.connect(self._on_finished)
        self._working = False
        self.setWindowTitle(f"{APP_TITLE} 업데이트")
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        ico = resource_path(ICON_FILE)
        if os.path.exists(ico):
            self.setWindowIcon(QIcon(ico))
        self.setFixedWidth(440)
        self._build()
        self.setStyleSheet(DARK_QSS)

    def _build(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(20, 18, 20, 18); v.setSpacing(10)
        title = QLabel("새 버전이 있습니다"); title.setObjectName("sect")
        v.addWidget(title)
        cur_new = QLabel("%s  →  %s" % (VERSION, self.man.get("version", "?")))
        cur_new.setObjectName("linkok")
        v.addWidget(cur_new)
        notes = (self.man.get("notes") or "").strip()
        if notes:
            box = QTextEdit(); box.setReadOnly(True); box.setPlainText(notes)
            box.setFixedHeight(110)
            v.addWidget(box)
        if self.forced:
            warn = QLabel("이 버전은 더 이상 사용할 수 없습니다. 업데이트가 필요합니다.")
            warn.setObjectName("err"); warn.setWordWrap(True)
            v.addWidget(warn)
        self.bar = QProgressBar(); self.bar.setRange(0, 100); self.bar.setValue(0)
        self.bar.setTextVisible(False)
        self.bar.hide()
        v.addWidget(self.bar)
        self.status = QLabel(""); self.status.setObjectName("muted"); self.status.setWordWrap(True)
        v.addWidget(self.status)
        row = QHBoxLayout(); row.addStretch(1)
        self.later_btn = QPushButton("종료" if self.forced else "나중에")
        self.later_btn.clicked.connect(self._later_click)
        row.addWidget(self.later_btn)
        self.go_btn = QPushButton("지금 업데이트"); self.go_btn.setObjectName("start")
        self.go_btn.clicked.connect(self._go_click)
        row.addWidget(self.go_btn)
        v.addLayout(row)

    # --- 동작 ---
    def _close_guarded(self) -> bool:
        """닫기 시도를 받아 실제로 닫아도 되는지 판정. 작업 중이면 취소로 전환하고 False."""
        if self._working:
            self.worker.cancel()
            self.status.setText("취소하는 중…")
            return False
        return True

    def closeEvent(self, e):
        if self._close_guarded():
            super().closeEvent(e)
        else:
            e.ignore()

    def reject(self):
        """ESC 키와 '나중에/취소' 버튼이 함께 타는 경로."""
        if not self._close_guarded():
            return
        super().reject()
        if self.forced:
            QApplication.quit()
            os._exit(0)      # 최소지원 미만 버전은 쓰지 못하게 바로 종료

    def _later_click(self):
        self.reject()

    def _go_click(self):
        if self._working:
            return
        self._working = True
        self.go_btn.setEnabled(False)
        self.later_btn.setText("취소")
        self.bar.show(); self.bar.setValue(0)
        self.status.setText("다운로드 중…")
        # X 버튼을 setWindowFlag 로 없애지 말 것 — exec_() 모달 중 네이티브 핸들이 재생성되며
        # 다이얼로그가 숨은 채 모달만 남는 문제가 런처에서 있었다. 닫기 차단은 가드가 담당.
        self.worker.apply_async(self.man)

    def _on_progress(self, got, total):
        if total > 0:
            self.bar.setRange(0, 100)
            self.bar.setValue(int(got * 100 / total))
            self.status.setText("다운로드 중…  %.1f / %.1f MB  (%d%%)"
                                % (got / 1048576.0, total / 1048576.0, got * 100 // total))
        else:
            self.bar.setRange(0, 0)
            self.status.setText("다운로드 중…  %.1f MB" % (got / 1048576.0,))

    def _on_stage(self, text):
        self.bar.setRange(0, 0)
        self.status.setText(text)
        self.later_btn.setEnabled(False)   # 파일 교체 중엔 취소해도 되돌릴 게 없다

    def _on_finished(self, err):
        self._working = False
        if err:
            self.bar.hide()
            self.bar.setRange(0, 100)
            self.go_btn.setEnabled(True)
            self.later_btn.setEnabled(True)
            self.later_btn.setText("종료" if self.forced else "나중에")
            self.status.setText(err)
            return
        self.bar.setRange(0, 100); self.bar.setValue(100)
        self.status.setText("업데이트 완료 — 다시 시작합니다.")
        self.later_btn.setEnabled(False)
        QTimer.singleShot(600, self._restart)

    def _restart(self):
        """새 exe 를 띄우고 현재 프로세스를 끝낸다."""
        import subprocess
        try:
            flags = 0
            if os.name == "nt":
                flags = 0x00000008 | 0x08000000    # DETACHED_PROCESS | CREATE_NO_WINDOW
            subprocess.Popen([sys.executable],
                             cwd=os.path.dirname(sys.executable) or None,
                             close_fds=True, creationflags=flags,
                             env=_fresh_instance_env())
        except Exception as e:
            QMessageBox.warning(
                self, "재시작 실패",
                "업데이트는 끝났지만 자동 재시작에 실패했습니다 (%s).\n"
                "프로그램을 직접 다시 실행해주세요." % e)
        self.accept()
        QApplication.quit()
        os._exit(0)      # Qt 이벤트루프/백그라운드 스레드가 종료를 붙잡지 않게 즉시 탈출


def main():
    cleanup_old_exe()        # 직전 업데이트가 남긴 .old 정리
    app = QApplication(sys.argv)
    ico = resource_path(ICON_FILE)
    if os.path.exists(ico):
        app.setWindowIcon(QIcon(ico))
    w = OptimizerWindow()
    w.show()
    # 업데이트 확인 — 창을 막지 않는다. 새 버전이 있을 때만 다이얼로그가 모달로 올라온다.
    updater = UpdateWorker(app)
    updater.found.connect(w.show_update)
    if IS_FROZEN:
        QTimer.singleShot(800, updater.check_async)
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
