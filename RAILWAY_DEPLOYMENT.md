# 🚀 Railway Deployment Guide: Enterprise Intelligence Engine

This guide walks you through deploying the unified Lead Research & Strategic Offering Matcher application on [Railway.app](https://railway.app).

---

## 📦 What Was Configured for Railway

All necessary production files are set up:

| File | Purpose |
|------|---------|
| `Procfile` | Declares the dynamic web process binding to `$PORT` |
| `railway.json` | Railway orchestration config (Nixpacks build & start command) |
| `nixpacks.toml` | Optimized Nixpacks build environment (Python 3.11 + GCC) |
| `Dockerfile` | Standalone container definition for Docker-based deployment |
| `requirements.txt` | Complete locked Python dependency list |
| `.streamlit/config.toml` | Headless, CORS-safe, production Streamlit settings |
| `.gitignore` | Prevents pushing secrets (`.env`) or temporary test files |

---

## 🛠️ Option 1: Deploy via GitHub (Recommended)

1. **Push your code to a GitHub Repository**:
   ```bash
   git init
   git add .
   git commit -m "feat: complete unified lead intelligence engine with React SaaS UI"
   git branch -M main
   git remote add origin https://github.com/<YOUR_USERNAME>/<YOUR_REPO_NAME>.git
   git push -u origin main
   ```

2. **Open Railway Dashboard**:
   - Go to [railway.app](https://railway.app) and sign in.
   - Click **+ New Project** &rarr; **Deploy from GitHub repo**.
   - Select your repository.

3. **Configure Environment Variables in Railway**:
   - Click on your deployed service &rarr; navigate to the **Variables** tab.
   - Add your environment variables (see table below).

4. **Generate a Public Domain**:
   - In your Railway Service Settings &rarr; **Networking** &rarr; click **Generate Domain**.
   - Your application will be live at `https://<your-subdomain>.up.railway.app`!

---

## 💻 Option 2: Deploy via Railway CLI

1. **Install Railway CLI**:
   ```bash
   npm i -g @railway/cli
   ```
2. **Login & Initialize**:
   ```bash
   railway login
   railway init
   ```
3. **Deploy**:
   ```bash
   railway up
   ```

---

## 🔑 Environment Variables to Set on Railway

Add these in the **Variables** tab on Railway:

| Variable | Description | Example / Default |
|----------|-------------|-------------------|
| `CLOUDFLARE_WORKER_URL` | Cloudflare Worker AI Engine endpoint | `https://lead-research-ai-worker.devika-worker.workers.dev` |
| `CF_AI_MODEL` | Cloudflare Llama model | `@cf/meta/llama-3.2-3b-instruct` |
| `CF_EMBEDDING_MODEL` | Cloudflare BGE 1024-dim model | `@cf/baai/bge-large-en-v1.5` |
| `CF_WORKER_AUTH_SECRET` | Secret token (if configured on worker) | *(Optional)* |
| `GOOGLE_API_KEY` | Google Custom Search API Key | *(Optional)* |
| `GOOGLE_CSE_ID` | Google Programmable Search Engine ID | *(Optional)* |
| `SERPER_API_KEY` | Serper API key for search fallback | *(Optional)* |
| `DATABASE_URL` | PostgreSQL connection string | *(Optional - defaults to SQLite)* |

---

## 🗄️ Optional: Add PostgreSQL Database on Railway

1. In your Railway project, click **+ New** &rarr; **Database** &rarr; **Add PostgreSQL**.
2. Railway will automatically expose `DATABASE_URL` to your app.
3. The app automatically detects `DATABASE_URL` and uses PostgreSQL for persistent storage!
