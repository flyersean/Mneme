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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import List, Dict, Optional, Tuple

import numpy as np
import requests

# ─── Config ────────────────────────────────────────────────────
OLLAMA_URL  = "http://localhost:11434"
MODEL       = os.environ.get("MNEME_MODEL", "fredrezones55/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive:latest")
CHUNK_DIR   = os.environ.get("MNEME_CHUNK_DIR", "/workspace/mneme_chunks")
INJECT_SYSTEM = os.environ.get("MNEME_INJECT_SYSTEM", "1")  # "0" to skip Mneme instructions injection
DB_PATH     = os.path.join(CHUNK_DIR, "mneme.db")

# Ollama config — let the model use its defaults
OLLAMA_TEMP    = 0.3

# ─── Multi-pass compression config ───
# CLASSIFY_MODEL removed — using embedding-based classification
MAX_HISTORY_MESSAGES = 32  # trim conversation to keep predict budget free
CHUNK_SIZE = 2000   # chars per chunk for splitting large tool outputs
DB_MSG_CAP  = 8000  # chars per message stored in SQLite (full content)
COMPRESS_THRESHOLD = 500    # chars — tool results larger than this get compressed
COMPRESS_MODEL     = MODEL   # use same model for compression
COMPRESS_MAX_TOK   = 2048    # max tokens for compression response

# Staging: archive after N user turns or idle seconds
STAGING_TURNS  = 6
STAGING_IDLE   = 120

# Routing thresholds (same as KV version)
CLASSIFY_THRESHOLD = 0.78
ROUTE_THRESHOLD    = 0.08  # tunable: raise for stricter matching, lower for more recall
BASELINE_NOISE     = 0.20  # fallback — overridden at startup by _calibrate_noise()
AGE_DECAY_DAYS     = 7     # recency half-life in days — newer chunks get a bonus

# Save-cycle counter — incremented on every staging flush AND manual <<SAVE>>
_archive_cycle = 0
_archive_cycle_lock = threading.Lock()
_chunk_seq = 0
_chunk_seq_lock = threading.Lock()

def _seed_chunk_seq():
    global _chunk_seq
    try:
        row = db.execute(
            "SELECT COALESCE(MAX(CAST(SUBSTR(chunk_id, 5) AS INTEGER)), 0) FROM chunks WHERE chunk_id LIKE 'mem_%'"
        ).fetchone()
        if row and row[0]:
            _chunk_seq = row[0]
            print(f"  [STARTUP] chunk_seq seeded to {_chunk_seq}", flush=True)
    except Exception:
        pass

def _next_cycle() -> int:
    global _archive_cycle
    with _archive_cycle_lock:
        _archive_cycle += 1
        return _archive_cycle

def _current_cycle() -> int:
    with _archive_cycle_lock:
        return _archive_cycle

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
        source      TEXT DEFAULT 'unknown',
        cycle       INTEGER DEFAULT 0,
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
""")

# ─── Schema migrations for existing DBs ─────────────────────────
for migration in (
    "ALTER TABLE chunks ADD COLUMN source TEXT DEFAULT 'unknown'",
    "ALTER TABLE chunks ADD COLUMN cycle INTEGER DEFAULT 0",
    "ALTER TABLE chunks ADD COLUMN session_id TEXT DEFAULT 'default'",
    "ALTER TABLE chunks ADD COLUMN indexable INTEGER DEFAULT 1",
    "ALTER TABLE strategies ADD COLUMN version INTEGER DEFAULT 1",
    "ALTER TABLE strategies ADD COLUMN parent_id TEXT DEFAULT ''",
    "ALTER TABLE strategies ADD COLUMN effective_grade REAL DEFAULT 0.0",
    "ALTER TABLE strategies ADD COLUMN use_count INTEGER DEFAULT 0",
    "ALTER TABLE strategies ADD COLUMN success_count INTEGER DEFAULT 0",
):
    try:
        db.execute(migration)
    except sqlite3.OperationalError:
        pass  # column already exists
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

# Multi-writer FAISS: disk persistence + file locking
FAISS_INDEX_FILE = os.path.join(CHUNK_DIR, "faiss.index")
FAISS_IDMAP_FILE = os.path.join(CHUNK_DIR, "faiss.idmap")
FAISS_LOCK_FILE   = os.path.join(CHUNK_DIR, "faiss.lock")

import fcntl

class faiss_lock:
    """Context manager for fcntl file lock around FAISS operations.
    Kernel-enforced — released on process death, no stale locks."""
    def __init__(self):
        self._fd = None
    def __enter__(self):
        self._fd = open(FAISS_LOCK_FILE, "w")
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        return self
    def __exit__(self, *args):
        if self._fd:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            self._fd.close()

def _save_index():
    """Save FAISS index + id_map to disk. Caller must hold faiss_lock."""
    if FAISS_OK and _index is not None:
        faiss.write_index(_index, FAISS_INDEX_FILE)
    with open(FAISS_IDMAP_FILE, "w") as f:
        json.dump(list(_id_map), f)

def _load_index_from_disk():
    """Load FAISS index + id_map from disk. Caller must hold faiss_lock."""
    global _id_map, _index
    if os.path.exists(FAISS_INDEX_FILE) and FAISS_OK:
        _index = faiss.read_index(FAISS_INDEX_FILE)
    else:
        _index = faiss.IndexFlatIP(DIM) if FAISS_OK else None
    if os.path.exists(FAISS_IDMAP_FILE):
        with open(FAISS_IDMAP_FILE) as f:
            _id_map = json.load(f)
    else:
        _id_map = []

def _load_index():
    """Rebuild FAISS index from SQLite (fallback if disk files missing)."""
    global _id_map
    rows = db.execute("SELECT chunk_id, vector FROM chunks WHERE vector IS NOT NULL").fetchall()
    with faiss_lock():
        _id_map.clear()
        if FAISS_OK and _index is not None:
            _index.reset()
        for cid, blob in rows:
            vec = _blob_to_vec(blob)
            if vec is not None:
                if FAISS_OK and _index is not None:
                    _index.add(vec.reshape(1, -1))
                _id_map.append(cid)
        _save_index()
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
    """Search FAISS with file lock — loads index from disk, searches, releases.
    Multi-writer safe: any proxy with the lock sees the latest index state."""
    with faiss_lock():
        _load_index_from_disk()  # Always fresh from disk
        if not _id_map:
            return []
        if FAISS_OK and _index is not None:
            k = min(top_k, len(_id_map))
            scores, idxs = _index.search(query_vec.reshape(1, -1), k)
            return [(float(s), _id_map[i]) for s, i in zip(scores[0], idxs[0])
                    if i >= 0 and float(s) >= threshold]
        return []

def _keyword_search(query: str, top_k: int, exclude_ids: set = None):
    """SQLite LIKE keyword fallback when FAISS is sparse.
    
    Splits query into words, searches messages column for each,
    deduplicates, returns [(0.0, chunk_id), ...] ordered by recency.
    """
    if not query or not query.strip():
        return []
    words = [w.strip() for w in query.split() if len(w.strip()) >= 2]
    if not words:
        return []
    exclude_ids = exclude_ids if exclude_ids is not None else set()
    seen = set()
    results = []
    # Search each word, collect matching chunk_ids
    for word in words:
        pattern = f"%{word}%"
        rows = db.execute(
            "SELECT chunk_id FROM chunks WHERE messages LIKE ? ORDER BY created_at DESC LIMIT ?",
            (pattern, top_k * 2)
        ).fetchall()
        for (cid,) in rows:
            if cid not in seen and cid not in exclude_ids:
                seen.add(cid)
                results.append((0.0, cid))  # score 0.0 = keyword match, no semantic score
            if len(results) >= top_k:
                break
        if len(results) >= top_k:
            break
    return results[:top_k]

def _hybrid_search(query: str, top_k: int, faiss_results: list):
    """Fill FAISS results with keyword matches if below threshold.
    
    Returns list of (score, chunk_id, method) tuples.
    """
    faiss_ids = {cid for _, cid in faiss_results}
    combined = [(s, cid, "faiss") for s, cid in faiss_results]
    if len(combined) < top_k:
        needed = top_k - len(combined)
        kw_results = _keyword_search(query, needed, exclude_ids=faiss_ids)
        combined.extend([(s, cid, "keyword") for s, cid in kw_results])
    return combined

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
                max_tokens: int = None, tools: list = None, options: dict = None) -> dict:
    """Send to Ollama, return {content, thinking, eval_count, done_reason}.
    Pass options dict for top_p, top_k, mirostat, etc."""
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
    for m in trimmed:
            mc = _extract_text(m.get("content", ""))
            msgs.append({"role": m["role"], "content": mc})
    
    opts = {"temperature": temperature}
    if options:
        opts.update(options)
    
    payload = {
        "model": MODEL, "stream": False, "messages": msgs,
        "options": opts
    }
    if tools:
        payload["tools"] = tools
    
    # Smarter truncation: keep first user message (task context) + last 2 turns
    MAX_MSG_CHARS = 0  # disabled
    trimmed_msgs = []
    for m in msgs:
        content = m.get("content", "")
        if MAX_MSG_CHARS > 0 and isinstance(content, str) and len(content) > MAX_MSG_CHARS:
            trimmed_msgs.append({**m, "content": content[:MAX_MSG_CHARS] + "..."})
        else:
            trimmed_msgs.append(m)
    
    sys_msgs = [m for m in trimmed_msgs if m.get("role") == "system"]
    non_sys = [m for m in trimmed_msgs if m.get("role") != "system"]
    
    if len(non_sys) > 4:
        first_user = [m for m in non_sys if m.get("role") == "user"][:1]  # task context
        recent = non_sys[-4:]  # last 2 turns
        msgs = sys_msgs + first_user + recent
    else:
        msgs = sys_msgs + non_sys
    
    total = sum(len(m.get("content","")) for m in msgs)
    if total > MAX_PROMPT_CHARS:
        print(f"  [WARN] Still {total} chars after trim — stripping oldest", flush=True)
        msgs = sys_msgs + non_sys[-3:]
    
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
def save_chunk(chunk_id: str, topic_label: str, messages: list,
               vector: np.ndarray, thinking: str = "", strategy: str = "",
               grade: str = "C", consensus: float = 0.0,
               outcome: str = "", problem_type: str = "other",
               source: str = "unknown", session_id: str = "default"):
    """Insert chunk into SQLite + FAISS."""
    # Source-tiered indexing: only index user/page/tool content, not model hallucinations
    is_indexable = True
    if source and source.startswith("model"):
        if grade in ("C", "D", "F"):
            is_indexable = False
    blob = _vec_to_blob(vector)
    msgs_json = json.dumps(
        [{"role": m["role"], "content": m["content"][:DB_MSG_CAP]} for m in messages]
    )

    db.execute("""
        INSERT OR REPLACE INTO chunks
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (chunk_id, topic_label, msgs_json, thinking[:8000], strategy,
          blob, grade, consensus, outcome, problem_type,
          source, _current_cycle(), datetime.now(timezone.utc).isoformat(), session_id, 1 if is_indexable else 0))

    db.commit()
    
    # Add to FAISS (only if indexable) — multi-writer safe via file lock
    if is_indexable:
        with faiss_lock():
            _load_index_from_disk()
            if FAISS_OK and _index is not None:
                _index.add(vector.reshape(1, -1))
            _id_map.append(chunk_id)
            _save_index()

