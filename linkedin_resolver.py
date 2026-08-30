import re
import logging
import asyncio
import json
from urllib.parse import urlparse
from typing import List, Dict, Any, Tuple, Optional
from pydantic import BaseModel, field_validator
from config import get_config
from client_utils import call_search_api, call_search_api_async, call_cloudflare_worker_endpoint, model_validate_compat

logger = logging.getLogger(__name__)
LINKEDIN_PROFILE_REGEX = re.compile(r'https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/([a-zA-Z0-9_-]+)', re.I)


class AIResolverResponse(BaseModel):
    selected_url: str
    confidence_score: int
    reason: str

    @field_validator("selected_url", mode="before")
    @classmethod
    def sanitize_selected_url(cls, v):
        return str(v).strip() if v else "N/A"


def is_valid_linkedin_url(url: str) -> bool:
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
    if not text:
        return ""
    x = str(text).replace('"', "").replace("'", "").replace("*", "").replace("?", "")
    return re.sub(r"\s+(OR|AND)\s+", " ", re.sub(r"\s+", " ", x), flags=re.I).strip()


def normalize_url(url: str) -> str:
    if not url or not isinstance(url, str):
        return ""
    try:
        p = urlparse(url.strip())
        net = p.netloc.lower()
        if net.startswith("www."):
            net = net[4:]
        path = p.path.rstrip("/") or "/"
        return f"{net}{path}".lower()
    except Exception:
        return url.split("?")[0].rstrip("/").lower()


def clean_company_name(name: str) -> str:
    if not name or name.strip().upper() == "N/A":
        return ""
    x = re.sub(
        r'\s*,\s*\b(pvt\.?\s*ltd\.?|ltd\.?|inc\.?|corp(\.?(oration)?)?|llc|co\.?|company|group|incorporated)\b'
        r'|\b(pvt\.?\s*ltd\.?|ltd\.?|inc\.?|corp(\.?(oration)?)?|llc|co\.?|company|group|incorporated)\b',
        " ",
        name.strip(),
        flags=re.I
    )
    return re.sub(r'[\s,\.\-]+$', "", re.sub(r"\s+", " ", x).strip())


def infer_company_from_email(email: str) -> str:
    if not email or "@" not in email:
        return ""
    domain = email.split("@")[-1].lower()
    if domain in {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "live.com", "icloud.com"}:
        return ""
    tlds = {"com", "net", "org", "gov", "edu", "ac", "co", "io", "in", "ph", "jp", "sg", "my", "vn", "th", "uk", "us"}
    generic = {"apac", "emea", "latam", "na", "eu", "mail", "server", "web", "portal", "cloud", "www"}
    parts = [p for p in domain.split(".") if p not in tlds and p not in generic and len(p) > 2]
    return parts[0] if parts else ""


def _calculate_name_match_score(lead_name: str, title: str, desc: str) -> Tuple[int, List[str]]:
    if not lead_name:
        return 0, []
    target = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", lead_name.lower())).strip()
    parts = [x for x in target.split() if len(x) > 1]
    text = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", f"{title} {desc}".lower())).strip()
    if target and target in text:
        return 45, ["Exact full-name match found."]
    matched = sum(bool(re.search(rf"\b{re.escape(x)}\b", text)) for x in parts)
    if not parts:
        return 0, []
    ratio = matched / len(parts)
    if ratio >= 1.0:
        return 35, ["All name components matched."]
    if ratio >= 0.66:
        return 25, ["Most name components matched."]
    if ratio >= 0.5:
        return 15, ["Partial name match found."]
    return 0, ["Insufficient name evidence."]


def _detect_title_company_mismatch(company: str, title: str, desc: str = "") -> Tuple[bool, str]:
    if not company or company.strip().upper() == "N/A" or not title:
        return False, ""
    target = clean_company_name(company).lower().strip()
    if not target:
        return False, ""
    nt = re.sub(r"\s*\|\s*linkedin.*$", "", re.sub(r"\s+", " ", title.lower().strip()), flags=re.I)
    words = {x for x in re.findall(r"[a-z0-9]+", target) if len(x) > 2}
    compact = re.sub(r"[^a-z0-9]", "", target)
    if target in nt or compact in re.sub(r"[^a-z0-9]", "", nt):
        return False, ""
    
    # 1. Match explicit employer patterns: "at Company", "@ Company", "with Company", "working at Company"
    m = re.search(r"(?:\bat\b|\b@\b|\bwith\b|\bworking at\b|\bemployed at\b)\s+([a-z0-9&.,'’\- ]{2,80}?)(?=\s*(?:\||\-|\.|\,|$))", nt, re.I)
    if m:
        employer = m.group(1).strip(" .,-")
        ew = {x for x in re.findall(r"[a-z0-9]+", employer) if len(x) > 2}
        generic_roles = {"consulting", "engineering", "sales", "marketing", "management", "operations", "recruiting", "freelance", "self employed", "student", "stealth"}
        if ew and not words.intersection(ew) and not all(w in generic_roles for w in ew):
            return True, employer
            
    # 2. Split on separators: "Title - Company", "Title | Company"
    parts = [x.strip() for x in re.split(r"\s+-\s+|\s+\|\s+", nt) if x.strip()]
    if len(parts) >= 2:
        employer = parts[-1].strip(" .,-")
        ew = {x for x in re.findall(r"[a-z0-9]+", employer) if len(x) > 2}
        generic = {
            "linkedin", "profile", "professional", "resume", "cv", "contact", 
            "experience", "page", "jobs", "member", "open to work", "stealth",
            "consultant", "freelance", "self employed"
        }
        loc_words = {
            "philippines", "manila", "metro manila", "remote", "asia", "india", 
            "usa", "united states", "uk", "singapore", "dubai", "uae", "malaysia",
            "indonesia", "vietnam", "thailand", "germany", "france", "canada"
        }
        if ew and not ew.intersection(generic) and not any(loc in employer for loc in loc_words):
            if not words.intersection(ew):
                return True, employer
    return False, ""


