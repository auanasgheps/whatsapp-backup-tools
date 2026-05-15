# ==============================================================================
# WA Media Archiver - Windows Companion Script
# Pulls WhatsApp database and contacts from an Android device via ADB
# For use before running wa_archiver.py on Linux
# v0.2
# ==============================================================================

param (
    [string]$OutputDir = ".\wa_pull",
    [switch]$DecryptDB,
    [string]$E2EKey = ""
)

# ==============================================================================
# Configuration
# ==============================================================================

$MSGSTORE_REMOTE = "/storage/emulated/0/Android/media/com.whatsapp/WhatsApp/Databases/msgstore.db.crypt15"
$CONTACTS_REMOTE = "content://com.android.contacts/data"

# ==============================================================================
# Helpers
# ==============================================================================

function Write-Header {
    Write-Host ""
    Write-Host "======================================" -ForegroundColor Cyan
    Write-Host "  WA Archiver - Windows Companion"     -ForegroundColor Cyan
    Write-Host "  v0.2"                                -ForegroundColor Cyan
    Write-Host "======================================" -ForegroundColor Cyan
    Write-Host ""
}

function Write-Step {
    param([string]$Message)
    Write-Host "[*] $Message" -ForegroundColor Green
}

function Write-WarningMsg {
    param([string]$Message)
    Write-Host "[!] $Message" -ForegroundColor Yellow
}

function Write-ErrorMsg {
    param([string]$Message)
    Write-Host "[X] $Message" -ForegroundColor Red
}

function Write-Success {
    param([string]$Message)
    Write-Host "[OK] $Message" -ForegroundColor Cyan
}

# ==============================================================================
# Checks
# ==============================================================================

function Test-ADB {
    Write-Step "Checking ADB availability..."
    try {
        $adbVersion = adb version 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw "ADB returned non-zero exit code"
        }
        Write-Success "ADB found: $($adbVersion[0])"
        return $true
    }
    catch [System.Management.Automation.CommandNotFoundException] {
        Write-ErrorMsg "ADB not found. Please install Android Platform Tools."
        Write-Host "  Download from: https://developer.android.com/tools/releases/platform-tools" -ForegroundColor Gray
        return $false
    }
    catch {
        Write-ErrorMsg "ADB found but returned an error: $_"
        return $false
    }
}

function Test-DeviceConnected {
    Write-Step "Checking for connected Android device..."
    $devices = adb devices 2>&1 | Select-String -Pattern "device$"
    if (-not $devices) {
        Write-ErrorMsg "No Android device found."
        Write-Host "  Make sure:"                                          -ForegroundColor Gray
        Write-Host "    - USB Debugging is enabled on your phone"         -ForegroundColor Gray
        Write-Host "    - The USB cable is connected"                      -ForegroundColor Gray
        Write-Host "    - You have authorized this computer on your phone" -ForegroundColor Gray
        return $false
    }
    $deviceCount = ($devices | Measure-Object).Count
    Write-Success "Found $deviceCount device(s)."
    return $true
}

function Test-WaCryptTools {
    Write-Step "Checking wa-crypt-tools availability..."
    try {
        python -c "import wa_crypt_tools" 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Import failed"
        }
        Write-Success "wa-crypt-tools is available."
        return $true
    }
    catch {
        Write-WarningMsg "wa-crypt-tools not found. Installing..."
        python -m pip install wa-crypt-tools
        if ($LASTEXITCODE -ne 0) {
            Write-ErrorMsg "Failed to install wa-crypt-tools."
            return $false
        }
        Write-Success "wa-crypt-tools installed successfully."
        return $true
    }
}

# ==============================================================================
# Pull functions
# ==============================================================================

