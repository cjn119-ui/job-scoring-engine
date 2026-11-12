"""Deterministic, explainable qualification rules for job postings.

Every claim carries its kind: 'verified' (quoted from the posting / structured provider field), 'inferred' (derived by a rule here) or 'unknown'. Nothing is invented: a salary that is
not published stays unpublished, a requirement that is not in the text is not assumed. Posting text is data - it is matched with fixed patterns and never obeyed. Two ranking variants
exist so they can be compared on labelled held-out evidence instead of trusting either.
"""
from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path

from .util import sha256_hex

ROOT = Path(__file__).resolve().parents[2]
PROFILE_PATH = Path(os.environ.get("JOB_SCORING_PROFILE") or ROOT / "config" / "profile.example.json")
VARIANTS = ("full", "title_only")


@lru_cache(maxsize=4)
def _load(path: str, mtime: float) -> dict:
    return json.loads(Path(path).read_text())


def load_profile(path: Path | str | None = None) -> dict:
    p = Path(path or PROFILE_PATH)
    # The cached source must stay immutable: hashing a previous _hash changes
    # the identity on every read and invalidates all Greenhouse evaluations.
    prof = {k: v for k, v in _load(str(p), p.stat().st_mtime).items() if k != "_hash"}
    prof["_hash"] = sha256_hex(prof)[:12]
    return prof


def _rx(patterns: list[str]):
    return re.compile("|".join(f"(?:{p})" for p in patterns), re.I)


@lru_cache(maxsize=512)
def _compiled(key: str):
    return _rx(json.loads(key))


def rx(patterns) -> re.Pattern:
    return _compiled(json.dumps(list(patterns)))