def _calculate_company_match_score(company: str, email: str, title: str, desc: str) -> Tuple[int, List[str], bool, bool]:
    target = company or infer_company_from_email(email)
    if not target:
        return 0, ["No company info available."], False, False
    clean = clean_company_name(target).lower().strip()
    generic = {
        "industries", "industry", "solutions", "services", "technologies", "technology",
        "systems", "system", "group", "holdings", "holding", "corporation", "corp",
        "limited", "ltd", "incorporated", "inc", "global", "international",
        "associates", "partners", "consulting", "ventures", "capital"
    }
    words = [w for w in clean.split() if len(w) > 2]
    if not words or all(w in generic for w in words):
        return 0, [f"Company target '{target}' is too generic."], False, False
    tl = (title or "").lower()
    dl = (desc or "").lower()
    tn = re.sub(r"\s+", "", tl)
    dn = re.sub(r"\s+", "", dl)
    cn = re.sub(r"\s+", "", clean)
    if clean in tl or cn in tn:
        return 50, [f"Company '{target}' matched in title."], True, False
    matched_title = [w for w in words if w not in generic and (w in tl or w in tn)]
    if matched_title:
        return 40, ["Company keyword matched in title."], True, False

    # Check for explicit employer conflict in title before inspecting description
    mismatch, detected_emp = _detect_title_company_mismatch(company, title, desc)
    if mismatch:
        return 0, [f"Title explicitly specifies conflicting employer '{detected_emp}'."], False, True

    if clean in dl or cn in dn:
        return 45, [f"Company '{target}' matched in description."], True, False
    matched_desc = [w for w in words if w not in generic and (w in dl or w in dn)]
    if matched_desc:
        return 35, ["Company keyword matched in description."], True, False
    if email and "@" in email:
        domain_root = email.split("@", 1)[1].lower().split(".")[0]
        if domain_root not in {"gmail", "yahoo", "outlook", "hotmail", "live", "icloud"}:
            if domain_root in tl or domain_root in tn or domain_root in dl or domain_root in dn:
                return 40, [f"Email domain keyword '{domain_root}' matched."], True, False

    return 0, ["No company evidence."], False, False


def _calculate_email_domain_match_score(email: str, title: str, desc: str) -> Tuple[int, List[str]]:
    if not email or "@" not in email:
        return 0, []
    domain = email.split("@", 1)[1].lower().strip()
    if domain in {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "live.com", "icloud.com"}:
        return 0, []
    root = domain.split(".")[0]
    if root and root in (title or "").lower():
        return 10, ["Corporate email domain appears in title."]
    return 0, []


def _calculate_country_match_score(country: str, title: str, desc: str) -> Tuple[int, List[str]]:
    if country and country.strip().upper() != "N/A" and country.lower().strip() in f"{title} {desc}".lower():
        return 20, [f"Country match: {country}"]
    return 0, []


def _calculate_role_match_score(role: str, title: str, desc: str, posts_text: str = "") -> Tuple[int, List[str]]:
    if not role or role.strip().upper() == "N/A":
        return 0, []
    raw_text = f"{title} {desc} {posts_text}".lower()
    stop_words = {"and", "the", "for", "with", "from", "at", "to", "in", "of", "prospective", "buyer", "evaluator", "business"}
    words = [x for x in re.findall(r"[a-z0-9]+", role.lower()) if len(x) > 2 and x not in stop_words]
    if not words:
        # Fallback to general terms if nothing left
        words = [x for x in re.findall(r"[a-z0-9]+", role.lower()) if len(x) > 2]
    if not words:
        return 0, []
    
    matched = [w for w in words if w in raw_text]
    reasons = []
    score = 0
    if len(matched) >= 2 and len(matched) == len(words):
        score += 30
        reasons.append(f"Full role match: '{role}'")
    elif matched:
        score += min(len(matched) * 10, 25)
        reasons.append(f"Role keyword match: {', '.join(matched)}")

    # Corroborate with recent LinkedIn posts & shared updates
    if posts_text:
        posts_lower = posts_text.lower()
        post_matched = [w for w in words if w in posts_lower]
        if post_matched:
            score = min(score + 15, 35)
            reasons.append(f"Recent LinkedIn posts/activity align with role: {', '.join(post_matched)}")

    return score, reasons


def _calculate_email_company_signal(email: str, company: str, title: str, desc: str) -> Tuple[int, List[str]]:
    if not email or "@" not in email or not company:
        return 0, []
    root = email.split("@", 1)[1].lower().split(".")[0]
    words = re.findall(r"[a-z0-9]+", clean_company_name(company).lower())
    if root in words:
        return 15, [f"Email domain aligns with {company}."]
    return 0, []


def generate_deterministic_linkedin_slug_urls(name: str, email: str = None, country: str = None) -> List[str]:
    """
    Generates deterministic LinkedIn URL candidate variations from name, email username, and country.
    """
    if not name:
        return []
    slugs = []
    tokens = re.findall(r"[a-z0-9]+", name.lower())
    if tokens:
        slugs.append("-".join(tokens))
        slugs.append("".join(tokens))
        if len(tokens) >= 3:
            rest = "-".join(tokens[2:])
            slugs.append(f"{tokens[0]}{tokens[1]}-{rest}")
            slugs.append(f"{tokens[0]}-{rest}")
        elif len(tokens) == 2:
            slugs.append(f"{tokens[0]}-{tokens[1]}")
            slugs.append(f"{tokens[0][0]}-{tokens[1]}")
            slugs.append(f"{tokens[0]}{tokens[1]}")

    if email and "@" in email:
        u_raw = email.split("@", 1)[0].lower()
        u_tokens = re.findall(r"[a-z0-9]+", u_raw)
        if u_tokens:
            slugs.append("-".join(u_tokens))
            slugs.append("".join(u_tokens))

    slugs = list(dict.fromkeys(s for s in slugs if s and len(s) >= 3))
    urls = []
    prefixes = ["https://www.linkedin.com/in/"]
    if country:
        c_code = country.strip().lower()
        if c_code in {"ph", "ae", "in", "uk", "sg", "au", "ca", "de", "fr"}:
            prefixes.insert(0, f"https://{c_code}.linkedin.com/in/")

    for s in slugs:
        for p in prefixes:
            urls.append(f"{p}{s}")
    return list(dict.fromkeys(urls))


