@echo off
setlocal
chcp 65001 >nul 2>&1
cd /d "%~dp0"

set "MODEL_DIR=E:\ModeLs"
set "MODEL=Qwen3.6-27B-Q3_K_M.gguf"
set "URL=https://huggingface.co/DhruvalLabs/Qwen3.6-27B-GGUF/resolve/main/Qwen3.6-27B-Q3_K_M.gguf?download=true"
set "SHA256=06e2b050c41a741338824e4b9b0b94a49795832fba1d0daeca492033a42d7bf8"

if not exist "%MODEL_DIR%" mkdir "%MODEL_DIR%"
echo Downloading %MODEL% to %MODEL_DIR%
echo Expected size: 13,500,735,744 bytes
echo The SHA-256 digest will be checked after download.
echo.

python -m aria2 -s 8 -r 10 --sha256 "%SHA256%" -o "%MODEL_DIR%\%MODEL%" "%URL%"
if errorlevel 1 (
    echo.
    echo Download incomplete or verification failed. Run this script again to resume.
    exit /b 1
)

echo.
echo Download and SHA-256 verification completed successfully.
exit /b 0
