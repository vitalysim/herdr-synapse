# Reference: recall

    herdr-synapse recall "<words>" [--kind post|fact|work|file|canvas] [--about S] [--as-of DATE] [--limit N]

One ranked list over the board (including the archive), the facts, the work
items (titles, briefs, settlement summaries, review notes), the text files
under the team's `artifacts/` folder, and the team's canvas. Words must all
appear; `"quoted words"` must appear together; if nothing matches every word,
any word counts.

Ranking fuses keyword relevance, recency and standing (a current fact with
several members behind it beats a retired one). `--about` puts what is about
that subject first; on the canvas that is the title of the frame a mark sits
in, so `--about "Login flow"` finds the marks inside that frame.
`--as-of` returns what the team believed on that date.

A canvas hit names the element and how to see it in place:

    [canvas E-12 · card] card limiter Rate limiter 100 req/min per key
      → herdr-synapse canvas look --around E-12

Everything the board says is searchable: element text, frame and section
titles, card bodies and badges, table cells, chart captions and their
read-back, kanban and timeline items, comments, the legend's meanings, and
every mark's `intent` — one more reason to write a real one.

Snippets mark the match with `»…«`. The index is a cache under the team's
state dir; it is brought up to date by each query, and the canvas half of it
rebuilds from the board itself, so a cleared board leaves nothing behind.
