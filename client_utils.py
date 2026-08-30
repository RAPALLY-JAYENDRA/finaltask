import asyncio
import logging
import random
import time
import httpx
from config import get_config

logger=logging.getLogger(__name__)

def model_validate_compat(model_cls,data):
    if hasattr(model_cls,"model_validate"): return model_cls.model_validate(data)
    return model_cls.parse_obj(data)

def model_dump_compat(model_inst):
    if hasattr(model_inst,"model_dump"): return model_inst.model_dump()
    return model_inst.dict()

def _get_worker_config():
    worker_url=get_config("CLOUDFLARE_WORKER_URL")
    if not worker_url: raise ValueError("CLOUDFLARE_WORKER_URL is missing.")
    worker_url=str(worker_url).strip()
    if not worker_url.startswith(("http://","https://")): raise ValueError("CLOUDFLARE_WORKER_URL must start with http:// or https://")
    auth_secret=get_config("CF_WORKER_AUTH_SECRET")
    headers={"Content-Type":"application/json","Accept":"application/json"}
    if auth_secret: headers["Authorization"]=f"Bearer {auth_secret}"
    return worker_url,headers

def _validate_worker_payload(payload):
    if not isinstance(payload,dict): raise TypeError(f"Cloudflare Worker payload must be a dict. Received: {type(payload).__name__}")
    required=["action","system_prompt","user_prompt"]
    missing=[field for field in required if not payload.get(field)]
    if missing: raise ValueError(f"Cloudflare Worker payload is missing required fields: {missing}. Received keys: {list(payload.keys())}")
    if payload.get("action")!="synthesize": raise ValueError(f"Invalid Cloudflare Worker action. Expected 'synthesize', received {payload.get('action')!r}")
    payload.setdefault("max_tokens",2500)
    payload.setdefault("model","@cf/meta/llama-3.1-8b-instruct")
    return payload

def _log_worker_request(worker_url,payload,headers):
    logger.info("WORKER REQUEST | url=%s | method=POST | payload_keys=%s | auth=%s | action=%s | model=%s | max_tokens=%s | system_prompt=%s | user_prompt=%s",worker_url,list(payload.keys()),"configured" if "Authorization" in headers else "not_configured",payload.get("action"),payload.get("model"),payload.get("max_tokens"),bool(payload.get("system_prompt")),bool(payload.get("user_prompt")))

def _log_worker_response(response,elapsed,prefix="WORKER"):
    logger.info("%s RESPONSE | status=%s | latency=%.2fs | text=%s",prefix,response.status_code,elapsed,response.text[:1000])

def _parse_worker_response(response):
    try: data=response.json()
    except ValueError: return {"raw_text":response.text}
    return data if isinstance(data,dict) else {"data":data}

def call_cloudflare_worker_endpoint(payload,max_retries=3,initial_delay=1.0):
    worker_url,headers=_get_worker_config()
    payload=_validate_worker_payload(payload)
    _log_worker_request(worker_url,payload,headers)
    delay=initial_delay
    last_error=None
    with httpx.Client(timeout=45.0) as client:
        for attempt in range(max_retries):
            started=time.perf_counter()
            try:
                logger.info("WORKER | request | attempt=%s/%s",attempt+1,max_retries)
                response=client.post(worker_url,json=payload,headers=headers)
                elapsed=time.perf_counter()-started
                _log_worker_response(response,elapsed)
                if response.status_code==200: return _parse_worker_response(response)
                if response.status_code in (429,502,503,504):
                    logger.warning("WORKER | transient_status=%s | body=%s",response.status_code,response.text[:1000])
                else:
                    raise RuntimeError(f"Cloudflare Worker returned {response.status_code}: {response.text[:1000]}")
            except Exception as exc:
                last_error=exc
                logger.exception("WORKER | attempt_failed | attempt=%s",attempt+1)
            if attempt<max_retries-1:
                sleep_time=delay*(1+0.1*random.random())
                logger.info("WORKER | retry_wait=%.2fs",sleep_time)
                time.sleep(sleep_time)
                delay*=2
    raise RuntimeError(f"Cloudflare Worker call failed after {max_retries} attempts. Last error: {last_error}")