def build_linkedin_search_queries(name: str, company: str = None, role: str = None, country: str = None, email: str = None, interests: str = None) -> List[str]:
    n = sanitize_search_input(name)
    raw_c = sanitize_search_input(company)
    c = sanitize_search_input(clean_company_name(company))
    r = sanitize_search_input(role)
    co = sanitize_search_input(country)
    e = sanitize_search_input(email)
    q = []

    # Clean generic role terms
    stop_role_words = {"influencer", "recommender", "decision", "maker", "gatekeeper", "champion", "other", "prospective", "evaluator", "business", "buyer"}
    role_tokens = [w for w in re.findall(r"[a-z0-9]+", (r or "").lower()) if len(w) > 2 and w not in stop_role_words]
    clean_role = " ".join(role_tokens) if role_tokens else ""

    # 1. Country name expansion (e.g. PH -> Philippines)
    c_map = {
        "ph": "Philippines",
        "ae": "UAE",
        "in": "India",
        "us": "United States",
        "uk": "United Kingdom",
        "sg": "Singapore",
        "sa": "Saudi Arabia"
    }
    country_name = c_map.get((co or "").lower(), co or "")

    # Extract email username variations (e.g. JanMichael.Cruz -> JanMichael Cruz)
    user_part = ""
    if e and "@" in e:
        u_raw = e.split("@", 1)[0]
        user_part = re.sub(r"[._\-+]+", " ", u_raw).strip()

    # Build exact URL slug variations (e.g. jan-michael-cruz, janmichael-cruz)
    name_slug = "-".join(re.findall(r"[a-z0-9]+", n.lower()))
    user_slug = "-".join(re.findall(r"[a-z0-9]+", (user_part or "").lower()))
    
    # High-Yield Single Query (Max 1 targeted query per lead)
    if n and (c or raw_c):
        comp = c or raw_c
        if country_name and country_name.upper() != "N/A":
            q.append(f'site:linkedin.com/in/ "{n}" "{comp}" "{country_name}"')
        else:
            q.append(f'site:linkedin.com/in/ "{n}" "{comp}"')
    elif n:
        if (co or "").lower() in {"ae", "uae"}:
            q.append(f'site:ae.linkedin.com/in/ "{n}"')
        elif (co or "").lower() == "ph":
            q.append(f'site:ph.linkedin.com/in/ "{n}"')
        elif (co or "").lower() == "in":
            q.append(f'site:in.linkedin.com/in/ "{n}"')
        else:
            q.append(f'site:linkedin.com/in/ "{n}"')

    return list(dict.fromkeys(x for x in q if x))[:1]


def _call_search_api(query: str, count: int = 10) -> list:
    return call_search_api(query=query, count=count)


def _pre_rank_linkedin_candidates(results: list, lead_name: str, company: str = None, email: str = None, country: str = None, role: str = None) -> list:
    ranked = []
    seen = set()
    for item in results or []:
        if not isinstance(item, dict):
            continue
        url = (item.get("url") or "").strip()
        if not is_valid_linkedin_url(url):
            continue
        norm = normalize_url(url)
        if norm in seen:
            continue
        seen.add(norm)

        title = str(item.get("title") or "").strip()
        desc = str(item.get("description") or item.get("content") or "").strip()

        mismatch, detected_emp = _detect_title_company_mismatch(company, title, desc)
        if mismatch:
            logger.info("[LINKEDIN_DIAGNOSTIC] Candidate Disqualified (Conflicting Employer): URL=%s | Title='%s' | ConflictingEmployer='%s'", url, title, detected_emp)
            continue

        ns, nr = _calculate_name_match_score(lead_name, title, desc)
        if ns < 20:
            logger.info("[LINKEDIN_DIAGNOSTIC] Candidate Disqualified (Name Mismatch): URL=%s | Title='%s' | LeadName='%s'", url, title, lead_name)
            continue

        cs, cr, hc, extra = _calculate_company_match_score(company, email, title, desc)
        es, er = _calculate_email_domain_match_score(email, title, desc)
        cos, cor = _calculate_country_match_score(country, title, desc)
        rs, rr = _calculate_role_match_score(role, title, desc)

        has_target_company = bool(company and clean_company_name(company))
        clean_comp = clean_company_name(company).lower() if company else ""
        pre_score = ns + cs + es + cos + rs
        exact_title_match = False
        if clean_comp and clean_comp in title.lower():
            pre_score += 500
            exact_title_match = True
        elif hc:
            pre_score += 100
        elif has_target_company and not hc and es == 0:
            pre_score = ns

        logger.info(
            "[LINKEDIN_DIAGNOSTIC] Candidate Evaluated: URL=%s | Title='%s' | PreScore=%d | NameScore=%d | CompanyScore=%d | ExactTitleMatch=%s | Reasons=%s",
            url, title, pre_score, ns, cs, exact_title_match, (nr + cr + er + cor + rr)
        )

        ranked.append({
            "url": url,
            "title": title,
            "description": desc,
            "pre_score": pre_score,
            "name_score": ns,
            "company_score": cs,
            "has_company_match": hc,
            "reasons": nr + cr + er + cor + rr
        })

    return sorted(ranked, key=lambda x: x["pre_score"], reverse=True)


def _extract_profile_name(markdown: str) -> str:
    for line in (markdown or "").splitlines():
        x = line.strip().lstrip("#* ").strip()
        if x and 2 <= len(x.split()) <= 5 and not any(k in x.lower() for k in ("linkedin", "experience", "education", "skills", "about")):
            return x
    return ""


def _extract_profile_headline(markdown: str) -> str:
    name = _extract_profile_name(markdown)
    lines = [x.strip().lstrip("#* ").strip() for x in (markdown or "").splitlines() if x.strip()]
    if name in lines:
        i = lines.index(name)
        for x in lines[i + 1:i + 8]:
            if x and len(x) < 240 and not x.lower().startswith(("contact info", "experience", "education", "skills", "about")):
                return x
    return ""


def _extract_profile_location(markdown: str) -> str:
    m = re.search(r"(?im)^(?:location|based in|lives in)\s*[:\-]\s*(.+)$", markdown or "")
    return m.group(1).strip() if m else ""


def _profile_identity_score(profile: dict, lead_name: str, company: str = None, email: str = None, country: str = None, role: str = None) -> dict:
    raw = profile.get("raw_text") or ""
    title = profile.get("title") or ""
    location = profile.get("location") or ""
    
    # Extract posts and recent updates
    posts_list = profile.get("posts") or profile.get("recent_activity") or profile.get("activities") or []
    posts_text = ""
    if isinstance(posts_list, list):
        for p in posts_list:
            if isinstance(p, dict):
                posts_text += " " + str(p.get("text") or p.get("title") or p.get("message") or "")
            elif isinstance(p, str):
                posts_text += " " + p

    ns, nr = _calculate_name_match_score(lead_name, title, raw)
    cs, cr, hc, hm = _calculate_company_match_score(company, email, title, raw)
    es, er = _calculate_email_domain_match_score(email, title, raw)
    cos, cor = _calculate_country_match_score(country, location, raw)
    rs, rr = _calculate_role_match_score(role, title, raw, posts_text=posts_text)
    mismatch, detected = _detect_title_company_mismatch(company, title, raw)

    score = min(ns + cs + es + cos + rs, 100)
    if hc:
        score = min(score + 10, 100)

    has_target_company = bool(company and clean_company_name(company))

    if mismatch:
        score = 0
    elif has_target_company and not hc and es == 0:
        score = 0

    reasons = nr + cr + er + cor + rr + ([f"Explicit employer conflict: {detected}"] if mismatch else [])
    return {
        "score": score,
        "name_score": ns,
        "company_score": cs,
        "email_score": es,
        "country_score": cos,
        "role_score": rs,
        "has_company_match": hc,
        "mismatch": mismatch,
        "reasons": reasons
    }


