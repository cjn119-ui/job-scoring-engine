"""Collect: fetch public board payloads through the allowlisted Fetcher, normalize them, then ingest."""

from __future__ import annotations

from . import evaluate as CE
from . import pipeline as P
from . import sources as CS

GH_DETAILS_PER_BOARD = 14        # Greenhouse list entries carry no description or pay: only the best-titled ones get a detail request


def fetch_board(f: CS.Fetcher, ats: str, token: str, prof: dict) -> tuple[list[dict], set[str]]:
    """-> (normalized postings that passed the coarse title screen, ids of EVERY posting the board listed)."""
    keep = lambda title, dept: CE.coarse_keep(title, dept, prof)       # noqa: E731
    if ats == "greenhouse":
        jobs = f.get_json(CS.gh_list_url(token)).get("jobs", [])
        stubs = [CS.gh_stub(token, j) for j in jobs]
        wanted = [s for s in stubs if keep(s["title"], "")]
        wanted.sort(key=lambda s: -CE._best_title(s["title"], prof)[1])
        out = [CS.gh_posting(token, s, f.get_json(CS.gh_detail_url(token, s["job_id"]))) for s in wanted[:GH_DETAILS_PER_BOARD]]
        return out, {s["job_id"] for s in stubs}
    if ats == "ashby":
        data = f.get_json(CS.ashby_url(token))
        return CS.ashby_postings(token, data, keep), {str(j.get("id")) for j in data.get("jobs") or [] if isinstance(j, dict)}
    if ats == "lever":
        data = f.get_json(CS.lever_url(token))
        return CS.lever_postings(token, data, keep), {str(j.get("id")) for j in data if isinstance(j, dict)}
    raise ValueError(f"unsupported ATS {ats!r}")


def run(state: dict, prof: dict, fetcher: CS.Fetcher, now_iso: str, boards_path=None, weights: dict | None = None) -> dict:
    """Fetch every board once, ingest the results, collapse duplicates. Blocked or missing boards are recorded, never retried around."""
    P.ensure_state(state, boards_path)
    for bid, b in state["boards"].items():
        for ats, token in b["candidates"]:
            try:
                postings, all_ids = fetch_board(fetcher, ats, token, prof)
            except CS.NotFound:
                b["status"] = "not_found"
                continue
            except CS.Blocked as exc:
                b["status"], b["blocked_note"] = "blocked", str(exc)
                break
            except CS.FetchError as exc:
                b["status"], b["fail"] = "error", b.get("fail", 0) + 1
                b["error_note"] = str(exc)[:100]
                break
            b.update(status="ok", ats=ats, slug=token, last_fetch=now_iso, total=len(all_ids), kept=len(postings), fetches=b.get("fetches", 0) + 1)
            b["tiers"] = P.ingest(state, prof, postings, bid, all_ids, now_iso, weights or {})
            state["stats"]["requests"] = fetcher.requests
            break
    P.mark_duplicates(state)
    return state
