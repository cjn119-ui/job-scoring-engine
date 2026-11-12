"""Markdown report of the shortlist. Untrusted posting text is neutralised before it is printed."""

from __future__ import annotations

from . import evaluate as CE
from . import pipeline as P


def _comp_line(ev: dict) -> str:
    c = ev["comp"]
    if c["status"] in ("verified_structured", "stated_in_text") and c["ranges"]:
        label = "verified (provider field)" if c["status"] == "verified_structured" else "stated in posting text"
        bands = "; ".join(f"${r['min']:,.0f}-${r['max']:,.0f}" for r in c["ranges"][:3])
        return f"{bands} - {label}" + (f" - {c['note']}" if c.get("note") else "")
    if c["status"] == "ote_unverified_base":
        return "; ".join(f"${r['min']:,.0f}-${r['max']:,.0f}" for r in c["ranges"][:3]) + " - on-target earnings (variable pay included); BASE NOT VERIFIED"
    return {"unpublished": "not published (unknown, not assumed)", "non_usd": "published in a non-USD currency (not compared)",
            "below_min": "below the configured minimum"}.get(c["status"], c["status"])


def render_report(st: dict, prof: dict, now_iso: str) -> tuple[str, dict]:
    top = P.shortlist(st, now_iso)
    stretch = P.shortlist(st, now_iso, tiers=("stretch",), n=5)
    ok = sum(1 for b in st["boards"].values() if b["status"] == "ok")
    L = ["# Job scoring report", "", f"Generated {now_iso}. Profile `{prof['_hash']}`. Facts are labelled; everything else is inference.", "",
         "## Coverage", f"- Boards fetched: {ok} of {len(st['boards'])}.",
         f"- Open postings kept after evaluation: {sum(1 for e in st['postings'].values() if e['open'])}; duplicates collapsed: {st['stats']['duplicates']}.", "",
         "## Strongest current matches"]
    if not top:
        L += ["No posting currently meets the strong-fit bar with no major gaps.", ""]
    for i, e in enumerate(top, 1):
        ev, fr = e["eval"], CE.freshness(e, now_iso)
        pr, why = CE.priority(e, now_iso)
        L += [f"### {i}. {P._safe(e['company'], 60)} - {P._safe(e['title'], 110)}",
              f"- **Where:** {P._safe('; '.join(e['locations'][:4]), 140)} - workplace: {e['workplace']}",
              f"- **Pay:** {_comp_line(ev)}",
              f"- **Source:** {e['pid'].split(':')[0]} public board `{P._url(e['url'])}` - freshness: {fr['label']}",
              f"- **Score {ev['score']} (family {ev['title_family']}):** " + "; ".join(f"{x['category'].replace('_', ' ')}: \"{P._safe(x['quote'], 130)}\"" for x in ev["evidence"][:4]),
              "- **Stated requirements (candidate attainment UNKNOWN):** " + ("; ".join(f"{r['category'].replace('_', ' ')}: \"{P._safe(r['quote'], 150)}\"" for r in ev.get("requirements", [])) or "none captured"),
              "- **Gaps / uncertainties:** " + ("; ".join([f"{g['gap'].replace('_', ' ')} (\"{P._safe(g['quote'], 90)}\")" for g in ev["gaps"]] + ev["uncertainties"]) or "none found by the rules"),
              f"- **Priority {pr}:** {why}", ""]
    if stretch:
        L += ["## Stretches (verify before investing)"]
        L += [f"- {P._safe(e['company'], 50)} - {P._safe(e['title'], 100)} - score {e['eval']['score']} - pay: {_comp_line(e['eval'])}" for e in stretch]
        L += [""]
    L += ["## Method and limits",
          "- Sources are the public board APIs of the companies listed in the boards file only.",
          "- Scores are rule-based and uncalibrated until compared against your own labels.",
          "- Pay is shown only when the posting or its provider field states it."]
    return "\n".join(L) + "\n", {"top": [e["pid"] for e in top], "sig": P.shortlist_sig(top)}
