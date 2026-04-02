# Resume Generator IDE

A VS Code-style web IDE that generates resumes on the fly and renders ATS-friendly PDFs server-side (pure Python).

## Prerequisites

- **Python 3.10+**
- Python dependencies from `requirements.txt`

## Quick Start

```bash
cd resume-app
pip install -r requirements.txt
python server.py
```

Open **http://localhost:8080** in your browser (or whatever `PORT` is set to in `.env`).

## How It Works

1. **Open `cv.pdf`** in the sidebar to view your full CV as an embedded PDF
2. **Open one of the built-in `.cfg` examples** and tweak industry tags
3. **Press ▶ Run** (or `Ctrl+Enter`) — the server renders config-filtered content to PDF
4. **Download** the generated PDF directly from the toolbar

## Config Syntax

```
industry=[quant]
include_projects=true
min_bullets=2
max_bullets=2
```

### Available Filters

**Industries (applied to projects):** quant, systems, ai_ml, software_engineering, formal_methods

**Controls:** include_projects (true/false), min_bullets (number), max_bullets (number)

Research and experience are always shown in full; project selection is industry-filtered and then added until available page space is used. When space is tight, project bullets can adapt from `max_bullets` down to `min_bullets` to fit one more project.

## Project Structure

```
resume-app/
├── server.py              # Flask backend + PDF generation
├── cv_data.json           # CV content used by server.py
├── requirements.txt
├── cv.pdf                 # Pre-compiled full CV (auto-generated on first run)
└── static/
    └── index.html          # Frontend (single-file, no build step)
```

## API

| Endpoint | Method | Description |
|---|---|---|
| `/` | GET | Serves the frontend |
| `/api/cv.pdf` | GET | Returns the full CV PDF |
| `/api/compile` | POST | Accepts `{config, filename}` JSON, returns compiled PDF |
| `/api/health` | GET | Health check |

## Deployment

Set `PORT` in `.env` (or environment variables) to change the listening port.
No LaTeX toolchain is required anymore (serverless-friendly).
For closer visual parity with the old LaTeX output, optionally add Charter font files in `fonts/`:
`Charter-Regular.ttf`, `Charter-Bold.ttf`, and `Charter-Italic.ttf`.

```bash
python server.py
```
