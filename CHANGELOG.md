# Changelog

## 0.4.1 (2026-11-12)

- **Licensed under GPL-3.0-or-later.**
- The plugin no longer ships any font files or the maintainer script that drew the palette cards; the cards ship as finished pictures.

## 0.4.0 (2026-11-12)

- **One-sentence install.** Ask Claude Code to install the plugin from its GitHub address; it installs the plugin and runs the setup in the same session.
- **Setup wizard** (`/scribeboard-higgsfield:setup`). It checks Python 3.12+, FFmpeg, the Python packages, the speech-check model, the picture upscaler, the Higgsfield connection and Chrome. For each missing one it asks "Do you already have it, or should I install it for you?", installs only what you approve, and gives step-by-step instructions for the rest.
- **Renamed** from scribe-higgsfield to scribeboard-higgsfield, to match the repository.

## 0.3.0 (2026-11-12)

- **Write-ahead credit ledger.** Every paid job is priced exactly, booked, and marked as started before it goes out, so a job interrupted halfway is checked, never paid for twice. The budget cap is checked against the exact price.
- **Credit guard hooks.** They refuse any Higgsfield picture or audio call that the scripts did not book while a video is being made.
- **Gate 3** shows the full video, the board and one clip per scene, cut on the narration so every sentence plays whole. Edits name what to fix (camera, narration or picture), and each goes to its own fix.
- **Narration:** a padded retake is checked uncut when the voice drops the throwaway word; rejected takes can be checked again for free; trial spellings fix a word said wrong.
- **Retries** carry every reviewer fix from the round, not only the latest.
- **Character sheets** stop after three failed reviews in a row and go to the user.

## 0.2.0

- First plugin build: the scribe-higgsfield skill, its scripts and the approval pages packaged for Claude Code.
