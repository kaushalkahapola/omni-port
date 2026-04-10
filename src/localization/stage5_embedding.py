import torch
import faiss
from transformers import RobertaTokenizer, RobertaModel
from typing import Optional, Dict, Any
from src.core.state import LocalizationResult

# Lazy-load to avoid long initialization unless Stage 5 is hit
_tokenizer = None
_model = None

def get_unixcoder_model():
    global _tokenizer, _model
    if _tokenizer is None:
        try:
            _tokenizer = RobertaTokenizer.from_pretrained("microsoft/unixcoder-base")
            _model = RobertaModel.from_pretrained("microsoft/unixcoder-base")
            _model.eval()
        except Exception:
            return None, None
    return _tokenizer, _model

def run_embedding_localization(repo_path: str, file_path: str, hunk: Dict[str, Any]) -> Optional[LocalizationResult]:
    """
    Stage 5: Embedding-based semantic search (LLM Fallback)
    Uses UniXcoder (stored in FAISS index) to embed code chunks.
    Resolves TYPE V patches.
    """
    try:
        with open(f"{repo_path}/{file_path}", 'r') as f:
            lines = f.readlines()
    except FileNotFoundError:
        return None

    old_content = hunk.get("old_content", "").strip()
    if not old_content:
        return None

    tokenizer, model = get_unixcoder_model()
    if not tokenizer or not model:
        return None # Failed to load embedding models

    # Chunk target file (e.g., 20-line sliding window)
    window_size = max(10, len(old_content.splitlines()))
    chunks = []
    chunk_start_lines = []
    
    # Slide by half the window size for overlap
    stride = max(1, window_size // 2)
    for i in range(0, max(1, len(lines) - window_size + 1), stride):
        chunk_text = "".join(lines[i:i+window_size])
        chunks.append(chunk_text)
        chunk_start_lines.append(i + 1)

    if not chunks:
        return None

    # Encode the target hunk
    tokens_old = tokenizer(old_content, padding=True, truncation=True, max_length=512, return_tensors="pt")
    with torch.no_grad():
        emb_old = model(**tokens_old).pooler_output.numpy()

    # Encode target file chunks
    tokens_chunks = tokenizer(chunks, padding=True, truncation=True, max_length=512, return_tensors="pt")
    with torch.no_grad():
        emb_chunks = model(**tokens_chunks).pooler_output.numpy()

    # Normalize embeddings for cosine similarity via L2
    faiss.normalize_L2(emb_old)
    faiss.normalize_L2(emb_chunks)

    # Search with FAISS
    d = emb_chunks.shape[1]
    index = faiss.IndexFlatIP(d) # Inner Product for Cosine Similarity
    index.add(emb_chunks)

    # Search top 1
    distances, indices = index.search(emb_old, 1)
    best_idx = indices[0][0]
    distance = distances[0][0] # Since normalized IP, this is cosine similarity [-1, 1]

    confidence = float(distance)
    
    # 0.70 threshold for semantic matching success
    if confidence > 0.70:
        start_line = chunk_start_lines[best_idx]
        return LocalizationResult(
            method_used="embedding",
            confidence=confidence,
            context_snapshot=chunks[best_idx],
            symbol_mappings={},
            file_path=file_path,
            start_line=start_line,
            end_line=start_line + window_size - 1
        )
        
    return None
