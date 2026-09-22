"""
Lightweight parser for text, markdown, code, and structured files.
"""
from pathlib import Path
from typing import Dict, Any

class TextParser:
    @staticmethod
    def parse(file_path: str) -> Dict[str, Any]:
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        content = path.read_text(encoding="utf-8", errors="replace")
        title = path.stem.replace("_", " ").title()

        # Check if first line is markdown header
        for line in content.splitlines()[:5]:
            stripped = line.strip()
            if stripped.startswith("# "):
                title = stripped.lstrip("# ").strip()
                break

        return {
            "title": title,
            "text": content,
            "source": str(path.resolve()),
            "source_type": "file"
        }
