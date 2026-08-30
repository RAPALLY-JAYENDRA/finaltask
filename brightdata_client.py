import requests
import logging
import json
import time
import random
import re
from typing import Any
from urllib.parse import urlparse
from config import get_config

logger=logging.getLogger(__name__)
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler=logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | brightdata_client | %(message)s"))
    logger.addHandler(handler)
logger.propagate=False

def is_valid_linkedin_url(url):
    if not isinstance(url,str) or not url:return False
    try:
        p=urlparse(url.strip())
        return p.scheme=="https" and p.netloc.lower().endswith("linkedin.com") and p.path.startswith("/in/") and bool(p.path[4:].strip("/"))
    except Exception:
        return False

def sanitize_external_text(text:Any)->str:
    if not text:return ""
    s=str(text).strip()
    s=re.sub(r"<!--.*?-->","",s,flags=re.DOTALL)
    s=re.sub(r"<[^>]*>","",s)
    return s.replace("<","&lt;").replace(">","&gt;")

def _get_retry_count():
    value=get_config("BRIGHTDATA_MAX_RETRIES")
    if not value:return 1
    try:return max(0,int(value))
    except (TypeError,ValueError):return 1

def _get_max_output_length():
    value=get_config("BRIGHTDATA_MAX_OUTPUT_LENGTH")
    if not value:return 8000
    try:return max(1000,int(value))
    except (TypeError,ValueError):return 8000

def scrape_linkedin_with_brightdata(linkedin_url:str,api_key:str=None,dataset_id:str=None)->dict:
    started=time.perf_counter()
    logger.info("[BRIGHTDATA] START | url=%s",linkedin_url)

    api_key=(api_key or get_config("BRIGHTDATA_API_KEY") or "").strip()
    if not api_key:
        logger.error("[BRIGHTDATA] Missing BRIGHTDATA_API_KEY")
        return {"error":"BRIGHTDATA_API_KEY is not configured"}

    if not is_valid_linkedin_url(linkedin_url):
        logger.error("[BRIGHTDATA] Invalid LinkedIn URL | %s",linkedin_url)
        return {"error":"invalid LinkedIn URL"}

    dataset_id = (dataset_id or get_config("BRIGHTDATA_DATASET_ID") or "gd_l1viktl72bvl7bjuj0").strip()
    if not dataset_id:
        logger.error("[BRIGHTDATA] Missing BRIGHTDATA_DATASET_ID")
        return {"error": "BRIGHTDATA_DATASET_ID is not configured"}

    endpoint = f"https://api.brightdata.com/datasets/v3/scrape?dataset_id={dataset_id}&format=json"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"input": [{"url": linkedin_url}]}
    max_retries=_get_retry_count()
    session=requests.Session()

    try:
        for attempt in range(1,max_retries+2):
            try:
                logger.info("[BRIGHTDATA] REQUEST | attempt=%s/%s",attempt,max_retries+1)
                response=session.post(endpoint,headers=headers,json=payload,timeout=(5.0,28.0))
                status=response.status_code
                logger.info("[BRIGHTDATA] RESPONSE | status=%s | attempt=%s",status,attempt)

                if status==200:
                    try:
                        results=response.json()
                    except (ValueError,json.JSONDecodeError):
                        logger.error("[BRIGHTDATA] Invalid JSON response")
                        return {"error":"invalid response"}

                    if isinstance(results,list):
                        if not results:
                            logger.warning("[BRIGHTDATA] Empty result list")
                            return {}
                        result=results[0]
                        if isinstance(result,dict):
                            logger.info("[BRIGHTDATA] SUCCESS | elapsed=%.2fs",time.perf_counter()-started)
                            return result
                        logger.error("[BRIGHTDATA] First result is not an object")
                        return {"error":"invalid response"}

                    if isinstance(results,dict):
                        if results.get("error"):
                            logger.error("[BRIGHTDATA] Provider error")
                            return {"error":"provider error"}
                        logger.info("[BRIGHTDATA] SUCCESS | elapsed=%.2fs",time.perf_counter()-started)
                        return results

                    logger.error("[BRIGHTDATA] Unexpected response structure")
                    return {"error":"invalid response"}

                if status in (400,401,403,404,409):
                    messages={
                        400:"invalid request parameters",
                        401:"authentication failed",
                        403:"unauthorized access",
                        404:"dataset or resource not found",
                        409:"conflict"
                    }
                    msg=messages.get(status,"request rejected")
                if status in (429,500,502,503,504):
                    if attempt>max_retries:
                        logger.error("[BRIGHTDATA] Max retries reached | status=%s",status)
                        return {"error":"provider unavailable"}
                    delay=(2**attempt)+random.uniform(0.1,0.5)
                    logger.warning("[BRIGHTDATA] Transient failure | status=%s | retry_in=%.2fs",status,delay)
                    time.sleep(delay)
                    continue

                logger.error("[BRIGHTDATA] Unexpected HTTP status | status=%s",status)
                return {"error":f"provider returned HTTP {status}"}

            except (requests.exceptions.ConnectionError,requests.exceptions.Timeout,TimeoutError) as exc:
                if attempt>max_retries:
                    logger.warning("[BRIGHTDATA] Connection/timeout after retries: %s", str(exc)[:200])
                    return {"error":"timeout"}
                delay=(2**attempt)+random.uniform(0.1,0.5)
                logger.warning("[BRIGHTDATA] Connection/timeout | retry_in=%.2fs | %s",delay,str(exc)[:200])
                time.sleep(delay)
    finally:
        session.close()

    logger.error("[BRIGHTDATA] Ended without a result")
    return {"error":"provider unavailable"}


