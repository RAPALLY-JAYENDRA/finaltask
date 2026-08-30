import os
import re
import time
import asyncio
import urllib.parse
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import List, Dict, Optional, Set, Any
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

# Try importing crawl4ai for deep structured markdown crawling
try:
    from crawl4ai import AsyncWebCrawler
    CRAWL4AI_AVAILABLE = True
except Exception:
    CRAWL4AI_AVAILABLE = False

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1"
}

class PageType(str, Enum):
    HOME = "home"
    ABOUT = "about"
    PRODUCTS_SERVICES = "products_services"
    SOLUTIONS = "solutions"
    CASE_STUDY = "case_study"
    NEWS_PRESS = "news_press"
    CAREERS = "careers"
    LEADERSHIP = "leadership"
    CONTACT = "contact"
    LEGAL = "legal"
    OTHER = "other"

@dataclass
class Evidence:
    """Canonical Evidence record storing verifiable quoted text with provenance."""
    evidence_id: str
    source_url: str
    source_title: str
    quoted_text: str
    normalized_text: str
    entity: str
    relationship: str  # portfolio_company | current_operation | historical_investment | stated_focus | business_model | generic_statement
    subject_entity: str = ""
    object_entity: str = ""
    sector_terms: List[str] = field(default_factory=list)
    evidence_type: str = "web_extract"
    is_first_party: bool = True
    publication_date: Optional[str] = None
    temporal_status: str = "current"  # current | historical | unknown
    verification_status: str = "verified"  # verified | unverified | contradicted
    confidence: str = "high"  # high | medium | low

    def to_dict(self) -> dict:
        return asdict(self)

@dataclass
class PageEvidence:
    url: str
    title: str
    page_type: PageType
    headings: List[str]
    clean_text: str
    canonical_snippets: List[str]
    extracted_keywords: List[str]
    credibility_weight: float
    timestamp: str
    status_code: int = 200

@dataclass
class BusinessSignal:
    category: str
    signal: str
    source_url: str
    confidence: str
    snippet: str

@dataclass
class EvidenceStore:
    domain: str
    company_name: str
    base_url: str
    pages: List[PageEvidence] = field(default_factory=list)
    signals: List[BusinessSignal] = field(default_factory=list)
    evidence_ledger: List[Evidence] = field(default_factory=list)
    observed_industries: List[str] = field(default_factory=list)
    observed_products: List[str] = field(default_factory=list)
    observed_technologies: List[str] = field(default_factory=list)
    observed_geographies: List[str] = field(default_factory=list)
    search_insights: List[str] = field(default_factory=list)
    confidence_score: float = 0.0
    confidence_label: str = "low"
    status: str = "insufficient_evidence"

    def add_evidence(
        self,
        source_url: str,
        source_title: str,
        quoted_text: str,
        entity: str = "",
        relationship: str = "current_operation",
        subject_entity: str = "",
        object_entity: str = "",
        sector_terms: Optional[List[str]] = None,
        evidence_type: str = "web_extract",
        is_first_party: bool = True,
        temporal_status: str = "current",
        confidence: str = "high"
    ) -> Optional[Evidence]:
        clean_quote = re.sub(r"\s+", " ", quoted_text).strip()
        if not clean_quote or len(clean_quote) < 25:
            return None

        # Check for duplicates in ledger
        norm_text = clean_quote.lower()
        for existing in self.evidence_ledger:
            if existing.source_url == source_url and existing.normalized_text == norm_text:
                return existing

        ev_id = f"ev_{len(self.evidence_ledger) + 1:03d}"
        ev = Evidence(
            evidence_id=ev_id,
            source_url=source_url,
            source_title=source_title or self.domain,
            quoted_text=clean_quote,
            normalized_text=norm_text,
            entity=entity or self.company_name,
            relationship=relationship,
            subject_entity=subject_entity or self.company_name,
            object_entity=object_entity or "",
            sector_terms=sector_terms or [],
            evidence_type=evidence_type,
            is_first_party=is_first_party,
            temporal_status=temporal_status,
            verification_status="verified",
            confidence=confidence
        )
        self.evidence_ledger.append(ev)
        return ev

    def get_aggregated_text(self, max_chars: int = 12000) -> str:
        """Assembles structured text strictly from harvested evidence."""
        if not self.pages and not self.evidence_ledger:
            return ""

        sections = []
        sorted_pages = sorted(self.pages, key=lambda p: p.credibility_weight, reverse=True)
        for page in sorted_pages:
            headings_str = " | ".join(page.headings[:5]) if page.headings else "N/A"
            sections.append(
                f"=== [{page.page_type.upper()}] {page.title} ({page.url}) ===\n"
                f"Headings: {headings_str}\n"
                f"Content Summary: {page.clean_text[:2500]}\n"
                f"Key Snippets: {' // '.join(page.canonical_snippets[:4])}\n"
            )
        if self.search_insights:
            sections.append("=== THIRD-PARTY VERIFIED PUBLIC KNOWLEDGE & SEARCH INSIGHTS ===\n" + "\n".join(self.search_insights))
        
        # Add Evidence Ledger Summary
        if self.evidence_ledger:
            ledger_lines = [
                f"[{ev.evidence_id}] ({ev.relationship}) \"{ev.quoted_text}\" (Source: {ev.source_url})"
                for ev in self.evidence_ledger[:30]
            ]
            sections.append("=== STRUCTURED EVIDENCE LEDGER ===\n" + "\n".join(ledger_lines))

        full = f"COMPANY: {self.company_name} (Domain: {self.domain})\n\n" + "\n".join(sections)
        return full[:max_chars]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["evidence_ledger"] = [e.to_dict() if hasattr(e, "to_dict") else asdict(e) for e in self.evidence_ledger]
        return d

