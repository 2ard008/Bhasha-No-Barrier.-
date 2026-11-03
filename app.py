import json
import os
import sqlite3
import base64
import binascii
import tempfile
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from sarvamai import SarvamAI


ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "bhasha.sqlite3"
MODEL = "sarvam-105b-conversations"
PORT = 8000
CATEGORIES = {
    "mathematics",
    "science",
    "language",
    "social_studies",
    "technology",
    "everyday_life",
    "general",
}


def load_project_env():
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8-sig").splitlines():
        name, separator, value = line.partition("=")
        if separator and name.strip() == "SARVAM_API_KEY":
            os.environ.setdefault(name.strip(), value.strip().strip("\"'"))
            break


def open_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            updated_at TEXT,
            title TEXT,
            category TEXT NOT NULL DEFAULT 'general',
            practice_json TEXT NOT NULL DEFAULT '[]'
        )"""
    )
    session_columns = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)")}
    for column, definition in {
        "updated_at": "TEXT",
        "title": "TEXT",
        "category": "TEXT NOT NULL DEFAULT 'general'",
        "practice_json": "TEXT NOT NULL DEFAULT '[]'",
    }.items():
        if column not in session_columns:
            conn.execute(f"ALTER TABLE sessions ADD COLUMN {column} {definition}")
    conn.execute("UPDATE sessions SET updated_at = created_at WHERE updated_at IS NULL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id),
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            extras_json TEXT NOT NULL DEFAULT '{}'
        )"""
    )
    message_columns = {row["name"] for row in conn.execute("PRAGMA table_info(messages)")}
    if "extras_json" not in message_columns:
        conn.execute("ALTER TABLE messages ADD COLUMN extras_json TEXT NOT NULL DEFAULT '{}'")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS learner_profile (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            data TEXT NOT NULL
        )"""
    )
    conn.execute(
        "INSERT OR IGNORE INTO learner_profile (id, data) VALUES (1, ?)",
        (json.dumps({"strengths": [], "focus": [], "patterns": [], "misconceptions": []}),),
    )
    conn.commit()
    return conn


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_session(conn, session_id=None):
    try:
        session_id = str(uuid.UUID(session_id)) if session_id else str(uuid.uuid4())
    except (ValueError, TypeError, AttributeError):
        session_id = str(uuid.uuid4())
    timestamp = now_iso()
    conn.execute(
        "INSERT OR IGNORE INTO sessions (id, created_at, updated_at) VALUES (?, ?, ?)",
        (session_id, timestamp, timestamp),
    )
    conn.commit()
    return session_id


def read_profile(conn):
    row = conn.execute("SELECT data FROM learner_profile WHERE id = 1").fetchone()
    return json.loads(row["data"])


def session_state(conn, session_id):
    messages = conn.execute(
        "SELECT role, content, created_at FROM messages WHERE session_id = ? ORDER BY id",
        (session_id,),
    ).fetchall()
    session = conn.execute(
        "SELECT title, category, practice_json FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    return {
        "session_id": session_id,
        "title": session["title"],
        "category": session["category"],
        "messages": [
            {"role": row["role"], "content": row["content"], "created_at": row["created_at"], "extras": json.loads(row["extras_json"])}
            for row in messages
        ],
        "profile": read_profile(conn),
        "practice_questions": json.loads(session["practice_json"]),
    }


def merge_profile(profile, update, diagnosis):
    def merge_list(existing, incoming, limit=8):
        values = list(existing or [])
        if not isinstance(incoming, list):
            incoming = []
        for item in incoming or []:
            if isinstance(item, str) and item.strip() and item.strip() not in values:
                values.append(item.strip())
        return values[-limit:]

    profile["strengths"] = merge_list(profile.get("strengths"), update.get("strengths"))
    profile["focus"] = merge_list(profile.get("focus"), update.get("focus"), 5)
    profile["patterns"] = merge_list(profile.get("patterns"), update.get("patterns"), 8)
    misconception = diagnosis.get("misconception")
    if isinstance(misconception, str) and misconception.strip():
        profile.setdefault("misconceptions", []).append(
            {
                "concept": diagnosis.get("concept") if isinstance(diagnosis.get("concept"), str) else "Concept check",
                "misconception": misconception.strip(),
                "evidence": diagnosis.get("evidence") if isinstance(diagnosis.get("evidence"), str) else "",
                "date": now_iso()[:10],
            }
        )
        profile["misconceptions"] = profile["misconceptions"][-12:]
    return profile


def make_system_prompt(profile, language, selected_category):
    learner_model = json.dumps(profile, ensure_ascii=False)
    return f"""You are Bhasha, a warm, patient Socratic tutor. Help the learner understand, never just copy an answer.

