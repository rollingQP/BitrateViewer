@echo off
echo ======================================
echo  Video Bitrate Viewer - Build Script
echo ======================================
echo.

if not exist "lib\ffprobe.exe" (
    echo ERROR: lib\ffprobe.exe not found!
    echo.
    echo Download FFmpeg from:
    echo https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip
    echo.
    echo Copy ffmpeg.exe and ffprobe.exe to the lib folder.
    pause
    exit /b 1
)

echo [1/2] Cleaning previous builds...
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"

echo [2/2] Building executable...
py -m PyInstaller video_bitrate_viewer.spec --noconfirm

if errorlevel 1 (
    echo BUILD FAILED!
    pause
    exit /b 1
)

echo.
echo ======================================
echo  Build Complete!
echo ======================================
echo  Output: dist\VideoBitrateViewer\
echo ======================================
pause