def _quote(text: str, m: re.Match, width: int = 150) -> str:
    """A verbatim excerpt around the match, trimmed to whole words (an ellipsis marks each cut)."""
    a = max(0, m.start() - width // 3)
    b = min(len(text), m.end() + width * 2 // 3)
    while a > 0 and a < m.start() and not text[a - 1].isspace():
        a += 1
    while b < len(text) and b > m.end() and not text[b].isspace():
        b -= 1
    return ("..." if a > 0 else "") + re.sub(r"\s+", " ", text[a:b]).strip() + ("..." if b < len(text) else "")


# ------------------------------------------------------------------------------------------------ stage 1
def coarse_keep(title: str, dept: str, prof: dict) -> bool:
    """Cheap title screen applied before any description is fetched: keep anything a target family could plausibly describe, drop what is clearly another profession.
    It never makes a positive judgement; it only avoids spending requests on roles that cannot be relevant."""
    t = f"{title} {dept}".lower()
    if rx(prof["hard_exclusions"]["title"]).search(title):
        return False
    if re.search(r"\b(recruit|talent acquisition|sourcer|legal|counsel|paralegal|attorney|accountant|accounting|controller|tax|treasury|payroll|copywriter|content (writer|marketing)|brand|designer|ux|ui|"
                 r"account executive|sales development|business development|\bsdr\b|\bbdr\b|customer success|account manager|marketing|public relations|communications|nurse|physician|clinical|"
                 r"warehouse|driver|technician|mechanic|electrician|facilities|office manager|executive assistant|chief of staff|people partner|hr business)\b", t):
        return False
    fam, w = _best_title(title, prof)
    if w >= 18:
        return True
    if fam == "software_generic" or not fam:
        kw = prof.get("coarse_keep_keywords") or ["engineer", "developer"]
        return bool(re.search(r"\b(" + "|".join(map(re.escape, kw)) + r")\b", t) and re.search(r"\b(engineer|developer|architect|analyst|specialist|consultant)\b", t))
    return False


# ------------------------------------------------------------------------------------------------ comp
_MULT = {"year": 1, "month": 12, "week": 52, "day": 260}


def annualize(r: dict, hours: int) -> tuple[float, float] | None:
    """Full-time-equivalent yearly figure, or None when the interval is unknown (then it is not compared at all)."""
    iv = r.get("interval")
    mult = hours if iv == "hour" else _MULT.get(iv)
    return None if mult is None else (r["min"] * mult, r["max"] * mult)


_OTE = re.compile(r"\b(ote|on[- ]target earnings|commission|variable (pay|compensation)|bonus plan)\b", re.I)
_SALESY = re.compile(r"\b(sales|account (executive|manager)|solutions? (engineer|architect|consultant)|customer engineer|gtm|go[- ]to[- ]market|revenue|business development|customer success|partner)\b", re.I)


def ote_risk(title: str, comp: list[dict], desc: str) -> bool:
    """A pay range on a sales-style role that mentions on-target earnings / commission is probably base + variable, so it does not VERIFY a base salary. Observed on real data: one
    provider's generic pay blurb says 'for sales roles the range is OTE' on every posting, so the title must be sales-style too - a mention alone proves nothing."""
    if not _SALESY.search(title):
        return False
    blurbs = " ".join(str(r.get("blurb") or "") for r in comp)
    return bool(_OTE.search(blurbs) or _OTE.search(desc[:3000]))


def comp_assessment(comp: list[dict], prof: dict, ote: bool = False) -> dict:
    """-> {status, best_max, min_of_ranges, ranges, note}. Status: verified_structured | stated_in_text | unpublished | below_min | non_usd.
    A posting lists several pay bands by geography; it is below the minimum only if EVERY USD band tops out below it."""
    floor, hours = prof.get("min_comp_usd"), prof.get("hours_per_year", 2080)       # floor is optional: None means pay is reported but never compared
    usd = [r for r in comp or [] if str(r.get("currency") or "?").upper() == "USD" and annualize(r, hours) is not None]
    if not comp:
        return {"status": "unpublished", "best_max": None, "ranges": [], "note": "no pay range published in the posting or its provider data"}
    if not usd:
        return {"status": "non_usd", "best_max": None, "ranges": comp, "note": "pay is in a non-USD or unspecified currency, or an unknown interval; not compared with the minimum"}
    ann = [(*annualize(r, hours), r) for r in usd]
    best = max(a[1] for a in ann)
    src = "verified_structured" if any(a[2].get("source") == "structured" for a in ann) else "stated_in_text"
    if ote and (floor is None or best >= floor):
        return {"status": "ote_unverified_base", "best_max": best, "ranges": [{"min": a[0], "max": a[1], "source": a[2].get("source")} for a in ann],
                "note": "range likely includes commission/variable pay (on-target earnings): base salary is NOT verified"}
    if floor is None:
        return {"status": src, "best_max": best, "ranges": [{"min": a[0], "max": a[1], "source": a[2].get("source")} for a in ann], "note": "no minimum configured: pay is reported, not compared"}
    note = "hourly/weekly/monthly pay annualised (hourly at %d h): a full-time equivalent, not a verified offer" % hours if any(a[2].get("interval") != "year" for a in ann) else ""
    if best < floor:
        return {"status": "below_min", "best_max": best, "ranges": [{"min": a[0], "max": a[1], "source": a[2].get("source")} for a in ann], "note": note or f"every published band tops out below ${floor:,.0f}"}
    return {"status": src, "best_max": best, "ranges": [{"min": a[0], "max": a[1], "source": a[2].get("source")} for a in ann], "note": note}


# ------------------------------------------------------------------------------------------------ location
def us_status(locations: list[str], workplace: str, prof: dict) -> str:
    """us | maybe_us (bare 'Remote'/unknown) | non_us. Only an explicitly foreign-only posting is non_us."""
    text = " | ".join(locations).lower()
    if not text.strip():
        return "maybe_us"
    us, non = rx(prof["us_markers"]), rx(prof["non_us_markers"])
    parts = [p.strip() for p in re.split(r"\||;|/", text) if p.strip()]
    if any(us.search(p) and not non.search(p) for p in parts):
        return "us"
    if any(non.search(p) for p in parts) and not any(us.search(p) for p in parts):
        return "non_us"
    return "us" if us.search(text) and not non.search(text) else "maybe_us"


def col_tier(locations: list[str], prof: dict) -> str:
    t = " ".join(locations).lower()
    for tier in ("high", "medium", "low"):
        if any(m in t for m in prof["cost_of_living_estimate"][tier]):
            return tier
    return "unknown"


# ------------------------------------------------------------------------------------------------ scoring
def _best_title(title: str, prof: dict) -> tuple[str, int]:
    best, w = "", 0
    for name, fam in prof["title_families"].items():
        if fam["weight"] > w and rx(fam["patterns"]).search(title):
            best, w = name, fam["weight"]
    return best, w


def role_evidence_text(desc: str) -> tuple[str, str]:
    """Prefer explicitly labelled role sections over company marketing copy.

    If no role heading is recognised, keep the text and disclose that limit.
    Requirements/gaps still read the full posting below.
    """
    role = re.compile(r"^(?:about (?:the|this) role|the role|responsibilities|(?:your |key )?responsibilities|what you(?:'|’)?ll (?:do|build)|what you will (?:do|build)|in this role|qualifications|requirements|what you(?:'|’)?ll bring|what we(?:'|’)?re looking for)\s*:?$", re.I)
    other = re.compile(r"^(?:about (?:us|the company)|our (?:mission|company|values)|who we are|benefits|(?:compensation|pay)(?: and benefits)?|equal (?:employment )?opportunity|why (?:join|work with) us)\s*:?$", re.I)
    lines = desc.splitlines(keepends=True)
    if not any(role.fullmatch(line.strip()) for line in lines):
        return desc, "whole posting; role sections not identified"
    keep, selected = False, []
    for line in lines:
        if role.fullmatch(line.strip()):
            keep = True
            selected.append(re.sub(r"[^\r\n]", " ", line))
        elif other.fullmatch(line.strip()):
            keep = False
            selected.append(re.sub(r"[^\r\n]", " ", line))
        else:
            selected.append(line if keep else re.sub(r"[^\r\n]", " ", line))
    # Preserve offsets so evidence excerpts still quote the original posting.
    return "".join(selected), "labelled role and qualification sections"


def extract_requirements(text: str) -> list[dict]:
    """Quote employer requirements without asserting the candidate's attainment."""
    patterns = {
        "years_experience": r"\b\d{1,2}\+?\s*(?:[-–]\s*\d{1,2})?\s+years?\s+(?:of\s+)?(?:[\w-]+\s+){0,4}experience\b",
        "degree": r"\b(?:(?:bachelor|master)(?:['’]s)?\s+(?:degree|in\b)|(?:bachelor|master)['’]s\b|ph\.?d\.?\b|doctorate\b|doctoral degree\b|(?:b\.?s\.?|m\.?s\.?)\s+(?:degree|in\b))",
        "distributed_systems": r"\bdistributed systems?\b",
    }
    out = []
    for category, pattern in patterns.items():
        for match in list(re.finditer(pattern, text, re.I))[:2]:
            out.append({"category": category, "posting_status": "stated; verify required versus preferred in context",
                        "candidate_status": "UNKNOWN", "quote": _quote(text, match)})
    return out


def evaluate(p: dict, prof: dict, variant: str = "full", weights: dict | None = None) -> dict:
    """-> evaluation dict. `weights` are optional bounded per-family multipliers learned from labelled outcomes."""
    title, desc = p["title"], p.get("description") or ""
    head = desc[:700]
    ev: dict = {"variant": variant, "profile": prof["_hash"], "exclusions": [], "gaps": [], "evidence": [], "uncertainties": [], "flags": []}
    score_text, ev["evidence_scope"] = role_evidence_text(desc)
    ev["requirements"] = extract_requirements(desc)
    ev["qualification_status"] = "UNKNOWN: candidate qualifications have not been verified"
    # hard exclusions -------------------------------------------------------------------------
    m = rx(prof["hard_exclusions"]["title"]).search(title)
    if m:
        ev["exclusions"].append({"rule": "title", "detail": f"title matches '{m.group(0)}'"})
    need = prof["hard_exclusions"]["description_repeated_min"]
    for name, pats in prof["hard_exclusions"]["description_repeated"].items():
        hits = list(rx(pats).finditer(desc))
        if len(hits) >= need or len([h for h in hits if h.start() < 700]) >= 2:
            ev["exclusions"].append({"rule": name, "detail": f"{len(hits)} mention(s); e.g. \"{_quote(desc, hits[0])}\""})
    # a generic 'prompt engineer' title with no engineering substance is excluded by the title rule above; with real code/API substance it is not reached here
    us = us_status(p.get("locations") or [], p.get("workplace", "unknown"), prof)
    conflict = re.search(r"\b(?:this )?(?:role|position|job) (?:is|will be) (?:located|based|hosted) in ([^.\n]{3,60})", desc[:1500], re.I)
    if conflict and rx(prof["non_us_markers"]).search(conflict.group(1)) and not rx(prof["us_markers"]).search(conflict.group(1)):
        us = "non_us"
        ev["uncertainties"].append(f"the provider's location field ({'; '.join(p.get('locations') or [])}) disagrees with the text: \"{_quote(desc, conflict)}\"")
    elif rx(prof["non_us_markers"]).search(title) and not rx(prof["us_markers"]).search(title):
        us = "non_us"
    ev["us"] = us
    if us == "non_us":
        ev["exclusions"].append({"rule": "location", "detail": "posting is located outside the United States only: " + "; ".join(p.get("locations") or [])})
    comp = comp_assessment(p.get("comp") or [], prof, ote_risk(title, p.get("comp") or [], desc))
    ev["comp"] = comp
    floor = prof.get("min_comp_usd")
    if comp["status"] == "below_min":
        ev["exclusions"].append({"rule": "compensation", "detail": f"published pay tops out at ${comp['best_max']:,.0f}, below the ${floor:,} minimum"})
    # relevance -------------------------------------------------------------------------------
    fam, tw = _best_title(title, prof)
    mult = (weights or {}).get(fam, 1.0) if fam else 1.0
    title_score = round(tw * mult, 1)
    title_promise = title_score                                             # what the title alone suggests (used to spot attractive-looking poor fits)
    ev["title_family"] = fam or None
    parts = {"title": title_score}
    if variant == "title_only":
        parts.update({"responsibilities": 0, "tech": 0, "ownership": 0})
        total = title_score * 100 / 40 if tw else 0
        ev["score"], ev["parts"] = round(min(total, 100), 1), parts
    else:
        resp_pts = 0
        used: list[tuple[int, int]] = []
        for cat, pats in prof["responsibility_signals"].items():
            mm = next((m_ for m_ in rx(pats).finditer(score_text) if not any(abs(m_.start() - a_) < 120 for a_, _ in used)), None) or rx(pats).search(score_text)       # prefer a passage not already quoted for another category
            if mm:
                resp_pts += 5
                used.append((mm.start(), mm.end()))
                ev["evidence"].append({"kind": "verified", "category": cat, "quote": _quote(desc, mm)})
        resp_pts = min(resp_pts, 35)
        tech_pts, techs = 0, []
        for name, t in prof["tech_signals"].items():
            if rx(t["patterns"]).search(score_text):
                tech_pts += t["points"]
                techs.append(name)
        tech_pts = min(tech_pts, 15)
        own = rx(prof["ownership_signals"]).search(score_text)
        own_pts = 10 if (own and len(rx(prof["ownership_signals"]).findall(score_text)) >= 2) else (5 if own else 0)
        if own:
            ev["evidence"].append({"kind": "verified", "category": "ownership", "quote": _quote(desc, own)})
        cats = {x["category"] for x in ev["evidence"]}
        needs = prof.get("title_family_requires", {}).get(fam)                 # e.g. {"data_platform": "pipelines"}
        if needs and needs not in cats and title_score:
            title_score = round(title_score * prof.get("unsupported_title_factor", 0.6), 1)       # the title promises it but the text shows none: partial credit only
            parts["title"] = title_score
            ev["uncertainties"].append(f"title says {fam.replace('_', ' ')} but the description shows no {needs.replace('_', ' ')} work")
        parts.update({"responsibilities": resp_pts, "tech": tech_pts, "ownership": own_pts})
        ev["techs"] = techs
        # gaps -----------------------------------------------------------------------------------
        for level, groups in prof["gaps"].items():
            for gname, pats in groups.items():
                hay = title if gname == "specialty_mismatch" else (title + " " + desc[:300] if gname == "people_management" else desc)
                mm = rx(pats).search(hay)
                if mm and not (gname == "specialty_mismatch" and fam in prof.get("specialty_exempt_families", []) and not re.search(r"\b(security|sre|site reliability|kernel|firmware|embedded)\b", title, re.I)):
                    ev["gaps"].append({"level": level, "gap": gname, "quote": _quote(hay, mm)})
        penalty = 15 * sum(g["level"] == "major" for g in ev["gaps"]) + 5 * sum(g["level"] == "minor" for g in ev["gaps"])
        total = title_score + resp_pts + tech_pts + own_pts - penalty
        ev["score"], ev["parts"], ev["penalty"] = round(max(total, 0), 1), parts, penalty
    # tiering ---------------------------------------------------------------------------------
    major = sum(g["level"] == "major" for g in ev["gaps"])
    if ev["exclusions"]:
        ev["tier"] = "excluded"
    elif ev["score"] >= prof["strong_threshold"] and major == 0:
        ev["tier"] = "strong"
    elif ev["score"] >= prof["stretch_threshold"]:
        ev["tier"] = "stretch"
    elif title_promise >= 28:
        ev["tier"] = "attractive_poor_fit"                     # the title promises more than the responsibilities deliver
    else:
        ev["tier"] = "poor_fit"
    # context flags (verified from text unless marked inferred) ------------------------------
    ev["relocation_support"] = bool(rx(prof["relocation_markers"]).search(desc))
    ev["evergreen"] = bool(rx(prof["evergreen_markers"]).search(title + " " + head))
    ev["injection_suspect"] = bool(rx(prof["injection_markers"]).search(desc))
    ev["col_tier_estimate"] = col_tier(p.get("locations") or [], prof)
    emp = str(p.get("employment") or "").lower()
    if "contract" in emp or "temp" in emp or re.search(r"\bcontract(or)?\b", title.lower()):
        ev["flags"].append("contract_or_temporary")
    if floor is not None and comp.get("ranges") and comp["status"] in ("verified_structured", "stated_in_text") and any(r["min"] < floor <= r["max"] for r in comp["ranges"]):
        ev["uncertainties"].append(f"published range begins below the ${floor:,} minimum (it straddles it)")
    if comp["status"] == "unpublished":
        ev["uncertainties"].append("compensation not published" + (f": the ${floor:,} minimum cannot be confirmed" if floor is not None else ""))
    if comp["status"] == "ote_unverified_base":
        ev["uncertainties"].append("published range looks like on-target earnings (includes variable pay): the base salary is not verified")
    if comp["status"] == "stated_in_text":
        ev["uncertainties"].append("pay range comes from free text in the description, not a structured field")
    if comp["status"] == "non_usd":
        ev["uncertainties"].append("pay is in a non-USD currency; not compared with the minimum")
    if us == "maybe_us":
        ev["uncertainties"].append("United States eligibility is not explicit in the location")
    if ev["evergreen"]:
        ev["uncertainties"].append("reads like a standing talent-pool posting, not a specific opening")
    if ev["injection_suspect"]:
        ev["flags"].append("injection_suspect")
        ev["uncertainties"].append("posting text contains instruction-like content; it is excluded from any model prompt")
    if variant == "full" and not ev["evidence"] and ev["tier"] in ("strong", "stretch"):
        ev["uncertainties"].append("few responsibility signals matched: fit rests mostly on the title")
    return ev


# ------------------------------------------------------------------------------------------------ ranking
def sort_key(entry: dict, now_days: float = 0.0) -> tuple:
    """Verified pay first, then score, then recency. Remote/hybrid/on-site are NOT ranked against each other."""
    ev = entry["eval"]
    rank = {"verified_structured": 0, "stated_in_text": 1}.get(ev["comp"]["status"], 2)
    return (rank, -ev["score"], entry.get("first_seen", ""), entry["pid"])


def freshness(entry: dict, now_iso: str) -> dict:
    """How current the opening looks, from the provider's own dates (verified) and our observations (verified). Age labels are inference."""
    from .util import parse_ts
    now = parse_ts(now_iso)
    out = {"published_at": entry.get("published_at"), "updated_at": entry.get("updated_at"), "first_seen": entry.get("first_seen"), "last_seen": entry.get("last_seen")}
    ref = entry.get("updated_at") or entry.get("published_at")
    try:
        age = (now - parse_ts(ref)).days if ref else None
    except (ValueError, TypeError):
        age = None
    out["age_days"] = age
    out["label"] = "unknown date" if age is None else ("fresh (<=30 d)" if age <= 30 else ("aging (31-120 d)" if age <= 120 else "stale (>120 d): hiring activity unconfirmed"))
    return out


def priority(entry: dict, now_iso: str) -> tuple[str, str]:
    ev = entry["eval"]
    fr = freshness(entry, now_iso)
    if ev["tier"] == "strong" and ev["comp"]["status"] in ("verified_structured", "stated_in_text") and fr["age_days"] is not None and 0 <= fr["age_days"] <= 60 and not ev["uncertainties"]:
        return "P1", "review qualifications first: strong profile alignment and recent published pay; candidate qualifications remain unverified"
    if ev["tier"] == "strong":
        return "P2", "worth a closer look: strong fit, but " + "; ".join(ev["uncertainties"][:2] or ["posting is not recent"])
    if ev["tier"] == "stretch":
        return "P3", "interesting stretch: review the gaps before investing"
    return "P4", "monitor only"
