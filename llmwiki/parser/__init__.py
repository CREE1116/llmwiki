"""
Unified parsing entrypoint for text, PDF, and web sources.
"""
from pathlib import Path
from typing import Dict, Any
from .text_parser import TextParser
from .web_parser import WebParser
from .pdf_parser import PDFParser

def parse_source(source: str) -> Dict[str, Any]:
    """Auto-detect source type and extract structured content."""
    clean_src = source.strip()
    if clean_src.startswith("http://") or clean_src.startswith("https://"):
        return WebParser.parse(clean_src)

    path = Path(clean_src)
    if not path.exists():
        # Treat as raw text if not a file
        return {
            "title": clean_src[:40].replace("\n", " "),
            "text": clean_src,
            "source": "raw_input",
            "source_type": "text"
        }

    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return PDFParser.parse(str(path))
    else:
        return TextParser.parse(str(path))