def load_chunk(chunk_id: str) -> Optional[dict]:
    row = db.execute(
        "SELECT chunk_id, topic_label, messages, thinking, strategy, "
        "grade, consensus, outcome, problem_type, source, session_id, cycle FROM chunks WHERE chunk_id=?",
        (chunk_id,)
    ).fetchone()
    if not row:
        return None
    return {
        "chunk_id": row[0], "topic_label": row[1],
        "messages": json.loads(row[2]), "thinking": row[3],
        "strategy": row[4], "grade": row[5],
        "consensus": row[6], "outcome": row[7],
        "problem_type": row[8], "source": row[9], "session_id": row[10], "cycle": row[11],
    }

# ─── Classification ────────────────────────────────────────────

CLASSIFY_PROMPT = (
    "Classify this conversation in exactly 3 lines.\n"
    "Line 1: LABEL: <2-4 word topic>\n"
    "Line 2: OUTCOME: SUCCESS/FAILURE/TRUNCATED/UNCERTAIN\n"
    "Line 3: TYPE: arithmetic/graph/scheduling/spatial/bayesian/logic/factual/other\n\n"
    "Conversation:\n"
)

# ─── Content-derived topic labels ─────────────────────────────

def _clean_content(text: str) -> str:
    """Strip browser wrapper boilerplate to get real content for embedding."""
    # browser_console/navigate output: remove ~600 chars of wrapper boilerplate
    lower = text[:300].lower()
    if "browser_console" in lower or "browser_navigate" in lower or "untrusted_tool_result" in lower:
        return text[600:] if len(text) > 600 else text
    return text


LABEL_MODEL = os.environ.get("LABEL_MODEL", "qwen2.5:0.5b")
LABEL_PROMPT = (
    "Output only a 3 to 5 word descriptive label for the following text. "
    "Do not use quotes, punctuation, or conversational filler.\n\n"
)

def _llm_topic_label(text: str) -> str:
    """Call qwen2.5:0.5b via Ollama to generate a semantic topic label.
    
    Falls back to _generate_topic_label on any error.
    """
    clean = _clean_content(text)[:2000]
    if not clean.strip():
        return "untitled"
    try:
        r = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": LABEL_MODEL,
                "prompt": LABEL_PROMPT + clean,
                "stream": False,
                "options": {
                    "num_predict": 15,
                    "num_ctx": 512,
                    "temperature": 0.0,
                },
            },
            timeout=30,
        )
        r.raise_for_status()
        label = r.json().get("response", "").strip()
        # Sanitize: strip quotes, collapse whitespace, cap length
        label = re.sub(r'["\']', '', label)
        label = re.sub(r'\s+', ' ', label).strip()
        if label and len(label) >= 3:
            return label[:60]
    except Exception as e:
        print(f"  [LABEL][ERROR] {type(e).__name__}: {e} — falling back to heuristic", flush=True)
    return _generate_topic_label(text)


def _llm_topic_labels_batch(texts: List[str], max_workers: int = 6) -> List[str]:
    """Concurrent batch labeling via qwen2.5:0.5b. Falls back per-item on error."""
    results: List[Optional[str]] = [None] * len(texts)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_llm_topic_label, t): i for i, t in enumerate(texts)}
        for fut in as_completed(futures):
            i = futures[fut]
            try:
                results[i] = fut.result()
            except Exception:
                results[i] = _generate_topic_label(texts[i])
    return [r if r is not None else _generate_topic_label(t) for r, t in zip(results, texts)]


def _generate_topic_label(text):
    """Derive a topic label from actual content words.

    Picks years, known locations/events if present, otherwise the first
    few content words. New domains auto-create new topics — no fixed
    cluster list, no 'other' bucket.
    """
    clean = _clean_content(text)[:2000].lower()
    keywords = []
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


# Old TOPIC_CLUSTERS / TOPIC_VECTORS removed — dynamic content-derived
# topic labels via _generate_topic_label are used everywhere instead.


