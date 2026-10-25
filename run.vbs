' ===========================================================
'  Huawei Smart Life PC Console - windowless launcher
'
'  Double-click this file to start the app with no console
'  window at all. run.bat only flashes a window while it
'  checks dependencies; this one shows nothing.
'
'  Run run.bat once first: it installs the dependencies.
'
'  (ASCII only on purpose - Windows Script Host reads .vbs in
'   the system codepage, so non-ASCII text would be garbled.)
' ===========================================================
Option Explicit

Dim sh, fso, root, check, started

Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

root = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = root

If Not fso.FileExists(root & "\main.py") Then
    MsgBox "main.py not found next to this launcher.", 16, "Huawei Smart Life"
    WScript.Quit 1
End If

' Check dependencies first (hidden window, no flash). Without this the app
' would fail silently and the user would think nothing happened.
check = sh.Run("cmd /c py -3 -c ""import PySide6""", 0, True)
If check <> 0 Then
    MsgBox "PySide6 is not installed yet." & vbCrLf & vbCrLf & _
           "Please run run.bat once - it installs the dependencies " & _
           "and then starts the app.", 16, "Huawei Smart Life"
    WScript.Quit 1
End If

started = False
On Error Resume Next
' pyw = the windowless variant of the py launcher
sh.Run "pyw -3 """ & root & "\main.py""", 0, False
If Err.Number = 0 Then started = True
Err.Clear
If Not started Then
    sh.Run "pythonw """ & root & "\main.py""", 0, False
    If Err.Number = 0 Then started = True
    Err.Clear
End If
On Error GoTo 0

If Not started Then
    MsgBox "Could not launch Python." & vbCrLf & vbCrLf & _
           "Please run run.bat to see the detailed error.", 16, "Huawei Smart Life"
End If
