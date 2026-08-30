import re
import logging
import inspect
import functools
import time
from urllib.parse import urlparse
from typing import List, Dict, Any, Optional, Tuple
from config import get_config
from linkedin_resolver import (
    resolve_linkedin_profile,
    resolve_linkedin_profile_async,
    build_linkedin_search_queries,
    _calculate_name_match_score,
    _calculate_company_match_score
)
from client_utils import (
    call_search_api,
    call_search_api_async,
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Streamlit can import/reload modules multiple times. Only add our handler once.
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setLevel(logging.INFO)
    _handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)s | search_client | %(message)s"
        )
    )
    logger.addHandler(_handler)

# Keep messages visible even when the host application's root logger is quiet.
logger.propagate = False

def _trace_result_summary(result: Any) -> str:
    """Return a safe, compact description for diagnostic logs."""
    try:
        if result is None:
            return "None"
        if isinstance(result, dict):
            keys = list(result.keys())
            summary = f"dict(keys={keys[:8]}"
            if len(keys) > 8:
                summary += ",..."
            summary += ")"
            if "success" in result:
                summary += f" success={result.get('success')}"
            if "linkedin_url" in result:
                summary += f" linkedin_url_present={bool(result.get('linkedin_url'))}"
            if "results" in result and isinstance(result.get("results"), list):
                summary += f" results={len(result['results'])}"
            return summary
        if isinstance(result, (list, tuple, set)):
            return f"{type(result).__name__}(len={len(result)})"
        if isinstance(result, str):
            return f"str(len={len(result)})"
        if isinstance(result, bool):
            return f"bool({result})"
        return type(result).__name__
    except Exception:
        return type(result).__name__

def trace_function(func):
    if inspect.iscoroutinefunction(func):
        @functools.wraps(func)
        async def async_wrapper(*args,**kwargs):
            started = time.perf_counter()
            logger.info("[TRACE] START %s", func.__qualname__)
            try:
                result = await func(*args,**kwargs)
                elapsed = time.perf_counter() - started
                logger.info(
                    "[TRACE] END %s | elapsed=%.2fs | result=%s",
                    func.__qualname__,
                    elapsed,
                    _trace_result_summary(result),
                )
                return result
            except Exception:
                elapsed = time.perf_counter() - started
                logger.exception(
                    "[TRACE] ERROR %s | elapsed=%.2fs",
                    func.__qualname__,
                    elapsed,
                )
                raise
        return async_wrapper
    @functools.wraps(func)
    def wrapper(*args,**kwargs):
        started = time.perf_counter()
        logger.info("[TRACE] START %s", func.__qualname__)
        try:
            result = func(*args,**kwargs)
            elapsed = time.perf_counter() - started
            logger.info(
                "[TRACE] END %s | elapsed=%.2fs | result=%s",
                func.__qualname__,
                elapsed,
                _trace_result_summary(result),
            )
            return result
        except Exception:
            elapsed = time.perf_counter() - started
            logger.exception(
                "[TRACE] ERROR %s | elapsed=%.2fs",
                func.__qualname__,
                elapsed,
            )
            raise
    return wrapper

LINKEDIN_PROFILE_REGEX = re.compile(r'https?://(?:[a-zA-Z0-9_-]+\.)*linkedin\.com/in/([a-zA-Z0-9_-]+)', re.I)

def is_valid_linkedin_url(url: str) -> bool:
    """
    Validates a LinkedIn URL strictly according to production security guidelines:
    - Must match valid LinkedIn profile /in/ structure
    - Excludes redirects, login walls, and localhost
    """
    if not url or not isinstance(url, str):
        return False
    url_str = url.strip()
    if "localhost" in url_str.lower() or "127.0.0.1" in url_str.lower():
        return False
    if not re.search(r"https?://(?:[a-zA-Z0-9_-]+\.)*linkedin\.com/in/([a-zA-Z0-9_-]+)", url_str, re.I):
        return False
    lower = url_str.lower()
    if any(x in lower for x in ["/company/", "/school/", "/jobs/", "/pulse/", "/posts/", "/learning/", "redirect", "url="]):
        return False
    return True
def sanitize_search_input(text: str) -> str:
    """
    Sanitizes user-provided search terms while preserving
    internally generated search operators such as site:linkedin.com/in/.
    """
    if not text:
        return ""
    cleaned = str(text).replace('"', "").replace("'", "")
    # Remove dangerous wildcard/control characters.
    cleaned = cleaned.replace("*", "").replace("?", "")
    # Remove boolean operators when supplied as user text.
    cleaned = re.sub(r"\s+(OR|AND)\s+", " ", cleaned, flags=re.IGNORECASE)
    # Collapse whitespace.
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned
def normalize_url(url: str) -> str:
    """
    Safely normalizes URLs for de-duplication:
    - Normalizes scheme/hostname
    - Strips fragments and query parameters
    - Normalizes trailing slashes
    - Handles malformed URLs safely without crashing.
    """
    if not url or not isinstance(url, str):
        return ""
    try:
        parsed = urlparse(url.strip())
        normalized_path = parsed.path.rstrip("/")
        if not normalized_path:
            normalized_path = "/"
        netloc = parsed.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return f"{netloc}{normalized_path}".lower()
    except Exception:
        return url.split("?")[0].rstrip("/").lower()
