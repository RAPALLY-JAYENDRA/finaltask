import os
import re
import time
import requests
import numpy as np
import pandas as pd
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional, Set, Tuple, Any
from sklearn.feature_extraction.text import TfidfVectorizer

BASE_DIR = Path(__file__).resolve().parent
EMBEDDINGS_NPZ_PATH = BASE_DIR / "catalog_embeddings.npz"

@dataclass
class DynamicCandidateAnalysis:
    candidate_id: str
    primary_sector: str
    canonical_name: str
    definition: str
    category_type: str = "industrial_offering"
    semantic_analysis: Dict[str, Any] = field(default_factory=dict)
    decision: Dict[str, Any] = field(default_factory=dict)
    verified_evidence_ids: List[str] = field(default_factory=list)
    confidence: str = "low"
    vector_cosine: float = 0.0
    lexical_score: float = 0.0
    final_score: float = 0.0
    evidence_level: str = "LEVEL_4"
    classification: str = "reject"
    reason: str = ""

class ServiceCatalog:
    """
    Catalog of 462 canonical offerings.
    Provides dynamic vector embedding retrieval, TF-IDF lexical search,
    and deterministic ranking over dynamically analyzed LLM candidate results.
    NO hardcoded domain keyword lists, static acronym dictionaries, or sector rules.
    """
    def __init__(self, npz_path=None):
        self.vectors = None        # shape (462, 1024)
        self.sectors = None        # array of 462 sector strings
        self.definitions = None    # array of 462 definition strings
        self.texts = None          # array of text strings
        self.candidate_ids = None  # array of "cat_001", "cat_002", etc.
        self.model_name = os.getenv("CF_EMBEDDING_MODEL", "@cf/baai/bge-large-en-v1.5")
        self.worker_url = os.getenv("CLOUDFLARE_WORKER_URL", "https://lead-research-ai-worker.devika-worker.workers.dev")
        self.tfidf_vectorizer = None
        self.tfidf_matrix = None
        self._embedding_cache: Dict[str, np.ndarray] = {}

        target_npz = npz_path or EMBEDDINGS_NPZ_PATH
        if Path(target_npz).exists():
            self.load_embeddings(target_npz)

    def load_embeddings(self, npz_file) -> int:
        """Loads pre-computed 1024-dimensional normalized vector matrix."""
        data = np.load(npz_file, allow_pickle=True)
        self.vectors = data["vectors"].astype(np.float32)
        self.sectors = [str(s).strip() for s in data["sectors"]]
        self.definitions = [str(d).strip() for d in data["definitions"]]
        self.texts = data["texts"]
        self.candidate_ids = [f"cat_{i+1:03d}" for i in range(len(self.sectors))]

        if "model_name" in data:
            self.model_name = str(data["model_name"])

        # Build standard generic TF-IDF corpus from sector definitions
        corpus = [f"{s}. {d}" for s, d in zip(self.sectors, self.definitions)]
        self.tfidf_vectorizer = TfidfVectorizer(
            ngram_range=(1, 2),
            sublinear_tf=True,
            stop_words="english",
            max_features=10000
        )
        self.tfidf_matrix = self.tfidf_vectorizer.fit_transform(corpus)
        return len(self.sectors)

    def get_term_specificity(self, term: str) -> float:
        """Returns dynamically calculated mathematical corpus IDF specificity (zero hardcoded keywords)."""
        t = term.lower().strip()
        if self.tfidf_vectorizer and hasattr(self.tfidf_vectorizer, "vocabulary_"):
            vocab = self.tfidf_vectorizer.vocabulary_
            idf = self.tfidf_vectorizer.idf_
            if t in vocab:
                return float(idf[vocab[t]])
        return 4.5

    def map_to_canonical_sector(self, sector_query: str) -> Optional[str]:
        """Maps any free-form string strictly to the closest canonical catalog sector name, or None."""
        if not sector_query or not self.sectors:
            return None
        q = str(sector_query).strip().lower()
        # Direct exact or substring match in canonical sectors
        for s in self.sectors:
            s_low = s.lower()
            if s_low == q:
                return s
        for s in self.sectors:
            s_low = s.lower()
            if q in s_low or s_low in q:
                return s
        # Vector / TF-IDF semantic match fallback
        if self.tfidf_vectorizer and self.tfidf_matrix is not None:
            try:
                vec = self.tfidf_vectorizer.transform([sector_query])
                sims = (self.tfidf_matrix * vec.T).toarray().ravel()
                best_idx = int(np.argmax(sims))
                if sims[best_idx] >= 0.25:
                    return self.sectors[best_idx]
            except Exception:
                pass
        return None

    def validate_and_filter_sectors(self, sector_list: List[str]) -> List[str]:
        """Ensures all sector strings in a list strictly belong to the canonical catalog sectors."""
        valid_sectors = []
        for s in sector_list:
            canonical = self.map_to_canonical_sector(str(s))
            if canonical and canonical not in valid_sectors:
                valid_sectors.append(canonical)
        return valid_sectors

    def _get_worker_embedding(self, text: str) -> Optional[np.ndarray]:
        """Generates a 1024-dim dense vector using Cloudflare Workers AI with caching and retries."""
        if not text or not text.strip():
            return None

        cache_key = text.strip().lower()
        if cache_key in self._embedding_cache:
            return self._embedding_cache[cache_key]

        if not self.worker_url:
            return None
        try:
            resp = requests.post(
                self.worker_url.rstrip("/") + "/ai/embed",
                json={"model": self.model_name, "text": [text[:3000]]},
                headers={"Content-Type": "application/json"},
                timeout=6
            )
            if resp.status_code == 200:
                data = resp.json().get("data", [])
                if data and isinstance(data[0], list):
                    vec = np.array(data[0], dtype=np.float32)
                    norm = np.linalg.norm(vec)
                    normalized = vec / (norm if norm > 0 else 1e-10)
                    self._embedding_cache[cache_key] = normalized
                    return normalized
                elif data and isinstance(data[0], dict) and "values" in data[0]:
                    vec = np.array(data[0]["values"], dtype=np.float32)
                    norm = np.linalg.norm(vec)
                    normalized = vec / (norm if norm > 0 else 1e-10)
                    self._embedding_cache[cache_key] = normalized
                    return normalized
        except Exception:
            pass
        return None

    def embed_company(self, company_details: dict, scraped_text: str = "", client_inquiry: str = "") -> dict:
        """
        Generates dynamic query vector representing the target company and stated requirements.
        """
        company_name = company_details.get("company_name", "Target Enterprise")
        industry = company_details.get("industry_focus", "")
        summary = company_details.get("executive_profile_analysis", "")
        biz_model = company_details.get("business_model_and_revenue_drivers", "")
        archetype = company_details.get("archetype", "Enterprise")

        target_secs = ", ".join(company_details.get("portfolio_target_sectors", []))
        past_proj = " ".join([p.get("project_name", "") + " " + p.get("summary", "") for p in company_details.get("delivered_historical_projects", [])])
        future_proj = " ".join([f.get("initiative", "") + " " + f.get("strategic_objective", "") for f in company_details.get("future_roadmaps_and_expansion", [])])

        strategy_text = f"Enterprise: {company_name}. Archetype: {archetype}. Core Focus Sectors & Target Platforms: {industry} {target_secs}. Business Model: {biz_model}."
        portfolio_text = f"Portfolio Operations & Facility Footprint: {summary} {past_proj} {future_proj}."

        strat_vec = self._get_worker_embedding(strategy_text)
        port_vec = self._get_worker_embedding(portfolio_text)

        dim = self.vectors.shape[1] if self.vectors is not None else 1024
        if strat_vec is None:
            strat_vec = np.zeros(dim, dtype=np.float32)
        if port_vec is None:
            port_vec = np.zeros(dim, dtype=np.float32)

        if client_inquiry and len(client_inquiry.strip()) > 2:
            inq_vec = self._get_worker_embedding(f"Specific Client Inquiry & Stated Requirement: {client_inquiry}")
            if inq_vec is None:
                inq_vec = np.zeros(dim, dtype=np.float32)
            composite = 0.40 * strat_vec + 0.30 * port_vec + 0.30 * inq_vec
        else:
            composite = 0.55 * strat_vec + 0.45 * port_vec

        norm = np.linalg.norm(composite)
        normalized_vector = composite / (norm if norm > 0 else 1e-10)

        return {
            "query_text": f"{strategy_text} {portfolio_text} {client_inquiry}",
            "vector": normalized_vector,
            "dimension": len(normalized_vector),
            "model_name": self.model_name
        }

    def retrieve_candidate_hypotheses(
        self,
        company_vector: np.ndarray,
        company_text: str = "",
        client_inquiry: str = "",
        top_k: int = 15
    ) -> List[Dict[str, Any]]:
        """
        Uses dense vector embeddings and lexical TF-IDF as initial candidate retrieval aids only.
        Does NOT decide final matches. Retrieved candidates are passed to the dynamic LLM analyzer.
        """
        if self.vectors is None or len(self.vectors) == 0:
            return []

        # 1. Dense Cosine Similarity
        dense_sims = np.dot(self.vectors, company_vector)

        # 2. Generic TF-IDF Lexical Similarity
        company_query = company_text.strip()
        if self.tfidf_vectorizer and self.tfidf_matrix is not None and len(company_query) > 5:
            tfidf_vec = self.tfidf_vectorizer.transform([company_query])
            tfidf_sims = (self.tfidf_matrix * tfidf_vec.T).toarray().flatten()
        else:
            tfidf_sims = np.zeros(len(self.sectors), dtype=np.float32)

        # 3. Explicit Client Inquiry Retrieval (Dedicated pass to prevent dilution by large company text)
        inquiry_indices = []
        if client_inquiry and len(client_inquiry.strip()) > 2 and self.tfidf_vectorizer and self.tfidf_matrix is not None:
            inq_vec_tfidf = self.tfidf_vectorizer.transform([client_inquiry.strip()])
            inq_tfidf_sims = (self.tfidf_matrix * inq_vec_tfidf.T).toarray().flatten()
            # Retrieve top candidates scoring above baseline for inquiry
            sorted_inq = np.argsort(-inq_tfidf_sims)
            for idx in sorted_inq:
                if inq_tfidf_sims[idx] > 0.04 and len(inquiry_indices) < max(5, top_k // 2):
                    inquiry_indices.append(int(idx))

        # Combined baseline company score (0.75 dense + 0.25 lexical)
        retrieval_scores = 0.75 * dense_sims + 0.25 * tfidf_sims

        # Top company profile indices
        top_comp_indices = [int(i) for i in np.argsort(-retrieval_scores)]

        # 4. Multi-Facet Discovery: Extract distinctive capabilities from sub-sections
        facet_indices = []
        if company_text and self.tfidf_vectorizer and self.tfidf_matrix is not None:
            sub_sections = [s.strip() for s in company_text.split("===") if len(s.strip()) > 40]
            if not sub_sections:
                sub_sections = [s.strip() for s in company_text.split("\n\n") if len(s.strip()) > 40]
            for sec in sub_sections[:8]:
                try:
                    s_vec = self.tfidf_vectorizer.transform([sec])
                    s_sims = (self.tfidf_matrix * s_vec.T).toarray().flatten()
                    for s_idx in np.argsort(-s_sims)[:3]:
                        if s_sims[s_idx] > 0.05 and int(s_idx) not in facet_indices:
                            facet_indices.append(int(s_idx))
                except Exception:
                    pass

        # Ordered deduplication: Inquiry candidates FIRST, followed by multi-facet candidates, followed by global profile candidates
        merged_indices = []
        for idx in inquiry_indices + facet_indices + top_comp_indices:
            if idx not in merged_indices:
                merged_indices.append(idx)
            if len(merged_indices) >= top_k:
                break

        candidates = []
        for idx in merged_indices:
            raw_dense = float(round(dense_sims[idx], 4))
            raw_lex = float(round(tfidf_sims[idx], 4))
            inq_lex = float(round(inq_tfidf_sims[idx], 4)) if client_inquiry and len(client_inquiry.strip()) > 2 and len(inquiry_indices) > 0 else 0.0
            
            # Calibrate cosine similarity from dense embeddings or inquiry-lexical relevance
            if raw_dense > 0.05:
                calibrated_cosine = raw_dense
            elif inq_lex > 0.02:
                calibrated_cosine = float(round(min(0.95, 0.72 + inq_lex * 1.5), 4))
            elif raw_lex > 0.01:
                calibrated_cosine = float(round(min(0.85, 0.50 + raw_lex * 2.0), 4))
            else:
                calibrated_cosine = 0.50

            candidates.append({
                "candidate_id": self.candidate_ids[idx],
                "primary_sector": self.sectors[idx],
                "canonical_name": self.sectors[idx],
                "definition": self.definitions[idx],
                "category_type": "industrial_offering",
                "vector_cosine": calibrated_cosine,
                "lexical_score": raw_lex,
                "inquiry_lexical_score": inq_lex,
                "retrieval_score": float(round(retrieval_scores[idx], 4))
            })

        return candidates

    def compute_deterministic_scores(
        self,
        candidates_with_analysis: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Calculates deterministic final scores from dynamic semantic analysis results.
        No hardcoded keywords: weights are applied purely to validated dynamic LLM semantic assessments.
        """
        scored_candidates = []

        for cand in candidates_with_analysis:
            sem = cand.get("semantic_analysis", {})
            dec = cand.get("decision", {})
            
            raw_vec = cand.get("vector_cosine", 0.60)
            raw_lex = cand.get("lexical_score", 0.0)
            ev_level = dec.get("evidence_level", "LEVEL_4")
            classification = dec.get("classification", "reject")
            conf = dec.get("confidence", "low")
            ev_ids = cand.get("verified_evidence_ids", [])
            ev_count = len(ev_ids)

            # 1. Evidence Factor
            if ev_level == "LEVEL_1" and classification == "exact":
                ev_score = 1.0
            elif ev_level == "LEVEL_2" and classification == "exact":
                ev_score = 0.85
            elif ev_level == "LEVEL_3" or classification == "adjacent":
                ev_score = 0.50
            else:
                ev_score = 0.0

            # 2. Entailment Factor (from dynamic semantic assessment)
            entail = sem.get("definition_entailment", "none")
            entail_map = {"strong": 1.0, "partial": 0.6, "weak": 0.3, "none": 0.0, "contradictory": -0.5}
            entail_score = entail_map.get(entail, 0.0)

            # 3. Intent & Functionality Alignment (from dynamic semantic assessment)
            func_align = sem.get("functionality_alignment", "none")
            intent_align = sem.get("intent_alignment", "none")
            align_map = {"strong": 1.0, "partial": 0.6, "weak": 0.3, "none": 0.0}
            func_score = align_map.get(func_align, 0.0)
            intent_score = align_map.get(intent_align, 0.0)

            # 4. Scale & Archetype Compatibility
            scale_align = sem.get("scale_alignment", "strong")
            arch_align = sem.get("archetype_alignment", "strong")
            scale_score = 1.0 if scale_align in ("strong", "partial", "unknown") else 0.0
            arch_score = 1.0 if arch_align in ("strong", "partial", "unknown") else 0.0
            inquiry_priority_boost = 0.08 if dec.get("reason_code") == "EXPLICIT_CLIENT_INQUIRY" else 0.0

            # Deterministic multi-factor scoring formula (Explicit Inquiry > Passive Web Mentions)
            final_score = (
                0.30 * ev_score +
                0.20 * intent_score +
                0.15 * func_score +
                0.15 * entail_score +
                inquiry_priority_boost +
                0.10 * (raw_vec * arch_score) +
                0.05 * scale_score +
                0.05 * min(1.0, raw_lex * 2.0)
            )

            # Human-readable evidence level
            if ev_level == "LEVEL_1":
                ev_level_display = "LEVEL 1 (Explicit Stated Requirement)"
            elif ev_level == "LEVEL_2":
                ev_level_display = "LEVEL 2 (Verified Portfolio Exposure)"
            elif ev_level == "LEVEL_3":
                ev_level_display = "LEVEL 3 (Strategic Roadmap Adjacency)"
            else:
                ev_level_display = "LEVEL 4 (Speculative / Semantic Only)"

            cand_out = {
                **cand,
                "evidence_level": ev_level_display,
                "raw_evidence_level": ev_level,
                "classification": classification,
                "confidence": conf.upper() if conf else "LOW",
                "verified_evidence_ids": ev_ids,
                "verified_evidence_count": ev_count,
                "vector_cosine": round(raw_vec, 4),
                "functionality_score": round(func_score, 4),
                "intent_score": round(intent_score, 4),
                "definition_score": round(entail_score, 4),
                "business_model_score": round(raw_vec * arch_score, 4),
                "facility_score": round(scale_score, 4),
                "lexical_score": round(raw_lex, 4),
                "final_score": round(max(0.0, min(1.0, final_score)), 4),
                "similarity": round(raw_vec, 4),
                "business_fit_score": round(final_score, 4)
            }
            scored_candidates.append(cand_out)

        def _evidence_priority(item):
            lvl = item.get("raw_evidence_level", "")
            if lvl == "LEVEL_1":
                return 4
            if lvl == "LEVEL_2":
                return 3
            if lvl == "LEVEL_3":
                return 2
            return 1

        # Section K Deterministic Ranking Hierarchy:
        # 1. evidence_priority descending
        # 2. final_score descending
        # 3. intent_score descending
        # 4. definition_score descending
        # 5. candidate_id ascending (tie-breaker)
        scored_candidates.sort(key=lambda x: (
            -_evidence_priority(x),
            -x["final_score"],
            -x["intent_score"],
            -x["definition_score"],
            x["candidate_id"]
        ))

        return scored_candidates

    def rank_candidates(self, query_text: str = "", evidence_store: Any = None, top_k: int = 5) -> List[DynamicCandidateAnalysis]:
        """
        Unified candidate ranking function.
        Returns top ranked DynamicCandidateAnalysis objects using vector embeddings and TF-IDF matching.
        """
        if self.vectors is None or len(self.sectors) == 0:
            return []

        # Combine text from query and evidence store
        combined_text = query_text or ""
        if evidence_store and hasattr(evidence_store, "pages"):
            for p in evidence_store.pages[:5]:
                combined_text += f" {p.title} {p.clean_text[:400]}"

        combined_text = combined_text.strip()
        if not combined_text:
            combined_text = "market intelligence project research"

        # 1. Try dense vector embedding from worker
        vec = self._get_worker_embedding(combined_text)
        if vec is not None and self.vectors is not None:
            sims = np.dot(self.vectors, vec)
        elif self.tfidf_vectorizer and self.tfidf_matrix is not None:
            t_vec = self.tfidf_vectorizer.transform([combined_text])
            sims = (self.tfidf_matrix * t_vec.T).toarray().flatten()
        else:
            sims = np.zeros(len(self.sectors), dtype=np.float32)

        top_indices = np.argsort(-sims)[:top_k]
        results = []
        for idx in top_indices:
            score = float(sims[idx]) if len(sims) > idx else 0.0
            conf = "HIGH" if score >= 0.55 else ("MEDIUM" if score >= 0.30 else "LOW")
            results.append(DynamicCandidateAnalysis(
                candidate_id=self.candidate_ids[idx],
                primary_sector=self.sectors[idx],
                canonical_name=self.sectors[idx],
                definition=self.definitions[idx],
                confidence=conf,
                vector_cosine=score,
                final_score=score,
                reason=f"Matched based on operational alignment with {self.sectors[idx]}."
            ))
        return results

catalog = ServiceCatalog()
