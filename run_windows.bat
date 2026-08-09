@echo off
setlocal
if "%~1"=="" (
  echo Drag an image folder onto this file, or pass the folder as its first argument.
  pause
  exit /b 1
)
py -m pip install -r "%~dp0requirements.txt"
py "%~dp0app.py" "%~1"