def _evaluate_company_domain_candidate( url: str, company_name: str = "", email: str = "" ) -> int:
    """
    Scores a possible official company website.
    Higher score means stronger evidence that the domain
    belongs to the lead's company.
    """
    if not url or not isinstance(url, str):
        return 0
    try:
        parsed = urlparse(url.strip())
        if parsed.scheme != "https":
            return 0
        domain = parsed.netloc.lower()
        if domain.startswith("www."):
            domain = domain[4:]
        blocked_domains = {
            "linkedin.com","facebook.com","twitter.com","instagram.com","youtube.com","zoominfo.com",
            "indiamart.com","crunchbase.com","glassdoor.com","yelp.com","yellowpages.com","wikipedia.org",
            "bloomberg.com",
            "prnewswire.com",
            "pressrelease.com",
            "pinterest.com",
            "reddit.com",
            "github.com",
            "flickr.com",
        }

        if any(
            domain == blocked or domain.endswith("." + blocked)
            for blocked in blocked_domains
        ):
            return 0

        score = 0

        # --------------------------------------------------
        # Email-domain match = strongest evidence
        # --------------------------------------------------

        if email and "@" in email:
            email_domain = email.split("@", 1)[1].lower().strip()

            if email_domain == domain:
                score += 60

        # --------------------------------------------------
        # Company-name keyword match
        # --------------------------------------------------

        clean_company = clean_company_name(
            company_name
        ).lower()

        company_words = [
            word
            for word in re.findall(
                r"[a-z0-9]+",
                clean_company
            )
            if len(word) > 2
        ]

        matched_words = [
            word
            for word in company_words
            if word in domain
        ]

        score += min(
            len(matched_words) * 15,
            30
        )

        # --------------------------------------------------
        # Homepage bonus
        # --------------------------------------------------

        path = parsed.path.strip("/")

        if not path or path in {
            "index.html",
            "index.php",
        }:
            score += 10

        return min(score, 100)

    except Exception:
        return 0
# --- Shared Scoring Helpers ---
def _calculate_name_match_score( lead_name: str, title: str, desc: str ) -> Tuple[int, List[str]]:
    """
    Strict person-name matching.

    Prioritizes exact full-name matches and only gives
    partial credit for strong component matches.
    """

    score = 0
    reasons = []

    if not lead_name:
        return 0, reasons

    target = re.sub(
        r"[^a-z0-9\s]",
        " ",
        lead_name.lower()
    )

    target = re.sub(
        r"\s+",
        " ",
        target
    ).strip()

    target_parts = [
        part
        for part in target.split()
        if len(part) > 1
    ]

    text = (
        f"{title} {desc}"
    ).lower()

    text_normalized = re.sub(
        r"[^a-z0-9\s]",
        " ",
        text
    )

    text_normalized = re.sub(
        r"\s+",
        " ",
        text_normalized
    ).strip()

    # ---------------------------------------------
    # Exact full-name match
    # ---------------------------------------------

    if target and target in text_normalized:
        score = 45
        reasons.append(
            "Exact full-name match found in search evidence."
        )
        return score, reasons

    # ---------------------------------------------
    # Strong individual-name matching
    # ---------------------------------------------

    matched_parts = 0

    for part in target_parts:
        if re.search(
            rf"\b{re.escape(part)}\b",
            text_normalized
        ):
            matched_parts += 1

    if not target_parts:
        return 0, reasons

    ratio = (
        matched_parts / len(target_parts)
    )

    if ratio >= 1.0:
        score = 35
        reasons.append(
            "All name components matched."
        )

    elif ratio >= 0.66:
        score = 25
        reasons.append(
            "Most name components matched."
        )

    elif ratio >= 0.5:
        score = 15
        reasons.append(
            "Partial name match found."
        )

    else:
        score = 0
        reasons.append(
            "Insufficient name evidence."
        )

    return score, reasons
def _detect_title_company_mismatch(company: str, title: str, desc: str = "") -> Tuple[bool, str]:
    """Detect an explicit employer conflict without treating every title fragment as an employer."""
    if not company or company.strip().upper() == "N/A" or not title:
        return False, ""

    target = clean_company_name(company).lower().strip()
    if not target:
        return False, ""

    normalized_title = re.sub(r"\s+", " ", title.lower().strip())
    normalized_title = re.sub(r"\s*\|\s*linkedin.*$", "", normalized_title, flags=re.I).strip()

    target_words = {
        word for word in re.findall(r"[a-z0-9]+", target)
        if len(word) > 2
    }
    if not target_words:
        return False, ""

    # A direct target-company mention is positive evidence, not a mismatch.
    target_compact = re.sub(r"[^a-z0-9]", "", target)
    title_compact = re.sub(r"[^a-z0-9]", "", normalized_title)
    if target in normalized_title or (target_compact and target_compact in title_compact):
        return False, ""

    # Explicit employer patterns: "at <company>", "@ <company>", "with <company>", "working at <company>"
    employer_match = re.search(
        r"(?:\bat\b|\b@\b|\bwith\b|\bworking at\b|\bemployed at\b)\s+([a-z0-9&.,'’\- ]{2,80}?)(?=\s*(?:\||\-|\.|\,|$))",
        normalized_title,
        flags=re.I,
    )
    if employer_match:
        employer = employer_match.group(1).strip(" .,-")
        employer_words = {
            word for word in re.findall(r"[a-z0-9]+", employer)
            if len(word) > 2
        }
        generic_roles = {"consulting", "engineering", "sales", "marketing", "management", "operations", "recruiting", "freelance", "self employed", "student", "stealth"}
        if employer_words and not target_words.intersection(employer_words) and not all(w in generic_roles for w in employer_words):
            return True, employer

    # Traditional "Name - Role - Company" LinkedIn title format.
    parts = [
        part.strip()
        for part in re.split(r"\s+-\s+|\s+\|\s+", normalized_title)
        if part.strip()
    ]
    if len(parts) >= 2:
        possible_employer = parts[-1].strip(" .,-")
        employer_words = {
            word for word in re.findall(r"[a-z0-9]+", possible_employer)
            if len(word) > 2
        }
        generic_terms = {
            "linkedin", "profile", "professional", "resume", "cv", "contact",
            "experience", "page", "jobs", "member", "open to work", "stealth",
            "consultant", "freelance", "self employed"
        }
        loc_words = {
            "philippines", "manila", "metro manila", "remote", "asia", "india",
            "usa", "united states", "uk", "singapore", "dubai", "uae", "malaysia",
            "indonesia", "vietnam", "thailand", "germany", "france", "canada"
        }
        if employer_words and not employer_words.intersection(generic_terms) and not any(loc in possible_employer for loc in loc_words):
            if target_words.intersection(employer_words):
                return False, ""
            normalized_desc = re.sub(r"\s+", " ", (desc or "").lower().strip())
            target_compact_desc = re.sub(r"[^a-z0-9]", "", target)
            desc_compact = re.sub(r"[^a-z0-9]", "", normalized_desc)
            if target in normalized_desc or (
                target_compact_desc and target_compact_desc in desc_compact
            ):
                return False, ""
            return True, possible_employer

    return False, ""


