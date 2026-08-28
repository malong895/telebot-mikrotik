Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "C:\Users\itfas\OneDrive\Documents\brother-did-it-telegrambot-services"
WshShell.Run """C:\Users\itfas\OneDrive\Documents\brother-did-it-telegrambot-services\venv\Scripts\pythonw.exe"" ""C:\Users\itfas\OneDrive\Documents\brother-did-it-telegrambot-services\service_runner.py""", 0, False