def classify_page(url: str, title: str, headings: List[str], text_sample: str) -> tuple:
    """Classifies webpage structure based on URL path semantics and header structure."""
    path = urllib.parse.urlparse(url).path.lower().strip("/")
    combined_meta = f"{path} {title} {' '.join(headings)}".lower()

    if not path or path in ["", "index.html", "index.php", "home", "en", "us", "global"]:
        return PageType.HOME, 1.0

    if any(k in combined_meta for k in ["product", "offering", "service", "solution", "capability", "platform", "hardware", "software", "system", "technology", "catalog", "division", "segment"]):
        return PageType.PRODUCTS_SERVICES, 1.4

    if any(k in combined_meta for k in ["case-stud", "case_study", "project", "portfolio", "success-stor", "customer", "installation", "deployment", "client", "work"]):
        return PageType.CASE_STUDY, 1.3

    if any(k in combined_meta for k in ["about", "company", "who-we-are", "history", "mission", "overview", "our-story"]):
        return PageType.ABOUT, 1.1

    if any(k in combined_meta for k in ["press", "news", "announcement", "media", "blog", "insight", "event", "article"]):
        return PageType.NEWS_PRESS, 0.9

    if any(k in combined_meta for k in ["career", "job", "join-us", "hiring", "opening", "culture"]):
        return PageType.CAREERS, 0.8

    if any(k in combined_meta for k in ["leader", "board", "team", "executive", "management", "govern", "director"]):
        return PageType.LEADERSHIP, 0.9

    if any(k in combined_meta for k in ["contact", "get-in-touch", "location", "office", "reach-us"]):
        return PageType.CONTACT, 0.7

    if any(k in combined_meta for k in ["privacy", "terms", "legal", "cookie", "disclaimer", "compliance", "policy"]):
        return PageType.LEGAL, 0.2

    return PageType.OTHER, 0.7

def extract_markdown_evidence(markdown_text: str, url: str) -> dict:
    """Extracts headings, clean text, and factual snippets from Crawl4AI markdown."""
    headings = []
    lines = markdown_text.splitlines()
    for line in lines:
        line_s = line.strip()
        if line_s.startswith(("# ", "## ", "### ")):
            clean_h = re.sub(r"^#+\s*", "", line_s).strip()
            if 3 < len(clean_h) < 100 and clean_h not in headings:
                headings.append(clean_h)

    # Clean markdown formatting for clean text
    clean_text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", markdown_text)
    clean_text = re.sub(r"[#*_`~>-]", " ", clean_text)
    clean_text = re.sub(r"\s+", " ", clean_text).strip()

    sentences = re.split(r"(?<=[.!?])\s+", clean_text)
    canonical_snippets = []
    for sent in sentences:
        clean_s = sent.strip()
        if 35 < len(clean_s) < 320:
            if clean_s not in canonical_snippets:
                canonical_snippets.append(clean_s)
                if len(canonical_snippets) >= 12:
                    break

    title = headings[0] if headings else urllib.parse.urlparse(url).netloc

    return {
        "title": title,
        "headings": headings[:8],
        "clean_text": clean_text,
        "canonical_snippets": canonical_snippets
    }

