Desktop Agent for Windows
=========================

This package is fully self-contained. It bundles the web UI, the agent engine,
and all built-in skills — no Python install is required on the target machine.

How to run
----------
1. Double-click "Start Desktop Agent.bat".
2. Your browser should open http://127.0.0.1:8899/.
3. Open Settings in the page and configure your model provider and API key.

What's included
---------------
  DesktopAgent.exe   - the agent server
  desktop/           - web UI (already bundled)
  skills/            - built-in agent skills (already bundled)
  AGENTS.md          - agent operating guidance (already bundled)

Data location
-------------
Settings, sessions, usage logs, and workspace files are stored under:
  %USERPROFILE%\.desktop_agent
  %USERPROFILE%\agent_workspace

If Windows Defender or SmartScreen warns about the executable, choose "More info"
and then "Run anyway" for locally built packages.

Troubleshooting
---------------
If the browser does not open automatically, keep the console window running and
open this address manually:
  http://127.0.0.1:8899/

If port 8899 is already occupied, edit "Start Desktop Agent.bat" and change:
  set AGENT_PORT=8899

