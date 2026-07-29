# Windows SimC Agent

## Prerequisites

Install and put the following on `PATH`:

- Python 3.11+
- Git for Windows
- CMake
- Ninja
- a C++ build toolchain compatible with the SimC CMake project (Visual Studio Build Tools / MSVC, launched from its developer shell)

Use a dedicated, writable directory for the Agent checkout and its data. Do not share its token/config directory with another Agent process.

## Configuration

Create `agent.json` in a writable location. `simc_path` must be the explicit final executable path. For automatic SimC build/update, set `simc_source_path` to a separate local source checkout directory (or an empty managed directory when the control plane requests an exact revision).

```json
{
  "server_url": "https://your-control-plane.example",
  "enrollment_token": "[REDACTED]",
  "backend_identifier": "your-windows-backend",
  "name": "windows-agent-01",
  "platform": "windows",
  "simc_path": "C:\\LMonitorSimCAgent\\bin\\simc.exe",
  "token_path": "C:\\LMonitorSimCAgent\\agent.token",
  "log_path": "C:\\LMonitorSimCAgent\\simc-agent.log",
  "simc_source_path": "C:\\LMonitorSimCAgent\\simc-source",
  "simc_compile_threads": 4,
  "auto_update_simc": true
}
```

When `token_path` and `log_path` are explicitly set as above, the completion outbox is a sibling directory named `completion-outbox`; do not delete it while there are unacknowledged terminal completions. If they are omitted, the token and log are created adjacent to the config file.

## Start

From PowerShell, preferably a Visual Studio developer PowerShell when automatic compilation is enabled:

```powershell
.\scripts\start-simc-agent.ps1 -Config C:\LMonitorSimCAgent\agent.json
```

For one claim cycle only:

```powershell
.\scripts\start-simc-agent.ps1 -Config C:\LMonitorSimCAgent\agent.json -Once
```

`start-simc-agent.cmd` is a thin wrapper around the PowerShell launcher for Task Scheduler or `cmd.exe`.

## Automatic compilation behavior

The Agent uses Git plus CMake/Ninja directly, without POSIX shell commands. It configures `BUILD_GUI=OFF`, builds target `simc`, expects `simc.exe` from the build directory on Windows, validates it with `simc.exe --version`, then atomically replaces the configured `simc_path`. It does not compile while a Run lease may be live.

On Windows, POSIX `chmod`, `fchmod`, directory `fsync`, and mode-bit consistency checks are intentionally skipped because NTFS ACLs are authoritative. On POSIX these private-mode checks and directory fsync protections remain enabled.
