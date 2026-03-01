Create a test task to simulate a GitHub issue webhook. This is used for local testing without needing a real webhook.

Ask the user for:
1. An issue number (any integer, e.g. 99)
2. A short title describing the task
3. An optional detailed body (defaults to the title if not provided)

Then run the script:

```bash
bash scripts/create-task.sh <issue_number> "<title>" "<body>"
```

After creating the task, confirm it was created and remind the user that the worker will pick it up automatically if the container is running (`docker compose up -d`).
