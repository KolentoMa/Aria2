@echo off
cd /d D:\Project\Aria2

set URL=https://huggingface.co/HauhauCS/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive/resolve/main/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-Q5_K_P.gguf?download=true

echo ============================================
echo  Qwen3.6 35B GGUF Download Script
echo ============================================
echo.
echo   URL: %URL%
echo   Save: %OUTPUT%
echo   Threads: 8
echo   Retries: 30
echo   Size: ~30 GB
echo.
echo   Ctrl+C to pause, rerun to resume
echo ============================================
echo.

python -m aria2 -s 8 -r 30 -o "%OUTPUT%" "%URL%"

if %ERRORLEVEL% EQU 0 (
    echo.
    echo ============================================
    echo   Download complete!
    echo ============================================
) else (
    echo.
    echo ============================================
    echo   Interrupted, progress saved.
    echo   Run this script again to resume.
    echo ============================================
)

pause
