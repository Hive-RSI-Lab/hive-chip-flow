# bad_pex_fill (spec_lint)

spec.yaml's `pex_fill` is not `"all"` or a non-empty list of distinct fill
modes from `floating` and `grounded`. Routes to the spec's author.

**Fix:** write `pex_fill: all` for both views, or a list such as
`pex_fill: [floating]`, or delete the field: absent means pex_sim runs no
post-fill view.