def clean_html(raw_html: str) -> dict:
    """Fallback HTML cleaner for standard HTTP requests."""
    title_match = re.search(r"<title[^>]*>(.*?)</title>", raw_html, re.I | re.DOTALL)
    title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else "Enterprise Page"
    title = title.replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'")

    headings = []
    for h in re.findall(r"<h[1-3][^>]*>(.*?)</h[1-3]>", raw_html, re.I | re.DOTALL):
        clean_h = re.sub(r"<[^>]+>", "", h).strip()
        clean_h = re.sub(r"\s+", " ", clean_h)
        if 4 < len(clean_h) < 120 and clean_h not in headings:
            headings.append(clean_h)

    text = re.sub(r"<(script|style|nav|header|footer|svg|noscript|iframe)[^>]*>.*?</\1>", " ", raw_html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " ")
    text = re.sub(r"\s+", " ", text).strip()

    sentences = re.split(r"(?<=[.!?])\s+", text)
    canonical_snippets = []

    for sent in sentences:
        clean_s = sent.strip()
        if 35 < len(clean_s) < 320:
            if clean_s not in canonical_snippets:
                canonical_snippets.append(clean_s)
                if len(canonical_snippets) >= 12:
                    break

    return {
        "title": title,
        "headings": headings[:8],
        "clean_text": text,
        "canonical_snippets": canonical_snippets
    }

def extract_links(raw_html: str, base_url: str) -> List[str]:
    """Finds verified internal subpage URLs on the same domain prioritized by path depth."""
    parsed_base = urllib.parse.urlparse(base_url)
    domain = parsed_base.netloc
    
    matches = re.findall(r'href=["\'](.*?)["\']', raw_html, re.I)
    discovered_links = []
    seen = set()

    for m in matches:
        m = m.strip()
        if not m or m.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
            continue
        full_url = urllib.parse.urljoin(base_url, m).split("#")[0].split("?")[0].rstrip("/")
        parsed_m = urllib.parse.urlparse(full_url)
        if parsed_m.netloc == domain and full_url != base_url.rstrip("/") and full_url not in seen:
            seen.add(full_url)
            discovered_links.append(full_url)

    # Sort links by directory depth (shallower and primary sections first)
    discovered_links.sort(key=lambda u: len(urllib.parse.urlparse(u).path.strip("/").split("/")))
    return discovered_links[:15]

def fetch_page_content(url: str, timeout: int = 7) -> tuple:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        if resp.status_code == 200 and len(resp.text) > 200 and "Just a moment..." not in resp.text:
            return url, resp.text, resp.status_code
    except Exception:
        pass
    return url, "", 500

def extract_universal_signals(text: str, url: str) -> List[BusinessSignal]:
    """Universal signal extraction for ANY type of enterprise."""
    signals = []
    seen = set()

    growth_matches = re.findall(r"\b((?:expanding|acquisition of|acquired|invested|partnership with|joint venture with|investment of \$?[0-9]+|commissioned|global expansion)\s+[A-Za-z0-9\$\.\,\s]{3,40})(?:\.|\,|\;|\n)", text, re.I)
    for m in growth_matches[:4]:
        clean_m = re.sub(r"\s+", " ", m).strip()
        if len(clean_m) > 10 and clean_m.lower() not in seen:
            seen.add(clean_m.lower())
            signals.append(BusinessSignal(
                category="growth_and_capex",
                signal=f"Growth / Expansion: {clean_m}",
                source_url=url,
                confidence="high",
                snippet=clean_m
            ))

    scale_matches = re.findall(r"\b(\$?[0-9]+(?:\.[0-9]+)?\s*(?:billion|million|bn|mn|mw|gw|tons|sq\s*ft|employees|countries|facilities|portfolio companies))\b", text, re.I)
    for s in scale_matches[:4]:
        clean_s = s.strip()
        if clean_s.lower() not in seen:
            seen.add(clean_s.lower())
            signals.append(BusinessSignal(
                category="operational_scale",
                signal=f"Operational Scale: {clean_s}",
                source_url=url,
                confidence="high",
                snippet=clean_s
            ))

    return signals

