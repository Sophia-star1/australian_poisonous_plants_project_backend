# Run the backend on your own computer

This guide gets the whole backend running on your own Windows or Mac
computer: the API, its database and its photo storage. Use it to try
everything out safely, or to test a change before it goes live. Nothing you
do here affects the real app or its users.

No programming knowledge is needed. Every command is given in full, and
each step tells you what you should see. The first time takes about 30 to
60 minutes, mostly waiting for downloads.

When you are ready to put the backend online for real users, follow
[Deploy on your own server](deploy-on-your-own-server.md) or
[Deploy on Google Cloud](deploy-on-google-cloud.md) instead.

## Contents

1. [What you need](#1-what-you-need)
2. [Words used in this guide](#2-words-used-in-this-guide)
3. [Open a terminal](#3-open-a-terminal)
4. [Install Docker Desktop](#4-install-docker-desktop)
5. [Get the code](#5-get-the-code)
6. [Add the database files](#6-add-the-database-files)
7. [Create the settings file](#7-create-the-settings-file)
8. [Start everything](#8-start-everything)
9. [Check that it works](#9-check-that-it-works)
10. [Create your administrator account](#10-create-your-administrator-account)
11. [Try a submission (optional)](#11-try-a-submission-optional)
12. [Stopping, starting and starting over](#12-stopping-starting-and-starting-over)
13. [If something goes wrong](#13-if-something-goes-wrong)

---

## 1. What you need

- **A computer** running Windows 10 or 11 (64-bit), or macOS 12 or newer,
  with at least 8 GB of memory and 10 GB of free disk space.
- **Permission to install programs** on it (an administrator account).
- **The database files** that came with the hand-over: one or more files
  ending in `.sql`. They contain the plant library. Without them the
  backend refuses to start (it would have no tables to work with).
- **Access to the code on GitHub** (the repository), or a ZIP file of it.
- An internet connection.

## 2. Words used in this guide

| Word | Meaning |
|---|---|
| **Terminal** | A window where you type commands instead of clicking. On Windows it is called **PowerShell**, on a Mac **Terminal**. |
| **Command** | One line of text you type (or paste) into the terminal, then press **Enter** to run. |
| **Docker** | A free program that runs other programs in sealed boxes called **containers**, so you do not have to install a database or Python yourself. |
| **Container** | One running part of the backend: the API, the database, the photo storage, and so on. |
| **Image** | The recipe a container is made from. The first start builds the API's image, which takes a while. |
| **`.env` file** | A small text file holding this installation's settings and passwords. You create it in step 7. |
| **API** | The backend itself: the service the mobile app and website talk to. |
| **`localhost`** | "This computer". `http://localhost:8000` means "port 8000 on this computer". |

## 3. Open a terminal

**Windows:** click **Start**, type `PowerShell`, and click **Windows
PowerShell**. A blue or black window opens with a line ending in `>`.

**Mac:** press **Cmd + Space**, type `Terminal`, and press **Enter**. A
window opens with a line ending in `%` or `$`.

How to use it:

- Commands in this guide are shown in grey boxes. Copy the whole line, paste
  it into the terminal (Windows: right-click; Mac: **Cmd + V**), and press
  **Enter**.
- A line starting with `#` is an explanation. You do not need to type it.
- When a guide box is marked **Windows** or **Mac**, use only the one for
  your computer.
- If a command seems to hang, wait: some take minutes. If you need to stop
  one, press **Ctrl + C**.

## 4. Install Docker Desktop

**Windows:**

1. Go to <https://www.docker.com/products/docker-desktop/> and click
   **Download for Windows – AMD64**. (Choose **ARM64** only if your PC has a
   Snapdragon/ARM processor, for example a Surface Pro X.)
2. Run the downloaded installer. Keep the option **Use WSL 2** ticked, then
   click **OK** and wait.
3. Restart the computer when asked.
4. Open **Docker Desktop** from the Start menu. Accept the terms. You can
   skip signing in.
5. Wait until the bottom-left corner of the Docker window says **Engine
   running** (green).

**Mac:**

1. Find out which processor your Mac has: Apple menu → **About This Mac**.
   If it says **Chip: Apple M1/M2/M3/M4**, you need the **Apple Silicon**
   version; if it says **Processor: Intel**, the **Intel chip** version.
2. Download it from <https://www.docker.com/products/docker-desktop/>.
3. Open the downloaded `.dmg` file and drag **Docker** into
   **Applications**.
4. Open **Docker** from Applications. Accept the terms and allow it to
   install its helper when asked (it may ask for your Mac password).
5. Wait until the whale icon in the menu bar stops animating, and the Docker
   window says **Engine running**.

**Check that it works.** In your terminal, run:

```bash
docker --version
docker compose version
```

> **You should see** two lines such as `Docker version 29.4.3` and
> `Docker Compose version v2.40.0`. The exact numbers do not matter.
>
> If you see "not recognized" or "command not found", close the terminal,
> make sure Docker Desktop is open and running, and open a new terminal.

Docker Desktop must be running whenever you use the backend on your
computer.

## 5. Get the code

Choose **one** of the two ways.

### Option A: download a ZIP (simplest)

1. Open the repository page on GitHub in your browser and sign in (the
   repository is private, so you need an account that has access).
2. Click the green **Code** button, then **Download ZIP**.
3. Unzip it into your **Documents** folder (Windows: right-click the file →
   **Extract All**; Mac: double-click the file).
4. Rename the unzipped folder to `apl-backend`. If you find a folder inside
   a folder with the same name, use the inner one: it is the one that
   contains a file called `docker-compose.yml`.

### Option B: with Git (makes updating easier later)

1. Install Git.
   - **Windows:** download it from <https://git-scm.com/download/win>, run
     the installer and keep every default option.
   - **Mac:** run `git --version` in Terminal. If Git is missing, macOS
     offers to install the "command line developer tools": click
     **Install**.
2. Run, replacing `<owner>/<repository>` with the repository's address on
   GitHub (as shown in your browser's address bar):

   **Windows:**
   ```powershell
   cd $HOME\Documents
   git clone https://github.com/<owner>/<repository>.git apl-backend
   ```

   **Mac:**
   ```bash
   cd ~/Documents
   git clone https://github.com/<owner>/<repository>.git apl-backend
   ```

   When asked for a password, GitHub needs an *access token*, not your
   account password. How to create one is described in
   [Deploy on your own server, step 8](deploy-on-your-own-server.md#8-get-the-code-onto-the-server).

### Go into the folder

Every command from now on must be run **inside the code folder**. Go there:

**Windows:**
```powershell
cd $HOME\Documents\apl-backend
dir
```

**Mac:**
```bash
cd ~/Documents/apl-backend
ls
```

> **You should see** a list that includes `docker-compose.yml`,
> `.env.example` (on Windows, `dir` may hide it; that is fine), `app`,
> `schema` and `docs`.
>
> If you open a new terminal later, run the `cd` line again first.

## 6. Add the database files

The backend starts with an empty database and fills it from the hand-over
`.sql` files, once, the first time it starts.

1. Copy the `.sql` files into the folder called **`schema`** inside
   `apl-backend` (use File Explorer or Finder: drag and drop).
2. The files run in **alphabetical order**, so their names must sort in the
   order they are meant to run. If they are not already numbered, rename
   them, for example `01_schema.sql`, `02_data.sql`. The file that creates
   the tables must come first.

Check:

**Windows:**
```powershell
dir schema
```

**Mac:**
```bash
ls schema
```

> **You should see** your `.sql` files next to a file called `README.md`.

## 7. Create the settings file

The settings live in a file called `.env`. You make it from the example
file, then fill in a few passwords.

**1. Make a copy of the example:**

**Windows:**
```powershell
Copy-Item .env.example .env
```

**Mac:**
```bash
cp .env.example .env
```

**2. Make four random passwords.** You will paste them into the file. Each
command prints a different long random value; run it four times and keep the
four values in a temporary note.

**Windows** (paste both lines; the first one only defines the helper):
```powershell
function New-Secret { $b = New-Object byte[] 24; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); ($b | ForEach-Object { $_.ToString("x2") }) -join "" }
New-Secret
```
Then run `New-Secret` three more times.

**Mac:**
```bash
openssl rand -hex 24
```
Run it four times.

> **You should see** a line of 48 letters and digits, such as
> `1efb96e30416250b135c693bc471e405cfc2d91102530817`.

**3. Open the file:**

**Windows:**
```powershell
notepad .env
```

**Mac:**
```bash
nano .env
```

On the Mac this opens a simple editor inside the terminal. Move around with
the arrow keys. To save, press **Ctrl + O** then **Enter**; to close, press
**Ctrl + X**. (Avoid TextEdit for this file: it can change quote marks and
break it.)

**4. Change these lines** (find them in the file; each is on its own line):

| Line | Change it to |
|---|---|
| `SECRET_KEY=change-me-to-a-random-secret` | `SECRET_KEY=` followed by your **first** random value |
| `POSTGRES_PASSWORD=` | `POSTGRES_PASSWORD=` followed by your **second** value |
| `STORAGE_ACCESS_KEY=` | `STORAGE_ACCESS_KEY=` followed by your **third** value |
| `STORAGE_SECRET_KEY=` | `STORAGE_SECRET_KEY=` followed by your **fourth** value |

For example, `POSTGRES_PASSWORD=cd1831f765c58ab166fddb7d0975709e229a09d1cc92c405`.
No spaces, no quote marks.

Leave everything else as it is. In particular, keep `COMPOSE_PROFILES`,
`API_DOMAIN` and `MEDIA_DOMAIN` empty: they are only for a server.

**5. Save and close** the file (Notepad: **File → Save**, then close the
window).

> **Windows only:** Notepad sometimes saves the file as `.env.txt`. Check
> with `dir .env*`: you must see a file called exactly `.env`. If you see
> `.env.txt`, rename it with `Rename-Item .env.txt .env`.

## 8. Start everything

Make sure Docker Desktop is running, then:

```bash
docker compose up -d --build
```

What this does: `up` starts every part of the backend; `--build` builds the
API from the code first; `-d` lets them keep running in the background
after the command finishes.

The **first time** this takes 5 to 15 minutes: Docker downloads about 2 GB
and builds the API. You will see many lines scroll by. Later starts take
seconds.

> **You should see** it finish with lines like:
>
> ```
>  ✔ Container apl-backend-migrate-1  Exited
>  ✔ Container apl-backend-app-1      Started
> ```
>
> If it stops with an error instead, see [section 13](#13-if-something-goes-wrong).

Now check that every part is up:

```bash
docker compose ps -a
```

> **You should see** `app`, `postgres`, `redis` and `storage` with the
> state **running**, and `migrate`, `storage-init` and
> `storage-permissions` as **Exited (0)**. Those three are one-time setup
> steps: "Exited (0)" means they finished successfully.

Finally, look at what the setup step did:

```bash
docker compose logs migrate
```

> **You should see** lines containing `migration_applied` and
> `migrations_done`. If you see `schema_missing`, the database files were
> not picked up: see [section 13](#13-if-something-goes-wrong).

## 9. Check that it works

Open these addresses in your web browser:

| Address | You should see |
|---|---|
| <http://localhost:8000/health> | `{"status":"ok"}`: the API is running. |
| <http://localhost:8000/health/ready> | `{"status":"ok","database":"ok"}`: it can reach its database. |
| <http://localhost:8000/api/v1/plants/count> | The number of plants in the library, from your database files. |
| <http://localhost:8000/docs> | A page listing everything the API can do. |

The last page, `/docs`, is where you will do administrator tasks. Each
grey or coloured bar is one action. Click a bar to open it, **Try it out**
to fill it in, and **Execute** to run it. The result appears underneath
under **Server response**.

## 10. Create your administrator account

An administrator can review submissions and manage plants. You create a
normal account first, then promote it.

**1. Sign up.** On <http://localhost:8000/docs>, find
**POST /api/v1/auth/signup** (under *auth*), click it, then **Try it out**.
Replace the example in the **Request body** box with your own details:

```json
{
  "email": "you@example.org",
  "password": "choose-a-long-password",
  "full_name": "Your Name"
}
```

The password needs at least 8 characters. Click **Execute**.

> **You should see** "Code **201**" under *Server response*. If you see
> 409, an account with that email already exists.

**2. Make it an administrator.** Back in the terminal (still in the code
folder), run this, with your email in place of `you@example.org`:

```bash
docker compose exec postgres psql -U postgres -d apl_db -c "UPDATE app_users SET role = 'admin' WHERE email = 'you@example.org';"
```

This runs one database command inside the database container.

> **You should see** `UPDATE 1`. If you see `UPDATE 0`, the email did not
> match exactly: check for typos and capital letters.

**3. Sign in on the docs page.** Open **POST /api/v1/auth/login**, **Try it
out**, enter your email and password, **Execute**. In the response, find
`"access_token": "eyJ..."` and copy the long text **between** the quote
marks.

Scroll to the top of the page and click the **Authorize** button (with a
padlock). Paste the token into the **Value** box, click **Authorize**, then
**Close**. You are now signed in on this page.

**4. Check.** Open **GET /api/v1/auth/me**, **Try it out**, **Execute**.

> **You should see** your account, with `"role": "admin"`.

The token expires after 30 minutes. When actions start answering 401, sign
in again (step 3).

## 11. Try a submission (optional)

This walks through what a contributor and a reviewer do in the app.

1. Open **POST /api/v1/submissions/**, **Try it out**. Under `photos`,
   click **Add string item** (if shown), then **Choose File**, and pick a
   photo of a plant (JPEG, PNG or WebP).
   Fill in `contributor_name` and `contributor_email` (any values; they are
   replaced by your account's details because you are signed in). Click
   **Execute**.

   > **You should see** Code **201**. In the response, `images` contains an
   > `image_url` starting with `http://localhost:9000/apl-media/`. Open it in
   > your browser to see the stored photo. If you get 422 "does not appear
   > to be a plant", the automatic check thought it was not a plant photo:
   > try another one.

   Note the submission's `id` (a long code like `7af53651-...`).

2. Review it as an administrator by following
   [Everyday tasks: reviewing submissions](everyday-tasks.md#3-reviewing-submissions).

## 12. Stopping, starting and starting over

Run these in the code folder:

| To... | Run | What happens to your data |
|---|---|---|
| Stop the backend | `docker compose stop` | Kept |
| Start it again | `docker compose start` | Kept |
| Apply a newer version of the code | `docker compose up -d --build` | Kept |
| Remove the containers | `docker compose down` | Kept (start again with `docker compose up -d`) |
| **Delete everything** and start from scratch | `docker compose down -v` | **Deleted**: database, accounts, photos |

`down -v` is the only way to make the database load the `.sql` files again
(for example after you replaced them).

When you have finished, you can quit Docker Desktop to free memory
(Windows: right-click the whale icon in the taskbar → **Quit Docker
Desktop**; Mac: whale icon in the menu bar → **Quit Docker Desktop**). The
backend stops with it and starts again the next time you run
`docker compose up -d`.

## 13. If something goes wrong

First, two commands that show what is happening:

```bash
docker compose ps -a
docker compose logs --tail 50 app
```

The second shows the API's last 50 log lines. Replace `app` with `migrate`,
`postgres` or `storage` to see another part.

| What you see | What it means and what to do |
|---|---|
| `docker` is "not recognized" / "command not found" | Docker Desktop is not installed, or the terminal was opened before installing it. Open Docker Desktop, then a new terminal. |
| "Cannot connect to the Docker daemon", "error during connect", "pipe/dockerDesktopLinuxEngine" | Docker Desktop is not running. Start it and wait for **Engine running**. |
| "required variable POSTGRES_PASSWORD is missing a value: set POSTGRES_PASSWORD in .env" (or STORAGE_...) | The `.env` file is missing, not saved, or that line is empty. Redo [step 7](#7-create-the-settings-file). On Windows check the file is not called `.env.txt`. |
| "Bind for ...:8000 failed: port is already allocated" | Another program already uses port 8000. Open `.env`, change `API_PORT=8000` to `API_PORT=8001`, run `docker compose up -d` again, and use `http://localhost:8001` instead of 8000. For port 9000, stop the other program that uses it. |
| `migrate` shows **Exited (1)** and its log says `schema_missing` | The database files were not loaded. Check they are in `schema` and end in `.sql`, then run `docker compose down -v` and `docker compose up -d --build` (this starts the database from scratch). |
| `migrate` log shows an SQL error such as `relation "..." does not exist` | The files ran in the wrong order: rename them so the one creating the tables sorts first, then `docker compose down -v` and `docker compose up -d --build`. |
| `postgres` log shows an error while loading a `.sql` file | The file has a problem the database could not process. Note the error and contact whoever produced the files. |
| A page in the browser does not load | Wait half a minute after starting and refresh. Check that `app` is **running** with `docker compose ps`, and look at its log. |
| The build stops with "no space left on device" | Free some disk space. In Docker Desktop: **Settings → Resources** shows where its disk lives; **Troubleshoot → Clean / Purge data** frees space (it also deletes your local data). |
| 401 "Invalid or expired token" on the docs page | Your sign-in expired. Sign in again ([step 10](#10-create-your-administrator-account), part 3). |
| 429 "Too many uploads" | More than 20 uploads in an hour. Wait, or add `SUBMISSION_RATE_LIMIT=1000` to `.env` and run `docker compose up -d`. |

Still stuck? Copy the output of `docker compose ps -a` and the relevant log
into a message to whoever maintains the system.
