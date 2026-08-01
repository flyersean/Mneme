"""
Mneme — Model-agnostic memory through text injection.

Architecture:
  Hermes/JAN → Flask proxy (:8080) → Ollama (:11434)
  
Storage: SQLite + FAISS (binary vectors, not JSON files)
Routing: Model-generated topic labels + FAISS similarity
Injection: Raw text chunks framed as memory, not instruction

Key patterns from raw-k-cache preserved:
  - Grade-aware recall ordering (A→F, same GRADE_PRIORITY)
  - Topic grouping with sibling loading
  - Two-pass dedup routing
  - CLASSIFY_THRESHOLD = 0.78 (same as KV version)
  - Staging buffer with auto-archive
  - Self-consistency grading (A/B/C/F)

Dependencies: ollama, requests, numpy, faiss-cpu
"""

import json, os, re, sqlite3, threading, time, uuid, struct
from datetime import datetime, timezone
from typing import List, Dict, Optional, Tuple

import numpy as np
import requests

# ─── Config ────────────────────────────────────────────────────
OLLAMA_URL  = "http://localhost:11434"
MODEL       = os.environ.get("MNEME_MODEL", "fredrezones55/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive:latest")
CHUNK_DIR   = os.environ.get("MNEME_CHUNK_DIR", "/workspace/mneme_chunks")
DB_PATH     = os.path.join(CHUNK_DIR, "mneme.db")

# Ollama config — let the model use its defaults
OLLAMA_TEMP    = 0.3

# ─── Multi-pass compression config ───
# CLASSIFY_MODEL removed — using embedding-based classification
MAX_HISTORY_MESSAGES = 32  # trim conversation to keep predict budget free
CHUNK_SIZE = 8000  # chars per chunk for large tool outputs
COMPRESS_THRESHOLD = 8000    # chars — tool results larger than this get compressed
COMPRESS_MODEL     = MODEL   # use same model for compression
COMPRESS_MAX_TOK   = 2048    # max tokens for compression response

# Staging: archive after N user turns or idle seconds
STAGING_TURNS  = 6
STAGING_IDLE   = 120

# Routing thresholds (same as KV version)
CLASSIFY_THRESHOLD = 0.78
ROUTE_THRESHOLD    = 0.08

os.makedirs(CHUNK_DIR, exist_ok=True)

# ─── Database ──────────────────────────────────────────────────

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA synchronous=NORMAL")

db.executescript("""
    CREATE TABLE IF NOT EXISTS chunks (
        chunk_id    TEXT PRIMARY KEY,
        topic_label TEXT NOT NULL,
        messages    TEXT NOT NULL,          -- JSON array of {role, content}
        thinking    TEXT DEFAULT '',
        strategy    TEXT DEFAULT '',
        vector      BLOB,                   -- 1024 × float32 = 4096 bytes
        grade       TEXT DEFAULT 'C',
        consensus   REAL DEFAULT 0.0,
        outcome     TEXT DEFAULT '',
        problem_type TEXT DEFAULT 'other',
        created_at  TEXT NOT NULL
    );
    
    CREATE TABLE IF NOT EXISTS strategies (
        strategy_id   TEXT PRIMARY KEY,
        problem_type  TEXT NOT NULL,
        strategy_text TEXT NOT NULL,
        source_chunk  TEXT,
        grade         TEXT DEFAULT 'B',
        created_at    TEXT NOT NULL
    );
    
    CREATE INDEX IF NOT EXISTS idx_chunks_topic ON chunks(topic_label);
    CREATE INDEX IF NOT EXISTS idx_chunks_type  ON chunks(problem_type);
    CREATE INDEX IF NOT EXISTS idx_strategies_type ON strategies(problem_type);

    CREATE TABLE IF NOT EXISTS tool_output_chunks (
        chunk_id    TEXT PRIMARY KEY,
        tool_name   TEXT NOT NULL,
        content     TEXT NOT NULL,          -- raw tool output, full fidelity
        size_chars  INTEGER NOT NULL,
        created_at  TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_tool_chunks_tool ON tool_output_chunks(tool_name);
""")
db.commit()

# ─── Grade Priority (same as raw-k-cache) ──────────────────────
GRADE_PRIORITY = {"A": 3, "B": 2, "C": 1, "F": 0}
DEFAULT_GRADE   = "C"

def grade_priority(chunk_id: str) -> int:
    row = db.execute("SELECT grade FROM chunks WHERE chunk_id=?", (chunk_id,)).fetchone()
    return GRADE_PRIORITY.get(row[0], GRADE_PRIORITY[DEFAULT_GRADE]) if row else 1

# ─── FAISS Index ───────────────────────────────────────────────

# snowflake-arctic-embed2 produces 1024-dim embeddings (nomic-embed-text was 768).
# NOTE: existing vectors in the DB are 768-dim and incompatible. Wipe
# mneme_chunks/mneme.db (or run a migration) before starting with
# the new embedder, otherwise FAISS will reject add/search on shape mismatch.
EMBED_MODEL = "snowflake-arctic-embed2"
DIM = 1024
try:
    import faiss
    _index = faiss.IndexFlatIP(DIM)          # inner product (cosine on norm'd vectors)
    _id_map: List[str] = []                  # index position → chunk_id
    FAISS_OK = True
except ImportError:
    _index = None; _id_map = []; FAISS_OK = False
    print("[mokv] FAISS not available — install faiss-cpu", flush=True)

_idx_lock = threading.Lock()

def _load_index():
    """Rebuild FAISS index from all chunk vectors in SQLite."""
    global _id_map
    rows = db.execute("SELECT chunk_id, vector FROM chunks WHERE vector IS NOT NULL").fetchall()
    with _idx_lock:
        _id_map.clear()
        if FAISS_OK:
            _index.reset()
        for cid, blob in rows:
            vec = _blob_to_vec(blob)
            if vec is not None:
                if FAISS_OK:
                    _index.add(vec.reshape(1, -1))
                _id_map.append(cid)
    print(f"[mokv] FAISS loaded {len(_id_map)} vectors", flush=True)

# ─── Vector Helpers ────────────────────────────────────────────

def _vec_to_blob(vec: np.ndarray) -> bytes:
    """1024 float32 → 4096 bytes."""
    return vec.astype(np.float32).tobytes()

def _blob_to_vec(blob: bytes) -> Optional[np.ndarray]:
    try:
        return np.frombuffer(blob, dtype=np.float32).copy()
    except:
        return None

# ─── Embedding: chunk + pool for long text ─────────────────────

# arctic-embed2 token limit is 8192; ~4000 chars is safely under even with
# dense prose. Overlap keeps sentence context across chunk boundaries.
CHUNK_CHARS    = 4000
CHUNK_OVERLAP  = 200

