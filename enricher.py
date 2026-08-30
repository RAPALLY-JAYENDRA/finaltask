import logging
import re
import json
import urllib.parse
import ipaddress
from datetime import datetime, timezone
from typing import List, Optional, Any, Dict
from pydantic import BaseModel, Field, field_validator

from config import get_config, is_google_cse_configured, is_serp_configured, is_worker_url_configured
from validator import validate_email
from client_utils import call_cloudflare_worker_endpoint, model_validate_compat, model_dump_compat
from search_client import _call_search_api, normalize_url, is_valid_linkedin_url, sanitize_search_input, search_company_profile
from linkedin_resolver import clean_company_name
from scraper import EvidenceStore, scrape_company_evidence, search_company_serp
from service_catalog import catalog
from worker_ai import ai

logger = logging.getLogger(__name__)

def is_safe_url(url: str) -> bool:
    if not isinstance(url, str) or not url.strip():
        return False
    try:
        parsed = urllib.parse.urlparse(url.strip())
        if parsed.scheme not in {'http', 'https'}:
            return False
        if not parsed.netloc or not parsed.hostname:
            return False
        hostname = parsed.hostname.lower().rstrip('.')
        blocked = {'localhost', '127.0.0.1', '0.0.0.0', '::1', '169.254.169.254'}
        if hostname in blocked:
            return False
        try:
            ip = ipaddress.ip_address(hostname)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return False
        except ValueError:
            pass
        return True
    except Exception:
        return False

# --- Pydantic Schemas ---

class MatchedOffering(BaseModel):
    product_name: str = "N/A"
    url: str = "https://www.blackridgeresearch.com"
    relevance_summary: str = "N/A"
    vector_cosine: float = 0.0
    confidence: str = "medium"

class LeadIntent(BaseModel):
    referred_product_or_service: str = "N/A"
    core_needs: str = ""
    company_alignment: str = ""
    matched_offerings: List[MatchedOffering] = Field(default_factory=list)
    expected_solutions: str = ""
    application_use_case: str = ""
    sales_pitch_hook: str = ""

class EducationEntry(BaseModel):
    school: str = "N/A"
    degree: str = "N/A"
    field: str = "N/A"
    period: str = "N/A"

class ExperienceEntry(BaseModel):
    title: str = "N/A"
    company: str = "N/A"
    period: str = "N/A"
    description: str = "N/A"

class CompanyDetails(BaseModel):
    name: str = "N/A"
    industry: str = "N/A"
    size: str = "N/A"
    website: str = "N/A"
    description: str = ""
    notable_projects: str = "N/A"
    locations: str = "N/A"

class DeliveredProject(BaseModel):
    project_name: str = "N/A"
    client_partner: str = "N/A"
    details: str = "N/A"
    evidence_quote: str = ""
    source_url: str = ""
    timeline: str = "N/A"

class ActiveOperation(BaseModel):
    operation_name: str = "N/A"
    scope: str = "N/A"
    details: str = "N/A"
    evidence_quote: str = ""
    source_url: str = ""

class FutureRoadmap(BaseModel):
    initiative_name: str = "N/A"
    target_timeline: str = "N/A"
    strategic_focus: str = "N/A"
    evidence_quote: str = ""
    source_url: str = ""

class ProjectsData(BaseModel):
    delivered_projects: List[DeliveredProject] = Field(default_factory=list)
    active_operations: List[ActiveOperation] = Field(default_factory=list)
    future_roadmaps: List[FutureRoadmap] = Field(default_factory=list)

class SalesStrategy(BaseModel):
    pitch_hook: str = ""
    value_propositions: List[str] = Field(default_factory=list)
    email_draft: str = ""
    objection_handling: List[str] = Field(default_factory=list)

class LeadDossier(BaseModel):
    lead_name: str = "N/A"
    lead_email: str = "N/A"
    lead_phone: str = "N/A"
    company_name: Optional[str] = "N/A"
    company_website: str = "N/A"
    country: str = "N/A"
    linkedin_url: str = ""
    summary: str = ""
    company_profile: str = ""
    lead_intent: LeadIntent = Field(default_factory=LeadIntent)
    projects: ProjectsData = Field(default_factory=ProjectsData)
    strategic_offerings: List[MatchedOffering] = Field(default_factory=list)
    sales_strategy: SalesStrategy = Field(default_factory=SalesStrategy)
    use_case: str = "N/A"
    buying_role: str = "N/A"
    budget: str = "N/A"
    timeline: str = "N/A"
    skills: List[str] = Field(default_factory=list)
    experience: List[ExperienceEntry] = Field(default_factory=list)
    education: List[EducationEntry] = Field(default_factory=list)
    company_details: CompanyDetails = Field(default_factory=CompanyDetails)
    web_insights: List[str] = Field(default_factory=list)

    @field_validator("web_insights", mode="before")
    @classmethod
    def parse_web_insights(cls, v: Any) -> List[str]:
        if isinstance(v, str):
            return [v.strip()] if v.strip() and v.strip().upper() != "N/A" else []
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        return []

    @field_validator("skills", mode="before")
    @classmethod
    def parse_skills(cls, v: Any) -> List[str]:
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()] if v.strip() and v.strip().upper() != "N/A" else []
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        return []

