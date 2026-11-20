@echo off
setlocal DisableDelayedExpansion
set "root=%HERMES_HOME%"
if not defined root set "root=%USERPROFILE%\.hermes"
if defined HERMES_PYTHON if exist "%HERMES_PYTHON%" goto selected
for %%P in ("%root%\hermes-agent\venv\Scripts\python.exe" "%root%\hermes-agent\.venv\Scripts\python.exe" "%USERPROFILE%\.hermes\hermes-agent\venv\Scripts\python.exe" "%USERPROFILE%\.hermes\hermes-agent\.venv\Scripts\python.exe") do if exist "%%~P" (
  set "HERMES_PYTHON=%%~P"
  goto selected
)
echo Use the existing Hermes Python to run bin\plugin.py assist-welcome.
pause
exit /b 1
:selected
"%HERMES_PYTHON%" -I -B "%~dp0bin\plugin.py" assist-welcome
if errorlevel 1 pause