def chunk_text(text: str,
               chunk_size: int = CHUNK_CHARS,
               overlap: int = CHUNK_OVERLAP) -> List[str]:
    """Split text into overlapping windows.

    Returns [text] unchanged when it fits in a single window. Overlap is
    clamped to half the chunk size so stepping always moves forward.
    """
    if not text:
        return [""]
    if len(text) <= chunk_size:
        return [text]
    overlap = min(overlap, chunk_size // 2)
    step = chunk_size - overlap
    chunks = []
    start = 0
    n = len(text)
    while start < n:
        chunks.append(text[start:start + chunk_size])
        if start + chunk_size >= n:
            break
        start += step
    return chunks

def pool_embeddings(vectors: List[np.ndarray]) -> np.ndarray:
    """Mean-pool a list of embedding vectors into a single centroid.

    The result is L2-normalized so it stays compatible with the FAISS
    IndexFlatIP cosine-similarity convention used elsewhere.
    """
    if not vectors:
        return np.zeros(DIM, dtype=np.float32)
    stacked = np.stack(vectors)
    centroid = stacked.mean(axis=0).astype(np.float32)
    return centroid / (np.linalg.norm(centroid) + 1e-8)

def _embed_single(text: str) -> np.ndarray:
    """Raw Ollama /api/embeddings call for one chunk. Raises on failure."""
    r = requests.post(
        f"{OLLAMA_URL}/api/embeddings",
        json={"model": EMBED_MODEL, "prompt": text},
        timeout=60,
    )
    r.raise_for_status()
    v = np.array(r.json()["embedding"], dtype=np.float32)
    return v / (np.linalg.norm(v) + 1e-8)

def embed(text: str) -> np.ndarray:
    """Embed text via snowflake-arctic-embed2 with chunk+pool for long input.

    - Short text (<= CHUNK_CHARS): single embedding call.
    - Long text: split into overlapping windows, embed each, mean-pool to
      a single 1024-dim centroid.
    - On ANY failure (Ollama down, model missing, network error, bad JSON)
      log and return a zero vector so the proxy never 500s on archival.
      A zero vector simply won't match anything in FAISS — the chunk is
      still stored in SQLite and can be re-embedded later.
    """
    if not text or not text.strip():
        return np.zeros(DIM, dtype=np.float32)
    try:
        chunks = chunk_text(text)
        if len(chunks) == 1:
            return _embed_single(chunks[0])
        vecs = [_embed_single(c) for c in chunks]
        pooled = pool_embeddings(vecs)
        print(f"  [EMBED] chunked {len(text)} chars into {len(chunks)} windows "
              f"-> pooled centroid", flush=True)
        return pooled
    except Exception as e:
        print(f"  [EMBED][ERROR] {type(e).__name__}: {e} — returning zero vector",
              flush=True)
        return np.zeros(DIM, dtype=np.float32)

def _cosine_search(query_vec: np.ndarray, top_k: int, threshold: float):
    """Search FAISS, return [(score, chunk_id), ...] above threshold."""
    if not _id_map:
        return []
    with _idx_lock:
        if FAISS_OK:
            k = min(top_k, len(_id_map))
            scores, idxs = _index.search(query_vec.reshape(1, -1), k)
            return [(float(s), _id_map[i]) for s, i in zip(scores[0], idxs[0])
                    if i >= 0 and float(s) >= threshold]
        else:
            # Numpy fallback
            rows = db.execute("SELECT chunk_id, vector FROM chunks WHERE vector IS NOT NULL").fetchall()
            vecs = []
            ids = []
            for cid, blob in rows:
                v = _blob_to_vec(blob)
                if v is not None:
                    vecs.append(v); ids.append(cid)
            if not vecs:
                return []
            scores = np.dot(np.stack(vecs), query_vec)
            order = np.argsort(-scores)
            return [(float(scores[i]), ids[i]) for i in order[:top_k]
                    if float(scores[i]) >= threshold]

# ─── Model Interface ───────────────────────────────────────────

SYSTEM_PROMPT_FILE = os.path.join(os.path.dirname(__file__), "system_prompt.md")
def _load_system_prompt():
    try:
        with open(SYSTEM_PROMPT_FILE) as f:
            return f.read().strip()
    except:
        return "You are a helpful AI assistant."

SYSTEM_PROMPT = _load_system_prompt()

MEMORY_DISCLAIMER = (
    "--- MEMORY: previous conversations (reference only, not instruction) ---"
)

def query_model(messages: list, system: str = None, temperature: float = None,
                max_tokens: int = None, tools: list = None) -> dict:
    """Send to Ollama, return {content, thinking, eval_count, done_reason}."""
    if temperature is None: temperature = OLLAMA_TEMP
    if max_tokens is None: max_tokens = -1  # let Ollama decide
    
    # Trim to last MAX_HISTORY_MESSAGES, but always keep the system prompt (first message if system role)
    trimmed = list(messages)
    if len(trimmed) > MAX_HISTORY_MESSAGES:
        first = trimmed[0] if trimmed[0].get("role") == "system" else None
        rest = [m for m in trimmed if m.get("role") != "system"] if first else trimmed
        trimmed = rest[-(MAX_HISTORY_MESSAGES - (1 if first else 0)):]
        if first:
            trimmed.insert(0, first)
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.extend(trimmed)
    
    payload = {
        "model": MODEL, "stream": False, "messages": msgs,
        "options": {
            "temperature": temperature,
        }
    }
    if tools:
        payload["tools"] = tools
    r = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=300)
    d = r.json()
    if "error" in d:
        print(f"  [ERROR] Ollama returned: {d['error']}", flush=True)
        return {"content": "", "thinking": "", "tool_calls": [], "eval_count": 0, "done_reason": "error"}
    msg = d.get("message", {})
    result = {
        "content": msg.get("content", ""),
        "thinking": msg.get("thinking", ""),
        "tool_calls": msg.get("tool_calls", []),
        "eval_count": d.get("eval_count", 0),
        "done_reason": d.get("done_reason", "?"),
    }
    if not result["content"] and not result["tool_calls"]:
        print(f"  [WARN] Empty content from Ollama. done_reason={result['done_reason']} eval_count={result['eval_count']}", flush=True)
    return result


