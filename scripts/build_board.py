#!/usr/bin/env python3
"""Build data.json for the board from saved raw X API output (stdlib only).

Usage:  python3 scripts/build_board.py --day YYYY-MM-DD [--raw raw/<fetch-folder>]

Raw layout (saved by the agent from the X MCP, one JSON object per file):
  users.json                get_users_by_usernames result (watchlist + operator)
  <handle>.json             get_users_posts page 1 (exclude=retweets,replies, max_results=25)
  <handle>__p2.json, ...    follow-up pages via pagination_token
  search_en.json / search_zh.json   search_posts_all results used for Outsiders
The builder never calls X. It only reads files, so it is deterministic.
"""
import argparse, glob, json, math, os, re, sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
J = lambda *p: os.path.join(ROOT, *p)
load = lambda p: json.load(open(p, encoding="utf-8"))


def score_for(hits, views):  # same formula as the board's renderer
    return round(100 * (.45 * min(hits / 12, 1) + .55 * min(views / 100000, 1)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", required=True)
    ap.add_argument("--raw", default=None)
    a = ap.parse_args()
    cfg = load(J("config", "watchlist.json"))
    tz = ZoneInfo(cfg["timezone"])
    MIN, STAR = cfg["min_views"], cfg["star_views"]
    D = a.day
    prevD = (datetime.fromisoformat(D) - timedelta(days=1)).date().isoformat()
    raw = a.raw or J("raw", sorted(os.listdir(J("raw")))[-1])
    op = cfg["operator"]
    accounts = cfg["accounts"]
    handles = accounts + [op]

    users = {}
    uj = load(os.path.join(raw, "users.json"))
    for u in (uj["data"] if "data" in uj else [dict(v, username=k) for k, v in uj.items()]):
        users[u["username"].lower()] = u

    def media_of(post, inc):
        keys = (post.get("attachments") or {}).get("media_keys") or []
        types = {m["media_key"]: m["type"] for m in (inc or {}).get("media", [])}
        ts = [types.get(k) for k in keys]
        return "video" if any(t in ("video", "animated_gif") for t in ts) else ("image" if "photo" in ts else "text")

    # ---- posts per handle + coverage
    posts, cover = {}, {}
    for h in handles:
        files = sorted(glob.glob(os.path.join(raw, f"{h}.json")) + glob.glob(os.path.join(raw, f"{h}__p*.json")),
                       key=lambda f: (0 if f.endswith(f"{h}.json") else int(re.search(r"__p(\d+)", f).group(1))))
        oldest, exhausted = None, False
        for f in files:
            d = load(f)
            inc = d.get("includes", {})
            for p in d.get("data", []):
                if any(r.get("type") == "replied_to" for r in p.get("referenced_tweets", []) or []):
                    continue  # self-thread replies leak through exclude=replies
                dt = datetime.fromisoformat(p["created_at"].replace("Z", "+00:00"))
                loc = dt.astimezone(tz)
                m = p["public_metrics"]
                posts[p["id"]] = dict(h=h, name=users[h.lower()]["name"], id=p["id"], day=loc.date().isoformat(),
                                      at=loc.isoformat(), v=m["impression_count"], text=p["text"], media=media_of(p, inc),
                                      quote=any(r.get("type") == "quoted" for r in p.get("referenced_tweets", []) or []))
                oldest = dt if oldest is None or dt < oldest else oldest
            exhausted = not d.get("meta", {}).get("next_token")
        cover[h] = None if exhausted else (oldest.astimezone(tz) if oldest else None)
    plist = list(posts.values())

    def day_complete(day):  # every account's fetch reaches back to (day start + 6h grace)
        start = datetime.fromisoformat(day).replace(tzinfo=tz) + timedelta(hours=6)
        return all(c is None or c <= start for c in cover.values())

    # ---- ledger (persists across runs, only complete days are (re)written)
    lp = J("data", "ledger.json")
    ledger = load(lp) if os.path.exists(lp) else {"days": {}}
    days_seen = sorted({p["day"] for p in plist})
    for day in days_seen:
        if day > D or not day_complete(day):
            continue
        ent = {}
        for h in accounts:
            ps = [p for p in plist if p["h"] == h and p["day"] == day]
            hits = [p for p in ps if p["v"] >= MIN]
            ent[h] = dict(hits=len(hits), views=sum(p["v"] for p in hits), best=max([p["v"] for p in ps], default=0),
                          best_id=(max(ps, key=lambda p: p["v"])["id"] if ps else None))
        ledger["days"][day] = ent
    json.dump(ledger, open(lp, "w"), indent=1)

    url = lambda p: f"https://x.com/{p['h']}/status/{p['id']}"
    foll = lambda h: users[h.lower()]["public_metrics"]["followers_count"]

    # ---- keyword lexicon over the last 3 complete days
    lex = {k: re.compile(v, re.I) for k, v in cfg["keyword_lexicon"].items()}
    win_days = [d for d in sorted(ledger["days"]) if d <= D][-3:]
    wp = [p for p in plist if p["day"] in win_days and p["h"] in accounts and p["v"] >= MIN]
    kw = {}
    for p in wp:
        for k, rx in lex.items():
            if rx.search(p["text"]):
                e = kw.setdefault(k, dict(keyword=k, posts=0, authors=set(), views=0))
                e["posts"] += 1; e["authors"].add(p["h"]); e["views"] += p["v"]
    kwl = []
    for e in kw.values():
        e["score"] = round(math.log10(max(e["views"], 1)) * len(e["authors"]) ** .5 * e["posts"] ** .5, 2)
        kwl.append(e)
    kwl.sort(key=lambda e: -e["score"])
    kwl = [e for e in kwl if e["posts"] >= 2][:12]
    for i, e in enumerate(kwl, 1):
        e["rank"] = i; e["authors"] = len(e["authors"])
    rank_w = {e["keyword"]: e for e in kwl}

    def why(p, followers):
        ms = [k for k, rx in lex.items() if k in rank_w and rx.search(p["text"])]
        miss = 1.0
        for k in ms:
            r = rank_w[k]["rank"]
            miss *= 1 - (.45 if r <= 3 else .3 if r <= 6 else .2)
        tm = round(100 * (1 - miss))
        if tm >= 50: verdict, line = "trend", "Rides a recurring topic: " + ", ".join(ms[:3])
        elif p["media"] == "video": verdict, line = "visual", "Video carried it; topic match is weak"
        elif followers >= 100000: verdict, line = "author", f"Large audience ({followers//1000}K followers) did most of the work"
        elif ms: verdict, line = "partial", "Partial topic match: " + ", ".join(ms[:3])
        else: verdict, line = "other", "No tracked keyword; check the post for a novelty angle"
        return dict(trend_match=tm, verdict=verdict, line=line, keywords=ms)

    tips = load(J("data", "tips.json")).get("tips", {})
    TIP = {"video": "Video hit: study the first 2 seconds and the caption style before you remake it.",
           "image": "Image hit: one strong screenshot or photo carried it; try a clean cover frame for your own clip.",
           "text": "Text-only hit: the first line does the work; open your video with that line as the hook."}

    def hook(t):
        t = re.sub(r"https?://t\.co/\S+", "", t).strip()
        first = t.split("\n")[0].strip() or t
        return (first[:140] + "…") if len(first) > 140 else first

    # ---- hits for D
    dp = sorted([p for p in plist if p["day"] == D and p["h"] in accounts and p["v"] >= MIN], key=lambda p: -p["v"])
    hits = []
    for p in dp:
        hits.append(dict(name=p["name"], handle="@" + p["h"], views=p["v"], url=url(p), hook=hook(p["text"]),
                         text=re.sub(r"https?://t\.co/\S+", "", p["text"]).strip()[:280], media=p["media"],
                         posted_at=p["at"], highlight=p["v"] >= STAR,
                         why_hit=why(p, foll(p["h"])), video_tip=tips.get(p["id"], TIP[p["media"]])))
    # ---- outsiders from searches
    wl = {h.lower() for h in handles}
    virals = []
    for f in ("search_en.json", "search_zh.json"):
        fp = os.path.join(raw, f)
        if not os.path.exists(fp):
            continue
        d = load(fp); us = {u["id"]: u for u in d.get("includes", {}).get("users", [])}
        for p in d.get("data", []):
            u = us.get(p["author_id"]); v = p["public_metrics"]["impression_count"]
            if not u or u["username"].lower() in wl or v < MIN:
                continue
            q = dict(id=p["id"], h=u["username"], text=p["text"], media="video" if False else media_of(p, d.get("includes")), at=p["created_at"])
            virals.append(dict(name=u["name"], handle="@" + u["username"], views=v,
                               url=f"https://x.com/{u['username']}/status/{p['id']}", hook=hook(p["text"]),
                               text=re.sub(r"https?://t\.co/\S+", "", p["text"]).strip()[:280], media=q["media"], posted_at=q["at"],
                               followers=u["public_metrics"]["followers_count"],
                               why_hit=why(dict(text=p["text"], media=q["media"]), u["public_metrics"]["followers_count"])))
    virals.sort(key=lambda v: -v["views"])

    # ---- roster / you
    roster = []
    for h in handles:
        ps = [p for p in plist if p["h"] == h and p["day"] == D]
        if not ps: continue
        best = max(ps, key=lambda p: p["v"])
        roster.append(dict(handle="@" + h, name=users[h.lower()]["name"], best_views=best["v"], views=best["v"],
                           total_views=sum(p["v"] for p in ps), posts=len(ps), hit=best["v"] >= MIN, is_you=(h == op),
                           url=url(best)))
    roster.sort(key=lambda r: -r["best_views"])
    mine = [p for p in plist if p["h"] == op and p["day"] == D]
    mb = max(mine, key=lambda p: p["v"]) if mine else None
    you = dict(handle="@" + op, name=users[op.lower()]["name"], posts=len(mine),
               best_views=mb["v"] if mb else 0, total_views=sum(p["v"] for p in mine),
               best_url=url(mb) if mb else "", best_hook=hook(mb["text"]) if mb else "",
               rank=1 + sum(1 for r in roster if not r["is_you"] and r["best_views"] > (mb["v"] if mb else 0)),
               of=len([r for r in roster if not r["is_you"]]) + 1, hit_min=bool(mb and mb["v"] >= MIN),
               hit_star=bool(mb and mb["v"] >= STAR))
    no_hits = [dict(handle="@" + h, name=users[h.lower()]["name"],
                    best_views=max([p["v"] for p in plist if p["h"] == h and p["day"] == D], default=0))
               for h in accounts if not any(p["h"] == h and p["day"] == D and p["v"] >= MIN for p in plist)]

    prev = ledger["days"].get(prevD)
    prevd = None
    if prev:
        ph = sum(e["hits"] for e in prev.values()); pv = sum(e["views"] for e in prev.values())
        prevd = dict(date=prevD, hits_count=ph, total_views=pv, x_score=score_for(ph, pv))

    # ---- leaderboards from ledger
    def board(days):
        agg = {}
        for dday in days:
            for h, e in ledger["days"].get(dday, {}).items():
                a_ = agg.setdefault(h, dict(handle="@" + h, name=users[h.lower()]["name"], hits=0, days=0, views=0, best=0, best_url=""))
                a_["hits"] += e["hits"]; a_["views"] += e["views"]; a_["days"] += 1 if e["hits"] else 0
                if e["best"] > a_["best"]:
                    a_["best"] = e["best"]; a_["best_url"] = f"https://x.com/{h}/status/{e['best_id']}" if e["best_id"] else ""
        rows = [r for r in agg.values() if r["hits"]]
        return sorted(rows, key=lambda r: (-r["views"], -r["hits"]))
    ldays = [d for d in sorted(ledger["days"]) if d <= D]
    leaderboards = dict(window_days=ldays[-7:], week=board(ldays[-7:]), month=board(ldays[-30:]),
                        tracking_start=ldays[0] if ldays else D)

    # ---- trend radar
    topics = []
    for name, keys in cfg["topics"].items():
        ps = [p for p in wp if any(lex[k].search(p["text"]) for k in keys if k in lex)]
        if not ps: continue
        tv = sum(p["v"] for p in ps)
        dv = sum(p["v"] for p in ps if p["day"] == D); pv_ = sum(p["v"] for p in ps if p["day"] == prevD)
        topics.append(dict(name=name, posts=len(ps), authors=len({p["h"] for p in ps}), views=tv,
                           momentum="up" if dv > pv_ * 1.2 else "down" if dv < pv_ * .8 else "flat",
                           keywords=keys, examples=[dict(handle="@" + p["h"], views=p["v"], url=url(p), hook=hook(p["text"]))
                                                    for p in sorted(ps, key=lambda p: -p["v"])[:3]]))
    topics.sort(key=lambda t: -t["views"])
    ideas_src = load(J("data", "ideas.json"))
    ideas = [dict(i, source="watchlist", visual=i.get("video_tip", "")) for i in ideas_src["ideas"]]
    verdicts = {}
    for h in hits: verdicts[h["why_hit"]["verdict"]] = verdicts.get(h["why_hit"]["verdict"], 0) + 1
    tv = sum(h["views"] for h in hits)
    top = hits[0] if hits else None
    headline = (f"{len(hits)} watchlist hits ≥{MIN:,} views on {D}, {sum(1 for h in hits if h['highlight'])} of them ≥{STAR:,}; "
                f"{tv:,} views in total." + (f" Top: {top['handle']} with {top['views']:,}." if top else ""))
    trend_radar = dict(date=D, window_days=win_days, corpus_posts=len(wp),
                       headline=headline, takeaway=ideas_src.get("takeaway", ""),
                       stats=dict(all=dict(verdicts=verdicts)), topics=topics[:6], ideas=ideas,
                       legend="Topics are keyword clusters over ≥%d-view watchlist posts of the last %d complete days." % (MIN, len(win_days)))

    base = load(J("data", "data.base.json"))
    out = dict(base)
    out.update(
        date=D, updated=datetime.now(tz).isoformat(timespec="seconds"), timezone=cfg["timezone"], lang="en",
        brand=dict(base.get("brand", {}), name="yinmin1987", handle="@" + op, title="yinmin1987 · X Virality Board"),
        operator=dict(handle="@" + op, name=users[op.lower()]["name"], followers=foll(op), profile_url="https://x.com/" + op),
        brand_title="X Virality Board", brand_handle="@" + op,
        hits=hits, virals=virals, virals_target=len(virals), virals_title="Outsiders · Agent / RAG / Memory · last ~48h",
        patterns=[f"{e['keyword']} ×{e['posts']}" for e in kwl[:10]],
        trend_keywords=dict(date=D, keywords=kwl), trend_radar=trend_radar,
        day_roster=roster, you_rank=you, previous_day=prevd, no_hits=no_hits, leaderboards=leaderboards)
    out.pop("experiment", None)
    json.dump(out, open(J("data.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"day {D}: {len(hits)} hits, {len(virals)} outsiders, window {win_days}, ledger days {sorted(ledger['days'])}")
    print("coverage start per account:", {h: (c.isoformat() if c else "full") for h, c in cover.items()})


if __name__ == "__main__":
    main()
