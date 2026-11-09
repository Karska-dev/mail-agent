# Security and privacy

Mail Agent reads your email, so it is built to keep everything on your own computer. This page explains where your data goes and how to report a problem.

## Where things are stored

| What | Where |
| --- | --- |
| Mail account passwords (app passwords) and the optional Claude API key | On a Mac: the macOS Keychain, under "Mail Agent", never in a file. On other systems: `~/.email_agent/secrets.json`, readable only by your user account. |
| Accounts list (address and server, no password), settings | `~/.email_agent/` |
| Learned rules, notes, to-dos, activity log | `~/.email_agent/` |
| A copy of each email the agent handled (so you can review or restore it) | `~/.email_agent/originals/` |
| Notes and to-dos removed by "Clean up" (so it can be undone) | `~/.email_agent/cleared/` |
| The Mac app's messages (start-up, errors) | `~/.email_agent/app.log` |

Delete `~/.email_agent/` (and, on a Mac, the "Mail Agent" entries in Keychain Access) to remove everything.

## What leaves your computer

- **Your mail server** (for example Gmail over IMAP with TLS): reading mail and moving it when you ask.
- **The AI reader you choose:**
  - *Built-in*: nothing leaves your computer.
  - *Local AI (Ollama)*: talks only to Ollama on your own computer (`127.0.0.1:11434`, or `OLLAMA_HOST` if you set it). Downloading a model fetches it from Ollama's library.
  - *Claude*: sends the email text to the Anthropic API (`api.anthropic.com`), only if you add an API key and pick this reader.
- **Unsubscribe**, only when you or one of your rules asks for it: a one-click unsubscribe request to the address the sender published. Otherwise the link is added to your to-dos for you to open.
- **Images in emails** are blocked by default. They load from the sender's servers only when you click "Load images".

## Safety measures

- The app's page is served only on your own computer (`127.0.0.1:8765`) and rejects requests from other websites.
- Emails are shown with scripts removed, in a sandboxed frame.
- Reading never marks mail as read unless an action does; *Practice mode* moves and deletes nothing.
- Possible scam emails are flagged, and links from them are never saved to notes or to-dos.

## Reporting a vulnerability

Please report security problems privately through GitHub: open the repository's **Security** tab and choose **Report a vulnerability**. Please don't open a public issue for security problems.
