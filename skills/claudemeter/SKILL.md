---
name: claudemeter
description: Launch the ClaudeMeter dashboard to monitor Claude Code token usage and costs in real time. Use this when the user wants to see their Claude usage, token counts, costs, or spending.
allowed-tools: Bash
---

Launch the ClaudeMeter live terminal dashboard.

## Steps

1. Check if `claudemeter` is installed:
   ```bash
   which claudemeter 2>/dev/null
   ```

2. If not installed, try to install it. Prefer `pipx` (isolated, always on PATH) over `pip`:
   ```bash
   if command -v pipx &>/dev/null; then
     pipx install git+https://github.com/mohsinahad/claudemeter.git
   else
     pip install --user git+https://github.com/mohsinahad/claudemeter.git
   fi
   ```
   If installation fails, tell the user to run one of the above commands manually in their terminal and then run `claudemeter` themselves.

3. Launch the dashboard. Since claudemeter is a live TUI it must run in a real terminal, not inside Claude's tool executor. Use the best available method:

   **macOS** — open a new window in whichever terminal app is running:
   ```bash
   if command -v osascript &>/dev/null; then
     # Try iTerm2 first, fall back to Terminal.app
     osascript <<'SCRIPT'
       tell application "System Events"
         set frontApp to name of first application process whose frontmost is true
       end tell
       if frontApp is "iTerm2" then
         tell application "iTerm2"
           create window with default profile command "claudemeter"
         end tell
       else
         tell application "Terminal" to do script "claudemeter"
       end if
   SCRIPT
   fi
   ```

   **Linux** — print instructions instead of blocking:
   ```bash
   echo "Run 'claudemeter' in your terminal to launch the dashboard."
   ```

4. Tell the user the dashboard is launching (or give them the command to run manually if the auto-launch didn't work).

## Notes
- No API key or config required — reads directly from `~/.claude/`
- Press `q` or `Ctrl+C` to exit the dashboard
- If the install step puts `claudemeter` in `~/.local/bin` and it's not on PATH, tell the user to add `export PATH="$HOME/.local/bin:$PATH"` to their shell profile