async def scrape_linkedin_profile_async(url: str) -> dict:
    if not is_valid_linkedin_url(url):
        return {"success": False, "raw_text": "", "title": "", "location": "", "reason": "Invalid LinkedIn URL."}

    apify_token = (get_config("APIFY_API_TOKEN") or "").strip()
    apify_actor = (get_config("APIFY_ACTOR_ID") or "supreme_coder~linkedin-profile-scraper").strip()
    key = (get_config("BRIGHTDATA_API_KEY") or "").strip()
    ds = (get_config("BRIGHTDATA_DATASET_ID") or "").strip()

    # Tier 1 (Primary): Bright Data structured scrape
    if key and (ds or True):
        try:
            from brightdata_client import scrape_linkedin_with_brightdata, format_brightdata_linkedin_profile
            loop = asyncio.get_running_loop()
            profile = await loop.run_in_executor(None, lambda: scrape_linkedin_with_brightdata(url, api_key=key, dataset_id=ds))
            if isinstance(profile, dict) and not profile.get("error") and profile:
                formatted = format_brightdata_linkedin_profile(profile)
                name = profile.get("name") or profile.get("full_name") or f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip()
                headline = profile.get("headline") or profile.get("title") or profile.get("occupation") or ""
                location = profile.get("location") or profile.get("locationName") or profile.get("address") or ""
                logger.info("[SCRAPE] Bright Data (Primary) succeeded for %s (%d chars)", url, len(formatted))
                return {
                    "success": True,
                    "raw_text": formatted[:14000],
                    "title": f"{name} | {headline}" if headline else name,
                    "location": location,
                    "source": "brightdata",
                    **profile
                }
            else:
                err_msg = profile.get("error") if isinstance(profile, dict) else "Empty or invalid response"
                logger.warning("[SCRAPE] Bright Data error: %s", err_msg)
        except Exception as e:
            logger.warning("[SCRAPE] Bright Data failed: %s", e)

    # Tier 2 (Secondary Fallback): Apify Scraper
    if apify_token:
        try:
            from apify_client import scrape_linkedin_with_apify, format_apify_linkedin_profile
            loop = asyncio.get_running_loop()
            profile = await loop.run_in_executor(None, lambda: scrape_linkedin_with_apify(url, api_token=apify_token, actor_id=apify_actor))
            if isinstance(profile, dict) and not profile.get("error"):
                formatted = format_apify_linkedin_profile(profile)
                name = (
                    profile.get("fullName")
                    or profile.get("name")
                    or f"{profile.get('firstName', '')} {profile.get('lastName', '')}".strip()
                )
                headline = profile.get("headline") or profile.get("title") or profile.get("occupation") or ""
                location = profile.get("location") or profile.get("locationName") or profile.get("city") or ""
                logger.info("[SCRAPE] Apify fallback succeeded for %s (%d chars)", url, len(formatted))
                return {
                    "success": True,
                    "raw_text": formatted[:14000],
                    "title": f"{name} | {headline}" if headline else name,
                    "location": location,
                    "source": "apify",
                    **profile
                }
            else:
                err_msg = profile.get("error") if isinstance(profile, dict) else "Unknown error"
                logger.warning("[SCRAPE] Apify fallback error for %s: %s", url, err_msg)
        except Exception as ae:
            logger.warning("[SCRAPE] Apify fallback failed: %s", ae)

    # Tier 3 (Tertiary Fallback): Jina Reader public fallback (markdown extraction)
    try:
        import requests as _req
        if hasattr(_req, "get"):
            jina_url = f"https://r.jina.ai/{url}"
            resp = _req.get(jina_url, timeout=10, headers={"Accept": "text/plain", "X-Return-Format": "text"})
            if resp.status_code == 200 and len(resp.text) > 200:
                text = resp.text[:10000]
                slug = url.rstrip("/").split("/in/")[-1].lower().replace("-", " ")
                if slug and len(slug) > 3 and slug[:8] not in text.lower():
                    logger.warning("[SCRAPE] Jina result does not match profile slug — discarding.")
                else:
                    extracted_name = _extract_profile_name(text)
                    extracted_headline = _extract_profile_headline(text)
                    extracted_location = _extract_profile_location(text)
                    logger.info("[SCRAPE] Jina Reader fallback succeeded for %s (%d chars)", url, len(text))
                    return {
                        "success": True,
                        "raw_text": text,
                        "title": f"{extracted_name} | {extracted_headline}" if extracted_headline else extracted_name,
                        "location": extracted_location,
                        "source": "jina_reader",
                    }
            else:
                logger.warning("[SCRAPE] Jina Reader returned %s for %s", resp.status_code, url)
    except Exception as e:
        logger.warning("[SCRAPE] Jina Reader fallback failed: %s", e)

    return {
        "success": False,
        "raw_text": "",
        "title": "",
        "location": "",
        "reason": "All LinkedIn scraping methods failed (Bright Data not configured or blocked; Jina Reader returned no usable content)."
    }


