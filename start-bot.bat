@echo off
rem ITFF bot launcher - starts the single supervised instance hidden
set "BASE=C:\Users\itfas\OneDrive\Documents\brother-did-it-telegrambot-services"
start "" "%BASE%\venv\Scripts\pythonw.exe" "%BASE%\service_runner.py"