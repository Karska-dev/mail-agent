**Mail Agent for Mac** — for Macs with Apple Silicon (M1 or newer), macOS 12 or later.

### Install
1. Download **Mail-Agent-mac-apple-silicon.zip** below and open it. Drag **Mail Agent** to your Applications folder.
2. Open it. The app isn't signed by Apple, so the first time macOS blocks it:
   - **macOS 15 (Sequoia) or newer:** click **Done**, then open **System Settings → Privacy & Security**, scroll down to "Mail Agent was blocked…", click **Open Anyway** and confirm.
   - **macOS 14 or older:** right-click the app, choose **Open**, then **Open** again.

   This is needed only once.
3. Mail Agent opens in your web browser. It has no window or Dock icon of its own: quit it with the **Quit** button on the page (it also quits by itself when no page has been open for 30 minutes). Open it again any time to bring the page back.

**Intel Mac?** Install with Python instead: `pipx install git+https://github.com/Karska-dev/mail-agent`, then run `mail-agent`.

Your data stays in `~/.email_agent` on your Mac and passwords go in the Keychain. See [SECURITY.md](https://github.com/Karska-dev/mail-agent/blob/main/SECURITY.md).