Teaching rules:
- First, invite the learner to explain what they have tried and how they are thinking. Do this before teaching or solving if they have not shared their reasoning yet.
- Once they share their thinking, identify any conceptual misconception from their actual words. If evidence is thin, ask one clarifying question instead of diagnosing.
- Give one manageable hint, question, or next step at a time. Do not reveal the full answer or complete solution. Let the learner do the next piece of thinking.
- Be encouraging without being patronizing. Acknowledge sound reasoning specifically.
- Adapt your hint to the learner model below. Treat past patterns as clues, not assumptions; check whether they apply now.
- Reply in {language}. Keep technical terms clear and explain them simply.
- Classify the doubt using one exact category slug: mathematics, science, language, social_studies, technology, everyday_life, or general. Selected category from the learner: {selected_category or 'none'}.
- When the learner has not yet shared any reasoning, only invite them to explain their thinking; leave visual, examples, and future_uses empty.
- Once useful, give one concise real-world example and one future connection where the idea is used. Keep these separate from the tutor reply and never let them reveal the full answer to the current doubt.
- Choose a small visual aid that fits the concept. Return a simple diagram type and 2–4 short labels; the app draws it. Use one of: number_line, parts_whole, cycle, flow, compare, shapes, concept_map, or none.
- Set mastery to ready_to_practice only after the learner correctly explains the core idea in their own words or applies it correctly to a new step. A simple claim of understanding is not enough. Otherwise use exploring or not_yet.
- Only when mastery is ready_to_practice, provide 4 or 5 short related practice questions of increasing difficulty, without answers. Otherwise practice_questions must be an empty list.

Return only valid JSON, with exactly these fields:
{{"reply":"A short message the learner sees, ending with one useful question when appropriate.","concept":"brief topic name or empty string","category":"one exact category slug from the allowed list","misconception":"brief misconception supported by evidence, otherwise empty string","evidence":"short quote or paraphrase from the learner, otherwise empty string","learner_update":{{"strengths":["specific observed strength"],"focus":["skill to practise"],"patterns":["recurring error pattern only if supported"]}},"visual":{{"type":"allowed diagram type","title":"short heading","caption":"one-sentence visual explanation","labels":["short label"]}},"examples":["one concise real-world example"],"future_uses":["one related idea or future use"],"mastery":"exploring, not_yet, or ready_to_practice","practice_questions":["question"]}}
Do not include a final answer in reply. Keep reply to 1–3 short sentences. For empty fields, use empty strings, empty arrays, or visual type none.

