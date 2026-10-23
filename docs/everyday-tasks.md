# Everyday tasks

How to run the service once it is online: reviewing what contributors
send in, looking after the plant library and the administrator accounts,
and checking that all is well. Everything here is done in your web browser.

For the technical background on each topic, see
[OPERATIONS.md](../OPERATIONS.md).

## Contents

1. [The administration page](#1-the-administration-page)
2. [Signing in](#2-signing-in)
3. [Reviewing submissions](#3-reviewing-submissions)
4. [Correcting or deleting a plant](#4-correcting-or-deleting-a-plant)
5. [Adding and removing administrators](#5-adding-and-removing-administrators)
6. [Adjusting the automatic photo check](#6-adjusting-the-automatic-photo-check)
7. [Checking that everything is healthy](#7-checking-that-everything-is-healthy)
8. [A routine to follow](#8-a-routine-to-follow)
9. [What the response codes mean](#9-what-the-response-codes-mean)

---

## 1. The administration page

All administrator tasks are done on the API's built-in page:

```
<your API address>/docs
```

for example `https://api.example.org/docs`, or
`http://localhost:8000/docs` on your own computer.

The page lists every action the API offers, grouped by topic (*auth*,
*plants*, *submissions*, *users*, *admin*, ...). For each action:

1. Click its bar to open it. It shows what it needs and what it returns.
2. Click **Try it out** (on the right) to make the fields editable.
3. Fill in the fields, or edit the JSON text in **Request body**.
4. Click **Execute**.
5. Read the result below, under **Server response**: the **Code** (for
   example 200) and the **Response body**.

**JSON** is the text format the API uses: `{ }` holds named values
(`"name": value`), `[ ]` holds lists, text goes in double quotes, and items
are separated by commas. When you edit an example, keep every quote mark,
comma and bracket in place.

Every response has the same shape:

```json
{ "code": 200, "message": "success", "data": { ... } }
```

`data` holds the result; when something is refused, `message` says why.

## 2. Signing in

Most actions need you to be signed in as an administrator.

1. Open **POST /api/v1/auth/login** → **Try it out**.
2. In **Request body**, put your email and password:
   ```json
   { "email": "you@example.org", "password": "your-password" }
   ```
3. Click **Execute**. In the response, find `"access_token": "eyJ..."` and
   copy the long text **between** the quote marks (not the quote marks).
4. Scroll to the top of the page, click **Authorize** (padlock button),
   paste into **Value**, click **Authorize**, then **Close**.

You stay signed in on this page for **30 minutes**. When actions start
answering **401**, repeat these steps.

Never share the token: for 30 minutes it acts as you.

## 3. Reviewing submissions

Contributors send photos of plants with a description from the app. Each
photo is checked automatically ("is this a plant?"), then the submission
waits for an administrator to **approve** it (its photos join the plant
library) or **reject** it. The contributor is emailed the outcome either
way.

### Find what is waiting

1. Open **GET /api/v1/submissions/** → **Try it out**.
2. Set **status** to `pending`. Leave the rest. **Execute**.
3. The response lists submissions under `data.items`, newest first. For
   each one, note:

| Field | What it is |
|---|---|
| `id` | The submission's code, like `7af53651-73ed-4dea-90c0-17e08030dcec`. You need it to review. |
| `contributor_name`, `contributor_email` | Who sent it |
| `user_description` | What they wrote |
| `images` → `image_url` | The photos. Copy an address into a new browser tab to see it. |
| `location_state`, `latitude`, `longitude` | Where they say they saw it, if given |
| `ai_is_plant` | `true`: every photo looked clearly like a plant. `false`: at least one was uncertain, look carefully. `null`: the check was off. |
| `content_version` | A short code you must send back when you review (it proves you reviewed what is there now) |

`data.total` is how many there are. If there are more than 20, change
**page** to `2` and **Execute** again.

### Approve it as a new plant

Use this when the species is **not yet** in the library.

1. Open **PATCH /api/v1/submissions/{submission_id}** → **Try it out**.
2. **submission_id:** paste the submission's `id`.
3. **Request body:** replace the example with the following, and fill in
   what you know. Only `botanical_name` is required; the more you add, the
   more useful the entry. Remove lines you do not want to fill (and the
   comma before them if they were last).

```json
{
  "status": "approved",
  "expected_version": "PASTE content_version HERE",
  "reviewer_notes": "Thank you, a clear photo of the seed pods.",
  "plant": {
    "botanical_name": "Abrus precatorius",
    "family_name": "Fabaceae",
    "common_names": ["Crab's eye", "Rosary pea"],
    "origin_status": "Native",
    "distribution": ["QLD", "NT", "WA"],
    "habitat": "Coastal scrub and monsoon forest margins",
    "toxic_parts": ["Seeds"],
    "lethality": "High",
    "health_effects": "Severe vomiting and diarrhoea if seeds are chewed.",
    "toxicity_by_species": {
      "humans": "Toxic",
      "dogs": "Toxic",
      "cattle": "Unknown"
    }
  }
}
```

- `reviewer_notes` is included in the email to the contributor, so write
  it for them.
- In `toxicity_by_species`, each species is `"Toxic"` or `"Unknown"`, and
  only these names are accepted: `humans`, `cattle`, `horses`, `sheep`,
  `goats`, `pigs`, `poultry`, `dogs`, `cats`, `other`, plus `notes` for any
  free-text remark.

4. **Execute.**

> **You should see** Code **200** and `"message": "Submission reviewed."`.
> `data.plant_id` is the new plant's number. Check it at
> `<API address>/api/v1/plants/<plant_id>`.

### Approve it into a plant that already exists

Use this when the species **is** already in the library: the photos are
added to that plant's gallery.

1. Find the plant's number: open **GET /api/v1/plants/search** → **Try it
   out**, put the name in **q** (for example `Abrus`), **Execute**, and note
   the `id` of the right plant.
2. Open **PATCH /api/v1/submissions/{submission_id}** as above, with:

```json
{
  "status": "approved",
  "expected_version": "PASTE content_version HERE",
  "reviewer_notes": "Thank you, added to the existing entry.",
  "plant_id": 2
}
```

### Reject it

```json
{
  "status": "rejected",
  "expected_version": "PASTE content_version HERE",
  "reviewer_notes": "Thank you. The photo is too blurred to identify; please send a closer one."
}
```

### If the review is refused

| Code | Message mentions | Do this |
|---|---|---|
| 409 | the submission changed | The contributor edited it after you looked. List it again, check it, and use the new `content_version`. |
| 409 | already reviewed | Someone else reviewed it first. Nothing to do. |
| 409 | a plant with this name already exists | Approve it into that plant instead (see above). |
| 422 | a field | The JSON has a mistake, for example a missing comma or quote, or a value that is not allowed. The message names the field. |
| 401 | | Sign in again ([section 2](#2-signing-in)). |

## 4. Correcting or deleting a plant

**To correct an entry**, send only the fields that change:

1. Open **PATCH /api/v1/plants/{plant_id}** → **Try it out**.
2. **plant_id:** the plant's number (find it with the search, as above).
3. **Request body**, for example:
   ```json
   {
     "common_names": ["Crab's eye", "Rosary pea", "Jequirity bean"],
     "lethality": "High"
   }
   ```
4. **Execute.** Code 200 returns the updated entry.

**To delete an entry** (for example a duplicate or a test):

1. Open **DELETE /api/v1/plants/{plant_id}** → **Try it out**, enter the
   number, **Execute**.
2. The response says how many photos were deleted with it.

**Deleting cannot be undone.** The entry and its library photos are gone
(only a backup can bring them back). Submissions that were approved into
it keep their own photos.

## 5. Adding and removing administrators

1. Ask the person to create an account in the app (or with **POST
   /api/v1/auth/signup**).
2. Find their account number: **GET /api/v1/users/** → **Try it out** →
   **Execute**. Find their email in the list and note its `id`.
3. Open **PATCH /api/v1/users/{user_id}/role** → **Try it out**, enter the
   `id`, and send:
   ```json
   { "role": "admin" }
   ```
   To remove someone's administrator rights, send `{ "role": "user" }`.

It takes effect immediately; they do not need to sign in again. Two things
are refused on purpose, so the service is never left without an
administrator: removing your own rights, and removing the last
administrator.

## 6. Adjusting the automatic photo check

Every uploaded photo gets a score from 0 to 1 for how sure the check is
that it shows a plant:

- below **`reject_below`** (default **0.50**), the upload is refused and
  the contributor asked for another photo;
- between the two numbers, it is accepted, and the reviewer sees
  `ai_is_plant: false` as a hint to look carefully;
- at or above **`accept_at`** (default **0.70**), it is accepted.

If honest photos (close-ups of bark, seeds, dried specimens) are being
refused too often, lower `reject_below` a little. If too many non-plant
photos reach the review queue, raise it.

- **See the current values:** **GET /api/v1/admin/screening** → **Try it
  out** → **Execute**.
- **Change them:** **PATCH /api/v1/admin/screening** → **Try it out** →
  ```json
  { "reject_below": 0.4, "accept_at": 0.7 }
  ```
  → **Execute**. The change applies within about 30 seconds, without
  restarting anything.

## 7. Checking that everything is healthy

Open in your browser:

| Address | Healthy answer | If not |
|---|---|---|
| `<API address>/health` | `{"status":"ok"}` | The API is down. See the guide you deployed with, section "If something goes wrong". |
| `<API address>/health/ready` | `{"status":"ok","database":"ok"}` | The database is not answering. On Supabase's free plan, the project may be **paused**: open the Supabase dashboard and restore it. |

To be told automatically when it is down, sign up for a free uptime
monitor (for example UptimeRobot or Better Stack) and give it the
`/health/ready` address. It emails you if the address stops answering.

Where to read the logs:

- **Your own server:** `docker compose logs --tail 100 app`, on the server
  in `/opt/apl-backend`.
- **Google Cloud:** <https://console.cloud.google.com/run> → `apl-backend` →
  **Logs**.

## 8. A routine to follow

| How often | Task |
|---|---|
| Every few days | Review pending submissions ([section 3](#3-reviewing-submissions)). |
| Weekly | Open `/health/ready`. On your own server, check last night's backup ran: `tail /var/log/apl-backup.log`. |
| Monthly | Own server: copy the backups to your computer ([server guide, 16.3](deploy-on-your-own-server.md#163-keep-a-copy-somewhere-else)) and check free disk space (`df -h /`). Google Cloud: look at the bill and at Supabase's usage page. |
| When there is a new version | Update it ([own server, section 17](deploy-on-your-own-server.md#17-updating-to-a-new-version); Google Cloud: [section 17](deploy-on-google-cloud.md#17-logs-updates-and-going-back)). |
| Once a year | Renew the domain, and the GitHub access token on your own server ([server guide, step 8](deploy-on-your-own-server.md#8-get-the-code-onto-the-server)). |

## 9. What the response codes mean

| Code | Meaning |
|---|---|
| 200, 201 | Done. (201: something new was created.) |
| 401 | Not signed in, or the sign-in expired: [sign in again](#2-signing-in). |
| 403 | Signed in, but not allowed: for example not an administrator. |
| 404 | Not found: check the number or code you entered. |
| 409 | Conflicts with the current state: the message explains (see [If the review is refused](#if-the-review-is-refused)). |
| 422 | Something in what you sent is not valid: the message names it. |
| 429 | Too many attempts in a short time: wait and try again. |
| 500, 503 | A problem on the server side: check `/health/ready` and the logs. |