def _calculate_company_match_score(
    company: str,
    email: str,
    title: str,
    desc: str
) -> Tuple[int, List[str], bool, bool]:
    """
    Calculates company matching score using both title AND search-description evidence.
    Returns: (score, reasons, has_company_match, has_mismatch)
    """
    score = 0
    reasons = []
    has_company_match = False
    has_mismatch = False

    comp_target = company
    if not comp_target or comp_target.strip().upper() == "N/A":
        comp_target = infer_company_from_email(email)

    if not comp_target:
        reasons.append("No company info available to match.")
        return score, reasons, has_company_match, has_mismatch

    clean_comp = clean_company_name(comp_target).lower().strip()
    generic_company_words = {
        "industries", "industry", "solutions", "services", "technologies", "technology",
        "systems", "system", "group", "holdings", "holding", "corporation", "corp",
        "limited", "ltd", "incorporated", "inc", "global", "international",
        "associates", "partners", "consulting", "ventures", "capital"
    }
    clean_words = [w for w in clean_comp.split() if len(w) > 2]
    is_pure_generic = not clean_words or all(w in generic_company_words for w in clean_words)

    if is_pure_generic:
        reasons.append(f"Company target '{comp_target}' is too generic. Bypassing company match.")
        return score, reasons, has_company_match, has_mismatch

    title_lower = title.lower()
    desc_lower = desc.lower()
    title_nospace = re.sub(r"\s+", "", title_lower)
    desc_nospace = re.sub(r"\s+", "", desc_lower)
    comp_nospace = re.sub(r"\s+", "", clean_comp)

    # Strongest: exact company phrase in the title.
    if clean_comp and clean_comp in title_lower:
        score += 50
        reasons.append(f"Company name '{comp_target}' matched in LinkedIn title.")
        has_company_match = True
    elif comp_nospace and comp_nospace in title_nospace:
        score += 50
        reasons.append(f"Company name '{comp_target}' matched in LinkedIn title.")
        has_company_match = True
    else:
        # Support "at Vertiv", "Vertiv | LinkedIn", and similar title formats.
        company_words = [w for w in clean_words if w not in generic_company_words]
        matched_title_words = [w for w in company_words if w in title_lower or w in title_nospace]
        if matched_title_words:
            score += 35
            reasons.append("Matched specific company keyword(s) in LinkedIn title.")
            has_company_match = True

    # Important: search descriptions often contain the current employer even when
    # the LinkedIn title itself is noisy or truncated.
    if not has_company_match:
        if clean_comp and clean_comp in desc_lower:
            score += 45
            reasons.append(f"Company name '{comp_target}' matched in LinkedIn search description.")
            has_company_match = True
        elif comp_nospace and comp_nospace in desc_nospace:
            score += 45
            reasons.append(f"Company name '{comp_target}' matched in LinkedIn search description.")
            has_company_match = True
        else:
            company_words = [w for w in clean_words if w not in generic_company_words]
            matched_desc_words = [w for w in company_words if w in desc_lower or w in desc_nospace]
            if matched_desc_words:
                score += 25
                reasons.append("Matched specific company keyword(s) in LinkedIn search description.")
                has_company_match = True

    # Corporate email-domain alignment is supporting evidence only.
    if email and "@" in email:
        email_domain = email.split("@", 1)[1].lower().strip()
        domain_root = email_domain.split(".")[0]
        if domain_root and any(
            word == domain_root or word in domain_root
            for word in clean_words
            if len(word) > 2
        ):
            score += 10
            reasons.append(f"Corporate email domain '{email_domain}' aligns with target company.")

    return min(score, 70), reasons, has_company_match, has_mismatch


def _calculate_email_domain_match_score(email: str, title: str, desc: str) -> Tuple[int, List[str]]:
    """Calculates email domain alignment as a supporting signal without treating snippet description text as identity proof."""
    score = 0
    reasons = []
    if not email or "@" not in email:
        return score, reasons
    domain = email.split("@", 1)[1].lower().strip()
    common_domains = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "live.com", "icloud.com"}
    if domain in common_domains:
        return score, reasons
    domain_keyword = domain.split(".")[0]
    if domain_keyword and domain_keyword in title.lower():
        score += 10
        reasons.append("Corporate email domain keyword appears in LinkedIn title.")
    return score, reasons

