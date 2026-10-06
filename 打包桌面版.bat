@echo off
rem Build the trimmed desktop exe (~46MB) into distqt\.
rem ASCII only: cmd decodes this file with the OEM codepage.
cd /d %~dp0

set SPEC=
for %%F in ("buildqt\*.spec") do set "SPEC=%%~fF"
if not defined SPEC (
  echo [FAIL] no *.spec found in buildqt\
  exit /b 1
)

py -m PyInstaller --noconfirm --clean "%SPEC%" --distpath distqt --workpath buildqt
if errorlevel 1 (
  echo [FAIL] build failed
  exit /b 1
)

for %%F in ("distqt\*.exe") do echo [OK] %%~fF  %%~zF bytes
echo done.
exit /b 0
