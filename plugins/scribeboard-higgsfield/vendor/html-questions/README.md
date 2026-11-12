# html-questions

Ask a batch of questions on a local web page instead of in chat. You pick, tick and type on the page, click **Copy answers**, and paste the result back into chat. The agent checks it and acts on all of it at once.

You don't need MD DOCS, a server, or an internet connection. Each page is a single `.html` file that opens from disk in Chrome.

Use it as `/html-questions` from any repo. The agent-facing reference is [SKILL.md](SKILL.md).

![A gate page: the rail of gates at the top, Copy answers, the Contents sidebar, and image choices](docs/gate-page.png)

## What a page can ask

- **Pick one**: a radio list, often with an image, audio clip or video under each option.
- **Pick any**: a checkbox list.
- **Write**: a text box, for example a replacement prompt.

Every question with options also has a note box. Every page ends with a general **Notes** box. Options can be marked **Recommended**.

Click an item anywhere to choose it. Click an image to see it full size, and press Esc to close it. Your choices are saved as a draft in the browser while you work, so a reload loses nothing.

![Choosing: the picked option gets an orange border, and the status reads "draft saved"](docs/choices.png)

## Your media stays where it is

Pages link to images, audio and video in their original folders. Nothing is copied or embedded, so a page is small and quick to build. If you move or delete a file, pages that show it lose that picture.

## Gates

A longer process can be split into **gates**: one page per step, built when that step is reached. Each gate page has a rail across the top with every gate in the chain:

| Dot | Meaning |
|---|---|
| grey | waiting for your answers |
| teal | answered |
| orange | accepted (the step passed) |

Click a gate on the rail to open that page. Earlier pages are never rebuilt. They read the shared list of gates whenever they open, so they always show the newest gates and states. A gate you already answered reopens with your submitted answers filled in, in any browser. Edits you make afterwards take precedence over the submitted answers, and you can copy them again to send a revision.

The rail appears only when there are two or more gates. A single questionnaire is just the questions.

![Reopening an answered gate: "submitted" in the header, the earlier choice still selected](docs/submitted-gate.png)

## Answering

1. Answer what you want. Questions marked **Required** need an answer before the agent accepts the reply.
2. Click **Copy answers**. The status changes to "answers copied to clipboard".
3. Paste into chat.

If the browser blocks the clipboard, the page shows the answers already selected. Press Ctrl+C (on a phone: long-press, then Copy) and paste.

## On a phone or tablet

Ask for the phone version ("make it for my phone") and the agent builds the page with `--mobile`. Instead of opening Chrome on the PC, it publishes the page through terminal-share and posts a link in the conversation.

1. Tap the link in terminal-share. The page opens in a new tab; you're already signed in.
2. Tap the options (tapping an option's picture picks it), type any notes, and tap **Copy answers**.
3. Go back to the terminal-share tab, paste into the message box, and send.

The phone page shows copies of the images, audio and video, so it doesn't depend on the PC's folders. Only a browser signed in to terminal-share can open it. The pages are kept in `%LOCALAPPDATA%\terminal-share\questions\`; `clean --mobile --older-than 14 --yes` clears out old ones.

## Where the files go

By default the files go in `.html-questions\<slug>\` inside the project you're working in. Add `.html-questions/` to that repo's `.gitignore`. A gate workflow such as Scribeboard keeps its pages in its own run folder.

Each questionnaire makes a few files with the same name stem:

| File | What it is |
|---|---|
| `<stem>.html` | the page |
| `<stem>.bundle.json` | what `read` checks the answers against |
| `<stem>.answers.json` | your pasted answers, saved by the agent |
| `<stem>.responses.json` | the checked, tidied answers |
| `<stem>.answers.js` | the submitted answers, which the page reloads |

Gate chains also have one shared `gates.js`.

## Cleaning up

Deleting the `.html-questions` folder is fine. To clear out old questionnaires and keep recent ones:

```powershell
python %USERPROFILE%\.agents\skills\html-questions\scripts\html_questions.py clean --root "<project>\.html-questions" --older-than 14
```

This is a dry run that lists what it would delete. Add `--yes` to delete. The same command clears old `/md-questions` pages:

```powershell
python %USERPROFILE%\.agents\skills\html-questions\scripts\html_questions.py clean --root "$env:USERPROFILE\.md-questions" --older-than 14 --yes
```

`clean` never deletes a gate list (`gates.js` or VidWiz's `backlot.js`).

## Requirements

- Python 3.10 or later, with `markdown-it-py`.
- Chrome, or any current browser.
