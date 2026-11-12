---
name: setup
description: "The scribeboard-higgsfield setup wizard: checks Python, FFmpeg, the Python packages, the speech-check model, the picture upscaler, the Higgsfield connection and Chrome, asks the user about each missing one (do you already have it, or should I install it?), installs only what the user approves and gives manual steps for the rest. Use for \"set up scribeboard-higgsfield\", \"install the scribe plugin\", \"scribe setup\", or when the scribe-higgsfield skill says setup is incomplete."
---

# Setup wizard (scribeboard-higgsfield)

You walk the user through setting up this plugin. Assume they know nothing about Python or FFmpeg: every question says plainly what you will install, how big it is and what changes on their computer. Ask every question in chat with the AskUserQuestion tool. Nothing is installed until the user says yes, and `install.py install` refuses without that answer. Anything you cannot do, you hand to the user as numbered manual steps.

**Where the plugin is.** Run as a skill, `${CLAUDE_PLUGIN_ROOT}` is this plugin's folder. Read straight after `claude plugin install` (before a restart, when the skill is not loaded yet), `${CLAUDE_PLUGIN_ROOT}` is the `installPath` that `claude plugin list --json` shows for `scribeboard-higgsfield@scribeboard-higgsfield`: the folder two levels above this file. `$S` = `${CLAUDE_PLUGIN_ROOT}/scripts`.

Start by telling the user, in two sentences: you will check seven things the plugin needs and ask before installing anything; at the end they restart Claude Code once.

## Step 1. Python (before any script)

Run `python -c "import sys; print(sys.version_info[0], sys.version_info[1], sys.executable)"`.

- **3.12 or newer:** `$PY0` = `python`. Go to step 2.
- **Anything else** (an error; nothing printed, which on Windows is the Microsoft Store stub; or an older version): on Windows, try `"%LOCALAPPDATA%\Programs\Python\Python312\python.exe"` the same way. If that runs, Python is installed but this session cannot see it: use that full path as `$PY0` and go to step 2 (the restart at the end fixes the PATH).
- **Not found:** ask with AskUserQuestion:
  - header `Python`
  - question: "I couldn't find Python 3.12 or newer on this computer. Do you already have it, or should I install it for you?"
  - "Install it for me (Recommended)": "I'll run winget to install Python 3.12 for your user only (about 30 MB) and add it to your PATH."
  - "I already have it": "Tell me where python.exe is (its folder or full path). I'll check it and add that folder to your PATH."
  - "I'll install it myself": "I'll give you the steps."

  **Install it for me** (Windows): run exactly

  ```
  winget install --exact --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements --override "/quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1"
  ```

  then `$PY0` = `"%LOCALAPPDATA%\Programs\Python\Python312\python.exe"`.

  **I already have it:** ask where (the AskUserQuestion "Other" answer, or a follow-up question). Run it the same way to check it is 3.12+. Then `$PY0` = that path, and in step 4 run `install --item python --choice path --path "<that path>"` to add it to the PATH.

  **I'll install it myself**, no winget, a failed winget, or macOS/Linux: give these steps and stop until the user says it is done:
  1. Download Python 3.12 from https://www.python.org/downloads/
  2. Run the installer with **Add python.exe to PATH** ticked.
  3. Restart Claude Code and ask me to continue the scribeboard-higgsfield setup.

## Step 2. Check everything

Run `$PY0 "$S/install.py" check`. It prints `CHECK_JSON` with seven items. Show the user one table, in the items' order:

| Item | Status | Details |
|---|---|---|

Status: ✓ ready, ✗ missing, ✗ not usable (state `broken`), ↻ ready after restart (state `restart`), or ○ your choice (state `choice`: an optional item that was found). Under the table, one short line for each item that is not ready, from its `why`.

If `ok` is true, go to step 6.

## Step 3. Ask

Every item whose `ask` is not null gets one question, in the items' order. Put up to four in one AskUserQuestion call. Use `ask.header`, `ask.question` and each option's `label` and `description` exactly as printed: do not add, drop or reword options, and do not ask about items that are ready. Note each answer's `choice`. For a `path` answer ("I already have it"), get the location from the user before step 4.

## Step 4. Install what the user approved

For each answer other than `skip`, in the items' order:

```
$PY0 "$S/install.py" install --item ID --choice CHOICE --approved "LABEL OF THE OPTION THE USER PICKED" [--path "WHERE THE USER SAID"]
```

Before each one, tell the user what is starting and how long it takes: the packages take a few minutes, and the speech model is a 1.5 GB download. Give long ones a long timeout or run them in the background; never cut one short.

- `INSTALL_JSON`: say what was done in one line. Pass on any `next` line.
- `FAILED <reason>`: show the reason in full, then give that item's `manual` steps as a numbered list. Skip the items that depend on it (the speech model needs the packages) and carry on with the rest.
- A `skip` answer: give that item's `manual` steps as a numbered list. Never run a fix for it.

Do not stop for a restart in the middle: finish every item you can first.

## Step 5. Check again

Run `$PY0 "$S/install.py" check` again and show the table again. Items still ✗ get their `manual` steps.

## Step 6. Higgsfield sign-in

If the `mcp__higgsfield__balance` tool exists in this session, call it. Credits back: tell the user their balance. If it asks for authentication: tell the user to run `/mcp`, pick **higgsfield** and sign in, then call it again. A plan without MCP access cannot make pictures; say so if the balance call reports that.

If the tool does not exist yet (the server was just added), sign-in happens after the restart.

## Step 7. Finish

If every item is ✓ or ↻ (or the user chose to do the rest themselves), end with this, leaving out the lines that do not apply:

1. **Restart Claude Code** (quit and start it again). This loads the plugin and anything added to your PATH.
2. **Sign in to Higgsfield:** run `/mcp`, pick **higgsfield** and sign in.
3. **Make a video:** ask me "Make a scribe video from my story", or run `/scribeboard-higgsfield:scribe-higgsfield`. I check the setup once more before anything is paid for.

When the setup runs as a skill with nothing left to restart, also run the final checks: `$PY` = what `$PY0 "$S/settings.py" python` prints; `$PY "$S/render_scenes.py" doctor --style rsa_animate` must end `all prerequisites present` and `$PY "$S/narrate.py" doctor` must end `"ok": true`.

## Rules

- Never install, download or change anything the user did not approve in this wizard.
- Never reword a question to make an install sound smaller or safer than its option says.
- Show every failure in full, with the manual steps for that item. Never hide or silently retry one.
- The plugin's credit guard calls `python`: until `python` on PATH is Python 3.12+, setup is not done.
