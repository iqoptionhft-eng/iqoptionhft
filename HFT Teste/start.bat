@echo off
cd /d "%~dp0"
set PY=%LocalAppData%\Programs\Python\Python312\python.exe
if not exist "%PY%" set PY=python
rem A5: so cria o venv e instala dependencias na primeira vez (versoes fixadas).
rem Para atualizar de proposito: apague a pasta .venv e rode de novo.
if not exist ".venv\Scripts\python.exe" (
  "%PY%" -m venv .venv
  call .venv\Scripts\activate.bat
  python -m pip install --upgrade pip
  pip install -r requirements.txt
) else (
  call .venv\Scripts\activate.bat
)
echo.
echo Abra a URL com #token=... que aparece abaixo (tambem salva em data\panel_token.txt)
python run.py
