$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$configPath = Join-Path $root ".neon_rift.config.json"
$port = if ($env:PORT) { [int]$env:PORT } else { 8080 }
$url = "http://localhost:$port/codex_asteroids.html"

$debugMode = $args -contains "--debug"
$forceRestart = $args -contains "--restart" -or $debugMode
$setupMode = $args -contains "--setup"

function Get-ArgValue([string]$name) {
    for ($i = 0; $i -lt $args.Count; $i++) {
        if ($args[$i] -eq $name -and $i + 1 -lt $args.Count) {
            return [string]$args[$i + 1]
        }
    }
    return $null
}

function Test-HttpAlive([string]$probeUrl) {
    try {
        $r = Invoke-WebRequest -Uri $probeUrl -UseBasicParsing -TimeoutSec 1
        return ($r.StatusCode -ge 200 -and $r.StatusCode -lt 500)
    } catch {
        return $false
    }
}

function Normalize-BaseUrl([string]$rawUrl) {
    if ([string]::IsNullOrWhiteSpace($rawUrl)) { return $null }
    try {
        $u = [Uri]$rawUrl
        if (-not $u.Scheme) { return $null }
        $p = if ($u.IsDefaultPort) { "" } else { ":$($u.Port)" }
        $path = $u.AbsolutePath.TrimEnd("/")
        if ($path -eq "/") { $path = "" }
        return "{0}://{1}{2}{3}" -f $u.Scheme, $u.Host, $p, $path
    } catch {
        return $null
    }
}

function Resolve-LlamaRoot([string]$baseRoot, [string]$hintRoot) {
    $candidates = @()
    if ($hintRoot) { $candidates += $hintRoot }
    if ($env:LLAMA_HOME) { $candidates += $env:LLAMA_HOME }
    $candidates += (Join-Path $baseRoot "llama")
    $candidates += (Join-Path $baseRoot "tools\\llama")
    $candidates += (Join-Path (Join-Path $baseRoot "..") "z-ai_chatbot_free\\local_models\\llama")
    $candidates += (Join-Path (Join-Path $baseRoot "..") "local_models\\llama")

    foreach ($candidate in $candidates) {
        if ([string]::IsNullOrWhiteSpace($candidate)) { continue }
        $full = [System.IO.Path]::GetFullPath($candidate)
        if (Test-Path -LiteralPath $full) {
            return $full
        }
    }

    return $null
}

function Resolve-LlamaExe([string]$llamaRoot) {
    if (-not $llamaRoot) { return $null }
    $exeCandidates = @(
        (Join-Path $llamaRoot "llama-server.exe"),
        (Join-Path $llamaRoot "bin\\llama-server.exe"),
        (Join-Path $llamaRoot "build\\bin\\Release\\llama-server.exe"),
        (Join-Path $llamaRoot "build\\bin\\llama-server.exe")
    )
    foreach ($exe in $exeCandidates) {
        if (Test-Path -LiteralPath $exe) {
            return [System.IO.Path]::GetFullPath($exe)
        }
    }
    return $null
}

function Resolve-GgufModel([string]$llamaRoot, [string]$hintModel) {
    if ($hintModel) {
        $fullHint = [System.IO.Path]::GetFullPath($hintModel)
        if (Test-Path -LiteralPath $fullHint) { return $fullHint }
    }

    if (-not $llamaRoot) { return $null }
    $modelsRoot = Join-Path $llamaRoot "models"
    $priority = @(
        "Qwen3.5-4B-UD-Q4_K_XL.gguf",
        "Qwen3.5-2B-Q4_K_M.gguf",
        "Qwen2.5-1.5B-Instruct-Q4_K_M.gguf",
        "Qwen3.5-0.8B-Q4_K_M.gguf"
    )

    $searchRoots = @()
    if (Test-Path -LiteralPath $modelsRoot) { $searchRoots += $modelsRoot }
    $searchRoots += $llamaRoot

    foreach ($searchRoot in $searchRoots) {
        foreach ($name in $priority) {
            $candidate = Join-Path $searchRoot $name
            if (Test-Path -LiteralPath $candidate) {
                return [System.IO.Path]::GetFullPath($candidate)
            }
        }
    }

    foreach ($searchRoot in $searchRoots) {
        $found = Get-ChildItem -LiteralPath $searchRoot -Filter "*.gguf" -File -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($found) {
            return $found.FullName
        }
    }

    return $null
}

