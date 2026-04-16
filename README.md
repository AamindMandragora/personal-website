# Resume Generator IDE

Fork this repo, replace the contents of `cv_data.json` with your own background, and generate a full CV plus tailored one-page resumes from the browser.

The app is a VS Code-style frontend backed by a small Flask server that renders ATS-friendly PDFs with ReportLab. There is no LaTeX setup, no frontend build step, and no separate database. Your source of truth is just JSON plus a few `.cfg` selector files.

## What You Can Customize

- `cv_data.json`: your name, education, research, experience, projects, skills, awards, and links
- `cv.pdf`: the full CV, regenerated automatically from `cv_data.json`
- `.cfg` files in the web IDE: targeted resume filters for industries, specific projects, and bullet limits

## Features

- Full CV generation directly from `cv_data.json`
- Tailored resume generation from editable `.cfg` files
- Project filtering by `industry=[...]`
- Explicit project selection with `projects=[...]`
- Ordered selectors: if `projects` comes before `industry`, those projects are prioritized first; if `industry` comes first, industry-matched projects fill first
- Duplicate-safe project selection
- PDF rendering done server-side in pure Python

## Quick Start

```bash
pip install -r requirements.txt
python server.py
```

Then open `http://localhost:5000`.

## How To Use It

1. Fork the repo.
2. Edit `cv_data.json` with your own content.
3. Start the server with `python server.py`.
4. Open the app in your browser.
5. View `cv.pdf` for your full CV.
6. Open or create a `.cfg` file and press `Run` to generate a tailored PDF.

## Config Syntax

```cfg
# Ordered selectors are honored top-to-bottom.
projects=[malloc, squishy]
industry=[systems, ai_ml]

include_projects=true
min_bullets=1
max_bullets=3
```

Supported keys:

- `industry=[...]`
- `projects=[...]`
- `include_projects=true|false`
- `min_bullets=N`
- `max_bullets=N`
- `earliest_start_date=YYYY` (filters experiences whose start year is at least this value; default 2024; lower it to include older entries)
- `earliest_end_date=YYYY` (filters experiences whose end year is at least this value; default 2024; lower it to include older entries)

By default the résumé generator (including the precompiled CV) filters experiences to those that start and end in 2024 or later unless you explicitly lower `earliest_start_date`/`earliest_end_date`.

Built-in industry tags:

- `quant`
- `systems`
- `ai_ml`
- `software_engineering`
- `formal_methods`

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
├── cv_data.json
├── cv.pdf
├── requirements.txt
├── fonts/
└── static/
    └── index.html
```

## API

| Endpoint | Method | Purpose |
|---|---|---|
| `/` | GET | Serves the web IDE |
| `/api/cv.pdf` | GET | Returns the full CV PDF |
| `/api/compile` | POST | Accepts `{ config, filename }` and returns a generated resume PDF |
| `/api/compile-raw` | POST | Accepts raw `.cfg` text and returns a generated resume PDF |
| `/api/data` | GET | Publishes `cv_data.json` so bots and screen readers can ingest the structured content |
| `/api/health` | GET | Health check |

## Notes

- `cv.pdf` is regenerated automatically when `cv_data.json` is newer.
- The full CV can span multiple pages.
- Generated resumes still try to stay compact and include as many selected projects as fit.
- If you want nicer typography, drop `Charter-Regular.ttf`, `Charter-Bold.ttf`, and `Charter-Italic.ttf` into `fonts/`.
- The frontend now injects the cv JSON as JSON-LD, so crawlers see the resume text without having to run the SPA.

## Forking This For Yourself

If you want to turn this into your own resume generator, the main workflow is:

1. Fork the repo.
2. Replace the contents of `cv_data.json`.
3. Rename the built-in `.cfg` presets if you want.
4. Add your own project IDs in `cv_data.json`, and update the industry tag mappings in `server.py` if you want different filtering buckets.
5. Regenerate `cv.pdf` and start tailoring resumes.

The whole point is that you should be able to treat this repo like a personal resume engine, not just a static website.
