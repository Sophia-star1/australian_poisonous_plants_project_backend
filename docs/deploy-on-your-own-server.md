# Deploy on your own server

This guide puts the backend online on a cloud server that you rent, so the
mobile app and website can use it. Everything runs on that one server: the
API, the database, the photo storage, and **Caddy**, which gives you secure
`https://` addresses with certificates it obtains and renews by itself.

No programming knowledge is needed. Every command is given in full, with
what it does and what you should see. Plan about 2 to 3 hours the first
time.

If you would rather not look after a server, see
[Deploy on Google Cloud](deploy-on-google-cloud.md). To try the backend on
your own computer first, see
[Run on your computer](run-on-your-computer.md).

## Contents

1. [How it fits together](#1-how-it-fits-together)
2. [What you need and what it costs](#2-what-you-need-and-what-it-costs)
3. [Create a login key on your computer](#3-create-a-login-key-on-your-computer)
4. [Rent the server](#4-rent-the-server)
5. [Point your domain at the server](#5-point-your-domain-at-the-server)
6. [Log in to the server](#6-log-in-to-the-server)
7. [Prepare the server](#7-prepare-the-server)
8. [Get the code onto the server](#8-get-the-code-onto-the-server)
9. [Upload the database files](#9-upload-the-database-files)
10. [Create the settings file](#10-create-the-settings-file)
11. [Start the backend](#11-start-the-backend)
12. [Create your administrator account](#12-create-your-administrator-account)
13. [Move the existing photos](#13-move-the-existing-photos)
14. [Set up email](#14-set-up-email)
15. [Connect the app and the website](#15-connect-the-app-and-the-website)
16. [Backups](#16-backups)
17. [Updating to a new version](#17-updating-to-a-new-version)
18. [Everyday commands](#18-everyday-commands)
19. [If something goes wrong](#19-if-something-goes-wrong)

---

## 1. How it fits together

```
 Phones and browsers
        │  https://api.example.org         https://media.example.org
        ▼                                   ▼
 ┌─────────────────────────── your server ───────────────────────────┐
 │  Caddy (ports 80 and 443, HTTPS certificates)                      │
 │     ├── api.example.org   ──► API ──► PostgreSQL (database)        │
 │     │                          └────► RustFS (photo storage)        │
 │     └── media.example.org ──► RustFS (photos, read-only)           │
 └────────────────────────────────────────────────────────────────────┘
```

- `api.<your domain>` is the address the app and website talk to.
- `media.<your domain>` serves the photos. Visitors can only read them;
  only the API can add or delete them.
- The database and photo storage are not reachable from the internet at all.

Throughout this guide, replace:

| Placeholder | With |
|---|---|
| `example.org` | Your domain |
| `SERVER_IP` | Your server's IP address (step 4) |
| `<owner>/<repository>` | The repository's address on GitHub |

## 2. What you need and what it costs

- **A domain name** that you control, for example `example.org`, and access
  to its DNS settings at the company you bought it from (Namecheap, GoDaddy,
  Cloudflare, ...). About US$10–20 a year.
- **A cloud server** (often called a VPS, Droplet or instance) running
  **Ubuntu 24.04 LTS**, with:
  - 2 processors (vCPUs), 4 GB of memory (2 GB works, with the extra step
    in 7.3), 50 GB of disk or more (photos take space);
  - a location close to your users, for example Sydney.

  Any provider works: DigitalOcean, AWS Lightsail, Vultr, Linode/Akamai,
  BinaryLane, Microsoft Azure, ... About US$12–25 a month. The examples
  below use **DigitalOcean**; other providers have the same choices under
  slightly different names.
- **The database files** from the hand-over (files ending in `.sql`).
- **Access to the code** on GitHub.
- **Your own computer** with a terminal: **PowerShell** on Windows,
  **Terminal** on a Mac. (See
  [Run on your computer, step 3](run-on-your-computer.md#3-open-a-terminal)
  if you have never used one.) You do not need Docker on your computer.
- Optional: an email mailbox the backend can send from (step 14).

## 3. Create a login key on your computer

Servers are logged into with a **key** rather than a password: a pair of
files on your computer. The *public* half goes on the server; the *private*
half never leaves your computer. This is both easier and much safer than a
password.

Open a terminal **on your computer** and run (the same on Windows and Mac):

```bash
ssh-keygen -t ed25519 -C "apl-server"
```

- When asked where to save it, press **Enter** to accept the default.
- When asked for a passphrase, type one and press **Enter** (nothing appears
  while you type; that is normal), then type it again. You will need it
  each time you log in. Write it down somewhere safe.

Now show the public half so you can copy it:

**Windows:**
```powershell
Get-Content $HOME\.ssh\id_ed25519.pub
```

**Mac:**
```bash
cat ~/.ssh/id_ed25519.pub
```

> **You should see** one line starting with `ssh-ed25519 AAAA` and ending
> with `apl-server`. Copy that whole line; you will paste it in step 4.

Never share the file **without** `.pub` (`id_ed25519`): it is the private
key.

## 4. Rent the server

On **DigitalOcean** (sign up at <https://www.digitalocean.com>, add a
payment method):

1. Click **Create → Droplets**.
2. **Region:** the one closest to your users (for example **Sydney**).
3. **Image:** **Ubuntu**, version **24.04 (LTS) x64**.
4. **Size:** **Basic**, **Regular**, **4 GB / 2 CPUs** (or larger).
5. **Authentication:** choose **SSH Key** → **New SSH Key**, paste the line
   from step 3, give it a name, **Add SSH Key**.
6. **Backups:** turn on **automatic backups** (recommended: a copy of the
   whole server every week or day, for a small extra fee).
7. Give it a hostname such as `apl-backend` and click **Create Droplet**.

After a minute the server appears with an **IPv4 address** such as
`170.64.123.45`. That is your `SERVER_IP`. Write it down.

> On **AWS Lightsail**: *Create instance* → *Linux/Unix* → *OS Only* →
> *Ubuntu 24.04 LTS* → upload your key under *Change SSH key pair* → choose
> a 4 GB plan. Then, under *Networking*, attach a **static IP** and add
> firewall rules for **HTTPS (443)** and **HTTP (80)**. Lightsail's login
> user is `ubuntu` instead of `root` (see step 6).

## 5. Point your domain at the server

You need two addresses, both pointing at the server:

| Name | Used for |
|---|---|
| `api.example.org` | The API |
| `media.example.org` | The photos |

At the company that manages your domain, open its **DNS settings** and add
two records:

| Type | Host / Name | Value / Points to | TTL |
|---|---|---|---|
| `A` | `api` | `SERVER_IP` | Automatic |
| `A` | `media` | `SERVER_IP` | Automatic |

For example on **Namecheap**: *Domain List* → **Manage** next to the domain
→ **Advanced DNS** → **Add New Record** → **A Record**.

> **Cloudflare users:** set each record's *Proxy status* to **DNS only**
> (grey cloud). Caddy needs to reach the server directly to obtain its
> certificates.

It can take from a few minutes to an hour for the records to work. Check
from your computer:

```bash
nslookup api.example.org
nslookup media.example.org
```

> **You should see** `Address: SERVER_IP` in the answer to each. Until you
> do, continue with the next steps; just do not start the backend (step 11)
> before both answers are right.

## 6. Log in to the server

From the terminal on your computer:

```bash
ssh root@SERVER_IP
```

- The first time, it asks *"Are you sure you want to continue connecting
  (yes/no/[fingerprint])?"*. Type `yes` and press **Enter**.
- Type your key's passphrase from step 3.

> **You should see** a welcome message and a prompt like
> `root@apl-backend:~#`. From now on, **commands you type here run on the
> server**, not on your computer. The prompt tells you where you are.

To log out, type `exit`. To log in again later, run the same `ssh` command.

> **Lightsail and some other providers** use another user name, such as
> `ubuntu`: run `ssh ubuntu@SERVER_IP`, then `sudo -i` to become the
> administrator (root). The rest of this guide assumes you are root.

## 7. Prepare the server

Run each block on the server. You can paste a whole block at once.

### 7.1 Install updates

```bash
apt update && apt upgrade -y
```

This downloads and installs the latest security updates. It takes a few
minutes. If a purple screen asks which services to restart, press **Enter**
to accept. If the output ends by saying a restart is required, run
`reboot`, wait a minute, and log in again (step 6).

Ubuntu also installs security updates automatically from now on.

### 7.2 Turn on the firewall

```bash
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 443/udp
ufw --force enable
ufw status
```

This blocks everything except logging in (SSH) and web traffic (ports 80
and 443).

> **You should see** `Status: active` and rules for `OpenSSH`, `80/tcp`,
> `443/tcp` and `443/udp`.

### 7.3 Add swap space (only for a 2 GB server)

Swap lets the server use disk space when memory runs short, so building the
backend does not fail. Skip this on a 4 GB server.

```bash
fallocate -l 2G /swapfile
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
free -h
```

> **You should see** a `Swap:` line showing `2.0Gi`.

### 7.4 Install Docker

```bash
curl -fsSL https://get.docker.com | sh
```

This downloads Docker's official installer and runs it. It takes a minute
or two.

```bash
docker --version
docker compose version
```

> **You should see** two version lines, such as `Docker version 29.x` and
> `Docker Compose version v2.x`.

## 8. Get the code onto the server

The repository is private, so the server needs permission to download it.
You give it a **personal access token**: a password that can only read this
one repository.

**Create the token** (in your browser, on GitHub):

1. Click your profile picture (top right) → **Settings**.
2. At the bottom of the left menu: **Developer settings** →
   **Personal access tokens** → **Fine-grained tokens** →
   **Generate new token**.
3. **Token name:** `apl-server`. **Expiration:** for example 1 year.
4. **Repository access:** **Only select repositories**, and pick the
   backend repository.
5. **Permissions** → **Repository permissions** → **Contents**:
   **Read-only**.
6. Click **Generate token** and **copy it now**: GitHub shows it only once.

**Download the code** (on the server):

```bash
git clone https://github.com/<owner>/<repository>.git /opt/apl-backend
```

- **Username:** your GitHub user name.
- **Password:** paste the token. Nothing appears while you paste; press
  **Enter**.

> **You should see** `Cloning into '/opt/apl-backend'...` and `done`.

Go into the folder, and remember the token so updates do not ask again:

```bash
cd /opt/apl-backend
git config --global credential.helper store
ls
```

> **You should see** files including `docker-compose.yml`, `deploy`, `docs`
> and `schema`.

The `credential.helper store` setting saves the token in a file on the
server that only root can read, the next time you enter it.

## 9. Upload the database files

Copy the hand-over `.sql` files from your computer to the server.

1. **On your computer**, put the `.sql` files in a folder called
   `handover-sql` on your Desktop. Make sure their names sort in the order
   they must run (for example `01_schema.sql`, `02_data.sql`): the server
   runs them alphabetically, and the file that creates the tables must come
   first.
2. Open a **second** terminal window on your computer (leave the server one
   open) and run:

   **Windows:**
   ```powershell
   scp -r $HOME\Desktop\handover-sql root@SERVER_IP:/root/
   ```

   **Mac:**
   ```bash
   scp -r ~/Desktop/handover-sql root@SERVER_IP:/root/
   ```

   It asks for your key's passphrase, then lists each file as it copies.

3. **Back on the server**, put them where the database will find them:

   ```bash
   cp /root/handover-sql/*.sql /opt/apl-backend/schema/
   ls /opt/apl-backend/schema
   ```

   > **You should see** your `.sql` files (and a `README.md`).

These files contain real data. They are excluded from Git, so they will
never be uploaded back to GitHub by accident.

## 10. Create the settings file

All settings live in a file called `.env`. You make it from the example
file.

```bash
cd /opt/apl-backend
cp .env.example .env
```

**Make four random passwords.** Run this four times; each prints a
different long random value. Copy each into a temporary note.

```bash
openssl rand -hex 32
```

> **You should see** a line of 64 letters and digits, such as
> `9f2c4e...`.

**Open the file:**

```bash
nano .env
```

This opens a simple text editor. Move with the arrow keys. **Ctrl + W**
searches for text (type part of a line, press **Enter**). To paste, right-
click (Windows) or **Cmd + V** (Mac). To save: **Ctrl + O**, then **Enter**.
To leave: **Ctrl + X**.

**Change these lines.** Each line is `NAME=value`, with no spaces around
`=` and no quote marks unless shown.

| Line in the file | Change it to | Why |
|---|---|---|
| `DEBUG=true` | `DEBUG=false` | Production mode. **Important.** |
| `SECRET_KEY=change-me-to-a-random-secret` | `SECRET_KEY=` your **first** random value | Signs everyone's logins |
| `POSTGRES_PASSWORD=` | your **second** value | The database password |
| `STORAGE_ACCESS_KEY=` | your **third** value | The photo storage's user name |
| `STORAGE_SECRET_KEY=` | your **fourth** value | The photo storage's password |
| `PUBLIC_MEDIA_URL=http://localhost:9000/apl-media` | `PUBLIC_MEDIA_URL=https://media.example.org/apl-media` | Where phones load photos from |
| `COMPOSE_PROFILES=` | `COMPOSE_PROFILES=https` | Starts Caddy for HTTPS |
| `API_DOMAIN=` | `API_DOMAIN=api.example.org` | |
| `MEDIA_DOMAIN=` | `MEDIA_DOMAIN=media.example.org` | |
| `ALLOWED_ORIGINS=[...]` | your website's address(es), for example `ALLOWED_ORIGINS=["https://example.org","https://www.example.org"]` | Lets your website call the API. Keep the square brackets and quote marks exactly as shown. If you have no website, leave the line as it is. |

Leave the email lines for now (step 14) and everything else as it is.

Save (**Ctrl + O**, **Enter**) and close (**Ctrl + X**). Then make the file
readable only by root, since it holds passwords:

```bash
chmod 600 .env
```

**Keep a copy of these values** in a password manager. If the server is
lost, you need them to restore from a backup.

## 11. Start the backend

Check once more that `nslookup` from step 5 shows the server's address for
both names. Then:

```bash
cd /opt/apl-backend
docker compose up -d --build
```

What this does: builds the API from the code, then starts every part in the
background. The **first time** it takes 10 to 20 minutes on a small server.
Later starts take seconds.

> **You should see** it end with lines such as
> `Container apl-backend-caddy-1 Started`.

Watch the start-up messages:

```bash
docker compose logs -f migrate app caddy
```

The `-f` keeps showing new lines as they come. Look for:

- from **migrate**: `migration_applied` and `migrations_done`: the
  database is ready;
- from **app**: `storage_backend` with `"backend": "s3"`: photo storage is
  connected;
- from **caddy**: `certificate obtained successfully` for each of your two
  domains: HTTPS works.

Press **Ctrl + C** to stop watching. This does **not** stop the backend.

Check the parts:

```bash
docker compose ps -a
```

> **You should see** `app`, `caddy`, `postgres`, `redis` and `storage` as
> **running**, and `migrate`, `storage-init` and `storage-permissions` as
> **Exited (0)** (one-time setup steps that finished successfully).

**Open in your browser:**

| Address | You should see |
|---|---|
| `https://api.example.org/health` | `{"status":"ok"}`, with a padlock in the address bar |
| `https://api.example.org/health/ready` | `{"status":"ok","database":"ok"}` |
| `https://api.example.org/api/v1/plants/count` | The number of plants in the library |
| `https://api.example.org/docs` | The page used for administrator tasks |

If something is not right, see [section 19](#19-if-something-goes-wrong).

## 12. Create your administrator account

Follow [Run on your computer, step 10](run-on-your-computer.md#10-create-your-administrator-account),
with two differences:

- Use `https://api.example.org/docs` instead of `http://localhost:8000/docs`.
- Run the command that makes you an administrator **on the server**, in
  `/opt/apl-backend`:

  ```bash
  docker compose exec postgres psql -U postgres -d apl_db -c "UPDATE app_users SET role = 'admin' WHERE email = 'you@example.org';"
  ```

  > **You should see** `UPDATE 1`.

After that, you can make other people administrators from the docs page:
see [Everyday tasks](everyday-tasks.md#5-adding-and-removing-administrators).

## 13. Move the existing photos

**Skip this if** your database files contain no photos from the original
deployment.

The plant photos in the hand-over database may still point at the original
project's storage, which belongs to the previous team and may be switched
off. This copies each photo into your server's storage, removes any GPS
location data on the way, and updates the database. Do it while the old
photos are still online.

```bash
cd /opt/apl-backend
docker compose run --rm --no-deps app python -m app.db.migrate_images --dry-run
```

This only **reports** what it would do. You should see lines containing
`image_would_move`, then `image_migration_done`. Now do it for real:

```bash
docker compose run --rm --no-deps app python -m app.db.migrate_images
```

> **You should see** a line per photo with `image_moved`, and at the end
> `image_migration_done` with `failed=0`.

If some photos failed, run the same command again later: it skips those
already moved. Check the result by opening any plant on
`https://api.example.org/api/v1/plants/1`: its `image_url` now starts with
`https://media.example.org/`.

## 14. Set up email

When an administrator approves or rejects a submission, the contributor
gets an email. Without these settings the backend still works; it only
writes the email into its log instead of sending it.

You need a mailbox on your domain, for example `info@example.org`, from any
email provider (Google Workspace, Microsoft 365, Namecheap Private Email,
Zoho, ...). From the provider's help pages, find its **SMTP** settings:
server name, port, user name and password.

On the server, `nano .env` and set:

```
SMTP_HOST=smtp.your-provider.com
SMTP_PORT=587
SMTP_USERNAME=info@example.org
SMTP_PASSWORD=the-mailbox-password
EMAIL_FROM=Australian Poisonous Plants <info@example.org>
```

- `EMAIL_FROM` must use the **same address** as `SMTP_USERNAME`.
- `587` is the usual port. If your provider says 465, use that.

Apply the change:

```bash
docker compose up -d
```

So that the emails do not land in spam, your provider's help pages explain
which **SPF**, **DKIM** and **DMARC** records to add to your domain's DNS
(the same place as step 5). Add all three.

**Test it:** submit something with your own email address, then reject it
from the docs page. The email should arrive within a minute. If it does
not, check the log:

```bash
docker compose logs app | grep email_
```

`email_sent` means it was handed to your provider. `email_send_failed`
or `email_send_error` shows the reason. Some server providers block
outgoing email ports on new accounts (DigitalOcean does). If the error is a
timeout, ask the provider's support to unblock port 587, or use the
alternative in [DEPLOY.md, section 6](../DEPLOY.md#6-email).

## 15. Connect the app and the website

- **Mobile app:** its developer changes the API address in the app's code
  (`src/constants/api.ts`, `API_BASE_URL`) to `https://api.example.org`,
  then publishes a new version of the app. Until users install that
  version, their app keeps talking to the old address.
- **Website:** make sure its address is in `ALLOWED_ORIGINS` (step 10).
  After changing it, run `docker compose up -d`.

## 16. Backups

Two things hold data: the **database** (accounts, plants, submissions) and
the **photos**. Set up both protections below.

### 16.1 Server backups from your provider

If you turned on automatic backups in step 4, your provider keeps copies of
the whole server. This protects against losing the server, but restores
everything at once, to the time of the copy.

### 16.2 Nightly backup of the data

The backend includes a backup script. Try it once by hand:

```bash
cd /opt/apl-backend
sh deploy/backup.sh
```

> **You should see** lines starting with `database:` and `photos:` and
> ending with `backup finished`. The backups are in
> `/opt/apl-backend/backups`: `db/` holds one database file per run (kept
> for 14 days) and `photos/` a copy of every photo.

Make it run every night at 3 am:

```bash
crontab -e
```

If asked to choose an editor, type `1` (nano) and press **Enter**. Go to the
bottom of the file and add this line:

```
0 3 * * * cd /opt/apl-backend && sh deploy/backup.sh >> /var/log/apl-backup.log 2>&1
```

Save (**Ctrl + O**, **Enter**) and close (**Ctrl + X**).

> **You should see** `crontab: installing new crontab`.

The next day, check that it ran:

```bash
tail /var/log/apl-backup.log
```

### 16.3 Keep a copy somewhere else

A backup on the same server is lost with the server. Every month or so,
download the backups to your computer. **On your computer:**

**Windows:**
```powershell
scp -r root@SERVER_IP:/opt/apl-backend/backups $HOME\Documents\apl-backups
```

**Mac:**
```bash
scp -r root@SERVER_IP:/opt/apl-backend/backups ~/Documents/apl-backups
```

### 16.4 Restoring from a backup

Only do this when data has really been lost. On the server, in
`/opt/apl-backend`:

**The database**, from one of the files in `backups/db/` (the newest is
listed last by `ls backups/db`):

```bash
docker compose stop app
docker compose exec -T postgres pg_restore -U postgres -d apl_db --clean --if-exists < backups/db/apl_db_2026-10-23_0300.dump
docker compose start app
```

Replace the file name with the one you want. The middle command replaces
the database's contents with the backup's. It prints nothing when it
succeeds.

**The photos**, from `backups/photos/` (puts back any missing photo; does
not delete anything):

```bash
docker compose run --rm photo-backup s3 sync /backup s3://apl-media --endpoint-url http://storage:9000 --only-show-errors
```

Then check `https://api.example.org/health/ready` and a few plants in the
app.

## 17. Updating to a new version

When the code on GitHub changes:

```bash
cd /opt/apl-backend
sh deploy/backup.sh
git pull
docker compose up -d --build
docker compose logs migrate
```

In order: take a backup first; download the new code; rebuild and restart
(only what changed is restarted, and database changes are applied
automatically before the new version starts); check the database step
ended with `migrations_done`.

**To go back** to the previous version if the new one misbehaves:

```bash
git log --oneline -5
git checkout <the code of the previous version, e.g. 5d8c48b>
docker compose up -d --build
```

Database changes made by the new version are not undone, but each one is
written so that the previous version still works with it. To return to the
latest version later, run `git checkout main`, then
`docker compose up -d --build`.

## 18. Everyday commands

Run these on the server, in `/opt/apl-backend`.

| To... | Run |
|---|---|
| See whether every part is running | `docker compose ps -a` |
| See the API's latest log lines | `docker compose logs --tail 100 app` |
| Follow the log live (Ctrl + C to stop) | `docker compose logs -f app` |
| Restart the API | `docker compose restart app` |
| Apply a changed `.env` | `docker compose up -d` |
| Stop everything | `docker compose stop` |
| Start everything again | `docker compose up -d` |
| See free disk space | `df -h /` |
| See Docker's disk use | `docker system df` |
| Remove old, unused images (frees space) | `docker image prune -f` |

The backend starts by itself when the server restarts.

Never run `docker compose down -v` on the server: the `-v` deletes the
database and all photos.

## 19. If something goes wrong

Start with:

```bash
cd /opt/apl-backend
docker compose ps -a
docker compose logs --tail 50 caddy
docker compose logs --tail 50 app
```

| What you see | What it means and what to do |
|---|---|
| The browser says the site "can't be reached" | Check `nslookup api.example.org` shows `SERVER_IP` (step 5), the firewall allows 80 and 443 (`ufw status`), and `caddy` is running. On Lightsail or Azure, also open ports 80 and 443 in the provider's own firewall. |
| Certificate warning in the browser, or the caddy log shows `could not get certificate` / `challenge failed` | Caddy could not prove it owns the domain: usually DNS not pointing at the server yet, or ports 80/443 blocked, or Cloudflare's proxy turned on. Fix that, then `docker compose restart caddy`. Caddy retries by itself, but after many failures Let's Encrypt makes it wait an hour. |
| caddy keeps restarting and its log says `Set API_DOMAIN and MEDIA_DOMAIN in .env first` | Fill in both lines in `.env` (step 10), then `docker compose up -d`. |
| "required variable ... is missing a value" | A required line in `.env` is empty. Fill it in (step 10). |
| `migrate` shows **Exited (1)**, and `docker compose logs migrate` says `schema_missing` | The database files were not loaded. Check them in `schema/`. Because the database was already created empty, it must be recreated: **only on a fresh install with no real data**, run `docker compose down -v`, then `docker compose up -d --build`. |
| `/health` works but `/health/ready` says `unavailable` | The database is not answering. Look at `docker compose logs --tail 50 postgres`, and check free disk space with `df -h /`. |
| Photos do not load | Open one photo address directly. A 404 means the photo is missing: run step 13 if not done. If `media.example.org` does not load at all, see the first two rows. |
| A new account or plant fails with "duplicate key" | Run the database step again: `docker compose run --rm migrate`. It fixes the counters that hand out new ids. |
| Uploads answer 429 "Too many uploads" | One address made more than 20 uploads in an hour. To allow more, add `SUBMISSION_RATE_LIMIT=60` to `.env`, then `docker compose up -d`. |
| The server is slow, or the API restarts by itself | It may be short of memory: check with `free -h`. Add swap (step 7.3) or move to a larger server size in your provider's dashboard. |
| The disk is full | `df -h /` shows it. Run `docker image prune -f`, delete old files in `backups/db/`, or enlarge the disk in your provider's dashboard. |

The technical reference for every setting is in [DEPLOY.md](../DEPLOY.md).
Running the service day to day is covered in
[Everyday tasks](everyday-tasks.md).