# ─── Native streaming query (SSE passthrough from Ollama) ─────
def query_model_stream(messages: list, tools: list = None):
    """Generator: yields Ollama SSE chunks as they arrive.
    Each chunk is a dict ready for json.dumps."""
    trimmed = list(messages)
    if len(trimmed) > MAX_HISTORY_MESSAGES:
        first = trimmed[0] if trimmed[0].get("role") == "system" else None
        rest = [m for m in trimmed if m.get("role") != "system"] if first else trimmed
        trimmed = rest[-(MAX_HISTORY_MESSAGES - (1 if first else 0)):]
        if first:
            trimmed.insert(0, first)
    msgs = []
    msgs.extend(trimmed)

    payload = {
        "model": MODEL, "stream": True, "messages": msgs,
        "options": {"temperature": OLLAMA_TEMP}
    }
    if tools:
        payload["tools"] = tools

    r = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=300, stream=True)
    cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"

    yield {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
           "model": FAKE_MODEL_ID,
           "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}

    for line in r.iter_lines():
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "error" in d:
            print(f"  [ERROR] Ollama streaming: {d['error']}", flush=True)
            yield {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                   "model": FAKE_MODEL_ID,
                   "choices": [{"index": 0, "delta": {}, "finish_reason": "error"}]}
            yield None  # DONE signal
            return

        if d.get("done"):
            break

        msg = d.get("message", {})
        content = msg.get("content", "")
        thinking = msg.get("thinking", "")

        if thinking:
            yield {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                   "model": FAKE_MODEL_ID,
                   "choices": [{"index": 0, "delta": {"reasoning": thinking, "role": "assistant"}, "finish_reason": None}]}
        if content:
            yield {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                   "model": FAKE_MODEL_ID,
                   "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]}

    yield {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
           "model": FAKE_MODEL_ID,
           "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
    yield None  # DONE signal




# ─── Chunk Storage ─────────────────────────────────────────────

def save_chunk(chunk_id: str, topic_label: str, messages: list,
               vector: np.ndarray, thinking: str = "", strategy: str = "",
               grade: str = "C", consensus: float = 0.0,
               outcome: str = "", problem_type: str = "other"):
    """Insert chunk into SQLite + FAISS."""
    blob = _vec_to_blob(vector)
    msgs_json = json.dumps(
        [{"role": m["role"], "content": m["content"][:8000]} for m in messages[-6:]]
    )
    
    db.execute("""
        INSERT OR REPLACE INTO chunks
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (chunk_id, topic_label, msgs_json, thinking[:8000], strategy,
          blob, grade, consensus, outcome, problem_type,
          datetime.now(timezone.utc).isoformat()))
    db.commit()
    
    # Add to FAISS
    with _idx_lock:
        if FAISS_OK:
            _index.add(vector.reshape(1, -1))
        _id_map.append(chunk_id)

def load_chunk(chunk_id: str) -> Optional[dict]:
    row = db.execute(
        "SELECT chunk_id, topic_label, messages, thinking, strategy, "
        "grade, consensus, outcome, problem_type FROM chunks WHERE chunk_id=?",
        (chunk_id,)
    ).fetchone()
    if not row:
        return None
    return {
        "chunk_id": row[0], "topic_label": row[1],
        "messages": json.loads(row[2]), "thinking": row[3],
        "strategy": row[4], "grade": row[5],
        "consensus": row[6], "outcome": row[7],
        "problem_type": row[8],
    }

# ─── Classification ────────────────────────────────────────────

CLASSIFY_PROMPT = (
    "Classify this conversation in exactly 3 lines.\n"
    "Line 1: LABEL: <2-4 word topic>\n"
    "Line 2: OUTCOME: SUCCESS/FAILURE/TRUNCATED/UNCERTAIN\n"
    "Line 3: TYPE: arithmetic/graph/scheduling/spatial/bayesian/logic/factual/other\n\n"
    "Conversation:\n"
)

# Topic clusters for embedding-based classification
TOPIC_CLUSTERS = {
    "technology": "programming code software development debugging api github git server deployment infrastructure python javascript shell terminal command",
    "memory_system": "memories memory retrieval injection context vector embedding FAISS database chunk storage proxy mneme session archive recall inject",
    "configuration": "config configuration setup settings profile install environment variable hermes",
    "web_content": "browser web page article wikipedia search reading extract navigate snapshot url fetch",
    "debugging": "error crash bug fix debug traceback 500 exception failure broken troubleshoot diagnosis",
    "data_analysis": "data statistics analysis numbers metrics counting spreadsheet table query",
    "conversation": "chat conversation discussion talking question answer explaining clarifying planning brainstorming",
    "olympics": "olympic games winter summer medal athlete sport competition milano cortina beijing",
    "politics_news": "government border incident crisis migration policy president minister country international",
    "sports": "sport football soccer team game match player champion league tournament score result",
    "science": "science research physics chemistry biology experiment astronomy space rocket launch nasa",
    "other": "uncategorized general topic miscellaneous"
}

# Pre-compute cluster embeddings at startup
TOPIC_VECTORS = {}
for label, desc in TOPIC_CLUSTERS.items():
    try:
        TOPIC_VECTORS[label] = embed(desc)
    except Exception:
        TOPIC_VECTORS[label] = None


def classify_chunk(messages: list) -> dict:
    """Classify using embedding similarity against topic clusters.
    No model call — pure vector math. Returns topic label + heuristics."""
    # Build text from messages for embedding
    text = " ".join(m["content"][:500] for m in messages if m["role"] in ("user", "assistant"))
    
    # Infer outcome heuristically from message content
    outcome = "SUCCESS"
    ptype = "other"
    
    full_text = " ".join(m["content"][:200] for m in messages)
    lower = full_text.lower()
    
    if any(w in lower for w in ("error", "failed", "crash", "500", "exception", "traceback")):
        outcome = "FAILURE"
        ptype = "error"
    elif any(w in lower for w in ("continue", "next chunk", "more chunks")):
        outcome = "TRUNCATED"
    elif any(w in lower for w in ("save", "archive", "memory", "store")):
        ptype = "memory_operation"
    elif any(w in lower for w in ("browser", "://", "page", "article", "wikipedia", "extract", "content")):
        ptype = "web_retrieval"
    elif any(w in lower for w in ("code", "function", "def ", "patch", "fix")):
        ptype = "code"
    
    # Embedding-based topic: find nearest cluster
    try:
        vec = embed(text)
        if vec is not None and vec.shape[0] > 0:
            best_label = "other"
            best_score = -2.0
            for label, cvec in TOPIC_VECTORS.items():
                if cvec is not None:
                    score = np.dot(vec, cvec) / (np.linalg.norm(vec) * np.linalg.norm(cvec) + 1e-8)
                    if score > best_score:
                        best_score = score
                        best_label = label
            
            # Generate readable topic label from best cluster + first words
            first_words = " ".join(text.split()[:6])[:60]
            topic_label = f"{best_label}: {first_words}" if best_label != "other" else first_words
        else:
            topic_label = "uncategorized"
    except Exception:
        topic_label = "uncategorized"
    
    return {
        "topic_label": topic_label[:80],
        "outcome": outcome,
        "problem_type": ptype,
    }

# ─── Strategy Generation ───────────────────────────────────────

def generate_strategy(messages: list, outcome: str) -> str:
    """Generate strategy heuristically — no model call needed for simple cases."""
    if outcome not in ("FAILURE", "TRUNCATED"):
        return ""
    text = " ".join(m["content"][:300] for m in messages[-3:] if m["role"] in ("user", "assistant"))
    # Return structured strategy note for the model to learn from
    if outcome == "FAILURE":
        return f"FAILURE on: {text[:200]}. Retry with different approach."
    return f"TRUNCATED on: {text[:200]}. Content too large — use chunked reading."

# ─── Routing ───────────────────────────────────────────────────

def route_query(query: str, top_k: int = 3, with_scores: bool = False) -> List:
    """Two-pass dedup: best per topic, then fill remaining."""
    q_vec = embed(query)
    scored = _cosine_search(q_vec, top_k * 3, ROUTE_THRESHOLD)
    if not scored:
        return []
    
    # Grade-aware sort
    scored.sort(key=lambda x: (-grade_priority(x[1]), -x[0]))
    
    # Pass 1: best chunk per topic
    results = []
    seen = set()
    for score, cid in scored:
        # Topic extracted by stripping version suffix
        topic = re.sub(r'_v\d+$', '', cid)
        if topic not in seen:
            results.append(cid)
            seen.add(topic)
            if len(results) >= top_k:
                return results
    
    # Pass 2: fill remaining
    for score, cid in scored:
        if cid not in results:
            results.append(cid)
            if len(results) >= top_k:
                break
    return results

def get_siblings(chunk_id: str) -> List[str]:
    row = db.execute("SELECT topic_label FROM chunks WHERE chunk_id=?", (chunk_id,)).fetchone()
    if not row:
        return [chunk_id]
    rows = db.execute("SELECT chunk_id FROM chunks WHERE topic_label=?", (row[0],)).fetchall()
    return [r[0] for r in rows]

def get_strategies(problem_type: str, limit: int = 3) -> List[str]:
    rows = db.execute(
        "SELECT strategy_text FROM strategies WHERE problem_type=? "
        "ORDER BY CASE grade WHEN 'A' THEN 0 WHEN 'B' THEN 1 ELSE 2 END LIMIT ?",
        (problem_type, limit)
    ).fetchall()
    return [r[0] for r in rows]

# ─── Context Injection ─────────────────────────────────────────

# Token budget for injected memory. Hard cap to prevent context overflow.
# The model has 32768 ctx total. System prompt + live conversation need room.
MAX_INJECTED_TOKENS = 4096   # ~4K tokens for memory + strategies
MAX_SIBLINGS        = 5      # max chunks per topic
MAX_CHUNK_WORDS     = 500    # split user messages longer than this

def _estimate_tokens(text: str) -> int:
    """Rough token count: ~1.3 tokens per word for English text."""
    return max(1, int(len(text.split()) * 1.3))

def _trim_chunks(ordered_ids: List[str], max_tokens: int) -> List[str]:
    """Grade-aware trim: keep highest-grade chunks that fit in token budget.
    
    Sorted A→F (A first = highest priority). Accumulate until budget exhausted.
    Chunks that don't fit are dropped (F-grade chunks dropped first).
    """
    selected = []
    used = 0
    for cid in ordered_ids:
        chunk = load_chunk(cid)
        if not chunk:
            continue
        text = "\n".join(
            f"{m['role']}: {m['content'][:300]}"
            for m in chunk.get("messages", [])
        )
        if chunk.get("strategy"):
            text += f"\n[strategy: {chunk['strategy']}]"
        
        cost = _estimate_tokens(text)
        if used + cost > max_tokens:
            continue  # skip this chunk, try next (lower grade)
        selected.append(cid)
        used += cost
    
    return selected

def _extract_text(content) -> str:
    """Extract text from message content (str or list of blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "image_url":
                    parts.append("[IMAGE: " + block.get("image_url", {}).get("url", "unknown") + "]")
        return "\n".join(parts)
    return str(content)


def build_context(query: str) -> Tuple[str, str]:
    if not query or not query.strip():
        return "", "other"  # empty query — skip injection
    """Build injected memory context with hard token cap.
    
    1. Route query → top-3 matching chunk IDs
    2. Expand to siblings (capped at MAX_SIBLINGS per topic)
    3. Grade-aware trim to fit MAX_INJECTED_TOKENS
    4. Append strategies for the detected problem type
    """
    chunk_ids = route_query(query, top_k=3)
    
    # Expand to siblings with cap
    all_ids = set()
    for cid in chunk_ids:
        all_ids.add(cid)
        siblings = get_siblings(cid)
        for sib in siblings[:MAX_SIBLINGS]:
            all_ids.add(sib)
    
    # Grade-aware ordering (A first, F last)
    ordered = sorted(all_ids, key=lambda c: (-grade_priority(c), c))
    
    # Trim to token budget — preserves high-grade, drops low-grade
    trimmed = _trim_chunks(ordered, MAX_INJECTED_TOKENS)
    
    # Build raw chunk text
    parts = []
    ptype = "other"
    for cid in trimmed:
        chunk = load_chunk(cid)
        if not chunk:
            continue
        ptype = chunk.get("problem_type", "other")
        topic = chunk.get("topic_label", "unknown")
        msg_text = f"--- {topic} (id:{cid}) ---\n"
        msg_text += "\n".join(
            f"{m['role']}: {m['content'][:300]}" 
            for m in chunk.get("messages", [])
        )
        if chunk.get("strategy"):
            msg_text += f"\n[learned strategy: {chunk['strategy']}]"
        parts.append(msg_text)
    
    if not parts:
        return "", ptype
    
    context = MEMORY_DISCLAIMER + "\n" + "\n---\n".join(parts)
    
    # Scan for structured chunk references in all archived conversations and surface them
    struct_refs = set()
    for cid in trimmed:
        chunk = load_chunk(cid)
        if chunk:
            for m in chunk.get("messages", []):
                text = _extract_text(m.get("content", ""))
                import re as _sre
                found = _sre.findall(r'\[chunk-[a-f0-9]+:\s*\d+[^\]]*\]', text)
                struct_refs.update(found)
    if struct_refs:
        context += "\n\n--- STORED RAW DATA (retrievable with <<DETAIL>>) ---\n"
        context += "\n".join(f"  {r}" for r in struct_refs)
    used_tokens = _estimate_tokens(context)
    
    # Add strategies for this problem type (separate from chunk budget)
    strategies = get_strategies(ptype)
    strat_text = ""
    if strategies:
        strat_text = "\n\n--- PROVEN STRATEGIES ---\n" + "\n".join(f"• {s}" for s in strategies)
        # Only append if strategies fit in remaining budget
        if _estimate_tokens(context + strat_text) <= MAX_INJECTED_TOKENS:
            context += strat_text
    
    print(f"  [INJECT] {len(trimmed)}/{len(ordered)} chunks, "
          f"~{used_tokens} tokens (cap: {MAX_INJECTED_TOKENS})", flush=True)

    # Log the full injected context for debugging recall failures
    try:
        with open("/tmp/injection_log.txt", "a", encoding="utf-8") as f:
            f.write(f"\n=== {datetime.now(timezone.utc).isoformat()} ===\n")
            f.write(f"QUERY: {query}\n")
            f.write(f"CHUNKS: {len(trimmed)}/{len(ordered)}  TOKENS: ~{used_tokens}\n")
            f.write(context + "\n")
    except Exception as e:
        print(f"  [INJECT][LOG-ERROR] {e}", flush=True)

    return context, ptype

# ─── Staging Buffer ────────────────────────────────────────────

class StagingBuffer:
    def __init__(self):
        self.messages: list = []
        self.last_activity = time.time()
        self.lock = threading.Lock()
    
    def add(self, role: str, content: str):
        with self.lock:
            self.messages.append({"role": role, "content": content})
            self.last_activity = time.time()
    
    def should_flush(self) -> bool:
        with self.lock:
            turns = sum(1 for m in self.messages if m["role"] == "user")
            return turns >= STAGING_TURNS or (
                self.messages and time.time() - self.last_activity > STAGING_IDLE
            )
    
    def flush(self) -> list:
        with self.lock:
            msgs = list(self.messages)
            self.messages.clear()
            self.last_activity = time.time()
            return msgs

staging = StagingBuffer()

def archive_staging():
    """Flush the staging buffer into topic-split archived chunks.

    Each topic group gets its own chunk. Within a topic, chunks are capped
    at MAX_CHUNK_SIZE chars. Overflow gets versioned sibling chunks.

    Returns the number of chunks archived.
    """
    msgs = staging.flush()
    if not msgs:
        return 0

    # Classify each message into a topic group
    groups = _topic_split(msgs)
    
    total = 0
    for topic_label, group_msgs in groups:
        n = _archive_group(topic_label, group_msgs)
        total += n
    
    print(f"  [ARCHIVE] {len(groups)} topics, {total} chunks total", flush=True)
    return total


def _classify_message(msg: dict) -> str:
    """Classify a single message using embedding similarity to topic clusters."""
    text = msg.get("content", "")
    if not text or len(text) < 10:
        return "other"
    
    try:
        vec = embed(text[:2000])  # use first 2K chars for embedding
        if vec is None or vec.shape[0] == 0:
            return "other"
        
        best_label = "other"
        best_score = -2.0
        for label, cvec in TOPIC_VECTORS.items():
            if cvec is not None:
                score = np.dot(vec, cvec) / (np.linalg.norm(vec) * np.linalg.norm(cvec) + 1e-8)
                if score > best_score:
                    best_score = score
                    best_label = label
        
        return best_label
    except Exception:
        return "other"


def _topic_split(msgs: list) -> list:
    """Split messages into topic groups. Returns [(topic_label, [msgs]), ...]."""
    from itertools import groupby
    
    # Assign topic to each message
    labeled = []
    for m in msgs:
        role = m.get("role", "")
        if role in ("user", "assistant"):
            topic = _classify_message(m)
        else:
            topic = "system"
        labeled.append((topic, m))
    
    # Group consecutive messages with same topic
    groups = []
    for topic, group in groupby(labeled, key=lambda x: x[0]):
        msgs_in_group = [g[1] for g in group]
        groups.append((topic, msgs_in_group))
    
    # Merge small groups (< 3 messages) into neighbors if they share a broad category
    merged = _merge_small_groups(groups)
    
    return merged


def _merge_small_groups(groups: list) -> list:
    """Merge tiny groups (1-2 msgs) into adjacent groups."""
    if len(groups) <= 1:
        return groups
    
    result = []
    i = 0
    while i < len(groups):
        topic, msgs = groups[i]
        if len(msgs) <= 2 and i + 1 < len(groups):
            # Merge with next group
            next_topic, next_msgs = groups[i + 1]
            merged_topic = f"{topic}+{next_topic}"[:40]
            merged_msgs = msgs + next_msgs
            result.append((merged_topic, merged_msgs))
            i += 2
        else:
            result.append((topic, msgs))
            i += 1
    return result


MAX_CHUNK_SIZE = 10000  # chars per chunk for embedding


def _clean_content(text: str) -> str:
    """Strip browser wrapper boilerplate to get real content for embedding."""
    # browser_console/navigate output: remove ~600 chars of wrapper boilerplate
    lower = text[:300].lower()
    if "browser_console" in lower or "browser_navigate" in lower or "untrusted_tool_result" in lower:
        return text[600:] if len(text) > 600 else text
    return text



def _generate_topic_label(text):
    clean = _clean_content(text)[:2000].lower()
    keywords = []
    import re as _tlre
    dates = _tlre.findall(r"(20\d{2})", clean)
    if dates:
        keywords.append(dates[0])
    locations = {"spain":"Spain","morocco":"Morocco","ceuta":"Ceuta","melilla":"Melilla","france":"France","italy":"Italy","japan":"Japan","china":"China","russia":"Russia","canada":"Canada","mexico":"Mexico","brazil":"Brazil","kumamoto":"Kumamoto"}
    for k,v in locations.items():
        if k in clean:
            keywords.append(v)
    events = {"earthquake":"earthquake","border":"border","migrant":"migration","olympic":"Olympics","election":"election","hurricane":"hurricane","storm":"storm"}
    for k,v in events.items():
        if k in clean:
            keywords.append(v)
    if keywords:
        return " ".join(keywords[:4])[:60]
    words = [w for w in clean.split()[:8] if len(w) > 3]
    return " ".join(words)[:60] or "untitled"


def _archive_group(topic_label: str, msgs: list) -> int:
    """Archive a topic group, splitting if too large. Returns chunk count."""
    SEMANTIC_ROLES = ("user", "assistant")
    
    # Build embedding text — strip browser wrapper noise for clean vectors
    user_text = " ".join(
        _clean_content(m["content"])[:5000] for m in msgs if m["role"] in SEMANTIC_ROLES
    )
    
    # If group is small enough, archive as single chunk
    if len(user_text) <= MAX_CHUNK_SIZE:
        descriptive = _generate_topic_label(user_text) if not topic_label or topic_label.startswith("web_content") or topic_label.startswith("other") else topic_label
        return _archive_single_chunk(msgs, user_text, descriptive)
    
    # Split into sibling chunks by MAX_CHUNK_SIZE
    total = 0
    offset = 0
    seq_base = db.execute(
        "SELECT COUNT(*) FROM chunks WHERE topic_label LIKE ?", 
        (f"{topic_label[:20]}%",)
    ).fetchone()[0] + 1
    
    # Split by message boundary, not raw char offset
    current = []
    current_text = ""
    
    for m in msgs:
        if m["role"] not in SEMANTIC_ROLES:
            current.append(m)
            continue
        
        frag = m["content"][:5000]
        if current_text and len(current_text) + len(frag) > MAX_CHUNK_SIZE:
            # Archive current batch
            descriptive = _generate_topic_label(current_text) if topic_label.startswith("web_content") or topic_label.startswith("other") else topic_label[:20]
            label = f"{descriptive[:30]}_p{seq_base}"
            _archive_single_chunk(current, current_text, label)
            total += 1
            seq_base += 1
            current = []
            current_text = ""
        
        current.append(m)
        current_text += " " + frag
    
    # Archive remaining
    if current:
        descriptive = _generate_topic_label(current_text) if topic_label.startswith("web_content") or topic_label.startswith("other") else topic_label[:20]
        label = f"{descriptive[:30]}_p{seq_base}" if total > 0 else (_generate_topic_label(user_text) if topic_label.startswith("web_content") or topic_label.startswith("other") else topic_label)
        _archive_single_chunk(current, current_text, label)
        total += 1
    
    return total


def _archive_single_chunk(msgs: list, user_text: str, topic_label: str) -> int:
    """Archive one chunk. Returns 1 on success."""
    # Determine outcome and problem type heuristically
    full_text = " ".join(m["content"][:200] for m in msgs if m["role"] in ("user", "assistant"))
    lower = full_text.lower()
    
    outcome = "SUCCESS"
    ptype = "other"
    
    if any(w in lower for w in ("error", "failed", "crash", "500", "exception", "traceback")):
        outcome = "FAILURE"
        ptype = "error"
    elif any(w in lower for w in ("continue", "next chunk", "more chunks")):
        outcome = "TRUNCATED"
    elif any(w in lower for w in ("save", "archive", "memory", "store")):
        ptype = "memory_operation"
    elif any(w in lower for w in ("browser", "http", "page", "article", "wikipedia", "extract")):
        ptype = "web_retrieval"
    elif any(w in lower for w in ("code", "function", "def ", "patch", "fix", "debug")):
        ptype = "code"
    
    strategy = generate_strategy(msgs, outcome)
    vec = embed(user_text)
    
    row = db.execute("SELECT COUNT(*) FROM chunks WHERE topic_label=?", (topic_label,)).fetchone()
    seq = (row[0] if row else 0) + 1
    chunk_id = f"{topic_label[:40]}_v{seq}"
    
    save_chunk(chunk_id, topic_label, msgs, vec, strategy=strategy,
               outcome=outcome, problem_type=ptype)
    
    if strategy and ptype != "other":
        sid = f"strat_{ptype}_{seq}_{int(time.time())}"
        db.execute(
            "INSERT OR REPLACE INTO strategies VALUES (?,?,?,?,?,?)",
            (sid, ptype, strategy, chunk_id, "B",
             datetime.now(timezone.utc).isoformat())
        )
        db.commit()
    
    print(f"  [ARCHIVE] {chunk_id} topic={topic_label[:30]} outcome={outcome} type={ptype} ({len(user_text)} chars)", flush=True)
    return 1


    """Split a message list into segments, each starting at a user message.

    Each segment is a list of messages: one user message plus all following
    assistant/tool messages up to (but not including) the next user message.
    Leading non-user messages are attached to the first segment.
    """
    segments = []
    current = []
    for m in msgs:
        if m["role"] == "user" and current:
            segments.append(current)
            current = [m]
        else:
            current.append(m)
    if current:
        segments.append(current)
    return segments


def _archive_split(msgs):
    """Split a long buffer into per-user-message segments and archive each
    segment as its own chunk with its own classification.

    This fixes topic mixing: previously the whole buffer got one label from
    the dominant topic; now each user-turn cluster gets its own label.
    Returns the number of chunks archived.
    """
    segments = _segment_by_user(msgs)

    # If only one segment, fall back to single archive
    if len(segments) <= 1:
        user_text = " ".join(
            m["content"][:5000] for m in msgs if m["role"] in ("user", "assistant")
        )
        return _archive_single(msgs, user_text)

    archived = 0
    for i, seg in enumerate(segments):
        seg_text = " ".join(
            m["content"][:5000] for m in seg if m["role"] in ("user", "assistant")
        )
        if not seg_text.strip():
            continue
        print(f"  [ARCHIVE] Segment {i+1}/{len(segments)}: {len(seg)} msgs, "
              f"{len(seg_text.split())} words", flush=True)
        archived += _archive_single(seg, seg_text)

    return archived


# ─── Tool Output Classification config ───
CLASSIFY_MAX_TOK   = 1024    # max tokens for classification response (thinking + answer)
CLASSIFY_TEMP      = 0.0     # deterministic classification

# ─── Multi-Pass Compression ────────────────────────────────────

COMPRESS_PROMPT_TEMPLATE = (
    "The following is the output of a {tool_name} call that was too large to process directly. "
    "Extract the key information, facts, and data that would be most relevant for answering "
    "the user's question. Preserve all critical details, code snippets, error messages, "
    "and specific values. Be comprehensive but concise. Format as clear structured text."
    "\n\n[TOOL OUTPUT]\n{tool_output}"
)

CLASSIFY_PROMPT_TEMPLATE = (
    "You are a classifier. Reply with ONLY one word: TEXT, STRUCTURED, or SHORT. No explanation, no thinking, just the word.\n\n"
    "Categories:\n"
    "TEXT — Articles, prose, HTML, documentation, error messages, natural language content. "
    "Can be summarized without losing critical information.\n"
    "STRUCTURED — Data tables, CSV, JSON arrays/objects with many records, log files, numeric datasets, "
    "API responses with structured records, spreadsheet data. Every value must be preserved — summarization loses data.\n"
    "SHORT — Under {threshold} characters, small enough to pass through unchanged.\n\n"
    "Tool: {tool_name}\n"
    "Size: {size} characters\n\n"
    "[TOOL OUTPUT PREVIEW — first 2000 chars]\n{preview}\n\n"
    "Reply with ONE word only:"
)

def compress_tool_output(tool_output: str, tool_name: str = "tool") -> str:
    """Use the model to extract key information from a large tool output.
    
    Returns the original output if compression fails or produces no result.
    This ensures nothing is silently lost.
    """
    if len(tool_output) <= COMPRESS_THRESHOLD:
        return tool_output
    
    prompt = COMPRESS_PROMPT_TEMPLATE.format(
        tool_name=tool_name,
        tool_output=tool_output,
    )
    
    print(f"  [COMPRESS] {tool_name} output: {len(tool_output)} chars -> compressing...", flush=True)
    
    try:
        result = query_model(
            [{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=COMPRESS_MAX_TOK,
        )
        compressed = result.get("content", "").strip()
        
        if not compressed:
            print(f"  [COMPRESS][WARN] Empty compression result, keeping original", flush=True)
            return tool_output
        
        print(f"  [COMPRESS] {len(tool_output)} -> {len(compressed)} chars "
              f"({len(compressed)*100//len(tool_output)}%)", flush=True)
        
        # Log compression for debugging
        try:
            with open("/tmp/compression_log.txt", "a", encoding="utf-8") as f:
                f.write(f"\n=== {datetime.now(timezone.utc).isoformat()} ===\n")
                f.write(f"TOOL: {tool_name}  ORIG: {len(tool_output)}  COMPRESSED: {len(compressed)}\n")
                f.write(f"--- COMPRESSED ---\n{compressed[:2000]}\n")
        except Exception as e:
            print(f"  [COMPRESS][LOG-ERROR] {e}", flush=True)
        
        return compressed
        
    except Exception as e:
        print(f"  [COMPRESS][ERROR] {type(e).__name__}: {e} — keeping original", flush=True)
        return tool_output


def classify_tool_output(tool_output: str, tool_name: str = "tool") -> str:
    """Classify a tool output as TEXT, STRUCTURED, or SHORT.

    Uses a fast model call (temp=0, 256 tokens) with only the first 2000 chars
    as preview. Returns one of: "TEXT", "STRUCTURED", "SHORT".
    Falls back to "TEXT" on any error (safe default: compression path).
    """
    size = len(tool_output)
    if size <= COMPRESS_THRESHOLD:
        return "SHORT"

    preview = tool_output[:2000]
    prompt = CLASSIFY_PROMPT_TEMPLATE.format(
        tool_name=tool_name,
        size=size,
        threshold=COMPRESS_THRESHOLD,
        preview=preview,
    )

    print(f"  [CLASSIFY] {tool_name} output: {size} chars — classifying...", flush=True)

    try:
        result = query_model(
            [{"role": "user", "content": prompt}],
            temperature=CLASSIFY_TEMP,
            max_tokens=CLASSIFY_MAX_TOK,
        )
        raw = result.get("content", "").strip().upper()

        # Extract just the category word
        for cat in ("TEXT", "STRUCTURED", "SHORT"):
            if cat in raw:
                print(f"  [CLASSIFY] {tool_name} → {cat} (raw: {raw[:80]})", flush=True)
                return cat

        # Fallback: if model returned something unexpected, default to TEXT
        print(f"  [CLASSIFY][WARN] Unexpected classification '{raw[:80]}', defaulting to TEXT", flush=True)
        return "TEXT"

    except Exception as e:
        print(f"  [CLASSIFY][ERROR] {type(e).__name__}: {e} — defaulting to TEXT", flush=True)
        return "TEXT"


def store_tool_chunk(tool_output: str, tool_name: str = "tool") -> str:
    """Store raw tool output in the tool_output_chunks table.

    Returns a short reference string to replace the content in messages.
    """
    chunk_id = f"chunk-{uuid.uuid4().hex[:8]}"
    size = len(tool_output)

    try:
        db.execute(
            "INSERT INTO tool_output_chunks (chunk_id, tool_name, content, size_chars, created_at) VALUES (?,?,?,?,?)",
            (chunk_id, tool_name, tool_output, size, datetime.now(timezone.utc).isoformat())
        )
        db.commit()

        reference = f"[Tool output indexed as {chunk_id}: {size:,} chars — use read_chunk('{chunk_id}') to retrieve]"
        print(f"  [CHUNK] Stored {tool_name} output as {chunk_id} ({size:,} chars)", flush=True)

        # Log chunk storage
        try:
            with open("/tmp/chunk_log.txt", "a", encoding="utf-8") as f:
                f.write(f"\n=== {datetime.now(timezone.utc).isoformat()} ===\n")
                f.write(f"CHUNK: {chunk_id}  TOOL: {tool_name}  SIZE: {size}\n")
                f.write(f"--- PREVIEW ---\n{tool_output[:8000]}\n")
        except Exception as e:
            print(f"  [CHUNK][LOG-ERROR] {e}", flush=True)

        return reference

    except Exception as e:
        print(f"  [CHUNK][ERROR] Failed to store chunk: {type(e).__name__}: {e}", flush=True)
        # Fallback: return truncated output if DB write fails
        return tool_output[:COMPRESS_THRESHOLD] + f"\n\n[... truncated, {size} chars total ...]"


def get_tool_chunk(chunk_id: str) -> Optional[str]:
    """Retrieve a stored tool output chunk by ID. Returns None if not found."""
    row = db.execute(
        "SELECT content FROM tool_output_chunks WHERE chunk_id=?", (chunk_id,)
    ).fetchone()
    return row[0] if row else None


def compress_large_tool_results(messages: list) -> list:
    """Silently stage large tool outputs for archival.
    
    Splits outputs > COMPRESS_THRESHOLD chars into chunks and adds each
    to the staging buffer. Model sees unchanged output — no chunk markers,
    no "continue" loops. Useful flag prevents repeated staging of same content.
    """
    _staged_hashes = getattr(compress_large_tool_results, '_staged_hashes', set())
    
    for msg in messages:
        if msg.get("role") != "tool":
            continue
        content = msg.get("content", "")
        if not isinstance(content, str) or len(content) <= COMPRESS_THRESHOLD:
            continue
        if not isinstance(content, str):
            continue
        
        # Dedup: don't stage the same page twice in same session
        import hashlib
        h = hashlib.md5(content[:200].encode()).hexdigest()
        if h in _staged_hashes:
            continue
        _staged_hashes.add(h)
        
        # Split into chunks and stage each
        for i in range(0, len(content), CHUNK_SIZE):
            chunk = content[i:i+CHUNK_SIZE]
            staging.add("assistant", chunk)
        
        print(f"  [STAGE] {len(content)} chars split into {(len(content)-1)//CHUNK_SIZE+1} chunks", flush=True)
        
        # Trigger archive if buffer has substantial content
        if staging.should_flush():
            import threading
            threading.Thread(target=archive_staging, daemon=True).start()
            print(f"  [STAGE] Auto-flushed staging buffer", flush=True)
    
    compress_large_tool_results._staged_hashes = _staged_hashes
    return messages  # unchanged — model sees full tool output


# ─── ORIGINAL CHUNKING (disabled) ───

def _compress_large_tool_results_OLD(messages: list) -> list:
    """Chunk large tool outputs for sequential reading.
    
    Large outputs are split into CHUNK_SIZE segments stored in a
    per-session buffer. The first chunk is injected inline with a
    [Chunk 1/N] marker. When the model replies "continue", the proxy
    swaps in the next chunk on the subsequent request.
    """
    global _active_chunks, _chunk_buffer
    _chunk_buffer = getattr(compress_large_tool_results, '_buffer', {})
    _active_chunks = getattr(compress_large_tool_results, '_active', {})
    
    session_id = "default"
    result = []
    
    # Debug: log all message roles
    for i, msg in enumerate(messages):
        r = msg.get("role", "?")
        c_len = len(msg.get("content", "")) if isinstance(msg.get("content", ""), str) else 0
        if c_len > 1000:
            print(f"  [CHUNK-DEBUG] msg[{i}] role={r} content_len={c_len}", flush=True)
    
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        
        if role == "tool":
            print(f"  [CHUNK-DEBUG] tool msg: type={type(content).__name__}, len={len(content) if hasattr(content, '__len__') else 'N/A'}", flush=True)
        if role == "tool" and isinstance(content, str) and len(content) > COMPRESS_THRESHOLD and "browser_navigate" not in content[:200]:
            print(f"  [CHUNK] Splitting {role} output: {len(content)} chars into {int(len(content)/CHUNK_SIZE)+1} chunks", flush=True)
            # Split into chunks
            chunks = [content[i:i+CHUNK_SIZE] for i in range(0, len(content), CHUNK_SIZE)]
            total = len(chunks)
            
            # Store all chunks
            _chunk_buffer[session_id] = chunks
            _active_chunks[session_id] = 0  # current chunk index
            
            # Inject first chunk with marker
            first = chunks[0]
            import hashlib, os as _os; hi = hashlib.md5(content[:200].encode()).hexdigest()[:8]; cd = "/tmp/mneme_chunks"; _os.makedirs(cd, exist_ok=True)
            for ci, chunk in enumerate(chunks):
                fp = f"{cd}/chunk_{hi}_{ci+1}of{total}.txt"
                with open(fp, "w") as cf:
                    cf.write(chunk)
            print(f"  [CHUNK] Wrote {total} chunks to {cd}/chunk_{hi}_*.txt", flush=True)
            hint = f"\n\n--- Page truncated. Read chunks with read_file: {cd}/chunk_{hi}_2of{total}.txt to {cd}/chunk_{hi}_{total}of{total}.txt ---"
            result.append({**msg, "content": first + hint})
            # Also stage full text for permanent archival with FAISS vector
            staging.add("assistant", content)
        else:
            result.append(msg)
    
    compress_large_tool_results._buffer = _chunk_buffer
    compress_large_tool_results._active = _active_chunks
    return result


def _advance_chunk(messages: list) -> list:
    return messages  # CHUNKING DISABLED


def _advance_chunk_OLD(messages: list) -> list:
    """If the last user message is 'continue', swap in the next chunk."""
    if not messages:
        return messages
    
    last = messages[-1]
    if last.get("role") != "user":
        return messages
    
    text = last.get("content", "").strip().lower()
    if text not in ("continue", "next", "more"):
        return messages
    
    session_id = "default"
    buf = getattr(compress_large_tool_results, '_buffer', {})
    active = getattr(compress_large_tool_results, '_active', {})
    
    chunks = buf.get(session_id, [])
    idx = active.get(session_id, 0) + 1
    
    if idx >= len(chunks):
        # All chunks consumed — replace with completion marker
        messages[-1]["content"] = "[All chunks read. Continue with your response.]"
        if session_id in buf:
            del buf[session_id]
        if session_id in active:
            del active[session_id]
        return messages
    
    # Swap in next chunk
    next_chunk = chunks[idx]
    active[session_id] = idx
    total = len(chunks)
    marker = f"\n\n[Chunk {idx+1}/{total} — reply \"continue\" for next chunk]"
    messages[-1]["content"] = next_chunk + marker
    
    compress_large_tool_results._buffer = buf
    compress_large_tool_results._active = active
    return messages

def _needs_chunk_loop(response_content: str) -> bool:
    """Check if model response is ONLY a chunk-advance signal.
    
    Strict: only exact short keywords. Longer responses with real content
    are NOT treated as chunk-advance signals.
    """
    text = response_content.strip().lower()
    if len(text) > 30:
        return False  # real response, not a chunk signal
    return text in ("continue", "next", "more", "next chunk", "continue reading", "[chunk loaded]")


def _model_loop_read_all(messages: list, tools: list = None) -> dict:
    return query_model(messages, tools=tools)  # CHUNKING DISABLED


def _model_loop_read_all_OLD(messages: list, tools: list = None) -> dict:
    """Internal loop: feed chunks to model until all consumed.
    
    Keeps calling Ollama as long as the model says "continue" after
    receiving a chunk. Returns the final non-continue result.
    """
    session_id = "default"
    buf = getattr(compress_large_tool_results, '_buffer', {})
    active = getattr(compress_large_tool_results, '_active', {})
    
    max_loops = 50
    nchunks = len(buf.get("default", []))
    print(f"  [LOOP] Model loop: {nchunks} chunks queued", flush=True)
    nchunks = len(buf.get("default", []))
    if nchunks > 1:
        print(f"  [LOOP] Chunk loop: {nchunks} chunks, auto-advancing tool calls", flush=True)
    for _ in range(max_loops):
        result = query_model(messages, tools=tools)
        content = result.get("content", "").strip()
        
        if not _needs_chunk_loop(content):
            # If model returned tool_calls but chunks remain, advance and loop
            tool_calls = result.get("tool_calls", []) or result.get("tool_calls_json", [])
            remaining = active.get("default", 0) < len(buf.get("default", []))
            if tool_calls and remaining:
                print(f"  [LOOP] Got tool_calls with {len(tool_calls)} calls — advancing chunk once", flush=True)
                _advance_chunk(messages)
                # After advancing, return the chunk content directly as a simulated response
                # so the model doesn't keep trying tools
                for i in range(len(messages) - 1, -1, -1):
                    if messages[i].get("role") == "tool":
                        return {"content": messages[i]["content"], "tool_calls": [], "eval_count": 0, "done_reason": "chunk_advance"}
                continue
            return result
        
        # Advance chunk and loop
        _advance_chunk(messages)
    
    # All chunks consumed — auto-save the conversation
    print(f"  [LOOP] All chunks consumed — auto-saving", flush=True)
    # Flush staging buffer to persist page content
    import threading
    threading.Thread(target=archive_staging, daemon=True).start()
    return {"content": "[All chunks consumed and saved. Continue with your analysis.]", 
            "thinking": "", "tool_calls": [], "eval_count": 0, "done_reason": "loop_complete"}

def process_chat(messages: list, session_id: str = "default", tools: list = None) -> dict:
    # Extract query from last USER message, not last message (which may be tool output)
    user_msg = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            user_msg = m["content"]
            break
    
    # ── Detail: load full chunk if DETAIL tag found ──
    import re as _detail_re
    # Scan last message regardless of role (model may output DETAIL in response)
    last_msg = messages[-1].get("content", "") if messages else ""
    detail_match = _detail_re.search(r"<<DETAIL\s+id:([^>]+)>>", last_msg, _detail_re.IGNORECASE)
    if detail_match:
        chunk_id = detail_match.group(1).strip()
        print(f"  [DETAIL] Loading chunk {chunk_id}", flush=True)
        chunk = load_chunk(chunk_id)
        if chunk:
            parts = []
            for m in chunk.get("messages", []):
                r = m["role"]
                c = m["content"][:8000]
                parts.append(f"{r}: {c}")
            full_text = "\n".join(parts)
            print(f"  [DETAIL] Returned {len(full_text)} chars", flush=True)
            return {"content": full_text[:20000], "tool_calls": [], "eval_count": 0, "done_reason": "detail"}
        else:
            return {"content": f"Chunk {chunk_id} not found.", "tool_calls": [], "eval_count": 0, "done_reason": "detail"}

    # ── Save trigger: <<SAVE>> forces archive ──
    SAVE_TRIGGER = "<<SAVE>>"
    if SAVE_TRIGGER in user_msg:
        user_msg = user_msg.replace(SAVE_TRIGGER, "").strip()
        messages[-1]["content"] = user_msg
        threading.Thread(target=archive_staging, daemon=True).start()
        print("  [SAVE] Triggered by user — archiving in background", flush=True)

    msg_tools = tools
    # Convert OpenAI-format tool_calls to Ollama format in incoming messages
    for m in messages:
        for tc in m.get("tool_calls", []):
            fn = tc.get("function", {})
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    fn["arguments"] = json.loads(args)
                except:
                    pass
    
    # Multi-pass compression: replace large tool outputs with model summaries
    # This prevents the model from burning its entire predict budget on raw HTML/JSON
    messages = compress_large_tool_results(messages)
    
    # Advance chunked tool output if user said "continue"
    messages = _advance_chunk(messages)
    
    # Advance chunked tool output if user said "continue"
    messages = _advance_chunk(messages)
    
    # Build injected memory
    context, ptype = build_context(user_msg)
    
    # Construct prompt with memory + system prompt + live messages
    prefix = SYSTEM_PROMPT
    if context:
        prefix += "\n\n" + context
    
    full_msgs = [{"role": "system", "content": prefix}] + messages
    
    # If chunks are pending, loop internally until all consumed
    buf = getattr(compress_large_tool_results, '_buffer', {})
    if buf.get("default"):
        result = _model_loop_read_all(full_msgs, tools=msg_tools)
    else:
        result = query_model(full_msgs, tools=msg_tools)
    
    staging.add("user", user_msg)
    if result["content"]:
        staging.add("assistant", result["content"])
    
    if staging.should_flush():
        threading.Thread(target=archive_staging, daemon=True).start()
    
    return {
        **result,
        "tool_calls": result.get("tool_calls", []),
        "context_injected": bool(context),
        "problem_type": ptype,
    }

# ─── Model Spoofing (for Hermes compatibility) ──────────────────

# Hermes requires models with >= 64001 context. We report a fake ID
# that includes this suffix so Hermes accepts the model.
FAKE_MODEL_ID = f"text-mneme:64k"
FAKE_CONTEXT   = 65536

# ─── Flask Proxy ───────────────────────────────────────────────

try:
    from flask import Flask, request, jsonify, Response, stream_with_context
    from flask_cors import CORS
    FLASK_OK = True
except ImportError:
    FLASK_OK = False

if FLASK_OK:
    app = Flask(__name__)
    CORS(app)
    
    def _cors_response(body, status=200):
        """Ensure CORS headers on every response."""
        resp = jsonify(body) if isinstance(body, dict) else body
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "*"
        return resp, status
    
    # ── OPTIONS preflight for all routes ──
    @app.route("/v1/chat/completions", methods=["OPTIONS"])
    @app.route("/api/chat/completions", methods=["OPTIONS"])
    @app.route("/v1/models", methods=["OPTIONS"])
    @app.route("/api/tags", methods=["OPTIONS"])
    @app.route("/api/show", methods=["OPTIONS"])
    def preflight():
        return _cors_response({})
    
    # ── Chat completions (non-streaming) ──
    @app.route("/v1/chat/completions", methods=["POST"])
    @app.route("/api/chat/completions", methods=["POST"])
    @app.route("/chat/completions", methods=["POST"])
    def chat_completions():
        data = request.get_json(force=True)
        stream = data.get("stream", False)

        print("  [DEBUG] stream={} model={}".format(stream, data.get("model", "?")), flush=True)
        messages = data.get("messages", [])
        

        if stream:
            return _chat_stream(messages, tools=data.get("tools"))
        
        result = process_chat(messages, tools=data.get("tools"))

        # /v1/ prefix = OpenAI format (provider: custom)
        # bare = Ollama format (provider: ollama)
        if request.path.startswith("/v1"):
            msg_obj = {"role": "assistant", "content": result.get("content", "")}
            if result.get("thinking"):
                msg_obj["reasoning"] = result["thinking"]
            if result.get("tool_calls"):
                oai_tc = []
                for i, tc in enumerate(result["tool_calls"]):
                    fn = tc.get("function", {})
                    args = fn.get("arguments", {})
                    args_str = json.dumps(args) if isinstance(args, dict) else args
                    oai_tc.append({
                        "index": i,
                        "id": tc.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": fn.get("name", ""),
                            "arguments": args_str,
                        }
                    })
                msg_obj["tool_calls"] = oai_tc
            return _cors_response({
                "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
                "object": "chat.completion",
                "system_fingerprint": "fp_ollama",
                "created": int(time.time()),
                "model": FAKE_MODEL_ID,
                "choices": [{
                    "index": 0,
                    "message": msg_obj,
                    "finish_reason": "tool_calls" if result.get("tool_calls") else "stop",
                }],
                "usage": {"prompt_tokens": 0, "completion_tokens": result.get("eval_count", 0), "total_tokens": result.get("eval_count", 0)},
            })
        else:
            msg = {"role": "assistant", "content": result.get("content", "")}
            if result.get("thinking"):
                msg["thinking"] = result["thinking"]
            if result.get("tool_calls"):
                msg["tool_calls"] = result["tool_calls"]
            return _cors_response({
                "model": FAKE_MODEL_ID,
                "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "message": msg,
                "done": True,
                "done_reason": result.get("done_reason", "stop"),
                "total_duration": int(time.time() * 1e9) % 1000000000,
                "load_duration": 0,
                "prompt_eval_count": result.get("eval_count", 0),
                "prompt_eval_duration": 0,
                "eval_count": result.get("eval_count", 0),
                "eval_duration": result.get("eval_count", 0) * 1000000,
            })
    
    # ── Chat completions (SSE streaming) ──
    def _chat_stream(messages, tools=None):
        result = process_chat(messages, tools=tools)
        content = result["content"]
        tool_calls = result.get("tool_calls", [])
        cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        
        def generate():
            # Send role first
            yield "data: " + json.dumps({
                "id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                "model": FAKE_MODEL_ID,
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            }) + "\n\n"
            
            # Stream thinking as reasoning if present
            thinking = result.get("thinking", "")
            if thinking:
                thinking_chunk = 16
                for i in range(0, len(thinking), thinking_chunk):
                    piece = thinking[i:i + thinking_chunk]
                    yield "data: " + json.dumps({
                        "id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                        "model": FAKE_MODEL_ID,
                        "choices": [{"index": 0, "delta": {"reasoning": piece, "role": "assistant"}, "finish_reason": None}],
                    }) + "\n\n"
            
            # If model returned tool_calls, emit them as deltas (OpenAI-style)
            if tool_calls:
                for i, tc in enumerate(tool_calls):
                    yield "data: " + json.dumps({
                        "id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                        "model": FAKE_MODEL_ID,
                        "choices": [{
                            "index": 0,
                            "delta": {
                                "tool_calls": [{
                                    "index": i,
                                    "id": tc.get("id", f"call_{uuid.uuid4().hex[:24]}"),
                                    "type": tc.get("type", "function"),
                                    "function": {
                                        "name": tc.get("function", {}).get("name", ""),
                                        "arguments": json.dumps(tc.get("function", {}).get("arguments", {})) if isinstance(tc.get("function", {}).get("arguments"), dict) else tc.get("function", {}).get("arguments", ""),
                                    },
                                }]
                            },
                            "finish_reason": None,
                        }],
                    }) + "\n\n"
                # Done with tool_calls finish reason
                yield "data: " + json.dumps({
                    "id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                    "model": FAKE_MODEL_ID,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                    "usage": {"completion_tokens": result.get("eval_count", 0)},
                }) + "\n\n"
                yield "data: [DONE]\n\n"
                return
            
            # Stream thinking as reasoning if present
            thinking = result.get("thinking", "")
            if thinking:
                thinking_chunk = 16
                for i in range(0, len(thinking), thinking_chunk):
                    piece = thinking[i:i + thinking_chunk]
                    yield "data: " + json.dumps({
                        "id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                        "model": FAKE_MODEL_ID,
                        "choices": [{"index": 0, "delta": {"reasoning": piece, "role": "assistant"}, "finish_reason": None}],
                    }) + "\n\n"
            
            # Stream content in chunks
            chunk_size = 16
            for i in range(0, len(content), chunk_size):
                piece = content[i:i + chunk_size]
                yield "data: " + json.dumps({
                    "id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                    "model": FAKE_MODEL_ID,
                    "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
                }) + "\n\n"
            
            # Done
            yield "data: " + json.dumps({
                "id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                "model": FAKE_MODEL_ID,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"completion_tokens": result.get("eval_count", 0)},
            }) + "\n\n"
            yield "data: [DONE]\n\n"
        
        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "Access-Control-Allow-Origin": "*",
            }
        )
    
    # ── Model list (Hermes-compatible) ──
    @app.route("/v1/models", methods=["GET"])
    @app.route("/models", methods=["GET"])
    def list_models():
        return _cors_response({
            "object": "list",
            "data": [{
                "id": FAKE_MODEL_ID,
                "object": "model",
                "owned_by": "text-mokv",
            }],
        })
    
    # ── Single model info (OpenAI-compatible) ──
    @app.route("/v1/models/<model_id>", methods=["GET"])
    def get_model(model_id):
        return _cors_response({
            "id": model_id,
            "object": "model",
            "created": int(time.time()),
            "owned_by": "text-mokv",
        })
    
    # ── Ollama-style /api/tags (for apps that query it) ──
    @app.route("/api/tags", methods=["GET"])
    def api_tags():
        return _cors_response({
            "models": [{
                "name": FAKE_MODEL_ID,
                "modified_at": datetime.now(timezone.utc).isoformat(),
                "size": 0,
                "digest": "text-mokv-v3",
                "details": {
                    "format": "text-memory",
                    "family": "text-mokv",
                    "parameter_size": "8B",
                },
            }],
        })
    
    # ── Ollama-style /api/show (model info) ──
    @app.route("/api/show", methods=["POST"])
    def api_show():
        data = request.get_json(force=True) or {}
        name = data.get("name", FAKE_MODEL_ID)
        return _cors_response({
            "modelfile": f"# Mneme\n# Model: {MODEL}\n",
            "parameters": "",
            "template": "",
            "details": {
                "format": "text-memory",
                "family": "text-mokv",
                "parameter_size": "8B",
            },
        })
    
    # ── Health check ──
    @app.route("/health", methods=["GET"])
    def health():
        return _cors_response({
            "status": "ok",
            "model": FAKE_MODEL_ID,
            "backend": MODEL,
            "chunks": len(_id_map),
        })

    # ── Save: force-flush the staging buffer ──
    @app.route("/save", methods=["POST"])
    def save():
        try:
            n = archive_staging()
            return _cors_response({"saved": True, "chunks": n})
        except Exception as e:
            print(f"  [SAVE][ERROR] {e}", flush=True)
            return _cors_response({"saved": False, "error": str(e)}, status=500)

# ─── Startup ───────────────────────────────────────────────────

_load_index()
print(f"[mokv] Mneme ready. model={MODEL} chunks={len(_id_map)} db={DB_PATH}",
      flush=True)

if __name__ == "__main__":
    if FLASK_OK:
        app.run(host="0.0.0.0", port=8080, threaded=True)
    else:
        print("[mokv] Flask not installed. Import as module for programmatic use.",
              flush=True)
