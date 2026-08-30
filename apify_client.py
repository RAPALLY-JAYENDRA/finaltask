import requests
import logging
import json
import time
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse
from config import get_config

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | apify_client | %(message)s"))
    logger.addHandler(handler)
logger.propagate = False


def is_valid_linkedin_url(url: str) -> bool:
    if not isinstance(url, str) or not url:
        return False
    try:
        p = urlparse(url.strip())
        return p.scheme == "https" and p.netloc.lower().endswith("linkedin.com") and p.path.startswith("/in/") and bool(p.path[4:].strip("/"))
    except Exception:
        return False


def sanitize_external_text(text: Any) -> str:
    if not text:
        return ""
    s = str(text).strip()
    s = re.sub(r"<!--.*?-->", "", s, flags=re.DOTALL)
    s = re.sub(r"<[^>]*>", "", s)
    return s.replace("<", "&lt;").replace(">", "&gt;")


_APIFY_PROFILE_CACHE: Dict[str, dict] = {}


def scrape_linkedin_with_apify(linkedin_url: str, api_token: Optional[str] = None, actor_id: Optional[str] = None) -> dict:
    """
    Calls an Apify Actor to scrape a LinkedIn profile URL with strict caching to conserve compute credits.
    Returns the raw profile dictionary or an error dictionary.
    """
    started = time.perf_counter()
    logger.info("[APIFY] START | url=%s", linkedin_url)

    if not is_valid_linkedin_url(linkedin_url):
        logger.error("[APIFY] Invalid LinkedIn URL | %s", linkedin_url)
        return {"error": "Invalid LinkedIn URL"}

    norm_url = linkedin_url.strip().lower().rstrip("/")
    if norm_url in _APIFY_PROFILE_CACHE:
        logger.info("[APIFY] Cache hit for %s — skipping API call and conserving credits.", norm_url)
        return _APIFY_PROFILE_CACHE[norm_url]

    token = (api_token or get_config("APIFY_API_TOKEN") or "").strip()
    if not token:
        logger.warning("[APIFY] Missing APIFY_API_TOKEN")
        return {"error": "APIFY_API_TOKEN is not configured"}
    
    actor = (actor_id or get_config("APIFY_ACTOR_ID") or "supreme_coder~linkedin-profile-scraper").strip().replace("/", "~")

    session = requests.Session()
    endpoint = f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items?token={token}&timeout=60"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    
    payload = {
        "urls": [{"url": linkedin_url}],
        "findContacts": False,
        "scrapeCompany": False
    }

    try:
        logger.info("[APIFY] Triggering actor %s for %s", actor, linkedin_url)
        req_start = time.perf_counter()
        response = session.post(endpoint, headers=headers, json=payload, timeout=(10.0, 65.0))
        latency = time.perf_counter() - req_start
        if response.status_code == 200:
            try:
                data = response.json()
                if isinstance(data, list) and data:
                    profile = data[0]
                    if isinstance(profile, dict):
                        logger.info("[APIFY] Scrape successful for %s (latency: %.2fs)", linkedin_url, latency)
                        _APIFY_PROFILE_CACHE[norm_url] = profile
                        return profile
                elif isinstance(data, dict):
                    if not data.get("error"):
                        logger.info("[APIFY] Scrape successful for %s (latency: %.2fs)", linkedin_url, latency)
                        _APIFY_PROFILE_CACHE[norm_url] = data
                        return data
            except Exception as je:
                logger.error("[APIFY] Failed to parse JSON: %s", je)
                return {"error": f"Failed to parse Apify response: {je}"}
        elif response.status_code == 404:
            logger.error("[APIFY] Actor not found: %s", actor)
            return {"error": f"Apify Actor '{actor}' not found (404)"}
        elif response.status_code == 401:
            logger.error("[APIFY] Invalid or unauthorized APIFY_API_TOKEN")
            return {"error": "Unauthorized: Check your APIFY_API_TOKEN"}
        else:
            resp_snippet = response.text[:250] if response.text else "No response body"
            err = f"Apify actor {actor} returned HTTP {response.status_code}: {resp_snippet}"
            logger.warning("[APIFY] %s", err)
            return {"error": err}
    except requests.exceptions.Timeout:
        logger.warning("[APIFY] Request timed out after 65s for %s", linkedin_url)
        return {"error": f"Apify scrape timed out on {actor}"}
    except Exception as exc:
        logger.exception("[APIFY] Unexpected error during scrape: %s", exc)
        return {"error": str(exc)}

    return {"error": "Unknown error during Apify scrape"}


def search_linkedin_with_apify(name: str, company: str = None, country: str = None, api_token: Optional[str] = None) -> List[dict]:
    """
    Searches LinkedIn for person candidates directly via Apify supreme_coder scraper actor.
    """
    token = (api_token or get_config("APIFY_API_TOKEN") or "").strip()
    if not token or not name:
        return []
    
    query = f"{name} {company}".strip() if company else name.strip()
    logger.info("[APIFY_SEARCH] START | query=%s", query)
    
    actor = "supreme_coder~linkedin-profile-scraper"
    session = requests.Session()
    endpoint = f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items?token={token}&timeout=45"
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    payload = {
        "urls": [{"url": f"https://www.linkedin.com/search/results/people/?keywords={query}"}],
        "findContacts": False,
        "scrapeCompany": False
    }
    
    try:
        response = session.post(endpoint, headers=headers, json=payload, timeout=(10.0, 50.0))
        if response.status_code == 200:
            data = response.json()
            if isinstance(data, list) and data:
                logger.info("[APIFY_SEARCH] Actor %s returned %d results", actor, len(data))
                return data
    except Exception as e:
        logger.warning("[APIFY_SEARCH] Actor %s error: %s", actor, e)
    return []


