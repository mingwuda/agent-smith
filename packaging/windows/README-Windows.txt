AgentSmith for Windows
=========================

How to run
----------
1. Double-click "Start Desktop Agent.bat".
2. Your browser should open http://127.0.0.1:8899/.
3. Open Settings in the page and configure your model provider and API key.

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
