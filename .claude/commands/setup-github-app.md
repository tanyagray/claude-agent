Create a GitHub App for claude-agent and configure the .env with the resulting credentials.

The setup script (`scripts/setup-github-app.sh`) has been split into individual subcommands so that YOU drive all user interaction via AskUserQuestion — no interactive terminal prompts.

## Steps

### 1. Check prerequisites

```bash
bash scripts/setup-github-app.sh check-prereqs
```

If this fails, tell the user what to install/configure and stop.

### 2. Get the target repo

Use AskUserQuestion to ask: "Which repository should the GitHub App be installed on?"

Suggest the current git remote as a default option:
```bash
git remote get-url origin
```

Parse owner/repo from the URL (strip `git@github.com:` or `https://github.com/` prefix and `.git` suffix).

### 3. Get the app name

Use AskUserQuestion to ask: "What should the app be called?"

Offer options like:
- `claude-agent` (default)
- Something project-specific based on the repo name

Explain that GitHub will suffix it with `[bot]`, so `claude-agent` becomes `@claude-agent[bot]`.

### 4. Start the callback server and open the browser

```bash
bash scripts/setup-github-app.sh start-server <owner/repo> <app-name>
```

This starts a local server and opens the browser automatically. Tell the user:
"I've opened GitHub in your browser. Click **Create GitHub App** to continue."

### 5. Wait for the callback

Run this in the background:
```bash
bash scripts/setup-github-app.sh wait-for-callback
```

This blocks until GitHub redirects back (up to 3 minutes). When it returns, it prints the OAuth code.

If it prints `TIMEOUT`, tell the user it timed out and run `bash scripts/setup-github-app.sh cleanup`.

### 6. Exchange the code for credentials

```bash
bash scripts/setup-github-app.sh exchange-code <code>
```

This returns JSON with `id`, `slug`, `pem`, `webhook_secret`. Save this output — you'll need it for later steps.

Tell the user: "App **<slug>** created (ID: <id>)."

### 7. Install the app on the repo

```bash
bash scripts/setup-github-app.sh open-install <app-slug>
```

This opens the installation page. Use AskUserQuestion to ask the user to confirm once they've selected their repo and clicked Install. Say:
"I've opened the installation page. Select **<repo>** and click **Install**, then confirm here."

Options: "Done, I've installed it" / "I need help"

### 8. Fetch the installation ID

Extract the PEM from the JSON credentials (step 6) and pass it with the app ID. The PEM must have real newlines (not `\n` literals):

```bash
PEM=$(echo '<json-creds>' | python3 -c "import json,sys; print(json.load(sys.stdin)['pem'])")
bash scripts/setup-github-app.sh get-installation-id <app-id> "$PEM"
```

If this returns `NOT_FOUND`, use AskUserQuestion to ask the user to enter it manually. Tell them where to find it: "Go to https://github.com/settings/installations, click the app, and copy the number at the end of the URL."

### 9. Format and display the env vars

```bash
bash scripts/setup-github-app.sh format-env '<json-creds>' <installation-id>
```

Pass the JSON from step 6 (single-quoted to protect special chars) and the installation ID.

Display the output to the user and explain each line briefly.

### 10. Remind about remaining setup

Tell the user:
- Paste the env vars into their `.env` file
- Update the app's webhook URL once they know their server address: `https://github.com/settings/apps/<slug>` → Webhook URL
- Enable "Allow GitHub Actions to create and approve pull requests" in the target repo under Settings → Actions → General
- Run `docker compose up -d` to start the agent

### Error handling

If any step fails, always run cleanup:
```bash
bash scripts/setup-github-app.sh cleanup
```