def format_apify_linkedin_profile(profile: dict) -> str:
    """
    Formats the JSON profile from Apify into rich markdown text for LLM consumption.
    """
    if not profile or not isinstance(profile, dict):
        return "N/A"

    parts = []

    name = (
        profile.get("fullName")
        or profile.get("name")
        or f"{profile.get('firstName', '')} {profile.get('lastName', '')}".strip()
        or f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip()
    )
    headline = profile.get("headline") or profile.get("title") or profile.get("occupation") or profile.get("position") or ""
    summary = profile.get("summary") or profile.get("about") or profile.get("description") or ""
    location = (
        profile.get("location")
        or profile.get("locationName")
        or profile.get("city")
        or profile.get("address")
        or ""
    )

    clean_name = sanitize_external_text(name)
    clean_headline = sanitize_external_text(headline)
    clean_location = sanitize_external_text(location)
    clean_summary = sanitize_external_text(summary)

    if clean_name:
        parts.append(f"Profile Name: {clean_name}")
    if clean_headline:
        parts.append(f"Headline/Title: {clean_headline}")
    if clean_location:
        parts.append(f"Location: {clean_location}")
    if clean_summary:
        parts.append(f"Summary/About: {clean_summary}")

    experience = (
        profile.get("experience")
        or profile.get("positions")
        or profile.get("workExperience")
        or profile.get("experiences")
        or profile.get("positionsHistory")
        or []
    )
    if isinstance(experience, dict):
        experience = [experience]
    if isinstance(experience, list) and experience:
        parts.append("\nWork Experience:")
        for exp in experience[:6]:
            if not isinstance(exp, dict):
                if isinstance(exp, str) and exp.strip():
                    parts.append(sanitize_external_text(exp))
                continue
            title = exp.get("title") or exp.get("role") or exp.get("position") or exp.get("jobTitle") or "Role"
            company = (
                exp.get("companyName")
                or exp.get("company")
                or exp.get("organization")
                or exp.get("employer")
                or "Company"
            )
            if isinstance(company, dict):
                company = company.get("name") or company.get("title") or "Company"
            
            time_period = exp.get("timePeriod") or exp.get("dateRange") or exp.get("duration") or exp.get("period")
            if not time_period:
                from_date = exp.get("startDate") or exp.get("from") or exp.get("start")
                to_date = exp.get("endDate") or exp.get("to") or exp.get("end") or "Present"
                if from_date:
                    time_period = f"{from_date} - {to_date}"

            desc = exp.get("description") or exp.get("summary") or exp.get("caption") or ""
            clean_title = sanitize_external_text(title)
            clean_company = sanitize_external_text(company)
            clean_time = sanitize_external_text(time_period)
            exp_str = f"**{clean_title}** at **{clean_company}** ({clean_time or 'N/A'})"
            if desc and isinstance(desc, str) and desc.strip():
                exp_str += f"\n  {sanitize_external_text(desc)[:400]}"
            parts.append(exp_str)

    education = (
        profile.get("education")
        or profile.get("educations")
        or profile.get("schools")
        or profile.get("educationHistory")
        or []
    )
    if isinstance(education, dict):
        education = [education]
    if isinstance(education, list) and education:
        parts.append("\nEducation:")
        for edu in education[:4]:
            if not isinstance(edu, dict):
                if isinstance(edu, str) and edu.strip():
                    parts.append(sanitize_external_text(edu))
                continue
            school = edu.get("schoolName") or edu.get("school") or edu.get("institution") or edu.get("university") or "University"
            degree = edu.get("degreeName") or edu.get("degree") or edu.get("degree_name") or ""
            field = edu.get("fieldOfStudy") or edu.get("field") or edu.get("major") or ""
            time_period = edu.get("dateRange") or edu.get("timePeriod") or edu.get("period") or ""
            if not time_period:
                from_date = edu.get("startDate") or edu.get("from")
                to_date = edu.get("endDate") or edu.get("to")
                if from_date:
                    time_period = f"{from_date} - {to_date or 'Present'}"

            details = []
            if degree:
                details.append(sanitize_external_text(degree))
            if field:
                details.append(sanitize_external_text(field))
            edu_str = f"**{sanitize_external_text(school)}**"
            if details:
                edu_str += f" ({', '.join(details)})"
            if time_period:
                edu_str += f" | {sanitize_external_text(time_period)}"
            parts.append(edu_str)

    skills = profile.get("skills") or profile.get("topSkills") or profile.get("skillsList") or []
    if isinstance(skills, list) and skills:
        clean_skills = [sanitize_external_text(s.get("name") if isinstance(s, dict) else s) for s in skills[:12] if s]
        if clean_skills:
            parts.append(f"\nSkills: {', '.join(clean_skills)}")

    return "\n".join(parts) if parts else "N/A"