def fetch_search_insights(company_name: str, domain: str) -> List[str]:
    """Retrieves verified third-party search and encyclopedic intelligence snippets."""
    insights = []
    clean_search_name = re.sub(r"\b(inc|llc|corp|ltd|group|investors|holdings)\b", "", company_name, flags=re.I).strip()
    if not clean_search_name:
        clean_search_name = company_name

    try:
        wiki_url = f"https://en.wikipedia.org/w/api.php?action=query&prop=extracts&explaintext=1&titles={urllib.parse.quote(clean_search_name)}&format=json"
        w_resp = requests.get(wiki_url, headers={"User-Agent": "LeadResearchAI/1.0"}, timeout=5)
        if w_resp.status_code == 200:
            pages = w_resp.json().get("query", {}).get("pages", {})
            for _, pdata in pages.items():
                extract = pdata.get("extract", "")
                if extract and len(extract) > 40:
                    lines = [line.strip() for line in extract.split("\n") if len(line.strip()) > 35 and not line.strip().startswith("=")]
                    for l in lines[:40]:
                        insights.append(f"Official Corporate Encyclopedia ({company_name}): {l}")
    except Exception:
        pass

    queries = [
        f'"{company_name}" operations products technology overview',
        f'"{company_name}" facilities business model scale',
        f'"{company_name}" power cooling telecom energy storage solutions',
        f'"{company_name}" solar hybrid renewable infrastructure'
    ]

    for q in queries:
        try:
            duck_url = f"https://api.duckduckgo.com/?q={urllib.parse.quote(q)}&format=json&no_html=1"
            resp = requests.get(duck_url, headers=HEADERS, timeout=3)
            if resp.status_code == 200:
                data = resp.json()
                abstract = data.get("AbstractText", "")
                heading = data.get("Heading", "")
                if abstract and len(abstract) > 40:
                    insights.append(f"Search Intelligence ({heading}): {abstract}")
                for topic in data.get("RelatedTopics", [])[:2]:
                    if isinstance(topic, dict) and topic.get("Text"):
                        insights.append(f"Fact: {topic.get('Text')}")
        except Exception:
            pass

    return insights