def generate_strategy(messages: list, outcome: str) -> str:
    """Generate strategy heuristically — no model call needed for simple cases."""
    if outcome not in ("FAILURE", "TRUNCATED"):
        return ""
    text = " ".join(m["content"][:CHUNK_SIZE] for m in messages[:] if m["role"] in ("user", "assistant"))
    # Return structured strategy note for the model to learn from
    if outcome == "FAILURE":
        return f"FAILURE on: {text[:200]}. Retry with different approach."
    return f"TRUNCATED on: {text[:200]}. Content too large — use chunked reading."

# ─── Routing ───────────────────────────────────────────────────

def _calibrate_noise(n_samples: int = 3) -> float:
    """Compute dynamic noise floor by embedding random strings and measuring min FAISS similarity."""
    import random, string as _st
    if not FAISS_OK or not _id_map:
        return 0.20
    scores = []
    for _ in range(n_samples):
        rand = ''.join(random.choices(_st.ascii_lowercase, k=20))
        try:
            vec = embed(rand)
            if vec is not None:
                hits = _cosine_search(vec, 5, 0.0)
                if hits:
                    scores.append(hits[-1][0])  # lowest similarity of top-5
        except Exception:
            pass
    if scores:
        return sum(scores) / len(scores)  # average minimum across samples
    return 0.20

def route_query(query: str, top_k: int = 3, with_scores: bool = False) -> List:
    """FAISS top-k with noise-normalized scores + recency weighting + keyword fallback.
    Dynamic K: adjusts retrieval count based on score spread above noise floor."""
    q_vec = embed(query)
    scored_raw = _cosine_search(q_vec, top_k * 3, 0.0)  # no threshold — normalize instead
    # Normalize: subtract baseline noise, filter negative
    scored = [(s - BASELINE_NOISE, cid) for s, cid in scored_raw if s - BASELINE_NOISE > ROUTE_THRESHOLD]
    
    # Dynamic K: adjust retrieval count based on signal strength
    if scored:
        best_delta = scored[0][0]  # highest noise-adjusted score
        if best_delta > 0.30:
            dynamic_k = min(top_k * 2, 10)  # Strong signal — get more
        elif best_delta > 0.15:
            dynamic_k = top_k  # Moderate signal — default
        elif best_delta > 0.05:
            dynamic_k = max(1, top_k // 2)  # Weak signal — fewer
        else:
            dynamic_k = 0  # Noise-level — inject nothing, don't pollute context
    else:
        dynamic_k = 0  # Nothing above noise floor
    
    if dynamic_k == 0:
        return []
    
    # Hybrid: fill with keyword matches if FAISS is sparse
    hybrid = _hybrid_search(query, dynamic_k, scored)
    if not hybrid:
        return []
    
    # Fetch cycle for all candidates
    cids = [cid for _, cid, _ in hybrid]
    placeholders = ','.join('?' for _ in cids)
    rows = db.execute(
        f"SELECT chunk_id, cycle FROM chunks WHERE chunk_id IN ({placeholders})",
        cids
    ).fetchall()
    cycle_map = {r[0]: r[1] for r in rows}
    current = _current_cycle()
    
    # Fetch grades for trust scoring
    grade_rows = db.execute(
        f"SELECT chunk_id, grade, source FROM chunks WHERE chunk_id IN ({placeholders})",
        cids
    ).fetchall()
    grade_map = {r[0]: r[1] for r in grade_rows}
    source_map = {r[0]: r[2] for r in grade_rows}
    
    SOURCE_W = {"user": 0.4, "page": 0.3, "tool": 0.2, "model": 0.0}
    GRADE_W  = {"A": 0.4, "B": 0.3, "C": 0.1, "D": 0.0, "F": 0.0}
    
    # Combined score: similarity + recency + trust
    def combined(score, cid):
        chunk_cycle = cycle_map.get(cid, current)
        cycle_delta = max(0, current - chunk_cycle)
        norm_age = 1.0 / (1 + cycle_delta)
        gr = grade_map.get(cid, "C")
        src = source_map.get(cid, "unknown")
        sw = SOURCE_W.get(src, 0.0) if not (src or "").startswith("model") else 0.0
        for prefix in ["user", "page", "tool"]:
            if (src or "").startswith(prefix):
                sw = SOURCE_W.get(prefix, 0.0)
                break
        gw = GRADE_W.get(gr.upper() if gr else "C", 0.1)
        trust = (sw + gw) / 2.0
        return score * (0.7 + 0.3 * trust) + norm_age * 0.1  # sim*trust + small recency boost
    
    scored_combined = [(combined(s, cid), cid) for s, cid, _ in hybrid]
    scored_combined.sort(reverse=True)
    return [cid for _, cid in scored_combined[:top_k]]

def get_siblings(chunk_id: str) -> List[str]:
    row = db.execute("SELECT topic_label FROM chunks WHERE chunk_id=?", (chunk_id,)).fetchone()
    if not row:
        return [chunk_id]
    rows = db.execute("SELECT chunk_id FROM chunks WHERE topic_label=?", (row[0],)).fetchall()
    return [r[0] for r in rows]

def get_siblings_batch(chunk_ids: List[str]) -> Dict[str, List[str]]:
    """Batch sibling fetch: one query per topic, not per chunk.
    
    Returns {chunk_id: [sibling_ids...]} — each chunk maps to its full sibling list.
    """
    if not chunk_ids:
        return {}
    placeholders = ",".join("?" for _ in chunk_ids)
    rows = db.execute(
        f"SELECT chunk_id, topic_label FROM chunks WHERE chunk_id IN ({placeholders})",
        chunk_ids
    ).fetchall()
    # Map topic -> chunks
    topics = {}
    for cid, topic in rows:
        topics.setdefault(topic, []).append(cid)
    # Inflate each chunk to its full sibling list (same topic)
    result = {}
    for cid, topic in rows:
        result[cid] = topics[topic]
    return result

def get_strategies(problem_type=None, limit=3):
    rows = db.execute(
        "SELECT strategy_text FROM strategies ORDER BY effective_grade DESC, use_count DESC LIMIT ?",
        (limit,)
    ).fetchall()
    return [r[0] for r in rows]

# ─── Context Injection ─────────────────────────────────────────

# Total prompt char limit — this model crashes above ~4600 chars with injection
MAX_PROMPT_CHARS = 4500  # system + injection + history must stay below this
# Token budget for injected memory. Hard cap to prevent context overflow.
# The model has 32768 ctx total. System prompt + live conversation need room.
MAX_INJECTED_TOKENS = 6000   # ~6K tokens — stay under model CUDA safe-zone

SEARCH_MEMORY_TOOL = {
    "type": "function",
    "function": {
        "name": "search_memory",
        "description": "Search Mneme memory for past conversations, facts, documents, or details. Use when you need more context than the injected memory provides — look up specific topics, API keys, file paths, or conversation details from prior sessions.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for — be specific"},
                "top_k": {"type": "integer", "description": "Number of results (default 5)"}
            },
            "required": ["query"]
        }
    }
}
MAX_SIBLINGS        = 3      # max chunks per topic (was 5 — caps sibling blowup)
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
            f"{m['role']}: {m['content']}"
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