def sanitize_llm_input_text(text: str) -> str:
    if not text:
        return ""
    forbidden_patterns = [
        r"(?i)you are a",
        r"(?i)system prompt",
        r"(?i)user prompt",
        r"(?i)instruction:",
        r"(?i)response:",
        r"(?i)ignore previous",
        r"(?i)override previous",
        r'"""',
        r"'''",
        r"==========+"
    ]
    cleaned = text
    for pattern in forbidden_patterns:
        cleaned = re.sub(pattern, " [sanitized] ", cleaned)
    cleaned = re.sub(r'\n+', '\n', cleaned)
    cleaned = re.sub(r' +', ' ', cleaned)
    return cleaned.strip()

def format_experience(exp_list: list) -> str:
    if not exp_list or not isinstance(exp_list, list):
        return "N/A"
    lines = []
    for item in exp_list:
        if isinstance(item, str) and item.strip() and item.strip().upper() != "N/A":
            lines.append(f"- {item.strip()}")
            continue
        if not isinstance(item, dict):
            item = model_dump_compat(item) if hasattr(item, "__dict__") else {}
        title = item.get("title") or "Professional"
        company = item.get("company") or "Enterprise"
        period = item.get("period") or ""
        desc = item.get("description") or ""
        role_str = f"**{title}** at **{company}**"
        if period and period.upper() != "N/A":
            role_str += f" ({period})"
        if desc and desc.upper() != "N/A" and desc.strip():
            role_str += f"\n  {desc.strip()}"
        lines.append(role_str)
    return "\n".join(lines) if lines else "N/A"

def format_education(edu_list: list) -> str:
    if not edu_list or not isinstance(edu_list, list):
        return "N/A"
    lines = []
    for item in edu_list:
        if isinstance(item, str) and item.strip() and item.strip().upper() != "N/A":
            lines.append(f"- {item.strip()}")
            continue
        if not isinstance(item, dict):
            item = model_dump_compat(item) if hasattr(item, "__dict__") else {}
        school = item.get("school") or "Institution"
        degree = item.get("degree") or ""
        field = item.get("field") or ""
        period = item.get("period") or ""
        edu_str = f"**{school}**"
        details = [d for d in [degree, field] if d and d.upper() != "N/A"]
        if details:
            edu_str += f": {', '.join(details)}"
        if period and period.upper() != "N/A":
            edu_str += f" ({period})"
        lines.append(edu_str)
    return "\n".join(lines) if lines else "N/A"