def search_linkedin_with_brightdata(name: str, company: str = None, api_key: str = None) -> list:
    """
    Searches LinkedIn for person candidates directly via Bright Data dataset search.
    """
    key = (api_key or get_config("BRIGHTDATA_API_KEY") or "").strip()
    if not key or not name:
        return []
    
    ds = (get_config("BRIGHTDATA_DATASET_ID") or "gd_l1viktl72bvl7bjuj0").strip()
    endpoint = f"https://api.brightdata.com/datasets/v3/scrape?dataset_id={ds}&format=json"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    query = f"{name} {company}".strip() if company else name.strip()
    
    name_parts = name.strip().split()
    first_name = name_parts[0] if name_parts else name
    last_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""
    
    payload = {
        "input": [
            {"keyword": query},
            {"first_name": first_name, "last_name": last_name, "company": company or ""}
        ]
    }
    
    try:
        logger.info("[BRIGHTDATA_SEARCH] START | query=%s", query)
        response = requests.post(endpoint, headers=headers, json=payload, timeout=(5.0, 30.0))
        if response.status_code == 200:
            data = response.json()
            if isinstance(data, list):
                logger.info("[BRIGHTDATA_SEARCH] Returned %d results", len(data))
                return data
    except Exception as e:
        logger.warning("[BRIGHTDATA_SEARCH] Exception: %s", e)
    return []


def normalize_brightdata_profile(profile: dict) -> dict:
    if not isinstance(profile, dict):
        return {}

    name = (
        profile.get("name")
        or profile.get("fullName")
        or f"{profile.get('first_name','')} {profile.get('last_name','')}".strip()
        or f"{profile.get('firstName','')} {profile.get('lastName','')}".strip()
    )
    headline = profile.get("headline") or profile.get("title") or profile.get("position") or profile.get("occupation")
    location = profile.get("location") or profile.get("locationName") or profile.get("address")
    summary = profile.get("summary") or profile.get("about") or profile.get("description") or profile.get("about_text")

    experience = profile.get("experience") or profile.get("positions") or profile.get("work_experience") or []
    education = profile.get("education") or profile.get("educations") or profile.get("schools") or []

    if isinstance(experience, dict):
        experience = [experience]
    if isinstance(education, dict):
        education = [education]

    compact_experience = []
    for exp in experience[:4]:
        if isinstance(exp, dict):
            compact_experience.append({
                "title": exp.get("title") or exp.get("role") or exp.get("position"),
                "company": (
                    exp.get("company")
                    if not isinstance(exp.get("company"), dict)
                    else exp.get("company", {}).get("name")
                ) or exp.get("company_name") or exp.get("companyName"),
                "description": (exp.get("description") or exp.get("summary") or "")[:240],
            })

    compact_education = []
    for edu in education[:3]:
        if isinstance(edu, dict):
            compact_education.append({
                "school": edu.get("school") or edu.get("school_name") or edu.get("schoolName"),
                "degree": edu.get("degree") or edu.get("degreeName"),
                "field": edu.get("fieldOfStudy") or edu.get("field_of_study") or edu.get("field"),
            })

    return {
        "name": sanitize_external_text(name),
        "headline": sanitize_external_text(headline),
        "location": sanitize_external_text(location),
        "summary": sanitize_external_text(summary)[:600],
        "experience": compact_experience,
        "education": compact_education,
    }