def _calculate_country_match_score(country: str, title: str, desc: str) -> Tuple[int, List[str]]:
    """Calculates country location matching score."""
    score = 0
    reasons = []
    if country and country.strip().upper() != "N/A":
        country_clean = country.lower().strip()
        if country_clean in title.lower() or country_clean in desc.lower():
            score += 20
            reasons.append(f"Country location match resolved in snippet: '{country}'")
    return score, reasons

def _calculate_role_match_score(role: str, title: str, desc: str) -> Tuple[int, List[str]]:
    """Calculates job role title keyword alignment score."""
    score = 0
    reasons = []
    if role and role.strip().upper() != "N/A":
        role_clean = role.lower().strip().replace("/", " ").replace("-", " ")
        role_words = [w for w in role_clean.split() if len(w) > 2]
        if role_words and any(w in title.lower() or w in desc.lower() for w in role_words):
            score += 15
            reasons.append(f"Role keyword alignment match: '{role}'")
    return score, reasons

def infer_company_from_email(email: str) -> str:
    """Infers company name keywords from corporate email domains."""
    if not email or "@" not in email:
        return ""
    domain = email.split("@")[-1].lower()
    common_domains = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "live.com", "icloud.com"}
    if domain in common_domains:
        return ""
    parts = domain.split(".")
    tlds = {"com", "net", "org", "gov", "edu", "ac", "co", "io", "in", "ph", "jp", "sg", "my", "vn", "th", "uk", "us"}
    generic_subdomains = {"apac", "emea", "latam", "na", "eu", "mail", "server", "web", "portal", "cloud", "www"}
    domain_parts = [p for p in parts if p not in tlds and p not in generic_subdomains and len(p) > 2]
    if domain_parts:
        return domain_parts[0]
    return ""
def _calculate_email_company_signal( email: str, company: str, title: str, desc: str ) -> Tuple[int, List[str]]:
    score = 0
    reasons = []

    if not email or "@" not in email:
        return score, reasons

    email_domain = email.split("@", 1)[1].lower().strip()
    domain_root = email_domain.split(".")[0]

    if company:
        company_clean = clean_company_name(company).lower()
        company_words = [
            w for w in re.findall(r"[a-z0-9]+", company_clean)
            if len(w) > 2
        ]

        if domain_root in company_words:
            score += 15
            reasons.append(
                f"Email domain '{email_domain}' aligns with company '{company}'."
            )

    return score, reasons

@trace_function
def extract_linkedin_profile_url(results:list,lead_name:str,clean_company:str=None,email:str=None,country:str=None,role:str=None,phone:str=None)->str:
    if not results or not isinstance(results,list):
        return ""
    if not lead_name or not isinstance(lead_name, str) or lead_name.strip().upper() in ["", "N/A", "NONE", "UNKNOWN", "NULL", "UNDEFINED"]:
        return ""
    logger.info("[LINKEDIN] Delegating profile resolution to linkedin_resolver/Bright Data | lead=%s | candidates=%s",lead_name,len(results))
    try:
        resolution=resolve_linkedin_profile(results,lead_name,clean_company,email=email,country=country,role=role)
        url=resolution.get("linkedin_url","") if isinstance(resolution,dict) else ""
        logger.info("[LINKEDIN] Resolver returned | url=%s | reason=%s",url,resolution.get("reason","") if isinstance(resolution,dict) else "invalid response")
        if url:
            return url
    except Exception:
        logger.exception("[LINKEDIN] Resolver failed | lead=%s",lead_name)

    # Local heuristic snippet scoring fallback for offline/test environments
    best_score = 0
    top_candidates = []
    for item in results:
        if not isinstance(item, dict):
            continue
        url = item.get("url") or ""
        if not is_valid_linkedin_url(url):
            continue
        match = LINKEDIN_PROFILE_REGEX.search(url)
        if not match:
            continue
        username = match.group(1)
        if username.lower() in ["dir", "jobs", "company", "posts", "pulse"]:
            continue
        title = item.get("title") or ""
        desc = item.get("description") or ""
        name_score, _ = _calculate_name_match_score(lead_name, title, desc)
        if name_score < 20:
            continue
        comp_score, _, has_comp_match, has_mismatch = _calculate_company_match_score(clean_company, email, title, desc)
        email_score, _ = _calculate_email_domain_match_score(email, title, desc)
        country_score, _ = _calculate_country_match_score(country, title, desc)
        role_score, _ = _calculate_role_match_score(role, title, desc)
        score = name_score + comp_score + email_score + country_score + role_score
        has_target_comp = clean_company and clean_company.strip().upper() != "N/A"
        if has_target_comp and not has_comp_match:
            score = min(score, 45)
        if score >= 60:
            candidate_url = f"https://www.linkedin.com/in/{username}"
            if score > best_score:
                best_score = score
                top_candidates = [candidate_url]
            elif score == best_score:
                if candidate_url not in top_candidates:
                    top_candidates.append(candidate_url)
    if best_score >= 60 and len(top_candidates) == 1:
        return top_candidates[0]
    return ""
def clean_company_name(name: str) -> str:
    """Strips corporate suffixes from the company name to increase search query matching reliability."""
    if not name or name.strip().upper() == "N/A":
        return ""
    cleaned = name.strip()
    # Match suffixes along with optional leading commas/whitespace and replace with a space
    pattern = re.compile(
        r'\s*,\s*\b(pvt\.?\s*ltd\.?|ltd\.?|inc\.?|corp(\.?(oration)?)?|llc|co\.?|company|group|incorporated)\b'
        r'|\b(pvt\.?\s*ltd\.?|ltd\.?|inc\.?|corp(\.?(oration)?)?|llc|co\.?|company|group|incorporated)\b',
        re.IGNORECASE
    )
    cleaned = pattern.sub(" ", cleaned)
    # Compress multiple spaces inside the name
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    # Remove trailing commas, dots, or hyphens
    cleaned = re.sub(r'[\s,\.\-]+$', '', cleaned).strip()
    return cleaned
