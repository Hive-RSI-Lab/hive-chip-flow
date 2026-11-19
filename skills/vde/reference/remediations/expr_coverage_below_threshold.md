# expr_coverage_below_threshold (cover)

Some term of a logical expression (`a && b`, `x || y`) never decided the
expression's value under the visible suite. The default floor is 90%
(`cover.expr_min`). Each missed term is an `expr_not_covered` finding,
labeled as Verilator labels it: `(b==0) => 0` means "b alone made it 0".
Routes to `testbench`.

**Cheapest fix first:** at the moment the statement runs, drive the named
term to the named value while the other terms let it decide. For
`(b==0) => 0` on `if (en) y <= a && b`, that means some cycle with en=1
and b=0.

**Trap:** toggle coverage of `b` can be 100% while it never decides the
expression, when every toggle happens in cycles where the expression is
not evaluated (en low) or another term already decides it. Verilator
counts per term, not every combination: `(b==0) => 0` is also covered by
a=0,b=0.
