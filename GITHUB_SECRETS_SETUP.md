# GitHub Actions Secrets: Lookup API deployment

This workflow is **manual**: Actions → Deploy Lookup API to VPS → Run workflow. It does not set up server login or DNS automatically. The existing DNS mapping `api.teamsecret.in` → `2f9a8433-c321-407b-8a42-f15ec0ae2057.cfargotunnel.com` must remain in place. The VPS should run Debian/Ubuntu with root SSH (or you must adapt the deployment paths and privileges). This deploy is **not zero-downtime** and is not a database migration.

## Step 1: Create GitHub repository secrets

Repository → Settings → Secrets and variables → Actions → New repository secret:

| Secret name | Content |
|---|---|
| `LOOKUP_SSH_HOST` | New VPS IP or hostname |
| `LOOKUP_SSH_USER` | `root` (this workflow assumes `/root/` paths) |
| `LOOKUP_SSH_PRIVATE_KEY` | Private key of a dedicated SSH deployment identity |
| `LOOKUP_SSH_KNOWN_HOSTS` | **Verified** SSH known_hosts line for the new server |
| `LOOKUP_ENV_B64` | Base64-encoded **complete** `/root/lookup/.env`, including the original `APP_ENCRYPTION_KEY`, bot token etc. |
| `LOOKUP_CF_JSON_B64` | Base64-encoded tunnel credentials JSON for tunnel `2f9a8433-c321-407b-8a42-f15ec0ae2057` |

Prepare the base64 values **locally**, without printing them to chat or terminal history:

```bash
base64 -w0 /root/lookup/.env > /root/lookup-env.b64
base64 -w0 /root/lookup-cf-home/.cloudflared/2f9a8433-c321-407b-8a42-f15ec0ae2057.json > /root/lookup-tunnel.b64
```

On macOS use `base64 -i FILE | tr -d '\n'` instead. Paste the **file contents** in the corresponding GitHub secret fields (not into source code). Delete the temporary base64 files securely after updating the secrets; treat base64 as sensitive, **not encryption**.

For a new SSH deployment key use `ssh-keygen -t ed25519 -f ~/.ssh/lookup_deploy -C lookup-deploy`, add its **public** key to the VPS `/root/.ssh/authorized_keys`, and save the **private** key in `LOOKUP_SSH_PRIVATE_KEY`. Establish `LOOKUP_SSH_KNOWN_HOSTS` using a trusted server fingerprint (compare the key/fingerprint via your VPS provider console); blindly trusting `ssh-keyscan` output is unsafe.

## Step 2: Configure `.env`

Keep original bot environment settings; include:

```env
CLOUDFLARE_TUNNEL_ID=2f9a8433-c321-407b-8a42-f15ec0ae2057
PUBLIC_API_URL=https://api.teamsecret.in
AUTO_CLOUDFLARE_TUNNEL=false
```

Remove the **old** `CLOUDFLARE_TUNNEL_TOKEN`. Current `start.sh` reads the tunnel ID and private JSON from `/root/lookup-cf-home/.cloudflared/` and generates a tunnel-only config; it does not re-create Cloudflare DNS. To change the public hostname you must also update the ingress hostname in `start.sh` and its DNS mapping.

## Step 3: Push this safe code to GitHub

Inspect tracked files before pushing: `git ls-files` and `git status`. **`.gitignore` does not remove secrets already committed.** Remove any tracked private files with `git rm --cached PATH` and, if previously pushed, revoke/rotate credentials and purge history as appropriate.

## Step 4: Deploy and verify

Actions → Deploy Lookup API to VPS → Run workflow. Then on the new VPS:

```bash
curl -i http://127.0.0.1:8000/
curl -i https://api.teamsecret.in/
screen -ls
```

The workflow only touches `/root/lookup` and `/root/lookup-cf-home/.cloudflared` and the dedicated `lookup-*` screen sessions. It does not remove DB2 tunnel files. **Before switching traffic**, stop the old Lookup connector: two servers running the same tunnel can both receive requests. Also ensure the new server has outbound network access and DNS mapping remains unchanged.

Security note: Previous Cloudflare API/tunnel tokens were pasted into chat earlier. Revoke exposed API tokens; do not include any token or JSON secret in public GitHub code, logs, screenshots, or chat.