@trace_function
def _call_search_api(query: str, count: int = 5) -> list:
    """Shared Google Search API wrapper."""
    return call_search_api(
        query=query,
        count=count,
    )
@trace_function
def search_lead(lead_name: Any, company: str = None, email: str = None, country: str = None, role: str = None, interests: str = None, message: str = None, count: int = 5) -> dict:
    """
    Queries Google (CSE / Serp API) to gather info about the lead.
    Builds refined search queries strictly using the site:linkedin.com/in/ operator
    and supports name spelling variations (quoted, hyphenated, and unquoted) to match verified LinkedIn profiles.
    """
    if isinstance(lead_name, dict):
        lead_dict = lead_name
        lead_name = lead_dict.get("name") or lead_dict.get("lead_name") or ""
        company = company or lead_dict.get("company") or ""
        email = email or lead_dict.get("email") or ""
        country = country or lead_dict.get("country") or ""
        role = role or lead_dict.get("role") or ""
        interests = interests or lead_dict.get("interests") or ""
        message = message or lead_dict.get("message") or ""

    name_clean = sanitize_search_input(lead_name)
    clean_company = sanitize_search_input(clean_company_name(company))
    inferred_company = sanitize_search_input(infer_company_from_email(email))
    comp_target = clean_company or inferred_company or ""
    if comp_target and (comp_target.upper() == "N/A" or "N/A" in comp_target.upper()):
        comp_target = ""
        
    hyphenated = name_clean.replace(" ", "-")
    name_or_clause = f'("{name_clean}" OR "{hyphenated}" OR ({name_clean}))'
    
    results = []
    
    BUYING_CLASSIFICATIONS = {
        "influencer", "recommender", "decision maker", "decision-maker", 
        "buyer", "end user", "end-user", "gatekeeper", "champion", "other",
        "influencer / recommender", "recommender / influencer"
    }
    
    role_query_part = ""
    if role and role.strip().upper() != "N/A":
        role_clean_lower = role.lower().strip()
        is_buying_class = any(cls in role_clean_lower for cls in BUYING_CLASSIFICATIONS)
        if not is_buying_class:
            role_clean_parts = role.replace("/", " ").replace("-", " ").strip()
            role_parts = [w for w in role_clean_parts.split() if len(w) > 2]
            if role_parts:
                role_query_part = " ".join(role_parts)
    
    interests_clean = ""

    if interests and interests.strip().upper() != "N/A":
        clean_ints = sanitize_search_input(interests)

        # Remove callback scheduling information.
        clean_ints = re.sub(
            r"(?i)(date|time|timezone)\s*:\s*[^,\n]+",
            " ",
            clean_ints,
        )

        clean_ints = clean_ints.replace(",", " ").replace(";", " ")

        words = [
            w for w in clean_ints.split()
            if len(w) > 2
            and not re.fullmatch(r"\d+", w)
            and "2026" not in w
        ]

        if words:
            interests_clean = " ".join(words[:3])

    country_clean = ""
    if country and country.strip().upper() != "N/A":
        country_value = country.strip().lower()
        country_map = {
            "ae": "UAE",
            "uae": "UAE",
            "united arab emirates": "UAE",
            "in": "India",
            "india": "India",
            "us": "USA",
            "usa": "USA",
            "uk": "UK",
        }
        country_clean = country_map.get(
            country_value,
            country.strip()
        )

    # ---------------------------------------------------------
    # LinkedIn candidate discovery
    # IMPORTANT:
    # Run multiple independent searches and COMBINE results.
    # Do not stop after the first non-empty result.
    # ---------------------------------------------------------

    # Unified Google-style multi-parameter query pyramid
    search_queries = build_linkedin_search_queries(
        name=name_clean,
        company=company,
        role=role,
        country=country_clean or country,
        email=email,
        interests=interests_clean or interests
    )

    # ---------------------------------------------------------
    # Execute ALL useful queries and merge results
    # ---------------------------------------------------------

    all_results = []
    seen_urls = set()

    for query_index, query in enumerate(search_queries, start=1):
        query_started = time.perf_counter()
        logger.info(
            "[SEARCH] QUERY %s/%s START | %s",
            query_index,
            len(search_queries),
            query,
        )

        try:
            query_results = _call_search_api(
                query,
                count=count,
            )
        except Exception as exc:
            logger.exception(
                "[SEARCH] QUERY %s/%s ERROR after %.2fs | %s",
                query_index,
                len(search_queries),
                time.perf_counter() - query_started,
                query,
            )
            continue

        logger.info(
            "[SEARCH] QUERY %s/%s END | elapsed=%.2fs | results=%s",
            query_index,
            len(search_queries),
            time.perf_counter() - query_started,
            len(query_results or []),
        )

        if not query_results:
            continue

        logger.info(
            "[LINKEDIN] Query returned %s results: %s",
            len(query_results),
            query,
        )

        for item in query_results:

            if not isinstance(item, dict):
                continue

            url = (
                item.get("url")
                or ""
            ).strip()

            if not is_valid_linkedin_url(url):
                continue

            normalized = normalize_url(url)

            if normalized in seen_urls:
                continue

            seen_urls.add(normalized)
            all_results.append(item)

    results = all_results

    logger.info("[TRACE] SEARCH COMPLETE: %s raw unique results collected.",len(results))
    logger.info(
        "[LINKEDIN] Collected %s unique LinkedIn candidates "
        "from %s search queries.",
        len(results),
        len(search_queries),
    )
    logger.info(
        "[TRACE] RESOLVER START | lead=%s | company=%s | candidates=%s",
        lead_name,
        clean_company or "N/A",
        len(results),
    )
    resolver_started = time.perf_counter()
    try:
        linkedin_resolution = resolve_linkedin_profile(
            results,
            lead_name,
            clean_company,
            email=email,
            country=country,
            role=role,
        )
    except Exception:
        logger.exception(
            "[TRACE] RESOLVER ERROR | lead=%s | elapsed=%.2fs",
            lead_name,
            time.perf_counter() - resolver_started,
        )
        raise
    logger.info("[TRACE] RESOLVER END: lead=%s url=%s reason=%s",lead_name,linkedin_resolution.get("linkedin_url",""),linkedin_resolution.get("reason",""))
    final_verified_url = linkedin_resolution.get("linkedin_url", "")
    linkedin_profile = linkedin_resolution.get("profile", {})

    # Always preserve successfully parsed candidate profiles in the research context.
    # Verification decides which URL is authoritative; scraping should not be lost.
    for parsed_candidate in linkedin_resolution.get("scraped_candidates", []):
        if parsed_candidate.get("status") != "parsed":
            continue
        parsed_profile = parsed_candidate.get("profile") or {}
        parsed_url = parsed_candidate.get("url") or ""
        if not parsed_url:
            continue
        results.append({
            "url": parsed_url,
            "title": parsed_profile.get("title") or parsed_profile.get("name") or lead_name,
            "description": parsed_profile.get("raw_text", ""),
            "content": parsed_profile.get("raw_text", ""),
            "source": "linkedin_scraper",
            "linkedin_identity_score": parsed_candidate.get("score", 0),
            "linkedin_identity_reasons": (parsed_candidate.get("identity") or {}).get("reasons", []),
            "verified": parsed_url == final_verified_url,
        })

    reason = linkedin_resolution.get("reason") or "Success"
    if not final_verified_url:
        from config import is_google_cse_configured, is_serp_configured
        if not is_google_cse_configured() and not is_serp_configured():
            reason = "Google Search API Key (GOOGLE_API_KEY_0 / SERPER_API_KEY) is not configured in the environment."
        elif not results:
            reason = f"Search API returned zero matches for queries related to '{lead_name}'."
        else:
            has_linkedin_results = any("linkedin.com/in/" in (item.get("url") or "").lower() for item in results)
            if not has_linkedin_results:
                reason = "Search results did not contain any personal LinkedIn profile (linkedin.com/in/) links."
            else:
                reason = linkedin_resolution.get("reason", "LinkedIn identity verification failed.")

    logger.info(
        "[TRACE] SEARCH_LEAD FINAL | lead=%s | candidates=%s | verified_url=%s | reason=%s",
        lead_name,
        len(results),
        bool(final_verified_url),
        reason,
    )
    return {
        "results": results,
        "linkedin_url": final_verified_url,
        "confidence_score": linkedin_resolution.get("confidence_score", 95 if final_verified_url else 0),
        "linkedin_profile": linkedin_profile,
        "reason": reason
    }