Learner model from earlier sessions (private context):
{learner_model}"""


def generate_tutor_reply(history, profile, language, selected_category):
    load_project_env()
    api_key = os.environ.get("SARVAM_API_KEY")
    if not api_key:
        raise RuntimeError("SARVAM_API_KEY is missing. Add it to the project .env file.")
    client = SarvamAI(api_subscription_key=api_key)
    response = client.chat.completions(
        model=MODEL,
        messages=[
            {"role": "system", "content": make_system_prompt(profile, language, selected_category)},
            *history,
        ],
        max_tokens=700,
    )
    raw = response.choices[0].message.content or ""
    cleaned = raw.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    elif cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    try:
        data = json.loads(cleaned.strip())
        if not isinstance(data, dict) or not isinstance(data.get("reply"), str):
            raise ValueError("Missing reply")
    except (json.JSONDecodeError, ValueError):
        data = {
            "reply": raw.strip() or "Show me the steps you have tried so far. What feels clear, and where does it get tricky?",
            "concept": "",
            "category": selected_category or "general",
            "misconception": "",
            "evidence": "",
            "learner_update": {},
            "visual": {"type": "none", "labels": []},
            "examples": [],
            "future_uses": [],
            "mastery": "exploring",
            "practice_questions": [],
        }
    data.setdefault("learner_update", {})
    if not isinstance(data.get("category"), str) or data.get("category") not in CATEGORIES:
        data["category"] = selected_category if selected_category in CATEGORIES else "general"
    if not isinstance(data.get("mastery"), str) or data.get("mastery") not in {"exploring", "not_yet", "ready_to_practice"}:
        data["mastery"] = "exploring"
    if not isinstance(data.get("visual"), dict):
        data["visual"] = {"type": "none", "labels": []}
    if not isinstance(data["visual"].get("type"), str) or data["visual"].get("type") not in {"number_line", "parts_whole", "cycle", "flow", "compare", "shapes", "concept_map", "none"}:
        data["visual"]["type"] = "none"
    for key in ("examples", "future_uses", "practice_questions"):
        if not isinstance(data.get(key), list):
            data[key] = []
    if data["mastery"] == "ready_to_practice":
        data["practice_questions"] = [q for q in data["practice_questions"] if isinstance(q, str) and q.strip()][:5]
        if len(data["practice_questions"]) < 4:
            data["mastery"] = "exploring"
            data["practice_questions"] = []
    else:
        data["practice_questions"] = []
    return data


class AppHandler(BaseHTTPRequestHandler):
    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/state":
            requested = parse_qs(parsed.query).get("session_id", [None])[0]
            with open_db() as conn:
                session_id = ensure_session(conn, requested)
                return self.send_json(session_state(conn, session_id))
        if parsed.path == "/api/sessions":
            with open_db() as conn:
                rows = conn.execute(
                    """SELECT s.id, s.title, s.category, s.created_at, s.updated_at,
                              COUNT(m.id) AS message_count
                       FROM sessions s LEFT JOIN messages m ON m.session_id = s.id
                       GROUP BY s.id ORDER BY s.updated_at DESC LIMIT 100"""
                ).fetchall()
                return self.send_json({"sessions": [dict(row) for row in rows if row["message_count"] > 0]})
        if parsed.path == "/" or parsed.path == "/index.html":
            body = (ROOT / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/transcribe":
            return self.transcribe_audio()
        if path != "/api/chat":
            return self.send_json({"error": "Not found."}, 404)
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size > 20_000:
                return self.send_json({"error": "That message is a bit long. Try a shorter one."}, 413)
            payload = json.loads(self.rfile.read(size) or b"{}")
            content = str(payload.get("message", "")).strip()
            language = str(payload.get("language", "English"))[:30]
            selected_category = payload.get("category")
            if not isinstance(selected_category, str) or selected_category not in CATEGORIES:
                selected_category = ""
            if not content:
                return self.send_json({"error": "Write a thought or question first."}, 400)

            with open_db() as conn:
                session_id = ensure_session(conn, payload.get("session_id"))
                profile = read_profile(conn)
                conn.execute(
                    "INSERT INTO messages (session_id, role, content, created_at) VALUES (?, 'user', ?, ?)",
                    (session_id, content, now_iso()),
                )
                rows = conn.execute(
                    "SELECT role, content FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT 12",
                    (session_id,),
                ).fetchall()
                history = [dict(row) for row in reversed(rows)]
                data = generate_tutor_reply(history, profile, language, selected_category)
                reply = data["reply"].strip()
                practice_questions = data["practice_questions"]
                conn.execute(
                    "INSERT INTO messages (session_id, role, content, created_at, extras_json) VALUES (?, 'assistant', ?, ?, ?)",
                    (
                        session_id,
                        reply,
                        now_iso(),
                        json.dumps({key: data.get(key) for key in ("concept", "category", "visual", "examples", "future_uses", "mastery", "practice_questions")}, ensure_ascii=False),
                    ),
                )
                learner_update = data.get("learner_update")
                if not isinstance(learner_update, dict):
                    learner_update = {}
                profile = merge_profile(profile, learner_update, data)
                conn.execute("UPDATE learner_profile SET data = ? WHERE id = 1", (json.dumps(profile, ensure_ascii=False),))
                previous_practice = conn.execute("SELECT practice_json FROM sessions WHERE id = ?", (session_id,)).fetchone()["practice_json"]
                saved_practice = json.dumps(practice_questions, ensure_ascii=False) if practice_questions else previous_practice
                conn.execute(
                    """UPDATE sessions SET
                       title = COALESCE(title, ?), category = ?, updated_at = ?, practice_json = ?
                       WHERE id = ?""",
                    (content[:120], data["category"], now_iso(), saved_practice, session_id),
                )
                conn.commit()
                return self.send_json({**{key: data.get(key) for key in ("reply", "concept", "category", "visual", "examples", "future_uses", "mastery", "practice_questions")}, "profile": profile, "session_id": session_id})
        except RuntimeError as exc:
            return self.send_json({"error": str(exc)}, 503)
        except Exception:
            return self.send_json(
                {"error": "I couldn’t reach Sarvam just now. Check your connection and try again."}, 502
            )

    def transcribe_audio(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size <= 0 or size > 12_000_000:
                return self.send_json({"error": "That recording is too large. Try a shorter clip."}, 413)
            payload = json.loads(self.rfile.read(size) or b"{}")
            encoded_audio = payload.get("audio_base64", "")
            if not isinstance(encoded_audio, str) or not encoded_audio:
                return self.send_json({"error": "I didn’t receive a recording. Try speaking again."}, 400)
            audio_bytes = base64.b64decode(encoded_audio, validate=True)
            if not audio_bytes or len(audio_bytes) > 8_000_000:
                return self.send_json({"error": "That recording is too large. Try a shorter clip."}, 413)
            mime_type = str(payload.get("mime_type", "audio/webm")).split(";", 1)[0].lower()
            extension = {
                "audio/webm": ".webm",
                "audio/mp4": ".m4a",
                "audio/ogg": ".ogg",
                "audio/wav": ".wav",
            }.get(mime_type)
            if extension is None:
                return self.send_json({"error": "This recording format isn’t supported. Try another browser."}, 415)

            load_project_env()
            api_key = os.environ.get("SARVAM_API_KEY")
            if not api_key:
                return self.send_json({"error": "SARVAM_API_KEY is missing from the project .env file."}, 503)
            client = SarvamAI(api_subscription_key=api_key)
            with tempfile.NamedTemporaryFile(suffix=extension) as audio_file:
                audio_file.write(audio_bytes)
                audio_file.flush()
                audio_file.seek(0)
                result = client.speech_to_text.transcribe(
                    file=audio_file,
                    model="saaras:v4",
                    mode="transcribe",
                    language_code="unknown",
                )
            transcript = getattr(result, "transcript", "") or ""
            if not transcript.strip():
                return self.send_json({"error": "I couldn’t make out any words. Try speaking a little closer to the mic."}, 422)
            return self.send_json({"transcript": transcript, "language_code": getattr(result, "language_code", None)})
        except (binascii.Error, json.JSONDecodeError):
            return self.send_json({"error": "I couldn’t read that recording. Please try again."}, 400)
        except Exception:
            return self.send_json(
                {"error": "I couldn’t transcribe that recording. Check your connection and try again."}, 502
            )

    def log_message(self, _format, *_args):
        # Avoid logging request contents or environment values.
        return


if __name__ == "__main__":
    load_project_env()
    if not os.environ.get("SARVAM_API_KEY"):
        raise SystemExit("Add SARVAM_API_KEY to .env before starting Bhasha.")
    with open_db() as connection:
        ensure_session(connection)
    print(f"Bhasha is ready at http://127.0.0.1:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), AppHandler).serve_forever()
