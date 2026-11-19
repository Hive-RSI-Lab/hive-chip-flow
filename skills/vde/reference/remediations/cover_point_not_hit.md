# cover_point_not_hit (cover)

Info severity: an SV cover statement that no requirement names was never
hit by the visible tests. It is reported and counted in `user_pct`, but it
does not fail the gate by itself. To make it count, name it in a
requirement's `cover:` list, and see `requirement_cover_not_hit.md`.