@trace_function
async def search_lead_async(lead_name: Any, company: str = None, email: str = None, country: str = None, role: str = None, interests: str = None, message: str = None, count: int = 5) -> dict:
    """
    Asynchronous version of search_lead that uses non-blocking call_search_api_async
    and extract_linkedin_profile_url_async.
    """
    from client_utils import call_search_api_async
    
    if isinstance(lead_name, dict):
        lead_dict = lead_name
        lead_name = lead_dict.get("name") or lead_dict.get("lead_name") or ""
        company = company or lead_dict.get("company") or ""
        email = email or lead_dict.get("email") or ""
        country = country or lead_dict.get("country") or ""
        role = role or lead_dict.get("role") or ""
        interests = interests or lead_dict.get("interests") or ""
        message = message or lead_dict.get("message") or ""

    name_clean = sanitize_search_input(lead_name)
    clean_company = sanitize_search_input(clean_company_name(company))
    inferred_company = sanitize_search_input(infer_company_from_email(email))
    comp_target = clean_company or inferred_company or ""
    if comp_target and (comp_target.upper() == "N/A" or "N/A" in comp_target.upper()):
        comp_target = ""
        
    hyphenated = name_clean.replace(" ", "-")
    name_or_clause = f'("{name_clean}" OR "{hyphenated}" OR ({name_clean}))'
    
    results = []
    
    BUYING_CLASSIFICATIONS = {
        "influencer", "recommender", "decision maker", "decision-maker", 
        "buyer", "end user", "end-user", "gatekeeper", "champion", "other",
        "influencer / recommender", "recommender / influencer"
    }
    
    role_query_part = ""
    if role and role.strip().upper() != "N/A":
        role_clean_lower = role.lower().strip()
        is_buying_class = any(cls in role_clean_lower for cls in BUYING_CLASSIFICATIONS)
        if not is_buying_class:
            role_clean_parts = role.replace("/", " ").replace("-", " ").strip()
            role_parts = [w for w in role_clean_parts.split() if len(w) > 2]
            if role_parts:
                role_query_part = " ".join(role_parts)
                
        interests_clean = ""

    if interests and interests.strip().upper() != "N/A":
        clean_ints = sanitize_search_input(interests)

        # Remove callback scheduling information.
        clean_ints = re.sub(
            r"(?i)(date|time|timezone)\s*:\s*[^,\n]+",
            " ",
            clean_ints,
        )

        clean_ints = clean_ints.replace(",", " ").replace(";", " ")

        words = [
            w for w in clean_ints.split()
            if len(w) > 2
            and not re.fullmatch(r"\d+", w)
            and "2026" not in w
        ]

        if words:
            interests_clean = " ".join(words[:3])

    country_clean = ""

    if country and country.strip().upper() != "N/A":
        country_value = country.strip().lower()

        country_map = {
            "ae": "UAE",
            "uae": "UAE",
            "united arab emirates": "UAE",
            "in": "India",
            "india": "India",
            "us": "USA",
            "usa": "USA",
            "uk": "UK",
        }

        country_clean = country_map.get(
            country_value,
            country.strip()
        )
    # Unified Google-style multi-parameter query pyramid
    search_queries = build_linkedin_search_queries(
        name=name_clean,
        company=company,
        role=role,
        country=country_clean or country,
        email=email,
        interests=interests_clean or interests
    )

    all_results = []
    seen_urls = set()

    for query_index, query in enumerate(search_queries, start=1):
        query_started = time.perf_counter()
        logger.info(
            "[SEARCH-ASYNC] QUERY %s/%s START | %s",
            query_index,
            len(search_queries),
            query,
        )
        try:
            query_results = await call_search_api_async(query, count=count)
        except Exception as exc:
            logger.exception(
                "[SEARCH-ASYNC] QUERY %s/%s ERROR after %.2fs | %s",
                query_index,
                len(search_queries),
                time.perf_counter() - query_started,
                query,
            )
            continue

        logger.info(
            "[SEARCH-ASYNC] QUERY %s/%s END | elapsed=%.2fs | results=%s",
            query_index,
            len(search_queries),
            time.perf_counter() - query_started,
            len(query_results or []),
        )

        if not query_results:
            continue

        logger.info("[LINKEDIN] Async query returned %s results: %s", len(query_results), query)

        for item in query_results:
            if not isinstance(item, dict):
                continue

            url = (item.get("url") or "").strip()
            if not is_valid_linkedin_url(url):
                continue

            normalized = normalize_url(url)
            if normalized in seen_urls:
                continue

            seen_urls.add(normalized)
            all_results.append(item)

    results = all_results

    logger.info("[TRACE] ASYNC SEARCH COMPLETE: %s unique results collected.",len(results))
    logger.info(
        "[LINKEDIN] Async collected %s unique LinkedIn candidates from %s search queries.",
        len(results),
        len(search_queries),
    )
    logger.info(
        "[TRACE] ASYNC RESOLVER START | lead=%s | company=%s | candidates=%s",
        lead_name,
        clean_company or "N/A",
        len(results),
    )
    resolver_started = time.perf_counter()
    try:
        linkedin_resolution = await resolve_linkedin_profile_async(
            results,
            lead_name,
            clean_company,
            email=email,
            country=country,
            role=role,
        )
    except Exception:
        logger.exception(
            "[TRACE] ASYNC RESOLVER ERROR | lead=%s | elapsed=%.2fs",
            lead_name,
            time.perf_counter() - resolver_started,
        )
        raise
    logger.info("[TRACE] ASYNC RESOLVER END: lead=%s url=%s reason=%s",lead_name,linkedin_resolution.get("linkedin_url",""),linkedin_resolution.get("reason",""))
    final_verified_url = linkedin_resolution.get("linkedin_url", "")
    linkedin_profile = linkedin_resolution.get("profile", {})
    if linkedin_profile.get("success") and final_verified_url:
        results.append({"url": final_verified_url, "title": linkedin_profile.get("title") or lead_name, "description": linkedin_profile.get("raw_text", ""), "content": linkedin_profile.get("raw_text", ""), "source": "linkedin_scraper"})
    reason = "Success"
    if not final_verified_url:
        from config import is_google_cse_configured, is_serp_configured
        if not is_google_cse_configured() and not is_serp_configured():
            reason = "Google Search API Key (GOOGLE_API_KEY_0 / SERPER_API_KEY) is not configured in the environment."
        elif not results:
            reason = f"Search API returned zero matches for queries related to '{lead_name}'."
        else:
            has_linkedin_results = any("linkedin.com/in/" in (item.get("url") or "").lower() for item in results)
            if not has_linkedin_results:
                reason = "Search results did not contain any personal LinkedIn profile (linkedin.com/in/) links."
            else:
                reason = linkedin_resolution.get("reason", "LinkedIn identity verification failed.")
    logger.info(
        "[TRACE] SEARCH_LEAD_ASYNC FINAL | lead=%s | candidates=%s | verified_url=%s | reason=%s",
        lead_name,
        len(results),
        bool(final_verified_url),
        reason,
    )
    return {
        "results": results,
        "linkedin_url": final_verified_url,
        "linkedin_profile": linkedin_profile,
        "reason": reason
    }
