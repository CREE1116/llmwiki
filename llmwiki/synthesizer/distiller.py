"""
Knowledge Distillation engine.
Transforms raw papers, documents, and web pages into high-density atomic concepts
and links them into the knowledge graph across the 3 layers:
- Layer 1: Archives full raw source text in RawStore.
- Layer 2: Synthesizes high-density markdown concept cards with Gemma.
- Layer 3: Synchronizes embeddings in VectorIndex.
"""
import json
import sys
import re
from typing import List, Dict, Any, Optional
from datetime import datetime
from .llm_client import LLMClient
from ..storage.models import Concept, Relation
from ..storage.store import Store

DISTILLER_SYSTEM_PROMPT = """You are an autonomous knowledge distillation engine for an AI-native knowledge warehouse.
Your goal is to extract maximum information density with zero human-fluff. Your output is queried directly by AI agents to perform high-precision reasoning with minimum token overhead.

Given a document, extract 1 to 3 atomic concepts.
For each concept, output a JSON object adhering to this schema:
{
  "id": "unique_snake_case_identifier",
  "name": "Concise Name",
  "aliases": ["alternative term 1", "abbreviation"],
  "type": "algorithm | architecture | theory | benchmark | tool | technique | character | setting | entity",
  "tags": ["domain1", "subdomain"],
  "summary": "Dense 1-2 sentence definition and core value proposition.",
  "mechanisms": [
    "Step 1 or primary mathematical/architectural mechanism",
    "Step 2 or optimization technique"
  ],
  "tradeoffs": {
    "pros": ["Key advantage 1", "Key advantage 2"],
    "cons": ["Limitation, compute bottleneck, or prerequisite"]
  },
  "formulas_or_code": [
    "Key equation, complexity bound O(...), or core snippet"
  ],
  "relations": [
    {
      "target": "id_of_related_concept",
      "type": "improves | requires | solves | variant_of | component_of | superseded_by | related_to",
      "reason": "Dense 1-sentence explanation of relationship"
    }
  ]
}

Return a JSON object: `{"concepts": [ {...}, {...} ]}`. Output valid JSON only."""