async def call_cloudflare_worker_endpoint_async(payload,max_retries=3,initial_delay=1.0):
    worker_url,headers=_get_worker_config()
    payload=_validate_worker_payload(payload)
    _log_worker_request(worker_url,payload,headers)
    delay=initial_delay
    last_error=None
    async with httpx.AsyncClient(timeout=45.0) as client:
        for attempt in range(max_retries):
            started=time.perf_counter()
            try:
                logger.info("WORKER_ASYNC | request | attempt=%s/%s",attempt+1,max_retries)
                response=await client.post(worker_url,json=payload,headers=headers)
                elapsed=time.perf_counter()-started
                _log_worker_response(response,elapsed,"WORKER_ASYNC")
                if response.status_code==200: return _parse_worker_response(response)
                if response.status_code in (429,502,503,504):
                    logger.warning("WORKER_ASYNC | transient_status=%s | body=%s",response.status_code,response.text[:1000])
                else:
                    raise RuntimeError(f"Cloudflare Worker returned {response.status_code}: {response.text[:1000]}")
            except Exception as exc:
                last_error=exc
                logger.exception("WORKER_ASYNC | attempt_failed | attempt=%s",attempt+1)
            if attempt<max_retries-1:
                sleep_time=delay*(1+0.1*random.random())
                logger.info("WORKER_ASYNC | retry_wait=%.2fs",sleep_time)
                await asyncio.sleep(sleep_time)
                delay*=2
    raise RuntimeError(f"Cloudflare Worker async call failed after {max_retries} attempts. Last error: {last_error}")

def _parse_retry_after(response):
    value=response.headers.get("Retry-After")
    if not value: return 0.0
    try: return float(value)
    except ValueError: return 2.0

_SEARCH_REQUESTS_LOG: list = []

def reset_search_request_metrics() -> None:
    global _SEARCH_REQUESTS_LOG
    _SEARCH_REQUESTS_LOG.clear()

def get_search_request_metrics() -> list:
    global _SEARCH_REQUESTS_LOG
    return list(_SEARCH_REQUESTS_LOG)

def get_search_request_count() -> int:
    global _SEARCH_REQUESTS_LOG
    return len(_SEARCH_REQUESTS_LOG)

def get_search_request_breakdown() -> dict:
    """
    Returns counts and active provider status for Google API Key vs Serp API.
    """
    global _SEARCH_REQUESTS_LOG
    google_cse_calls = [r for r in _SEARCH_REQUESTS_LOG if "CSE" in r.get("provider", "") or "Google Official" in r.get("provider", "")]
    serp_api_calls = [r for r in _SEARCH_REQUESTS_LOG if "Serp" in r.get("provider", "")]
    
    google_api_key = (
        get_config("GOOGLE_API_KEY_0")
        or get_config("GOOGLE_API_KEY")
        or get_config("GOOGLE_CUSTOM_SEARCH_KEY")
    )
    google_cse_id = (
        get_config("GOOGLE_CSE_ID_0")
        or get_config("GOOGLE_CSE_ID")
        or get_config("GOOGLE_CX")
        or get_config("GOOGLE_SEARCH_CX")
    )
    
    serper_key = (
        get_config("SERPER_API_KEY")
        or get_config("GOOGLE_SERPER_API_KEY")
        or get_config("SERPER_KEY")
        or get_config("SERPER_API")
        or get_config("SERPERDEV_API_KEY")
    )
    serpapi_key = (
        get_config("SERPAPI_API_KEY")
        or get_config("SERP_API_KEY")
        or get_config("SERPAPI_KEY")
    )

    if google_api_key and google_cse_id:
        active_primary = "Google Official API Key (CSE)"
    elif serper_key or serpapi_key:
        active_primary = "Serp API (Serper / SerpApi)"
    else:
        active_primary = "None (Not Configured)"

    return {
        "active_primary": active_primary,
        "total_count": len(_SEARCH_REQUESTS_LOG),
        "google_cse_count": len(google_cse_calls),
        "serp_api_count": len(serp_api_calls),
        "logs": list(_SEARCH_REQUESTS_LOG)
    }


