# Start QC-HUB backend and frontend in two PowerShell windows.
# Run this from the repository root: .\start-project.ps1

$root = "C:\Users\Taofeek Olatunji\Documents\QC-HUB"

Start-Process powershell -ArgumentList @(
    "-NoExit",
    "-Command",
    "Set-Location '$root\\backend'; & .\\.venv\\Scripts\\Activate.ps1; uvicorn app.main:app --reload --host 127.0.0.1 --port 8000"
)

Start-Process powershell -ArgumentList @(
    "-NoExit",
    "-Command",
    "Set-Location '$root\\frontend'; npm install; npm run dev"
)
