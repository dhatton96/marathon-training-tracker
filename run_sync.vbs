Option Explicit

Dim objShell, strDir, strCmd
strDir = "C:\Users\user\Desktop\marathon"

' Run sync_strava.py silently, log output to sync_log.txt for debugging
strCmd = "cmd /c cd /d """ & strDir & """ && " & _
         """C:\Python314\python.exe"" sync_strava.py >> """ & _
         strDir & "\sync_log.txt"" 2>&1"

Set objShell = CreateObject("WScript.Shell")
objShell.Run strCmd, 0, True   ' 0 = hidden window, True = wait for completion
Set objShell = Nothing
