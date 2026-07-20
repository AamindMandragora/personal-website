# Resume Generator IDE

Fork this repo, replace the contents of `cv_data.json` with your own background, and generate a full CV plus tailored one-page resumes from the browser.

The app is a VS Code-style frontend backed by a small Flask server that renders ATS-friendly PDFs with ReportLab. There is no LaTeX setup and no frontend build step. CV content lives in MongoDB (seeded once from `cv_data.json`); resume filter presets live in the browser’s `localStorage`. Editing the CV requires a shared password from `.env`.

## What You Can Customize

- MongoDB `cv.cv_data`: your name, education, research, experience, projects, skills, awards, and links (seeded from `cv_data.json` on first boot)
- `cv.pdf`: the full CV, regenerated when CV data is saved
- Local `.cfg` presets in the web IDE: targeted resume filters for industries, specific projects, and bullet limits (stored in `localStorage`, never written to the server)

## Features

- Full CV generation directly from `cv_data.json`
- Tailored resume generation from editable `.cfg` files
- Editor-only comments on research / experience / projects (and individual bullets) for collaborators with the shared password
- Copy/paste export and import of the full CV or a single entry as human-readable text or lossless JSON
- Project filtering by `industry=[...]` (also filters research, experience, coursework, awards, languages, and tools)
- Explicit project selection with `projects=[...]`
- Ordered selectors: if `projects` comes before `industry`, those projects are prioritized first; if `industry` comes first, industry-matched projects fill first
- Duplicate-safe project selection
- PDF rendering done server-side in pure Python

## Quick Start

```bash
pip install -r requirements.txt
# Ensure .env has MONGODB_URI, EDIT_PASSWORD, SESSION_SECRET
python server.py
```

Then open `http://localhost:5000`. Click **Edit CV** and enter `EDIT_PASSWORD` to change source data.

## How To Use It

1. Fork the repo.
2. Edit `cv_data.json` with your own content.
3. Start the server with `python server.py`.
4. Open the app in your browser.
5. View `cv.pdf` for your full CV.
6. Open or create a `.cfg` file and press `Run` to generate a tailored PDF.
7. In **Edit CV**, use **Export** / **Import** to share draft text with a friend, or leave **Comments** / **Note** threads on entries and bullets (saved separately from CV Save).

## Config Syntax

```cfg
# Ordered selectors are honored top-to-bottom.
projects=[malloc, audio_relay]
industry=[systems, ai_ml, networking]

include_projects=true
min_bullets=1
max_bullets=3
title=Systems Resume
```

Supported keys:

- `industry=[...]`
- `projects=[...]`
- `include_projects=true|false`
- `min_bullets=N`
- `max_bullets=N`
- `title=...` (PDF document metadata title shown in browser tabs and PDF readers; defaults to `{name} - {filename}`)
- `earliest_start_date=YYYY` (filters experiences whose start year is at least this value; default 2024; lower it to include older entries)
- `earliest_end_date=YYYY` (filters experiences whose end year is at least this value; default 2024; lower it to include older entries)

By default the résumé generator (including the precompiled CV) filters experiences to those that start and end in 2024 or later unless you explicitly lower `earliest_start_date`/`earliest_end_date`.

Built-in industry tags:

- `systems` — low-level C/C++, allocators, concurrency, system programming
- `networking` — sockets, relays, messaging protocols
- `ai_ml` — ML, LLMs, agents, generative AI
- `formal_methods` — verification, Lean4, Dafny, proof assistants
- `fullstack` — web apps, APIs, React, FastAPI
- `math` — pure math background, stats, modeling
- `pedagogy` — tutoring, teaching, educational tools
- `compilers` — lexers, parsers, compiler frontends
- `quant_finance` — trading, options pricing, financial engineering
- `security` — encryption, privacy-first systems

When `industry=[...]` is set, every tagged section is filtered to matching entries. Coursework, awards, languages, and tools each render on a single line (truncated with `...` if needed). Tailored resumes prune research, experience, and projects to fit one page.

