# tt_ship_mismatch

A workflow in the submission tree ships something other than the GDS the top
gates signed off (`engine/lib/ttship.py`): a re-harden with another LibreLane,
PDK or tt-support-tools, or a `custom_gds` step whose GDS or LEF sha256 is not
the run's. The message names the workflow and the file.

Fix the workflow, using the staged one as the pattern, and re-run release. If
`gds/` or `lef/` is what moved, re-stage it from the signed-off run. Never
change the check.