class Distiller:
    def __init__(self, store: Optional[Store] = None, llm: Optional[LLMClient] = None):
        self.store = store or Store()
        self.llm = llm or LLMClient()

    def _clean_json_output(self, raw_text: str) -> str:
        """Strip markdown codeblocks or trailing chatter."""
        text = raw_text.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()

        # Find first '[' and last ']'
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end != -1 and end > start:
            return text[start:end+1]

        # Or first '{' and last '}'
        start_obj = text.find("{")
        end_obj = text.rfind("}")
        if start_obj != -1 and end_obj != -1 and end_obj > start_obj:
            return f"[{text[start_obj:end_obj+1]}]"

        return text

    def _heuristic_distill_fallback(self, doc_title: str, text: str, source: str, raw_doc_id: str) -> List[Concept]:
        """
        Extract 2 to 4 atomic concepts (Architecture, Technique, Entity, Theory)
        from a single document even when local LLM is temporarily unreachable.
        Deconstructs titles/headings, cleans web/wiki noise, establishes structural relations,
        and deduplicates/merges against existing knowledge via vector similarity.
        """
        # Strip common web/wiki title suffixes
        clean_title = re.sub(
            r"\s*[-—|::]\s*(나무위키|위키백과|Wikipedia|velog|GitHub|tistory|블로그|Blog|뉴스|News).*$",
            "", doc_title, flags=re.IGNORECASE
        ).strip()
        if not clean_title:
            clean_title = doc_title.strip()

        # Filter noise lines (web navigation, wiki headers, table of contents)
        noise_prefixes = (
            "최근 변경", "최근 토론", "특수 기능", "최근 수정", "편집 토론",
            "분류 ", "목차", "table of contents", "navigation", "cookie",
            "all rights reserved", "copyright", "상위 문서:"
        )
        raw_lines = [l.strip() for l in re.split(r"[\r\n]+", text) if len(l.strip()) > 3]
        meaningful_lines = []
        is_past_header = False

        for l in raw_lines:
            l_clean = re.sub(r"^#+\s*", "", l).strip()
            if not l_clean:
                continue

            # Detect main content entrypoint (e.g. 1. 개요, 1. 소개, Abstract, Introduction)
            if re.match(r"^(?:1\s*[\.\:]\s*(?:개요|소개|설명|서론|overview|introduction)|abstract|overview)", l_clean, flags=re.IGNORECASE):
                is_past_header = True
                continue

            if not is_past_header:
                if any(p in l_clean for p in noise_prefixes):
                    continue
                if re.match(r"^\d+\s*$", l_clean):  # Bare numbers
                    continue
                if re.match(r"^\d+\s*\.\s*.+\d+\s*\.\s*.+", l_clean):  # Table of contents line e.g. "1 . 개요 2 . 명칭..."
                    continue
                if "[편집]" in l_clean:
                    continue

            if len(l_clean) < 8:
                continue

            meaningful_lines.append(l_clean)

        # If clean_title is a URL, DOI, or raw hash/filename, extract real title from first meaningful lines
        if re.match(r"^https?://|^doi:|^doc_|^[a-zA-Z0-9_\.\-]+\.(?:pdf|txt|md)$", clean_title, flags=re.IGNORECASE):
            for l in meaningful_lines[:6]:
                if len(l) >= 6 and not re.match(r"^https?://|^(?:©|승인|인용|doi:)", l, flags=re.IGNORECASE):
                    clean_title = l[:60]
                    break

        # Split title into atomic entity candidates
        # Note: Do NOT split on single hyphen inside words like TYPE-MOON or Claude-3! Only split on space-padded dashes or colons
        split_pattern = r"(?:\s+[-—]\s+)|\s*[:：|]\s*|(?:\s+via\s+)|\s+using\s+|\s+for\s+|\s+and\s+|(?:\s+및\s+)|(?:\s+을 위한\s+)|(?:\s+를 위한\s+)|(?:\s+을 통한\s+)|(?:\s+를 통한\s+)"
        raw_parts = [p.strip() for p in re.split(split_pattern, clean_title, flags=re.IGNORECASE) if len(p.strip()) >= 2]

        cleaned_parts = []
        for p in raw_parts:
            p_clean = re.sub(r"^(최신 업데이트|인용|http[s]?://\S+|doi:\S+)", "", p, flags=re.IGNORECASE).strip()
            # If part contains slash (e.g. TYPE-MOON/세계관), treat full phrase first, then subparts
            if "/" in p_clean:
                joined_phrase = p_clean.replace("/", " ").strip()
                if joined_phrase and joined_phrase not in cleaned_parts:
                    cleaned_parts.append(joined_phrase)
                sub_parts = [sp.strip() for sp in p_clean.split("/") if len(sp.strip()) >= 2]
                for sp in sub_parts:
                    if sp not in cleaned_parts and len(sp) >= 2:
                        cleaned_parts.append(sp)
            else:
                if len(p_clean) >= 2 and p_clean not in cleaned_parts:
                    cleaned_parts.append(p_clean)

        today = datetime.now().strftime("%Y-%m-%d")

        if not cleaned_parts:
            cleaned_parts = [clean_title[:40]]

        primary_name = cleaned_parts[0]
        cid_primary = re.sub(r"[^a-zA-Z0-9가-힣]+", "_", primary_name.lower()).strip("_")
        if not cid_primary:
            cid_primary = f"concept_{raw_doc_id[:8]}"

        summary_primary = meaningful_lines[0] if meaningful_lines else f"Core principles of {primary_name}"
        if len(meaningful_lines) > 1:
            summary_primary += f" {meaningful_lines[1]}"

        concepts_to_save = []
        relations_primary = []

        for i, sub_part in enumerate(cleaned_parts[1:4], 1):
            sub_cid = re.sub(r"[^a-zA-Z0-9가-힣]+", "_", sub_part.lower()).strip("_")
            if not sub_cid or sub_cid == cid_primary:
                continue

            rel_type = "solves" if i == len(cleaned_parts) - 1 and len(cleaned_parts) > 2 else "related_to"
            sub_type = "theory" if any(kw in sub_part.lower() for kw in ["공정성", "fairness", "problem", "bubble", "버블", "한계"]) else "concept"

            sub_summary = meaningful_lines[i] if i < len(meaningful_lines) else f"Key mechanism: {sub_part} in context of {primary_name}"
            sub_concept = Concept(
                id=sub_cid,
                name=sub_part,
                aliases=[],
                type=sub_type,
                tags=["atomic_extracted"],
                relations=[Relation(type="part_of", target=cid_primary, reason=f"{primary_name}의 핵심 구성요소 및 관련 개념")],
                summary=sub_summary[:300],
                mechanisms=[meaningful_lines[i + 1]] if (i + 1) < len(meaningful_lines) else [f"{sub_part}의 동작 원리 및 설정"],
                tradeoffs={"pros": [], "cons": []},
                formulas_or_code=[],
                sources=[f"raw:{raw_doc_id}", source],
                created_at=today,
                updated_at=today
            )
            concepts_to_save.append(sub_concept)
            relations_primary.append(Relation(type=rel_type, target=sub_cid, reason=f"{primary_name}에서 다루는 핵심 개념"))

        primary_concept = Concept(
            id=cid_primary,
            name=primary_name,
            aliases=[],
            type="architecture" if len(concepts_to_save) > 0 else "concept",
            tags=["atomic_extracted"],
            relations=relations_primary,
            summary=summary_primary[:300],
            mechanisms=meaningful_lines[2:5] if len(meaningful_lines) > 4 else ["See raw document for detailed mechanism."],
            tradeoffs={"pros": [], "cons": []},
            formulas_or_code=[],
            sources=[f"raw:{raw_doc_id}", source],
            created_at=today,
            updated_at=today
        )
        concepts_to_save.insert(0, primary_concept)

        batch_ids = {c.id for c in concepts_to_save}
        results = []
        for c in concepts_to_save:
            saved_c, action = self.store.save_concept_with_dedup(c, exclude_ids=batch_ids - {c.id})
            results.append(saved_c)

        return results

    def distill(self, doc_title: str, text: str, source: str) -> List[Concept]:
        """
        Extract atomic concepts from source text across all 3 layers:
        1. Archives raw content to Layer 1 (RawStore).
        2. Distills atomic concepts with local LLM to Layer 2.
        3. Syncs embeddings to Layer 3.
        """
        # Step 1: Save full raw document (Layer 1)
        raw_doc_id = self.store.raw.save(title=doc_title, source_uri=source, content=text)

        # Truncate text if excessively long for local context (keep first ~8500 chars)
        truncated_text = text[:8500]

        # Fetch existing concept IDs to guide relation linking
        existing_ids = self.store.db.list_all_ids()[:40]
        context_hint = ""
        if existing_ids:
            context_hint = f"\nExisting concepts in warehouse to link against if relevant: {', '.join(existing_ids)}"

        user_prompt = f"""Document Title: {doc_title}
Source URI: {source}
{context_hint}

Content:
\"\"\"
{truncated_text}
\"\"\"

Extract the atomic concept(s) as JSON according to system instructions."""

        # Step 2: Call local LLM
        try:
            response_text = self.llm.generate(
                prompt=user_prompt,
                system=DISTILLER_SYSTEM_PROMPT,
                json_format=True
            )
            cleaned_json = self._clean_json_output(response_text)
            data = json.loads(cleaned_json)

            # Unwrap { "concepts": [ ... ] } or { "items": [ ... ] }
            if isinstance(data, dict):
                if "concepts" in data and isinstance(data["concepts"], list):
                    data = data["concepts"]
                elif "items" in data and isinstance(data["items"], list):
                    data = data["items"]
                elif "data" in data and isinstance(data["data"], list):
                    data = data["data"]
                else:
                    data = [data]

            if not isinstance(data, list) or not data:
                raise ValueError("LLM returned empty or non-list data")

            # Validate that there is at least one meaningful item
            valid_items = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                name = item.get("name") or item.get("id") or ""
                summary = item.get("summary") or ""
                if (not name or name.strip().lower() in ("concept", "none", "null", "")) and not summary:
                    continue
                valid_items.append(item)

            if not valid_items:
                raise ValueError("LLM returned empty concepts without meaningful name or summary")

            data = valid_items
        except Exception as e:
            print(f"[LLMWiki] Notice: Local LLM distillation unavailable or returned empty ({e}). Using baseline extractor.", file=sys.stderr)
            return self._heuristic_distill_fallback(doc_title, text, source, raw_doc_id)

        today = datetime.now().strftime("%Y-%m-%d")
        created_concepts = []

        for item in data:
            cid = item.get("id", "").strip().lower().replace(" ", "_").replace("-", "_")
            if not cid or cid == "concept":
                cid = item.get("name", "concept").strip().lower().replace(" ", "_").replace("-", "_")
            if not cid or cid == "concept":
                cid = f"concept_{raw_doc_id[:8]}"

            # Parse relations
            relations = []
            for r in item.get("relations", []):
                if isinstance(r, dict) and r.get("target"):
                    target_id = r["target"].strip().lower().replace(" ", "_")
                    relations.append(Relation(
                        type=r.get("type", "related_to"),
                        target=target_id,
                        reason=r.get("reason", "")
                    ))

            concept = Concept(
                id=cid,
                name=item.get("name", cid.replace("_", " ").title()),
                aliases=item.get("aliases", []),
                type=item.get("type", "concept"),
                tags=item.get("tags", []),
                relations=relations,
                summary=item.get("summary", ""),
                mechanisms=item.get("mechanisms", []),
                tradeoffs=item.get("tradeoffs", {"pros": [], "cons": []}),
                formulas_or_code=item.get("formulas_or_code", []),
                sources=[f"raw:{raw_doc_id}", source],
                created_at=today,
                updated_at=today
            )

            # Persist to Layer 2 and Layer 3 with deduplication & semantic linking
            saved_concept, action = self.store.save_concept_with_dedup(concept)
            created_concepts.append(saved_concept)

        return created_concepts
