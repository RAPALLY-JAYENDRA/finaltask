import os
from dotenv import load_dotenv

load_dotenv()

def get_config(key: str, default=None):
    value = os.getenv(key, default)
    if isinstance(value, str):
        value = value.strip()
    return value

def is_worker_url_configured():
    return bool(
        get_config("CLOUDFLARE_WORKER_URL") or get_config("CF_WORKER_URL") or get_config("WORKER_URL")
    )

def is_google_cse_configured():
    api_key = (
        get_config("GOOGLE_API_KEY_0")
        or get_config("GOOGLE_API_KEY")
        or get_config("GOOGLE_CUSTOM_SEARCH_KEY")
    )
    cse_id = (
        get_config("GOOGLE_CSE_ID_0")
        or get_config("GOOGLE_CSE_ID")
        or get_config("GOOGLE_CX")
        or get_config("GOOGLE_SEARCH_CX")
    )
    return bool(api_key and cse_id)

def is_serp_configured():
    return bool(
        get_config("SERPER_API_KEY")
        or get_config("SERPAPI_API_KEY")
        or get_config("SERP_API_KEY")
        or get_config("GOOGLE_SERPER_API_KEY")
    )

def validate_config():
    required = ["CLOUDFLARE_WORKER_URL"]
    recommended = [
        "GOOGLE_API_KEY_0",
        "GOOGLE_CSE_ID_0",
        "SERPER_API_KEY",
        "APIFY_API_TOKEN",
        "BRIGHTDATA_API_KEY",
        "BRIGHTDATA_DATASET_ID",
    ]
    optional = [
        "GOOGLE_API_KEY",
        "GOOGLE_CSE_ID",
        "SERPAPI_API_KEY",
        "SERP_API_KEY",
        "APIFY_ACTOR_ID",
        "CF_WORKER_AUTH_SECRET",
        "DATABASE_URL",
        "APP_USERNAME",
        "APP_PASSWORD",
    ]

    result = {
        "valid": True,
        "missing_required": [],
        "missing_recommended": [],
        "missing_optional": [],
    }

    for key in required:
        if not get_config(key):
            result["missing_required"].append(key)

    if not is_google_cse_configured() and not is_serp_configured():
        result["missing_required"].append("GOOGLE_API_KEY_0 / GOOGLE_CSE_ID_0 (or SERPER_API_KEY)")

    for key in recommended:
        if not get_config(key) and key not in result["missing_required"]:
            result["missing_recommended"].append(key)

    for key in optional:
        if not get_config(key):
            result["missing_optional"].append(key)

    if result["missing_required"]:
        result["valid"] = False

    return result
