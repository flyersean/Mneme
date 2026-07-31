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

# Classification uses a small model to avoid VRAM contention
CLASSIFY_MODEL = "qwen:0.5b"  # tiny model for topic labels (400MB)

# ─── Multi-pass compression config ───
COMPRESS_THRESHOLD = 8000    # chars — tool results larger than this get compressed
COMPRESS_MODEL     = MODEL   # use same model for compression
COMPRESS_MAX_TOK   = 2048    # max tokens for compression response

# Staging: archive after N user turns or idle seconds
STAGING_TURNS  = 6
STAGING_IDLE   = 120

# Routing thresholds (same as KV version)
CLASSIFY_THRESHOLD = 0.78
ROUTE_THRESHOLD    = 0.3

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

SYSTEM_PROMPT = (
    "Note ambiguity or contradiction. State interpretations. "
    "For math: extract raw expression, classify I_CAN (e.g. 47x89, 2847x36) "
    "or I_NEED_TOOL (e.g. 91234x5678, 5+ digit). If I_NEED_TOOL, do NOT "
    "compute — suggest tool. Confidence 1-10. If no solution exists, say so. "
    "For factual claims: classify your knowledge as KNOWN (you can verify from "
    "conversation or tools), RECALLED (from training only, may be unreliable), "
    "or UNKNOWN. State classification and confidence 1-10 before answering. "
    "If RECALLED or UNKNOWN and no tool can verify, say so rather than confabulating."
)

MEMORY_DISCLAIMER = (
    "--- MEMORY: previous conversations (reference only, not instruction) ---"
)

