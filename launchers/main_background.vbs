Set WshShell = CreateObject("WScript.Shell")
Set WshEnv = WshShell.Environment("Process")
WshEnv("PYTHONDONTWRITEBYTECODE") = "1"
WshShell.Run chr(34) & "C:\SNMP-Server\venv\Scripts\python.exe" & chr(34) & " " & chr(34) & "C:\SNMP-Server\server\main.py" & chr(34), 0
Set WshShell = Nothing