async def resolve_linkedin_profile_async(results: list, lead_name: str, company: str = None, email: str = None, country: str = None, role: str = None, max_candidates: int = 4) -> dict:
    logger.info("[LINKEDIN_DIAGNOSTIC] Resolution Start: Lead='%s' | Company='%s' | TotalCandidatesInPool=%d", lead_name, company or "N/A", len(results or []))
    ranked = _pre_rank_linkedin_candidates(results, lead_name, company, email, country, role)

    # If Search discovery produced 0 valid candidates, fallback to direct Apify / Bright Data People Search
    if not ranked and lead_name:
        logger.info("[LINKEDIN_DIAGNOSTIC] Primary Search returned 0 matching candidates. Triggering direct Apify / Bright Data LinkedIn search fallback...")
        from apify_client import search_linkedin_with_apify
        from brightdata_client import search_linkedin_with_brightdata

        loop = asyncio.get_running_loop()
        direct_results = []
        try:
            apify_hits = await loop.run_in_executor(None, lambda: search_linkedin_with_apify(lead_name, company, country))
            for h in apify_hits or []:
                if isinstance(h, dict):
                    u = h.get("url") or h.get("linkedinUrl") or h.get("profileUrl")
                    if is_valid_linkedin_url(u):
                        direct_results.append({
                            "url": u,
                            "title": h.get("title") or h.get("headline") or f"{h.get('name', lead_name)} - {company or ''}",
                            "description": h.get("summary") or h.get("about") or str(h)
                        })
        except Exception as ae:
            logger.warning("[LINKEDIN_DIAGNOSTIC] Direct Apify search exception: %s", ae)

        if not direct_results:
            try:
                bd_hits = await loop.run_in_executor(None, lambda: search_linkedin_with_brightdata(lead_name, company))
                for h in bd_hits or []:
                    if isinstance(h, dict):
                        u = h.get("url") or h.get("linkedinUrl") or h.get("profileUrl")
                        if is_valid_linkedin_url(u):
                            direct_results.append({
                                "url": u,
                                "title": h.get("name") or h.get("headline") or f"{lead_name} - {company or ''}",
                                "description": h.get("summary") or str(h)
                            })
            except Exception as bde:
                logger.warning("[LINKEDIN_DIAGNOSTIC] Direct Bright Data search exception: %s", bde)

        if direct_results:
            logger.info("[LINKEDIN_DIAGNOSTIC] Direct LinkedIn search returned %d candidate(s). Re-ranking...", len(direct_results))
            ranked = _pre_rank_linkedin_candidates(direct_results, lead_name, company, email, country, role)

    if not ranked:
        fail_msg = "No valid LinkedIn candidates remained after mismatch filtering." if results else "Google Search returned 0 LinkedIn profile results."
        logger.warning("[LINKEDIN_DIAGNOSTIC] Resolution Failed: %s", fail_msg)
        return {"linkedin_url": "", "confidence_score": 0, "profile": {}, "scraped_candidates": [], "reason": fail_msg}

    logger.info("[LINKEDIN_DIAGNOSTIC] Ranked Candidates Pool: %d candidate(s)", len(ranked))
    for idx, cand in enumerate(ranked):
        logger.info("[LINKEDIN_DIAGNOSTIC] Candidate #%d: URL=%s | PreScore=%d | Title='%s'", idx + 1, cand["url"], cand["pre_score"], cand["title"])

    verified = []
    scraped_candidates = []
    # Prioritize candidates with plausible pre-scores to eliminate timeout cascades
    high_potential = [c for c in ranked if c.get("pre_score", 0) >= 45]
    scrape_pool = high_potential[:2] if high_potential else ranked[:1]

    for candidate in scrape_pool:
        logger.info("[LINKEDIN_DIAGNOSTIC] Attempting live scrape for candidate: %s (PreScore=%d)", candidate["url"], candidate.get("pre_score", 0))
        scraped = await scrape_linkedin_profile_async(candidate["url"])
        if not scraped.get("success"):
            logger.info("[LINKEDIN_DIAGNOSTIC] Scraper authwall/failure for %s: Reason='%s'", candidate["url"], scraped.get("reason"))
            scraped_candidates.append({
                "url": candidate["url"],
                "status": "not_parsed",
                "reason": scraped.get("reason"),
                "search_evidence": candidate
            })
            continue
        # Merge search snippet context into live scraped profile to guarantee company match preservation
        enriched_scraped = dict(scraped)
        if not enriched_scraped.get("title") or enriched_scraped.get("title") in {"LinkedIn", "LinkedIn: Log In or Sign Up"}:
            enriched_scraped["title"] = candidate.get("title", "")
        cand_text = f"{candidate.get('title', '')}\n{candidate.get('description', '')}"
        enriched_scraped["raw_text"] = f"{enriched_scraped.get('raw_text', '')}\n{cand_text}".strip()

        identity = _profile_identity_score(enriched_scraped, lead_name, company, email, country, role)
        
        # If candidate had exact title / company match verified by search index:
        if candidate.get("exact_title_match") or (candidate.get("has_company_match") and candidate.get("name_score", 0) >= 40):
            identity["has_company_match"] = True
            identity["company_score"] = max(identity.get("company_score", 0), 50)
            identity["score"] = max(identity.get("score", 0), 95)
            identity["reasons"].append("Corporate title and employer verified via search engine index.")

        record = {
            "url": candidate["url"],
            "status": "parsed",
            "score": identity["score"],
            "identity": identity,
            "profile": enriched_scraped,
            "search_evidence": candidate
        }
        scraped_candidates.append(record)
        logger.info("[LINKEDIN_DIAGNOSTIC] Live Scraped Profile Identity Score: URL=%s | Score=%d | Reasons=%s", candidate["url"], identity["score"], identity["reasons"])
        if identity["mismatch"] or identity["name_score"] < 15:
            logger.info("[LINKEDIN_DIAGNOSTIC] Discarded live scraped profile due to mismatch / low name score.")
            continue
        if company and company.strip().upper() != "N/A" and (not identity["has_company_match"] or identity["score"] < 45):
            logger.info("[LINKEDIN_DIAGNOSTIC] Discarded live scraped profile due to missing company match / low score: Score=%d | HasCompanyMatch=%s", identity["score"], identity["has_company_match"])
            continue
        verified.append({"candidate": candidate, "scraped": enriched_scraped, "identity": identity})

    # Solution 2: Deterministic Slug Probing (Email-to-Slug & Name-to-Slug Direct Resolution)
    if not verified and lead_name:
        slug_urls = generate_deterministic_linkedin_slug_urls(lead_name, email, country)
        logger.info("[LINKEDIN_DIAGNOSTIC] Generated %d deterministic slug candidate(s). Probing live...", len(slug_urls))
        for s_url in slug_urls[:6]:
            logger.info("[LINKEDIN_DIAGNOSTIC] Probing deterministic slug URL: %s", s_url)
            scraped = await scrape_linkedin_profile_async(s_url)
            if scraped.get("success"):
                identity = _profile_identity_score(scraped, lead_name, company, email, country, role)
                if not identity["mismatch"] and identity["name_score"] >= 20 and (identity["has_company_match"] or identity["company_score"] > 0):
                    logger.info("[LINKEDIN_DIAGNOSTIC] Deterministic slug probing SUCCEEDED for %s (IdentityScore=%d)", s_url, identity["score"])
                    verified.append({
                        "candidate": {"url": s_url, "title": scraped.get("title", lead_name), "pre_score": 100},
                        "scraped": scraped,
                        "identity": identity
                    })
                    break

    # Option B: Google Search API Fallback (Queries Google's Global Index via Serper)
    if not verified and lead_name:
        from client_utils import call_google_serper_search_api_async
        google_queries = [
            f'site:linkedin.com/in/ "{lead_name}" "{company or ""}"'.strip(),
            f'"{lead_name}" "{company or ""}" site:linkedin.com'.strip(),
            f'"{lead_name}" "{company or ""}" "{country or ""}" site:linkedin.com'.strip()
        ]
        google_candidates = []
        for gq in google_queries:
            if not gq:
                continue
            g_hits = await call_google_serper_search_api_async(gq, count=10)
            for gh in g_hits:
                u = gh.get("url", "")
                if is_valid_linkedin_url(u):
                    google_candidates.append(gh)
            if google_candidates:
                break
                
        if google_candidates:
            logger.info("[LINKEDIN_DIAGNOSTIC] Google Search API (Serper) discovered %d candidate(s). Evaluating...", len(google_candidates))
            g_ranked = _pre_rank_linkedin_candidates(google_candidates, lead_name, company, email, country, role)
            for candidate in g_ranked[:3]:
                ranked.insert(0, candidate)
                logger.info("[LINKEDIN_DIAGNOSTIC] Live scraping Google Search candidate: %s", candidate["url"])
                scraped = await scrape_linkedin_profile_async(candidate["url"])
                if scraped.get("success"):
                    identity = _profile_identity_score(scraped, lead_name, company, email, country, role)
                    if not identity["mismatch"] and identity["name_score"] >= 20:
                        verified.append({"candidate": candidate, "scraped": scraped, "identity": identity})
                        break
                elif candidate.get("has_company_match") and candidate.get("name_score", 0) >= 20:
                    mock_profile = {
                        "raw_text": f"{candidate.get('title', '')}\n{candidate.get('description', '')}",
                        "title": candidate.get("title", ""),
                        "location": "",
                        "source": "google_search_snippet"
                    }
                    identity = {
                        "score": 90,
                        "has_company_match": True,
                        "name_score": candidate.get("name_score", 45),
                        "company_score": 40,
                        "reasons": ["Verified corporate identity via Google Search index snippet."]
                    }
                    verified.append({"candidate": candidate, "scraped": mock_profile, "identity": identity})
                    logger.info("[LINKEDIN_DIAGNOSTIC] Verified Google Search candidate via search snippet: %s", candidate["url"])
                    break

    if not verified and lead_name:
        logger.info("[LINKEDIN_DIAGNOSTIC] Web search produced no company-verified match. Triggering direct Apify / Bright Data LinkedIn search fallback...")
        from apify_client import search_linkedin_with_apify
        from brightdata_client import search_linkedin_with_brightdata

        loop = asyncio.get_running_loop()
        direct_results = []
        try:
            apify_hits = await loop.run_in_executor(None, lambda: search_linkedin_with_apify(lead_name, company, country))
            for h in apify_hits or []:
                if isinstance(h, dict):
                    u = h.get("url") or h.get("linkedinUrl") or h.get("profileUrl")
                    if is_valid_linkedin_url(u):
                        direct_results.append({
                            "url": u,
                            "title": h.get("title") or h.get("headline") or f"{h.get('name', lead_name)} - {company or ''}",
                            "description": h.get("summary") or h.get("about") or str(h)
                        })
        except Exception as ae:
            logger.warning("[LINKEDIN_DIAGNOSTIC] Direct Apify search exception: %s", ae)

        if not direct_results:
            try:
                bd_hits = await loop.run_in_executor(None, lambda: search_linkedin_with_brightdata(lead_name, company))
                for h in bd_hits or []:
                    if isinstance(h, dict):
                        u = h.get("url") or h.get("linkedinUrl") or h.get("profileUrl")
                        if is_valid_linkedin_url(u):
                            direct_results.append({
                                "url": u,
                                "title": h.get("name") or h.get("headline") or f"{lead_name} - {company or ''}",
                                "description": h.get("summary") or str(h)
                            })
            except Exception as bde:
                logger.warning("[LINKEDIN_DIAGNOSTIC] Direct Bright Data search exception: %s", bde)

        if direct_results:
            logger.info("[LINKEDIN_DIAGNOSTIC] Direct LinkedIn search returned %d candidate(s). Evaluating...", len(direct_results))
            direct_ranked = _pre_rank_linkedin_candidates(direct_results, lead_name, company, email, country, role)
            for candidate in direct_ranked[:3]:
                ranked.insert(0, candidate)
                logger.info("[LINKEDIN_DIAGNOSTIC] Live scraping direct search candidate: %s", candidate["url"])
                scraped = await scrape_linkedin_profile_async(candidate["url"])
                if scraped.get("success"):
                    identity = _profile_identity_score(scraped, lead_name, company, email, country, role)
                    if not identity["mismatch"] and identity["name_score"] >= 20:
                        verified.append({"candidate": candidate, "scraped": scraped, "identity": identity})
                        break
                elif candidate.get("has_company_match") and candidate.get("name_score", 0) >= 20:
                    mock_profile = {
                        "raw_text": f"{candidate.get('title', '')}\n{candidate.get('description', '')}",
                        "title": candidate.get("title", ""),
                        "location": "",
                        "source": "direct_search_snippet"
                    }
                    identity = {
                        "score": 90,
                        "has_company_match": True,
                        "name_score": candidate.get("name_score", 45),
                        "company_score": 40,
                        "reasons": ["Verified corporate identity via direct search candidate."]
                    }
                    verified.append({"candidate": candidate, "scraped": mock_profile, "identity": identity})
                    break

    if not verified:
        logger.info("[LINKEDIN_DIAGNOSTIC] Live scrapers blocked/unavailable. Checking search index snippet fallback...")
        clean_comp = clean_company_name(company).lower() if company else ""
        for cand in ranked:
            has_co = cand.get("has_company_match") or (clean_comp and clean_comp in cand.get("title", "").lower())
            if has_co and cand.get("name_score", 0) >= 20:
                mock_profile = {
                    "raw_text": f"{cand.get('title', '')}\n{cand.get('description', '')}",
                    "title": cand.get("title", ""),
                    "location": "",
                    "source": "search_snippet"
                }
                logger.info("[LINKEDIN_DIAGNOSTIC] Selected Search Snippet Profile: URL=%s | Title='%s' | Confidence=85", cand["url"], cand["title"])
                return {
                    "linkedin_url": cand["url"],
                    "confidence_score": 85,
                    "profile": mock_profile,
                    "scraped_candidates": scraped_candidates,
                    "reason": f"Verified LinkedIn Profile via search index ({cand.get('title')})"
                }
        fail_msg = "No candidates met verification threshold after evaluating name and company matches."
        logger.warning("[LINKEDIN_DIAGNOSTIC] Resolution Failed: %s", fail_msg)
        return {"linkedin_url": "", "confidence_score": 0, "profile": {}, "scraped_candidates": scraped_candidates, "reason": fail_msg}

    verified.sort(
        key=lambda x: (
            1 if x["identity"].get("has_company_match") else 0,
            x["identity"].get("company_score", 0),
            x["identity"].get("score", 0),
            x["identity"].get("name_score", 0)
        ),
        reverse=True
    )
    best = verified[0]
    score = best["identity"]["score"]
    if score < 45 and not best["identity"].get("has_company_match"):
        fail_msg = f"Candidate score ({score}) below required threshold (45)."
        logger.warning("[LINKEDIN_DIAGNOSTIC] Resolution Failed: %s", fail_msg)
        return {"linkedin_url": "", "confidence_score": score, "profile": {}, "scraped_candidates": scraped_candidates, "reason": fail_msg}

    profile = dict(best["scraped"])
    profile["identity_score"] = score
    profile["identity_reasons"] = best["identity"]["reasons"]
    profile["pre_search_evidence"] = best["candidate"]
    logger.info("[LINKEDIN_DIAGNOSTIC] Resolution Succeeded (Live Scrape): URL=%s | Score=%d", best["candidate"]["url"], score)
    return {
        "linkedin_url": best["candidate"]["url"],
        "confidence_score": score,
        "profile": profile,
        "scraped_candidates": scraped_candidates,
        "reason": "Verified LinkedIn Profile"
    }


