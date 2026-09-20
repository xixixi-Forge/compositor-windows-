# compositor-linux

A Qt 6 / C++20 image editor: a Linux port of the macOS app Compositor.

`CLAUDE.md` holds the full project instructions — build and test commands, how
to drive the app headless, and the coding conventions. Read that first; this
file exists so agents that look for `AGENTS.md` (opencode, pi, and others)
know how the Windows headless hang is handled.

## Windows: the DBus autolaunch hang is fixed in the program

The app links `Qt6DBus.dll` (for the XDG portal colour scheme). With no session
bus address set, libdbus used to autolaunch `dbus-daemon` via `dbus-launch`;
that daemon inherits the caller's stdout pipe and outlives the process, so a
caller waiting for EOF on the pipe never returns even though the app has
exited. Agent tool runners hung exactly this way, with no error anywhere.

`main()` now sets `DBUS_SESSION_BUS_ADDRESS=disabled:` itself on Windows when no
address is configured, before `QApplication` is constructed, so libdbus never
autolaunches a daemon. A genuinely configured bus is left untouched, and other
platforms are unaffected. Nothing needs to be set from the outside:

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
$p = Start-Process build-win/src/app/compositor-linux.exe `
       -ArgumentList '--headless','--rpc-socket','smoke','--demo' -PassThru
python tools/rpc_smoke.py smoke
Stop-Process -Id $p.Id -Force
```

Verified against `build-win` with `tools/rpc_smoke.py`, with
`DBUS_SESSION_BUS_ADDRESS` unset: the app runs normally, the RPC socket
listens, the process returns promptly, and no `dbus-daemon.exe` appears.
