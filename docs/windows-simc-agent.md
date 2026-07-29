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

Create `agent.json` in a writable location. For a **first start**, only the one-time enrollment token and the final SimC executable path are needed:

```json
{
  "enrollment_token": "[REDACTED]",
  "simc_path": "C:\\LMonitorSimCAgent\\bin\\simc.exe"
}
```

The Agent derives the remaining settings:

- `server_url`: defaults to the LMonitor control plane;
- Backend binding: comes from the one-time enrollment token; do not configure `backend_identifier` for a new Agent;
- `name`: defaults to a stable `simc-agent-<host fingerprint>` label; override it only for a custom display name;
- `platform` and `host_identifier`: automatically detected;
- `agent.token`, `simc-agent.log`, `completion-outbox/`, and managed `simc-source/`: created beside `agent.json`.

For the no-argument launcher default, name that file `simc_agent.json` beside `simc_agent_consumer.py`; `agent.json` is also valid whenever its path is passed explicitly with `-Config`.

`simc_path` is always the explicit final executable path. It may not exist for the initial automatic build. If it is an existing build output under a SimulationCraft Git checkout (for example `C:\\simulationcraft\\build\\simc.exe`), the Agent discovers that checkout and maintains it. Otherwise it creates and maintains the sibling `simc-source/` checkout, then installs the verified `simc.exe` at the configured path.

Only add optional fields when overriding a default, for example `server_url` for a private control plane, `token_path`/`log_path` for a separate state directory, or `simc_compile_threads` to cap build concurrency. Do not delete `completion-outbox/` while it contains unacknowledged terminal completions.

## Start

Put `simc_agent.json` beside `simc_agent_consumer.py`, then start without a config argument:

```powershell
.\scripts\start-simc-agent.ps1
```

The CMD wrapper has the same default and remains intended for Task Scheduler or `cmd.exe`:

```cmd
scripts\start-simc-agent.cmd
```

Use `-Config` only when the configuration is deliberately stored elsewhere:

```powershell
.\scripts\start-simc-agent.ps1 -Config C:\LMonitorSimCAgent\agent.json
```

For one claim cycle only:

```powershell
.\scripts\start-simc-agent.ps1 -Once
```

Set `"max_concurrent_runs": 2` (or another integer from 1 to 64) only to opt an Agent into parallel Run execution. The default is `1`; Agent capacity is enforced by the server's live leases as well as the local thread pool.

`start-simc-agent.cmd` is a thin wrapper around the PowerShell launcher for Task Scheduler or `cmd.exe`.

## Automatic compilation behavior

The Agent uses Git plus CMake/Ninja directly, without POSIX shell commands. It configures `BUILD_GUI=OFF`, builds target `simc`, expects `simc.exe` from the build directory on Windows, validates it by invoking `simc.exe` with no arguments and checking for its `SimulationCraft` banner, then atomically replaces the configured `simc_path`. It does not compile while a Run lease may be live.

On Windows, POSIX `chmod`, `fchmod`, directory `fsync`, and mode-bit consistency checks are intentionally skipped because NTFS ACLs are authoritative. On POSIX these private-mode checks and directory fsync protections remain enabled.
