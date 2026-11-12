"""State, ingestion and shortlist: merge evaluated postings into a small JSON-serialisable state.

The state is a plain dict (boards, postings, stats). `ingest` evaluates postings with `evaluate`, keeps the interesting tiers, records closed postings
(a posting missing from a successful list is closed, not deleted), describes what changed since last seen, collapses duplicates and builds a shortlist
with at most two entries per company. Read-only research: nothing here contacts a recruiter or submits anything.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import evaluate as CE
from .util import sha256_hex

MAX_FULL_ENTRIES = 400
KEEP_TIERS = ("strong", "stretch", "attractive_poor_fit")
BOARDS_PATH = CE.ROOT / "config" / "boards.example.json"


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40]


def _safe(s, n: int = 160) -> str:
    """Untrusted text for the report: no control characters, markup or link syntax, bounded length."""
    s = re.sub(r"[\x00-\x1f\x7f`|<>\[\]]", " ", str(s or ""))
    return re.sub(r"\s+", " ", s).strip()[:n]


def _url(u) -> str:
    u = str(u or "")
    return u if re.fullmatch(r"https://[A-Za-z0-9.-]+(?::443)?(/[A-Za-z0-9._~%/?=&#+:,@-]*)?", u) and len(u) < 300 else ""


# ------------------------------------------------------------------------------------------------ state
def ensure_state(st: dict, boards_path: Path | None = None) -> dict:
    """Create missing state sections and register every board listed in the boards file."""
    st.setdefault("boards", {})
    st.setdefault("postings", {})
    st.setdefault("gh_pending", {})
    st.setdefault("stats", {"requests": 0, "refreshes": 0, "boards_blocked": 0, "excluded": {}, "duplicates": 0, "tier_counts": {}})
    st.setdefault("reports", {"count": 0})
    st.setdefault("weights", {"values": {}, "provenance": None})
    p = Path(boards_path or BOARDS_PATH)
    if p.is_file():
        for c in json.loads(p.read_text()).get("companies", []):
            bid = _slug(c["company"])
            if bid not in st["boards"]:
                st["boards"][bid] = {"company": c["company"], "candidates": [list(x) for x in c["candidates"]][:4], "idx": 0, "status": "unverified", "fetches": 0, "last_fetch": None,
                                     "fail": 0, "blocked_until": None, "total": None, "kept": None, "tiers": {}, "verified_at": None}
    return st


def _entry_from(p: dict, ev: dict, bid: str, now_iso: str, old: dict | None) -> dict:
    e = {"pid": p["pid"], "board_id": bid, "company": p["company"], "title": p["title"], "locations": p["locations"], "workplace": p["workplace"], "url": p["url"], "apply_url": p["apply_url"],
         "published_at": p["published_at"], "updated_at": p["updated_at"], "comp": p["comp"], "employment": p.get("employment"), "department": p.get("department"),
         "content_hash": p["content_hash"], "desc_fp": p.get("desc_fp", ""), "first_seen": (old or {}).get("first_seen") or now_iso, "last_seen": now_iso, "open": True, "eval": ev,
         "history": (old or {}).get("history", []), "surfaced": (old or {}).get("surfaced") or {"count": 0, "last_hash": None, "last_report": None}}
    if p.get("source_type"):
        e["source_type"] = p["source_type"]
    if p.get("provenance"):
        e["provenance"] = p["provenance"]
    if old and old["content_hash"] != p["content_hash"]:
        e["history"] = (e["history"] + [{"at": now_iso, "change": _describe_change(old, e)}])[-5:]
    elif old and old["eval"]["tier"] != ev["tier"]:
        e["history"] = (e["history"] + [{"at": now_iso, "change": f"tier {old['eval']['tier']} -> {ev['tier']}"}])[-5:]
    return e


def _describe_change(old: dict, new: dict) -> str:
    bits = []
    if old["title"] != new["title"]:
        bits.append("title changed")
    if old["locations"] != new["locations"]:
        bits.append("locations changed")
    oc, nc = old["eval"]["comp"], new["eval"]["comp"]
    if oc.get("best_max") != nc.get("best_max") or oc["status"] != nc["status"]:
        bits.append(f"pay {oc['status']} -> {nc['status']}" + (f" (max ${nc['best_max']:,.0f})" if nc.get("best_max") else ""))
    return ", ".join(bits) or "description changed"


def ingest(st: dict, prof: dict, postings: list[dict], bid: str, all_ids: set[str], now_iso: str, weights: dict) -> dict:
    """Evaluate fetched postings and merge them into state. `all_ids` is EVERY id the board listed, so a posting missing from a successful list is recorded as closed."""
    tiers: dict[str, int] = {}
    stats = st["stats"]
    for p in postings:
        ev = CE.evaluate(p, prof, "full", weights)
        tiers[ev["tier"]] = tiers.get(ev["tier"], 0) + 1
        for x in ev["exclusions"]:
            stats["excluded"][x["rule"]] = stats["excluded"].get(x["rule"], 0) + 1
        old = st["postings"].get(p["pid"])
        if ev["tier"] in KEEP_TIERS:
            st["postings"][p["pid"]] = _entry_from(p, ev, bid, now_iso, old)
        elif old:
            old["open"] = True
            old["last_seen"] = now_iso
            old["eval"] = ev                                           # it dropped out of the interesting tiers; kept only as a stub so the change is visible
            old["history"] = (old["history"] + [{"at": now_iso, "change": f"tier -> {ev['tier']}"}])[-5:]
    for pid, e in st["postings"].items():
        if e["board_id"] == bid:
            if pid.split(":", 2)[2] in all_ids:
                if e["last_seen"] != now_iso and e["open"] is False:
                    e["open"] = True
            elif e["open"]:
                e["open"], e["closed_at"] = False, now_iso
    for pid in [k for k, e in st["postings"].items() if e["board_id"] == bid and e["open"] and k.split(":", 2)[2] in all_ids and e["last_seen"] != now_iso]:
        st["postings"][pid]["last_seen"] = now_iso                      # listed, not re-evaluated this pass (coarse-dropped or pending detail): still open
    _cap_entries(st)
    return tiers


def _cap_entries(st: dict) -> None:
    ps = st["postings"]
    if len(ps) <= MAX_FULL_ENTRIES:
        return
    def keep_rank(e):
        # closed first, then the weakest tier, then the lowest score: recency must not decide (an older but strong opening is worth more than a newer weak one)
        return (e["open"], {"strong": 3, "stretch": 2, "attractive_poor_fit": 1}.get(e["eval"]["tier"], 0), e["eval"]["score"])
    for pid in sorted(ps, key=lambda k: keep_rank(ps[k]))[: len(ps) - MAX_FULL_ENTRIES]:
        ps.pop(pid)


def mark_duplicates(st: dict) -> int:
    """Same company + normalised title + same description fingerprint = one opportunity listed in several places. The shortlist shows one and notes the others."""
    seen: dict[tuple, str] = {}
    n = 0
    for pid in sorted(st["postings"]):
        e = st["postings"][pid]
        e.pop("dup_of", None)
        if not e["open"]:
            continue
        k = (e["company"].lower(), re.sub(r"\W+", " ", e["title"].lower()).strip(), e.get("desc_fp") or sha256_hex([x["quote"] for x in e["eval"]["evidence"][:3]])[:8])
        if k in seen:
            e["dup_of"] = seen[k]
            n += 1
        else:
            seen[k] = pid
    st["stats"]["duplicates"] = n
    return n


MAX_PER_COMPANY = 2


def shortlist(st: dict, now_iso: str, tiers=("strong",), n: int = 5) -> list[dict]:
    """Best first: published pay class, then score. At most MAX_PER_COMPANY per company, so five slots cannot be one employer's whole careers page."""
    items = [e for e in st["postings"].values() if e["open"] and e["eval"]["tier"] in tiers and not e.get("dup_of")]
    items.sort(key=CE.sort_key)
    out, per = [], {}
    for e in items:
        c = e["company"].lower()
        if per.get(c, 0) >= MAX_PER_COMPANY:
            continue
        per[c] = per.get(c, 0) + 1
        out.append(e)
        if len(out) == n:
            break
    return out



def precision_at_k(ranked: list[str], positives: set[str], k: int = 5) -> float:
    top = ranked[:k]
    return sum(1 for x in top if x in positives) / len(top) if top else 0.0


def shortlist_sig(items: list[dict]) -> str:
    """Stable signature of a shortlist, so a report can say whether anything actually changed."""
    return sha256_hex([(e["pid"], e["content_hash"]) for e in items])[:12]
