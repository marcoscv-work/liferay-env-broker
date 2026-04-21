# Example Installation Prompt

Use this prompt to ask Codex to install and validate the broker on a new test machine.

```text
Install and configure the Liferay Environment Broker on a test Linux host.

Connection details:

- SSH host/IP: <BROKER_HOST>
- SSH user: <SSH_USER>
- SSH port: <SSH_PORT_OR_22>
- SSH authentication: <password / existing SSH key / jump host>
- Sudo access: <yes/no>
- Target broker URL reachable by users: http://<BROKER_HOST>:8899

Repository/package source:

- Repository URL: https://github.com/marcoscv-work/liferay-env-broker
- Branch: main

Installation tasks:

1. Connect to the host over SSH.
2. Detect the operating system.
3. Install required packages:
   - python3
   - python3-venv
   - python3-pip
   - docker.io
   - unzip
   - rsync
   - curl
   - git
4. Enable and start Docker.
5. Clone or upload the repository to a temporary directory.
6. Generate strong random tokens for:
   - admin
   - developer
7. Configure `config.yaml` on the target machine only:
   - set `api_tokens`
   - set `base_url_template` to `http://<BROKER_HOST>:{port}`
   - tune resource profiles based on available RAM
   - tune `ram_buffer_mb` so the broker can create environments safely on this host
8. Run `install.sh`.
9. Restart `liferay-broker.service`.
10. Store generated credentials on the host in a root-only file, for example:
    `/root/liferay-env-broker-credentials.txt`
11. Do not commit real tokens, host IPs, generated registries, or runtime config back to the public repository.

Validation tasks:

1. Check service status:
   `systemctl status liferay-broker.service --no-pager -l`
2. Check logs:
   `journalctl -u liferay-broker.service -n 80 --no-pager`
3. Check API health:
   `curl http://127.0.0.1:8899/health`
4. Check authenticated API access using the generated developer token:
   `GET /v1/profiles`
5. Check Docker:
   `docker run --rm hello-world`
6. If the host has enough RAM and Docker Hub access, optionally test creating a Liferay environment with the smallest suitable profile.

Report back with:

- Installed path
- Service status
- Broker URL
- Where credentials were stored
- Resource profile values applied
- Validation results
- Any limitations, especially RAM or Docker image pull issues
```

