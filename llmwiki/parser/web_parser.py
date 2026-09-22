"""
Lightweight web content parser and bounded same-site crawler.

The crawler follows only relevant links on the same host, clamps depth/page
limits, and gives wiki article links priority.
"""
import re
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

import httpx


class SimpleHTMLTextExtractor(HTMLParser):
    """Extract readable text while preserving headings and authored links."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text_parts: List[str] = []
        self.links: List[Dict[str, str]] = []
        self.title = ""
        self.in_title = False
        self.ignored_tags = {"script", "style", "nav", "footer", "header", "noscript", "svg", "canvas"}
        self.current_ignored = 0
        self.current_link_href: Optional[str] = None
        self.current_link_text: List[str] = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attrs_dict = dict(attrs)
        if tag in self.ignored_tags:
            self.current_ignored += 1
            return
        if self.current_ignored > 0:
            return
        if tag == "title":
            self.in_title = True
        elif tag in {"h1", "h2", "h3", "h4"}:
            self.text_parts.append("\n\n" + ("#" * int(tag[1])) + " ")
        elif tag == "li":
            self.text_parts.append("\n- ")
        elif tag == "br":
            self.text_parts.append("\n")
        elif tag == "a":
            href = (attrs_dict.get("href") or "").strip()
            if href:
                self.current_link_href = href
                self.current_link_text = []

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self.ignored_tags:
            if self.current_ignored > 0:
                self.current_ignored -= 1
            return
        if self.current_ignored > 0:
            return
        if tag == "title":
            self.in_title = False
        elif tag == "a" and self.current_link_href:
            anchor = re.sub(r"\s+", " ", " ".join(self.current_link_text)).strip()
            self.links.append({"href": self.current_link_href, "text": anchor})
            self.current_link_href = None
            self.current_link_text = []
        elif tag in {"p", "div", "section", "article", "h1", "h2", "h3", "h4", "li", "tr"}:
            self.text_parts.append("\n")

    def handle_data(self, data):
        if self.current_ignored > 0:
            return
        cleaned = re.sub(r"\s+", " ", data).strip()
        if not cleaned:
            return
        if self.in_title:
            self.title += (" " if self.title else "") + cleaned
            return
        self.text_parts.append(cleaned + " ")
        if self.current_link_href:
            self.current_link_text.append(cleaned)

    def get_text(self) -> str:
        raw_text = "".join(self.text_parts)
        raw_text = re.sub(r"[ \t]+\n", "\n", raw_text)
        raw_text = re.sub(r"\n[ \t]+", "\n", raw_text)
        raw_text = re.sub(r"\n{3,}", "\n\n", raw_text)
        return raw_text.strip()


class WebParser:
    USER_AGENT = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
    SKIP_EXTENSIONS = re.compile(
        r"\.(?:png|jpe?g|gif|webp|svg|ico|pdf|zip|7z|rar|mp[34]|wav|ogg|webm|woff2?|ttf|css|js)(?:$|\?)",
        re.IGNORECASE,
    )

    @staticmethod
    def canonical_url(url: str) -> str:
        """Normalize a URL enough for crawl deduplication."""
        parts = urlsplit(url.strip())
        scheme = parts.scheme.lower()
        host = (parts.hostname or "").lower()
        if not scheme or not host:
            return url.strip()
        port = parts.port
        netloc = host
        if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
            netloc = f"{host}:{port}"
        path = parts.path or "/"
        if path != "/":
            path = path.rstrip("/")
        query = "" if host == "namu.wiki" else parts.query
        return urlunsplit((scheme, netloc, path, query, ""))

    @classmethod
    def _normalize_links(cls, base_url: str, links: List[Dict[str, str]]) -> List[Dict[str, str]]:
        normalized: Dict[str, Dict[str, str]] = {}
        for item in links:
            href = (item.get("href") or "").strip()
            if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
                continue
            absolute = urljoin(base_url, href)
            parts = urlsplit(absolute)
            if parts.scheme not in {"http", "https"} or not parts.hostname:
                continue
            if cls.SKIP_EXTENSIONS.search(parts.path):
                continue
            canonical = cls.canonical_url(absolute)
            anchor = re.sub(r"\s+", " ", item.get("text") or "").strip()
            previous = normalized.get(canonical)
            if previous is None or len(anchor) > len(previous.get("text", "")):
                normalized[canonical] = {"url": canonical, "text": anchor}
        return list(normalized.values())

    @staticmethod
    def _clean_generic(text: str) -> str:
        lines: List[str] = []
        for raw in text.splitlines():
            line = re.sub(r"[ \t]+", " ", raw).strip()
            if not line:
                if lines and lines[-1] != "":
                    lines.append("")
                continue
            lines.append(line)
        return "\n".join(lines).strip()

    @classmethod
    def _clean_namuwiki(cls, title: str, text: str) -> Tuple[str, str]:
        clean_title = re.sub(r"\s*[-–—|]\s*나무위키\s*$", "", title, flags=re.IGNORECASE).strip()
        noise_exact = {
            "최근 변경", "최근 토론", "특수 기능", "나무뉴스", "역링크", "토론", "편집", "역사",
            "ACL", "로그인", "회원가입", "RandomPage", "목차", "분류", "새로고침",
        }
        noise_patterns = (
            re.compile(r"^이 문서는 .*에서 넘어왔습니다\.?$"),
            re.compile(r"^최근 수정 시각[:：]"),
            re.compile(r"^상위 문서[:：]"),
            re.compile(r"^분류[:：]\s*"),
            re.compile(r"^\[편집\]$"),
            re.compile(r"^\d+(?:\.\d+)*\.?\s*$"),
        )
        cleaned: List[str] = []
        for raw in text.splitlines():
            line = re.sub(r"\[편집\]", "", raw)
            line = re.sub(r"[ \t]+", " ", line).strip()
            if not line:
                if cleaned and cleaned[-1] != "":
                    cleaned.append("")
                continue
            plain = re.sub(r"^#{1,4}\s*", "", line).strip()
            if plain in noise_exact or any(pattern.search(plain) for pattern in noise_patterns):
                continue
            if re.fullmatch(r"(?:\[\d+\]\s*)+", plain):
                continue
            cleaned.append(line)
        return clean_title or title.strip(), cls._clean_generic("\n".join(cleaned))

    @classmethod
    def parse(cls, url: str, timeout: float = 15.0) -> Dict[str, Any]:
        headers = {"User-Agent": cls.USER_AGENT, "Accept-Language": "ko,en;q=0.8"}
        with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
            response = client.get(url)
            response.raise_for_status()
            html_content = response.text
            final_url = cls.canonical_url(str(response.url))

        extractor = SimpleHTMLTextExtractor()
        extractor.feed(html_content)

        title = extractor.title or final_url
        text = extractor.get_text()
        host = (urlsplit(final_url).hostname or "").lower()
        if host == "namu.wiki":
            title, text = cls._clean_namuwiki(title, text)
        else:
            text = cls._clean_generic(text)

        return {
            "title": title,
            "text": text,
            "source": final_url,
            "source_type": "web",
            "links": cls._normalize_links(final_url, extractor.links),
        }

    @classmethod
    def _is_crawlable(cls, root_url: str, candidate_url: str, same_site: bool = True) -> bool:
        root = urlsplit(root_url)
        candidate = urlsplit(candidate_url)
        if candidate.scheme not in {"http", "https"} or not candidate.hostname:
            return False
        if same_site and (candidate.hostname or "").lower() != (root.hostname or "").lower():
            return False
        if cls.SKIP_EXTENSIONS.search(candidate.path):
            return False
        if (root.hostname or "").lower() == "namu.wiki":
            decoded_path = unquote(candidate.path)
            if not decoded_path.startswith("/w/") or len(decoded_path) <= 3:
                return False
            article_name = decoded_path[3:]
            blocked_namespaces = ("분류:", "파일:", "틀:", "사용자:", "나무위키:")
            return not article_name.startswith(blocked_namespaces)
        return True

    @classmethod
    def _link_score(cls, root_url: str, parent_url: str, link: Dict[str, str]) -> float:
        target_url = link["url"]
        anchor = (link.get("text") or "").strip()
        root_path = unquote(urlsplit(root_url).path).strip("/")
        target_path = unquote(urlsplit(target_url).path).strip("/")
        parent_path = unquote(urlsplit(parent_url).path).strip("/")

        score = 0.0
        if target_path.startswith(root_path + "/"):
            score += 45.0
        elif root_path and root_path.lower() in target_path.lower():
            score += 70.0
        if parent_path and target_path.startswith(parent_path + "/"):
            score += 15.0

        topic = root_path.split("/", 1)[-1] if root_path.startswith("w/") else root_path
        topic_tokens = [t.lower() for t in re.split(r"[^0-9A-Za-z가-힣]+", topic) if len(t) >= 2]
        anchor_lower = anchor.lower()
        target_lower = target_path.lower()
        for token in topic_tokens[:6]:
            if token in anchor_lower:
                score += 18.0
            if token in target_lower:
                score += 12.0

        central_terms = ("세계관", "설정", "작품", "목록", "인물", "등장인물", "조직", "역사", "개요", "용어")
        peripheral_terms = ("만우절", "이벤트", "event", "fes", "anniversary", "기념")
        if any(term in anchor_lower or term in target_lower for term in central_terms):
            score += 24.0
        if any(term in anchor_lower or term in target_lower for term in peripheral_terms):
            score -= 120.0

        if anchor:
            score += min(len(anchor), 40) / 8.0
        if anchor in {"더 보기", "보기", "링크", "여기", "관련 문서"}:
            score -= 25.0
        if target_url == root_url:
            score -= 1000.0
        return score

    @classmethod
    def crawl(
        cls,
        start_url: str,
        depth: int = 1,
        max_pages: int = 8,
        same_site: bool = True,
        timeout: float = 15.0,
    ) -> List[Dict[str, Any]]:
        """Level-bounded crawl with relevance ordering and hard safety caps."""
        depth = max(0, min(int(depth), 3))
        max_pages = max(1, min(int(max_pages), 40))
        root_url = cls.canonical_url(start_url)
        root_page = cls.parse(root_url, timeout=timeout)
        root_url = root_page["source"]
        root_page["crawl_depth"] = 0
        root_page["parent_url"] = None
        root_page["anchor_text"] = ""
        pages: List[Dict[str, Any]] = [root_page]
        visited = {root_url}

        if depth == 0 or max_pages == 1:
            return pages

        remaining = max_pages - 1
        base_quota = remaining // depth
        extra = remaining % depth
        level_quotas = {
            level: base_quota + (1 if level <= extra else 0)
            for level in range(1, depth + 1)
        }

        def collect_candidates(parent: Dict[str, Any], seen_targets: set) -> List[Tuple[float, str, str, str]]:
            candidates = []
            for link in parent.get("links", []):
                target = link.get("url", "")
                if not target or target in visited or target in seen_targets:
                    continue
                if not cls._is_crawlable(root_url, target, same_site=same_site):
                    continue
                seen_targets.add(target)
                candidates.append((
                    cls._link_score(root_url, parent["source"], link),
                    target,
                    parent["source"],
                    link.get("text", ""),
                ))
            return candidates

        frontier = collect_candidates(root_page, set())

        for current_depth in range(1, depth + 1):
            quota = level_quotas.get(current_depth, 0)
            if quota <= 0 or not frontier or len(pages) >= max_pages:
                break

            frontier.sort(key=lambda item: item[0], reverse=True)
            next_by_url: Dict[str, Tuple[float, str, str, str]] = {}
            successful = 0

            for _, url, parent_url, anchor_text in frontier:
                if successful >= quota or len(pages) >= max_pages:
                    break
                if url in visited:
                    continue
                visited.add(url)

                try:
                    page = cls.parse(url, timeout=timeout)
                except Exception:
                    continue

                page["crawl_depth"] = current_depth
                page["parent_url"] = parent_url
                page["anchor_text"] = anchor_text
                pages.append(page)
                successful += 1

                if current_depth >= depth:
                    continue

                seen_targets = set(next_by_url)
                for candidate in collect_candidates(page, seen_targets):
                    score, target, candidate_parent, candidate_anchor = candidate
                    existing = next_by_url.get(target)
                    if existing is None or score > existing[0]:
                        next_by_url[target] = (score, target, candidate_parent, candidate_anchor)

            frontier = list(next_by_url.values())

        return pages
