@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul 2>&1
cd /d "%~dp0"

set "MODEL_DIR=E:\ModeLs"
set "URL_FILE=%TEMP%\aria2_urls_%RANDOM%.txt"

echo ============================================================
echo   Aria2 verified model downloader
echo   Default model directory: %MODEL_DIR%
echo ============================================================
echo.
echo Paste one direct file URL per line. Submit an empty line to start.
echo You may also enter the path of a .txt URL list as the first line.
echo.

set "FIRST="
set /p "FIRST=^> "
if not defined FIRST exit /b 1
if /i "%FIRST:~-4%"==".txt" if exist "%FIRST%" (
    copy /y "%FIRST%" "%URL_FILE%" >nul
    goto SETTINGS
)
>"%URL_FILE%" echo(%FIRST%

:COLLECT
set "LINE="
set /p "LINE=^> "
if not defined LINE goto SETTINGS
>>"%URL_FILE%" echo(%LINE%
goto COLLECT

:SETTINGS
set "OUTPUT_DIR=%MODEL_DIR%"
set /p "OUTPUT_DIR=Save directory [%MODEL_DIR%]: "
if not defined OUTPUT_DIR set "OUTPUT_DIR=%MODEL_DIR%"
set "THREADS=8"
set /p "THREADS=Segments [8]: "
if not defined THREADS set "THREADS=8"
set "RETRIES=10"
set /p "RETRIES=Retries [10]: "
if not defined RETRIES set "RETRIES=10"
if not exist "%OUTPUT_DIR%" mkdir "%OUTPUT_DIR%"

set /a SUCCESS=0, FAILED=0
for /f "usebackq delims=" %%U in ("%URL_FILE%") do call :DOWNLOAD "%%U"
del "%URL_FILE%" >nul 2>&1
echo.
echo Finished. Success: %SUCCESS%  Failed: %FAILED%
exit /b %FAILED%

:DOWNLOAD
set "URL=%~1"
if not defined URL goto :eof
for /f "tokens=* delims=" %%N in ("%URL%") do set "URL=%%N"
for %%N in ("%URL:/=\%") do set "FILENAME=%%~nxN"
for /f "tokens=1 delims=?#" %%N in ("%FILENAME%") do set "FILENAME=%%N"
if not defined FILENAME (
    echo [FAIL] Cannot extract filename from %URL%
    set /a FAILED+=1
    goto :eof
)
echo.
echo Downloading %FILENAME%
python -m aria2 -s %THREADS% -r %RETRIES% -o "%OUTPUT_DIR%\%FILENAME%" "%URL%"
if errorlevel 1 (
    set /a FAILED+=1
) else (
    set /a SUCCESS+=1
)
goto :eof