Project IDs come from the entries in `cv_data.json` and are also listed in the sidebar of the frontend.

## Selector Ordering

The order of `projects` and `industry` in a `.cfg` file matters.

```cfg
projects=[lean4game]
industry=[systems]
```

This puts `lean4game` first, then fills the remaining project slots with matching `systems` projects.

```cfg
industry=[systems]
projects=[lean4game]
```

This puts `systems` projects first, then adds `lean4game` afterward if it is not already included.

Duplicates are removed automatically.

## Project Structure

```text
personal-website/
├── server.py
├── cv_data.json      # bootstrap / fallback seed for MongoDB
├── cv.pdf
├── configs/          # optional local examples (not served for write)
├── requirements.txt
├── fonts/
├── .env              # MONGO_*, EDIT_PASSWORD, SESSION_SECRET (gitignored)
└── static/
    └── index.html
```

## Environment

```bash
MONGODB_URI=mongodb+srv://...   # Atlas / Vercel connection string
EDIT_PASSWORD=...             # shared password for Edit CV
SESSION_SECRET=...            # random long string for signed cookies
SESSION_COOKIE_SECURE=true    # optional; auto-enabled on Vercel
```

### Vercel

The repo root [`server.py`](server.py) is the Flask entrypoint (`app`). Set the same env vars in the Vercel project. `SESSION_COOKIE_SECURE` turns on automatically when `VERCEL=1`.

Also in MongoDB Atlas → Network Access, allow `0.0.0.0/0` (or Vercel’s egress) so serverless functions can reach the cluster. PDF output uses `/tmp` + an in-memory cache on Vercel because the deployment filesystem is read-only.

## API

| Endpoint | Method | Purpose |
| --- | --- | --- |
| `/` | GET | Serves the web IDE |
| `/api/cv.pdf` | GET | Returns the full CV PDF |
| `/api/data` | GET | Publishes CV JSON from MongoDB |
| `/api/cv` | PUT | Replace CV document (session auth); regenerates PDF |
| `/api/comments` | GET | List editor comments (session auth; not public) |
| `/api/comments` | POST | Create entry/bullet comment (session auth) |
| `/api/comments/<id>` | PATCH | Update comment body / resolved (session auth) |
| `/api/comments/<id>` | DELETE | Delete comment (session auth) |
| `/api/auth/login` | POST | `{ password }` → sets HttpOnly session cookie |
| `/api/auth/logout` | POST | Clears session |
| `/api/auth/me` | GET | `{ authenticated: bool }` |
| `/api/compile` | POST | Accepts `{ config, filename }` and returns a generated resume PDF |
| `/api/compile-raw` | POST | Accepts raw `.cfg` text and returns a generated resume PDF |
| `/api/health` | GET | Health check |

## Notes

- `cv.pdf` is regenerated on startup when stale, and after authenticated CV saves.
- Resume `.cfg` presets are stored in browser `localStorage`, not on the server.
- Editor comments live as a sibling `comments` field on the Mongo CV document (not inside public `data` / JSON-LD / PDFs).
- Text/JSON export-import runs entirely in the browser against the CV draft; import does not touch comments.
- The full CV can span multiple pages.
- Generated resumes still try to stay compact and include as many selected projects as fit.
- If you want nicer typography, drop `Charter-Regular.ttf`, `Charter-Bold.ttf`, and `Charter-Italic.ttf` into `fonts/`.
- The frontend injects the CV JSON as JSON-LD, so crawlers see the resume text without having to run the SPA.

## Forking This For Yourself

If you want to turn this into your own resume generator, the main workflow is:

1. Fork the repo.
2. Replace the contents of `cv_data.json`.
3. Copy `configs/example.cfg` as a starting point for tailored resumes.
4. Add your own project IDs in `cv_data.json`, and update the industry tag mappings in `server.py` if you want different filtering buckets.
5. Regenerate `cv.pdf` and start tailoring resumes.

The whole point is that you should be able to treat this repo like a personal resume engine, not just a static website.