function Load-Config([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) {
        return @{}
    }
    try {
        $obj = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
        return @{
            backend = [string]$obj.backend
            ollamaBaseUrl = [string]$obj.ollamaBaseUrl
            llamaRoot = [string]$obj.llamaRoot
            llamaModel = [string]$obj.llamaModel
            llamaPort = [int]($obj.llamaPort)
        }
    } catch {
        Write-Host "Config non valida, ignoro: $path"
        return @{}
    }
}

function Save-Config([string]$path, [hashtable]$cfg) {
    $obj = [ordered]@{
        backend = $cfg.backend
        ollamaBaseUrl = $cfg.ollamaBaseUrl
        llamaRoot = $cfg.llamaRoot
        llamaModel = $cfg.llamaModel
        llamaPort = $cfg.llamaPort
    }
    ($obj | ConvertTo-Json -Depth 8) | Set-Content -LiteralPath $path -Encoding UTF8
}

function Test-OllamaBaseUrl([string]$baseUrl) {
    $normalized = Normalize-BaseUrl $baseUrl
    if (-not $normalized) { return $false }
    return (Test-HttpAlive -probeUrl "$normalized/api/tags")
}

function Start-LlamaServer([string]$exePath, [string]$modelPath, [int]$listenPort, [bool]$debugOpen) {
    if (Test-HttpAlive -probeUrl "http://127.0.0.1:$listenPort/health") {
        return
    }
    $argsList = @("-m", "`"$modelPath`"", "--host", "127.0.0.1", "--port", "$listenPort")
    if ($debugOpen) {
        $cmd = "title llama.cpp Server && cd /d `"$([System.IO.Path]::GetDirectoryName($exePath))`" && `"$exePath`" $($argsList -join ' ')"
        Start-Process -FilePath "cmd.exe" -ArgumentList "/k", $cmd -WindowStyle Normal
    } else {
        Start-Process -FilePath $exePath -ArgumentList ($argsList -join ' ') -WindowStyle Hidden
    }
    Start-Sleep -Milliseconds 600
}

function Invoke-SetupWizard([hashtable]$seedCfg, [bool]$showEntryMenu = $false) {
    if ($showEntryMenu) {
        Write-Host ""
        Write-Host "=== Neon Rift Setup ==="
        Write-Host "1) Usa ultima configurazione"
        Write-Host "2) Modifica configurazione (wizard)"
        $entryChoice = Read-Host "Scelta [1-2] (default 1)"
        if ([string]::IsNullOrWhiteSpace($entryChoice) -or $entryChoice -eq "1") {
            return $seedCfg
        }
    }

    Write-Host ""
    Write-Host "=== Neon Rift LLM Setup ==="
    Write-Host "1) auto"
    Write-Host "2) ollama"
    Write-Host "3) llamacpp"

    $choice = Read-Host "Scegli backend [1-3] (default 1)"
    $backend = "auto"
    if ($choice -eq "2") { $backend = "ollama" }
    if ($choice -eq "3") { $backend = "llamacpp" }

    $cfg = @{}
    $cfg.backend = $backend
    $cfg.llamaPort = if ($seedCfg.llamaPort -and $seedCfg.llamaPort -gt 0) { [int]$seedCfg.llamaPort } else { 8091 }

    if ($backend -eq "ollama" -or $backend -eq "auto") {
        $defaultOk = Test-OllamaBaseUrl "http://127.0.0.1:11434"
        $defaultMsg = if ($defaultOk) { "OK" } else { "non raggiungibile" }
        Write-Host "Ollama default (http://127.0.0.1:11434): $defaultMsg"
        $custom = Read-Host "Endpoint Ollama personalizzato (invio = usa default)"
        if ([string]::IsNullOrWhiteSpace($custom)) {
            $cfg.ollamaBaseUrl = "http://127.0.0.1:11434"
        } else {
            $cfg.ollamaBaseUrl = $custom.Trim()
        }
    }

    if ($backend -eq "llamacpp" -or $backend -eq "auto") {
        $llamaRootGuess = Resolve-LlamaRoot -baseRoot $root -hintRoot $seedCfg.llamaRoot
        $rootPrompt = Read-Host "Cartella llama.cpp (invio = auto: $llamaRootGuess)"
        $cfg.llamaRoot = if ([string]::IsNullOrWhiteSpace($rootPrompt)) { $llamaRootGuess } else { $rootPrompt.Trim() }

        $modelGuess = Resolve-GgufModel -llamaRoot $cfg.llamaRoot -hintModel $seedCfg.llamaModel
        $modelPrompt = Read-Host "Modello GGUF (invio = auto: $modelGuess)"
        $cfg.llamaModel = if ([string]::IsNullOrWhiteSpace($modelPrompt)) { $modelGuess } else { $modelPrompt.Trim() }

        $portPrompt = Read-Host "Porta llama.cpp (invio = $($cfg.llamaPort))"
        if (-not [string]::IsNullOrWhiteSpace($portPrompt)) {
            $parsed = 0
            if ([int]::TryParse($portPrompt, [ref]$parsed) -and $parsed -gt 0) {
                $cfg.llamaPort = $parsed
            }
        }
    }

    return $cfg
}

