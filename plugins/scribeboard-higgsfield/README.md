# scribeboard-higgsfield 0.4.1

Turns a story into a narrated whiteboard ("scribe") video. The pictures (scenes, a title card, optional character
sheets) and the narration are made through Higgsfield; the board, the drawing animation and the video are made
locally. You approve the work on five pages in your browser: Gate 0 start, Gate 1 script and sheets, Style (scene 1),
Gate 2 images and Gate 3 video.

## Install

You need Claude Code on Windows and a paid Higgsfield plan. Open Claude Code and ask:

> Please install the scribeboard-higgsfield plugin from https://github.com/chippwalters/scribeboard-higgsfield

Claude installs the plugin, then checks what your computer needs: Python, FFmpeg, a few Python packages, a speech
model, a picture upscaler and the Higgsfield connection. It asks before installing anything ("Do you already have
it, or should I install it for you?") and gives you the steps for anything it cannot do. At the end you restart
Claude Code once and sign in to Higgsfield.

## Instructions for Claude

When a user asks you to install this plugin:

1. Install it (the user approves the command):

   ```
   claude plugin install scribeboard-higgsfield --marketplace chippwalters/scribeboard-higgsfield
   ```

2. Find where it went: `claude plugin list --json` lists `scribeboard-higgsfield@scribeboard-higgsfield` with its `installPath`.
3. Read `<installPath>/skills/setup/SKILL.md` and follow it now, in this session, reading `${CLAUDE_PLUGIN_ROOT}`
   as `<installPath>`. It checks Python, FFmpeg, the packages, the speech model, the upscaler, the Higgsfield
   connection and Chrome; asks the user about each missing one; installs only what the user approves; and gives
   manual steps for the rest.
4. Finish with its last step: the user restarts Claude Code (that loads the plugin, the PATH changes and the
   Higgsfield server), signs in with `/mcp`, and then asks for a scribe video.

If `claude plugin install` fails, show the error and give the user the command above to run in a terminal.

## Use

Ask Claude "Make a scribe video from my story", or run `/scribeboard-higgsfield:scribe-higgsfield`, and pick your story file.
Claude follows `scripts/scribe.py status`, which always names the one next step.

## Credits

Every paid job is priced first and booked in the run's `credits.json`, with the budget you set on Gate 0 and Gate 1.
The plugin's hooks (`hooks/hooks.json`, `scripts/hf_guard.py`) book each quote, mark each paid call before it goes out
and record its job, and refuse a Higgsfield generate call that no script booked while a run is under way.

## Setup

The install runs the setup wizard; run `/scribeboard-higgsfield:setup` again any time. The wizard checks Python 3.12+, FFmpeg, the Python packages
(`requirements.txt`), the faster-whisper speech-check model, the Real-ESRGAN upscaler, the Higgsfield MCP server and
Chrome; it asks before installing anything and writes this machine's paths to the per-user `settings.json`
(`%LOCALAPPDATA%\scribe-higgsfield` on Windows), which overrides `scribe-settings.json` and survives plugin updates.
`scripts/install.py check` shows the state at any time.