def enrich_lead_dossier(
    lead_input: dict,
    search_context: list = None,
    company_search_context: list = None,
    evidence_store: Optional[EvidenceStore] = None
) -> dict:
    """
    Master lead enrichment pipeline unifying:
    1. Person profile & LinkedIn resolution
    2. Multi-page company website scraping & evidence ledger
    3. Dense 1024-dim vector service catalog matching
    4. Senior Principal AI executive synthesis & projects extraction
    """
    name = (lead_input.get("name") or "").strip()
    email = (lead_input.get("email") or "").strip()
    phone = (lead_input.get("phone") or "").strip()
    company = (lead_input.get("company") or "").strip()
    country = (lead_input.get("country") or "").strip()
    interests = (lead_input.get("interests") or lead_input.get("referred_product") or "").strip()
    message = (lead_input.get("message") or "").strip()
    website = (lead_input.get("website") or lead_input.get("page_url") or "").strip()

    # 1. Resolve LinkedIn URL
    extracted_li = ""
    for h in (search_context or []):
        u = h.get("url") or ""
        if "linkedin.com/in/" in u.lower():
            extracted_li = u
            break
    
    linkedin_url = lead_input.get("linkedin_url") or extracted_li or ""
    if linkedin_url:
        linkedin_url = normalize_url(linkedin_url)

    # 2. Company Evidence Gathering & Scraping if not already provided
    if not evidence_store:
        target_domain_or_name = website or company
        try:
            evidence_store = scrape_company_evidence(target_domain_or_name)
        except Exception as e:
            logger.warning(f"Scraper error for {target_domain_or_name}: {e}")
            evidence_store = EvidenceStore(domain=target_domain_or_name, company_name=company, base_url=website)

    # 3. Vector Matching against 462 Canonical Offerings
    matched_offerings_list = []
    try:
        # Build query from inbound message, interests, and company evidence
        evidence_text_sample = " ".join([p.clean_text[:500] for p in evidence_store.pages[:5]])
        match_query = f"{interests} {message} {company} {evidence_text_sample}".strip()
        
        # Rank top offerings using ServiceCatalog
        top_candidates = catalog.rank_candidates(
            query_text=match_query,
            evidence_store=evidence_store,
            top_k=5
        )
        for cand in top_candidates:
            matched_offerings_list.append({
                "product_name": cand.canonical_name,
                "url": f"https://www.blackridgeresearch.com/reports/{cand.canonical_name.lower().replace(' ', '-')}",
                "relevance_summary": cand.reason or cand.definition,
                "vector_cosine": round(float(cand.vector_cosine), 4),
                "confidence": cand.confidence
            })
    except Exception as e:
        logger.warning(f"Vector matching error: {e}")

    # 4. Senior Principal AI Synthesis & Executive Research
    # Build unified context for AI
    scraped_context = ""
    for p in evidence_store.pages[:6]:
        scraped_context += f"\n--- Page: {p.title} ({p.url}) ---\n{p.clean_text[:2500]}\n"

    linkedin_context = ""
    for hit in (search_context or [])[:5]:
        linkedin_context += f"\n- {hit.get('title')}: {hit.get('snippet') or hit.get('content')}"

    # Run AI analysis via WorkerAI or Cloudflare Worker
    ai_system_prompt = """You are a Senior Principal Enterprise Intelligence & Offering Matcher.
Analyze the target executive lead and enterprise from the provided factual evidence and website data.
Return ONLY valid JSON matching this schema:
{
  "summary": "Comprehensive senior principal executive assessment of the lead and business context.",
  "company_profile": "Detailed operational profile: business model, products, target sectors, facilities, and scale.",
  "buying_role": "Decision Maker | Technical Evaluator | Procurement | Influencer | Research End-User",
  "use_case": "Specific technical and commercial use cases aligned with their operations.",
  "budget": "Estimated project/procurement budget range (e.g. $50K-$250K, $250K-$1M, Enterprise capex)",
  "timeline": "Immediate (0-30 days) | Q1-Q2 Active | Long-term Roadmap",
  "skills": ["Skill 1", "Skill 2", "Skill 3"],
  "experience": [
    {"title": "Role Title", "company": "Company Name", "period": "YYYY-YYYY or Present", "description": "Key responsibilities"}
  ],
  "education": [
    {"school": "University Name", "degree": "Degree", "field": "Major", "period": "YYYY-YYYY"}
  ],
  "delivered_projects": [
    {"project_name": "Name", "client_partner": "Client/Partner", "details": "Scope and impact", "evidence_quote": "Quote", "source_url": "URL"}
  ],
  "active_operations": [
    {"operation_name": "Name", "scope": "Operational scope", "details": "Metrics and capacity", "evidence_quote": "Quote", "source_url": "URL"}
  ],
  "future_roadmaps": [
    {"initiative_name": "Name", "target_timeline": "Timeline", "strategic_focus": "Strategic focus", "evidence_quote": "Quote", "source_url": "URL"}
  ],
  "sales_pitch_hook": "Sharp, value-driven opening hook connecting their exact pain points to relevant intelligence solutions.",
  "value_propositions": ["Value Prop 1", "Value Prop 2", "Value Prop 3"],
  "email_draft": "Executive-to-executive tailored outreach email with specific references to their projects and inbound message.",
  "objection_handling": ["Objection & Response 1", "Objection & Response 2"]
}"""

    ai_user_prompt = f"""Target Lead Input:
- Name: {name}
- Email: {email}
- Phone: {phone}
- Company: {company}
- Country: {country}
- Website: {website}
- Stated Interests / Product: {interests}
- Inbound Message / Notes: {message}

Personal & Professional LinkedIn Search Context:
{linkedin_context or 'No direct personal search hits available.'}

Company Website Scraped Intelligence:
{scraped_context or 'No live scraped website content available.'}

Matched Vector Offerings:
{json.dumps(matched_offerings_list, indent=2)}
"""

    raw_ai_res = {}
    try:
        if is_worker_url_configured():
            worker_resp = call_cloudflare_worker_endpoint({
                "action": "synthesize",
                "system_prompt": ai_system_prompt,
                "user_prompt": ai_user_prompt,
                "max_tokens": 3000
            })
            raw_text = worker_resp.get("response") or worker_resp.get("raw_text") or worker_resp.get("text") or ""
            if isinstance(raw_text, str):
                clean_json_str = re.sub(r'^```(?:json)?\s*', '', raw_text.strip(), flags=re.IGNORECASE)
                clean_json_str = re.sub(r'\s*```$', '', clean_json_str)
                raw_ai_res = json.loads(clean_json_str)
            elif isinstance(raw_text, dict):
                raw_ai_res = raw_text
    except Exception as e:
        logger.warning(f"Worker AI synthesis error: {e}")

    # Fallback to local worker_ai helper if needed
    if not raw_ai_res:
        try:
            llm_text = ai._call_llm(ai_user_prompt, ai_system_prompt)
            if llm_text:
                clean_str = re.sub(r'^```(?:json)?\s*', '', llm_text.strip(), flags=re.IGNORECASE)
                clean_str = re.sub(r'\s*```$', '', clean_str)
                raw_ai_res = json.loads(clean_str)
        except Exception as e:
            logger.warning(f"Local AI fallback error: {e}")

    # Build validated dossier dict
    summary_val = raw_ai_res.get("summary") or f"Strategic intelligence analysis for {name} ({company})."
    comp_prof_val = raw_ai_res.get("company_profile") or f"{company} operates in commercial/industrial markets."
    
    # Process projects
    deliv_projects = raw_ai_res.get("delivered_projects") or []
    act_operations = raw_ai_res.get("active_operations") or []
    fut_roadmaps = raw_ai_res.get("future_roadmaps") or []

    # Process experience and education
    exp_entries = raw_ai_res.get("experience") or []
    edu_entries = raw_ai_res.get("education") or []

    # Process sales strategy
    pitch_hook = raw_ai_res.get("sales_pitch_hook") or f"Empower {company} with deep market intelligence and project tracking."
    val_props = raw_ai_res.get("value_propositions") or []
    email_draft = raw_ai_res.get("email_draft") or ""
    obj_handling = raw_ai_res.get("objection_handling") or []

    # Lead Intent
    intent_data = {
        "referred_product_or_service": interests or "Market Research & Project Tracking",
        "core_needs": message or "Enterprise market intelligence & pipeline data.",
        "company_alignment": raw_ai_res.get("buying_role") or "Decision Maker",
        "matched_offerings": matched_offerings_list,
        "expected_solutions": raw_ai_res.get("use_case") or "Project stage-gate intelligence and market forecasting.",
        "application_use_case": raw_ai_res.get("use_case") or "Market Expansion & Capex Tracking",
        "sales_pitch_hook": pitch_hook
    }

    final_dossier = {
        "name": name,
        "email": email,
        "phone": phone,
        "company": company,
        "country": country,
        "website": website,
        "linkedin_url": linkedin_url,
        "email_validity": validate_email(email) if email else "N/A",
        "professional_summary": summary_val,
        "company_profile": comp_prof_val,
        "buying_role": raw_ai_res.get("buying_role") or "Decision Maker",
        "use_case": raw_ai_res.get("use_case") or (message[:200] if message else "N/A"),
        "budget": raw_ai_res.get("budget") or "$50K - $150K",
        "timeline": raw_ai_res.get("timeline") or "Q1-Q2 Active",
        "skills": raw_ai_res.get("skills") or [],
        "experience": exp_entries,
        "work_experience": format_experience(exp_entries),
        "education": edu_entries,
        "education_formatted": format_education(edu_entries),
        "lead_intent": intent_data,
        "referred_product": interests,
        "message": message,
        "projects": {
            "delivered_projects": deliv_projects,
            "active_operations": act_operations,
            "future_roadmaps": fut_roadmaps
        },
        "strategic_offerings": matched_offerings_list,
        "sales_strategy": {
            "pitch_hook": pitch_hook,
            "value_propositions": val_props,
            "email_draft": email_draft,
            "objection_handling": obj_handling
        },
        "scraped_pages_count": len(evidence_store.pages) if evidence_store else 0,
        "observed_technologies": evidence_store.observed_technologies if evidence_store else [],
        "observed_industries": evidence_store.observed_industries if evidence_store else []
    }

    return final_dossier
