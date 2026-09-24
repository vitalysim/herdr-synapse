# Reference: recall

    herdr-synapse recall "<words>" [--kind post|fact|work|file] [--about S] [--as-of DATE] [--limit N]

One ranked list over the board (including the archive), the facts, the work
items (titles, briefs, settlement summaries, review notes) and the text files
under the team's `artifacts/` folder. Words must all appear; `"quoted words"`
must appear together; if nothing matches every word, any word counts.

Ranking fuses keyword relevance, recency and standing (a current fact with
several members behind it beats a retired one). `--about` puts what is about
that subject first. `--as-of` returns what the team believed on that date.

Snippets mark the match with `»…«`. The index is a cache under the team's
state dir; it is brought up to date by each query.