def resolve_linkedin_profile(results: list, lead_name: str, company: str = None, email: str = None, country: str = None, role: str = None, max_candidates: int = 8) -> dict:
    try:
        return asyncio.run(resolve_linkedin_profile_async(results, lead_name, company, email, country, role, max_candidates))
    except RuntimeError as e:
        if "asyncio.run() cannot be called" not in str(e):
            raise
        box = {}

        def runner():
            box["v"] = asyncio.run(resolve_linkedin_profile_async(results, lead_name, company, email, country, role, max_candidates))

        t = __import__("threading").Thread(target=runner)
        t.start()
        t.join()
        return box.get("v", {"linkedin_url": "", "confidence_score": 0, "profile": {}, "reason": "Resolver thread failed."})


def search_lead(lead_name: str, company: str = None, email: str = None, country: str = None, role: str = None, interests: str = None, message: str = None, count: int = 10) -> dict:
    name = sanitize_search_input(lead_name)
    comp = sanitize_search_input(clean_company_name(company)) or sanitize_search_input(infer_company_from_email(email))
    country_map = {"ae": "UAE", "uae": "UAE", "united arab emirates": "UAE", "in": "India", "india": "India", "us": "USA", "usa": "USA", "uk": "UK"}
    country_clean = country_map.get(country.strip().lower(), country.strip()) if country and country.strip().upper() != "N/A" else ""
    queries = build_linkedin_search_queries(name, comp, role, country_clean, email, interests)
    results = []
    seen = set()
    for query in queries:
        try:
            items = _call_search_api(query, count=max(count, 10))
        except Exception as e:
            logger.warning("[LINKEDIN] Search failed: %s", e)
            continue
        for item in items or []:
            url = (item.get("url") or "").strip() if isinstance(item, dict) else ""
            if is_valid_linkedin_url(url) and normalize_url(url) not in seen:
                seen.add(normalize_url(url))
                results.append(item)

    resolution = resolve_linkedin_profile(results, name, comp, email, country_clean, role)
    final_url = resolution.get("linkedin_url", "")
    for x in resolution.get("scraped_candidates", []):
        if x.get("status") == "parsed":
            p = x.get("profile") or {}
            u = x.get("url") or ""
            if u:
                results.append({
                    "url": u,
                    "title": p.get("title") or name,
                    "description": p.get("raw_text", ""),
                    "content": p.get("raw_text", ""),
                    "source": "linkedin_scraper",
                    "linkedin_identity_score": x.get("score", 0),
                    "linkedin_identity_reasons": (x.get("identity") or {}).get("reasons", []),
                    "verified": u == final_url
                })
    reason = resolution.get("reason", "Success") if final_url else resolution.get("reason", "LinkedIn identity verification failed.")
    return {
        "results": results,
        "linkedin_url": final_url,
        "confidence_score": resolution.get("confidence_score", 0),
        "linkedin_profile": resolution.get("profile", {}),
        "reason": reason
    }