def call_search_api(query: str, count: int = 10, max_retries: int = 3, initial_delay: float = 1.0) -> list:
    """
    Unified search function:
    - Tier 1 (Primary): Google Official Custom Search Engine (GOOGLE_API_KEY_0 + GOOGLE_CSE_ID_0)
    - Tier 2 (Fallback): Google Serper / SerpApi (SERPER_API_KEY / SERPAPI_API_KEY)
    """
    if not query:
        return []

    google_api_key = (
        get_config("GOOGLE_API_KEY_0")
        or get_config("GOOGLE_API_KEY")
        or get_config("GOOGLE_CUSTOM_SEARCH_KEY")
    )
    google_cse_id = (
        get_config("GOOGLE_CSE_ID_0")
        or get_config("GOOGLE_CSE_ID")
        or get_config("GOOGLE_CX")
        or get_config("GOOGLE_SEARCH_CX")
    )

    serper_key = (
        get_config("SERPER_API_KEY")
        or get_config("GOOGLE_SERPER_API_KEY")
        or get_config("SERPER_KEY")
        or get_config("SERPER_API")
        or get_config("SERPERDEV_API_KEY")
    )
    serpapi_key = (
        get_config("SERPAPI_API_KEY")
        or get_config("SERP_API_KEY")
        or get_config("SERPAPI_KEY")
    )

    # -------------------------------------------------------------
    # Tier 1 (Primary): Google Official CSE (Custom Search JSON API)
    # -------------------------------------------------------------
    if google_api_key and google_cse_id:
        req_index = len(_SEARCH_REQUESTS_LOG) + 1
        started = time.perf_counter()
        provider = "Google Official API Key (CSE)"
        url = "https://www.googleapis.com/customsearch/v1"
        params = {
            "key": google_api_key,
            "cx": google_cse_id,
            "q": query,
            "num": min(max(count, 10), 10)
        }

        logger.info("[SEARCH_API_CALL #%d] Provider=%s | Query: %r", req_index, provider, query)

        with httpx.Client(timeout=15.0) as client:
            try:
                resp = client.get(url, params=params)
                elapsed = f"{time.perf_counter() - started:.2f}s"
                logger.info("[SEARCH_API_CALL #%d] HTTP status=%s (elapsed=%s)", req_index, resp.status_code, elapsed)
                if resp.status_code == 200:
                    data = resp.json()
                    items = data.get("items", [])
                    results = []
                    for item in items:
                        results.append({
                            "title": item.get("title", ""),
                            "url": item.get("link", ""),
                            "description": item.get("snippet", ""),
                            "content": item.get("snippet", "")
                        })
                    logger.info("[SEARCH_API_CALL #%d] Google CSE returned %d results", req_index, len(results))
                    _SEARCH_REQUESTS_LOG.append({
                        "index": req_index,
                        "provider": provider,
                        "engine_type": "Google API Key & CSE",
                        "status": f"Success ({len(results)} results)",
                        "query": query,
                        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "duration": elapsed,
                        "count": count
                    })
                    if results:
                        return results
                    logger.info("[SEARCH_API_CALL #%d] Google CSE returned 0 results. Checking fallback...", req_index)
                else:
                    _SEARCH_REQUESTS_LOG.append({
                        "index": req_index,
                        "provider": provider,
                        "engine_type": "Google API Key & CSE",
                        "status": f"Error {resp.status_code} (Quota Exceeded / Blocked) -> Falling back to Serp API",
                        "query": query,
                        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "duration": elapsed,
                        "count": count
                    })
                    logger.warning("[SEARCH_API_CALL #%d] Google CSE returned status %s: %s. Cascading to fallback...", req_index, resp.status_code, resp.text[:300])
            except Exception as e:
                elapsed = f"{time.perf_counter() - started:.2f}s"
                _SEARCH_REQUESTS_LOG.append({
                    "index": req_index,
                    "provider": provider,
                    "engine_type": "Google API Key & CSE",
                    "status": f"Exception: {str(e)[:100]} -> Falling back to Serp API",
                    "query": query,
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "duration": elapsed,
                    "count": count
                })
                logger.warning("[SEARCH_API_CALL #%d] Google CSE exception: %s. Cascading to fallback...", req_index, e)

    # -------------------------------------------------------------
    # Tier 2A (Fallback / Primary if CSE not set): Google Serper (serper.dev)
    # -------------------------------------------------------------
    if serper_key:
        req_index = len(_SEARCH_REQUESTS_LOG) + 1
        started = time.perf_counter()
        is_fallback = bool(google_api_key and google_cse_id)
        provider = "Serp API (Serper Fallback)" if is_fallback else "Serp API (Serper Primary)"
        url = "https://google.serper.dev/search"
        headers = {"X-API-KEY": serper_key, "Content-Type": "application/json"}
        payload = {"q": query, "num": max(count, 10)}

        logger.info("[SEARCH_API_CALL #%d] Provider=%s | Query: %r", req_index, provider, query)

        with httpx.Client(timeout=15.0) as client:
            for attempt in range(max_retries):
                try:
                    resp = client.post(url, headers=headers, json=payload)
                    elapsed = f"{time.perf_counter() - started:.2f}s"
                    logger.info("[SEARCH_API_CALL #%d] HTTP status=%s (elapsed=%s)", req_index, resp.status_code, elapsed)
                    if resp.status_code == 200:
                        data = resp.json()
                        organic = data.get("organic", [])
                        results = []
                        for item in organic:
                            results.append({
                                "title": item.get("title", ""),
                                "url": item.get("link", ""),
                                "description": item.get("snippet", ""),
                                "content": item.get("snippet", "")
                            })
                        logger.info("[SEARCH_API_CALL #%d] Returned %d results", req_index, len(results))
                        _SEARCH_REQUESTS_LOG.append({
                            "index": req_index,
                            "provider": provider,
                            "engine_type": "Serp API",
                            "status": f"Success ({len(results)} results)",
                            "query": query,
                            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                            "duration": elapsed,
                            "count": count
                        })
                        return results
                    if resp.status_code in (429, 500, 502, 503, 504):
                        delay = initial_delay * (2 ** attempt) + random.uniform(0.1, 0.5)
                        logger.warning("[SEARCH_API_CALL #%d] Transient status %s | retrying in %.2fs", req_index, resp.status_code, delay)
                        time.sleep(delay)
                        continue
                    logger.error("[SEARCH_API_CALL #%d] Permanent error status=%s | body=%s", req_index, resp.status_code, resp.text[:500])
                    _SEARCH_REQUESTS_LOG.append({
                        "index": req_index,
                        "provider": provider,
                        "engine_type": "Serp API",
                        "status": f"Error {resp.status_code}",
                        "query": query,
                        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "duration": elapsed,
                        "count": count
                    })
                    break
                except Exception as e:
                    logger.warning("[SEARCH_API_CALL #%d] Request exception: %s", req_index, e)
                    if attempt < max_retries - 1:
                        time.sleep(initial_delay * (2 ** attempt))

    # -------------------------------------------------------------
    # Tier 2B (Fallback / Alternative): SerpApi (serpapi.com)
    # -------------------------------------------------------------
    if serpapi_key:
        req_index = len(_SEARCH_REQUESTS_LOG) + 1
        started = time.perf_counter()
        is_fallback = bool(google_api_key and google_cse_id)
        provider = "Serp API (SerpApi Fallback)" if is_fallback else "Serp API (SerpApi Primary)"
        url = "https://serpapi.com/search"
        params = {"engine": "google", "q": query, "api_key": serpapi_key, "num": max(count, 10)}

        logger.info("[SEARCH_API_CALL #%d] Provider=%s | Query: %r", req_index, provider, query)

        with httpx.Client(timeout=15.0) as client:
            for attempt in range(max_retries):
                try:
                    resp = client.get(url, params=params)
                    elapsed = f"{time.perf_counter() - started:.2f}s"
                    logger.info("[SEARCH_API_CALL #%d] HTTP status=%s (elapsed=%s)", req_index, resp.status_code, elapsed)
                    if resp.status_code == 200:
                        data = resp.json()
                        organic = data.get("organic_results", [])
                        results = []
                        for item in organic:
                            results.append({
                                "title": item.get("title", ""),
                                "url": item.get("link", ""),
                                "description": item.get("snippet", ""),
                                "content": item.get("snippet", "")
                            })
                        logger.info("[SEARCH_API_CALL #%d] Returned %d results", req_index, len(results))
                        _SEARCH_REQUESTS_LOG.append({
                            "index": req_index,
                            "provider": provider,
                            "engine_type": "Serp API",
                            "status": f"Success ({len(results)} results)",
                            "query": query,
                            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                            "duration": elapsed,
                            "count": count
                        })
                        return results
                    if resp.status_code in (429, 500, 502, 503, 504):
                        delay = initial_delay * (2 ** attempt) + random.uniform(0.1, 0.5)
                        logger.warning("[SEARCH_API_CALL #%d] Transient status %s | retrying in %.2fs", req_index, resp.status_code, delay)
                        time.sleep(delay)
                        continue
                    logger.error("[SEARCH_API_CALL #%d] Permanent error status=%s | body=%s", req_index, resp.status_code, resp.text[:500])
                    _SEARCH_REQUESTS_LOG.append({
                        "index": req_index,
                        "provider": provider,
                        "engine_type": "Serp API",
                        "status": f"Error {resp.status_code}",
                        "query": query,
                        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "duration": elapsed,
                        "count": count
                    })
                    break
                except Exception as e:
                    logger.warning("[SEARCH_API_CALL #%d] Request exception: %s", req_index, e)
                    if attempt < max_retries - 1:
                        time.sleep(initial_delay * (2 ** attempt))

    logger.warning("No search provider available or all requests failed for query: %r", query)
    return []


async def call_search_api_async(query: str, count: int = 10, max_retries: int = 3, initial_delay: float = 1.0) -> list:
    """
    Asynchronous version of unified search function (Google CSE Primary -> Serper/SerpApi Fallback).
    """
    if not query:
        return []

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: call_search_api(query, count=count, max_retries=max_retries, initial_delay=initial_delay))
