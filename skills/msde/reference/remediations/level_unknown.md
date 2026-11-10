# level_unknown (split)

A signal names a `level` that `engine/reference/levels.yaml` does not have, so
no swing, transition or threshold limit exists for it.

**Cheapest fix first:** use a level from the table. If the block runs a
level the table lacks, add it there with numbers taken from the PDK's liberty.
