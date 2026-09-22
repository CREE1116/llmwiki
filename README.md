# 🧠 LLMWiki: LLM 전용 3계층 초경량 자율 지식 창고

> **사람이 읽기 위한 문서가 아닌, AI 에이전트(LLM)가 최소한의 토큰으로 최대의 고밀도 지식을 즉각 인출·연결할 수 있도록 설계된 로컬 지식 창고입니다.**

---

## 🏛️ 3계층 아키텍처 (3-Tier Layered Architecture)

LLM의 컨텍스트 윈도우 낭비를 막고, 빠른 인출과 엄밀한 팩트 검증을 동시에 달성하기 위해 지식을 3개 층위로 분리하여 유기적으로 연동합니다.

```
[사용자 / AI 에이전트 질의]
            │
            ▼
┌────────────────────────────────────────────────────────┐
│  Layer 3: 벡터 & 시맨틱 인덱스 (Vector & Search Index)  │
│  - 로컬 임베딩(Ollama / NumPy 코사인 유사도) + SQLite FTS5│
│  - 0.001초 단위의 초고속 시맨틱/키워드 후보 인출     │
└────────────────────────┬───────────────────────────────┘
                         │
                         ▼
┌────────────────────────────────────────────────────────┐
│  Layer 2: 요약본/원자적 지식 (Distilled Concepts)       │
│  - Gemma 등 로컬 모델이 압축한 고밀도 마크다운 (.md)   │
│  - 핵심 원리, 수식, 트레이드오프, 상호 관계(Relations)  │
│  - LLM 추론 시 최소 토큰 소모로 최대 정보 전달         │
└────────────────────────┬───────────────────────────────┘
                         │ (필요시 상세 검증/증명 인출)
                         ▼
┌────────────────────────────────────────────────────────┐
│  Layer 1: 원문 보관소 (Raw Document Archive)           │
│  - 원본 논문 텍스트(PDF), 웹페이지 본문, 원본 코드     │
│  - 정확한 수치, 증명, 원문 인용 필요 시에만 드릴다운  │
└────────────────────────────────────────────────────────┘
```

---

## ✨ 주요 특징

1. **데몬/서버 제로: CLI + Skill 완벽 통합**:
   - 백그라운드 서버를 항상 띄워둘 필요 없이, 에이전트가 필요할 때만 고속 CLI 명령(`llmwiki search`, `llmwiki get`)을 실행합니다.
   - **Antigravity Skill**(`~/.gemini/config/skills/llmwiki/SKILL.md`)이 등록되어 있어, 대화 도중 자동으로 로컬 지식을 검색하고 인용합니다.
2. **완전한 로컬 구동 (Zero External Cloud / DB)**:
   - 별도 Docker나 무거운 DB 서버 없이, Python 표준 라이브러리와 내장 `sqlite3`(FTS5 + 그래프 테이블)로 단일 파일 데이터베이스를 운용합니다.
3. **원자적 지식 카드 (Atomic Knowledge Representation)**:
   - 원문을 통째로 모델 컨텍스트에 넣지 않고, 핵심 메커니즘, 장단점, 수식, 상호 링크(`[[WikiLinks]]`)로 고밀도 압축합니다.
4. **다양한 소스 지원**:
   - 논문 PDF, 일반 텍스트, 마크다운, 웹페이지 URL을 던져주면 자동으로 파싱 및 지식화합니다.
5. **표준 MCP (Model Context Protocol) 지원 (선택 사항)**:
   - Claude Desktop 등 MCP 규격이 필요한 클라이언트용 `llmwiki serve-mcp`도 함께 내장되어 있습니다.

---

## 🚀 빠른 시작 (Quick Start)

### 0. 앱 설치

가장 간단한 방법은 GitHub Releases에서 운영체제에 맞는 설치 파일을 받는 것입니다.

- **macOS**: DMG
- **Windows**: NSIS 설치 파일 또는 Portable EXE
- **Linux**: AppImage 또는 DEB

데스크톱 앱에는 독립 실행형 `llmwiki` CLI가 함께 포함되어 있어 Python을 별도로 설치할 필요가 없습니다. 첫 실행 시 CLI를 사용자 영역에 설치하고, 감지된 Codex/Claude Code MCP 및 Ollama 설정을 자동으로 연결합니다.

설치 후 터미널에서도 바로 사용할 수 있습니다:

```bash
llmwiki stats
llmwiki search "attention optimization"
```

### 1. 지식 인제스천 (문서 및 웹사이트 흡수)
```bash
# 파일(논문, 마크다운, 텍스트) 흡수
llmwiki ingest ./paper.pdf

# 웹 URL 흡수
llmwiki ingest https://arxiv.org/abs/2205.14135
```

### 2. 지식 검색 (Search)
```bash
# 하이브리드 검색 (기본값: 벡터 시맨틱 + FTS5 키워드)
llmwiki search "attention optimization"

# 시맨틱 벡터 전용 검색
llmwiki search "GPU memory IO bottleneck" --mode semantic

# FTS5 키워드 전용 검색
llmwiki search "FlashAttention" --mode keyword
```

