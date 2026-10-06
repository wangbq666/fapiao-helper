@echo off
rem Build the trimmed desktop exe into distqt\.
rem   打包桌面版.bat         -> onefile  distqt\发票助手.exe      (默认)
rem   打包桌面版.bat dir     -> onedir   distqt\发票助手\ 文件夹   (启动更快)
rem   打包桌面版.bat dir zip -> onedir + 7z 压缩包(找到 7z 时)
rem 本文件必须保存为 GBK 编码(cmd 按 OEM 代码页解码; 存成 UTF-8 会碎行)。
cd /d %~dp0

set MODE=%1
if "%MODE%"=="" set MODE=onefile

if "%MODE%"=="dir" (
  set "SPEC=buildqt\发票助手Qt_dir.spec"
) else (
  set "SPEC=buildqt\发票助手Qt.spec"
)

if not exist "%SPEC%" (
  echo [FAIL] spec not found: %SPEC%
  exit /b 1
)

rem UPX(可选): tools\upx\upx.exe 存在时自动启用, 主要对 onedir 有效
set "UPXDIR=tools\upx"
if exist "%UPXDIR%\upx.exe" set "PATH=%UPXDIR%;%PATH%"
where upx >nul 2>nul
if not errorlevel 1 (echo [INFO] UPX enabled) else echo [INFO] UPX not found, skip

py -m PyInstaller --noconfirm --clean "%SPEC%" --distpath distqt --workpath buildqt
if errorlevel 1 (
  echo [FAIL] build failed
  exit /b 1
)

if "%MODE%"=="dir" (
  echo [OK] onedir ready: distqt\发票助手\发票助手.exe
) else (
  for %%F in ("distqt\*.exe") do echo [OK] %%~fF  %%~zF bytes
)

if "%MODE%"=="dir" if "%2"=="zip" (
  where 7z >nul 2>nul
  if errorlevel 1 (
    echo [INFO] 7z not found, folder ready at distqt\发票助手\
  ) else (
    7z a -t7z -mx=9 distqt\发票助手-免安装.zip ".\distqt\发票助手\*"
    echo [OK] distqt\发票助手-免安装.zip
  )
)

echo done.
exit /b 0
