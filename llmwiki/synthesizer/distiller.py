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

    def _clean_web_noise(self, text: str) -> str:
        """Strip wiki navigation templates, footers, categories, and noise."""
        cleaned = re.sub(r"\[include\([^\)]+\)\]", "", text, flags=re.IGNORECASE)
        cleaned = re.sub(r"\{\{[^\}]+\}\}", "", cleaned)
        cleaned = re.sub(r"\[\[분류:[^\]]+\]\]", "", cleaned)
        cleaned = re.sub(r"\[\*[^\]]*\]", "", cleaned)
        cleaned = re.sub(r"\[\d+\]", "", cleaned)

        noise_lines = (
            "최근 변경", "최근 토론", "특수 기능", "최근 수정", "편집 토론",
            "목차", "table of contents", "navigation", "cookie",
            "all rights reserved", "copyright", "상위 문서:"
        )
        lines = []
        for line in cleaned.splitlines():
            l_strip = line.strip()
            if any(nl in l_strip for nl in noise_lines) and len(l_strip) < 30:
                continue
            lines.append(line)
        return "\n".join(lines)

    def _chunk_text(self, text: str, chunk_size: int = 7000) -> List[str]:
        """Split long document into semantic chunks using headers or paragraphs."""
        if len(text) <= chunk_size:
            return [text]

        chunks = []
        sections = re.split(r"(?=\n#{1,3}\s+)", text)
        current = ""
        for sec in sections:
            if len(current) + len(sec) < chunk_size:
                current += sec
            else:
                if current.strip():
                    chunks.append(current.strip())
                if len(sec) > chunk_size:
                    paras = sec.split("\n\n")
                    sub_cur = ""
                    for p in paras:
                        if len(sub_cur) + len(p) < chunk_size:
                            sub_cur += p + "\n\n"
                        else:
                            if sub_cur.strip():
                                chunks.append(sub_cur.strip())
                            sub_cur = p + "\n\n"
                    current = sub_cur
                else:
                    current = sec

        if current.strip():
            chunks.append(current.strip())

        return chunks[:6] if chunks else [text[:chunk_size]]

    def _extract_key_entities_from_text(self, text: str, limit: int = 8) -> List[Dict[str, Any]]:
        """
        Extract prominent named entities from body text using syntax patterns
        (bold text, quotes, headers, and repeated significant nouns).
        Guarantees that core characters, mechanisms, and lore terms (e.g. '알퀘이드')
        are reliably distilled into concepts even without LLM.
        """
        entities = {}

        # 1. Bold text patterns: '''Keyword''' or **Keyword**
        bolds = re.findall(r"(?:'''|\*\*)([가-힣a-zA-Z0-9_\- ]{2,30})(?:'''|\*\*)", text)
        for b in bolds:
            b_clean = b.strip()
            if len(b_clean) >= 2 and not b_clean.isdigit():
                entities[b_clean] = entities.get(b_clean, 0) + 4

        # 2. Korean quotation patterns: 「Keyword」, 『Keyword』, "Keyword"
        quotes = re.findall(r"[「『\"]([가-힣a-zA-Z0-9_\- ]{2,25})[」』\"]", text)
        for q in quotes:
            q_clean = q.strip()
            if len(q_clean) >= 2 and not q_clean.isdigit():
                entities[q_clean] = entities.get(q_clean, 0) + 3

        # 3. Section subheadings: e.g. ## 2.1. 알퀘이드 루트
        subheads = re.findall(r"\n#{1,4}\s*(?:\d+[\.\s]+)*([가-힣a-zA-Z0-9_\- ]{2,30})", text)
        for sh in subheads:
            sh_clean = re.sub(r"^(?:개요|소개|역사|목록|기타|상세|특징|설정)\s*", "", sh.strip()).strip()
            if len(sh_clean) >= 2 and not sh_clean.isdigit():
                entities[sh_clean] = entities.get(sh_clean, 0) + 5

        stop_words = {
            "문서", "편집", "토론", "역사", "분류", "상위", "하위", "기타", "내용", "참조",
            "자세한", "설명", "개요", "특징", "항목", "관련", "경우", "이후", "당시",
            "자신", "그녀", "그들", "때문", "정도", "사실", "생각", "존재", "사람",
            "세계관", "작품", "시리즈", "캐릭터", "주인공", "등장인물", "플레이어", "나무위키"
        }

        filtered = []
        for ent, score in sorted(entities.items(), key=lambda x: x[1], reverse=True):
            if ent in stop_words or any(sw == ent for sw in stop_words):
                continue
            if len(ent) < 2 or len(ent) > 25:
                continue

            # Find representative sentence
            pattern = re.compile(rf"([^.?!;\n]*?{re.escape(ent)}[^.?!;\n]*[.?!;\n])", re.DOTALL)
            match = pattern.search(text)
            context = match.group(1).replace("\n", " ").strip() if match else f"{ent}에 대한 핵심 설정 및 특성"
            filtered.append({
                "name": ent,
                "score": score,
                "context": context[:250]
            })
            if len(filtered) >= limit:
                break

        return filtered

    def _heuristic_distill_fallback(self, doc_title: str, text: str, source: str, raw_doc_id: str) -> List[Concept]:
        """
        Extract primary architecture concept AND deep body entities (e.g. characters, lore,
        mechanisms) from document text even when local LLM is temporarily unreachable.
        """
        clean_text = self._clean_web_noise(text)

        # Strip common web/wiki title suffixes
        clean_title = re.sub(
            r"\s*[-—|::]\s*(나무위키|위키백과|Wikipedia|velog|GitHub|tistory|블로그|Blog|뉴스|News).*$",
            "", doc_title, flags=re.IGNORECASE
        ).strip()
        if not clean_title:
            clean_title = doc_title.strip()

        raw_lines = [l.strip() for l in re.split(r"[\r\n]+", clean_text) if len(l.strip()) > 3]
        meaningful_lines = []
        for l in raw_lines:
            l_clean = re.sub(r"^#+\s*", "", l).strip()
            if len(l_clean) >= 8 and not re.match(r"^\d+\s*$", l_clean):
                meaningful_lines.append(l_clean)

        today = datetime.now().strftime("%Y-%m-%d")
        target_ws = self.store.workspace or "default"

        # 1. Primary Concept from Document Title
        cid_primary = re.sub(r"[^a-zA-Z0-9가-힣]+", "_", clean_title.lower()).strip("_")
        if not cid_primary:
            cid_primary = f"concept_{raw_doc_id[:8]}"

        summary_primary = meaningful_lines[0] if meaningful_lines else f"{clean_title}의 개요 및 핵심 원리"
        if len(meaningful_lines) > 1:
            summary_primary += f" {meaningful_lines[1]}"

        concepts_to_save = []
        relations_primary = []

        # 2. Extract Deep Body Entities (Characters, Settings, Sub-mechanisms)
        body_entities = self._extract_key_entities_from_text(clean_text, limit=6)
        for ent_info in body_entities:
            ent_name = ent_info["name"]
            ent_cid = re.sub(r"[^a-zA-Z0-9가-힣]+", "_", ent_name.lower()).strip("_")
            if not ent_cid or ent_cid == cid_primary:
                continue

            sub_concept = Concept(
                id=ent_cid,
                name=ent_name,
                aliases=[],
                type="concept",
                tags=["atomic_extracted", "entity"],
                relations=[Relation(type="part_of", target=cid_primary, reason=f"{clean_title} 문서 내 핵심 등장 요소 및 개념")],
                summary=ent_info["context"],
                mechanisms=[ent_info["context"]],
                tradeoffs={"pros": [], "cons": []},
                formulas_or_code=[],
                sources=[f"raw:{raw_doc_id}", source],
                workspace=target_ws,
                created_at=today,
                updated_at=today
            )
            concepts_to_save.append(sub_concept)
            relations_primary.append(Relation(type="related_to", target=ent_cid, reason=f"{clean_title}에 소속/등장하는 주요 개념"))

        # 3. Create Primary Document Concept
        primary_concept = Concept(
            id=cid_primary,
            name=clean_title,
            aliases=[],
            type="architecture" if len(concepts_to_save) > 0 else "concept",
            tags=["atomic_extracted", "core_doc"],
            relations=relations_primary,
            summary=summary_primary[:300],
            mechanisms=meaningful_lines[2:5] if len(meaningful_lines) > 4 else ["See raw document for detailed mechanism."],
            tradeoffs={"pros": [], "cons": []},
            formulas_or_code=[],
            sources=[f"raw:{raw_doc_id}", source],
            workspace=target_ws,
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
        target_ws = self.store.workspace or "default"
        raw_doc_id = self.store.raw.save(title=doc_title, source_uri=source, content=text, workspace=target_ws)

        # Clean noise and partition text into semantic chunks if document is long
        clean_full_text = self._clean_web_noise(text)
        chunks = self._chunk_text(clean_full_text, chunk_size=7500)

        # Fetch existing concept IDs to guide relation linking
        existing_ids = self.store.db.list_all_ids()[:40]
        context_hint = ""
        if existing_ids:
            context_hint = f"\nExisting concepts in warehouse to link against if relevant: {', '.join(existing_ids)}"

        all_distilled = []
        for chunk_idx, chunk_text in enumerate(chunks[:5]):
            section_label = f" (Part {chunk_idx + 1}/{len(chunks)})" if len(chunks) > 1 else ""
            user_prompt = f"""Document Title: {doc_title}{section_label}
Source URI: {source}
{context_hint}

Content:
\"\"\"
{chunk_text}
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

                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict):
                            name = item.get("name") or item.get("id") or ""
                            if name and name.strip().lower() not in ("concept", "none", "null", ""):
                                all_distilled.append(item)
            except Exception:
                pass

        # If LLM returned nothing across chunks or failed, use deep entity fallback
        if not all_distilled:
            print("[LLMWiki] Notice: Local LLM distillation unavailable or returned empty. Using baseline deep extractor.", file=sys.stderr)
            return self._heuristic_distill_fallback(doc_title, text, source, raw_doc_id)

        data = all_distilled

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
                workspace=self.store.workspace or "default",
                created_at=today,
                updated_at=today
            )

            # Persist to Layer 2 and Layer 3 with deduplication & semantic linking
            saved_concept, action = self.store.save_concept_with_dedup(concept)
            created_concepts.append(saved_concept)

        return created_concepts