async def _crawl4ai_deep_harvest(url: str, base_url: str, store: EvidenceStore) -> bool:
    """Deep asynchronous crawling using Crawl4AI with evidence ledger population."""
    if not CRAWL4AI_AVAILABLE:
        return False
    try:
        async with AsyncWebCrawler(verbose=False) as crawler:
            home_res = await crawler.arun(url=url)
            if not home_res or not home_res.success or len(home_res.markdown or "") < 100:
                return False

            ext = extract_markdown_evidence(home_res.markdown, url)
            p_type, weight = classify_page(url, ext["title"], ext["headings"], ext["clean_text"])
            
            home_evidence = PageEvidence(
                url=url,
                title=ext["title"],
                page_type=p_type,
                headings=ext["headings"],
                clean_text=ext["clean_text"],
                canonical_snippets=ext["canonical_snippets"],
                extracted_keywords=[],
                credibility_weight=weight,
                timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                status_code=200
            )
            store.pages.append(home_evidence)
            store.signals.extend(extract_universal_signals(ext["clean_text"], url))

            for snip in ext["canonical_snippets"]:
                store.add_evidence(
                    source_url=url,
                    source_title=ext["title"],
                    quoted_text=snip,
                    entity=store.company_name,
                    relationship="current_operation",
                    evidence_type="homepage_extract",
                    is_first_party=True
                )

            internal_links = []
            if home_res.links and isinstance(home_res.links, dict):
                internal = home_res.links.get("internal", [])
                for item in internal:
                    href = item.get("href") if isinstance(item, dict) else str(item)
                    if href and href.startswith("http") and urllib.parse.urlparse(href).netloc == urllib.parse.urlparse(base_url).netloc and href != url:
                        if href not in internal_links:
                            internal_links.append(href)

            # Sort subpages by depth
            internal_links.sort(key=lambda u: len(urllib.parse.urlparse(u).path.strip("/").split("/")))
            for sub_url in internal_links[:12]:
                try:
                    sub_res = await crawler.arun(url=sub_url)
                    if sub_res and sub_res.success and len(sub_res.markdown or "") > 100:
                        sub_ext = extract_markdown_evidence(sub_res.markdown, sub_url)
                        sub_type, sub_weight = classify_page(sub_url, sub_ext["title"], sub_ext["headings"], sub_ext["clean_text"])
                        evidence = PageEvidence(
                            url=sub_url,
                            title=sub_ext["title"],
                            page_type=sub_type,
                            headings=sub_ext["headings"],
                            clean_text=sub_ext["clean_text"],
                            canonical_snippets=sub_ext["canonical_snippets"],
                            extracted_keywords=[],
                            credibility_weight=sub_weight,
                            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            status_code=200
                        )
                        store.pages.append(evidence)
                        store.signals.extend(extract_universal_signals(sub_ext["clean_text"], sub_url))

                        for snip in sub_ext["canonical_snippets"]:
                            store.add_evidence(
                                source_url=sub_url,
                                source_title=sub_ext["title"],
                                quoted_text=snip,
                                entity=store.company_name,
                                relationship="current_operation",
                                evidence_type="subpage_extract",
                                is_first_party=True
                            )
                except Exception:
                    pass

            return len(store.pages) > 0
    except Exception:
        return False

