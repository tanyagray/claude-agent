Deploy claude-agent to AWS EC2 for a target GitHub repository. This creates an EC2 instance running the agent container with persistent storage and a stable public IP for webhooks.

## Steps

### 1. Check prerequisites

Run `aws sts get-caller-identity` and show the user the **Account ID** and **ARN**. Ask them to confirm this is the correct AWS account before proceeding.

Check that the IAM instance profile exists:
```bash
aws iam get-instance-profile --instance-profile-name claude-agent-ssm-reader 2>/dev/null
```

If it doesn't exist, ask the user if they want to create it, then run:
```bash
bash scripts/aws-iam-bootstrap.sh --region <region>
```

### 2. Collect deployment parameters

Ask the user for:

1. **Target repo** — the GitHub repo this agent instance will serve (e.g., `myorg/myrepo`)
2. **AWS region** — default: `us-east-1`
3. **Instance type** — default: `t3.small` (suggest `t3.medium` for larger repos)
4. **Docker image** — default: `ghcr.io/<target-repo>:latest` (this is the claude-agent image, override if hosted elsewhere)

### 3. Run the deployment

Execute the deploy script with the collected parameters:

```bash
bash scripts/aws-deploy.sh create \
  --repo "<owner/repo>" \
  --region "<region>" \
  --instance-type "<instance-type>" \
  --image "<image>"
```

The script will interactively prompt for secrets (GITHUB_TOKEN, GITHUB_WEBHOOK_SECRET, ANTHROPIC_API_KEY, SLACK_WEBHOOK_URL).

### 4. Report results

After deployment, summarize:
- **Webhook URL** — the user needs to add this to their GitHub repo settings
- **SSH command** — for debugging and log access
- **Health check URL** — to verify the deployment
- **Webhook secret** — the user needs this for the GitHub webhook configuration
- Remind them to configure the GitHub webhook: Settings → Webhooks → Add webhook

## Other operations

- **Update** (pull latest image): `bash scripts/aws-deploy.sh update --repo owner/repo`
- **Teardown** (destroy all resources): `bash scripts/aws-deploy.sh teardown --repo owner/repo`
- **Status** (check all deployments): `bash scripts/aws-deploy.sh status`