function Pull-MsgStore {
    param([string]$DestDir)

    $destFile = Join-Path $DestDir "msgstore.db.crypt15"
    # Resolve to absolute path to avoid any ambiguity
    $destFile = [System.IO.Path]::GetFullPath($destFile)

    Write-Step "Pulling WhatsApp database from device..."
    Write-Host "  Remote: $MSGSTORE_REMOTE" -ForegroundColor Gray
    Write-Host "  Local : $destFile"        -ForegroundColor Gray

    # Redirect all ADB output to console only, never capture it
    adb pull $MSGSTORE_REMOTE $destFile
    if ($LASTEXITCODE -ne 0) {
        Write-ErrorMsg "Failed to pull msgstore. Make sure WhatsApp is installed and the path is correct."
        return $null
    }

    # Verify the file actually landed on disk
    if (-not (Test-Path $destFile)) {
        Write-ErrorMsg "ADB reported success but file was not found at: $destFile"
        return $null
    }

    $fileSize = (Get-Item $destFile).Length / 1MB
    Write-Success "Database pulled successfully. Size: $([math]::Round($fileSize, 2)) MB"

    # Return only the clean absolute path
    return $destFile
}

function Pull-Contacts {
    param([string]$DestDir)

    $destFile = [System.IO.Path]::GetFullPath((Join-Path $DestDir "wa_contacts"))
    Write-Step "Pulling WhatsApp contacts from device..."

    $raw = adb shell content query --uri $CONTACTS_REMOTE --projection "display_name:data1" 2>$null

    if ($LASTEXITCODE -ne 0) {
        Write-ErrorMsg "Failed to pull contacts."
        return $null
    }

    # Filter WhatsApp contacts on the PowerShell side
    $waContacts = $raw | Where-Object { $_ -match "@s.whatsapp.net" }
    [System.IO.File]::WriteAllLines($destFile, $waContacts)

    $count = ($waContacts | Measure-Object).Count
    Write-Success "Contacts pulled successfully. Found $count WhatsApp contacts."
    return $destFile
}

function Decrypt-MsgStore {
    param(
        [string]$CryptFile,
        [string]$Key,
        [string]$DestDir
    )

    # Resolve both paths to absolute to avoid any relative path issues
    $CryptFile = [System.IO.Path]::GetFullPath($CryptFile)
    $destFile  = [System.IO.Path]::GetFullPath((Join-Path $DestDir "msgstore.db"))

    Write-Step "Decrypting WhatsApp database..."
    Write-Host "  Input : $CryptFile" -ForegroundColor Gray
    Write-Host "  Output: $destFile"  -ForegroundColor Gray
    Write-Host "  Key   : [provided]"  -ForegroundColor Gray

    # Call wadecrypt with explicitly separated arguments
    # Using the python -m form to avoid PATH issues
    if (Test-Path $destFile) {
        Write-WarningMsg "Overwriting existing decrypted database: $destFile"
    }
    python -m wa_crypt_tools.wadecrypt $Key $CryptFile $destFile
    if ($LASTEXITCODE -ne 0) {
        Write-ErrorMsg "Decryption failed. Please check your E2E key."
        Write-Host "  You can retry manually:" -ForegroundColor Gray
        Write-Host "  python -m wa_crypt_tools.wadecrypt YOUR_KEY `"$CryptFile`" `"$destFile`"" -ForegroundColor Gray
        return $null
    }

    if (-not (Test-Path $destFile)) {
        Write-ErrorMsg "Decryption appeared to succeed but output file was not created."
        return $null
    }

    $fileSize = (Get-Item $destFile).Length / 1MB
    Write-Success "Decryption successful. Size: $([math]::Round($fileSize, 2)) MB"
    return $destFile
}

# ==============================================================================
# Summary
# ==============================================================================