def format_brightdata_linkedin_profile(profile:dict)->str:
    if not profile or not isinstance(profile,dict):return "N/A"
    parts=[]

    name=profile.get("name") or profile.get("fullName") or f"{profile.get('first_name','')} {profile.get('last_name','')}".strip() or f"{profile.get('firstName','')} {profile.get('lastName','')}".strip()
    headline=profile.get("headline") or profile.get("title") or profile.get("position") or profile.get("occupation")
    summary=profile.get("summary") or profile.get("about") or profile.get("description") or profile.get("about_text")
    location=profile.get("location") or profile.get("locationName") or profile.get("address")

    clean_name=sanitize_external_text(name)
    clean_headline=sanitize_external_text(headline)
    clean_location=sanitize_external_text(location)
    clean_summary=sanitize_external_text(summary)

    if clean_name:parts.append(f"Profile Name: {clean_name}")
    if clean_headline:parts.append(f"Headline/Title: {clean_headline}")
    if clean_location:parts.append(f"Location: {clean_location}")
    if clean_summary:parts.append(f"Summary/About: {clean_summary}")

    experience=profile.get("experience") or profile.get("positions") or profile.get("work_experience") or profile.get("workExperience") or profile.get("experiences") or profile.get("experienceHistory") or profile.get("experience_history") or profile.get("history") or []
    if isinstance(experience,dict):experience=[experience]
    if isinstance(experience,list) and experience:
        parts.append("\nWork Experience:")
        for exp in experience[:6]:
            if not isinstance(exp,dict):
                if isinstance(exp,str) and exp.strip():parts.append(sanitize_external_text(exp))
                continue
            title=exp.get("title") or exp.get("role") or exp.get("position") or exp.get("jobTitle") or exp.get("job_title") or "Role"
            company=exp.get("company") or exp.get("company_name") or exp.get("companyName") or exp.get("organization") or exp.get("employer") or "Company"
            if isinstance(company,dict):company=company.get("name") or company.get("companyName") or company.get("title") or "Company"
            time_period=exp.get("timePeriod") or exp.get("dateRange") or exp.get("duration") or exp.get("period")
            if not time_period:
                from_date=exp.get("from") or exp.get("start_date") or exp.get("startDate") or exp.get("start")
                to_date=exp.get("to") or exp.get("end_date") or exp.get("endDate") or exp.get("end") or "Present"
                if from_date:time_period=f"{from_date} - {to_date}"
            desc=exp.get("description") or exp.get("summary") or exp.get("caption")
            clean_title=sanitize_external_text(title)
            clean_company=sanitize_external_text(company)
            clean_time=sanitize_external_text(time_period)
            exp_str=f"**{clean_title}** at **{clean_company}** ({clean_time or 'N/A'})"
            if desc and isinstance(desc,str) and desc.strip():exp_str+=f"\n  {sanitize_external_text(desc)[:400]}"
            parts.append(exp_str)

    education=profile.get("education") or profile.get("educations") or profile.get("schools") or profile.get("educationHistory") or profile.get("education_history") or profile.get("academic") or profile.get("courses") or []
    if isinstance(education,dict):education=[education]
    if isinstance(education,list) and education:
        parts.append("\nEducation:")
        for edu in education[:4]:
            if not isinstance(edu,dict):
                if isinstance(edu,str) and edu.strip():parts.append(sanitize_external_text(edu))
                continue
            school=edu.get("school") or edu.get("school_name") or edu.get("schoolName") or edu.get("institution") or edu.get("institutionName") or edu.get("university") or edu.get("college") or "University"
            degree=edu.get("degree") or edu.get("degreeName") or edu.get("degree_name") or edu.get("degree_type") or edu.get("diploma")
            field=edu.get("fieldOfStudy") or edu.get("field_of_study") or edu.get("field") or edu.get("major") or edu.get("discipline")
            time_period=edu.get("timePeriod") or edu.get("dateRange") or edu.get("period")
            if not time_period:
                from_date=edu.get("from") or edu.get("start_date") or edu.get("startDate") or edu.get("start")
                to_date=edu.get("to") or edu.get("end_date") or edu.get("endDate") or edu.get("end")
                if from_date:time_period=f"{from_date} - {to_date or 'Present'}"
            details=[]
            if degree:details.append(sanitize_external_text(degree))
            if field:details.append(sanitize_external_text(field))
            edu_str=f"**{sanitize_external_text(school)}**"
            if details:edu_str+=f" ({', '.join(details)})"
            if time_period:edu_str+=f" | {sanitize_external_text(time_period)}"
            parts.append(edu_str)

    posts=profile.get("posts") or profile.get("recent_activity") or profile.get("activities") or profile.get("updates") or profile.get("shareHistory") or []
    if isinstance(posts,dict):posts=[posts]
    if isinstance(posts,list) and posts:
        parts.append("\nRecent Activity / Posts:")
        for post in posts[:4]:
            if not isinstance(post,dict):
                if isinstance(post,str) and post.strip():parts.append(f"- {sanitize_external_text(post)}")
                continue
            text=post.get("text") or post.get("title") or post.get("message") or post.get("description")
            time_str=post.get("time") or post.get("date") or post.get("published") or ""
            if text and str(text).strip():
                item=f"- {sanitize_external_text(text)[:300]}"
                if time_str:item+=f" ({sanitize_external_text(time_str)})"
                parts.append(item)

    output="\n".join(parts) if parts else "N/A"
    max_length=_get_max_output_length()
    if len(output)>max_length:output=output[:max_length]+"\n... [Profile truncated due to size constraints]"
    return output

def extract_fallback_profile_from_snippet(title:str,description:str,url:str)->str:
    parts=[]
    if title:parts.append(f"Name/Title from snippet: {sanitize_external_text(title)}")
    if description:parts.append(f"Description: {sanitize_external_text(description)}")
    if url:parts.append(f"Source URL: {sanitize_external_text(url)}")
    return "\n".join(parts) if parts else "N/A"
