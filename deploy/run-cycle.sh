#!/bin/sh
set -eu

cd /app
export GIT_SSH_COMMAND="ssh -i /run/secrets/github_deploy_key -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/run/secrets/github_known_hosts"

exec python3 scheduled_cycle.py