function Write-Summary {
    param(
        [string]$OutputDir,
        [string]$MsgStorePath,
        [string]$ContactsPath,
        [string]$DecryptedPath
    )

    Write-Host ""
    Write-Host "======================================" -ForegroundColor Cyan
    Write-Host "  Pull Complete"                        -ForegroundColor Cyan
    Write-Host "======================================" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Output folder : $OutputDir" -ForegroundColor White

    if ($MsgStorePath) {
        Write-Host "Database      : $MsgStorePath" -ForegroundColor White
    }
    if ($DecryptedPath) {
        Write-Host "Decrypted DB  : $DecryptedPath" -ForegroundColor White
    }
    if ($ContactsPath) {
        Write-Host "Contacts      : $ContactsPath" -ForegroundColor White
    }

    Write-Host ""
    Write-Host "Next steps:"                                                  -ForegroundColor Yellow
    Write-Host "  1. Transfer the output folder to your Linux machine"        -ForegroundColor Gray
    Write-Host "  2. Transfer your WhatsApp Media folder to Linux as well"    -ForegroundColor Gray
    Write-Host "  3. Run wa_archiver.py on Linux"                             -ForegroundColor Gray
    Write-Host ""
    Write-Host "Example command:" -ForegroundColor Yellow

    # Build command hint line by line - no dynamic string construction
    Write-Host "  python wa_archiver.py \"          -ForegroundColor White

    if ($DecryptedPath) {
        Write-Host "    --msgstore /path/to/msgstore.db \" -ForegroundColor White
    }
    else {
        Write-Host "    --msgstore /path/to/msgstore.db.crypt15 \" -ForegroundColor White
        if ($E2EKey) {
            Write-Host "    --e2e $E2EKey \" -ForegroundColor White
        }
        else {
            Write-Host "    --e2e YOUR_E2E_KEY \" -ForegroundColor White
        }
    }

    Write-Host "    --wa_root /path/to/WhatsApp \"  -ForegroundColor White
    Write-Host "    --output /path/to/output \"     -ForegroundColor White
    Write-Host "    --contacts /path/to/wa_contacts \" -ForegroundColor White
    Write-Host "    --dry-run"                      -ForegroundColor White
    Write-Host ""

    if (-not $DecryptedPath) {
        Write-Host "To decrypt manually on Linux:" -ForegroundColor Yellow
        Write-Host "  wadecrypt YOUR_KEY msgstore.db.crypt15 msgstore.db" -ForegroundColor Gray
        Write-Host ""
    }
}

# ==============================================================================
# Main
# ==============================================================================

Write-Header

# --- Validate arguments ---
if ($OutputDir -match '^-') {
    Write-ErrorMsg "Invalid output directory: '$OutputDir'."
    Write-Host "  Tip: PowerShell flags use a single dash: -DecryptDB (not --DecryptDB)" -ForegroundColor Gray
    exit 1
}

# --- Validate prerequisites ---
if (-not (Test-ADB))             { exit 1 }
if (-not (Test-DeviceConnected)) { exit 1 }

if ($DecryptDB) {
    if (-not $E2EKey) {
        Write-ErrorMsg "-DecryptDB was specified but no -E2EKey was provided."
        Write-Host "  Usage: .\$(Split-Path -Leaf $PSCommandPath) -DecryptDB -E2EKey YOUR_KEY" -ForegroundColor Gray
        exit 1
    }
    if (-not (Test-WaCryptTools)) { exit 1 }
}

# --- Create output directory ---
if (-not (Test-Path $OutputDir)) {
    New-Item -ItemType Directory -Path $OutputDir | Out-Null
    Write-Success "Created output directory: $OutputDir"
}
else {
    Write-WarningMsg "Output directory already exists: $OutputDir"
}
$OutputDir = (Resolve-Path $OutputDir).Path

# --- Pull database ---
$msgStorePath = Pull-MsgStore -DestDir $OutputDir
if (-not $msgStorePath) { exit 1 }

# --- Pull contacts ---
$contactsPath = Pull-Contacts -DestDir $OutputDir
if (-not $contactsPath) { exit 1 }

# --- Optional decryption ---
$decryptedPath = $null
if ($DecryptDB) {
    $decryptedPath = Decrypt-MsgStore `
        -CryptFile $msgStorePath `
        -Key $E2EKey `
        -DestDir $OutputDir
    if (-not $decryptedPath) { exit 1 }
}

# --- Summary ---
Write-Summary `
    -OutputDir     $OutputDir `
    -MsgStorePath  $msgStorePath `
    -ContactsPath  $contactsPath `
    -DecryptedPath $decryptedPath