@echo off
chcp 65001 >nul 2>&1
setlocal enabledelayedexpansion
cd /d D:\Project\Aria2

:: Clear Python cache to ensure latest code
if exist "aria2\__pycache__" rmdir /s /q "aria2\__pycache__" >nul 2>&1

set "URLFILE=%TEMP%\aria2_urls.txt"

:START
cls
echo ============================================================
echo   Aria2 Download Tool - Multi-thread Resume
echo ============================================================
echo.
echo   Paste download URLs, one per line, empty line to finish:
echo   ------------------------------------------------------------
echo   HuggingFace:  .../resolve/main/filename
echo   GitHub:       .../releases/download/tag/filename
echo   Or input a .txt file path to read URL list
echo   ------------------------------------------------------------
echo.

> "!URLFILE!" echo.

set "LINE="
set /p "LINE=> "

if "!LINE!"=="" goto START

if /i "!LINE:~-4!"==".txt" (
    if exist "!LINE!" (
        copy "!LINE!" "!URLFILE!" >nul
        echo   URLs loaded from file
        timeout /t 1 >nul
        goto CONFIRM_SETTINGS
    )
)

>> "!URLFILE!" echo !LINE!

:COLLECT
set "LINE="
echo.
set /p "LINE=> "
if "!LINE!"=="" goto CONFIRM_SETTINGS
>> "!URLFILE!" echo !LINE!
goto COLLECT


:CONFIRM_SETTINGS
echo.
echo   ------------------------------------------------------------
echo   Download strategy:
echo     [1] Mirror - no proxy needed (recommended)
echo     [2] Direct - requires global proxy
echo     [3] Try direct first, fallback to mirror
echo.
set "STRATEGY=1"
set /p "STRATEGY=  Select [1]: "
if "!STRATEGY!"=="" set "STRATEGY=1"

set "OUTPUT_DIR=D:\models"
set /p "OUTPUT_DIR=  Save to [D:\models]: "
if "!OUTPUT_DIR!"=="" set "OUTPUT_DIR=D:\models"

set "THREADS=8"
set /p "THREADS=  Threads [8]: "
if "!THREADS!"=="" set "THREADS=8"

set "RETRIES=30"
set /p "RETRIES=  Retries [30]: "
if "!RETRIES!"=="" set "RETRIES=30"

set "TOTAL=0"
for /f "usebackq delims=" %%a in ("!URLFILE!") do (
    set "LINE=%%a"
    if not "!LINE!"=="" set /a TOTAL+=1
)

echo.
echo ============================================================
echo   Files: !TOTAL!
if "!STRATEGY!"=="1" echo   Strategy: Mirror
if "!STRATEGY!"=="2" echo   Strategy: Direct
if "!STRATEGY!"=="3" echo   Strategy: Direct then Mirror
echo   Save to: !OUTPUT_DIR!\
echo   Threads: !THREADS!   Retries: !RETRIES!
echo ============================================================
echo.
set "CONFIRM=y"
set /p "CONFIRM=  Confirm? [Y/n]: "
if /i "!CONFIRM!"=="n" goto START

if not exist "!OUTPUT_DIR!" mkdir "!OUTPUT_DIR!"

set "INDEX=0"
set "SUCCESS=0"
set "FAILED=0"

for /f "usebackq delims=" %%a in ("!URLFILE!") do (
    set "LINE=%%a"
    if not "!LINE!"=="" (
        set /a INDEX+=1
        call :PROCESS_URL "!LINE!"
    )
)

echo.
echo ============================================================
echo   Done - Success: !SUCCESS!  Failed: !FAILED!  Total: !TOTAL!
echo ============================================================
echo.

del "!URLFILE!" >nul 2>&1
set "AGAIN=n"
set /p "AGAIN=  Download more? [y/N]: "
if /i "!AGAIN!"=="y" goto START

cd /d D:\Project\Aria2
endlocal
exit /b


:PROCESS_URL
set "URL=%~1"

for /f "tokens=1 delims=?" %%a in ("!URL!") do set "URL=%%a"

echo !URL! | findstr /i "/tree/" >nul && (
    echo.
    echo   [!INDEX!/!TOTAL!] Skip - repo page, not file link
    set /a FAILED+=1
    goto :eof
)
echo !URL! | findstr /i "/blob/" >nul && (
    echo.
    echo   [!INDEX!/!TOTAL!] Skip - blob page, use /resolve/ link
    set /a FAILED+=1
    goto :eof
)

set "SOURCE=Generic"
set "MIRROR_URL=!URL!"
set "HAS_MIRROR=0"

echo !URL! | findstr /i "huggingface.co" >nul && (
    set "SOURCE=HuggingFace"
    set "MIRROR_URL=!URL:huggingface.co=hf-mirror.com!"
    set "HAS_MIRROR=1"
)
echo !URL! | findstr /i "github.com" >nul && (
    set "SOURCE=GitHub"
    set "MIRROR_URL=https://ghproxy.net/!URL!"
    set "HAS_MIRROR=1"
)
echo !URL! | findstr /i "modelscope" >nul && (
    set "SOURCE=ModelScope"
    set "HAS_MIRROR=0"
)
echo !URL! | findstr /i "hf-mirror.com" >nul && (
    set "SOURCE=HuggingFace"
    set "HAS_MIRROR=0"
)
echo !URL! | findstr /i "ghproxy" >nul && (
    set "SOURCE=GitHub"
    set "HAS_MIRROR=0"
)

set "FILENAME=!URL!"
:EXTRACT_NAME
for /f "tokens=1* delims=/" %%a in ("!FILENAME!") do (
    if not "%%b"=="" (
        set "FILENAME=%%b"
        goto EXTRACT_NAME
    )
)

if "!STRATEGY!"=="1" (
    if "!HAS_MIRROR!"=="1" (
        set "DL_URL=!MIRROR_URL!"
    ) else (
        set "DL_URL=!URL!"
    )
) else if "!STRATEGY!"=="2" (
    set "DL_URL=!URL!"
) else if "!STRATEGY!"=="3" (
    set "DL_URL=!URL!"
) else (
    set "DL_URL=!URL!"
)

echo.
echo   [!INDEX!/!TOTAL!] !SOURCE! - !FILENAME!
echo   ------------------------------------------------

cd /d "!OUTPUT_DIR!"

:DOWNLOAD_THIS
python -m aria2 -s !THREADS! -r !RETRIES! -o "!FILENAME!" "!DL_URL!"

if !ERRORLEVEL! EQU 0 (
    echo   [OK] Done
    set /a SUCCESS+=1
) else if "!STRATEGY!"=="3" (
    if "!DL_URL!"=="!URL!" (
        if "!HAS_MIRROR!"=="1" (
            echo   Direct failed, switching to mirror...
            set "DL_URL=!MIRROR_URL!"
            goto DOWNLOAD_THIS
        )
    )
    echo   [FAIL] Interrupted - rerun to resume
    set /a FAILED+=1
) else (
    echo   [FAIL] Interrupted - rerun to resume
    set /a FAILED+=1
)

if