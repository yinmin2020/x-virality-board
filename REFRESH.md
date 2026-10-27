# Daily refresh (agent-run)

The builder never calls X. A daily routine (an agent with the `x` MCP, read-only) does this:

1. `D` = yesterday in America/Anguilla (`TZ=America/Anguilla date -d yesterday +%F`). `F` = today's date; make `raw/F/`.
2. `get_users_by_usernames` for the 12 accounts in `config/watchlist.json` plus `yinmin1987` -> save as `raw/F/users.json`
   (either the raw MCP JSON, or `{username: user_object}`).
3. For every account call `get_users_posts` with `exclude=retweets,replies`, `max_results=25`,
   `start_time=<D-1 00:00 local, as UTC Z>`, `post.fields=public_metrics,created_at,attachments,referenced_tweets`,
   `expansions=attachments.media_keys`, `media.fields=type`. Save page 1 as `raw/F/<handle>.json`.
   While `meta.next_token` exists and the oldest post is still newer than `start_time`, fetch the next page
   (`pagination_token`) and save as `<handle>__p2.json`, `__p3.json`, ... (prolific accounts need 2-3 pages).
4. Outsiders: run the two queries in `config/watchlist.json -> outsider_queries` with `search_posts_all`
   (`start_time` = 48h ago, `max_results=25`, `sort_order=relevancy`, expansions `author_id,attachments.media_keys`,
   `user.fields=username,name,public_metrics`) -> `raw/F/search_en.json`, `raw/F/search_zh.json`.
5. Write 3 fresh English post ideas (each with `hook_en` and a `video_tip`) into `data/ideas.json`. Use only facts
   and numbers that appear on the board. Optionally add per-post tips in `data/tips.json`.
6. Run:  `scripts/refresh.sh "$D" raw/F`   (= `python3 scripts/build_board.py --day D --raw raw/F`, then commit + push).
7. GitHub Pages redeploys in about 1-2 minutes: https://yinmin2020.github.io/x-virality-board/

About 25-30 X calls per run. Limits: 30 calls/min, 1000/day. Never post, like, reply or follow.

Notes
- `data/ledger.json` keeps per-day totals so the week/month leaderboards accumulate. Only days whose fetch reached back
  to the day start (+6h grace) are written, so incomplete days never pollute it.
- Thresholds, timezone and the watchlist live in `config/watchlist.json`. Keyword lexicon and topic clusters too.
- Known limits: the X API truncates long post text; self-quote badges are not detected; the 32-search keyword table and
  TOP-10 drafts of the original template are not part of this lightweight build.
