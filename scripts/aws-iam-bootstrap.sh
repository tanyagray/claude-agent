#!/bin/bash
# One-time IAM setup for claude-agent EC2 deployments.
# Creates an IAM role + instance profile that allows EC2 instances
# to read secrets from SSM Parameter Store under /claude-agent/*.
#
# Usage: bash scripts/aws-iam-bootstrap.sh [--region us-east-1]
#
# Idempotent — safe to run multiple times.

set -euo pipefail

ROLE_NAME="claude-agent-ssm-reader"
POLICY_NAME="claude-agent-ssm-read"
REGION="${AWS_REGION:-us-east-1}"

while [[ $# -gt 0 ]]; do
  case $1 in
    --region) REGION="$2"; shift 2 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

export AWS_DEFAULT_REGION="$REGION"

echo "==> Bootstrapping IAM for claude-agent in $REGION"

ACCOUNT_ID=$(aws sts get-caller-identity --query 'Account' --output text)
POLICY_ARN="arn:aws:iam::${ACCOUNT_ID}:policy/${POLICY_NAME}"

# --- Policy ---
if aws iam get-policy --policy-arn "$POLICY_ARN" &>/dev/null; then
  echo "    Policy $POLICY_NAME already exists, skipping."
else
  echo "    Creating IAM policy: $POLICY_NAME"
  aws iam create-policy \
    --policy-name "$POLICY_NAME" \
    --policy-document '{
      "Version": "2012-10-17",
      "Statement": [
        {
          "Effect": "Allow",
          "Action": [
            "ssm:GetParameter",
            "ssm:GetParameters"
          ],
          "Resource": "arn:aws:ssm:*:'"$ACCOUNT_ID"':parameter/claude-agent/*"
        }
      ]
    }' \
    --tags Key=Project,Value=claude-agent >/dev/null
fi

# --- Role ---
if aws iam get-role --role-name "$ROLE_NAME" &>/dev/null; then
  echo "    Role $ROLE_NAME already exists, skipping."
else
  echo "    Creating IAM role: $ROLE_NAME"
  aws iam create-role \
    --role-name "$ROLE_NAME" \
    --assume-role-policy-document '{
      "Version": "2012-10-17",
      "Statement": [
        {
          "Effect": "Allow",
          "Principal": { "Service": "ec2.amazonaws.com" },
          "Action": "sts:AssumeRole"
        }
      ]
    }' \
    --tags Key=Project,Value=claude-agent >/dev/null

  aws iam attach-role-policy \
    --role-name "$ROLE_NAME" \
    --policy-arn "$POLICY_ARN"
fi

# --- Instance Profile ---
if aws iam get-instance-profile --instance-profile-name "$ROLE_NAME" &>/dev/null; then
  echo "    Instance profile $ROLE_NAME already exists, skipping."
else
  echo "    Creating instance profile: $ROLE_NAME"
  aws iam create-instance-profile \
    --instance-profile-name "$ROLE_NAME" \
    --tags Key=Project,Value=claude-agent >/dev/null

  aws iam add-role-to-instance-profile \
    --instance-profile-name "$ROLE_NAME" \
    --role-name "$ROLE_NAME"

  echo "    Waiting for instance profile to propagate..."
  sleep 10
fi

echo ""
echo "==> IAM bootstrap complete."
echo "    Role:             $ROLE_NAME"
echo "    Policy:           $POLICY_ARN"
echo "    Instance Profile: $ROLE_NAME"