function Resolve-Backend([hashtable]$baseCfg, [bool]$interactiveFallback) {
    $resolved = @{}
    $backend = if ($baseCfg.backend) { $baseCfg.backend.ToLowerInvariant() } else { "auto" }

    $ollamaUrl = Normalize-BaseUrl $baseCfg.ollamaBaseUrl
    if (-not $ollamaUrl) {
        $ollamaUrl = "http://127.0.0.1:11434"
    }

    $llamaPort = if ($baseCfg.llamaPort -and [int]$baseCfg.llamaPort -gt 0) { [int]$baseCfg.llamaPort } else { 8091 }
    $llamaRoot = Resolve-LlamaRoot -baseRoot $root -hintRoot $baseCfg.llamaRoot
    $llamaExe = Resolve-LlamaExe -llamaRoot $llamaRoot
    $llamaModel = Resolve-GgufModel -llamaRoot $llamaRoot -hintModel $baseCfg.llamaModel

    $ollamaOk = Test-OllamaBaseUrl $ollamaUrl
    $llamaReady = ($llamaExe -and $llamaModel)

    if ($backend -eq "ollama" -and $ollamaOk) {
        $resolved.backend = "ollama"
        $resolved.ollamaBaseUrl = $ollamaUrl
        return $resolved
    }

    if ($backend -eq "llamacpp" -and $llamaReady) {
        Start-LlamaServer -exePath $llamaExe -modelPath $llamaModel -listenPort $llamaPort -debugOpen:$debugMode
        $resolved.backend = "llamacpp"
        $resolved.llamaRoot = $llamaRoot
        $resolved.llamaModel = $llamaModel
        $resolved.llamaPort = $llamaPort
        return $resolved
    }

    if ($backend -eq "auto") {
        if ($llamaReady) {
            Start-LlamaServer -exePath $llamaExe -modelPath $llamaModel -listenPort $llamaPort -debugOpen:$debugMode
            $resolved.backend = "llamacpp"
            $resolved.llamaRoot = $llamaRoot
            $resolved.llamaModel = $llamaModel
            $resolved.llamaPort = $llamaPort
            return $resolved
        }
        if ($ollamaOk) {
            $resolved.backend = "ollama"
            $resolved.ollamaBaseUrl = $ollamaUrl
            return $resolved
        }
    }

    if ($interactiveFallback) {
        $wizardCfg = Invoke-SetupWizard -seedCfg $baseCfg -showEntryMenu $false
        return (Resolve-Backend -baseCfg $wizardCfg -interactiveFallback:$false)
    }

    return $null
}

$loadedCfg = Load-Config -path $configPath

$cliBackend = Get-ArgValue "--backend"
$cliOllamaUrl = Get-ArgValue "--ollama-url"
$cliLlamaHome = Get-ArgValue "--llama-home"
$cliModel = Get-ArgValue "--model"
$cliLlamaPort = Get-ArgValue "--llama-port"

$effectiveCfg = @{}
$effectiveCfg.backend = if ($cliBackend) {
    $cliBackend
} elseif ($env:NEON_RIFT_LLM_BACKEND) {
    $env:NEON_RIFT_LLM_BACKEND
} elseif ($loadedCfg.backend) {
    $loadedCfg.backend
} else {
    "auto"
}

$effectiveCfg.ollamaBaseUrl = if ($cliOllamaUrl) {
    $cliOllamaUrl
} elseif ($env:OLLAMA_BASE_URL) {
    $env:OLLAMA_BASE_URL
} elseif ($loadedCfg.ollamaBaseUrl) {
    $loadedCfg.ollamaBaseUrl
} else {
    "http://127.0.0.1:11434"
}

$effectiveCfg.llamaRoot = if ($cliLlamaHome) {
    $cliLlamaHome
} elseif ($env:LLAMA_HOME) {
    $env:LLAMA_HOME
} elseif ($loadedCfg.llamaRoot) {
    $loadedCfg.llamaRoot
} else {
    $null
}

