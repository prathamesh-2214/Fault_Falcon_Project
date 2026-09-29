@echo off
rem FaultFalcon: double-click to run. First run only: creates the Python environment (~5 min download).
cd /d "%~dp0"

".venv\Scripts\python.exe" -c "import streamlit, torch, transformers, chromadb" >nul 2>nul
if errorlevel 1 (
  where uv >nul 2>nul
  if errorlevel 1 (
    echo uv is needed once to create the Python environment. Install it with:
    echo   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 ^| iex"
    echo then run this file again.
    pause
    exit /b 1
  )
  echo First run: creating the Python environment ^(one time, a few minutes^) ...
  if not exist ".venv\Scripts\python.exe" uv venv --python 3.10 .venv || goto :error
  uv pip install --python .venv\Scripts\python.exe -r requirements.txt || goto :error
)

".venv\Scripts\python.exe" -m ff.launch %*
exit /b %errorlevel%

:error
echo Setup failed. See the messages above.
pause
exit /b 1
