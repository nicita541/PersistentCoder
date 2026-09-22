# Typed tools and the future host runner

The Coding Agent cannot submit arbitrary command strings.  Its edit envelope may
contain only typed calls from the allowlist in
`app/agent/coder/tool_protocol.py`.  Trusted application code validates paths and
options and converts a call to a fixed argument vector.  The executor requires a
`run_argv` implementation and has no fallback to a string or host shell.

The current project runner is the Docker sandbox.  This keeps model-selected
Python code away from the host even when that code itself tries to access files
outside the mounted project.

## Future direct-on-PC mode

`ProjectToolRunner` is the seam for a future non-Docker implementation.  Such an
implementation must be fail-closed and give the child process operating-system
access only to the selected project directory (plus a minimal read-only runtime).
It must also preserve argv-only process creation, time/output limits,
cancellation, and network denial.

Changing the child process working directory or checking paths before launch is
not sufficient: arbitrary Python code can open an absolute path after it starts.
For that reason direct host execution remains disabled until an OS-level boundary
(for example a restricted Windows token plus explicit filesystem ACLs or an
equivalent isolated worker) is implemented and tested.
