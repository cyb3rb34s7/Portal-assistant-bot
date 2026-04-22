Got it. Pushing now.

Pushed successfully. Let me clean up all traces of the token from the sandbox:

## Done — code is live on GitHub

**Repo:** https://github.com/cyb3rb34s7/Portal-assistant-bot
**Branch:** `main`
**Commit:** `fcfe8db` — Initial CurationPilot POC

The token has been scrubbed from the sandbox's git remote config (now just the plain HTTPS URL), and a full workspace scan confirmed no lingering token strings anywhere.

## Your next steps

### 1. Revoke the token (do this now — takes 10 seconds)

Go to **https://github.com/settings/tokens** → find `curationpilot-poc-push` → click **Revoke**. 

You already shared it in chat, and it's better practice to invalidate any token that touched a shared channel. Since we're done with it, there's zero cost to revoking.

### 2. Clone the repo on your PC

```powershell
cd $HOME\Desktop        # or wherever you want it
git clone https://github.com/cyb3rb34s7/Portal-assistant-bot.git
cd Portal-assistant-bot
```

### 3. Install prereqs

```powershell
# Python
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
playwright install chromium

# Portal
cd sample_portal
npm install
cd ..
```

### 4. Run the POC (three PowerShell windows)

**Window 1 — portal**
```powershell
cd sample_portal
npm run dev
```
Wait for `Local: http://localhost:5173/`.

**Window 2 — Chrome with CDP**
```powershell
& "C:\Program Files\Google\Chrome\Application\chrome.exe" `
  --remote-debugging-port=9222 `
  --user-data-dir="$env:USERPROFILE\.curationpilot-chrome-profile" `
  --no-first-run --no-default-browser-check `
  http://localhost:5173
```
A Chrome window opens with the sample portal loaded. Leave it open.

**Window 3 — the pilot**
```powershell
.\.venv\Scripts\Activate.ps1
python -m pilot doctor
python -m pilot run sample_tasks\add_and_verify.json
```

`doctor` should print `Connected. 1 page(s): - http://localhost:5173/`. If yes, you're past Gate 0 on your own machine. Then `run` will drive the Chrome window, pause at the delete gate (type `y`), and create a `sessions\<id>\` folder with the full audit trail.

Tell me what `python -m pilot doctor` prints on your machine — that's the real moment of truth.
