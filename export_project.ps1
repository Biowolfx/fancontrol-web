# export_final.ps1 - Final working version
$projectRoot = "C:\Users\Admin\Projects\FanControlWeb"
$outputDir = "$projectRoot\export"

# Create output directory
New-Item -ItemType Directory -Force -Path $outputDir | Out-Null

Write-Host "========================================" -ForegroundColor Cyan
Write-Host " FanControl Web - Export Script" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

# Start building the install script
$bash = @"
#!/bin/bash
# FanControl Web v2.9 - Complete Installation Script
# Generated: $(Get-Date -Format "yyyy-MM-dd HH:mm:ss")

set -e
INSTALL_DIR="/volume1/docker/fancontrol-web"
mkdir -p "`$INSTALL_DIR"
cd "`$INSTALL_DIR"

echo "=========================================="
echo " FanControl Web v2.9 Installation"
echo "=========================================="

docker-compose down 2>/dev/null || true
rm -rf data/fan_config.json
mkdir -p templates/js data/logs

"@

# List of files to include
$files = @(
    @{Path="app.py"; Perms="644"},
    @{Path="requirements.txt"; Perms="644"},
    @{Path="Dockerfile"; Perms="644"},
    @{Path="docker-compose.yml"; Perms="644"},
    @{Path="templates/index.html"; Perms="644"},
    @{Path="templates/js/main.js"; Perms="644"}
)

# Add each file to the script
foreach ($f in $files) {
    $fullPath = Join-Path $projectRoot $f.Path
    if (Test-Path $fullPath) {
        $content = Get-Content $fullPath -Raw -Encoding UTF8
        $size = [math]::Round((Get-Item $fullPath).Length / 1KB, 2)
        
        $bash += "`n# ======================================`n"
        $bash += "# Creating $($f.Path) ($size KB)`n"
        $bash += "# ======================================`n"
        $bash += "cat > $($f.Path) << 'ENDOFFILE'`n"
        $bash += $content
        $bash += "`nENDOFFILE`n"
        
        Write-Host "  + $($f.Path) ($size KB)" -ForegroundColor Green
    } else {
        Write-Host "  ! MISSING: $($f.Path)" -ForegroundColor Red
    }
}

# Add final build commands
$bash += @'

echo ""
echo "=== Building Docker container ==="
docker-compose build --no-cache

echo ""
echo "=== Starting container ==="
docker-compose up -d

echo ""
echo "========================================"
echo " Installation Complete! (v2.9)"
echo " URL: http://$(hostname -I | awk '{print $1}'):5059"
echo ""
echo " Useful commands:"
echo "  docker logs -f fancontrol-web"
echo "  docker-compose restart"
echo "  docker-compose down"
echo "========================================"
'@

# Save the file
$outputPath = Join-Path $outputDir "install_fancontrol.sh"
$bash | Out-File -FilePath $outputPath -Encoding UTF8 -NoNewline

$finalSize = [math]::Round((Get-Item $outputPath).Length / 1KB, 2)
$lines = (Get-Content $outputPath).Count

Write-Host "`n========================================" -ForegroundColor Green
Write-Host " EXPORT COMPLETE" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host " File: install_fancontrol.sh" -ForegroundColor White
Write-Host " Size: $finalSize KB" -ForegroundColor White
Write-Host " Lines: $lines" -ForegroundColor White
Write-Host "`n Location: $outputDir" -ForegroundColor Yellow
Write-Host "`n To deploy on Synology:" -ForegroundColor Cyan
Write-Host " 1. Copy file to NAS" -ForegroundColor White
Write-Host " 2. SSH: chmod +x install_fancontrol.sh" -ForegroundColor White
Write-Host " 3. SSH: bash install_fancontrol.sh" -ForegroundColor White

# Open the folder
Invoke-Item $outputDir