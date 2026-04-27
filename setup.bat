@echo off
REM BrowPic - first-time setup
cd /d "%~dp0"

echo === Creez venv ===
python -m venv .venv
if errorlevel 1 goto err

echo === Instalez dependintele ===
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 goto err

echo === Instalez Chromium pentru Playwright ===
python -m playwright install chromium
if errorlevel 1 goto err

echo.
echo === Gata. Ruleaza run.bat ca sa pornesti aplicatia. ===
pause
exit /b 0

:err
echo.
echo EROARE la setup. Verifica ca ai Python 3.10+ instalat si in PATH.
pause
exit /b 1
