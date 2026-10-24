@echo off
echo ============================================
echo   PZ Optimizer  ( optimizer.py )  --^>  exe build
echo ============================================
echo.

echo [0/4] Checking Python...
py --version
if errorlevel 1 (
    echo.
    echo [!] Python not found. Install it first:
    echo       winget install -e --id Python.Python.3.12
    pause
    exit /b 1
)
echo.

echo [1/4] Checking bundled resources...
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

echo [2/4] Installing PyQt5 / PyInstaller...
py -m pip install PyQt5
if errorlevel 1 goto :pipfail
py -m pip install --upgrade pyinstaller
if errorlevel 1 goto :pipfail
echo.

echo [3/4] Building exe... ^(the GraalVM zip makes this take a while^)
REM --uac-admin : always run elevated (game folder is often under Program Files)
py -m PyInstaller --onefile --noconsole --uac-admin --name PZ-Optimizer --icon=pongdu.ico --add-data "pongdu.ico;." --add-data "opt_conf;opt_conf" --add-data "graalvm-jdk-25_windows-x64_bin.zip;." optimizer.py
if errorlevel 1 (
    echo.
    echo [!] Build failed. Copy the red error above and ask about it.
    pause
    exit /b 1
)
echo.

echo [4/4] Hashing output...
certutil -hashfile "dist\PZ-Optimizer.exe" SHA256 | findstr /v ":"

echo.
echo ============================================
echo   DONE!  Run  dist\PZ-Optimizer.exe
echo   Upload it as a GitHub release asset ^(not to git^).
echo ============================================
pause
exit /b 0

:pipfail
echo.
echo [!] pip failed. See the error above.
pause
exit /b 1
