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

On the **first interactive start**, do not create a configuration file manually. Start the Agent and enter the two required values when prompted:

1. one-time `enrollment_token`;
2. final `simc_path` (for example `C:\\LMonitorSimCAgent\\bin\\simc.exe`).

The Agent atomically creates `simc_agent.json` beside `simc_agent_consumer.py` with private permissions, then starts registration. This prompt appears only when that configuration file is absent; unattended Task Scheduler/service starts fail closed instead of waiting for input.

The equivalent minimal file is:
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

Only add optional fields when overriding a default. The first interactive start writes only the two required fields; `simc_agent.example.json` is the complete field reference:

| Field | Default | When to change it |
|---|---|---|
| `server_url` | `https://wowdaily.cn` | Use a private control-plane address. HTTPS is required unless `allow_insecure_http` is explicitly enabled for a local test environment. |
| `backend_identifier` | Empty | Legacy compatibility only; a new Agent is bound by its one-time enrollment token and should leave this empty. |
| `name` | Stable `simc-agent-<host fingerprint>` | Set a human-readable display name for the Dashboard. |
| `max_concurrent_runs` | `1` | Explicitly opt in to 2–64 parallel Runs; the server also enforces this live-lease limit. |
| `poll_interval_seconds` | `5` | Change idle queue polling frequency. |
| `request_timeout_seconds` | `30` | Change individual control-plane HTTP request timeout. |
| `max_run_seconds` | `7200` | Cap one SimC Run before it is marked timed out. |
| `auto_update` | `true` | Disable only when Agent code updates are managed externally. |
| `repository_path` | Agent checkout | Point to the dedicated LMonitor checkout used for Agent self-update. |
| `auto_update_simc` | `true` | Disable automatic SimC source maintenance/builds. |
| `simc_source_path` | Existing checkout above `simc_path`, otherwise sibling `simc-source/` | Use a specific SimulationCraft source checkout. |
| `simc_update_interval_seconds` | `1800` | Change automatic SimC maintenance interval. |
| `simc_compile_threads` | `2` | Cap CMake/Ninja build concurrency, from 1 to 64. |
| `token_path` | Beside the config as `*.token` | Store the long-lived Agent token in a separate protected state directory. |
| `log_path` | Beside the config as `*.log` | Store rotating Agent logs elsewhere. |
| `platform`, `host_identifier` | Auto-detected | Only use for controlled diagnostics; `host_identifier` must be 32–128 lowercase hex characters. |
| `allow_insecure_http` | `false` | Local development only; never enable over an untrusted network. |

Do not delete `completion-outbox/` while it contains unacknowledged terminal completions.

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

`start-simc-agent.cmd` is a thin wrapper around the PowerShell launcher for Task Scheduler or `cmd.exe`. The PowerShell launcher is the process supervisor: an unexpected non-zero Agent exit is restarted after five seconds, while `-Once` preserves the exact one-cycle exit code. Agent code self-update uses `execv` and continues in-process, so it does not depend on Task Scheduler detecting an exit.

## Automatic compilation behavior

The Agent uses Git plus CMake/Ninja directly, without POSIX shell commands. It configures `BUILD_GUI=OFF`, builds target `simc`, expects `simc.exe` from the build directory on Windows, validates it by invoking `simc.exe` with no arguments and checking for its `SimulationCraft` banner, then atomically replaces the configured `simc_path`. It does not compile while a Run lease may be live.

On Windows, POSIX `chmod`, `fchmod`, directory `fsync`, and mode-bit consistency checks are intentionally skipped because NTFS ACLs are authoritative. On POSIX these private-mode checks and directory fsync protections remain enabled.