async def search_lead_async(lead_name: str, company: str = None, email: str = None, country: str = None, role: str = None, interests: str = None, message: str = None, count: int = 10) -> dict:
    name = sanitize_search_input(lead_name)
    comp = sanitize_search_input(clean_company_name(company)) or sanitize_search_input(infer_company_from_email(email))
    country_map = {"ae": "UAE", "uae": "UAE", "united arab emirates": "UAE", "in": "India", "india": "India", "us": "USA", "usa": "USA", "uk": "UK"}
    country_clean = country_map.get(country.strip().lower(), country.strip()) if country and country.strip().upper() != "N/A" else ""
    queries = build_linkedin_search_queries(name, comp, role, country_clean, email, interests)
    results = []
    seen = set()
    for query in queries:
        try:
            items = await call_search_api_async(query, count=max(count, 10))
        except Exception as e:
            logger.warning("[LINKEDIN] Async search failed: %s", e)
            continue
        for item in items or []:
            url = (item.get("url") or "").strip() if isinstance(item, dict) else ""
            if is_valid_linkedin_url(url) and normalize_url(url) not in seen:
                seen.add(normalize_url(url))
                results.append(item)

    resolution = await resolve_linkedin_profile_async(results, name, comp, email, country_clean, role)
    final_url = resolution.get("linkedin_url", "")
    if final_url:
        p = resolution.get("profile") or {}
        results.append({
            "url": final_url,
            "title": p.get("title") or name,
            "description": p.get("raw_text", ""),
            "content": p.get("raw_text", ""),
            "source": "linkedin_scraper",
            "verified": True
        })
    return {
        "results": results,
        "linkedin_url": final_url,
        "confidence_score": resolution.get("confidence_score", 0),
        "linkedin_profile": resolution.get("profile", {}),
        "reason": resolution.get("reason", "Success")
    }


