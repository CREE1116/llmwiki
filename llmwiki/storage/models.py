"""
Data models for LLMWiki knowledge representation.
Optimized for high information density and token efficiency.
"""
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional, Any
from datetime import datetime
import json

@dataclass
class Relation:
    """Directed knowledge relation between two concepts."""
    type: str               # e.g., 'improves', 'solves', 'requires', 'component_of', 'variant_of'
    target: str             # concept_id of target
    reason: str = ""        # Dense explanation of the relationship

@dataclass
class Concept:
    """Atomic knowledge unit for LLM consumption."""
    id: str                                  # Unique slug, e.g. "flash_attention_2"
    name: str                                # Human/Model readable title
    aliases: List[str] = field(default_factory=list)
    type: str = "concept"                    # algorithm, architecture, theory, benchmark, tool, metric
    tags: List[str] = field(default_factory=list)
    relations: List[Relation] = field(default_factory=list)
    summary: str = ""                        # 1-2 sentence dense essence
    mechanisms: List[str] = field(default_factory=list) # Core working principles / steps
    tradeoffs: Dict[str, List[str]] = field(default_factory=lambda: {"pros": [], "cons": []})
    formulas_or_code: List[str] = field(default_factory=list) # Key math or code
    sources: List[str] = field(default_factory=list) # URL, PDF file path or title
    created_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d"))
    updated_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d"))

    def to_markdown(self) -> str:
        """Serialize into high-density Markdown with YAML frontmatter."""
        import yaml
        frontmatter = {
            "id": self.id,
            "name": self.name,
            "aliases": self.aliases,
            "type": self.type,
            "tags": self.tags,
            "relations": [asdict(r) for r in self.relations],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "sources": self.sources
        }
        fm_str = yaml.dump(frontmatter, sort_keys=False, allow_unicode=True).strip()

        lines = [f"---\n{fm_str}\n---", f"\n# {self.name}\n"]
        if self.summary:
            lines.append(f"### Summary\n{self.summary.strip()}\n")

        if self.mechanisms:
            lines.append("### Key Mechanisms")
            for i, m in enumerate(self.mechanisms, 1):
                lines.append(f"{i}. {m.strip()}")
            lines.append("")

        if self.tradeoffs.get("pros") or self.tradeoffs.get("cons"):
            lines.append("### Trade-offs & Constraints")
            if self.tradeoffs.get("pros"):
                for p in self.tradeoffs["pros"]:
                    lines.append(f"- **Pros**: {p.strip()}")
            if self.tradeoffs.get("cons"):
                for c in self.tradeoffs["cons"]:
                    lines.append(f"- **Cons**: {c.strip()}")
            lines.append("")

        if self.formulas_or_code:
            lines.append("### Formulations / Key Code")
            for f in self.formulas_or_code:
                lines.append(f"{f.strip()}\n")

        if self.relations:
            lines.append("### Knowledge Links")
            for r in self.relations:
                lines.append(f"- `[[{r.target}]]` ({r.type}): {r.reason}")
            lines.append("")

        return "\n".join(lines)

    @classmethod
    def from_markdown(cls, text: str) -> "Concept":
        """Deserialize from Markdown with YAML frontmatter."""
        import yaml
        if not text.startswith("---"):
            raise ValueError("Markdown missing frontmatter")
        parts = text.split("---", 2)
        if len(parts) < 3:
            raise ValueError("Invalid frontmatter format")

        fm = yaml.safe_load(parts[1]) or {}
        body = parts[2].strip()

        rels = []
        for r in fm.get("relations", []):
            if isinstance(r, dict):
                rels.append(Relation(type=r.get("type", "related_to"), target=r.get("target", ""), reason=r.get("reason", "")))

        # Parse body sections
        summary = ""
        mechanisms = []
        tradeoffs = {"pros": [], "cons": []}
        formulas_or_code = []

        current_sec = None
        for line in body.splitlines():
            line_str = line.strip()
            if line_str.startswith("### Summary"):
                current_sec = "summary"
                continue
            elif line_str.startswith("### Key Mechanisms"):
                current_sec = "mechanisms"
                continue
            elif line_str.startswith("### Trade-offs & Constraints"):
                current_sec = "tradeoffs"
                continue
            elif line_str.startswith("### Formulations / Key Code"):
                current_sec = "formulas"
                continue
            elif line_str.startswith("### Knowledge Links"):
                current_sec = "links"
                continue
            elif line_str.startswith("### "):
                current_sec = "other"
                continue

            if not line_str:
                continue

            if current_sec == "summary":
                # Fallback extraction may preserve a source Markdown heading as
                # the first summary sentence. It is content here, not a section.
                line_str = line_str.lstrip("# ")
                summary += (" " + line_str if summary else line_str)
            elif line_str.startswith("#"):
                continue
            elif current_sec == "mechanisms":
                cleaned = line_str.lstrip("0123456789.-* ")
                if cleaned:
                    mechanisms.append(cleaned)
            elif current_sec == "tradeoffs":
                if "Pros" in line_str:
                    tradeoffs["pros"].append(line_str.split("Pros**:", 1)[-1].strip().lstrip("- "))
                elif "Cons" in line_str:
                    tradeoffs["cons"].append(line_str.split("Cons**:", 1)[-1].strip().lstrip("- "))
            elif current_sec == "formulas":
                formulas_or_code.append(line_str)

        return cls(
            id=fm.get("id", ""),
            name=fm.get("name", fm.get("id", "")),
            aliases=fm.get("aliases", []),
            type=fm.get("type", "concept"),
            tags=fm.get("tags", []),
            relations=rels,
            summary=summary,
            mechanisms=mechanisms,
            tradeoffs=tradeoffs,
            formulas_or_code=formulas_or_code,
            sources=fm.get("sources", []),
            created_at=fm.get("created_at", ""),
            updated_at=fm.get("updated_at", "")
        )


@dataclass
class SearchResult:
    """Search hit optimized for model prompt injection."""
    concept_id: str
    name: str
    type: str
    summary: str
    tags: List[str]
    score: float
    matched_by: str  # 'fts' or 'relation' or 'tag'
