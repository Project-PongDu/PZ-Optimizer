@echo off
echo ============================================
echo   PZ Optimizer  ( optimizer.py )  --^>  exe build
echo ============================================
echo.

echo [0/5] Checking Python...
py --version
if errorlevel 1 (
    echo.
    echo [!] Python not found. Install it first:
    echo       winget install -e --id Python.Python.3.12
    pause
    exit /b 1
)
echo.

echo [1/5] Checking bundled resources...
REM GraalVM zip is NOT in git (GitHub rejects files over 100MB).
REM Download the Windows x64 zip of Oracle GraalVM for JDK 25 (LTS) from
REM https://www.graalvm.org/downloads/ and put it next to this file, unmodified.
if not exist "graalvm-jdk-25_windows-x64_bin.zip" (
    echo [!] graalvm-jdk-25_windows-x64_bin.zip not found next to build.bat.
    echo     Download it from https://www.graalvm.org/downloads/  ^(Oracle GraalVM, JDK 25 LTS, Windows x64^)
    pause
    exit /b 1
)
if not exist "opt_conf\IsoWorld.class" (
    echo [!] opt_conf\ patch classes not found.
    pause
    exit /b 1
)
echo   GraalVM zip sha256:
certutil -hashfile "graalvm-jdk-25_windows-x64_bin.zip" SHA256 | findstr /v ":"
echo.

echo [2/5] Installing PyQt5 / PyInstaller...
py -m pip install PyQt5
if errorlevel 1 goto :pipfail
py -m pip install --upgrade pyinstaller
if errorlevel 1 goto :pipfail
echo.

echo [2.5/5] Collecting texture optimization files ^(Every Texture Optimized, from your Steam Workshop^)...
REM Copies the PNGs of the two Workshop items into tex_conf\ so they ship inside the exe like opt_conf\.
REM You (the builder) must be subscribed to both; players are not. tex_conf\ is not in git.
py collect_textures.py
if errorlevel 1 (
    echo.
    echo [!] Could not collect textures. Subscribe to Workshop items 2812026801 and 3119788162 and let Steam download them.
    pause
    exit /b 1
)
echo.

echo [3/5] Building exe... ^(the GraalVM zip makes this take a while^)
REM --uac-admin : always run elevated (game folder is often under Program Files)
py -m PyInstaller --onefile --noconsole --uac-admin --name PZ-Optimizer --icon=optimizer.ico --add-data "optimizer.ico;." --add-data "opt_conf;opt_conf" --add-data "tex_conf;tex_conf" --add-data "graalvm-jdk-25_windows-x64_bin.zip;." optimizer.py
if errorlevel 1 (
    echo.
    echo [!] Build failed. Copy the red error above and ask about it.
    pause
    exit /b 1
)
echo.

echo [4/5] Generating version.json for the release...
REM Reads VERSION from optimizer.py, hashes dist\PZ-Optimizer.exe, writes dist\version.json.
REM Upload BOTH files as assets on the GitHub release, tagged with the same version string -
REM the optimizer's auto-updater fetches version.json from /releases/latest/download/.
py make_version.py
if errorlevel 1 (
    echo.
    echo [!] Could not generate version.json. The exe is still fine.
)

echo.
echo [5/5] Done.
echo ============================================
echo   Run  dist\PZ-Optimizer.exe
echo.
echo   To publish an update, create a GitHub release
echo   tagged with the VERSION above and attach BOTH:
echo       dist\PZ-Optimizer.exe
echo       dist\version.json     ^(edit "notes" first^)
echo ============================================
pause
exit /b 0

:pipfail
echo.
echo [!] pip failed. See the error above.
pause
exit /b 1