def _trim_chunks_cached(ordered_ids: List[str], max_tokens: int, cache: Dict[str, Optional[dict]]) -> List[str]:
    """Cached variant of _trim_chunks — uses pre-loaded chunks, no per-chunk DB hits."""
    selected = []
    used = 0
    for cid in ordered_ids:
        chunk = cache.get(cid)
        if not chunk:
            continue
        text = "\n".join(
            f"{m['role']}: {m['content']}"
            for m in chunk.get("messages", [])
        )
        if chunk.get("strategy"):
            text += f"\n[strategy: {chunk['strategy']}]"
        
        cost = _estimate_tokens(text)
        if used + cost > max_tokens:
            continue
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
    
    # Expand to siblings with cap — batch query instead of per-chunk
    all_ids = set()
    siblings_map = get_siblings_batch(chunk_ids)
    for cid in chunk_ids:
        all_ids.add(cid)
        siblings = siblings_map.get(cid, [])
        for sib in siblings[:MAX_SIBLINGS]:
            all_ids.add(sib)
    
    # Batch-fetch grades for all candidates — avoids per-chunk SQLite hits during sort
    if all_ids:
        placeholders = ",".join("?" for _ in all_ids)
        grade_rows = db.execute(
            f"SELECT chunk_id, grade FROM chunks WHERE chunk_id IN ({placeholders})",
            list(all_ids)
        ).fetchall()
        _grade_cache = {cid: GRADE_PRIORITY.get(g, GRADE_PRIORITY[DEFAULT_GRADE]) for cid, g in grade_rows}
    else:
        _grade_cache = {}
    
    # Grade-aware ordering (A first, F last)
    ordered = sorted(all_ids, key=lambda c: (-_grade_cache.get(c, 1), c))
    
    # Batch-load all candidate chunks once — reused for trim, text build, and struct_ref scan
    _chunk_cache: Dict[str, Optional[dict]] = {}
    if ordered:
        placeholders = ",".join("?" for _ in ordered)
        rows = db.execute(
            f"SELECT chunk_id, topic_label, messages, thinking, strategy, "
            f"grade, consensus, outcome, problem_type FROM chunks WHERE chunk_id IN ({placeholders})",
            ordered
        ).fetchall()
        for row in rows:
            _chunk_cache[row[0]] = {
                "chunk_id": row[0], "topic_label": row[1],
                "messages": json.loads(row[2]), "thinking": row[3],
                "strategy": row[4], "grade": row[5],
                "consensus": row[6], "outcome": row[7],
                "problem_type": row[8],
            }
    
    # Trim to token budget — preserves high-grade, drops low-grade
    trimmed = _trim_chunks_cached(ordered, MAX_INJECTED_TOKENS, _chunk_cache)
    
    # Build raw chunk text
    parts = []
    ptype = "other"
    for cid in trimmed:
        chunk = _chunk_cache.get(cid)
        if not chunk:
            continue
        ptype = chunk.get("problem_type", "other")
        topic = chunk.get("topic_label", "unknown")
        sid = chunk.get("session_id", "default")
        sid_tag = f" [session:{sid}]" if sid and sid != "default" else ""
        msg_text = f"--- [{cid}]{sid_tag} [G:{chunk.get('grade','?')}] [src:{chunk.get('source','?')}] {chunk.get('created_at','')[:19]} {topic} ---\n"
        # If next sequential chunk exists, hint it
        msg_text += "\n".join(
            f"{m['role']}: {m['content']}"
            for m in chunk.get("messages", [])
        )
        if chunk.get("strategy"):
            msg_text += f"\n[learned strategy: {chunk['strategy']}]"
        # Add next-chunk hint for sequential navigation
        next_seq = f"mem_{int(cid.split('_')[1]) + 1}" if cid.startswith('mem_') else None
        if next_seq:
            msg_text += f"\n[see also: {next_seq}]"
        parts.append(msg_text)
    
    if not parts:
        # No memory chunks — inject strategies as fallback context
        srows = db.execute(
            "SELECT strategy_text FROM strategies ORDER BY effective_grade DESC, use_count DESC LIMIT 3"
        ).fetchall()
        if srows:
            strat_text = "\n\n=== SYSTEM DIRECTIVES (learned from past experience) ===\n"
            for s in srows:
                strat_text += "DIRECTIVE: " + s[0][:200] + "\n"
            return strat_text, ptype
        return "", ptype
    
    # Build memory context
    context = MEMORY_DISCLAIMER + "\n" + "\n---\n".join(parts)
    
    # Scan for structured chunk references
    struct_refs = set()
    for cid in trimmed:
        chunk = _chunk_cache.get(cid)
        if chunk:
            for m in chunk.get("messages", []):
                text = _extract_text(m.get("content", ""))
                found = re.findall(r'\[chunk-[a-f0-9]+:\s*\d+[^\]]*\]', text)
                struct_refs.update(found)
    if struct_refs:
        context += "\n\n--- STORED RAW DATA (retrievable with <<DETAIL>>) ---\n"
        context += "\n".join(f"  {r}" for r in struct_refs)
    
    # Inject strategy directives ABOVE memory — they have higher epistemic weight
    srows = db.execute(
        "SELECT strategy_text FROM strategies "
        "ORDER BY effective_grade DESC, use_count DESC LIMIT 3"
    ).fetchall()
    if srows:
        directives = "\n=== SYSTEM DIRECTIVES (learned from past experience) ===\n"
        for s in srows:
            directives += "DIRECTIVE: " + s[0][:200] + "\n"
        # Strategies go at TOP — above memory, below system prompt
        if _estimate_tokens(directives + context) <= MAX_INJECTED_TOKENS:
            context = directives + "\n" + context
    
    used_tokens = _estimate_tokens(context)
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

    # Include Mneme instructions with injection (skip when MNEME_INJECT_SYSTEM=0)
    if INJECT_SYSTEM != "0":
        context = "=== MNEME INSTRUCTIONS ===\n" + SYSTEM_PROMPT + "\n\n" + context
    return context, ptype

# ─── Staging Buffer ────────────────────────────────────────────

class StagingBuffer:
    def __init__(self):
        self.messages: list = []
        self.last_activity = time.time()
        self.lock = threading.Lock()
    
    def add(self, role: str, content: str, source: str = "unknown", session: str = "default", grade: str = "C"):
        with self.lock:
            # Filter Hermes system-prompt artifacts from memory
            if role == "assistant":
                noise = ["update the skill library", "Be ACTIVE", "Signals to look for", "Review the conversation above", "missed learning opportunity"]
                if any(p in content for p in noise):
                    content = "[filtered: system instruction artifact]"
            self.messages.append({"role": role, "content": content, "source": source, "session": session, "grade": grade})
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

    # Increment save-cycle counter on every flush
    _next_cycle()

    # Classify each message into a topic group
    groups = _topic_split(msgs)
    
    total = 0
    for topic_label, group_msgs in groups:
        n = _archive_group(topic_label, group_msgs)
        total += n
    
    print(f"  [ARCHIVE] {len(groups)} topics, {total} chunks total (cycle={_current_cycle()})", flush=True)
    return total


def _classify_message(msg: dict) -> str:
    """Generate a content-derived topic label for a single message.

    Uses LLM semantic labeling with heuristic fallback. New domains
    auto-create new topics from actual content words — no 'other' bucket.
    """
    text = msg.get("content", "")
    if not text or len(text) < 10:
        return "untitled"
    return _llm_topic_label(text)


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


def _archive_group(topic_label: str, msgs: list) -> int:
    """Archive a topic group, splitting if too large. Returns chunk count."""
    SEMANTIC_ROLES = ("user", "assistant")
    
    # Build embedding text — strip browser wrapper noise for clean vectors
    user_text = " ".join(
        _clean_content(m["content"])[:5000] for m in msgs if m["role"] in SEMANTIC_ROLES
    )
    
    # Determine source from messages — prefer explicit source tags from staging
    source = "unknown"
    for m in msgs:
        if m.get("source") and m["source"] != "unknown":
            source = m["source"]
            break
    if source == "unknown":
        source = _infer_source(msgs)
    
    # If group is small enough, archive as single chunk
    if len(user_text) <= MAX_CHUNK_SIZE:
        descriptive = _llm_topic_label(user_text) if not topic_label or topic_label.startswith("web_content") or topic_label.startswith("other") else topic_label
        return _archive_single_chunk(msgs, user_text, descriptive, source=source)
    
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
            descriptive = _llm_topic_label(current_text) if topic_label.startswith("web_content") or topic_label.startswith("other") else topic_label[:20]
            label = f"{descriptive[:30]}_p{seq_base}"
            _archive_single_chunk(current, current_text, label, source=source)
            total += 1
            seq_base += 1
            current = []
            current_text = ""
        
        current.append(m)
        current_text += " " + frag
    
    # Archive remaining
    if current:
        descriptive = _llm_topic_label(current_text) if topic_label.startswith("web_content") or topic_label.startswith("other") else topic_label[:20]
        label = f"{descriptive[:30]}_p{seq_base}" if total > 0 else (_llm_topic_label(user_text) if topic_label.startswith("web_content") or topic_label.startswith("other") else topic_label)
        _archive_single_chunk(current, current_text, label, source=source)
        total += 1
    
    return total


