# PZ Optimizer

Project Zomboid 설치 폴더 최적화 도구. 퐁듀 런처에 있던 '게임 최적화' 기능을 분리한 것.

## 항목

| 항목 | 대상 빌드 | 바뀌는 것 |
|---|---|---|
| 청크 로딩 / 월드 업데이트 / 좀비 네트워크 / 좀비 개체수 | B41 | `zombie/**.class` 패치본으로 교체 (`opt_conf/`) |
| JVM 힙 = RAM 절반 | B41·B42 | `ProjectZomboid64.json`·`.bat` 의 `-Xms`/`-Xmx` |
| JVM 교체 (GraalVM) | B42 (Java 25) | `jre64/` → Oracle GraalVM, `-XX:+AlwaysPreTouch` `-Djdk.graal.CompilerConfiguration=enterprise` 추가 |

- 원본은 게임 폴더 `puppet_opt_backup/` 에 보관. 체크 해제 = 원본 복원.
- `jre64` 는 복사하지 않고 `puppet_opt_backup/jre64` 로 이름만 옮긴다.
- 적용 상태는 `puppet_opt_backup/pongdu_opt_state.json` 에 기록 → 퐁듀 런처가 읽어서 "최적화 풀림"을 표시.
- PZ 업데이트·무결성 검사 후에는 다시 실행해야 한다.

## 빌드

1. Oracle GraalVM for JDK 25 (LTS) Windows x64 zip 을 받아 **수정 없이** `build.bat` 옆에 둔다 (git 에는 안 올라감).
2. `build.bat` 실행 → `dist/PZ-Optimizer.exe`
3. `dist/version.json` 의 `notes` 채우기
4. GitHub 릴리스 생성 — 태그는 `optimizer.py` 의 `VERSION` 과 똑같이 (예: `v1.1.0`)
5. `dist/PZ-Optimizer.exe` 와 `dist/version.json` 을 둘 다 asset 으로 첨부

## 자동 업데이트

실행하면 `releases/latest/download/version.json` 을 조회해 새 버전이면 업데이트 창을 띄운다.
[지금 업데이트] → 같은 폴더에 `.new` 로 받아 sha256 검증 → 자기 교체 → 재실행.
`min_supported` 를 넣으면 그 미만 버전은 업데이트하지 않으면 실행이 종료된다.

현재 포함 버전:

| 파일 | GraalVM | JDK | sha256 |
|---|---|---|---|
| `graalvm-jdk-25_windows-x64_bin.zip` | 25.0.4 | 25.0.4+7-LTS | `5198b29a6448cb72b9a3cef10866723e2d1e2ec4799561a344e032bb7c61e555` |

## 라이선스 메모

Oracle GraalVM 은 GraalVM Free Terms and Conditions(GFTC) 로 배포된다 — 무료 재배포 허용, 유료 배포 불가.
zip 은 Oracle 원본 그대로 넣고, 압축 해제한 폴더에 Oracle 의 `LICENSE.txt` 가 함께 들어간다.
