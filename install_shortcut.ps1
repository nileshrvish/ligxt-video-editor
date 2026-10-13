# Creates a "Ligxt Video Editor" shortcut on the Desktop (and in the Start menu). It starts Ligxt
# without a terminal window and puts an icon in the system tray (Open / Exports folder / Stop).
# Usage: .\install_shortcut.ps1          (run setup.ps1 first)
$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$Pythonw = Join-Path $Root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $Pythonw)) {
    Write-Host "The Python environment is missing. Run .\setup.ps1 first." -ForegroundColor Red
    exit 1
}
$Ico = Join-Path $Root "frontend\public\ligxt.ico"
if (-not (Test-Path $Ico)) {
    & (Join-Path $Root ".venv\Scripts\python.exe") -c "import sys; sys.path.insert(0, r'$Root'); import importlib.util as u; s = u.spec_from_file_location('l', r'$Root\Ligxt.pyw'); m = u.module_from_spec(s); s.loader.exec_module(m); m.icon_image().save(r'$Ico', sizes=[(16,16),(32,32),(48,48),(64,64)])"
}
$Shell = New-Object -ComObject WScript.Shell
$Targets = @([Environment]::GetFolderPath("Desktop"), (Join-Path ([Environment]::GetFolderPath("Programs")) ""))
foreach ($Dir in $Targets) {
    $Lnk = $Shell.CreateShortcut((Join-Path $Dir "Ligxt Video Editor.lnk"))
    $Lnk.TargetPath = $Pythonw
    $Lnk.Arguments = "`"$(Join-Path $Root 'Ligxt.pyw')`""
    $Lnk.WorkingDirectory = $Root
    $Lnk.IconLocation = $Ico
    $Lnk.Description = "Ligxt Video Editor (starts the local server, opens the browser, tray icon to stop it)"
    $Lnk.Save()
    Write-Host "Shortcut created: $(Join-Path $Dir 'Ligxt Video Editor.lnk')" -ForegroundColor Green
}
