#!/usr/bin/env python3
"""
server.py — Flask backend for the Resume IDE.
Endpoints:
  GET  /                    → serves the frontend
  GET  /api/cv.pdf          → returns the pre-compiled full CV PDF
  POST /api/compile          → accepts JSON config body, returns a filtered resume PDF
  POST /api/compile-raw      → accepts raw .cfg text body, returns a filtered resume PDF
"""
import os, json, copy
from flask import Flask, request, send_file, jsonify, send_from_directory
from io import BytesIO
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

app = Flask(__name__, static_folder="static", static_url_path="/static")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CV_PDF_PATH = os.path.join(BASE_DIR, "cv.pdf")
CV_JSON_PATH = os.path.join(BASE_DIR, "cv_data.json")
FONTS_DIR = os.path.join(BASE_DIR, "fonts")
INDEX_HTML_PATH = os.path.join(BASE_DIR, "static", "index.html")

# ─── CORS (no flask-cors needed) ───
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    return response

app.after_request(add_cors)

def load_cv_data():
    with open(CV_JSON_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def get_cv_data():
    """Always read the latest CV source data from disk."""
    return load_cv_data()

# Layout constants tuned to 1 cm page margins.
CM = inch / 2.54
LEFT = 1.0 * CM
RIGHT = letter[0] - (1.0 * CM)
TOP = letter[1] - (1.0 * CM)
BOTTOM = 1.0 * CM
WIDTH = RIGHT - LEFT
FONT_REGULAR = "Times-Roman"
FONT_BOLD = "Times-Bold"
FONT_ITALIC = "Times-Italic"
SPACING_SCALE = 0.85


def sv(value):
    """Scale vertical spacing uniformly."""
    return value * SPACING_SCALE


def register_charter_fonts():
    """Register local Charter TTFs when available."""
    global FONT_REGULAR, FONT_BOLD, FONT_ITALIC
    regular_path = os.path.join(FONTS_DIR, "Charter-Regular.ttf")
    bold_path = os.path.join(FONTS_DIR, "Charter-Bold.ttf")
    italic_path = os.path.join(FONTS_DIR, "Charter-Italic.ttf")
    if not (os.path.exists(regular_path) and os.path.exists(bold_path) and os.path.exists(italic_path)):
        return False
    try:
        pdfmetrics.registerFont(TTFont("Charter-Regular", regular_path))
        pdfmetrics.registerFont(TTFont("Charter-Bold", bold_path))
        pdfmetrics.registerFont(TTFont("Charter-Italic", italic_path))
        FONT_REGULAR = "Charter-Regular"
        FONT_BOLD = "Charter-Bold"
        FONT_ITALIC = "Charter-Italic"
        return True
    except Exception:
        return False


register_charter_fonts()


def normalize_text(text):
    """Normalize common LaTeX escapes for reportlab rendering."""
    if text is None:
        return ""
    return (
        str(text)
        .replace("\\&", "&")
        .replace("\\%", "%")
        .replace("\\_", "_")
        .replace("\\$", "$")
        .replace("\\#", "#")
        .replace("\\{", "{")
        .replace("\\}", "}")
        .replace("~", " ")
        .replace("--", "—")
    )

INDUSTRY_TAGS = {
    "quant": {"quant_dev", "trading", "finance"},
    "systems": {"systems", "networking", "hardware"},
    "ai_ml": {"ml", "swe"},
    "software_engineering": {"swe", "ml"},
    "formal_methods": {"formal_verification", "math"},
}

INDUSTRY_ALIASES = {
    "quant_dev": "quant",
    "trading": "quant",
    "finance": "quant",
    "systems": "systems",
    "networking": "systems",
    "hardware": "systems",
    "ml": "ai_ml",
    "ai": "ai_ml",
    "swe": "software_engineering",
    "formal_verification": "formal_methods",
    "math": "formal_methods",
    "hackathon": "software_engineering",
}

# ─── Config parsing ───
def parse_config_text(text):
    config = {}
    selector_steps = []
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip().lower()
        val = val.strip()
        if val.startswith("[") and val.endswith("]"):
            val = [v.strip().strip("'\"").lower() for v in val[1:-1].split(",") if v.strip()]
        else:
            val = val.strip("'\"")
        if key in ("industry", "industries"):
            values = val if isinstance(val, list) else [str(val).lower()]
            selector_steps.append({"kind": "industry", "values": values})
            existing = config.get("industry", [])
            if isinstance(existing, str):
                existing = [existing]
            config["industry"] = existing + values
            continue
        if key in ("project", "projects"):
            values = val if isinstance(val, list) else [str(val).lower()]
            selector_steps.append({"kind": "projects", "values": values})
            existing = config.get("projects", [])
            if isinstance(existing, str):
                existing = [existing]
            config["projects"] = existing + values
            continue
        config[key] = val
    if selector_steps:
        config["_selector_steps"] = selector_steps
    return config

# ─── Filtering ───
def filter_cv(config, *, force_include_all_projects=False):
    source_cv = get_cv_data()
    data = copy.deepcopy(source_cv)
    industries = config.get("industry") or config.get("industries")
    selected_projects = config.get("projects") or config.get("project")
    if isinstance(industries, str):
        industries = [industries]
    if isinstance(selected_projects, str):
        selected_projects = [selected_projects]

    resolved_industries = []
    if industries:
        for i in industries:
            resolved_industries.append(str(i).strip().lower())

    resolved_projects = []
    if selected_projects:
        for project in selected_projects:
            resolved_projects.append(str(project).strip().lower())

    expanded_tags = set()
    for key in resolved_industries:
        canonical = INDUSTRY_ALIASES.get(key, key)
        expanded_tags |= INDUSTRY_TAGS.get(canonical, {key})

    project_lookup = {}
    for item in source_cv.get("projects", []):
        pid = str(item.get("id", "")).strip().lower()
        pname = str(item.get("name", "")).strip().lower()
        if pid:
            project_lookup[pid] = item
        if pname:
            project_lookup[pname] = item

    def project_matches_tags(item, tags):
        return any(tag in tags for tag in item.get("tags", []))

    def select_projects_by_steps():
        steps = config.get("_selector_steps", [])
        if not steps:
            return None

        ordered = []
        seen = set()

        def add_item(item):
            pid = str(item.get("id", "")).strip().lower()
            if not pid or pid in seen:
                return
            ordered.append(copy.deepcopy(item))
            seen.add(pid)

        for step in steps:
            if step["kind"] == "projects":
                for project_key in step["values"]:
                    item = project_lookup.get(project_key)
                    if item:
                        add_item(item)
            elif step["kind"] == "industry":
                for industry_key in step["values"]:
                    canonical = INDUSTRY_ALIASES.get(industry_key, industry_key)
                    tags = INDUSTRY_TAGS.get(canonical, {industry_key})
                    for item in source_cv.get("projects", []):
                        if project_matches_tags(item, tags):
                            add_item(item)
        return ordered, seen

    def filter_by_tags(items):
        if not expanded_tags:
            return items
        return [it for it in items if project_matches_tags(it, expanded_tags)]

    def order_projects_for_resume(items):
        # Order by industry priority first, then preserve canonical JSON order
        # within each industry's bucket.
        if not resolved_industries:
            return list(items)

        ordered = []
        seen = set()
        for key in resolved_industries:
            canonical = INDUSTRY_ALIASES.get(key, key)
            tags = INDUSTRY_TAGS.get(canonical, {key})
            for it in items:
                pid = str(it.get("id", "")).lower()
                if pid in seen:
                    continue
                if project_matches_tags(it, tags):
                    ordered.append(it)
                    seen.add(pid)

        # Safety: append any unmatched residual items in JSON order.
        for it in items:
            pid = str(it.get("id", "")).lower()
            if pid not in seen:
                ordered.append(it)
        return ordered

    selected_by_steps = select_projects_by_steps()
    if selected_by_steps is not None:
        ordered_projects, seen = selected_by_steps
        if force_include_all_projects:
            for item in source_cv.get("projects", []):
                pid = str(item.get("id", "")).strip().lower()
                if pid and pid not in seen:
                    ordered_projects.append(copy.deepcopy(item))
                    seen.add(pid)
        data["projects"] = ordered_projects
    else:
        if force_include_all_projects:
            data["projects"] = copy.deepcopy(source_cv.get("projects", []))
            if resolved_industries:
                data["projects"] = order_projects_for_resume(data["projects"])
        else:
            data["projects"] = filter_by_tags(data["projects"])
            data["projects"] = order_projects_for_resume(data["projects"])

    include_projects = str(config.get("include_projects", "true")).lower() != "false"
    if not include_projects:
        data["projects"] = []

    min_b_raw = config.get("min_bullets")
    max_b_raw = config.get("max_bullets")
    min_b = None
    max_b = None
    if min_b_raw is not None and str(min_b_raw).strip():
        min_b = max(1, int(min_b_raw))
    if max_b_raw is not None and str(max_b_raw).strip():
        max_b = max(1, int(max_b_raw))
    if min_b is not None and max_b is not None and min_b > max_b:
        min_b, max_b = max_b, min_b

    # Cap bullets for research/experience directly by max_b if provided.
    if max_b is not None:
        for section in ("research", "experience"):
            data[section] = [{**it, "bullets": it["bullets"][:max_b]} for it in data[section]]

    # Drop empty items so we never render whitespace-only content blocks.
    for section in ("research", "experience", "projects"):
        cleaned = []
        for it in data[section]:
            bullets = [b for b in it.get("bullets", []) if normalize_text(b).strip()]
            if bullets:
                cleaned.append({**it, "bullets": bullets})
        data[section] = cleaned

    # Avoid sparse/empty resumes unless explicitly allowed.
    allow_empty = str(config.get("allow_empty", "false")).lower() == "true"
    if not allow_empty and not data["projects"]:
        data["projects"] = copy.deepcopy(source_cv.get("projects", []))[:3]
        if max_b is not None:
            data["projects"] = [{**it, "bullets": it["bullets"][:max_b]} for it in data["projects"]]

    data["_bullet_bounds"] = {"min": min_b, "max": max_b}
    return data

def wrap_text(c, text, font_name, font_size, max_width):
    words = normalize_text(text).split()
    if not words:
        return [""]
    lines = []
    current = words[0]
    for w in words[1:]:
        trial = f"{current} {w}"
        if c.stringWidth(trial, font_name, font_size) <= max_width:
            current = trial
        else:
            lines.append(current)
            current = w
    lines.append(current)
    return lines

def draw_line(c, text, x, y, font_name="Helvetica", font_size=10, max_width=None, line_gap=12):
    c.setFont(font_name, font_size)
    if max_width is None:
        c.drawString(x, y, text)
        return y - line_gap
    lines = wrap_text(c, text, font_name, font_size, max_width)
    for line in lines:
        c.drawString(x, y, line)
        y -= line_gap
    return y


def draw_bulleted_line(c, text, x, y, font_name="Helvetica", font_size=10, max_width=None, line_gap=12):
    bullet_x = x + 6
    text_x = x + 14
    available_width = None if max_width is None else max_width - (text_x - x)
    lines = wrap_text(c, text, font_name, font_size, available_width)
    c.setFont(font_name, font_size)
    c.drawString(bullet_x, y, u"\u2022")
    for line in lines:
        c.drawString(text_x, y, line)
        y -= line_gap
    return y

def draw_section_header(c, title, x, y):
    c.setFont(FONT_BOLD, 14)
    c.drawString(x, y, title)
    y -= sv(5)
    c.setLineWidth(0.8)
    c.line(x, y, RIGHT, y)
    return y - sv(18)

def maybe_new_page(c, y, needed_height):
    if y - needed_height >= BOTTOM:
        return y
    c.showPage()
    return TOP

def _estimate_entry_height(c, item, content_w):
    title_line = sv(16)
    total = title_line + sv(6)
    for bullet in item.get("bullets", []):
        bullet_lines = wrap_text(c, bullet, FONT_REGULAR, 10.2, content_w - 20)
        total += sv(15) * max(1, len(bullet_lines))
    total += sv(12)
    return total

def _item_with_bullets(item, bullet_count):
    if bullet_count is None:
        return item
    return {**item, "bullets": item.get("bullets", [])[:bullet_count]}


def draw_entries(c, y, title, items, allow_new_page=True, min_bullets=None, max_bullets=None, keep_entry_together=True):
    if not items:
        return y, 0
    left = LEFT
    right = RIGHT
    content_w = WIDTH

    y = maybe_new_page(c, y, sv(36))
    y = draw_section_header(c, title, left, y)
    drawn = 0
    for it in items:
        # For tight page-fit mode, adapt bullets between max->min to fit one more project.
        bullet_choices = [None]
        if not allow_new_page and (min_bullets is not None or max_bullets is not None):
            total_bullets = len(it.get("bullets", []))
            hi = min(max_bullets if max_bullets is not None else total_bullets, total_bullets)
            lo = min_bullets if min_bullets is not None else hi
            lo = max(1, min(lo, hi))
            bullet_choices = list(range(hi, lo - 1, -1))

        candidate = it
        needed = _estimate_entry_height(c, candidate, content_w)
        if not allow_new_page:
            for bcount in bullet_choices:
                trial = _item_with_bullets(it, bcount)
                trial_needed = _estimate_entry_height(c, trial, content_w)
                if (y - trial_needed) >= BOTTOM:
                    candidate = trial
                    needed = trial_needed
                    break

        if keep_entry_together:
            if (y - needed) < BOTTOM:
                if not allow_new_page:
                    break
                y = maybe_new_page(c, y, needed + sv(10))
                candidate = it
        elif (y - sv(28)) < BOTTOM:
            if not allow_new_page:
                break
            y = maybe_new_page(c, y, sv(28))
        c.setFont(FONT_BOLD, 10.6)
        name_text = normalize_text(candidate["name"])
        c.drawString(left, y, name_text)
        c.setFont(FONT_ITALIC, 10.6)
        subtitle = normalize_text(candidate["subtitle"])
        name_end = left + c.stringWidth(name_text, FONT_BOLD, 10.6)
        c.setFont(FONT_REGULAR, 10.6)
        c.drawString(name_end + 1, y, ", ")
        c.setFont(FONT_ITALIC, 10.6)
        c.drawString(name_end + 8, y, subtitle)
        c.setFont(FONT_REGULAR, 10.6)
        c.drawRightString(right, y, normalize_text(candidate["dates"]))
        y -= sv(16)
        for bullet in candidate["bullets"]:
            y = maybe_new_page(c, y, sv(32))
            bullet_lines = wrap_text(c, bullet, FONT_REGULAR, 10.2, content_w - 20)
            c.setFont(FONT_REGULAR, 10.2)
            c.drawString(left + 6, y, u"\u2022")
            c.drawString(left + 14, y, bullet_lines[0])
            y -= sv(15)
            for line in bullet_lines[1:]:
                c.drawString(left + 14, y, line)
                y -= sv(15)
        y -= sv(10)
        drawn += 1
    return y, drawn

def generate_pdf(data, title="Resume", include_all_projects=False):
    """Generate ATS-friendly PDF bytes with LaTeX-like visual layout."""
    try:
        buf = BytesIO()
        c = canvas.Canvas(buf, pagesize=letter, pageCompression=1)
        c.setTitle(f"{data['name']} - {title}")
        c.setAuthor(data["name"])
        y = TOP

        # Header: align name block with the two-line socials block.
        row1_y = y - sv(1)
        row2_y = y - sv(18)
        # Lower the large name so its visual block spans roughly the same vertical band.
        name_y = row2_y - sv(2)

        c.setFont(FONT_REGULAR, 29)
        c.drawString(LEFT, name_y, normalize_text(data["name"]))
        c.setFont(FONT_REGULAR, 11)
        c.drawRightString(RIGHT, row1_y, f"{normalize_text(data['location'])} | {normalize_text(data['email'])}")
        c.drawRightString(
            RIGHT,
            row2_y,
            f"{normalize_text(data['phone'])} | GitHub | LinkedIn"
        )
        y = row2_y - sv(24)

        # keep actual links machine-readable and clickable
        # Row 1 links (email)
        c.linkURL(f"mailto:{data['email']}", (RIGHT - 142, row1_y - 3, RIGHT, row1_y + 9), relative=0)
        # Row 2 links (phone/github/linkedin)
        c.linkURL(f"tel:{''.join(ch for ch in data['phone'] if ch.isdigit() or ch == '+')}", (RIGHT - 206, row2_y - 3, RIGHT - 126, row2_y + 9), relative=0)
        c.linkURL(data["github"], (RIGHT - 83, row2_y - 3, RIGHT - 46, row2_y + 9), relative=0)
        c.linkURL(data["linkedin"], (RIGHT - 44, row2_y - 3, RIGHT, row2_y + 9), relative=0)
        y -= sv(12)

        y = draw_section_header(c, "Education", LEFT, y)
        edu = data["education"]
        c.setFont(FONT_BOLD, 11)
        c.drawString(LEFT, y, normalize_text(edu["school"]))
        c.setFont(FONT_REGULAR, 10.5)
        c.drawRightString(RIGHT, y, normalize_text(edu["dates"]))
        y -= sv(16)
        c.setFont(FONT_ITALIC, 10.5)
        c.drawString(LEFT, y, normalize_text(edu["degree"]))
        y -= sv(16)

        y = draw_bulleted_line(
            c,
            f"Relevant Coursework: {', '.join(normalize_text(x) for x in edu['coursework'])}",
            LEFT,
            y,
            FONT_REGULAR,
            10,
            WIDTH,
            sv(16),
        )
        y -= sv(12)

        y = draw_section_header(c, "Technical Skills and Awards", LEFT, y)
        y = draw_line(c, f"Awards: {', '.join(normalize_text(x) for x in data['awards'])}", LEFT, y, FONT_REGULAR, 10, WIDTH, sv(16))
        y = draw_line(c, f"Languages: {', '.join(normalize_text(x) for x in data['languages'])}", LEFT, y, FONT_REGULAR, 10, WIDTH, sv(16))
        y = draw_line(c, f"Tools & Libraries: {', '.join(normalize_text(x) for x in data['tools'])}", LEFT, y, FONT_REGULAR, 10, WIDTH, sv(16))
        y -= sv(10)

        bounds = data.get("_bullet_bounds", {}) if isinstance(data, dict) else {}
        min_b = bounds.get("min")
        max_b = bounds.get("max")

        # Full CVs can spill projects onto later pages; tailored resumes stay compact.
        y, _ = draw_entries(c, y, "Research", data.get("research", []), allow_new_page=True, max_bullets=max_b)
        y, _ = draw_entries(c, y, "Experience", data.get("experience", []), allow_new_page=True, max_bullets=max_b)
        y, _ = draw_entries(
            c,
            y,
            "Technical Projects",
            data.get("projects", []),
            allow_new_page=include_all_projects,
            min_bullets=min_b,
            max_bullets=max_b,
            keep_entry_together=not include_all_projects,
        )

        c.save()
        buf.seek(0)
        return buf.read(), None
    except Exception as e:
        return None, str(e)

# ─── Pre-compile CV on startup ───
def cv_pdf_is_stale():
    if not os.path.exists(CV_PDF_PATH):
        return True
    if not os.path.exists(CV_JSON_PATH):
        return False
    return os.path.getmtime(CV_JSON_PATH) > os.path.getmtime(CV_PDF_PATH)


def ensure_cv_pdf():
    if not cv_pdf_is_stale():
        return
    print("[startup] Generating CV PDF from cv_data.json...")
    pdf_bytes, err = generate_pdf(get_cv_data(), "CV", include_all_projects=True)
    if pdf_bytes:
        with open(CV_PDF_PATH, "wb") as f:
            f.write(pdf_bytes)
        print(f"[startup] CV PDF generated ({len(pdf_bytes)} bytes)")
    else:
        print(f"[startup] CV generation failed: {err}")

# ─── Routes ───
def render_index_html():
    try:
        with open(INDEX_HTML_PATH, "r", encoding="utf-8") as f:
            html = f.read()
    except Exception as exc:
        return jsonify({"error": f"Unable to read index.html: {exc}"}), 500
    json_ld = json.dumps(get_cv_data(), ensure_ascii=False).replace("</script>", "<\\/script>")
    script = f'<script type="application/ld+json">{json_ld}</script>'
    return html.replace("<!--JSON_LD_PLACEHOLDER-->", script)


@app.route("/")
def index():
    return render_index_html()

@app.route("/api/cv.pdf")
def get_cv_pdf():
    ensure_cv_pdf()
    if os.path.exists(CV_PDF_PATH):
        return send_file(CV_PDF_PATH, mimetype="application/pdf",
                         download_name="advayth_pashupati_cv.pdf")
    return jsonify({"error": "CV PDF not available"}), 500


@app.route("/api/data")
def get_cv_json():
    """Expose the latest cv_data.json to automated agents."""
    try:
        data = get_cv_data()
        return jsonify({"source": "cv_data.json", "updated": os.path.getmtime(CV_JSON_PATH), "data": data})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

@app.route("/api/compile", methods=["POST", "OPTIONS"])
def compile_resume():
    if request.method == "OPTIONS":
        return "", 204
    data = request.get_json(force=True)
    config_text = data.get("config", "")
    filename = data.get("filename", "resume")
    if not config_text.strip():
        return jsonify({"error": "Empty config"}), 400

    config = parse_config_text(config_text)
    if not config:
        return jsonify({"error": "No valid key=value pairs found"}), 400

    filtered = filter_cv(config)
    pdf_bytes, err = generate_pdf(filtered, title=filename)

    if pdf_bytes:
        return send_file(
            BytesIO(pdf_bytes),
            mimetype="application/pdf",
            download_name=f"{filename}.pdf",
        )
    else:
        return jsonify({"error": f"PDF generation failed: {err}"}), 500


@app.route("/api/compile-cv", methods=["POST", "OPTIONS"])
def compile_cv():
    if request.method == "OPTIONS":
        return "", 204
    data = request.get_json(force=True)
    config_text = data.get("config", "")
    filename = data.get("filename", "cv")
    if not config_text.strip():
        return jsonify({"error": "Empty config"}), 400

    config = parse_config_text(config_text)
    if not config:
        return jsonify({"error": "No valid key=value pairs found"}), 400

    filtered = filter_cv(config, force_include_all_projects=True)
    pdf_bytes, err = generate_pdf(filtered, title=filename, include_all_projects=True)

    if pdf_bytes:
        return send_file(
            BytesIO(pdf_bytes),
            mimetype="application/pdf",
            download_name=f"{filename}.pdf",
        )
    else:
        return jsonify({"error": f"PDF generation failed: {err}"}), 500

@app.route("/api/compile-raw", methods=["POST", "OPTIONS"])
def compile_raw():
    """Accept raw .cfg text as the request body."""
    if request.method == "OPTIONS":
        return "", 204
    config_text = request.get_data(as_text=True)
    if not config_text.strip():
        return jsonify({"error": "Empty config"}), 400

    config = parse_config_text(config_text)
    filtered = filter_cv(config)
    pdf_bytes, err = generate_pdf(filtered, title="resume")

    if pdf_bytes:
        return send_file(BytesIO(pdf_bytes), mimetype="application/pdf",
                         download_name="resume.pdf")
    return jsonify({"error": f"PDF generation failed: {err}"}), 500

@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "pdf_engine": "reportlab"})

if __name__ == "__main__":
    register_charter_fonts()
    ensure_cv_pdf()
    port = 5000
    print(f"[server] Starting on http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
