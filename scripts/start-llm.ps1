# Starts the local model server (llama.cpp's llama-server, Vulkan backend) with an
# OpenAI-compatible API at http://127.0.0.1:8090/v1.
#
#   scripts\start-llm.ps1                      # Qwen3.5 9B, 16k context, this machine only
#   scripts\start-llm.ps1 -Context 32768       # larger context (uses more video memory)
#   scripts\start-llm.ps1 -Expose              # also reachable from the cluster (host.docker.internal)
#
# Binaries and models live outside the repository (LLM_HOME, default D:\llm):
#   %LLM_HOME%\llama.cpp\<build>\llama-server.exe   and   %LLM_HOME%\models\*.gguf
param(
    [string]$Model = "Qwen_Qwen3.5-9B-Q6_K.gguf",
    [string]$Alias = "qwen3.5-9b",
    [int]$Port = 8090,
    [int]$Context = 16384,
    [string]$Build = "b11429",
    # The discrete card. `llama-server --list-devices` also lists the integrated GPU (Vulkan1):
    # without this, layers could be split across both.
    [string]$Device = "Vulkan0",
    [switch]$Expose
)

$llmHome = if ($env:LLM_HOME) { $env:LLM_HOME } else { "D:\llm" }
$server = Join-Path $llmHome "llama.cpp\$Build\llama-server.exe"
$modelPath = Join-Path $llmHome "models\$Model"
foreach ($path in @($server, $modelPath)) {
    if (-not (Test-Path $path)) { Write-Error "Not found: $path"; exit 1 }
}

$bindAddress = if ($Expose) { "0.0.0.0" } else { "127.0.0.1" }

# -ngl 99: every layer on the GPU.  --jinja: the model's own chat template, needed for tool calls.
& $server -m $modelPath --alias $Alias --host $bindAddress --port $Port `
    -c $Context -ngl 99 --device $Device --jinja
