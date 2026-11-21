# Pocket Superset

A phone window into the Claude Code sessions running in Superset worktrees on your laptop.
You keep working on the laptop. On the phone you see each live session as a card, open one to
read what you asked and what Claude answered as it happens, and send the next prompt. The
prompt lands in the same terminal, so the laptop pane shows it too.

It only talks to the laptop's Superset CLI (`snapshot`, `terminals read`, `terminals send`) and
reads Claude's own transcript files. No remote desktop, no mouse control, no shell from the
phone.

## What's in it

| Path | What it does |
|---|---|
| `server.py` | The bridge. Python standard library only. Serves the app and four routes: cards, live stream, prompt, key. |
| `app/` | The phone app: plain HTML/CSS/JS, installable from Chrome (manifest + service worker). |
| `scripts/install.sh` | Runs the bridge in the background at login (launchd, label `local.pocket-superset`). |
| `scripts/phone-link.sh` | Gives the bridge a private HTTPS address on your tailnet with Tailscale Serve. |
| `scripts/uninstall.sh` | Stops and removes the background service. |
| `scripts/make_icons.py` | Redraws the app icons. |
| `tests/` | `python3 -m unittest discover -s tests` |

## Set it up

1. **Run the bridge.** `./scripts/install.sh`. It copies the app to `~/.pocket-superset/app`
   (launchd can't start programs from Desktop), starts it on port 8780 and prints the log.
   If the log says *Not logged in*, the CLI has no login of its own (inside Superset terminals
   it borrows the desktop app's). Run `superset auth login` once, then `install.sh` again.
   An API key in the Keychain (`pocket-superset-paradev`) also works if you have one.
2. **Reach it from the phone.** Open the Tailscale app on the Mac and sign in. Install Tailscale
   on the phone with the same account. Then run `./scripts/phone-link.sh` and open the
   `https://…ts.net` address it prints in Chrome on the phone.
3. **Pair the phone.** On the laptop open `http://127.0.0.1:8780/local/pair`. Type the 6-digit
   code on the phone. It works once and expires in 5 minutes.
4. **Install it.** Chrome menu, then *Add to Home screen*. Tap *Turn on alerts* in the app to
   get a notification when a session finishes or needs you.

To try it on the laptop first: `python3 server.py`, open `http://127.0.0.1:8780/local/pair`,
then `http://127.0.0.1:8780/pair` in another tab.

## Safety rules the bridge enforces

- Every API call needs a paired device. Devices get a random secret in an HttpOnly,
  SameSite=Strict cookie; only its hash is stored in `~/.pocket-superset/devices.json`.
- The pairing page only opens on the laptop itself (not through Tailscale).
- The bridge listens on `127.0.0.1` only. Tailscale Serve is the only way in. Never use Funnel,
  which would make it public.
- Optional: `--allow-login you@example.com` (or `POCKET_ALLOW_LOGIN`) refuses every other
  Tailscale user.
- A prompt is sent only when Claude's input box is on screen. If Claude exited to the shell or
  shows a menu, nothing is typed, so phone text can never run as a shell command.
- Before sending, it checks the laptop's input box. If something is already there (your draft
  or Claude's suggestion), the phone asks whether to replace it, add after it or cancel. If Claude
  is mid-turn, it asks before queueing.
- Keys are an allow-list: digits, Esc, Enter and arrows. Menu picks re-read the screen first and
  refuse if the menu changed.
- Every prompt, key and pairing goes to `~/.pocket-superset/audit.log`.
- Lost the phone? `python3 ~/.pocket-superset/app/server.py --revoke-all`.

## How it decides what to show

- **Cards:** `superset snapshot` lists terminals running Claude. Each is matched to its transcript
  under `~/.claude/projects/` by the session title Claude puts in the terminal title.
- **State** (finished / needs you / working) comes from the transcript, because the org policy
  blocks the hooks Superset would use, so Superset reports every agent as "starting".
- **Live view:** the transcript gives the conversation; `terminals read` gives the screen tail,
  the input box and any open prompt, about every 1.5 s while Claude works.
