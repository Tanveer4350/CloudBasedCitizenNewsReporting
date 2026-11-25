# Civic Press

A citizen news reporting and verification app. Citizens can submit reports with a category, location, and optional photo/video; editors can revise, approve, or reject reports; approved stories appear in the searchable public feed. It supports local SQLite mode and Supabase cloud database/private-storage mode.

## Run on Windows

Requires Python 3.10 or later.

```powershell
cd D:\CloudBasedCitizenNewsReporting
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py --create-editor
.\.venv\Scripts\python.exe app.py
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). Create citizen accounts in the app. The editor account is created separately by the command above; its password is entered privately in the terminal.

## Connect Supabase free tier

The app's backend stays on your computer, while report/account records and uploaded media are stored in Supabase.

1. Create a Supabase project.
2. In the Supabase SQL Editor, run [`supabase/schema.sql`](supabase/schema.sql).
3. In **Storage**, create a bucket named `civic-press-media`. Leave **Public bucket** turned off.
4. If the project already has the original Civic Press schema, run [`supabase/admin_role_migration.sql`](supabase/admin_role_migration.sql) in the Supabase SQL Editor to enable administrator accounts. For a new project, use the updated `schema.sql`.
5. In **Project Settings → API Keys**, copy the project URL and a server-only **secret key** (`sb_secret_...`). The legacy `service_role` key also works. Never use a publishable/anon key here: the server key has elevated access and must stay private.
6. Copy `.env.example` to `.env` in the project directory and edit the values locally. Put in the project URL and server-only secret key, and replace `NEWS_SECRET` with a random value of at least 32 characters. Do not commit `.env` or send the secret in chat:

```powershell
Copy-Item .env.example .env
# Open .env in a text editor and replace the example values.
.\.venv\Scripts\python.exe app.py --create-admin
.\.venv\Scripts\python.exe app.py
```

Both Supabase variables must be set to enable cloud mode; if both are unset, the app uses local SQLite and local uploads. Confirm cloud mode at `/api/health`, which should return `"storage": "supabase"`. Supabase's free plan currently includes 500 MB of database and 1 GB of file storage; free projects may pause after a week of inactivity. Check the [current Supabase limits](https://supabase.com/pricing).

Keep `NEWS_SECRET` the same every time the app starts, or existing sessions will be invalidated. For HTTPS deployments, set `COOKIE_SECURE=1`. The Supabase secret key belongs only in the backend environment; do not put it in browser code.

The app loads `.env` at startup. Keep that file local; it is excluded from Git.

## Keep reports available to everyone

Local mode stores reports in `news.db` and uploaded media in `uploads/` on this computer only. To share the same feed with other people, configure Supabase cloud mode and deploy this FastAPI app to an internet-accessible host; visitors must use that hosted URL. Reports approved by an administrator are then returned in the public feed. Creating an admin account is a one-time server command (`python app.py --create-admin`); registration never grants admin access. Editors can continue using the separate Editor desk.

No hosting provider can promise data will exist forever. Use a managed database/storage plan that meets your retention needs, set up and periodically test backups/exports, and keep the hosting account and Supabase project active. Supabase free projects may pause after inactivity.

## Local development

To keep login sessions valid across local server restarts, set a persistent secret before starting the server:

```powershell
$env:NEWS_SECRET = "replace-with-a-long-random-secret"
.\.venv\Scripts\python.exe app.py
```

The app listens on localhost by default.

## Included

- Responsive community-news feed with keyword, category, and location filters.
- Citizen registration/sign-in, report submission, optional image/video upload (8 MB maximum), and submission status tracking.
- Separate editor account provisioning; editors can edit submitted details, add a note, approve for publication, or reject.
- SQLite persistence by default, or Supabase Data API and private Storage when configured.
- Password hashing, signed HTTP-only session cookies, and role-checked editor routes.
- JSON API and interactive API docs at `/docs`.

## Project structure

```text
app.py          FastAPI routes, authentication, local/Supabase persistence
static/         Browser interface, styles, and client-side application
uploads/        Local report media (excluded from Git)
news.db         Local SQLite database (created at first launch; excluded from Git)
supabase/       SQL schema for the cloud database
```

## Current cloud scope

With Supabase configured, account/report records and report media are cloud-backed, but the FastAPI server still needs to be running on your computer. Existing local SQLite data is not automatically copied to Supabase. Hosting the API publicly is a separate deployment step.