def extract_linkedin_profile_url(results: list, lead_name: str, clean_company: str = None, email: str = None, country: str = None, role: str = None, phone: str = None) -> str:
    if not results or not lead_name:
        return ""
    candidates = []
    for item in results:
        if not isinstance(item, dict):
            continue
        url = (item.get("url") or "").strip()
        if is_valid_linkedin_url(url):
            candidates.append(item)
    if not candidates:
        return ""
    scored = []
    for item in candidates:
        title = item.get("title", "")
        desc = item.get("description") or item.get("content") or ""
        mismatch, _ = _detect_title_company_mismatch(clean_company, title, desc)
        if mismatch:
            continue
        ns, nr = _calculate_name_match_score(lead_name, title, desc)
        cs, cr, hc, _ = _calculate_company_match_score(clean_company, email, title, desc)
        es, er = _calculate_email_domain_match_score(email, title, desc)
        cos, cor = _calculate_country_match_score(country, title, desc)
        rs, rr = _calculate_role_match_score(role, title, desc)
        if ns < 15:
            continue
        score = min(ns + cs + es + cos + rs + (10 if hc and ns >= 40 else 0), 100)
        scored.append((score, item))
    if not scored:
        return ""
    scored.sort(key=lambda x: x[0], reverse=True)
    if scored[0][0] < 50:
        return ""
    if len(scored) > 1 and scored[0][0] - scored[1][0] < 5:
        return ""
    return scored[0][1]["url"]


async def extract_linkedin_profile_url_async(results: list, lead_name: str, clean_company: str = None, email: str = None, country: str = None, role: str = None, phone: str = None) -> str:
    return extract_linkedin_profile_url(results, lead_name, clean_company, email, country, role, phone)


def verify_linkedin_confidence(profile_url: str, name: str, company: str, email: str, item: dict = None, role: str = None, country: str = None, threshold: int = 45) -> dict:
    if not profile_url:
        return {"score": 0, "confidence": "Low", "reasons": ["No profile URL resolved."], "needs_review": True}
    title = item.get("title", "") if isinstance(item, dict) else ""
    desc = item.get("description", "") if isinstance(item, dict) else ""
    ns, nr = _calculate_name_match_score(name, title, desc)
    cs, cr, hc, _ = _calculate_company_match_score(company, email, title, desc)
    es, er = _calculate_email_domain_match_score(email, title, desc)
    cos, cor = _calculate_country_match_score(country, title, desc)
    rs, rr = _calculate_role_match_score(role, title, desc)
    score = ns + cs + es + cos + rs
    confidence = "High" if score >= 55 else "Medium" if score >= 35 else "Low"
    return {
        "score": score,
        "confidence": confidence,
        "reasons": nr + cr + er + cor + rr,
        "needs_review": score < threshold
    }


def verify_linkedin_confidence_ai(results: list, lead_name: str, company: str = None, email: str = None, country: str = None, role: str = None) -> str:
    if not results:
        return ""
    candidates = [{"index": i, "url": x.get("url", ""), "title": x.get("title", ""), "description": x.get("description", "")} for i, x in enumerate(results) if isinstance(x, dict)]
    payload = {
        "action": "synthesize",
        "system_prompt": "Select the correct LinkedIn profile. Return JSON only with selected_url, confidence_score and reason. Never invent a URL.",
        "user_prompt": json.dumps({"name": lead_name, "company": company, "email": email, "country": country, "role": role, "candidates": candidates}),
        "context": json.dumps({"name": lead_name, "company": company, "email": email, "country": country, "role": role, "candidates": candidates}),
        "max_tokens": 1000,
        "json_schema": {
            "name": "ai_linkedin_resolver",
            "schema": {
                "type": "object",
                "properties": {
                    "selected_url": {"type": "string"},
                    "confidence_score": {"type": "integer"},
                    "reason": {"type": "string"}
                },
                "required": ["selected_url", "confidence_score", "reason"]
            }
        }
    }
    try:
        data = call_cloudflare_worker_endpoint(payload)
        if isinstance(data, str):
            data = json.loads(data)
        if isinstance(data, dict):
            r = model_validate_compat(AIResolverResponse, data)
            if r.selected_url.upper() != "N/A" and is_valid_linkedin_url(r.selected_url) and r.confidence_score >= 60:
                return r.selected_url
    except Exception as e:
        logger.warning("[AI] LinkedIn resolution failed: %s", e)
    return ""


async def verify_linkedin_confidence_ai_async(results: list, lead_name: str, company: str = None, email: str = None, country: str = None, role: str = None) -> str:
    try:
        from client_utils import call_cloudflare_worker_endpoint_async
        if not results:
            return ""
        candidates = [{"index": i, "url": x.get("url", ""), "title": x.get("title", ""), "description": x.get("description", "")} for i, x in enumerate(results) if isinstance(x, dict)]
        prompt = json.dumps({"name": lead_name, "company": company, "email": email, "country": country, "role": role, "candidates": candidates})
        data = await call_cloudflare_worker_endpoint_async({
            "action": "synthesize",
            "system_prompt": "Select the correct LinkedIn profile. Return JSON only with selected_url, confidence_score and reason. Never invent a URL.",
            "user_prompt": prompt,
            "context": prompt,
            "max_tokens": 1000,
            "json_schema": {
                "name": "ai_linkedin_resolver",
                "schema": {
                    "type": "object",
                    "properties": {
                        "selected_url": {"type": "string"},
                        "confidence_score": {"type": "integer"},
                        "reason": {"type": "string"}
                    },
                    "required": ["selected_url", "confidence_score", "reason"]
                }
            }
        })
        if isinstance(data, str):
            data = json.loads(data)
        if isinstance(data, dict):
            r = model_validate_compat(AIResolverResponse, data)
            if r.selected_url.upper() != "N/A" and is_valid_linkedin_url(r.selected_url) and r.confidence_score >= 60:
                return r.selected_url
    except Exception as e:
        logger.warning("[AI] Async LinkedIn resolution failed: %s", e)
    return ""

resolve_lead_linkedin_profile = resolve_linkedin_profile
