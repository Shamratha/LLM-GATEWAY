# Deploy the full stack on Oracle Cloud (Always-Free) with HTTPS

This runs the **whole** stack — gateway + Redis + Prometheus + Grafana — behind
Caddy with automatic HTTPS, on a free Oracle ARM VM. End result: a live
`https://<you>.duckdns.org/docs` API and `/grafana/` dashboards.

Roughly 20–30 minutes, mostly waiting on Oracle.

## 0. Push the repo to GitHub (one time)

The VM clones the code from GitHub, so it must be pushed first:

```bash
git remote add origin https://github.com/<you>/llm-gateway.git
git push -u origin main
```

## 1. Create the VM

In the Oracle Cloud console → **Compute → Instances → Create Instance**:
- **Image:** Canonical **Ubuntu 22.04**
- **Shape:** **Ampere (VM.Standard.A1.Flex)** — set 1–2 OCPU and 6–12 GB RAM (all within Always-Free).
- **SSH keys:** upload or download a key pair.
- Create, then copy the instance's **Public IP address**.

## 2. Free domain (DuckDNS)

1. Go to duckdns.org, sign in, create a subdomain, e.g. `mygateway` → `mygateway.duckdns.org`.
2. Set its **IP** to your VM's public IP. Save.

## 3. Open the ports (Oracle security list)

Console → your VM's **VCN → Security Lists → default → Add Ingress Rules**. Add two:
- Source `0.0.0.0/0`, IP Protocol TCP, Destination port **80**
- Source `0.0.0.0/0`, IP Protocol TCP, Destination port **443**

(The VM's own firewall is handled by `setup.sh` in step 5.)

## 4. SSH in and clone

```bash
ssh -i /path/to/key ubuntu@<VM_PUBLIC_IP>
git clone https://github.com/<you>/llm-gateway.git
cd llm-gateway
```

## 5. Configure and launch

```bash
cp deploy/.env.example .env
nano .env          # set DOMAIN, ADMIN_API_KEY (openssl rand -hex 32), GRAFANA_ADMIN_PASSWORD
bash deploy/setup.sh
```

`setup.sh` installs Docker, opens the VM firewall for 80/443, and starts the
stack. Caddy fetches a TLS certificate on first boot (~30–60s).

## 6. Verify

- **API docs:** `https://<you>.duckdns.org/docs`
- **Try it:**
  ```bash
  curl https://<you>.duckdns.org/v1/chat/completions \
    -H "Authorization: Bearer sk-alpha-pro-0001" -H "Content-Type: application/json" \
    -d '{"model":"mock-fast","messages":[{"role":"user","content":"hello"}]}'
  ```
- **Grafana:** `https://<you>.duckdns.org/grafana/` (login `admin` / your `GRAFANA_ADMIN_PASSWORD`)

## Operating it

```bash
# logs
sudo docker compose -f docker-compose.prod.yml logs -f
# update after a git push
git pull && sudo docker compose -f docker-compose.prod.yml up -d --build
# stop
sudo docker compose -f docker-compose.prod.yml down
```

## Troubleshooting

- **No cert / site won't load:** confirm DuckDNS points at the right IP, both
  security-list rules exist, and `setup.sh` ran cleanly. Check `logs -f caddy`.
- **Grafana redirect loop or 404:** make sure `DOMAIN` in `.env` matches the URL
  you're visiting exactly (no trailing slash in `.env`).
- **Provider calls fail:** with blank keys the gateway serves the mock provider —
  that's expected. Add real keys to `.env` and re-run the `up -d` command.

## Security notes

- Only Caddy (80/443) is public; gateway, Redis, Prometheus, and Grafana are on
  the internal Docker network. **Never** add a host port for Prometheus.
- Rotate `ADMIN_API_KEY` and the Grafana password from the defaults.
- Redis has no auth but isn't reachable off-box; if you ever publish it, set a
  password and `REDIS_URL=redis://:pass@host:6379/0`.
