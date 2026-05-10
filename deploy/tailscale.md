# Phone access via Tailscale (no public ports)

The dashboard binds to `127.0.0.1:8501` only. To reach it from your phone without exposing it to the internet, put the VPS and your phone on the same Tailscale tailnet — they'll see each other on a private virtual network and nothing else can.

## 5-minute setup

### 1. Sign up for Tailscale

[tailscale.com/start](https://tailscale.com/start). Free tier covers up to 100 devices — plenty.

### 2. Install Tailscale on the VPS

```bash
ssh root@<your-ip>
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up
```

The `tailscale up` command prints a URL. Open it in your laptop browser, sign in with your Tailscale account, and authorize the new node. Pick a friendly name (e.g. `agent-trading`).

Verify:

```bash
tailscale status
tailscale ip -4    # the 100.x.y.z address Tailscale assigned
```

### 3. Install Tailscale on your phone

App Store / Play Store → Tailscale → log in with the same account.

### 4. Reach the dashboard

On your phone's browser:

```
http://<tailscale-ip>:8501
```

Or use MagicDNS (enabled by default on free tier):

```
http://agent-trading:8501
```

That's it. No firewall holes, no public exposure, no port forwarding.

## Optional hardening — Tailscale ACLs

In the Tailscale admin → Access Controls, restrict who can reach port 8501:

```json
{
  "acls": [
    {
      "action": "accept",
      "src":    ["YOU@example.com"],
      "dst":    ["agent-trading:8501"]
    }
  ]
}
```

Means only your own account can hit the dashboard, even if you ever invite collaborators to the tailnet.

## Why this is better than `--server.address 0.0.0.0` + a firewall rule

| Bind to 0.0.0.0 + firewall rule | Tailscale |
|---|---|
| Public port open (even if firewalled to one IP) | No public port at all |
| Phone IP changes on different networks → firewall rule needs editing | Phone keeps its tailnet IP everywhere |
| Streamlit has no auth | Tailscale is a private mesh; only your authenticated devices |
| Bind audit risk if firewall rule lapses | Zero exposure if Tailscale daemon misconfigured |

## Headless laptop reconnect

If you ever need to re-authenticate Tailscale on the VPS (e.g. after long downtime):

```bash
ssh root@<your-ip>
tailscale up --force-reauth
```

Open the printed URL on your laptop. The VPS keeps its same tailnet IP across re-auths.

## Removing Tailscale

```bash
tailscale logout
apt-get remove tailscale
```

The Streamlit service keeps running on `127.0.0.1:8501`; you'll just need an SSH tunnel to reach it instead.