def _infer_source(msgs: list) -> str:
    """Infer source tag from message list.
    
    Scans for tool outputs, browser_navigate URLs, user/model messages.
    Returns source string like 'page:example.com', 'tool:terminal', 'user', 'model'.
    """
    # Check for browser_navigate in any message (most specific)
    for m in msgs:
        content = m.get("content", "")
        if not isinstance(content, str):
            continue
        # Look for browser_navigate tool call or URL patterns
        if "browser_navigate" in content[:500] or "browser_console" in content[:500]:
            # Try to extract domain from URL
            urls = re.findall(r'https?://(?:www\.)?([^/\s]+)', content)
            if urls:
                return f"page:{urls[0]}"
            return "page:unknown"
    
    # Check for tool outputs
    for m in msgs:
        role = m.get("role", "")
        if role == "tool":
            # Try to find tool name from content or context
            content = m.get("content", "")
            if isinstance(content, str):
                # Look for common tool signatures
                for tool in ("browser_console", "browser_navigate", "terminal", "search", "web_search", "read_file", "write_file"):
                    if tool in content[:200]:
                        return f"tool:{tool}"
            return "tool:unknown"
    
    # Check roles present
    roles = {m.get("role", "") for m in msgs}
    if "user" in roles and "assistant" in roles:
        return "conversation"
    elif "user" in roles:
        return "user"
    elif "assistant" in roles:
        return "model"
    
    return "unknown"


def _archive_single_chunk(msgs: list, user_text: str, topic_label: str, source: str = "unknown") -> int:
    """Archive one chunk. Returns 1 on success."""
    # Determine outcome and problem type heuristically
    full_text = " ".join(m["content"][:200] for m in msgs if m["role"] in ("user", "assistant"))
    lower = full_text.lower()
    
    session_id = "default"
    chunk_grade = "C"
    print(f"  [ARCHIVE-DEBUG] extracting grade from {len(msgs)} msgs", flush=True)
    for m in msgs:
        sid = m.get("session", "")
        if sid and sid != "default":
            session_id = sid
        g = m.get("grade", "")
        if g and g in ("A","B","C","D","F"):
            chunk_grade = g
    print(f"  [ARCHIVE-DEBUG] final chunk_grade={chunk_grade}", flush=True)
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
    # Temporal stamping: prepend date to embedding text so FAISS can
    # distinguish temporally distinct facts (e.g., "favorite model is X"
    # on Monday vs "favorite model is Y" on Friday). Display text unchanged.
    date_prefix = datetime.now(timezone.utc).strftime("[%Y-%m-%d] ")
    vec = embed(date_prefix + user_text)
    
    row = db.execute("SELECT COUNT(*) FROM chunks WHERE topic_label=?", (topic_label,)).fetchone()
    seq = (row[0] if row else 0) + 1
    global _chunk_seq
    with _chunk_seq_lock:
        _chunk_seq += 1
        chunk_id = f"mem_{int(time.time()*1000000)}"
    # topic_label and seq still in DB for search
    
    # Pass generated sequential chunk_id through to save_chunk
    save_chunk(chunk_id, topic_label, msgs, vec, strategy=strategy, session_id=session_id, grade=chunk_grade,
               outcome=outcome, problem_type=ptype, source=source)
    
    if strategy and ptype != "other":
        sid = f"strat_{ptype}_{seq}_{int(time.time())}"
        # Check for existing similar strategy (semantic dedup)
        existing_version = 0
        try:
            svec_check = embed(strategy)
            if svec_check is not None and FAISS_OK:
                strat_hits = _cosine_search(svec_check, 1, 0.85)
                for _, cid in strat_hits:
                    if cid.startswith("strat_"):
                        ex = db.execute("SELECT strategy_id, version FROM strategies WHERE strategy_id=?",
                            (cid.replace("strat_", "", 1),)).fetchone()
                        if ex:
                            existing_version = ex[1]
                            sid = ex[0]  # reuse existing ID
                            break
        except Exception:
            pass
        
        new_version = existing_version + 1
        db.execute(
            "INSERT OR REPLACE INTO strategies VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (sid, ptype, strategy, chunk_id, "B",
             datetime.now(timezone.utc).isoformat(),
             new_version, sid if existing_version > 0 else "",
             0.0, 0, 0)
        )
        db.commit()
        print(f"  [STRATEGY] v{new_version} {strategy[:60]}...", flush=True)
        # Embed into FAISS for retrieval
        try:
            svec2 = embed(strategy)
            if svec2 is not None and FAISS_OK:
                with faiss_lock():
                    _load_index_from_disk()
                    if _index is not None:
                        _index.add(svec2.reshape(1, -1))
                    _id_map.append(f"strat_{sid}")
                    _save_index()
        except Exception:
            pass
    
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
    """Store raw tool output via unified staging → archive → chunks pipeline.

    Returns a short reference string to replace the content in messages.
    """
    size = len(tool_output)

    # Stage for unified ingestion — will be archived into chunks table
    staging.add("assistant", tool_output, source=f"tool:{tool_name}")

    reference = f"[Tool output staged as tool:{tool_name}: {size:,} chars — will be archived to memory]"
    print(f"  [CHUNK] Staged {tool_name} output ({size:,} chars) for unified ingestion", flush=True)

    return reference


def get_tool_chunk(chunk_id: str) -> Optional[str]:
    """Retrieve a stored chunk by ID from the unified chunks table.
    
    Repointed from tool_output_chunks to chunks table.
    """
    chunk = load_chunk(chunk_id)
    if not chunk:
        return None
    parts = []
    for m in chunk.get("messages", []):
        parts.append(m.get("content", ""))
    return "\n".join(parts) if parts else None


def compress_large_tool_results(messages: list) -> list:
    """Silently stage large tool outputs for archival.
    
    Splits outputs > COMPRESS_THRESHOLD chars into chunks and adds each
    to the staging buffer. Model sees unchanged output — no chunk markers,
    no "continue" loops. Useful flag prevents repeated staging of same content.
    
    Source auto-tagging: scans messages for last browser_navigate call,
    extracts domain from URL, tags staged content as page:{domain}.
    """
    _staged_hashes = getattr(compress_large_tool_results, '_staged_hashes', set())
    
    # Scan for last browser_navigate to determine page source
    page_source = None
    for msg in reversed(messages):
        if msg.get("role") == "tool":
            content = msg.get("content", "")
            if isinstance(content, str) and "browser_navigate" in content[:500]:
                urls = re.findall(r'https?://(?:www\.)?([^/\s]+)', content)
                if urls:
                    page_source = f"page:{urls[0]}"
                break
    
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
        
        # Determine source for this tool output
        tool_source = page_source or "tool:unknown"
        if not page_source:
            # Try to identify tool from content
            for tool in ("browser_console", "browser_navigate", "terminal", "search", "web_search", "read_file", "write_file"):
                if tool in content[:200]:
                    tool_source = f"tool:{tool}"
                    break
        
        # Split into chunks and stage each with source metadata
        for i in range(0, len(content), CHUNK_SIZE):
            chunk = content[i:i+CHUNK_SIZE]
            staging.add("assistant", chunk, source=tool_source)
        
        print(f"  [STAGE] {len(content)} chars split into {(len(content)-1)//CHUNK_SIZE+1} chunks (source={tool_source})", flush=True)
        
        # Trigger archive if buffer has substantial content
        if staging.should_flush():
            import threading
            threading.Thread(target=archive_staging, daemon=True).start()
            print(f"  [STAGE] Auto-flushed staging buffer", flush=True)
    
    compress_large_tool_results._staged_hashes = _staged_hashes
    return messages  # unchanged — model sees full tool output


