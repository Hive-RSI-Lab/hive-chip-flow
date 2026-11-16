# lint_ifdef_formal_port (lint, info)

An output port that exists only when FORMAL is defined: the usual way to
let a formal harness watch an internal signal. It is reported so a reader
sees it, and never fails the gate. If formal also needs an input that
sim and synthesis lack, that is `lint_ifdef_divergence`: drive it from
the harness through an existing port or a `$anyconst` inside the module.
