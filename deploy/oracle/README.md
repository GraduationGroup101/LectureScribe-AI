# Oracle deployment

This is a deployment kit, not proof of a live deployment. A working Oracle VM,
SSH access, and Cloudflare tunnel credentials are required. No Oracle resources
are provisioned and no domain records are changed by these scripts.

## 1. Create the VM

In your Oracle **home region**, create an Ubuntu **24.04** instance using
`VM.Standard.A1.Flex`, **2 OCPUs / 12 GB RAM**, and a 50 GB boot volume. Check the
console's free eligibility and your *total* tenancy usage before creating it.
Do not select a paid shape or rely on temporary trial credits. Free A1 capacity
may be unavailable. Save the SSH private key locally and note the public IP.
Allow TCP port 22 from your own IP only. Do not open ports 8000 or 11434.

Oracle currently documents 1,500 OCPU-hours and 9,000 GB-hours/month for A1
(equivalent to 2 OCPUs / 12 GB), not the older 4 OCPU / 24 GB allocation.
Always Free instances can be reclaimed for inactivity; this is not a guaranteed
production SLA. [Oracle quotas and conditions](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm).

## 2. Upload from Windows

Run from the project root in PowerShell, replacing the two placeholders:

```powershell
.\deploy\oracle\deploy.ps1 -Server YOUR_PUBLIC_IP -KeyPath 'C:\path\oracle.key'
```

Confirm the SSH host fingerprint against the VM before accepting it. The script
uses your ignored `.env` (or `-EnvPath`) and uploads it separately into a private
temporary directory. Secrets are never included in the code archive or printed.
Server credentials are stored at `/etc/lecturescribe/app.env` with restricted
permissions. The laptop's `.env` is not changed.

The installer installs Python, FFmpeg, Deno, yt-dlp's EJS dependency, cloudflared,
and the project's requirements. It runs the tests and checks `/health`. Ubuntu
24.04 ARM64 and AMD64 are supported. Whisper uses **OpenRouter first, CPU/int8
fallback** on the server; there is no GPU on the free A1 instance. Existing
Windows-only model/FFmpeg paths are removed from the server's environment copy.
API credits are separate from Oracle hosting and are not made free by this setup.

Add `-ValidateOnly` to check local prerequisites without connecting. Updating an
active server refuses to proceed while jobs are queued/running. Deploy during an
idle maintenance window; installs are not rolling/zero-downtime deployments.

## 3. Enable the Ollama formatting fallback

This installer does **not** silently download Ollama or its model. Better Formatting
still needs it when OpenRouter fails. Connect with SSH and install it using the
[official Linux instructions](https://docs.ollama.com/linux):

```bash
curl -fsSL https://ollama.com/install.sh -o /tmp/ollama-install.sh
# Inspect the downloaded installer before running it.
sudo sh /tmp/ollama-install.sh
sudo systemctl enable --now ollama
ollama pull llama3.1:8b-instruct-q4_K_M
curl --fail http://127.0.0.1:11434/api/tags
```

Keep Ollama on localhost. CPU Whisper and an 8B model are slower than your laptop
GPU and share the VM's memory. Test a short lecture and both fallback paths before
claiming they are ready. Without Ollama, a cloud-formatting failure in Better
Formatting mode can still fail the job; Fast mode can return the raw transcript.

## 4. Move the existing Cloudflare tunnel

For the existing **locally managed** `lecturescribe` tunnel, transfer its UUID
credential JSON to the VM. Do not transfer the account-wide `cert.pem` just to
run the connector. Do not paste the JSON contents into chat or commit them.

On the VM, create a private staging directory, upload the JSON using SCP, then
install it as root-readable `/etc/cloudflared/lecturescribe.json`. Copy
`/opt/lecturescribe/deploy/oracle/cloudflared.yml.example` to
`/etc/cloudflared/config.yml` and replace `YOUR_TUNNEL_UUID` with its actual UUID.

```bash
sudo cloudflared --config /etc/cloudflared/config.yml tunnel ingress validate
sudo cloudflared --config /etc/cloudflared/config.yml service install
sudo systemctl enable --now cloudflared
sudo systemctl status cloudflared --no-pager
```

The existing `lecturescribe.app` DNS route must point to that same tunnel UUID.
The `www` ingress also needs a corresponding DNS route if you want to use it.
If the tunnel is **remotely managed**, instead use its dashboard connector
installation and set the published hostname service to `http://localhost:8000`.
Do not mix remote tokens and local JSON credentials.
[Cloudflare Linux service setup](https://developers.cloudflare.com/tunnel/features/locally-managed-tunnels/as-a-service/linux/).

After checking the Oracle connector, stop the old laptop connector so Cloudflare
does not randomly send visitors to either machine. Check:

```bash
curl --fail https://lecturescribe.app/health
```

Also submit a **new short video** in each mode and check the result/progress;
a health response alone does not verify downloading, API credits, or fallbacks.
YouTube may block datacenter IPs. If extraction fails, inspect the logs rather
than assuming cloud transcription fixes the download step.

## State, logs, and backups

Code lives in `/opt/lecturescribe`. Jobs, cache, downloads, and transcript folders
live in `/var/lib/lecturescribe`, independent of code updates. The frontend is
linked there so the application's existing relative paths keep working. Model
and runtime caches live in `/var/cache/lecturescribe`.

The deploy archive deliberately does **not** migrate laptop jobs/transcripts or
MP3 files. Existing server state is preserved. A laptop history migration needs
a separate idle-time snapshot and Windows-to-Linux path conversion in the copied
JSON; never overwrite a running server's state with it.

```bash
sudo systemctl status lecturescribe cloudflared --no-pager
sudo journalctl -u lecturescribe -f
sudo journalctl -u cloudflared -f
```

Both configured services start after reboot. There is one Uvicorn worker and no
reload watcher, matching the JSON-backed job queue. Restarting during work still
interrupts that work; systemd is not a durable job queue.

Back up `/var/lib/lecturescribe` while idle to storage outside the VM. Protect API
keys and tunnel credentials separately. Before public launch, protect the app
with Cloudflare Access or add authentication/rate limits: the existing public
job endpoints can expose history and let strangers consume your paid API credits.
Never commit `.env`, SSH keys, or tunnel credentials. Rotate previously shared
API keys before using them for the public deployment.
