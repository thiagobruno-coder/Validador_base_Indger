@echo off
chcp 65001 >nul
title Painel - Busca ANEEL
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto iniciar

echo ============================================================
echo  Primeira execucao: preparando o ambiente (leva alguns minutos)
echo ============================================================
where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo O Python nao foi encontrado.
  echo Instale em https://www.python.org/downloads/ marcando "Add python.exe to PATH"
  echo e depois clique duas vezes neste arquivo de novo.
  pause
  exit /b 1
)
python -m venv .venv
if errorlevel 1 goto falhou
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto falhou

:iniciar
".venv\Scripts\python.exe" -m aneel_busca painel
pause
exit /b 0

:falhou
echo.
echo Nao foi possivel instalar as dependencias.
echo Se estiver na rede da empresa, tente outra rede ou peca ao TI o endereco do proxy.
rmdir /s /q .venv 2>nul
pause
exit /b 1