# ─── ORIGINAL CHUNKING (disabled) ───

def _advance_chunk(messages: list) -> list:
    return messages  # CHUNKING DISABLED


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


# Regex for <<DETAIL id:chunk_id>> syntax
_detail_re = re.compile(r"<<DETAIL\s+id:([^>]+)>>", re.IGNORECASE)

def process_chat(messages: list, session_id: str = "default", tools: list = None) -> dict:
    # Extract query from ALL recent user messages — not just the last one.
    # Multi-turn context is captured so "also the earthquake" finds earthquake
    # chunks alongside Ebola chunks from earlier in the conversation.
    user_msgs = [_extract_text(m["content"])[:500] for m in reversed(messages) 
                 if m.get("role") == "user"][:3]  # last 3 user turns
    user_msg = " ".join(reversed(user_msgs))  # chronological order
    
    # ── Detail: load full chunk if DETAIL tag found ──
    # Scan last message regardless of role (model may output DETAIL in response)
    last_msg = _extract_text(messages[-1].get("content", "")) if messages else ""
    detail_match = _detail_re.search(last_msg)
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

    # Strip all <<COMMANDS>> from user messages
    _cmd_re = re.compile(r"<<[A-Z_]+(?:\s+[^>]+)?>>")
    for m in messages:
        if m.get("role") == "user":
            raw = _extract_text(m.get("content", ""))
            cleaned = _cmd_re.sub("", raw).strip()
            if cleaned: m["content"] = cleaned
    user_msgs2 = [_extract_text(m["content"])[:500] for m in reversed(messages) if m.get("role") == "user"][:3]
    user_msg = " ".join(reversed(user_msgs2))

    msg_tools = tools if tools else []
    # Always include search_memory tool — never let Hermes tools override it
    pass  # SEARCH_MEMORY_TOOL handled by harness
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
    
    # Build injected memory (already includes Mneme instructions)
    context, ptype = build_context(user_msg)
    
    # Insert Mneme (instructions + memory) as a system message after Hermes
    mneme_system = context
    insert_at = 0
    for i, m in enumerate(messages):
        if m.get("role") == "system":
            insert_at = i + 1
            break
    messages.insert(insert_at, {"role": "system", "content": mneme_system})
    full_msgs = messages
    
    # If chunks are pending, loop internally until all consumed
    # DEBUG
    with open("/workspace/sys_dump.txt","w") as f:
        for m in full_msgs:
            if m.get("role") == "system":
                f.write("=== SYSTEM MSG ===\n")
                f.write(m["content"][:600])
                f.write("\n...\n")
    result = query_model(full_msgs, tools=msg_tools)
    
    # Handle search_memory tool calls — execute and inject results
    if result.get("tool_calls") and not result.get("content"):
        for tc in result["tool_calls"]:
            fn = tc.get("function", {})
            if fn.get("name") == "search_memory":
                q = fn.get("arguments", {}).get("query", "")
                k = fn.get("arguments", {}).get("top_k", 5)
                print(f"  [SEARCH-TOOL] model searching: '{q[:80]}' top_k={k}", flush=True)
                hits = route_query(q, top_k=k)
                if hits:
                    lines = ["Search results from Mneme memory:\n"]
                    for h in hits:
                        cid = h[1]
                        crow = db.execute("SELECT topic_label, grade, messages FROM chunks WHERE chunk_id=?", (cid,)).fetchone()
                        if crow:
                            label, grd, msgs_json = crow[0], crow[1], crow[2]
                            lines.append(f"[{cid} | G:{grd}] {label}")
                            try:
                                msgs = json.loads(msgs_json)
                                for m in msgs[:5]:
                                    c = m.get("content", "")[:300]
                                    if c:
                                        lines.append(f"  {m['role']}: {c}")
                            except:
                                pass
                        lines.append("")
                    inject = "\n".join(lines[:30])  # cap
                    result["content"] = inject
                    print(f"  [SEARCH-TOOL] injected {len(hits)} results ({len(inject)} chars)", flush=True)
                else:
                    result["content"] = "No matching memories found."
                    print("  [SEARCH-TOOL] no results", flush=True)
    
    # Parse [GRADE:] from model output
    grade = "C"
    if result["content"]:
        gm = re.search(r"\[GRADE:\s*([ABCDF])\]", result["content"], re.IGNORECASE)
        if gm:
            grade = gm.group(1).upper()
            print(f"  [GRADE] Model grade: {grade}", flush=True)
    
    staging.add("user", user_msg, source="user", session=session_id)
    if result["content"]:
        staging.add("assistant", result["content"], source="model", session=session_id, grade=grade)
    
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


# ─── Phase 2: Proxy-Driven Strategy Lifecycle ──────────────────

def _save_strategy(text, grade, existing_id=""):
    import time as _t
    sid = "strat_" + str(int(_t.time()))
    new_version = 1
    parent = ""
    try:
        svec = embed(text.strip())
        if svec is not None and FAISS_OK:
            hits = _cosine_search(svec, 1, 0.75)
            for _, cid in hits:
                if cid.startswith("strat_"):
                    ex = db.execute("SELECT strategy_id, version FROM strategies WHERE strategy_id=?", (cid.replace("strat_", "", 1),)).fetchone()
                    if ex:
                        sid = ex[0]; new_version = ex[1] + 1; parent = sid
                        break
    except: pass
    if existing_id and "strat_" in str(existing_id) and not parent:
        clean_id = str(existing_id).replace("strat_", "").strip()
        ex = db.execute("SELECT strategy_id, version FROM strategies WHERE strategy_id=?", (clean_id,)).fetchone()
        if ex: sid = ex[0]; new_version = ex[1] + 1; parent = sid
    db.execute("INSERT OR REPLACE INTO strategies VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (sid, "model", text.strip(), "", grade, datetime.now(timezone.utc).isoformat(),
         new_version, parent, 0.0, 0, 0))
    db.commit()
    try:
        svec = embed(text.strip())
        if svec is not None and FAISS_OK:
            with faiss_lock():
                _load_index_from_disk()
                if _index is not None:
                    _index.add(svec.reshape(1, -1))
                _id_map.append(f"strat_{sid}")
                _save_index()
    except: pass

