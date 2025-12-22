import json
from datetime import timedelta
from urllib import request as urlrequest
from urllib.parse import urlencode

import db


def _is_fresh(updated_at: str, now_iso: str, days: int = 7) -> bool:
    try:
        from datetime import datetime

        updated_dt = datetime.fromisoformat(updated_at)
        now_dt = datetime.fromisoformat(now_iso)
        return now_dt - updated_dt <= timedelta(days=days)
    except Exception:
        return False


def fetch_suggestions(query: str, now_iso: str):
    q = query.strip().lower()
    if len(q) < 3:
        return []
    cached = db.get_cached_rxnorm(q)
    if cached and cached.get("response_json") and cached.get("updated_at") and _is_fresh(cached["updated_at"], now_iso):
        try:
            return json.loads(cached["response_json"])
        except Exception:
            pass
    url = f"https://rxnav.nlm.nih.gov/REST/approximateTerm.json?{urlencode({'term': q, 'maxEntries': 10})}"
    try:
        req = urlrequest.Request(url, headers={"User-Agent": "HealthlineMVP"})
        with urlrequest.urlopen(req, timeout=3) as resp:
            data = resp.read().decode("utf-8")
            parsed = json.loads(data)
            candidates = parsed.get("approximateGroup", {}).get("candidate", []) or []
            results = []
            seen = set()
            for cand in candidates[:10]:
                rxcui = cand.get("rxcui")
                name = cand.get("name")
                if not rxcui or not name:
                    continue
                if rxcui in seen:
                    continue
                seen.add(rxcui)
                results.append({"rxcui": str(rxcui), "name": name})
                if len(results) >= 10:
                    break
            db.upsert_rxnorm_cache(q, json.dumps(results))
            return results
    except Exception:
        return []
