# smtp-graph-relay

An internal-only SMTP relay that accepts mail from legacy devices (printers,
scanners, UPSs, etc.) and forwards it through **Microsoft Graph**
`/users/{id}/sendMail` using an Entra ID app registration with
`Mail.Send` application permission.

- **No SMTP AUTH / XOAUTH2** to upstream &mdash; works on tenants where SMTP
  submission is disabled.
- Client-credentials flow: a background daemon, no user sign-in, no refresh
  tokens to babysit.
- LAN-IP allowlist (CIDR) on the front door, sender allowlist on the way out.
- Plain SMTP on :25, no auth required from clients &mdash; because your
  printer from 2007 cannot do STARTTLS.
- Raw MIME is forwarded to Graph as-is (base64), so attachments,
  Content-Type, headers etc. are preserved.

## 1. Create the Entra ID app registration

1. Azure portal &rarr; **Entra ID** &rarr; **App registrations** &rarr; **New
   registration**. Name it e.g. `smtp-graph-relay`. Single tenant is fine.
2. In the new app, **API permissions** &rarr; **Add a permission** &rarr;
   **Microsoft Graph** &rarr; **Application permissions** &rarr; `Mail.Send`.
   Click **Grant admin consent**.
3. **Certificates & secrets** &rarr; **New client secret**. Copy the *Value*
   (not the ID). You will not see it again.
4. From **Overview**, copy **Directory (tenant) ID** and **Application
   (client) ID**.

