Set WshShell = CreateObject("WScript.Shell")
WshShell.Run chr(34) & "C:\SNMP-Server\venv\Scripts\python.exe" & chr(34) & " " & chr(34) & "C:\SNMP-Server\main.py" & chr(34), 0
Set WshShell = Nothing