def search_company_serp(query_or_url: str, api_key: str = None) -> dict:
    """
    Main Entrypoint: Crawls and extracts deep structured evidence using Crawl4AI
    with automatic multi-source fallback and canonical evidence ledger generation.
    Returns fail-closed status if no evidence could be gathered.
    """
    clean_input = query_or_url.strip()
    if not clean_input.startswith(("http://", "https://")):
        url = "https://" + clean_input
    else:
        url = clean_input

    parsed = urllib.parse.urlparse(url)
    domain = parsed.netloc.replace("www.", "").strip()
    clean_name = domain.split(".")[0].capitalize() if domain else "Enterprise"
    base_url = f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else url

    store = EvidenceStore(
        domain=domain,
        company_name=clean_name,
        base_url=base_url
    )

    # 1. Primary Engine: Crawl4AI Deep Structured Ingestion
    crawl4ai_success = False
    if CRAWL4AI_AVAILABLE:
        try:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        crawl4ai_success = pool.submit(asyncio.run, _crawl4ai_deep_harvest(url, base_url, store)).result()
                else:
                    crawl4ai_success = asyncio.run(_crawl4ai_deep_harvest(url, base_url, store))
            except RuntimeError:
                crawl4ai_success = asyncio.run(_crawl4ai_deep_harvest(url, base_url, store))
        except Exception:
            crawl4ai_success = False

    # 2. Resilient Fallback: Multi-Threaded HTTP Parser if Crawl4AI was blocked or unavailable
    if not crawl4ai_success or len(store.pages) == 0:
        _, home_html, status_code = fetch_page_content(url, timeout=7)
        if home_html:
            extracted = clean_html(home_html)
            p_type, weight = classify_page(url, extracted["title"], extracted["headings"], extracted["clean_text"])
            
            home_evidence = PageEvidence(
                url=url,
                title=extracted["title"],
                page_type=p_type,
                headings=extracted["headings"],
                clean_text=extracted["clean_text"],
                canonical_snippets=extracted["canonical_snippets"],
                extracted_keywords=[],
                credibility_weight=weight,
                timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                status_code=status_code
            )
            store.pages.append(home_evidence)
            store.signals.extend(extract_universal_signals(extracted["clean_text"], url))

            for snip in extracted["canonical_snippets"]:
                store.add_evidence(
                    source_url=url,
                    source_title=extracted["title"],
                    quoted_text=snip,
                    entity=store.company_name,
                    relationship="current_operation",
                    evidence_type="homepage_fallback",
                    is_first_party=True
                )

            sub_links = extract_links(home_html, base_url)
            if sub_links:
                with ThreadPoolExecutor(max_workers=5) as executor:
                    future_to_url = {executor.submit(fetch_page_content, u, 6): u for u in sub_links}
                    for future in as_completed(future_to_url):
                        sub_url, sub_html, sub_status = future.result()
                        if sub_html and sub_status == 200:
                            sub_ext = clean_html(sub_html)
                            sub_type, sub_weight = classify_page(sub_url, sub_ext["title"], sub_ext["headings"], sub_ext["clean_text"])
                            if len(sub_ext["clean_text"]) > 100:
                                evidence = PageEvidence(
                                    url=sub_url,
                                    title=sub_ext["title"],
                                    page_type=sub_type,
                                    headings=sub_ext["headings"],
                                    clean_text=sub_ext["clean_text"],
                                    canonical_snippets=sub_ext["canonical_snippets"],
                                    extracted_keywords=[],
                                    credibility_weight=sub_weight,
                                    timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                    status_code=sub_status
                                )
                                store.pages.append(evidence)
                                store.signals.extend(extract_universal_signals(sub_ext["clean_text"], sub_url))

                                rel_type = "portfolio_company" if "portfolio" in sub_url.lower() or sub_type == PageType.CASE_STUDY else ("stated_focus" if sub_type == PageType.PRODUCTS_SERVICES else "current_operation")
                                for snip in sub_ext["canonical_snippets"]:
                                    store.add_evidence(
                                        source_url=sub_url,
                                        source_title=sub_ext["title"],
                                        quoted_text=snip,
                                        entity=store.company_name,
                                        relationship=rel_type,
                                        evidence_type="subpage_fallback",
                                        is_first_party=True
                                    )

    # 3. Third-party Search & Knowledge Insights
    if domain:
        store.search_insights = fetch_search_insights(clean_name, domain)
        for insight in store.search_insights:
            store.add_evidence(
                source_url=f"https://en.wikipedia.org/wiki/{urllib.parse.quote(clean_name)}",
                source_title="Encyclopedic Third-Party Knowledge",
                quoted_text=insight,
                entity=clean_name,
                relationship="generic_statement",
                evidence_type="encyclopedic_knowledge",
                is_first_party=False,
                confidence="medium"
            )

    # 4. Fail-closed Confidence Assessment
    total_pages = len(store.pages)
    total_evidence = len(store.evidence_ledger)
    types_found = {p.page_type for p in store.pages}
    has_product_page = PageType.PRODUCTS_SERVICES in types_found or PageType.SOLUTIONS in types_found
    
    if total_pages >= 4 and has_product_page and total_evidence >= 5:
        store.confidence_score = 0.95
        store.confidence_label = "high"
        store.status = "verified"
    elif total_pages >= 1 and total_evidence >= 1:
        store.confidence_score = 0.80
        store.confidence_label = "medium"
        store.status = "partially_verified"
    else:
        store.confidence_score = 0.0
        store.confidence_label = "low"
        store.status = "insufficient_evidence"

    aggregated_text = store.get_aggregated_text()

    return {
        "domain": domain,
        "base_url": base_url,
        "content": aggregated_text,
        "evidence_store": store,
        "evidence_ledger": [e.to_dict() for e in store.evidence_ledger],
        "search_results_count": len(store.pages),
        "source_links": [p.url for p in store.pages] if store.pages else ([f"https://{domain}"] if domain else []),
        "status": store.status
    }

def scrape_company_evidence(query_or_url: str) -> EvidenceStore:
    """Convenience wrapper returning EvidenceStore directly from search_company_serp."""
    res = search_company_serp(query_or_url)
    ev = res.get("evidence_store")
    if ev and isinstance(ev, EvidenceStore):
        return ev
    return EvidenceStore(domain=str(query_or_url), company_name=str(query_or_url), base_url="")
