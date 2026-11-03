# Bhasha No barrier — Doubt Solver

A small local Socratic tutor. It asks learners to explain their thinking, adapts hints using a saved learner profile, and can add diagrams, examples, and practice questions.

## Run locally

1. Create and activate a virtual environment.
2. Install dependencies with `python -m pip install -r requirements.txt`.
3. Copy `.env.example` to `.env` and add your Sarvam API key there.
4. Run `python app.py` and open `http://127.0.0.1:8000`.

Chat and voice recordings are sent to Sarvam for model responses and transcription. Conversation history and the learner profile are stored in the local `bhasha.sqlite3` database, which is ignored by Git.

## Security

- Keep `.env` private. Never commit an API key.
- `.env`, the SQLite database, virtual environment, caches, and scratch folders are ignored.
- The app binds to `127.0.0.1` for local use.