def _strategy_lifecycle(grade, messages):
    try:
        if grade in ("A", "B"):
            q1 = [{"role": "user", "content": "You graded this response " + grade + ". Did you use a novel approach worth saving? Answer yes or no."}]
            r1 = query_model(q1)
            if "yes" not in (r1.get("content","") or "").strip().lower(): return
            q2 = [{"role": "user", "content": "If this improves an existing strategy state the strategy ID (strat_XXX). If new, say new. One word only."}]
            r2 = query_model(q2)
            q3 = [{"role": "user", "content": "Describe the approach in 2-3 sentences."}]
            r3 = query_model(q3)
            if r3.get("content"): _save_strategy(r3["content"].strip(), grade, r2.get("content","").strip())
        elif grade in ("C", "D", "F"):
            # Extract an imperative directive instead of boilerplate
            try:
                msgs_text = "\n".join(
                    f"{m['role']}: {_extract_text(m.get('content',''))[:400]}"
                    for m in messages[-6:] if m.get('role') in ('user', 'assistant')
                )
                q = [{"role": "user", "content": (
                    "You graded a response " + grade + ". Based on this exchange:\n\n" +
                    msgs_text[:2000] + "\n\n" +
                    "Extract ONE imperative rule that would have prevented this failure. "
                    "The rule MUST be: short (1 sentence), specific, and actionable. "
                    "Format as a direct command. NO explanation, NO context — just the rule.\n\n"
                    "Good examples:\n"
                    "- ALWAYS verify the container IP before routing ports.\n"
                    "- NEVER trust model-generated file paths without checking with ls first.\n"
                    "- WHEN the user asks about configuration, search memory before answering.\n\n"
                    "Bad examples:\n"
                    "- I should have checked the IP first (not imperative)\n"
                    "- The failure was caused by... (descriptive, not prescriptive)\n\n"
                    "Respond with ONLY the rule, nothing else."
                )}]
                r = query_model(q)
                if r.get("content"):
                    directive = r["content"].strip()[:300]
                    # Strip common prefixes the model might add
                    for prefix in ("RULE:", "Rule:", "rule:", "- ", "• ", "* "):
                        if directive.startswith(prefix):
                            directive = directive[len(prefix):].strip()
                    if len(directive) > 10:  # Sanity check
                        _save_strategy(directive, grade)
                        print(f"  [STRATEGY-DIRECTIVE] {directive[:80]}...", flush=True)
            except Exception as e:
                print(f"  [STRATEGY-DIRECTIVE][ERR] {str(e)[:100]}", flush=True)
    except Exception as e:
        print(f"  [STRATEGY][ERR] {str(e)[:100]}", flush=True)


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
        
        # Auto-generate session ID for new conversations
        user_count = sum(1 for m in messages if m.get("role") == "user")
        if user_count <= 1:
            # New conversation — generate unique session
            import hashlib
            first_msg = _extract_text(next((m.get("content","") for m in messages if m.get("role") == "user"), ""))
            h = hashlib.md5(first_msg[:100].encode()).hexdigest()[:8]
        session_id = f"conv_{h}_{int(time.time()) % 100000}" if user_count <= 1 else "default"
        
        if stream:
            return _chat_stream(messages, tools=data.get("tools"), session_id=session_id)
        
        result = process_chat(messages, tools=data.get("tools"), session_id=session_id)

        # Parse [GRADE:] and [STRATEGY:] from model output
        ct = result.get("content", "")
        gm = re.search(r"\[GRADE:\s*([ABCDF])\]", ct, re.IGNORECASE)
        grade = gm.group(1).upper() if gm else "D"  # D = unverified default
        # Grade already parsed in process_chat
        
        sm = re.search(r"STRATEGY:\s*(.+?)(?:\]|$)", ct, re.MULTILINE)
        if sm:
            try:
                st = sm.group(1).strip()
                sid = "strat_" + str(int(time.time()))
                existing_version = 0
                # Semantic dedup: check FAISS for similar strategies
                try:
                    svec = embed(st)
                    if svec is not None and FAISS_OK:
                        hits = _cosine_search(svec, 1, 0.75)
                        for _, cid in hits:
                            if cid.startswith("strat_"):
                                ex = db.execute("SELECT strategy_id, version FROM strategies WHERE strategy_id=?", (cid.replace("strat_", "", 1),)).fetchone()
                                if ex:
                                    existing_version = ex[1]
                                    sid = ex[0]
                                    break
                except Exception:
                    pass
                new_version = existing_version + 1
                db.execute("INSERT OR REPLACE INTO strategies VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (sid, "model", st, "", "A",
                     datetime.now(timezone.utc).isoformat(),
                     new_version, sid if existing_version > 0 else "",
                     0.0, 0, 0))
                db.commit()
                print(f"  [STRATEGY] v{new_version} {st[:60]}...", flush=True)
                # Add to FAISS for future dedup
                try:
                    svec2 = embed(st)
                    if svec2 is not None and FAISS_OK:
                        with faiss_lock():
                            _load_index_from_disk()
                            if _index is not None:
                                _index.add(svec2.reshape(1, -1))
                            _id_map.append(f"strat_{sid}")
                            _save_index()
                except Exception:
                    pass
            except Exception as e:
                print("  [STRATEGY][ERR] " + str(e)[:100], flush=True)
        
        print(f"  [GRADE] Parsed: {grade}", flush=True)
        
        # Update effectiveness of strategies referenced in this response
        try:
            # Find strategy IDs mentioned in response
            import re as _sre2
            refs = _sre2.findall(r'STRATEGY #([\w]+)', ct)
            for ref_id in refs:
                sid = f"strat_{ref_id}"
                row = db.execute(
                    "SELECT effective_grade, use_count, success_count FROM strategies WHERE strategy_id LIKE ?",
                    (f"%{ref_id}%",)
                ).fetchone()
                if row:
                    old_eg = row[0] or 0.0
                    uc = (row[1] or 0) + 1
                    sc = (row[2] or 0) + (1 if grade in ("A", "B") else 0)
                    grade_val = {"A": 1.0, "B": 0.75, "C": 0.5, "D": 0.25, "F": 0.0}.get(grade, 0.5)
                    new_eg = old_eg * 0.7 + grade_val * 0.3
                    db.execute(
                        "UPDATE strategies SET effective_grade=?, use_count=?, success_count=? WHERE strategy_id LIKE ?",
                        (new_eg, uc, sc, f"%{ref_id}%")
                    )
                    db.commit()
                    print(f"  [STRATEGY-EFF] #{ref_id} eff={new_eg:.2f} used={uc} success={sc}", flush=True)
        except Exception:
            pass

        # /v1/ prefix = OpenAI format (provider: custom)
        # bare = Ollama format (provider: ollama)
        resp = None
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
            resp = _cors_response({
                "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
                "session_id": session_id,
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
            return resp
        else:
            msg = {"role": "assistant", "content": result.get("content", "")}
            if result.get("thinking"):
                msg["thinking"] = result["thinking"]
            if result.get("tool_calls"):
                msg["tool_calls"] = result["tool_calls"]
            resp = _cors_response({
                "model": FAKE_MODEL_ID,
                "session_id": session_id,
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
    def _chat_stream(messages, tools=None, session_id="default"):
        result = process_chat(messages, tools=tools, session_id=session_id)
        ct = result.get("content", "")
        gm = re.search(r"\[GRADE:\s*([ABCDF])\]", ct, re.IGNORECASE)
        grade = gm.group(1).upper() if gm else "D"
        threading.Thread(target=_strategy_lifecycle, args=(grade, messages), daemon=True).start()
        content = result.get("content", "")
        tool_calls = result.get("tool_calls", [])

        # Handle search_memory server-side for streaming clients
        if tool_calls and not content:
            has_search = any(
                tc.get("function", {}).get("name") == "search_memory"
                for tc in tool_calls
            )
            if has_search:
                # Execute search_memory, inject results, re-query
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    if fn.get("name") == "search_memory":
                        q = fn.get("arguments", {}).get("query", "")
                        k = fn.get("arguments", {}).get("top_k", 5)
                        hits = route_query(q, top_k=k)
                        if hits:
                            lines = ["Search results:\n"]
                            for h in hits:
                                cid = h[1]
                                crow = db.execute(
                                    "SELECT topic_label, grade, messages FROM chunks WHERE chunk_id=?",
                                    (cid,)
                                ).fetchone()
                                if crow:
                                    label, grd, msgs_json = crow
                                    lines.append(f"[{cid} | G:{grd}] {label}")
                            result["content"] = "\n".join(lines)
                            print(f"  [STREAM-SEARCH] {len(hits)} results for '{q[:60]}'", flush=True)
                        else:
                            result["content"] = "No matching memories found."
                content = result["content"]
                tool_calls = []  # Don't send to client
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
    @app.route("/detail/<chunk_id>", methods=["GET"])
    def detail_chunk(chunk_id):
        chunk = load_chunk(chunk_id)
        if not chunk:
            return _cors_response({"error": "not found"}, status=404)
        parts = []
        for m in chunk.get("messages", []):
            r = m["role"]
            content = m["content"][:DB_MSG_CAP]
            parts.append({"role": r, "content": content})
        return _cors_response({
            "chunk_id": chunk.get("chunk_id"),
            "topic_label": chunk.get("topic_label"),
            "source": chunk.get("source", "unknown"),
            "cycle": chunk.get("cycle", 0),
            "messages": parts,
        })


    @app.route("/health", methods=["GET"])
    def health():
        return _cors_response({
            "status": "ok",
            "model": FAKE_MODEL_ID,
            "backend": MODEL,
            "chunks": len(_id_map),
        })

    # ── Save: force-flush the staging buffer ──
    @app.route("/search", methods=["POST"])
    def search_memory():
        data = request.get_json(force=True)
        query = data.get("query", "")
        top_k = data.get("top_k", 10)
        vec = embed(query)
        results_raw = _cosine_search(vec, top_k, 0.0)
        faiss_results = [(s - BASELINE_NOISE, cid) for s, cid in results_raw if s - BASELINE_NOISE > ROUTE_THRESHOLD]
        # Hybrid: fill with keyword matches if FAISS is sparse
        hybrid = _hybrid_search(query, top_k, faiss_results)
        chunks = []
        for score, chunk_id, method in hybrid:
            row = db.execute("SELECT topic_label, grade, created_at, outcome, source, session_id, cycle FROM chunks WHERE chunk_id=?", (chunk_id,)).fetchone()
            if row:
                entry = {"chunk_id": chunk_id, "topic_label": row[0], "grade": row[1], "created_at": row[2], "outcome": row[3], "source": row[4], "cycle": row[5], "similarity": round(score, 4), "method": method}
                chunks.append(entry)
        return _cors_response({"results": chunks})


    @app.route("/list", methods=["GET"])
    def list_chunks():
        rows = db.execute("SELECT chunk_id, topic_label, grade, created_at, LENGTH(messages) as size FROM chunks ORDER BY created_at DESC LIMIT 50").fetchall()
        chunks = [{"chunk_id": r[0], "topic_label": r[1], "grade": r[2], "created_at": r[3], "size_chars": r[4]} for r in rows]
        return _cors_response({"chunks": chunks, "total": len(chunks)})


    @app.route("/save", methods=["POST"])
    def save():
        try:
            n = archive_staging()
            return _cors_response({"saved": True, "chunks": n})
        except Exception as e:
            print(f"  [SAVE][ERROR] {e}", flush=True)
            return _cors_response({"saved": False, "error": str(e)}, status=500)
    
    # ── Learning Mode ──────────────────────────────────────────
    
    @app.route("/mode/learn", methods=["POST"])
    def mode_learn():
        """Proxy-driven learning mode: parameter cycling + strategy extraction.
        POST body: {problem, iterations?, params?}
        Cycles through parameter sets, grades at standard temp, extracts strategies."""
        data = request.get_json(force=True)
        problem = data.get("problem", "")
        iterations = min(data.get("iterations", 5), 10)
        custom_params = data.get("params", None)
        
        if not problem:
            return _cors_response({"error": "problem required"}, status=400)
        
        # Default parameter sets for exploration
        default_params = [
            {"temperature": 0.3, "top_p": 0.5},
            {"temperature": 0.7, "top_p": 0.9},
            {"temperature": 1.2, "top_p": 0.95},
            {"temperature": 1.5, "top_k": 20},
            {"mirostat": 2, "mirostat_tau": 8.0},
        ]
        param_sets = custom_params or default_params
        
        results = []
        strategies = []
        
        for i in range(iterations):
            params = param_sets[i % len(param_sets)]
            print(f"  [LEARN] iteration {i+1}/{iterations} params={params}", flush=True)
            
            # Build prompt for this iteration
            if i == 0:
                prompt = f"Solve or analyze: {problem}\n\nConsider approaches that are NON-OBVIOUS. What would someone who disagrees with the conventional answer propose?"
            else:
                prev = results[-1].get("content", "")[:300]
                prompt = f"Previous approach: {prev}\n\nWhat ASSUMPTIONS did it make? Can you find a solution that doesn't rely on those assumptions? Problem: {problem}"
            
            msgs = [{"role": "user", "content": prompt}]
            
            # Query with varied parameters
            result = query_model(msgs, options=params)
            
            # Grade at standard temp (0.7) — always use same temp for fair comparison
            grade_msgs = [{"role": "user", "content": (
                f"Grade this answer [A-F] based on correctness, novelty, and whether it "
                f"found an approach the obvious answer misses.\n\nANSWER: {result.get('content', '')[:1000]}\n\n"
                f"Respond with ONLY: [GRADE: A/B/C/D/F]"
            )}]
            grade_result = query_model(grade_msgs)
            grade_text = grade_result.get("content", "")
            gm = re.search(r"\[GRADE:\s*([ABCDF])\]", grade_text, re.IGNORECASE)
            grade = gm.group(1).upper() if gm else "C"
            
            iteration = {
                "iteration": i + 1,
                "params": params,
                "content": result.get("content", "")[:2000],
                "grade": grade,
            }
            results.append(iteration)
            
            if grade in ("A", "B"):
                # Extract strategy from good responses
                strat_msgs = [{"role": "user", "content": (
                    f"Extract 1-3 operational STRATEGIES from this {grade}-grade answer. "
                    f"Format each as: [STRATEGY: one-sentence imperative rule]\n\n"
                    f"ANSWER: {result.get('content', '')[:1500]}"
                )}]
                strat_result = query_model(strat_msgs)
                for sm in re.finditer(r"STRATEGY:\s*(.+?)(?:\]|$)", strat_result.get("content", ""), re.MULTILINE):
                    s_text = sm.group(1).strip()[:300]
                    if len(s_text) > 10:
                        strategies.append(s_text)
                        _save_strategy(s_text, grade)
                        print(f"  [LEARN-STRATEGY] {s_text[:80]}...", flush=True)
        
        # Synthesis: extract final strategies from all A-grade responses
        if any(r["grade"] in ("A", "B") for r in results):
            best = [r["content"][:500] for r in results if r["grade"] in ("A", "B")]
            synth_msgs = [{"role": "user", "content": (
                f"Here are the best solutions to: {problem}\n\n" +
                "\n---\n".join(best[:3]) +
                "\n\nExtract 1-3 operational SYSTEM RULES. Format each as: RULE: <imperative instruction>"
            )}]
            synth_result = query_model(synth_msgs)
            for rm in re.finditer(r"RULE:\s*(.+?)(?:\n|$)", synth_result.get("content", "")):
                rule_text = rm.group(1).strip()[:300]
                if len(rule_text) > 10:
                    strategies.append(f"RULE: {rule_text}")
        
        return _cors_response({
            "problem": problem,
            "iterations": results,
            "strategies": list(dict.fromkeys(strategies))[-5:],  # deduplicated, last 5
        })

# ─── Startup ───────────────────────────────────────────────────

_load_index()
_seed_chunk_seq()
# Calibrate noise baseline AFTER FAISS is loaded
BASELINE_NOISE = _calibrate_noise()
print(f"  [STARTUP] Noise baseline: {BASELINE_NOISE:.4f}", flush=True)
print(f"[mokv] Mneme ready. model={MODEL} chunks={len(_id_map)} db={DB_PATH}",
      flush=True)

if __name__ == "__main__":
    if FLASK_OK:
        app.run(host="0.0.0.0", port=8080, threaded=True)
    else:
        print("[mokv] Flask not installed. Import as module for programmatic use.",
              flush=True)