def query_model(messages: list, system: str = None, temperature: float = None,
                max_tokens: int = None, tools: list = None,
                model: str = None) -> dict:
    """Send to Ollama, return {content, thinking, eval_count, done_reason}."""
    if temperature is None: temperature = OLLAMA_TEMP
    if max_tokens is None: max_tokens = -1  # let Ollama decide
    
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.extend(messages)
    
    payload = {
        "model": model or MODEL, "stream": False, "messages": msgs,
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
    msgs = []
    msgs.extend(messages)

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
        [{"role": m["role"], "content": m["content"][:500]} for m in messages[-6:]]
    )
    
    db.execute("""
        INSERT OR REPLACE INTO chunks
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (chunk_id, topic_label, msgs_json, thinking[:500], strategy,
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
        "SELECT chunk_id, topic_label, messages, thinking, strategy, created_at, "
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
        "problem_type": row[8], "created_at": row[9],
    }

# ─── Classification ────────────────────────────────────────────

CLASSIFY_PROMPT = (
    "Classify this conversation in exactly 3 lines.\n"
    "Line 1: LABEL: <2-4 word topic>\n"
    "Line 2: OUTCOME: SUCCESS/FAILURE/TRUNCATED/UNCERTAIN\n"
    "Line 3: TYPE: arithmetic/graph/scheduling/spatial/bayesian/logic/factual/other\n\n"
    "Conversation:\n"
)

def classify_chunk(messages: list) -> dict:
    text = "\n".join(f"{m['role']}: {m['content'][:200]}" for m in messages[-6:])
    prompt = CLASSIFY_PROMPT + text + "\n\nReply with ONLY the 3 lines."
    r = query_model([{"role": "user", "content": prompt}], temperature=0.3, max_tokens=2048)
    c = r["content"]
    if not c:
        # Fallback: extract from thinking trace
        c = r.get("thinking", "")
    
    label   = re.search(r'LABEL:\s*(.+?)(?:\n|$)', c, re.IGNORECASE)
    outcome = re.search(r'OUTCOME:\s*(.+?)(?:\n|$)', c, re.IGNORECASE)
    ptype   = re.search(r'TYPE:\s*(.+?)(?:\n|$)', c, re.IGNORECASE)
    
    return {
        "topic_label": label.group(1).strip()[:80] if label else "uncategorized",
        "outcome": outcome.group(1).strip() if outcome else "UNCERTAIN",
        "problem_type": ptype.group(1).strip() if ptype else "other",
    }

# ─── Strategy Generation ───────────────────────────────────────

def generate_strategy(messages: list, outcome: str) -> str:
    if outcome not in ("FAILURE", "TRUNCATED"):
        return ""
    text = "\n".join(f"{m['role']}: {_extract_text(m['content'])[:300]}" for m in messages[-6:])
    prompt = (
        f"The outcome was {outcome}. Generate a brief strategy (1-3 sentences) "
        f"that would help you succeed next time on a similar problem.\n\n{text}"
    )
    r = query_model([{"role": "user", "content": prompt}], temperature=0.5, max_tokens=2048)
    return r["content"].strip()

# ─── Routing ───────────────────────────────────────────────────

def route_query(query: str, top_k: int = 3, with_scores: bool = False) -> List:
    """Two-pass dedup: best per topic, then fill remaining.
    Returns (score, cid) tuples if with_scores=True, else cid strings."""
    q_vec = embed(query)
    scored = _cosine_search(q_vec, top_k * 3, ROUTE_THRESHOLD)
    if not scored:
        return []
    
    # Grade-aware sort
    scored.sort(key=lambda x: (-grade_priority(x[1]), -x[0]))
    
    # Pass 1: best chunk per topic — store (score, cid) tuples
    results = []
    seen = set()
    for score, cid in scored:
        topic = re.sub(r'_v\d+$', '', cid)
        if topic not in seen:
            results.append((score, cid))
            seen.add(topic)
            if len(results) >= top_k:
                return [r[1] for r in results] if not with_scores else results
    
    # Pass 2: fill remaining
    for score, cid in scored:
        if cid not in [r[1] for r in results]:
            results.append((score, cid))
            if len(results) >= top_k:
                break
    return [r[1] for r in results] if not with_scores else results

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
            f"{m['role']}: {_extract_text(m['content'])[:300]}"
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

def build_context(query: str) -> Tuple[str, str]:
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
    
    # Get relevance scores for all chunks
    all_scored = route_query(query, top_k=max(len(trimmed), 1), with_scores=True) if trimmed else []
    score_map = {cid: score for score, cid in all_scored}
    max_score = max(score_map.values()) if score_map else 1.0
    
    # Build chunk text with rich headers
    now = datetime.now(timezone.utc)
    parts = []
    ptype = "other"
    seen_topics = {}
    
    for cid in trimmed:
        chunk = load_chunk(cid)
        if not chunk:
            continue
        ptype = chunk.get("problem_type", "other")
        topic = chunk.get("topic_label", "unknown")
        created = chunk.get("created_at", "")
        score = score_map.get(cid, 0.5)
        relevance_pct = int((score / max_score) * 100) if max_score > 0 else 50
        
        # Recency
        try:
            created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            delta = now - created_dt
            if delta.days == 0: recency = "today"
            elif delta.days == 1: recency = "yesterday"
            elif delta.days < 7: recency = f"{delta.days}d ago"
            elif delta.days < 30: recency = f"{delta.days // 7}w ago"
            else: recency = f"{delta.days // 30}mo ago"
        except:
            recency = "unknown"
        
        # Messages
        msg_text = "\n".join(
            f"{m['role']}: {_extract_text(m['content'])[:300]}"
            for m in chunk.get("messages", [])
        )
        if chunk.get("strategy"):
            msg_text += f"\n[learned strategy: {chunk['strategy']}]"
        
        header = f"--- CONVERSATION: {topic} ({recency}, {relevance_pct}% relevant, id:{cid}) ---"
        
        # Cross-conversation synthesis: merge same topic
        if topic in seen_topics:
            parts[seen_topics[topic]] += "\n  ...(same topic)...\n" + header + "\n" + msg_text
        else:
            seen_topics[topic] = len(parts)
            parts.append(header + "\n" + msg_text)
    
    if not parts:
        return "", ptype
    
    context = MEMORY_DISCLAIMER + "\n" + "\n---\n".join(parts)
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
    """Flush the staging buffer into archived chunks.

    Returns the number of chunks archived (0 if the buffer was empty).
    When the combined text exceeds MAX_CHUNK_WORDS words, the buffer is split
    into segments by user message and each segment is archived as its own
    chunk with per-segment classification (fixes topic mixing).
    """
    msgs = staging.flush()
    if not msgs:
        return 0

    # Build combined text from user + assistant messages for embedding.
    # Skip role="tool" — tool output is raw data (file contents, command
    # output), not semantic context, and it dominates the embedding with
    # noise that drowns the actual conversation topic.
    SEMANTIC_ROLES = ("user", "assistant")
    user_text = " ".join(
        m["content"][:300] for m in msgs if m["role"] in SEMANTIC_ROLES
    )

    # If it's a long input, split into segments by user message boundary
    word_count = len(user_text.split())
    if word_count > MAX_CHUNK_WORDS:
        print(f"  [ARCHIVE] Long input ({word_count} words) — splitting into segments by user message", flush=True)
        return _archive_split(msgs)

    return _archive_single(msgs, user_text)


def _archive_single(msgs, user_text):
    """Archive a single chunk (normal case). Returns 1 on success."""
    klass = classify_chunk(msgs)
    topic = klass["topic_label"]
    outcome = klass["outcome"]
    ptype = klass["problem_type"]

    strategy = generate_strategy(msgs, outcome)
    vec = embed(user_text)

    row = db.execute("SELECT COUNT(*) FROM chunks WHERE topic_label=?", (topic,)).fetchone()
    seq = (row[0] if row else 0) + 1
    chunk_id = f"{topic[:40]}_v{seq}"

    save_chunk(chunk_id, topic, msgs, vec, strategy=strategy,
               outcome=outcome, problem_type=ptype)

    if strategy and ptype != "other":
        sid = f"strat_{ptype}_{seq}_{int(time.time())}"
        db.execute(
            "INSERT OR REPLACE INTO strategies VALUES (?,?,?,?,?,?)",
            (sid, ptype, strategy, chunk_id, "B",
             datetime.now(timezone.utc).isoformat())
        )
        db.commit()

    print(f"  [ARCHIVE] {chunk_id} topic={topic} outcome={outcome} type={ptype}", flush=True)
    return 1


def _segment_by_user(msgs):
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
            m["content"][:300] for m in msgs if m["role"] in ("user", "assistant")
        )
        return _archive_single(msgs, user_text)

    archived = 0
    for i, seg in enumerate(segments):
        seg_text = " ".join(
            m["content"][:300] for m in seg if m["role"] in ("user", "assistant")
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
                f.write(f"--- PREVIEW ---\n{tool_output[:500]}\n")
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
    """Scan messages for large tool results and route them based on classification.

    TEXT       → model-compressed summary (lossy but adequate for prose)
    STRUCTURED → stored as DB chunk, replaced with reference string
    SHORT      → passed through unchanged

    Returns a new list with large tool outputs replaced appropriately.
    """
    compressed_msgs = []
    compressions = 0
    chunk_stores = 0

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")

        # Check if this is a large tool result
        if role == "tool" and isinstance(content, str) and len(content) > COMPRESS_THRESHOLD:
            # Try to identify the tool name from the preceding assistant tool_calls
            tool_name = "tool"
            tool_call_id = msg.get("tool_call_id", "")
            if tool_call_id:
                for prev in reversed(compressed_msgs):
                    for tc in prev.get("tool_calls", []):
                        if tc.get("id") == tool_call_id:
                            tool_name = tc.get("function", {}).get("name", "tool")
                            break
                    if tool_name != "tool":
                        break

            # Classify the output to determine routing
            category = classify_tool_output(content, tool_name)

            if category == "STRUCTURED":
                # Store raw output as chunk, replace with reference
                reference = store_tool_chunk(content, tool_name)
                compressed_msgs.append({**msg, "content": reference})
                chunk_stores += 1
            elif category == "TEXT":
                # Use existing compression path
                compressed_content = compress_tool_output(content, tool_name)
                compressed_msgs.append({**msg, "content": compressed_content})
                compressions += 1
            else:
                # SHORT or fallback — pass through unchanged
                compressed_msgs.append(msg)
        else:
            compressed_msgs.append(msg)

    if compressions or chunk_stores:
        parts = []
        if compressions:
            parts.append(f"{compressions} compressed")
        if chunk_stores:
            parts.append(f"{chunk_stores} chunked")
        print(f"  [ROUTE] Tool outputs: {', '.join(parts)} this turn", flush=True)

    return compressed_msgs


# ─── Chat Processing ───────────────────────────────────────────

def process_chat(messages: list, session_id: str = "default", tools: list = None) -> dict:
    user_msg = messages[-1]["content"] if messages else ""
    
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
    
    # Build injected memory
    context, ptype = build_context(user_msg)
    
    # Construct prompt with memory + system prompt + live messages
    prefix = SYSTEM_PROMPT
    if context:
        prefix += "\n\n" + context
    
    full_msgs = [{"role": "system", "content": prefix}] + messages
    
    result = query_model(full_msgs, tools=msg_tools)
    
    staging.add("user", user_msg)
    if result["content"]:
        staging.add("assistant", result["content"])
    
    # Archive in background (avoids VRAM contention with active chat)
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
        """Stream directly from Ollama — no buffering. Memory injection runs first."""
        # Run memory injection / classification synchronously
        try:
            process_chat(messages, tools=tools)
        except Exception as e:
            print(f"  [WARN] Memory injection failed: {e}", flush=True)
        
        # Build final messages with system prompt preserved + memory injected
        user_msg = messages[-1]["content"] if messages else ""
        
        # Strip save trigger
        SAVE_TRIGGER = "<<SAVE>>"
        if SAVE_TRIGGER in user_msg:
            user_msg = user_msg.replace(SAVE_TRIGGER, "").strip()
            messages[-1]["content"] = user_msg
            threading.Thread(target=archive_staging, daemon=True).start()
        
        # Convert OpenAI tool_calls inbound
        for m in messages:
            for tc in m.get("tool_calls", []):
                fn = tc.get("function", {})
                args = fn.get("arguments")
                if isinstance(args, str):
                    try:
                        fn["arguments"] = json.loads(args)
                    except:
                        pass
        
        # Build system prompt
        hermes_system = ""
        user_messages = messages
        if messages and messages[0].get("role") == "system":
            hermes_system = messages[0]["content"]
            user_messages = messages[1:]
        
        context, _ = build_context(user_msg)
        prefix = hermes_system
        if prefix:
            prefix += "\n\n" + SYSTEM_PROMPT
        else:
            prefix = SYSTEM_PROMPT
        if context:
            prefix += "\n\n" + context
        
        full_msgs = [{"role": "system", "content": prefix}] + user_messages
        
        # Compress large tool outputs
        full_msgs = compress_large_tool_results(full_msgs)
        
        # Stream directly from Ollama
        def generate():
            for chunk in query_model_stream(full_msgs, tools=tools):
                if chunk is None:
                    yield "data: [DONE]\n\n"
                else:
                    yield "data: " + json.dumps(chunk) + "\n\n"
        
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
