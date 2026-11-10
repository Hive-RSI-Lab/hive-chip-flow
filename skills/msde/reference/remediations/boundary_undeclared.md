# boundary_undeclared (boundary)

The analog macro has crossing pins and `analog/spec/spec.yaml` has no
`boundary:` section, so nothing says how to exercise them.

**Cheapest fix first:** add the section; `engine/lib/boundary.py`'s docstring
gives the form. One entry per crossing pin, plus a level for every other pin
under `static`.
