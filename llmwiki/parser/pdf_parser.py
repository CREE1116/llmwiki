"""
PDF text parser.
Uses pypdf if available, with a lightweight pure-Python PDF stream fallback.
"""
from pathlib import Path
from typing import Dict, Any
import re
import zlib

class PDFParser:
    _TITLE_NOISE_RE = re.compile(
        r"(?:^---\s*page\s+\d+\s*---$|^doi\b|https?://|www\.|@|"
        r"^available online\b|^contents lists\b|^journal homepage\b|"
        r"^received\b|^accepted\b|^published\b|^copyright\b|^©|"
        r"^original paper\b|^systematic review\b|^type\s+mini\s+review\b|"
        r"^vol\.?\s*[:\d]|^ieee\s+(?:transactions|access)\b|"
        r"^proceedings?\b|^int\s+j\s+comput\s+vis\b|"
        r"\b(?:issn|isbn)\b)",
        re.IGNORECASE,
    )

    @staticmethod
    def _parse_with_pypdf(path: Path) -> str:
        import pypdf
        reader = pypdf.PdfReader(str(path))
        text_pages = []
        for i, page in enumerate(reader.pages):
            page_text = page.extract_text() or ""
            if page_text.strip():
                text_pages.append(f"--- Page {i+1} ---\n{page_text}")
        return "\n\n".join(text_pages)

    @staticmethod
    def _parse_with_fallback(path: Path) -> str:
        """Pure-Python stream decompression fallback for basic PDFs without dependencies."""
        data = path.read_bytes()
        extracted = []

        # Find all FlateDecode streams
        stream_matches = re.finditer(rb"stream[\r\n]+(.*?)[\r\n]+endstream", data, re.DOTALL)
        for m in stream_matches:
            stream_data = m.group(1)
            try:
                decompressed = zlib.decompress(stream_data)
            except Exception:
                decompressed = stream_data

            # Look for text within parentheses in BT ... ET blocks
            text_blocks = re.findall(rb"\((.*?)\)\s*Tj", decompressed)
            if not text_blocks:
                # Also check for hex strings or TJ array
                text_blocks = re.findall(rb"\[(.*?)\]\s*TJ", decompressed)

            for tb in text_blocks:
                try:
                    s = tb.decode("latin1", errors="ignore")
                    clean_s = re.sub(r"\\[0-9]{3}", "", s).replace(r"\(", "(").replace(r"\)", ")")
                    if len(clean_s.strip()) > 1:
                        extracted.append(clean_s.strip())
                except Exception:
                    continue

        return " ".join(extracted)

    @classmethod
    def _looks_like_title(cls, value: str) -> bool:
        text = re.sub(r"\s+", " ", value).strip()
        if len(text) < 8 or len(text) > 180 or cls._TITLE_NOISE_RE.search(text):
            return False
        if re.fullmatch(r"[\d\W_]+", text):
            return False
        if re.search(r"\b(?:19|20)\d{2}\b", text) and re.search(r"\d+\s*[:–-]\s*\d+", text):
            return False
        if re.search(r"\b(?:19|20)\d{2}\b", text) and re.search(r"\b\d{5,6}\b", text):
            return False
        words = re.findall(r"[A-Za-z가-힣0-9][A-Za-z가-힣0-9'’+.-]*", text)
        if len(words) < 2:
            return False
        singletons = sum(1 for word in words if len(word) == 1)
        if len(words) >= 5 and singletons / len(words) >= 0.45:
            return False
        letters = re.sub(r"[^A-Za-z]", "", text)
        if letters and letters.isupper() and len(words) <= 5:
            return False
        return True

    @classmethod
    def parse(cls, file_path: str) -> Dict[str, Any]:
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"PDF not found: {file_path}")

        text = ""
        try:
            text = cls._parse_with_pypdf(path)
        except ImportError:
            try:
                text = cls._parse_with_fallback(path)
            except Exception as e:
                text = f"[PDF Parse Error: {e}]"

        title = re.sub(r"[_-]+", " ", path.stem).strip() or path.stem
        for line in text.splitlines()[:45]:
            clean = re.sub(r"\s+", " ", line).strip()
            if cls._looks_like_title(clean):
                title = clean
                break

        return {
            "title": title,
            "text": text,
            "source": str(path.resolve()),
            "source_type": "pdf"
        }