# -----------------------------------------------------------------------------
# LinkedIn profile research: DISCOVER -> SCRAPE -> VERIFY
# -----------------------------------------------------------------------------
@trace_function
def search_company_profile(company_name: str, domain: str = None,email: str=None,count: int = 5) -> list:
    """
    Searches specifically for information about the lead's company using site operators or keywords.
    """
    results = []

    comp_clean = company_name.strip() if company_name else ""
    if comp_clean.upper() == "N/A" or "N/A" in comp_clean.upper():
        comp_clean = ""

    # Dynamic domain resolution using company name if domain is not provided
    if not domain and comp_clean:
        query_resolve = f'"{comp_clean}" website OR homepage'
        resolve_results = _call_search_api(query_resolve, count=5)
        
        best_domain = ""
        best_domain_score = 0
        
        for hit in resolve_results:
            if not isinstance(hit, dict):
                continue
            hit_url = hit.get("url", "")
            if hit_url:
                url_score = _evaluate_company_domain_candidate(
    hit_url,
    company_name=comp_clean,
    email=email
)
                if url_score > best_domain_score and url_score > 50:
                    try:
                        parsed = urlparse(hit_url.strip())
                        netloc = parsed.netloc.lower()
                        if netloc.startswith("www."):
                            netloc = netloc[4:]
                        if netloc:
                            best_domain = netloc
                            best_domain_score = url_score
                    except Exception:
                        pass
                        
        if best_domain:
            domain = best_domain
            logger.info(f"Resolved company website domain dynamically from search: {domain} (confidence score: {best_domain_score})")
    
    # Query: Single comprehensive search for official company web offerings and business profile
    if domain:
        query_site = f"site:{domain}"
        site_results = _call_search_api(query_site, count=max(count, 10))
        for item in site_results or []:
            if not any(normalize_url(r.get("url", "")) == normalize_url(item.get("url", "")) for r in results):
                results.append(item)
    elif comp_clean:
        query_web = f'"{comp_clean}" ("projects" OR "services" OR "news" OR "portfolio" OR "clients" OR "about us")'
        web_results = _call_search_api(query_web, count=max(count, 10))
        for item in web_results or []:
            if not any(normalize_url(r.get("url", "")) == normalize_url(item.get("url", "")) for r in results):
                results.append(item)

    logger.info(f"Search API company profile returned {len(results)} total unique results.")
    return results

