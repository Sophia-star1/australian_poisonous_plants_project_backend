# Deploy on Google Cloud (Cloud Run + Supabase)

This guide puts the backend online using two managed services, so there is
no server for you to look after:

- **Supabase** keeps the **database** and the **photos**.
- **Google Cloud Run** runs the **API**. It starts more copies when busy,
  stops them when idle, and gives you an `https://` address.
- **Google Cloud Build** turns the code into a runnable package and puts
  it on Cloud Run, by hand or automatically whenever the code on GitHub
  changes.

This is how the original project team ran it. No programming knowledge is
needed: every command is given in full, with what it does and what you
should see. Plan about 2 to 3 hours the first time.

If you would rather run everything on one server you rent, see
[Deploy on your own server](deploy-on-your-own-server.md).

## Contents

1. [What you need and what it costs](#1-what-you-need-and-what-it-costs)
2. [Create the Supabase project](#2-create-the-supabase-project)
3. [Load the database](#3-load-the-database)
4. [Collect three values from Supabase](#4-collect-three-values-from-supabase)
5. [Create the Google Cloud project and install its tool](#5-create-the-google-cloud-project-and-install-its-tool)
6. [Prepare the code's deployment settings](#6-prepare-the-codes-deployment-settings)
7. [Turn on the Google services](#7-turn-on-the-google-services)
8. [Store the passwords in Secret Manager](#8-store-the-passwords-in-secret-manager)
9. [Give the build its permissions](#9-give-the-build-its-permissions)
10. [Deploy](#10-deploy)
11. [Lock the database against direct access](#11-lock-the-database-against-direct-access)
12. [Create your administrator account](#12-create-your-administrator-account)
13. [Move the existing photos](#13-move-the-existing-photos)
14. [Deploy automatically from GitHub](#14-deploy-automatically-from-github)
15. [Use your own domain (optional)](#15-use-your-own-domain-optional)
16. [Connect the app and the website](#16-connect-the-app-and-the-website)
17. [Logs, updates and going back](#17-logs-updates-and-going-back)
18. [If something goes wrong](#18-if-something-goes-wrong)

---

## 1. What you need and what it costs

- **A Google account** with a payment card, for Google Cloud.
- **A Supabase account** (<https://supabase.com>; you can sign in with
  GitHub).
- **The database files** from the hand-over (files ending in `.sql`).
- **The code**, downloaded to your computer: see
  [Run on your computer, step 5](run-on-your-computer.md#5-get-the-code).
- **A terminal** on your computer: **PowerShell** on Windows, **Terminal**
  on a Mac (see
  [Run on your computer, step 3](run-on-your-computer.md#3-open-a-terminal)).
- Optional: an email mailbox the backend can send from.

**Costs, roughly:**

| Service | Cost |
|---|---|
| Supabase | Free plan for trying. The free plan **pauses a project after about a week without use**, which takes the app offline, so for real users use the **Pro plan** (about US$25 a month). |
| Cloud Run | Charged per use. At this app's traffic, usually within the free allowance or a few dollars a month. |
| Cloud Build, Artifact Registry, Secret Manager | Usually cents a month. |

Google lets you set a **budget alert** (Billing → Budgets & alerts) so you
get an email if spending passes an amount you choose. Set one.

Throughout this guide, replace:

| Placeholder | With |
|---|---|
| `PROJECT_ID` | Your Google Cloud project's ID (step 5) |
| `REGION` | The Google Cloud region you choose (step 5) |

## 2. Create the Supabase project

1. Sign in at <https://supabase.com/dashboard> and click **New project**.
2. **Name:** for example `apl`.
3. **Database password:** click **Generate a password**, then **copy it and
   keep it safe** (a password manager is ideal). You need it in step 4 and
   cannot see it again later.
4. **Region:** close to your users, for example **Oceania (Sydney)** or
   **Southeast Asia (Singapore)**. Pick the Google Cloud region in step 5
   close to the same place.
5. Click **Create new project** and wait about two minutes until the
   dashboard is ready.

## 3. Load the database

The hand-over `.sql` files create the tables and fill in the plant library.
Load them in order, starting with the one that creates the tables (usually
numbered `01_...`).

For each file:

1. In the Supabase dashboard's left menu, open **SQL Editor**, and click
   **New query** (or the **+** button).
2. Open the `.sql` file on your computer with **Notepad** (Windows:
   right-click → *Open with* → *Notepad*) or **TextEdit** (Mac). Select all
   (**Ctrl + A** / **Cmd + A**) and copy.
3. Paste it into the SQL Editor and click **Run** (or **Ctrl + Enter**).

> **You should see** "Success. No rows returned" (or a row count). If a
> warning asks you to confirm a "destructive operation", read it and
> confirm: the database is still empty.
>
> If a file is too large for the editor (the editor becomes very slow or
> shows an error about size), use the alternative at the end of this
> section.

**Check:** in the left menu, open **Table Editor**.

> **You should see** tables including `plants_v2`, `plant_images_v2`,
> `submissions`, `submission_images` and `app_users`. Click `plants_v2` to
> see the plants.

**Alternative for large files** (needs Docker Desktop from
[Run on your computer, step 4](run-on-your-computer.md#4-install-docker-desktop)):
put the files in a folder, open a terminal in that folder, and run for each
file (with the connection string from step 4, *Session pooler* or *Direct*
version, in quotes, using `postgresql://` at the start):

**Windows:**
```powershell
docker run --rm -v "${PWD}:/sql" postgres:16 psql "postgresql://postgres....:YOUR-PASSWORD@....supabase.com:5432/postgres" -v ON_ERROR_STOP=1 -f /sql/01_schema.sql
```

**Mac:**
```bash
docker run --rm -v "$PWD:/sql" postgres:16 psql "postgresql://postgres....:YOUR-PASSWORD@....supabase.com:5432/postgres" -v ON_ERROR_STOP=1 -f /sql/01_schema.sql
```

## 4. Collect three values from Supabase

Write these three values into a temporary note. They are passwords: do not
email them or put them in a file that others can see.

**1. The database address (`DATABASE_URL`).**

1. At the top of the project dashboard, click **Connect**.
2. Under **Connection String**, find **Transaction pooler** and copy the
   address. It looks like
   `postgresql://postgres.abcdefgh:[YOUR-PASSWORD]@aws-0-ap-southeast-2.pooler.supabase.com:6543/postgres`.
3. Replace `[YOUR-PASSWORD]` (including the square brackets) with the
   database password from step 2.
4. Change the beginning from `postgresql://` to `postgresql+asyncpg://`.

The result looks like
`postgresql+asyncpg://postgres.abcdefgh:Xy7...@aws-0-ap-southeast-2.pooler.supabase.com:6543/postgres`.

**2. The project address (`SUPABASE_URL`).** In **Project Settings** (gear
icon) → **Data API** (or **API**), copy the **Project URL**, such as
`https://abcdefgh.supabase.co`.

**3. The secret key (`SUPABASE_SERVICE_ROLE_KEY`).** In **Project Settings**
→ **API Keys**, under **Secret keys**, create or reveal a key starting with
`sb_secret_` and copy it. (Older projects show it as the **service_role**
key under *Legacy API keys*; that works too.)

This key can read and change everything in the project. Only the backend
may have it: never put it in the app or the website.

The photo buckets do not need creating: the first deployment creates them.

## 5. Create the Google Cloud project and install its tool

**Create the project** (in your browser):

1. Go to <https://console.cloud.google.com> and sign in. Accept the terms if
   asked.
2. Set up billing: menu (☰) → **Billing** → **Link a billing account** /
   **Create account**, and add your card.
3. At the top of the page, click the project picker → **New project**.
   Name it `apl-backend` and click **Create**.
4. Note the **Project ID** shown under the name (for example
   `apl-backend-412345`). That is `PROJECT_ID`.

**Choose a region.** This is where the API runs, for example
`australia-southeast1` (Sydney) or `asia-southeast1` (Singapore), near the
Supabase region from step 2. That is `REGION`.

**Install the Google Cloud command-line tool** (`gcloud`):

**Windows:**

1. Download the installer from
   <https://dl.google.com/dl/cloudsdk/channels/rapid/GoogleCloudSDKInstaller.exe>
   and run it, keeping the defaults.
2. At the end, leave **Run 'gcloud init'** ticked and click **Finish**. A
   terminal window opens.

**Mac:**

1. Open <https://cloud.google.com/sdk/docs/install>, section *macOS*, and
   download the package for your Mac (**macOS ARM 64-bit** for Apple M
   chips, **x86_64** for Intel).
2. Double-click the downloaded file to unpack it, and move the
   `google-cloud-sdk` folder into your home folder.
3. In Terminal, run:
   ```bash
   ~/google-cloud-sdk/install.sh
   ```
   Answer `Y` to the questions, then **close Terminal and open a new one**.
4. Run `gcloud init`.

**In the `gcloud init` window** (both systems): choose to log in, sign in
with your Google account in the browser that opens, and when asked to pick
a project, choose the one you created. If asked for a default region, you
can skip it.

**Check:**

```bash
gcloud config list
```

> **You should see** your email after `account =` and your project ID after
> `project =`.

## 6. Prepare the code's deployment settings

The file `cloudbuild.yaml` in the code folder tells Google how to build and
run the backend. Its last part, under `substitutions:`, holds the settings
for your deployment.

Open it in a text editor (Windows: `notepad cloudbuild.yaml`; Mac:
`nano cloudbuild.yaml`, run in the code folder) and scroll to the end. Change
the values after the colons:

| Setting | Set it to |
|---|---|
| `_REGION` | Your `REGION`, for example `australia-southeast1` |
| `_SUPABASE_URL` | Your Supabase project address (step 4, value 2) |
| `_EMAIL_FROM` | The sender, for example `Australian Poisonous Plants <info@example.org>`. **No email yet?** Put `""` (two quote marks): emails are then written to the log instead of sent. |
| `_SMTP_HOST` | Your mail provider's SMTP server, for example `smtp.example.com`, or `""` for no email |
| `_SMTP_PORT` | `"587"` (keep the quote marks) unless your provider says otherwise. Keep a number here even with no email: an empty value stops the API from starting. |
| `_SMTP_USERNAME` | The mailbox address, the same as in `_EMAIL_FROM`, or `""` |
| `_ALLOWED_ORIGINS` | Your website's addresses, for example `'["https://example.org","https://www.example.org"]'`: keep the outer single quotes, square brackets and double quotes exactly. If you have no website, `'[]'`. |

Leave `_SERVICE_NAME` and `_REPO_NAME` as they are. Save the file.

If you use Git, also save the change to GitHub (`git commit -am "Deployment
settings"` then `git push`), so that automatic deployments (step 14) use it.

## 7. Turn on the Google services

In the terminal, go to the code folder (see
[Run on your computer, step 5](run-on-your-computer.md#go-into-the-folder)),
then run:

```bash
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com
```

This switches on Cloud Run, Cloud Build, Artifact Registry (where the built
package is stored) and Secret Manager (where passwords are kept). It takes
a minute.

Then create the place where the built package is stored, replacing `REGION`:

```bash
gcloud artifacts repositories create apl-backend --repository-format=docker --location=REGION
```

> **You should see** `Created repository [apl-backend]`.

## 8. Store the passwords in Secret Manager

The backend reads its passwords from Google's **Secret Manager**, never from
the code. Create five secrets in the browser, which avoids any typing
mistakes in the terminal:

1. Go to <https://console.cloud.google.com/security/secret-manager> (check
   your project is selected at the top).
2. Click **Create secret**. Enter the **Name** and the **Secret value** from
   the table below, leave the rest, and click **Create secret**.
3. Repeat for each row.

| Name (exactly) | Secret value |
|---|---|
| `secret-key` | A long random value. Make one with `openssl rand -hex 32` (Mac) or the `New-Secret` helper from [Run on your computer, step 7](run-on-your-computer.md#7-create-the-settings-file) (Windows). |
| `database-url` | The database address from step 4 (value 1), starting with `postgresql+asyncpg://` |
| `supabase-secret-key` | The secret key from step 4 (value 3) |
| `smtp-password` | The mailbox password, or the word `unused` if you have no email yet |
| `resend-api-key` | The word `unused` (an alternative email service; not needed) |

Paste values without spaces or line breaks before or after. All five must
exist, even the unused ones, or the deployment stops.

## 9. Give the build its permissions

Cloud Build works under a Google "service account" that needs permission to
read the secrets and to deploy. Run the block for your computer, after
replacing `PROJECT_ID` on the first line:

**Windows (PowerShell):**
```powershell
$PROJECT_ID = "PROJECT_ID"
$NUMBER = gcloud projects describe $PROJECT_ID --format="value(projectNumber)"
$SA = "serviceAccount:$NUMBER-compute@developer.gserviceaccount.com"
foreach ($ROLE in "secretmanager.secretAccessor", "run.admin", "iam.serviceAccountUser", "artifactregistry.writer", "logging.logWriter", "storage.objectViewer") {
  gcloud projects add-iam-policy-binding $PROJECT_ID --member=$SA --role="roles/$ROLE" --condition=None --quiet
}
```

**Mac:**
```bash
PROJECT_ID="PROJECT_ID"
NUMBER=$(gcloud projects describe "$PROJECT_ID" --format="value(projectNumber)")
SA="serviceAccount:${NUMBER}-compute@developer.gserviceaccount.com"
for ROLE in secretmanager.secretAccessor run.admin iam.serviceAccountUser artifactregistry.writer logging.logWriter storage.objectViewer; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="$SA" --role="roles/$ROLE" --condition=None --quiet
done
```

> **You should see** six blocks of text starting with `Updated IAM policy
> for project`. They list who has access; you do not need to read them.

## 10. Deploy

In the terminal, in the code folder:

```bash
gcloud builds submit --config=cloudbuild.yaml
```

What this does: uploads the code to Google, builds it, prepares the
database (applies the backend's changes and creates the three photo buckets
in Supabase), then starts the new version on Cloud Run. It takes 10 to 15
minutes and prints its progress as it goes.

> **You should see** it end with a line containing `SUCCESS`, and, a little
> above it, `Service [apl-backend] revision [...] has been deployed and is
> serving 100 percent of traffic`.

Get the service's address:

```bash
gcloud run services describe apl-backend --region=REGION --format="value(status.url)"
```

> **You should see** an address such as
> `https://apl-backend-abc123xyz-ts.a.run.app`. This is your **API address**.

Open in your browser, adding these to the end of the API address:

| Add | You should see |
|---|---|
| `/health` | `{"status":"ok"}` |
| `/health/ready` | `{"status":"ok","database":"ok"}` |
| `/api/v1/plants/count` | The number of plants |
| `/docs` | The page used for administrator tasks |

In Supabase, **Storage** in the left menu now shows three buckets:
`submission-images`, `plant-images-v2` and `avatars`.

## 11. Lock the database against direct access

Supabase also offers the database directly to the internet through its own
"Data API", protected per table by **Row Level Security** (RLS). The
backend does not use that route, so switch RLS on for every table: this
closes the route without affecting the backend (it connects as the tables'
owner, which RLS does not restrict).

In Supabase's **SQL Editor**, run:

```sql
DO $$
DECLARE t record;
BEGIN
  FOR t IN SELECT tablename FROM pg_tables WHERE schemaname = 'public' LOOP
    EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t.tablename);
  END LOOP;
END $$;
```

> **You should see** "Success. No rows returned". In **Table Editor**, the
> tables no longer show an "RLS disabled" warning.

Run it again whenever a later update adds a table.

## 12. Create your administrator account

1. Create your account on the docs page, as in
   [Run on your computer, step 10](run-on-your-computer.md#10-create-your-administrator-account),
   parts 1, 3 and 4, using `<API address>/docs`.
2. To make it an administrator, run this in Supabase's **SQL Editor**, with
   your email:

   ```sql
   UPDATE app_users SET role = 'admin' WHERE email = 'you@example.org' RETURNING email, role;
   ```

   > **You should see** one row with your email and `admin`. If no row
   > appears, the email did not match exactly: check for typos and capital
   > letters.

From then on, you can promote others from the docs page: see
[Everyday tasks](everyday-tasks.md#5-adding-and-removing-administrators).

## 13. Move the existing photos

**Skip this if** your database files contain no photos from the original
deployment.

The plant photos in the hand-over database may still point at the previous
team's Supabase project, which may be switched off. This copies each photo
into **your** Supabase storage, removes any GPS location data on the way,
and updates the database. Do it while the old photos are still online.

It runs on your computer and needs Docker Desktop (see
[Run on your computer, step 4](run-on-your-computer.md#4-install-docker-desktop)).

1. In the code folder, create a file called `migrate.env`
   (Windows: `notepad migrate.env`; Mac: `nano migrate.env`) containing
   these four lines, with your values from step 4:

   ```
   STORAGE_BACKEND=supabase
   DATABASE_URL=postgresql+asyncpg://postgres.abcdefgh:YOUR-PASSWORD@aws-0-....pooler.supabase.com:6543/postgres
   SUPABASE_URL=https://abcdefgh.supabase.co
   SUPABASE_SERVICE_ROLE_KEY=sb_secret_...
   ```

2. Build the backend on your computer (5 to 15 minutes the first time):

   ```bash
   docker build -t apl-backend .
   ```

3. See what would be moved (changes nothing):

   ```bash
   docker run --rm --env-file migrate.env apl-backend python -m app.db.migrate_images --dry-run
   ```

   > **You should see** lines with `image_would_move` and a final
   > `image_migration_done`.

4. Move them:

   ```bash
   docker run --rm --env-file migrate.env apl-backend python -m app.db.migrate_images
   ```

   > **You should see** `image_moved` lines and, at the end,
   > `image_migration_done` with `failed=0`. If some failed, run it again
   > later: it skips those already moved.

5. **Delete `migrate.env`** (it contains passwords):
   Windows `Remove-Item migrate.env`, Mac `rm migrate.env`.

## 14. Deploy automatically from GitHub

So far you deploy by running `gcloud builds submit` (step 10). Instead,
Google can deploy by itself every time the code on GitHub's `main` branch
changes.

1. Go to <https://console.cloud.google.com/cloud-build/triggers>, choose
   your `REGION` in the **Region** menu, and click **Create trigger**.
2. **Name:** `apl-backend-deploy`.
3. **Event:** *Push to a branch*.
4. **Source:** under *Repository*, choose **Connect new repository** (or
   **Link repository**) → **GitHub**, sign in to GitHub, allow the Google
   Cloud Build app access to **only** the backend repository, and select it.
5. **Branch:** `^main$`.
6. **Configuration:** *Cloud Build configuration file*, location
   `cloudbuild.yaml`.
7. **Service account:** the one ending in
   `-compute@developer.gserviceaccount.com`.
8. Click **Create**.

To test it, click **Run** next to the trigger. A build starts; follow it
under **History**. From now on, every change merged into `main` on GitHub
is deployed within about 10 minutes.

## 15. Use your own domain (optional)

Your API already works at its `run.app` address. To use, for example,
`api.example.org` instead:

1. Open <https://console.cloud.google.com/run/domains> and click **Add
   mapping**.
2. Choose the `apl-backend` service and enter `api.example.org`. If Google
   asks you to verify that you own the domain, follow its instructions (it
   gives you a DNS record to add at your domain's company).
3. Google shows one or more DNS records (type `CNAME`, `A` or `AAAA`). Add
   them in your domain's DNS settings exactly as shown.
4. Wait: the certificate for HTTPS can take from 15 minutes to a few hours.

Domain mapping is not offered in every region. If your region does not show
it, keep the `run.app` address; it is secure and stable.

## 16. Connect the app and the website

- **Mobile app:** its developer changes the API address in the app's code
  (`src/constants/api.ts`, `API_BASE_URL`) to your API address, then
  publishes a new version of the app.
- **Website:** its address must be in `_ALLOWED_ORIGINS` (step 6). After
  changing it, deploy again (step 10, or push to GitHub with step 14 set up).

## 17. Logs, updates and going back

**Logs** show what the backend is doing, including errors:
<https://console.cloud.google.com/run> → `apl-backend` → **Logs**. Or in the
terminal:

```bash
gcloud run services logs read apl-backend --region=REGION --limit=50
```

**Updating to a new version:** with step 14 set up, merging the change into
`main` on GitHub deploys it. Otherwise, download the new code and run
`gcloud builds submit --config=cloudbuild.yaml` again. Database changes
are applied automatically before the new version starts.

**Going back** to the previous version: <https://console.cloud.google.com/run>
→ `apl-backend` → **Revisions**. Each deployment is a revision. Select the
previous one, click **Manage traffic**, give it **100%**, and **Save**. It
takes effect within seconds.

**Backups:** the database is backed up by Supabase (daily on paid plans;
see *Database → Backups*). Photos are not: see
[OPERATIONS.md, Backups](../OPERATIONS.md#backups) for copying them.

**Changing a password:** in Secret Manager, open the secret → **New
version** → paste the new value → **Add new version**. Then deploy again
(step 10) so the service picks it up.

## 18. If something goes wrong

During a deployment, the error is in the build log: in the terminal output,
or at <https://console.cloud.google.com/cloud-build/builds> → the failed
build. Look at the step that failed.

| What you see | What it means and what to do |
|---|---|
| `PERMISSION_DENIED` or `does not have permission` during a build | Step 9 was not completed, or ran on another project. Run it again. |
| `Secret ... not found` or `Permission denied on secret` | A secret from step 8 is missing or misspelled. Names must match exactly. |
| The `migrate` step fails with `schema_missing` | The database is missing tables: step 3 was not finished. Load the files, then deploy again. |
| The `migrate` step fails with "password authentication failed" or "could not translate host name" | `database-url` is wrong. Rebuild it carefully (step 4, value 1) and add it as a new version of the secret (step 17, *Changing a password*). |
| `Repository "apl-backend" not found` | Step 7's repository is missing, or `_REGION` (step 6) differs from the region you created it in. |
| The deploy step fails mentioning `allUsers` or an organisation policy | Your Google organisation forbids public services. An administrator of the organisation must allow it, or use a project outside the organisation. |
| `/health` works but `/health/ready` is `unavailable` | The database is not answering. In Supabase, check the project is not **paused** (Free plan): click **Restore project**. |
| Photos do not load | Check the three buckets exist in Supabase **Storage** and are marked **Public**. If a bucket is private, the deployment logs `storage_bucket_private`: make it public in its settings. |
| The website gets "CORS" errors but the app works | The website's address is missing from `_ALLOWED_ORIGINS` (step 6). Add it exactly (with `https://`, no slash at the end) and deploy again. |
| Uploads answer 429 "Too many uploads" | More than 20 uploads from one address in an hour; see `SUBMISSION_RATE_LIMIT` in [DEPLOY.md](../DEPLOY.md#7-configuration-reference). |
| The first request after a quiet period is slow | Normal: Cloud Run starts the API on demand, taking a few seconds. To keep one copy always running (about US$10–15 a month more), open `cloudbuild.yaml`, find the line `- "--min-instances"` and change the `"0"` on the line below it to `"1"`, then deploy again. (Changing it in the Cloud Run console would be undone by the next deployment.) |

The technical reference for every setting is in [DEPLOY.md](../DEPLOY.md).
Running the service day to day is covered in
[Everyday tasks](everyday-tasks.md).
