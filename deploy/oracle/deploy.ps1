[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-zA-Z0-9][a-zA-Z0-9.-]*$')]
    [string]$Server,
    [Parameter(Mandatory = $true)]
    [string]$KeyPath,
    [ValidatePattern('^[a-z_][a-z0-9_-]*$')]
    [string]$SshUser = 'ubuntu',
    [string]$EnvPath,
    [switch]$ValidateOnly
)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '../..')).Path
if (-not $EnvPath) { $EnvPath = Join-Path $root '.env' }
$key = (Resolve-Path -LiteralPath $KeyPath).Path
$envFile = (Resolve-Path -LiteralPath $EnvPath).Path
foreach ($command in 'ssh', 'scp', 'tar') {
    if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
        throw "Required command not found: $command"
    }
}
# Explicit allowlist: no .git, downloads, outputs, credentials, or unrelated projects.
$files = @(
    'api.py', 'MainCode_FasterWhisper.py', 'clean_with_Llama.py',
    'openrouter_transcription.py', 'url_to_mp3.py', 'requirements.txt',
    'front', 'tests', 'deploy/oracle'
)
foreach ($file in $files) {
    if (-not (Test-Path -LiteralPath (Join-Path $root $file))) {
        throw "Missing deployment file: $file"
    }
}
if ($ValidateOnly) {
    Write-Output 'Required files and commands found. No connection or upload performed.'
    Write-Output 'Release excludes .env; credentials are uploaded separately over SSH.'
    return
}

$target = "${SshUser}@${Server}"
# Confirm the host fingerprint on first connection; never disable host-key checking.
$sshOptions = @('-i', $key, '-o', 'IdentitiesOnly=yes', '-o', 'ConnectTimeout=20')
$stageOutput = & ssh @sshOptions $target 'umask 077; mktemp -d /tmp/lecturescribe-upload.XXXXXXXX'
if ($LASTEXITCODE -ne 0) { throw 'SSH connection failed. Nothing uploaded.' }
$stage = ($stageOutput | Select-Object -Last 1).Trim()
if ($stage -notmatch '^/tmp/lecturescribe-upload\.[a-zA-Z0-9]+$') {
    throw 'Unexpected staging directory from SSH.'
}
$archive = Join-Path ([System.IO.Path]::GetTempPath()) ("lecturescribe-" + [guid]::NewGuid().ToString('N') + '.tar.gz')
try {
    # A relative cwd avoids Windows tar corrupting non-ASCII project paths.
    Push-Location -LiteralPath $root
    try {
        & tar -czf $archive --exclude=__pycache__ --exclude='*.pyc' @files
        if ($LASTEXITCODE -ne 0) { throw 'Unable to build release archive.' }
    } finally {
        Pop-Location
    }
    & scp @sshOptions $archive "${target}:$stage/release.tar.gz"
    if ($LASTEXITCODE -ne 0) { throw 'Release upload failed.' }
    & scp @sshOptions $envFile "${target}:$stage/app.env"
    if ($LASTEXITCODE -ne 0) { throw 'Environment upload failed.' }
    $install = "tar -xzf $stage/release.tar.gz -C $stage deploy/oracle/install.sh && sudo -n bash $stage/deploy/oracle/install.sh $stage/release.tar.gz $stage/app.env"
    & ssh @sshOptions $target $install
    if ($LASTEXITCODE -ne 0) {
        throw 'Server installation failed. Inspect the installer output; do not switch the domain yet.'
    }
    Write-Output 'API installed and checked on the Oracle VM. Cloudflare setup is still required.'
} finally {
    # These exact staging paths were validated/created above; no workspace deletion.
    & ssh @sshOptions $target "rm -rf -- $stage"
    if (Test-Path -LiteralPath $archive) {
        Remove-Item -LiteralPath $archive
    }
}
