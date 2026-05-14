# Deploying APL Backend to Google Cloud Run

This guide walks you through deploying the APL Backend API to Google Cloud Run with automated CI/CD via Cloud Build. All commands are written for **Windows PowerShell**.

## Prerequisites

- Google Cloud project with billing enabled
- [Google Cloud CLI (gcloud)](https://cloud.google.com/sdk/docs/install) installed and authenticated
- Docker installed locally (for testing builds)

## One-Time Setup

### 1. Configure gcloud CLI

```powershell
# Login to Google Cloud
gcloud auth login

# Set your project
gcloud config set project YOUR_PROJECT_ID

# Enable required APIs (all on one line, space-separated)
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com
```

### 2. Create Artifact Registry repository

This is where your Docker images will be stored.

```powershell
gcloud artifacts repositories create apl-backend --repository-format=docker --location=asia-southeast1 --description="APL Backend Docker images"
```

### 3. Store secrets in Secret Manager

Cloud Run reads sensitive config from Secret Manager instead of .env files.

```powershell
# Database URL (your Supabase connection string)
# First, write the value to a temp file, then create the secret from it
"postgresql+asyncpg://postgres:PoisonousPlant2026@db.nxiokqrqxtjjqjiexknj.supabase.co:5432/postgres" | Out-File -Encoding utf8NoBOM -NoNewline secret-db.txt
gcloud secrets create database-url --data-file=secret-db.txt
Remove-Item secret-db.txt

# JWT secret key (generate a random one)
python -c "import secrets; print(secrets.token_urlsafe(32), end='')" | Out-File -Encoding utf8NoBOM -NoNewline secret-key.txt
gcloud secrets create secret-key --data-file=secret-key.txt
Remove-Item secret-key.txt

# Redis URL (optional — only create if you plan to use Redis)
# "redis://your-redis-host:6379/0" | Out-File -Encoding utf8NoBOM -NoNewline secret-redis.txt
# gcloud secrets create redis-url --data-file=secret-redis.txt
# Remove-Item secret-redis.txt
```

### 4. Grant Cloud Run access to secrets

```powershell
# Get the project number
$PROJECT_NUMBER = gcloud projects describe YOUR_PROJECT_ID --format="value(projectNumber)"

# Grant secret access to the default compute service account
gcloud secrets add-iam-policy-binding database-url --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" --role="roles/secretmanager.secretAccessor"

gcloud secrets add-iam-policy-binding secret-key --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" --role="roles/secretmanager.secretAccessor"

# Only run this if you created the redis-url secret
# gcloud secrets add-iam-policy-binding redis-url --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" --role="roles/secretmanager.secretAccessor"
```

## Deploy

### Option A: Manual deploy (first time or one-off)

```powershell
# Build and push the image (run from the project root directory)
cd C:\Users\71404\Desktop\apl_backend
gcloud builds submit --config=cloudbuild.yaml
```

### Option B: Set up CI/CD with Cloud Build triggers

Connect your GitHub/GitLab repo so every push to `main` auto-deploys:

```powershell
gcloud builds triggers create github --name="apl-backend-deploy" --repo-owner="YOUR_GITHUB_USERNAME" --repo-name="apl_backend" --branch-pattern="^main$" --build-config="cloudbuild.yaml" --region="asia-southeast1"
```

Or set this up in the [Cloud Build Triggers UI](https://console.cloud.google.com/cloud-build/triggers).

## Verify Deployment

```powershell
# Get the service URL
gcloud run services describe apl-backend --region=asia-southeast1 --format="value(status.url)"

# Test health endpoint (PowerShell)
Invoke-RestMethod -Uri "https://YOUR_SERVICE_URL/health"
# Expected: status ok

# Open Swagger docs in browser
Start-Process "https://YOUR_SERVICE_URL/docs"
```

## Useful Commands

```powershell
# View logs
gcloud run services logs read apl-backend --region=asia-southeast1

# Update environment variables
gcloud run services update apl-backend --region=asia-southeast1 --set-env-vars="DEBUG=false"

# Scale settings
gcloud run services update apl-backend --region=asia-southeast1 --min-instances=1 --max-instances=20

# View current config
gcloud run services describe apl-backend --region=asia-southeast1

# Delete the service (careful!)
gcloud run services delete apl-backend --region=asia-southeast1
```

## Cost Estimation

Cloud Run charges per request and per vCPU/memory-second:

| Usage Level | Estimated Monthly Cost |
|---|---|
| Light (< 100k requests) | Free tier covers it |
| Moderate (100k–1M requests) | ~$5–$20 |
| Heavy (1M+ requests) | ~$20–$100+ |

Cloud Run's free tier includes 2 million requests/month and 360,000 vCPU-seconds/month.

## Troubleshooting

**Build fails with "permission denied"**

Make sure Cloud Build service account has the required roles:

```powershell
$PROJECT_NUMBER = gcloud projects describe YOUR_PROJECT_ID --format="value(projectNumber)"

gcloud projects add-iam-policy-binding YOUR_PROJECT_ID --member="serviceAccount:${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com" --role="roles/run.admin"

gcloud projects add-iam-policy-binding YOUR_PROJECT_ID --member="serviceAccount:${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com" --role="roles/iam.serviceAccountUser"
```

**Container fails to start**

Check logs: `gcloud run services logs read apl-backend --region=asia-southeast1`

Common cause: DATABASE_URL secret not set or unreachable from Cloud Run.

**Cold start too slow**

Set minimum instances to 1 to keep one instance always warm (~$10–15/month extra):

```powershell
gcloud run services update apl-backend --region=asia-southeast1 --min-instances=1
```
