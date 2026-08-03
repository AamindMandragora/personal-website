#!/usr/bin/env python3
"""
server.py — Flask backend for the Resume IDE.
Endpoints:
  GET  /                    → serves the frontend
  GET  /api/cv.pdf          → returns the CV PDF (?force=1 rebuilds from Mongo)
  GET  /api/data            → public CV JSON (from MongoDB)
  PUT  /api/cv              → replace CV document (auth required); regen PDF
  GET  /api/comments        → list editor comments (auth)
  POST /api/comments        → create editor comment (auth)
  PATCH /api/comments/<id>  → update comment body/resolved (auth)
  DELETE /api/comments/<id> → delete comment (auth)
  POST /api/auth/login      → set session cookie from shared password
  POST /api/auth/logout     → clear session cookie
  GET  /api/auth/me         → { authenticated: bool }
  POST /api/compile         → accepts JSON config body, returns a filtered resume PDF
  POST /api/compile-raw     → accepts raw .cfg text body, returns a filtered resume PDF
"""
import os, json, copy, re, datetime, base64, time, functools, secrets
from io import BytesIO

import certifi
from dotenv import load_dotenv
from flask import Flask, request, send_file, jsonify, session
from pymongo import MongoClient
from pymongo.errors import AutoReconnect, ConnectionFailure, ServerSelectionTimeoutError
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

IS_VERCEL = os.environ.get("VERCEL") == "1"
# Vercel’s filesystem is read-only except /tmp; local keeps cv.pdf in the repo.
CV_PDF_PATH = "/tmp/cv.pdf" if IS_VERCEL else os.path.join(BASE_DIR, "cv.pdf")
REPO_CV_PDF_PATH = os.path.join(BASE_DIR, "cv.pdf")
CV_JSON_PATH = os.path.join(BASE_DIR, "cv_data.json")
FONTS_DIR = os.path.join(BASE_DIR, "fonts")
INDEX_HTML_PATH = os.path.join(BASE_DIR, "static", "index.html")

_secure_cookie_env = os.environ.get("SESSION_COOKIE_SECURE", "").strip().lower()
if _secure_cookie_env in ("1", "true", "yes"):
    SESSION_COOKIE_SECURE = True
elif _secure_cookie_env in ("0", "false", "no"):
    SESSION_COOKIE_SECURE = False
else:
    # Default: secure cookies on Vercel (HTTPS), off for local http://localhost
    SESSION_COOKIE_SECURE = IS_VERCEL

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.secret_key = os.environ.get("SESSION_SECRET") or os.urandom(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=SESSION_COOKIE_SECURE,
)

if IS_VERCEL:
    # Trust X-Forwarded-* from Vercel’s reverse proxy.
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

MONGODB_URI = os.environ.get("MONGODB_URI", "").strip()
EDIT_PASSWORD = os.environ.get("EDIT_PASSWORD", "")
CV_SLUG = "main"
_MONGO_TRANSIENT = (AutoReconnect, ConnectionFailure, ServerSelectionTimeoutError)

_mongo_client = None
_cv_cache = {"data": None, "updated": None, "source": None}
_comments_cache = {"comments": None}
_pdf_cache = {"bytes": None, "updated": None}
_runtime_ready = False
COMMENT_SECTIONS = frozenset({"research", "experience", "projects"})


# ─── CORS (no flask-cors needed) ───
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,PATCH,DELETE,OPTIONS"
    return response


app.after_request(add_cors)


def reset_mongo_client():
    """Drop a broken client so the next call opens a fresh TLS session."""
    global _mongo_client
    if _mongo_client is not None:
        try:
            _mongo_client.close()
        except Exception:
            pass
    _mongo_client = None


def get_mongo_collection():
    """Return the cv.cv_data collection, creating the client lazily."""
    global _mongo_client
    if _mongo_client is None:
        if not MONGODB_URI:
            raise RuntimeError("MONGODB_URI must be set in .env")
        _mongo_client = MongoClient(
            MONGODB_URI,
            serverSelectionTimeoutMS=8000,
            connectTimeoutMS=10000,
            tls=True,
            tlsCAFile=certifi.where(),
        )
    return _mongo_client["cv"]["cv_data"]


def with_mongo_retry(fn):
    """Run fn(collection); on transient TLS/network errors, reconnect once."""
    try:
        return fn(get_mongo_collection())
    except _MONGO_TRANSIENT:
        reset_mongo_client()
        return fn(get_mongo_collection())