### 3. 지식 상세 조회 (Get)
```bash
# Layer 2 원자적 지식 카드 출력
llmwiki get flash_attention

# 1-hop 연결된 개념 관계망 함께 보기
llmwiki get flash_attention --neighbors

# Layer 1 원본 원문 발췌문 함께 보기
llmwiki get flash_attention --raw
```

### 4. 지식 그래프 관계망 탐색 (Graph)
```bash
llmwiki graph flash_attention --hops 1
```

### 5. LLM 조회 감사 로그 확인 (Logs)
```bash
# 최근 AI 에이전트가 어떤 질문을 던지고 어떤 지식을 꺼내갔는지 확인
llmwiki logs --limit 20
```

### 6. 소스에서 데스크톱 앱 실행 (개발용)
릴리즈 설치 파일 대신 소스에서 실행하려면:
```bash
./bin/llmwiki-app
# 또는
cd app && npm start
```

### 7. 통계 확인 (Stats)
```bash
llmwiki stats
```

---

## 🖥️ Electron 데스크톱 앱 주요 화면

1. **🕸️ 지식 그래프 (Knowledge Graph)**:
   - 2D Canvas 기반 실시간 물리 시뮬레이션(Force-directed layout).
   - 노드 드래그, 줌, 패닝, 검색 필터링.
   - 노드 클릭 시 우측 드로어에서 핵심 요약, 메커니즘, 관계망, 원문 발췌문 확인.
2. **📚 지식 DB 탐색기 (Database Explorer)**:
   - **개념(Layer 2)**: 테이블 뷰, 태그 필터, 마크다운 뷰어.
   - **원문(Layer 1)**: 수집된 원본 논문/문서 목록 및 전체 텍스트 모달 뷰.
3. **📜 LLM 조회 감사 로그 (Query Logs)**:
   - AI 에이전트(Antigravity 등)가 Skill/CLI로 창고를 조회할 때마다 실시간 타임라인으로 기록.
   - 시각, 호출 주체(`SKILL`, `CLI`, `APP`), 질의어, 반환 개수 확인.
4. **📥 자료 흡수 (Ingest)**:
   - URL 입력 또는 파일 탐색기를 통해 PDF, 마크다운, 텍스트 파일을 GUI에서 직접 흡수.

---

## 🔌 MCP (Model Context Protocol) 서버 연동

LLMWiki를 에이전트(Antigravity, Claude Desktop, Cursor 등)에 연결하여 에이전트가 스스로 지식을 인출하고 보관하게 할 수 있습니다.

설치형 앱은 감지된 Codex/Claude Code에 MCP를 자동 등록합니다. 수동 연결이 필요한 클라이언트에서는 설치된 CLI를 직접 지정하면 됩니다.

### MCP 설정 예시 (`mcp.json`):
```json
{
  "mcpServers": {
    "llmwiki": {
      "command": "llmwiki",
      "args": ["serve-mcp"]
    }
  }
}
```

### 제공되는 MCP 도구 목록:
- `wiki_search`: 키워드 및 시맨틱 쿼리로 적합한 지식 카드 검색 (토큰 최소화 요약 반환)
- `wiki_fetch`: 특정 개념의 상세 카드, 지식 그래프 연결망, Layer 1 원문 발췌문 조회
- `wiki_read_raw`: 검증 및 엄밀한 수식 확인을 위해 Layer 1 원본 문서 전체 열람
- `wiki_traverse`: 특정 개념을 중심으로 연결된 지식 네트워크 탐색
- `wiki_list_concepts`: 창고에 보관된 전체 지식 개념 목록 확인
- `wiki_ingest`: 대화 도중 AI가 새로운 논문이나 웹 URL을 즉시 창고에 흡수
- `wiki_stats`: 지식 창고의 원문, 개념, 관계 엣지 통계 조회

---

## 📁 디렉토리 구조

```
llmwiki/
├── llmwiki/
│   ├── config.py             # 기본 경로 및 로컬 모델 설정
│   ├── cli.py                # 터미널용 CLI
│   ├── parser/               # PDF, Web, Text 통합 파서
│   ├── synthesizer/          # Gemma 기반 지식 증류 엔진
│   ├── storage/              # 3계층 스토리지 엔진
│   │   ├── raw_store.py      # Layer 1: 원문 보관소
│   │   ├── store.py          # Layer 2: 원자적 마크다운 지식
│   │   ├── db.py             # SQLite FTS5 + 그래프 엣지
│   │   ├── vector_index.py   # Layer 3: 벡터 임베딩 & 하이브리드 랭킹
│   │   └── graph.py          # NetworkX 지식 그래프 순회
│   └── mcp/                  # MCP stdio 서버
├── data/
│   ├── raw/                  # Layer 1 원문 파일 보관 디렉토리
│   ├── concepts/             # Layer 2 원자적 마크다운 (.md) 파일들
│   └── index.db              # SQLite DB (FTS5 + 그래프 + 벡터 BLOB)
├── pyproject.toml
└── README.md
```