> `Mail.Send` application permission lets the app send as **any** mailbox in
> the tenant. To scope it down, configure an
> [Application Access Policy](https://learn.microsoft.com/graph/auth-limit-mailbox-access)
> (ExchangeOnline PowerShell: `New-ApplicationAccessPolicy`) that restricts
> the app to a mail-enabled security group containing only your device
> mailboxes (`printer@`, `scanner@`, ...). Do this &mdash; the relay's sender
> allowlist is defense-in-depth but the tenant-side policy is the real fence.

## 2. Configure the relay

```bash
cp config.example.yaml config.yaml
$EDITOR config.yaml
```

Fill in `azure.tenant_id`, `azure.client_id`, `azure.client_secret` (or leave
blank and set `AZURE_TENANT_ID` / `AZURE_CLIENT_ID` / `AZURE_CLIENT_SECRET`
as environment variables in `docker-compose.yml`).

Set `allowed_networks` to the LAN ranges that may submit mail, and
`allowed_senders` to the device mailboxes you created in step 1.

## 3. Run it

```bash
docker compose up -d --build
docker compose logs -f
```

Point your printer/scanner at `<docker-host-ip>:25`, no authentication, no
TLS. The relay logs each accepted message with source IP, envelope, and
Graph response.

## 4. Test it

From a host whose IP is in `allowed_networks`:

```bash
# swaks is the easiest SMTP test tool
swaks \
  --server <docker-host-ip> \
  --port 25 \
  --from printer@contoso.com \
  --to you@example.com \
  --header "Subject: hello from the relay" \
  --body "test $(date)"
```

Or with raw netcat:

```bash
{
  printf 'HELO test\r\n'
  printf 'MAIL FROM:<printer@contoso.com>\r\n'
  printf 'RCPT TO:<you@example.com>\r\n'
  printf 'DATA\r\n'
  printf 'From: printer@contoso.com\r\n'
  printf 'To: you@example.com\r\n'
  printf 'Subject: hello\r\n\r\n'
  printf 'body\r\n.\r\n'
  printf 'QUIT\r\n'
} | nc -q1 <docker-host-ip> 25
```

## How it works

```
 printer/scanner  ---SMTP:25--->  smtp-graph-relay (this project)
                                       |
                                       | 1. IP check (CIDR allowlist)
                                       | 2. MAIL FROM / Header-From allowlist
                                       | 3. OAuth2 client-credentials -> token
                                       |
                                       v
                         POST https://graph.microsoft.com/v1.0/users/{from}/sendMail
                         Content-Type: text/plain
                         Body: base64(raw MIME)
                                       |
                                       v
                         Exchange Online delivers to recipient
```

## Message size handling

Scanners produce big PDFs, so the relay picks the right Graph API path based
on message size:

| Raw MIME size | Path | API calls |
|---|---|---|
| &le; ~3 MB | `POST /users/{id}/sendMail` (raw MIME) | 1 |
| &gt; ~3 MB | Create draft &rarr; attach (inline or upload session) &rarr; send | 3 + 1 per attachment (+ chunks) |

- The one-shot MIME endpoint hard-caps at ~4 MB of base64 (~3 MB raw); that
  is why the threshold exists. Over that, we parse the incoming MIME,
  create a draft message via JSON, and upload each attachment: small
  attachments (&lt;3 MB) go inline as base64 JSON, larger ones use a
  resumable upload session with 3840 KiB chunks (12 &times; 320 KiB).
- `smtp.max_message_size` in `config.yaml` enforces an early `552` reject at
  the SMTP layer. Keep it at or below your tenant's Exchange Online
  **MaxSendSize** (default 35 MB, admin-configurable up to 150 MB via
  `Set-Mailbox <mailbox> -MaxSendSize 150MB`). The default in the example
  config is 35 MB.
- Per-attachment ceiling via upload session is ~150 MB, but overall message
  size is bounded by MaxSendSize, so tune both ends in lockstep.

If a draft creation succeeds but an attachment upload or the final send
fails, the relay issues a best-effort `DELETE` on the draft so failed
messages don't linger in the sender mailbox's Drafts folder.

## Other notes

- `save_to_sent_items` defaults to `false`; flipping it to `true` makes a
  copy land in the sender mailbox's *Sent Items*. Note: the draft-based
  path always writes to *Sent Items* because draft messages are mailbox
  resources &mdash; this flag only affects the one-shot path.
- The image runs as a non-root user and relies on `CAP_NET_BIND_SERVICE` in
  `docker-compose.yml` to let it bind to :25.
- Graph returns `202 Accepted` synchronously; actual delivery is asynchronous
  inside Exchange Online. If you need bounce handling, monitor the sender
  mailbox or use message trace in Exchange admin.

## Files

- `app/main.py` &mdash; entrypoint, signal handling, controller wiring.
- `app/handler.py` &mdash; aiosmtpd handler: IP + sender policy.
- `app/graph.py` &mdash; OAuth2 token cache + Graph send client (one-shot MIME + draft/upload-session dispatch).
- `app/mime_split.py` &mdash; splits inbound MIME into body + attachments for the large-message path.
- `app/config.py` &mdash; YAML loader, CIDR allowlist, sender allowlist.
- `requirements.in` &mdash; top-level runtime dependencies (what humans edit).
- `requirements.txt` &mdash; fully resolved hash-locked lockfile (generated).

## Supply-chain hardening

The image is built with end-to-end hash verification from the base layer up:

- **Base image pinned by digest.** `FROM python:3.12-slim@sha256:...` in the
  `Dockerfile` &mdash; the tag is cosmetic; the digest is authoritative and
  cannot be re-pointed by the upstream publisher.
- **Every Python dependency is hash-locked**, including transitive deps.
  `requirements.txt` is generated with `pip-compile --generate-hashes` from
  `requirements.in` and lists a sha256 for every wheel / sdist.
- **Install runs with `--require-hashes --no-deps --only-binary=:all:`**:
    - `--require-hashes` makes pip refuse any package whose download hash
      doesn't match the lockfile (proven in the build logs if tampered).
    - `--no-deps` forbids pip from resolving anything not explicitly listed;
      since the lockfile is already fully transitive this is safe.
    - `--only-binary=:all:` blocks sdist fallback, preventing attacker
      `setup.py` execution even if a wheel gets yanked.

### Regenerating the lockfile

When bumping a pin in `requirements.in`:

```bash
docker run --rm -u "$(id -u):$(id -g)" -e HOME=/tmp \
    -v "$PWD:/w" -w /w \
    python:3.12-slim@sha256:804ddf3251a60bbf9c92e73b7566c40428d54d0e79d3428194edf40da6521286 \
    sh -c 'pip install --quiet --user pip-tools==7.4.1 && \
           /tmp/.local/bin/pip-compile --generate-hashes --allow-unsafe \
               --output-file=requirements.txt requirements.in'
```

Review the diff carefully &mdash; new transitive packages will appear in the
lockfile. Then rebuild: `docker compose build --no-cache`.

### Upgrading the base image

1. Pick a new digest from https://hub.docker.com/_/python/tags.
2. Update the `FROM` line in `Dockerfile` **and** the example command in
   `requirements.in` / this README.
3. Regenerate the lockfile (above) so wheels are selected against the new
   Python ABI if the minor version changed.
4. `docker build --no-cache`.
