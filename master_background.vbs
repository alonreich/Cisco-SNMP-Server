Set WshShell = CreateObject("WScript.Shell")
' Run master.bat with window style 0 (hidden)
WshShell.Run chr(34) & "C:\SNMP-Server\master.bat" & chr(34), 0
Set WshShell = Nothing
