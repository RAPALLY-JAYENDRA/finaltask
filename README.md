# Enterprise Lead Research & Strategic Offering Platform

A unified, end-to-end B2B Lead Intelligence platform combining multi-channel contact resolution, deep multi-page domain scraping, 1024-dimensional dense vector offering matching across 462 canonical offerings, Senior Principal AI synthesis, and executive PDF dossier export.

---

## ⚡ Core Architecture & Pipeline

1. **Unified Lead & Inbound Intake (`app.py`)**:
   - Ingests Lead Name, Email, Phone, Company, Domain, Country, Stated Interests, and Inbound Messages.
2. **Contact & LinkedIn Resolution (`linkedin_resolver.py`, `search_client.py`)**:
   - Automated LinkedIn profile matching, email DNS/MX verification, and contact intelligence.
3. **Dual-Engine Company Website Crawler (`scraper.py`)**:
   - Fast concurrent multi-page extraction across enterprise subpages (`/about`, `/products`, `/solutions`, `/projects`, `/case-studies`, etc.) with anti-bot search fallback.
4. **1024-Dimensional Dense Vector Matcher (`service_catalog.py`, `catalog_embeddings.npz`)**:
   - Computes Cosine Similarity and TF-IDF term ranking against 462 canonical sector service definitions using `@cf/baai/bge-large-en-v1.5`.
5. **Senior Principal Executive AI Synthesis (`worker_ai.py`, `enricher.py`)**:
   - Extracts Delivered Projects, Active Operations, and Future Roadmaps with direct source citations.
   - Formulates tailored value propositions, executive email drafts, and objection handling playbooks.
6. **Persistence & PDF Export (`database.py`, `pdf_generator.py`)**:
   - Stores full dossiers in SQLite/PostgreSQL and exports multi-page confidential executive dossiers in PDF format.

---

## 🚀 Quickstart

### 1. Install Requirements
```bash
pip install -r requirements.txt
```

### 2. Configure Environment
Copy `.env.example` to `.env` and set your Cloudflare Worker URL or search API keys:
```bash
cp .env.example .env
```

### 3. Launch the Application
```bash
streamlit run app.py
```
"# finaltask" 
