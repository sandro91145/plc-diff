@echo off
rem Starts PLC Diff without a console window.
set "PY=%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe"
if not exist "%PY%" set "PY=pythonw"
start "" "%PY%" "%~dp0PLCDiff.pyw"