@trace_function
def verify_linkedin_confidence(profile_url: str, name: str, company: str, email: str, item: dict, role: str = None, country: str = None, threshold: int = 45) -> dict:
    """
    Computes a confidence score (0 to 100) for a LinkedIn profile match.
    Corroborates name similarity, company alignment, and email domain matches.
    Returns:
        {
            "score": int,
            "confidence": "High" | "Medium" | "Low",
            "reasons": list of str,
            "needs_review": bool
        }
    """
    if not profile_url:
        return {"score": 0, "confidence": "Low", "reasons": ["No profile URL resolved."], "needs_review": True}
        
    if not item or not isinstance(item, dict):
        return {"score": 0, "confidence": "Low", "reasons": ["Search snippet result is not a valid dictionary."], "needs_review": True}
        
    title = item.get("title") or ""
    desc = item.get("description") or ""
    
    # Calculate components using shared helpers
    mismatch, detected_employer = _detect_title_company_mismatch(company, title, desc)
    name_score, name_reasons = _calculate_name_match_score(name, title, desc)
    comp_score, comp_reasons, has_company_match, has_mismatch = _calculate_company_match_score(company, email, title, desc)
    email_score, email_reasons = _calculate_email_domain_match_score(email, title, desc)
    country_score, country_reasons = _calculate_country_match_score(country, title, desc)
    role_score, role_reasons = _calculate_role_match_score(role, title, desc)
    
    score = name_score + comp_score + email_score + country_score + role_score
    reasons = name_reasons + comp_reasons + email_reasons + country_reasons + role_reasons

    has_target_company = bool(company and clean_company_name(company))

    if mismatch:
        score = 0
        confidence = "Low"
        reasons.append(f"Rejected: candidate works at conflicting employer '{detected_employer}'.")
    elif has_target_company and not has_company_match and email_score == 0:
        score = min(score, 30)
        confidence = "Low"
        reasons.append(f"Rejected: no evidence connecting candidate to target company '{company}'.")
    elif score >= 55 and (not has_target_company or has_company_match or email_score > 0):
        confidence = "High"
    elif score >= 35:
        confidence = "Medium"
    else:
        confidence = "Low"
        
    return {
        "score": score,
        "confidence": confidence,
        "reasons": reasons,
        "needs_review": score < threshold or confidence == "Low"
    }

@trace_function
def verify_linkedin_confidence_ai(results:list,lead_name:str,company:str=None,email:str=None,country:str=None,role:str=None)->str:
    if not results or not lead_name:return ""
    logger.info("[LINKEDIN] AI resolver disabled; using linkedin_resolver/Bright Data | lead=%s",lead_name)
    return extract_linkedin_profile_url(results,lead_name,company,email,country,role)

@trace_function
async def verify_linkedin_confidence_ai_async(results:list,lead_name:str,company:str=None,email:str=None,country:str=None,role:str=None)->str:
    if not results or not lead_name:return ""
    logger.info("[LINKEDIN] Async AI resolver disabled; using linkedin_resolver/Bright Data | lead=%s",lead_name)
    try:
        resolution=await resolve_linkedin_profile_async(results,lead_name,company,email=email,country=country,role=role)
        return resolution.get("linkedin_url","") if isinstance(resolution,dict) else ""
    except Exception:
        logger.exception("[LINKEDIN] Async resolver failed | lead=%s",lead_name)
        return ""
@trace_function
async def extract_linkedin_profile_url_async(results:list,lead_name:str,clean_company:str=None,email:str=None,country:str=None,role:str=None,phone:str=None)->str:
    if not results or not isinstance(results,list) or not lead_name:return ""
    logger.info("[LINKEDIN] Async delegation to linkedin_resolver/Bright Data | lead=%s | candidates=%s",lead_name,len(results))
    try:
        resolution=await resolve_linkedin_profile_async(results,lead_name,clean_company,email=email,country=country,role=role)
        url=resolution.get("linkedin_url","") if isinstance(resolution,dict) else ""
        logger.info("[LINKEDIN] Async resolver returned | url=%s | reason=%s",url,resolution.get("reason","") if isinstance(resolution,dict) else "invalid response")
        return url
    except Exception:
        logger.exception("[LINKEDIN] Async resolver failed | lead=%s",lead_name)
        return ""