$effectiveCfg.llamaModel = if ($cliModel) {
    $cliModel
} elseif ($env:LLAMA_CPP_MODEL_PATH) {
    $env:LLAMA_CPP_MODEL_PATH
} elseif ($loadedCfg.llamaModel) {
    $loadedCfg.llamaModel
} else {
    $null
}

$effectiveCfg.llamaPort = if ($cliLlamaPort) {
    [int]$cliLlamaPort
} elseif ($env:LLAMA_CPP_PORT) {
    [int]$env:LLAMA_CPP_PORT
} elseif ($loadedCfg.llamaPort) {
    [int]$loadedCfg.llamaPort
} else {
    8091
}

if ($setupMode) {
    $effectiveCfg = Invoke-SetupWizard -seedCfg $effectiveCfg -showEntryMenu $true
}

$resolved = Resolve-Backend -baseCfg $effectiveCfg -interactiveFallback:$true
if (-not $resolved) {
    throw "Nessun backend LLM disponibile. Avvia con --setup per configurazione guidata."
}

if ($resolved.backend -eq "llamacpp") {
    $env:NEON_RIFT_LLM_BACKEND = "llamacpp"
    $env:LLAMA_CPP_PORT = "$($resolved.llamaPort)"
    $env:LLAMA_CPP_MODEL_NAME = [System.IO.Path]::GetFileName($resolved.llamaModel)
    $env:LLAMA_CPP_MODEL_PATH = $resolved.llamaModel
    $env:LLAMA_HOME = $resolved.llamaRoot
    Write-Host "LLM backend: llama.cpp ($($env:LLAMA_CPP_MODEL_NAME)) su :$($resolved.llamaPort)"
} else {
    $env:NEON_RIFT_LLM_BACKEND = "ollama"
    $env:OLLAMA_BASE_URL = $resolved.ollamaBaseUrl
    Write-Host "LLM backend: Ollama ($($env:OLLAMA_BASE_URL))"
}

$persistCfg = @{
    backend = $resolved.backend
    ollamaBaseUrl = if ($resolved.ollamaBaseUrl) { $resolved.ollamaBaseUrl } else { $effectiveCfg.ollamaBaseUrl }
    llamaRoot = if ($resolved.llamaRoot) { $resolved.llamaRoot } else { $effectiveCfg.llamaRoot }
    llamaModel = if ($resolved.llamaModel) { $resolved.llamaModel } else { $effectiveCfg.llamaModel }
    llamaPort = if ($resolved.llamaPort) { $resolved.llamaPort } else { $effectiveCfg.llamaPort }
}
Save-Config -path $configPath -cfg $persistCfg

if ($forceRestart) {
    if (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue) {
        Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique |
            ForEach-Object {
                try { Stop-Process -Id $_ -Force -ErrorAction Stop } catch {}
            }
    } else {
        netstat -ano | Select-String ":$port\s+.*LISTENING\s+(\d+)" | ForEach-Object {
            if ($_.Matches.Count -gt 0) {
                try { Stop-Process -Id ([int]$_.Matches[0].Groups[1].Value) -Force -ErrorAction Stop } catch {}
            }
        }
    }
    Start-Sleep -Milliseconds 300
}

$node = Get-Command node -ErrorAction SilentlyContinue
if ($node) {
    $serverScript = Join-Path $root "neon_rift_server.js"
    $alreadyRunning = $false

    try {
        $response = Invoke-WebRequest -Uri "http://localhost:$port/" -UseBasicParsing -TimeoutSec 1
        $alreadyRunning = $response.StatusCode -eq 200
    } catch {
        $alreadyRunning = $false
    }

    if (-not $alreadyRunning) {
        $serverArgs = if ($debugMode) { "`"$serverScript`" --debug" } else { "`"$serverScript`"" }
        if ($debugMode) {
            $cmd = "title Neon Rift Debug Server && cd /d `"$root`" && `"$($node.Source)`" $serverArgs"
            Start-Process -FilePath "cmd.exe" -ArgumentList "/k", $cmd -WorkingDirectory $root -WindowStyle Normal
        } else {
            Start-Process -FilePath $node.Source -ArgumentList $serverArgs -WorkingDirectory $root -WindowStyle Hidden
        }
        Start-Sleep -Milliseconds 700
    }

    Start-Process $url
    Write-Host "Neon Rift avviato: $url"
    exit 0
}

throw "Node.js non trovato nel PATH. Questo progetto richiede il server Node (neon_rift_server.js)."
