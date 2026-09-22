"""
Configuration manager for LLMWiki.
Supports persistent user settings, multi-provider LLMs, and zero-code onboarding.
"""
from pathlib import Path
import os
import json
import sys

# Base paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _default_data_dir() -> Path:
    """Keep source checkouts self-contained, but packaged builds user-writable."""
    configured = os.environ.get("LLMWIKI_DATA_DIR")
    if configured:
        return Path(configured).expanduser()
    if getattr(sys, "frozen", False):
        return Path.home() / ".llmwiki"
    return PROJECT_ROOT / "data"


DATA_DIR = _default_data_dir()
CONCEPTS_DIR = DATA_DIR / "concepts"
RAW_DIR = DATA_DIR / "raw"
DB_PATH = DATA_DIR / "index.db"
CONFIG_PATH = DATA_DIR / "config.json"

# Ensure directories exist
CONCEPTS_DIR.mkdir(parents=True, exist_ok=True)
RAW_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_CONFIG = {
    "initialized": False,
    "provider": "ollama",
    "model": "gemma2:9b",
    "ollama_host": "http://localhost:11434",
    "api_key": "",
    "installed_skills": {
        "antigravity": True,
        "claude": False,
        "codex": False
    }
}

VALID_PROVIDERS = {"ollama", "codex_cli", "claude_cli", "antigravity", "claude", "openai"}

def load_config() -> dict:
    """Load configuration from disk with defaults."""
    if not CONFIG_PATH.exists():
        return dict(DEFAULT_CONFIG)
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        cfg = dict(DEFAULT_CONFIG)
        cfg.update(data)
        return cfg
    except Exception:
        return dict(DEFAULT_CONFIG)

def save_config(updates: dict) -> dict:
    """Update and persist configuration to disk."""
    current = load_config()
    current.update(updates)
    CONFIG_PATH.write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")
    return current

# Active configuration values
_CFG = load_config()
OLLAMA_HOST = _CFG.get("ollama_host", "http://localhost:11434")
DEFAULT_MODEL = _CFG.get("model", "gemma2:9b")
FALLBACK_MODELS = ["gemma2:2b", "gemma:7b", "gemma:2b", "llama3.2:3b", "qwen2.5:7b"]