def load_cv_data_from_disk():
    with open(CV_JSON_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def write_cv_data_to_disk(data):
    """Best-effort local mirror (skipped on Vercel’s read-only FS)."""
    if IS_VERCEL:
        return
    with open(CV_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def seed_mongo_if_empty():
    """Insert cv_data.json into Mongo once if the main document is missing."""
    def _seed(col):
        if col.find_one({"slug": CV_SLUG}, projection={"_id": 1}):
            return False
        data = load_cv_data_from_disk()
        now = time.time()
        col.insert_one({"slug": CV_SLUG, "data": data, "comments": [], "updated": now})
        _cv_cache["data"] = data
        _cv_cache["updated"] = now
        _cv_cache["source"] = "mongodb"
        _comments_cache["comments"] = []
        print("[startup] Seeded MongoDB cv.cv_data from cv_data.json")
        return True

    return with_mongo_retry(_seed)


def invalidate_cv_cache():
    _cv_cache["data"] = None
    _cv_cache["updated"] = None
    _cv_cache["source"] = None
    _comments_cache["comments"] = None


def get_cv_data(*, refresh=False, allow_disk_fallback=True):
    """Return CV source data from Mongo (cached), optionally falling back to disk.

    refresh=True bypasses the in-memory cache and re-reads Mongo.
    allow_disk_fallback=False raises if Mongo is unavailable or has no document
    (used by force PDF regeneration so it never silently uses cv_data.json).
    """
    if not refresh and _cv_cache["data"] is not None:
        return copy.deepcopy(_cv_cache["data"])
    try:
        def _read(col):
            return col.find_one(
                {"slug": CV_SLUG},
                projection={"_id": 0, "data": 1, "updated": 1},
            )

        doc = with_mongo_retry(_read)
        if doc and doc.get("data") is not None:
            _cv_cache["data"] = doc["data"]
            _cv_cache["updated"] = doc.get("updated")
            _cv_cache["source"] = "mongodb"
            return copy.deepcopy(doc["data"])
        if not allow_disk_fallback:
            raise RuntimeError("MongoDB has no CV document for slug={!r}".format(CV_SLUG))
    except Exception as exc:
        if not allow_disk_fallback:
            raise
        print(f"[cv] Mongo read failed, falling back to disk: {exc}")
    if not allow_disk_fallback:
        raise RuntimeError("MongoDB CV data unavailable")
    data = load_cv_data_from_disk()
    _cv_cache["data"] = data
    _cv_cache["updated"] = os.path.getmtime(CV_JSON_PATH) if os.path.exists(CV_JSON_PATH) else None
    _cv_cache["source"] = "disk"
    return copy.deepcopy(data)


def get_comments(*, refresh=False):
    """Return editor comments from Mongo (cached). Not part of public CV JSON."""
    if not refresh and _comments_cache["comments"] is not None:
        return copy.deepcopy(_comments_cache["comments"])
    try:
        def _read(col):
            return col.find_one(
                {"slug": CV_SLUG},
                projection={"_id": 0, "comments": 1},
            )

        doc = with_mongo_retry(_read)
        comments = list(doc.get("comments") or []) if doc else []
        _comments_cache["comments"] = comments
        return copy.deepcopy(comments)
    except Exception as exc:
        print(f"[comments] Mongo read failed: {exc}")
        _comments_cache["comments"] = []
        return []


def save_comments(comments):
    """Persist comments list without touching CV data."""
    if not isinstance(comments, list):
        raise ValueError("comments must be a list")
    now = time.time()

    def _write(col):
        col.update_one(
            {"slug": CV_SLUG},
            {
                "$set": {"comments": comments, "updated": now},
                "$setOnInsert": {"slug": CV_SLUG, "data": {}},
            },
            upsert=True,
        )

    with_mongo_retry(_write)
    _comments_cache["comments"] = copy.deepcopy(comments)
    _cv_cache["updated"] = now
    return now


def new_comment_id():
    return f"c_{int(time.time() * 1000)}_{secrets.token_hex(4)}"


def normalize_comment_bullet(bullet):
    """Validate optional bullet anchor; return None or {variant, index, quote}."""
    if bullet is None:
        return None
    if not isinstance(bullet, dict):
        raise ValueError("bullet must be an object or null")
    variant = str(bullet.get("variant", "")).strip()
    if not variant:
        raise ValueError("bullet.variant is required")
    try:
        index = int(bullet.get("index"))
    except (TypeError, ValueError):
        raise ValueError("bullet.index must be an integer") from None
    if index < 0:
        raise ValueError("bullet.index must be >= 0")
    quote = bullet.get("quote", "")
    if quote is None:
        quote = ""
    if not isinstance(quote, str):
        raise ValueError("bullet.quote must be a string")
    return {"variant": variant, "index": index, "quote": quote}


def save_cv_data(data, *, allow_disk_fallback=False):
    """Persist CV JSON to Mongo (and local disk mirror) and refresh the cache.

    Uses $set on data/updated so sibling editor comments are preserved.
    By default Mongo is required; disk is only a post-success mirror.
    Set allow_disk_fallback=True only for offline/dev recovery paths.
    """
    if not isinstance(data, dict):
        raise ValueError("CV data must be a JSON object")
    now = time.time()

    def _write(col):
        col.update_one(
            {"slug": CV_SLUG},
            {
                "$set": {"data": data, "updated": now},
                "$setOnInsert": {"slug": CV_SLUG, "comments": []},
            },
            upsert=True,
        )

    try:
        with_mongo_retry(_write)
    except Exception as exc:
        if allow_disk_fallback and not IS_VERCEL:
            print(f"[cv] Mongo write failed, saving to cv_data.json: {exc}")
            write_cv_data_to_disk(data)
            _cv_cache["data"] = data
            _cv_cache["updated"] = now
            _cv_cache["source"] = "disk"
            return now
        raise RuntimeError(f"MongoDB write failed: {exc}") from exc

    try:
        write_cv_data_to_disk(data)
    except Exception as exc:
        print(f"[cv] Disk mirror skipped: {exc}")
    _cv_cache["data"] = data
    _cv_cache["updated"] = now
    _cv_cache["source"] = "mongodb"
    return now


def is_authenticated():
    return bool(session.get("authenticated"))


def require_auth(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if not is_authenticated():
            return jsonify({"error": "Authentication required"}), 401
        return fn(*args, **kwargs)

    return wrapper


def ensure_runtime():
    """Fonts + Mongo seed — runs once per process (needed on Vercel where __main__ never runs)."""
    global _runtime_ready
    if _runtime_ready:
        return
    register_fonts()
    try:
        seed_mongo_if_empty()
        get_cv_data()
    except Exception as exc:
        print(f"[startup] MongoDB unavailable ({exc}); using cv_data.json fallback")
    _runtime_ready = True


@app.before_request
def _ensure_runtime_before_request():
    ensure_runtime()

# Layout constants tuned to 1 cm page margins.
CM = inch / 2.54
LEFT = 1.0 * CM
RIGHT = letter[0] - (1.0 * CM)
TOP = letter[1] - (1.0 * CM)
BOTTOM = 0.75 * CM
WIDTH = RIGHT - LEFT
FONT_REGULAR = "Times-Roman"
FONT_BOLD = "Times-Bold"
FONT_ITALIC = "Times-Italic"
FONT_CODE = "Courier"
SPACING_SCALE = 0.85
CURRENT_YEAR = datetime.datetime.now().year
DEFAULT_EARLIEST_START_YEAR = 2024
DEFAULT_EARLIEST_END_YEAR = 2024
BULLET_FONT_SIZE = 10.2
ENTRY_TITLE_SIZE = 10.6
SECTION_HEADER_SIZE = 11
MARKUP_SEGMENT_PATTERN = re.compile(r"\*\*(.+?)\*\*|`([^`]+)`")


def sv(value):
    """Scale vertical spacing uniformly."""
    return value * SPACING_SCALE


def register_fonts():
    """Register local Charter + monospace TTFs when available."""
    global FONT_REGULAR, FONT_BOLD, FONT_ITALIC, FONT_CODE
    regular_path = os.path.join(FONTS_DIR, "Charter-Regular.ttf")
    bold_path = os.path.join(FONTS_DIR, "Charter-Bold.ttf")
    italic_path = os.path.join(FONTS_DIR, "Charter-Italic.ttf")
    if os.path.exists(regular_path) and os.path.exists(bold_path) and os.path.exists(italic_path):
        try:
            pdfmetrics.registerFont(TTFont("Charter-Regular", regular_path))
            pdfmetrics.registerFont(TTFont("Charter-Bold", bold_path))
            pdfmetrics.registerFont(TTFont("Charter-Italic", italic_path))
            FONT_REGULAR = "Charter-Regular"
            FONT_BOLD = "Charter-Bold"
            FONT_ITALIC = "Charter-Italic"
        except Exception:
            pass
    mono_path = os.path.join(FONTS_DIR, "LiberationMono-Regular.ttf")
    if os.path.exists(mono_path):
        try:
            pdfmetrics.registerFont(TTFont("LiberationMono-Regular", mono_path))
            FONT_CODE = "LiberationMono-Regular"
        except Exception:
            FONT_CODE = "Courier"
    else:
        FONT_CODE = "Courier"


register_fonts()


def _font_for_style(style):
    if style == "bold":
        return FONT_BOLD
    if style == "code":
        return FONT_CODE
    return FONT_REGULAR


def _size_for_style(style, base_size):
    if style == "code":
        return max(8.0, base_size - 0.6)
    return base_size


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


def _clean_bullet_list(bullets):
    return [b for b in (bullets or []) if normalize_text(b).strip()]


def _bullet_variants(item):
    """
    Return {count: [bullet, ...]} for an entry.

    Preferred shape in cv_data.json:
      "bullets": {"1": ["..."], "2": ["...", "..."], ...}

    Legacy shape (flat list) is treated as prefixes of that list.
    """
    raw = item.get("bullets") if isinstance(item, dict) else None
    if isinstance(raw, dict):
        out = {}
        for key, value in raw.items():
            try:
                count = int(key)
            except (TypeError, ValueError):
                continue
            if count < 1 or not isinstance(value, list):
                continue
            cleaned = _clean_bullet_list(value)
            if cleaned:
                out[count] = cleaned
        return out
    if isinstance(raw, list):
        cleaned = _clean_bullet_list(raw)
        return {i: cleaned[:i] for i in range(1, len(cleaned) + 1)}
    return {}


def _store_bullet_variants(variants):
    return {str(k): list(v) for k, v in sorted(variants.items())}


def _available_bullet_counts(item, max_bullets=None, min_bullets=None):
    keys = sorted(_bullet_variants(item))
    if max_bullets is not None:
        keys = [k for k in keys if k <= max_bullets]
    if min_bullets is not None:
        keys = [k for k in keys if k >= min_bullets]
    return keys


def _section_bounds(bounds, section):
    """Return (min_b, max_b) for a section, falling back to global."""
    if not bounds:
        return None, None
    sec = bounds.get("sections", {}).get(section, {})
    min_b = sec.get("min") if sec.get("min") is not None else bounds.get("min")
    max_b = sec.get("max") if sec.get("max") is not None else bounds.get("max")
    return min_b, max_b


def _resolve_bullets(item, count=None):
    """Pick the bullet list for a target count (exact variant, else nearest <=)."""
    variants = _bullet_variants(item)
    if not variants:
        return []
    if count is None:
        return list(variants[max(variants)])
    if count in variants:
        return list(variants[count])
    lower = [k for k in variants if k <= count]
    if lower:
        return list(variants[max(lower)])
    return list(variants[min(variants)])


def _config_year_from_value(value, fallback):
    if value is None:
        return fallback
    try:
        return int(str(value).strip())
    except ValueError:
        return fallback


def _parse_experience_year_range(dates_text):
    if not dates_text:
        return 0, 9999
    normalized = str(dates_text).lower()
    year_matches = re.findall(r"\d{4}", normalized)
    start_year = int(year_matches[0]) if year_matches else 0
    end_year = int(year_matches[-1]) if year_matches else CURRENT_YEAR
    if "present" in normalized:
        end_year = max(end_year, CURRENT_YEAR)
    return start_year, end_year


def _filter_experiences_by_years(experiences, min_start_year, min_end_year):
    filtered = []
    for entry in experiences:
        start_year, end_year = _parse_experience_year_range(entry.get("dates", ""))
        if min_start_year is not None and start_year < min_start_year:
            continue
        if min_end_year is not None and end_year < min_end_year:
            continue
        filtered.append(entry)
    return filtered


def _normalize_tagged_items(items):
    normalized = []
    for entry in items or []:
        if isinstance(entry, str):
            normalized.append({"name": entry, "tags": []})
        elif isinstance(entry, dict):
            normalized.append({
                "name": entry.get("name") or "",
                "tags": [
                    str(tag).strip().lower()
                    for tag in entry.get("tags", [])
                    if isinstance(tag, str) and tag.strip()
                ],
            })
    return normalized


def _order_tagged_items(items, resolved_industries):
    if not resolved_industries or not items:
        return items
    ordered = []
    seen = set()
    for key in resolved_industries:
        canonical = INDUSTRY_ALIASES.get(key, key)
        tags = INDUSTRY_TAGS.get(canonical, {canonical})
        for item in items:
            name = item.get("name", "")
            if not name:
                continue
            key_name = name.strip().lower()
            if key_name in seen:
                continue
            if any(tag in tags for tag in item.get("tags", [])):
                ordered.append(item)
                seen.add(key_name)
    for item in items:
        name = item.get("name", "")
        key_name = name.strip().lower()
        if name and key_name not in seen:
            ordered.append(item)
            seen.add(key_name)
    return ordered


def _order_section(values, tagged_items, resolved_industries):
    normalized_values = [str(v) for v in values or [] if isinstance(v, str)]
    ordered = []
    seen = set()
    tagged = _normalize_tagged_items(tagged_items)
    tagged_ordered = _order_tagged_items(tagged, resolved_industries)
    for item in tagged_ordered:
        name = item.get("name", "")
        key_name = name.strip().lower()
        if name and key_name not in seen:
            ordered.append(name)
            seen.add(key_name)
    for raw in normalized_values:
        key_name = raw.strip().lower()
        if key_name and key_name not in seen:
            ordered.append(raw.strip())
            seen.add(key_name)
    return ordered or normalized_values


def _expanded_tags_from_industries(resolved_industries):
    expanded = set()
    for key in resolved_industries or []:
        canonical = INDUSTRY_ALIASES.get(key, key)
        expanded |= INDUSTRY_TAGS.get(canonical, {key})
    return expanded


def _item_matches_tags(item, expanded_tags):
    if not expanded_tags:
        return True
    return any(tag in expanded_tags for tag in item.get("tags", []))


def _filter_tagged_names(values, tagged_items, resolved_industries, expanded_tags):
    ordered = _order_section(values, tagged_items, resolved_industries)
    if not expanded_tags:
        return ordered
    tagged = _normalize_tagged_items(tagged_items)
    allowed = set()
    for item in tagged:
        name = item.get("name", "")
        if not name:
            continue
        if _item_matches_tags(item, expanded_tags):
            allowed.add(name.strip().lower())
    return [name for name in ordered if name.strip().lower() in allowed]


def _filter_entries_by_tags(entries, expanded_tags):
    if not expanded_tags:
        return entries
    return [entry for entry in entries if _item_matches_tags(entry, expanded_tags)]

INDUSTRY_TAGS = {
    "systems": {"systems"},
    "networking": {"networking"},
    "ai_ml": {"ai_ml"},
    "formal_methods": {"formal_methods"},
    "fullstack": {"fullstack"},
    "math": {"math"},
    "pedagogy": {"pedagogy"},
    "compilers": {"compilers"},
    "quant_finance": {"quant_finance"},
    "security": {"security"},
}

INDUSTRY_ALIASES = {
    "systems": "systems",
    "os_dev": "systems",
    "networking": "networking",
    "ai": "ai_ml",
    "ml": "ai_ml",
    "ai_ml": "ai_ml",
    "formal_methods": "formal_methods",
    "formal_verification": "formal_methods",
    "fullstack": "fullstack",
    "swe": "fullstack",
    "math": "math",
    "pure_math": "math",
    "pedagogy": "pedagogy",
    "compilers": "compilers",
    "compiler": "compilers",
    "quant_finance": "quant_finance",
    "finance": "quant_finance",
    "quant": "quant_finance",
    "security": "security",
    "crypto": "security",
}

# ─── Config parsing ───
def _split_config_list(val):
    """Parse list values from [a, b] or bare a,b forms."""
    if isinstance(val, list):
        return [str(v).strip().strip("'\"").lower() for v in val if str(v).strip()]
    text = str(val).strip().strip("'\"")
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    return [v.strip().strip("'\"").lower() for v in text.split(",") if v.strip()]


_SECTION_DOT_RE = re.compile(r'^(research|experience|projects?)\.(.+)')


def parse_config_text(text):
    config = {}
    selector_steps = []
    current_section = None
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        block_open = re.match(r'^(\w+)\s*\{', line)
        if block_open:
            current_section = block_open.group(1).strip().lower().replace("-", "_")
            continue
        if line.startswith("}"):
            current_section = None
            continue
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip().lower().replace("-", "_")
        val = val.strip()
        if current_section:
            key = f"{current_section}.{key}"
        sec_match = _SECTION_DOT_RE.match(key)
        if sec_match:
            section = sec_match.group(1)
            if section == "project":
                section = "projects"
            sub_key = sec_match.group(2)
            sec_cfg = config.setdefault("_sections", {}).setdefault(section, {})
            if val.startswith("[") and val.endswith("]"):
                sec_cfg[sub_key] = _split_config_list(val)
            else:
                sec_cfg[sub_key] = val.strip("'\"")
            continue
        if val.startswith("[") and val.endswith("]"):
            val = _split_config_list(val)
        else:
            val = val.strip("'\"")
        if key in ("industry", "industries"):
            values = _split_config_list(val)
            selector_steps.append({"kind": "industry", "values": values})
            existing = config.get("industry", [])
            if isinstance(existing, str):
                existing = _split_config_list(existing)
            config["industry"] = existing + values
            continue
        if key in ("project", "projects"):
            values = _split_config_list(val)
            selector_steps.append({"kind": "projects", "values": values})
            existing = config.get("projects", [])
            if isinstance(existing, str):
                existing = _split_config_list(existing)
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
    earliest_start_year = _config_year_from_value(config.get("earliest_start_date"), DEFAULT_EARLIEST_START_YEAR)
    earliest_end_year = _config_year_from_value(config.get("earliest_end_date"), DEFAULT_EARLIEST_END_YEAR)
    industries = config.get("industry") or config.get("industries")
    selected_projects = config.get("projects") or config.get("project")
    if isinstance(industries, str):
        industries = [industries]
    if isinstance(selected_projects, str):
        selected_projects = [selected_projects]

    resolved_industries = []
    if industries:
        for i in industries:
            resolved_industries.extend(_split_config_list(i))

    expanded_tags = _expanded_tags_from_industries(resolved_industries)
    data["_ordering_industries"] = resolved_industries
    data["_expanded_tags"] = expanded_tags

    project_lookup = {}
    for item in source_cv.get("projects", []):
        pid = str(item.get("id", "")).strip().lower()
        pname = str(item.get("name", "")).strip().lower()
        if pid:
            project_lookup[pid] = item
        if pname:
            project_lookup[pname] = item

    def project_matches_tags(item, tags):
        return _item_matches_tags(item, tags)

    data["research"] = _filter_entries_by_tags(data.get("research", []), expanded_tags)
    data["experience"] = _filter_experiences_by_years(
        _filter_entries_by_tags(data.get("experience", []), expanded_tags),
        earliest_start_year,
        earliest_end_year,
    )

    edu = data.get("education", {})
    edu["coursework"] = _order_section(
        edu.get("coursework", []),
        edu.get("coursework_tags", []),
        resolved_industries,
    )
    data["education"] = edu
    data["awards"] = _order_section(
        data.get("awards", []),
        data.get("award_tags", []),
        resolved_industries,
    )
    data["language_tags"] = _order_tagged_items(
        _normalize_tagged_items(data.get("language_tags", [])),
        resolved_industries,
    )
    data["tools"] = _order_section(
        data.get("tools", []),
        data.get("tool_tags", []),
        resolved_industries,
    )

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
        for step in steps:
            if step["kind"] == "industry":
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

    sections_cfg = config.get("_sections", {})
    section_bounds = {}
    for section in ("research", "experience", "projects"):
        sec_cfg = sections_cfg.get(section, {})
        sec_min_raw = sec_cfg.get("min_bullets")
        sec_max_raw = sec_cfg.get("max_bullets")
        sec_min = None
        sec_max = None
        if sec_min_raw is not None and str(sec_min_raw).strip():
            sec_min = max(1, int(sec_min_raw))
        if sec_max_raw is not None and str(sec_max_raw).strip():
            sec_max = max(1, int(sec_max_raw))
        if sec_min is not None and sec_max is not None and sec_min > sec_max:
            sec_min, sec_max = sec_max, sec_min
        if sec_min is not None or sec_max is not None:
            sb = {}
            if sec_min is not None:
                sb["min"] = sec_min
            if sec_max is not None:
                sb["max"] = sec_max
            section_bounds[section] = sb

    bounds = {"min": min_b, "max": max_b}
    if section_bounds:
        bounds["sections"] = section_bounds
    data["_bullet_bounds"] = bounds

    for section in ("research", "experience", "projects"):
        _, sec_max = _section_bounds(bounds, section)
        if sec_max is not None:
            capped = []
            for it in data[section]:
                variants = {
                    k: v for k, v in _bullet_variants(it).items() if k <= sec_max
                }
                if variants:
                    capped.append({**it, "bullets": _store_bullet_variants(variants)})
            data[section] = capped

    for section in ("research", "experience", "projects"):
        cleaned = []
        for it in data[section]:
            variants = _bullet_variants(it)
            if variants:
                cleaned.append({**it, "bullets": _store_bullet_variants(variants)})
        data[section] = cleaned

    allow_empty = str(config.get("allow_empty", "false")).lower() == "true"
    if not allow_empty and not data["projects"]:
        data["projects"] = copy.deepcopy(source_cv.get("projects", []))[:3]
        _, p_max = _section_bounds(bounds, "projects")
        if p_max is not None:
            capped = []
            for it in data["projects"]:
                variants = {
                    k: v for k, v in _bullet_variants(it).items() if k <= p_max
                }
                if variants:
                    capped.append({**it, "bullets": _store_bullet_variants(variants)})
            data["projects"] = capped

    coursework_lines_raw = config.get("coursework_lines")
    if coursework_lines_raw is not None:
        try:
            data["_coursework_lines"] = max(1, int(str(coursework_lines_raw).strip()))
        except (TypeError, ValueError):
            pass

    return data


def pdf_title_from_config(config, filename_fallback, person_name):
    """Resolve PDF document metadata title from cfg or fall back to name + filename."""
    raw = config.get("title") or config.get("pdf_title")
    if raw is not None and str(raw).strip():
        return normalize_text(str(raw).strip())
    return f"{normalize_text(person_name)} - {filename_fallback}"

WHITESPACE_PATTERN = re.compile(r"(\s+)")


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


def _fit_text_one_line(c, text, font_name, font_size, max_width):
    text = normalize_text(text)
    if not text:
        return ""
    if c.stringWidth(text, font_name, font_size) <= max_width:
        return text
    trimmed = text
    while trimmed and c.stringWidth(trimmed, font_name, font_size) > max_width:
        trimmed = trimmed[:-1].rstrip(" ,;")
    return trimmed


def _fit_comma_list_one_line(c, items, font_name, font_size, max_width):
    names = [normalize_text(x) for x in items or [] if normalize_text(x).strip()]
    if not names:
        return ""
    joined = ", ".join(names)
    if c.stringWidth(joined, font_name, font_size) <= max_width:
        return joined
    while len(names) > 1:
        names = names[:-1]
        trial = ", ".join(names)
        if c.stringWidth(trial, font_name, font_size) <= max_width:
            return trial
    return _fit_text_one_line(c, names[0], font_name, font_size, max_width)


def draw_labelled_one_line(c, label, text, x, y, font_name=FONT_REGULAR, font_size=10, max_width=None, line_gap=12):
    label_text = f"{label}: "
    c.setFont(FONT_BOLD, font_size)
    c.drawString(x, y, label_text)
    text_x = x + c.stringWidth(label_text, FONT_BOLD, font_size)
    if max_width is None:
        max_width = WIDTH
    available_width = max(0, max_width - (text_x - x))
    normalized = normalize_text(text)
    if c.stringWidth(normalized, font_name, font_size) <= available_width:
        fitted = normalized
    else:
        fitted = _fit_text_one_line(c, normalized, font_name, font_size, available_width)
    c.setFont(font_name, font_size)
    c.drawString(text_x, y, fitted)
    return y - line_gap


def draw_bulleted_one_line(c, text, x, y, font_name="Helvetica", font_size=10, max_width=None, line_gap=12):
    bullet_x = x + 6
    text_x = x + 14
    if max_width is None:
        max_width = WIDTH
    available_width = max(0, max_width - (text_x - x))
    normalized = normalize_text(text)
    if c.stringWidth(normalized, font_name, font_size) <= available_width:
        fitted = normalized
    else:
        fitted = _fit_text_one_line(c, normalized, font_name, font_size, available_width)
    c.setFont(font_name, font_size)
    c.drawString(bullet_x, y, u"\u2022")
    c.drawString(text_x, y, fitted)
    return y - line_gap


def _markup_segments(text):
    """Split text into (segment, style) pairs for **bold** and `code` markup."""
    normalized = normalize_text(text)
    segments = []
    pos = 0
    for match in MARKUP_SEGMENT_PATTERN.finditer(normalized):
        if match.start() > pos:
            segments.append((normalized[pos:match.start()], "regular"))
        if match.group(1) is not None:
            segments.append((match.group(1), "bold"))
        else:
            segments.append((match.group(2), "code"))
        pos = match.end()
    if pos < len(normalized):
        segments.append((normalized[pos:], "regular"))
    if not segments:
        segments.append(("", "regular"))
    return segments


def wrap_markup_lines(c, text, font_size, max_width):
    """Wrap text containing **bold** and `code` markers into styled lines."""
    tokens = []
    for segment, style in _markup_segments(text):
        if not segment:
            continue
        for chunk in WHITESPACE_PATTERN.split(segment):
            if chunk:
                tokens.append((chunk, style))
    if not tokens:
        return [[(" ", "regular")]]

    lines = []
    current = []
    current_width = 0.0
    for token, style in tokens:
        font_name = _font_for_style(style)
        token_size = _size_for_style(style, font_size)
        token_width = c.stringWidth(token, font_name, token_size)
        if current and current_width + token_width > max_width:
            lines.append(current)
            current = []
            current_width = 0.0
            if token.isspace():
                continue
        if not current and token.isspace():
            continue
        current.append((token, style))
        current_width += token_width
    if current:
        lines.append(current)
    if not lines:
        lines.append([])
    return lines


def wrap_bolded_lines(c, text, font_size, max_width):
    """Backward-compatible alias for wrap_markup_lines."""
    return wrap_markup_lines(c, text, font_size, max_width)

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


def draw_labelled_line(c, label, text, x, y, font_name=FONT_REGULAR, font_size=10, max_width=None, line_gap=12):
    label_text = f"{label}: "
    c.setFont(FONT_BOLD, font_size)
    c.drawString(x, y, label_text)
    text_x = x + c.stringWidth(label_text, FONT_BOLD, font_size)
    if max_width is None:
        max_width = WIDTH
    available_width = max_width - (text_x - x)
    c.setFont(font_name, font_size)
    lines = wrap_text(c, text, font_name, font_size, available_width)
    for line in lines:
        c.drawString(text_x, y, line)
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
    label = normalize_text(title).upper()
    c.setFont(FONT_BOLD, SECTION_HEADER_SIZE)
    c.drawString(x, y, label)
    y -= sv(5)
    c.setLineWidth(0.8)
    c.line(x, y, RIGHT, y)
    return y - sv(18)

def maybe_new_page(c, y, needed_height, slack=sv(4)):
    if y - needed_height >= BOTTOM:
        return y
    c.showPage()
    return TOP

def _estimate_bullet_line_count(c, bullet, content_w):
    return max(1, len(wrap_markup_lines(c, bullet, BULLET_FONT_SIZE, content_w - 20)))


def _entry_content_height(c, item, content_w):
    """Ink height for an entry; trailing gap is excluded from keep-together checks."""
    total = sv(16)
    bullets = item.get("bullets", [])
    if isinstance(bullets, dict):
        bullets = _resolve_bullets(item, None)
    for bullet in bullets:
        total += sv(15) * _estimate_bullet_line_count(c, bullet, content_w)
    return total


def _estimate_entry_height(c, item, content_w):
    return _entry_content_height(c, item, content_w) + sv(8)


def _item_with_bullets(item, bullet_count):
    return {**item, "bullets": _resolve_bullets(item, bullet_count)}


def _estimate_section_header_height():
    # Cursor is already on the title baseline when draw_section_header runs;
    # only the underline offset and trailing gap move y downward.
    return sv(5) + sv(18)


def _entry_fits_together(c, item, y, content_w, floor_y=BOTTOM):
    return (y - _entry_content_height(c, item, content_w)) >= floor_y


def _layout_content_bottom_y(c, items, bullet_counts, start_y, content_w, floor_y=BOTTOM):
    """Simulate draw cursor; final value is the ink bottom (before last trailing gap)."""
    y = start_y
    for item, bullet_count in zip(items, bullet_counts):
        candidate = _item_with_bullets(item, bullet_count)
        if not _entry_fits_together(c, candidate, y, content_w, floor_y):
            return floor_y - 1
        y -= _estimate_entry_height(c, candidate, content_w)
    return y + sv(8)


def _layout_bottom_y(c, items, bullet_counts, start_y, content_w, keep_entry_together=False, floor_y=BOTTOM):
    y = start_y
    for item, bullet_count in zip(items, bullet_counts):
        candidate = _item_with_bullets(item, bullet_count)
        if keep_entry_together and not _entry_fits_together(c, candidate, y, content_w, floor_y):
            return floor_y - 1
        y -= _estimate_entry_height(c, candidate, content_w)
    return y


def _per_item_bullet_cap(item, max_bullets):
    keys = _available_bullet_counts(item, max_bullets=max_bullets)
    return keys[-1] if keys else 0


def _next_lower_bullet_count(item, current, min_bullets, max_bullets):
    keys = [
        k
        for k in _available_bullet_counts(item, max_bullets=max_bullets, min_bullets=min_bullets)
        if k < current
    ]
    return keys[-1] if keys else None


def _next_higher_bullet_count(item, current, max_bullets):
    keys = [
        k for k in _available_bullet_counts(item, max_bullets=max_bullets) if k > current
    ]
    return keys[0] if keys else None


def _simulate_tailored_bottom(c, plan, start_y, content_w):
    """Return ink-bottom y after planned sections (BOTTOM-1 if overflow)."""
    y = start_y
    drew_entry = False
    for key in ("research", "experience", "projects"):
        items = plan.get(key) or []
        if not items:
            continue
        y -= _estimate_section_header_height()
        for item, count in items:
            candidate = _item_with_bullets(item, count)
            if not _entry_fits_together(c, candidate, y, content_w):
                return BOTTOM - 1
            y -= _estimate_entry_height(c, candidate, content_w)
            drew_entry = True
    # Match _layout_content_bottom_y: exclude the last entry's trailing gap.
    return y + sv(8) if drew_entry else y


def _pack_bullet_counts(c, items, start_y, min_bullets, max_bullets, content_w, floor_y=BOTTOM):
    """Fit as many leading items as possible with balanced bullet counts.

    Trim the currently densest entries first (later ties first); refill the
    currently sparsest entries first (earlier ties first). Avoids lopsided
    layouts like [3, 3, 1, 1] when [2, 2, 2, 2] / [2, 2, 2, 1] would fit.
    """
    if not items:
        return []

    lo = max(1, min_bullets) if min_bullets is not None else 1

    def fits(subset, counts):
        return _layout_content_bottom_y(c, subset, counts, start_y, content_w, floor_y) >= floor_y

    for item_count in range(len(items), 0, -1):
        subset = items[:item_count]
        caps = [_per_item_bullet_cap(it, max_bullets) for it in subset]
        if any(cap < lo for cap in caps):
            # Entry lacks a variant at/above min_bullets; fall back to its largest available.
            counts = []
            for it, cap in zip(subset, caps):
                available = _available_bullet_counts(it, max_bullets=max_bullets)
                if not available:
                    counts = None
                    break
                counts.append(max(available))
            if counts is None:
                continue
        else:
            counts = caps[:]

        # Trim densest-first so remaining counts stay as even as possible.
        while not fits(subset, counts):
            candidates = []
            for idx in range(item_count):
                nxt = _next_lower_bullet_count(subset[idx], counts[idx], lo, max_bullets)
                if nxt is not None:
                    candidates.append((counts[idx], idx, nxt))
            if not candidates:
                break
            # Highest count first; among ties, trim later entries first.
            _, idx, nxt = max(candidates, key=lambda t: (t[0], t[1]))
            counts[idx] = nxt

        if not fits(subset, counts):
            continue

        # Refill sparsest-first so extras spread evenly instead of stacking up front.
        changed = True
        while changed:
            changed = False
            candidates = []
            for idx in range(item_count):
                nxt = _next_higher_bullet_count(subset[idx], counts[idx], max_bullets)
                if nxt is not None and nxt <= caps[idx]:
                    candidates.append((counts[idx], idx, nxt))
            # Lowest count first; among ties, prefer earlier entries.
            candidates.sort(key=lambda t: (t[0], t[1]))
            for _, idx, nxt in candidates:
                trial = counts[:]
                trial[idx] = nxt
                if fits(subset, trial):
                    counts = trial
                    changed = True
                    break

        return list(zip(subset, counts))

    first = items[0]
    available = _available_bullet_counts(first, max_bullets=max_bullets)
    if not available:
        return [(first, lo)]
    for count in reversed(available):
        if count < lo and count != available[0]:
            continue
        if _layout_content_bottom_y(c, [first], [count], start_y, content_w, floor_y) >= floor_y:
            return [(first, count)]
    return [(first, available[0])]


def _plan_one_page_layout(c, items, start_y, min_bullets, max_bullets, content_w, floor_y=BOTTOM):
    return _pack_bullet_counts(c, items, start_y, min_bullets, max_bullets, content_w, floor_y)


def _layout_metrics(c, plan, start_y, content_w):
    bottom_y = _simulate_tailored_bottom(c, plan, start_y, content_w)
    usable = max(1.0, start_y - BOTTOM)
    slack = max(0.0, bottom_y - BOTTOM) if bottom_y >= BOTTOM else usable
    fill = 1.0 - (slack / usable) if bottom_y >= BOTTOM else 0.0
    project_items = plan.get("projects") or []
    research_items = plan.get("research") or []
    experience_items = plan.get("experience") or []
    bullets = sum(count for _, count in research_items + experience_items + project_items)
    project_bullets = sum(count for _, count in project_items)
    project_n = len(project_items)
    avg_project_bullets = (project_bullets / project_n) if project_n else 0.0
    return {
        "slack": slack,
        "fill": fill,
        "bullets": bullets,
        "project_bullets": project_bullets,
        "avg_project_bullets": avg_project_bullets,
        "projects": project_n,
        "experience": len(experience_items),
        "research": len(research_items),
        "bottom_y": bottom_y,
    }


def _pack_prefix_sections(c, research, experience, r_min, r_max, e_min, e_max, start_y, content_w):
    """Keep research/experience when possible; trim trailing entries/bullets first."""
    research_n = len(research)
    experience_n = len(experience)

    def try_counts(r_items, e_items):
        y = start_y
        r_plan = []
        e_plan = []
        if r_items:
            y -= _estimate_section_header_height()
            r_plan = _pack_bullet_counts(c, r_items, y, r_min, r_max, content_w)
            if not r_plan and r_items:
                return None
            for item, count in r_plan:
                y -= _estimate_entry_height(c, _item_with_bullets(item, count), content_w)
        if e_items:
            y -= _estimate_section_header_height()
            e_plan = _pack_bullet_counts(c, e_items, y, e_min, e_max, content_w)
            if not e_plan and e_items:
                return None
            for item, count in e_plan:
                y -= _estimate_entry_height(c, _item_with_bullets(item, count), content_w)
        ink_y = y + sv(8) if (r_plan or e_plan) else y
        if ink_y < BOTTOM:
            return None
        return {"research": r_plan, "experience": e_plan, "y_after": y}

    for drop_e in range(experience_n + 1):
        for drop_r in range(research_n + 1):
            r_items = research[: research_n - drop_r]
            e_items = experience[: experience_n - drop_e]
            packed = try_counts(r_items, e_items)
            if packed is not None:
                return packed
    return {"research": [], "experience": [], "y_after": start_y}


def _enumerate_tailored_plans(c, data, start_y, bounds, content_w):
    research = list(data.get("research", []))
    experience = list(data.get("experience", []))
    projects = list(data.get("projects", []))

    r_min, r_max = _section_bounds(bounds, "research")
    e_min, e_max = _section_bounds(bounds, "experience")
    p_min, p_max = _section_bounds(bounds, "projects")

    prefix_maxes = [v for v in (r_max, e_max) if v is not None]
    hi = max(prefix_maxes) if prefix_maxes else None

    prefix_caps = []
    if hi is None:
        prefix_caps = [None]
    else:
        for cap in range(hi, 0, -1):
            prefix_caps.append(cap)
        if not prefix_caps:
            prefix_caps = [hi]

    plans = []
    seen = set()

    for prefix_cap in prefix_caps:
        eff_r_min = r_min
        if prefix_cap is not None and (eff_r_min is None or prefix_cap < eff_r_min):
            eff_r_min = prefix_cap
        eff_e_min = e_min
        if prefix_cap is not None and (eff_e_min is None or prefix_cap < eff_e_min):
            eff_e_min = prefix_cap
        prefix = _pack_prefix_sections(
            c, research, experience,
            eff_r_min, prefix_cap, eff_e_min, prefix_cap,
            start_y, content_w,
        )
        y_projects = prefix["y_after"]

        for project_count in range(len(projects), 0, -1):
            subset = projects[:project_count]
            header_y = y_projects - _estimate_section_header_height()
            packed = _pack_bullet_counts(
                c, subset, header_y, p_min, p_max, content_w
            )
            if not packed or len(packed) < project_count:
                continue
            plan = {
                "research": prefix["research"],
                "experience": prefix["experience"],
                "projects": packed,
            }
            metrics = _layout_metrics(c, plan, start_y, content_w)
            if metrics["bottom_y"] < BOTTOM:
                continue
            sig = (
                tuple((it.get("id"), count) for it, count in plan["research"]),
                tuple((it.get("id"), count) for it, count in plan["experience"]),
                tuple((it.get("id"), count) for it, count in plan["projects"]),
            )
            if sig in seen:
                continue
            seen.add(sig)
            plans.append({"plan": plan, "metrics": metrics})

        if prefix["research"] or prefix["experience"]:
            plan = {
                "research": prefix["research"],
                "experience": prefix["experience"],
                "projects": [],
            }
            metrics = _layout_metrics(c, plan, start_y, content_w)
            if metrics["bottom_y"] >= BOTTOM:
                sig = (
                    tuple((it.get("id"), count) for it, count in plan["research"]),
                    tuple((it.get("id"), count) for it, count in plan["experience"]),
                    tuple(),
                )
                if sig not in seen:
                    seen.add(sig)
                    plans.append({"plan": plan, "metrics": metrics})

    return plans


def _most_projects_key(entry):
    m = entry["metrics"]
    # Max project count first; among ties prefer more project bullets, then denser page.
    return (m["projects"], m["project_bullets"], m["fill"])


def _most_bullets_key(entry):
    m = entry["metrics"]
    # Total project bullets so 2×3 beats 1×4; fill still breaks near-ties.
    return (m["project_bullets"], m["fill"], m["avg_project_bullets"], m["projects"])


def _tightest_fill_key(entry):
    m = entry["metrics"]
    return (m["fill"], m["project_bullets"], m["projects"])


def _select_layout_candidates(scored_plans):
    """Pick up to three diverse layouts: most projects, most project bullets, densest fill."""
    if not scored_plans:
        return []

    def clone(entry, layout_id, label):
        return {
            "id": layout_id,
            "label": label,
            "plan": entry["plan"],
            "metrics": entry["metrics"],
        }

    def plan_sig(entry):
        plan = entry.get("plan") or {}
        return (
            tuple((it.get("id"), count) for it, count in plan.get("research", [])),
            tuple((it.get("id"), count) for it, count in plan.get("experience", [])),
            tuple((it.get("id"), count) for it, count in plan.get("projects", [])),
        )

    chosen = []
    seen = set()

    def add(entry, layout_id, label):
        sig = plan_sig(entry)
        if sig in seen:
            return False
        seen.add(sig)
        chosen.append(clone(entry, layout_id, label))
        return True

    def add_best(key_fn, layout_id, label_fn):
        ranked = sorted(scored_plans, key=key_fn, reverse=True)
        for entry in ranked:
            label = label_fn(entry) if callable(label_fn) else label_fn
            if add(entry, layout_id, label):
                return entry
        return None

    # Prefer most-projects first so that option is never displaced by densest-fill.
    max_projects = max(e["metrics"]["projects"] for e in scored_plans)
    max_project_bullets = max(e["metrics"]["project_bullets"] for e in scored_plans)
    add_best(
        _most_projects_key,
        "most_projects",
        lambda e: (
            "Most projects"
            if e["metrics"]["projects"] >= max_projects
            else "More projects"
        ),
    )
    add_best(
        _most_bullets_key,
        "most_bullets",
        lambda e: (
            "Most bullets"
            if e["metrics"]["project_bullets"] >= max_project_bullets
            else "More bullets"
        ),
    )
    # Tightest fill must be the literal densest plan — never a leftover runner-up.
    true_tightest = max(scored_plans, key=_tightest_fill_key)
    if not add(true_tightest, "tightest_fill", "Tightest fill"):
        for cand in chosen:
            if plan_sig(cand) == plan_sig(true_tightest):
                if "tightest" not in cand["label"].lower():
                    cand["label"] = f"{cand['label']} · Tightest fill"
                break

    leftovers = sorted(scored_plans, key=_tightest_fill_key, reverse=True)
    for idx, entry in enumerate(leftovers):
        if len(chosen) >= 3:
            break
        add(entry, f"alt_{idx}", "Alternative")
    return chosen[:3]


def _plan_tailored_resume(c, data, start_y, bounds, content_w, strategy=None):
    scored = _enumerate_tailored_plans(c, data, start_y, bounds, content_w)
    if not scored:
        return {"research": [], "experience": [], "projects": []}
    strategy_key = (strategy or "tightest_fill").strip().lower()
    if strategy_key in ("most_projects", "projects"):
        best = max(scored, key=_most_projects_key)
    elif strategy_key in ("most_bullets", "bullets"):
        best = max(scored, key=_most_bullets_key)
    else:
        best = max(scored, key=_tightest_fill_key)
    return best["plan"]


def _layout_summary(metrics):
    slack_in = metrics["slack"] / 72.0
    avg = metrics.get("avg_project_bullets", 0.0)
    return (
        f"{metrics['projects']} projects · {avg:.1f} bullets/project · "
        f"~{slack_in:.2f}\" free"
    )


def _draw_single_entry(c, candidate, y, left, right, content_w, allow_new_page, keep_entry_together):
    if keep_entry_together:
        if not _entry_fits_together(c, candidate, y, content_w):
            if not allow_new_page:
                return y, False
            y = maybe_new_page(c, y, _estimate_entry_height(c, candidate, content_w) + sv(10))
    elif (y - sv(28)) < BOTTOM:
        if not allow_new_page:
            return y, False
        y = maybe_new_page(c, y, sv(28))
    c.setFont(FONT_BOLD, ENTRY_TITLE_SIZE)
    name_text = normalize_text(candidate["name"])
    c.drawString(left, y, name_text)
    c.setFont(FONT_ITALIC, ENTRY_TITLE_SIZE)
    subtitle = normalize_text(candidate["subtitle"])
    name_end = left + c.stringWidth(name_text, FONT_BOLD, ENTRY_TITLE_SIZE)
    c.setFont(FONT_REGULAR, ENTRY_TITLE_SIZE)
    c.drawString(name_end + 1, y, ", ")
    c.setFont(FONT_ITALIC, ENTRY_TITLE_SIZE)
    c.drawString(name_end + 8, y, subtitle)
    dates_text = normalize_text(candidate.get("dates", ""))
    if dates_text:
        c.setFont(FONT_REGULAR, ENTRY_TITLE_SIZE)
        c.drawRightString(right, y, dates_text)
    y -= sv(16)
    for bullet in candidate["bullets"]:
        if allow_new_page:
            y = maybe_new_page(c, y, sv(32))
        bullet_lines = wrap_markup_lines(c, bullet, BULLET_FONT_SIZE, content_w - 20)
        for idx, line in enumerate(bullet_lines):
            if not line:
                continue
            if idx == 0:
                c.setFont(FONT_REGULAR, BULLET_FONT_SIZE)
                c.drawString(left + 6, y, u"\u2022")
            x_cursor = left + 14
            for token, style in line:
                font = _font_for_style(style)
                size = _size_for_style(style, BULLET_FONT_SIZE)
                c.setFont(font, size)
                c.drawString(x_cursor, y, token)
                x_cursor += c.stringWidth(token, font, size)
            y -= sv(15)
    y -= sv(8)
    return y, True


def draw_entries(
    c,
    y,
    title,
    items,
    allow_new_page=True,
    min_bullets=None,
    max_bullets=None,
    keep_entry_together=True,
    planned=None,
):
    if planned is None and not items:
        return y, 0
    if planned is not None and not planned:
        return y, 0
    left = LEFT
    right = RIGHT
    content_w = WIDTH

    if allow_new_page:
        y = maybe_new_page(c, y, sv(36))
    y = draw_section_header(c, title, left, y)
    drawn = 0

    if planned is not None:
        draw_list = planned
    elif not allow_new_page and keep_entry_together:
        draw_list = _plan_one_page_layout(c, items, y, min_bullets, max_bullets, content_w)
    else:
        draw_list = [
            (it, _per_item_bullet_cap(it, max_bullets) if max_bullets is not None else None)
            for it in items
        ]

    for it, bullet_count in draw_list:
        candidate = _item_with_bullets(it, bullet_count)
        y, ok = _draw_single_entry(
            c, candidate, y, left, right, content_w, allow_new_page, keep_entry_together
        )
        if ok:
            drawn += 1
        elif not allow_new_page:
            break
    return y, drawn

def _draw_resume_prefix(c, data):
    """Draw name/contact/education/skills; return y ready for research/experience/projects."""
    y = TOP
    row1_y = y - sv(1)
    row2_y = y - sv(18)
    name_y = row2_y - sv(2)

    c.setFont(FONT_REGULAR, 29)
    c.drawString(LEFT, name_y, normalize_text(data["name"]))
    c.setFont(FONT_REGULAR, 11)
    c.drawRightString(RIGHT, row1_y, f"{normalize_text(data['location'])} | {normalize_text(data['email'])}")
    c.drawRightString(
        RIGHT,
        row2_y,
        f"{normalize_text(data['phone'])} | GitHub | LinkedIn",
    )
    y = row2_y - sv(24)

    c.linkURL(f"mailto:{data['email']}", (RIGHT - 142, row1_y - 3, RIGHT, row1_y + 9), relative=0)
    c.linkURL(
        f"tel:{''.join(ch for ch in data['phone'] if ch.isdigit() or ch == '+')}",
        (RIGHT - 206, row2_y - 3, RIGHT - 126, row2_y + 9),
        relative=0,
    )
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

    ordering = data.get("_ordering_industries", [])
    coursework_values = edu.get("coursework", [])
    coursework_tags = edu.get("coursework_tags", [])
    coursework_ordered = _order_section(coursework_values, coursework_tags, ordering)
    if coursework_ordered:
        coursework_text = "Relevant Coursework: " + ", ".join(
            normalize_text(x) for x in coursework_ordered
        )
        max_cw_lines = data.get("_coursework_lines")
        if max_cw_lines == 1:
            y = draw_bulleted_one_line(
                c, coursework_text, LEFT, y, FONT_REGULAR, 10, WIDTH, sv(16),
            )
        elif max_cw_lines is not None:
            text_x = LEFT + 14
            available_width = WIDTH - (text_x - LEFT)
            lines = wrap_text(c, coursework_text, FONT_REGULAR, 10, available_width)[:max_cw_lines]
            c.setFont(FONT_REGULAR, 10)
            c.drawString(LEFT + 6, y, u"•")
            for line in lines:
                c.drawString(text_x, y, line)
                y -= sv(16)
        else:
            y = draw_bulleted_line(
                c, coursework_text, LEFT, y, FONT_REGULAR, 10, WIDTH, sv(16),
            )
    y -= sv(12)

    y = draw_section_header(c, "Technical Skills and Awards", LEFT, y)
    awards = data.get("awards", [])
    if awards:
        awards_label = "Awards and Recognitions"
        awards_label_w = c.stringWidth(f"{awards_label}: ", FONT_BOLD, 10)
        awards_body = _fit_comma_list_one_line(
            c, awards, FONT_REGULAR, 10, max(0, WIDTH - awards_label_w)
        )
        y = draw_labelled_one_line(
            c, awards_label, awards_body, LEFT, y, FONT_REGULAR, 10, WIDTH, sv(16)
        )
    languages_ordered = _order_section([], data.get("language_tags", []), ordering)
    if languages_ordered:
        languages_label = "Languages"
        languages_label_w = c.stringWidth(f"{languages_label}: ", FONT_BOLD, 10)
        languages_body = _fit_comma_list_one_line(
            c, languages_ordered, FONT_REGULAR, 10, max(0, WIDTH - languages_label_w)
        )
        y = draw_labelled_one_line(
            c, languages_label, languages_body, LEFT, y, FONT_REGULAR, 10, WIDTH, sv(16)
        )
    tools = data.get("tools", [])
    if tools:
        tools_label = "Tools & Libraries"
        tools_label_w = c.stringWidth(f"{tools_label}: ", FONT_BOLD, 10)
        tools_body = _fit_comma_list_one_line(
            c, tools, FONT_REGULAR, 10, max(0, WIDTH - tools_label_w)
        )
        y = draw_labelled_one_line(
            c, tools_label, tools_body, LEFT, y, FONT_REGULAR, 10, WIDTH, sv(16)
        )
    y -= sv(10)
    return y


def generate_pdf(
    data,
    title="Resume",
    pdf_title=None,
    include_all_projects=False,
    layout_plan=None,
    layout_strategy=None,
):
    """Generate ATS-friendly PDF bytes with LaTeX-like visual layout."""
    try:
        buf = BytesIO()
        c = canvas.Canvas(buf, pagesize=letter, pageCompression=1)
        document_title = pdf_title or f"{normalize_text(data['name'])} - {title}"
        c.setTitle(document_title)
        c.setAuthor(data["name"])

        y = _draw_resume_prefix(c, data)
        bounds = data.get("_bullet_bounds", {}) if isinstance(data, dict) else {}

        if include_all_projects:
            r_min, r_max = _section_bounds(bounds, "research")
            e_min, e_max = _section_bounds(bounds, "experience")
            p_min, p_max = _section_bounds(bounds, "projects")
            y, _ = draw_entries(
                c, y, "Research", data.get("research", []),
                allow_new_page=True, min_bullets=r_min, max_bullets=r_max,
            )
            y, _ = draw_entries(
                c, y, "Experience", data.get("experience", []),
                allow_new_page=True, min_bullets=e_min, max_bullets=e_max,
            )
            y, _ = draw_entries(
                c,
                y,
                "Technical Projects",
                data.get("projects", []),
                allow_new_page=True,
                min_bullets=p_min,
                max_bullets=p_max,
                keep_entry_together=False,
            )
        else:
            tailored = layout_plan or _plan_tailored_resume(
                c, data, y, bounds, WIDTH, strategy=layout_strategy
            )
            y, _ = draw_entries(
                c,
                y,
                "Research",
                [],
                allow_new_page=False,
                keep_entry_together=True,
                planned=tailored.get("research", []),
            )
            y, _ = draw_entries(
                c,
                y,
                "Experience",
                [],
                allow_new_page=False,
                keep_entry_together=True,
                planned=tailored.get("experience", []),
            )
            y, _ = draw_entries(
                c,
                y,
                "Technical Projects",
                [],
                allow_new_page=False,
                keep_entry_together=True,
                planned=tailored.get("projects", []),
            )

        c.save()
        buf.seek(0)
        return buf.read(), None
    except Exception as e:
        return None, str(e)


def generate_layout_candidates(data, title="Resume", pdf_title=None):
    """Build up to three diversified one-page layout PDFs for the caller to choose."""
    measure_buf = BytesIO()
    measure_c = canvas.Canvas(measure_buf, pagesize=letter, pageCompression=1)
    start_y = _draw_resume_prefix(measure_c, data)
    bounds = data.get("_bullet_bounds", {}) if isinstance(data, dict) else {}
    scored = _enumerate_tailored_plans(measure_c, data, start_y, bounds, WIDTH)
    selected = _select_layout_candidates(scored)
    layouts = []
    for cand in selected:
        pdf_bytes, err = generate_pdf(
            data,
            title=title,
            pdf_title=pdf_title,
            include_all_projects=False,
            layout_plan=cand["plan"],
        )
        if not pdf_bytes:
            continue
        layouts.append(
            {
                "id": cand["id"],
                "label": cand["label"],
                "summary": _layout_summary(cand["metrics"]),
                "metrics": {
                    "projects": cand["metrics"]["projects"],
                    "bullets": cand["metrics"]["bullets"],
                    "avg_project_bullets": round(
                        cand["metrics"].get("avg_project_bullets", 0.0), 2
                    ),
                    "slack_in": round(cand["metrics"]["slack"] / 72.0, 2),
                    "fill": round(cand["metrics"]["fill"], 3),
                },
                "pdf_base64": base64.b64encode(pdf_bytes).decode("ascii"),
            }
        )
    return layouts

# ─── Pre-compile / cache CV PDF ───
def cv_pdf_is_stale():
    get_cv_data()
    updated = _cv_cache.get("updated")
    if _pdf_cache.get("bytes") is not None and _pdf_cache.get("updated") == updated:
        return False
    if not os.path.exists(CV_PDF_PATH):
        # Bundled repo PDF is usable until Mongo data is newer than the file mtime.
        if os.path.exists(REPO_CV_PDF_PATH) and updated is not None:
            try:
                if float(updated) <= os.path.getmtime(REPO_CV_PDF_PATH):
                    return False
            except OSError:
                pass
        return True
    if updated is None:
        return True
    return float(updated) > os.path.getmtime(CV_PDF_PATH)


def regenerate_cv_pdf(*, require_mongo=False):
    """Generate the full CV PDF into memory (and /tmp or repo path when writable).

    When require_mongo=True, always re-fetch CV JSON from MongoDB and never fall
    back to cv_data.json. Raises on Mongo failure.
    """
    if require_mongo:
        get_cv_data(refresh=True, allow_disk_fallback=False)
        print("[cv] Generating CV PDF from fresh MongoDB data...")
    else:
        print("[cv] Generating CV PDF from CV data cache/source...")
    filtered = filter_cv({}, force_include_all_projects=True)
    pdf_bytes, err = generate_pdf(filtered, "CV", include_all_projects=True)
    if not pdf_bytes:
        print(f"[cv] CV generation failed: {err}")
        return False
    _pdf_cache["bytes"] = pdf_bytes
    _pdf_cache["updated"] = _cv_cache.get("updated")
    try:
        with open(CV_PDF_PATH, "wb") as f:
            f.write(pdf_bytes)
        print(f"[cv] CV PDF generated ({len(pdf_bytes)} bytes) → {CV_PDF_PATH}")
    except OSError as exc:
        # Expected on a fully read-only FS; memory cache still serves the PDF.
        print(f"[cv] CV PDF kept in memory only (disk write failed: {exc})")
    return True


def load_pdf_bytes_from_disk():
    for path in (CV_PDF_PATH, REPO_CV_PDF_PATH):
        if not os.path.exists(path):
            continue
        try:
            with open(path, "rb") as f:
                return f.read()
        except OSError:
            continue
    return None


def get_cv_pdf_bytes(force=False):
    """Return current CV PDF bytes, regenerating when Mongo data is newer.

    When force=True, drop memory caches, reload CV data from Mongo only
    (never cv_data.json), and rebuild the PDF from scratch. Raises if Mongo
    is unavailable so callers can return an error instead of a stale PDF.
    """
    if force:
        _cv_cache["data"] = None
        _cv_cache["updated"] = None
        _cv_cache["source"] = None
        _pdf_cache["bytes"] = None
        _pdf_cache["updated"] = None
        if not regenerate_cv_pdf(require_mongo=True):
            raise RuntimeError("PDF regeneration from MongoDB failed")
        return _pdf_cache.get("bytes")

    get_cv_data(refresh=True)
    updated = _cv_cache.get("updated")
    if _pdf_cache.get("bytes") is not None and _pdf_cache.get("updated") == updated:
        return _pdf_cache["bytes"]

    if not cv_pdf_is_stale():
        disk = load_pdf_bytes_from_disk()
        if disk:
            _pdf_cache["bytes"] = disk
            _pdf_cache["updated"] = updated
            return disk

    if regenerate_cv_pdf():
        return _pdf_cache.get("bytes")
    return load_pdf_bytes_from_disk()


def ensure_cv_pdf():
    get_cv_pdf_bytes()

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
    force = request.args.get("force", "").strip().lower() in ("1", "true", "yes")
    try:
        pdf_bytes = get_cv_pdf_bytes(force=force)
    except Exception as exc:
        print(f"[cv] force regenerate failed: {exc}")
        return jsonify({"error": str(exc), "source": "mongodb"}), 500
    if pdf_bytes:
        return send_file(
            BytesIO(pdf_bytes),
            mimetype="application/pdf",
            download_name="advayth_pashupati_cv.pdf",
        )
    return jsonify({"error": "CV PDF not available"}), 500


@app.route("/api/data")
def get_cv_json():
    """Expose the latest CV JSON (MongoDB) to automated agents."""
    try:
        # Always re-read Mongo so warm Vercel instances / long-lived local
        # servers don't serve a stale in-memory snapshot to the editor.
        data = get_cv_data(refresh=True)
        updated = _cv_cache.get("updated")
        return jsonify({
            "source": _cv_cache.get("source") or "unknown",
            "updated": updated,
            "data": data,
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/cv", methods=["PUT", "OPTIONS"])
def put_cv():
    if request.method == "OPTIONS":
        return "", 204
    if not is_authenticated():
        return jsonify({"error": "Authentication required"}), 401
    payload = request.get_json(force=True, silent=True)
    if payload is None:
        return jsonify({"error": "Invalid JSON body"}), 400
    data = payload.get("data", payload) if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return jsonify({"error": "CV data must be a JSON object"}), 400
    try:
        updated = save_cv_data(data)
        ok = regenerate_cv_pdf(require_mongo=True)
        if not ok:
            return jsonify({
                "error": "Saved to MongoDB but PDF regeneration failed",
                "updated": updated,
                "source": "mongodb",
            }), 500
        return jsonify({"ok": True, "updated": updated, "source": "mongodb"})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/comments", methods=["GET", "POST", "OPTIONS"])
def comments_collection():
    if request.method == "OPTIONS":
        return "", 204
    if not is_authenticated():
        return jsonify({"error": "Authentication required"}), 401

    if request.method == "GET":
        try:
            return jsonify({"comments": get_comments(refresh=True)})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    payload = request.get_json(force=True, silent=True) or {}
    section = (payload.get("section") or "").strip()
    entry_id = (payload.get("entryId") or "").strip()
    body = payload.get("body", "")
    author = (payload.get("author") or "").strip() or "Editor"
    if section not in COMMENT_SECTIONS:
        return jsonify({"error": "section must be research, experience, or projects"}), 400
    if not entry_id:
        return jsonify({"error": "entryId is required"}), 400
    if not isinstance(body, str) or not body.strip():
        return jsonify({"error": "body is required"}), 400
    try:
        bullet = normalize_comment_bullet(payload.get("bullet"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    comment = {
        "id": new_comment_id(),
        "section": section,
        "entryId": entry_id,
        "bullet": bullet,
        "body": body.strip(),
        "author": author[:80],
        "created": time.time(),
        "resolved": False,
    }
    try:
        comments = get_comments()
        comments.append(comment)
        updated = save_comments(comments)
        return jsonify({"ok": True, "comment": comment, "updated": updated}), 201
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/comments/<comment_id>", methods=["PATCH", "DELETE", "OPTIONS"])
def comments_item(comment_id):
    if request.method == "OPTIONS":
        return "", 204
    if not is_authenticated():
        return jsonify({"error": "Authentication required"}), 401

    comments = get_comments()
    idx = next((i for i, c in enumerate(comments) if c.get("id") == comment_id), None)
    if idx is None:
        return jsonify({"error": "Comment not found"}), 404

    if request.method == "DELETE":
        try:
            comments.pop(idx)
            updated = save_comments(comments)
            return jsonify({"ok": True, "updated": updated})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    payload = request.get_json(force=True, silent=True) or {}
    comment = comments[idx]
    if "body" in payload:
        body = payload.get("body")
        if not isinstance(body, str) or not body.strip():
            return jsonify({"error": "body must be a non-empty string"}), 400
        comment["body"] = body.strip()
    if "resolved" in payload:
        comment["resolved"] = bool(payload.get("resolved"))
    if "author" in payload:
        author = payload.get("author")
        if not isinstance(author, str) or not author.strip():
            return jsonify({"error": "author must be a non-empty string"}), 400
        comment["author"] = author.strip()[:80]
    comments[idx] = comment
    try:
        updated = save_comments(comments)
        return jsonify({"ok": True, "comment": comment, "updated": updated})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/auth/login", methods=["POST", "OPTIONS"])
def auth_login():
    if request.method == "OPTIONS":
        return "", 204
    if not EDIT_PASSWORD:
        return jsonify({"error": "EDIT_PASSWORD is not configured on the server"}), 500
    body = request.get_json(force=True, silent=True) or {}
    password = body.get("password", "")
    if not password or password != EDIT_PASSWORD:
        return jsonify({"error": "Invalid password"}), 401
    session.clear()
    session["authenticated"] = True
    return jsonify({"authenticated": True})


@app.route("/api/auth/logout", methods=["POST", "OPTIONS"])
def auth_logout():
    if request.method == "OPTIONS":
        return "", 204
    session.clear()
    return jsonify({"authenticated": False})


@app.route("/api/auth/me")
def auth_me():
    return jsonify({"authenticated": is_authenticated()})


@app.route("/api/compile", methods=["POST", "OPTIONS"])
def compile_resume():
    if request.method == "OPTIONS":
        return "", 204
    data = request.get_json(force=True)
    config_text = data.get("config", "")
    filename = data.get("filename", "resume")
    want_candidates = bool(data.get("candidates"))
    layout_strategy = data.get("layout") or data.get("layout_strategy")
    if not config_text.strip():
        return jsonify({"error": "Empty config"}), 400

    config = parse_config_text(config_text)
    if not config:
        return jsonify({"error": "No valid key=value pairs found"}), 400
    if layout_strategy is None:
        layout_strategy = config.get("layout")

    filtered = filter_cv(config)
    pdf_title = pdf_title_from_config(config, filename, filtered["name"])

    if want_candidates:
        layouts = generate_layout_candidates(filtered, title=filename, pdf_title=pdf_title)
        if not layouts:
            return jsonify({"error": "No feasible one-page layouts"}), 500
        return jsonify({"filename": filename, "layouts": layouts})

    pdf_bytes, err = generate_pdf(
        filtered,
        title=filename,
        pdf_title=pdf_title,
        layout_strategy=layout_strategy,
    )

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
    pdf_title = pdf_title_from_config(config, filename, filtered["name"])
    pdf_bytes, err = generate_pdf(filtered, title=filename, pdf_title=pdf_title, include_all_projects=True)

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
    pdf_title = pdf_title_from_config(config, "resume", filtered["name"])
    pdf_bytes, err = generate_pdf(filtered, title="resume", pdf_title=pdf_title)

    if pdf_bytes:
        return send_file(BytesIO(pdf_bytes), mimetype="application/pdf",
                         download_name="resume.pdf")
    return jsonify({"error": f"PDF generation failed: {err}"}), 500

@app.route("/api/health")
def health():
    return jsonify({
        "status": "ok",
        "pdf_engine": "reportlab",
        "vercel": IS_VERCEL,
        "secure_cookie": SESSION_COOKIE_SECURE,
    })


if __name__ == "__main__":
    ensure_runtime()
    ensure_cv_pdf()
    port = int(os.environ.get("PORT", "5000"))
    print(f"[server] Starting on http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
