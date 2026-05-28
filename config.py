import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key")
    SQLALCHEMY_DATABASE_URI = "sqlite:///theo.db"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
    }

    # Allow pasted/uploaded images (base64 data URLs) in form POSTs.
    # Werkzeug 3.x defaults to ~500 KB per-field, which a single pasted
    # screenshot easily exceeds. 32 MB total request body gives headroom
    # for several high-resolution images per add-item submission.
    MAX_CONTENT_LENGTH = 32 * 1024 * 1024  # 32 MB total request body
    MAX_FORM_MEMORY_SIZE = 32 * 1024 * 1024  # 32 MB for form fields

    # Anthropic / Claude API — powers the auto-generated J. Peterman-style
    # item stories. Optional: the app runs fine without it; the Stories
    # feature simply stays dormant until a key is present.
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
