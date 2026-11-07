# match_centroid_offset (layout_match)

spec.yaml's `matching` declares a `common_centroid` group, and in the
extracted layout the mean position of one device's units is further than
the group's `tol_um` from another's. The finding gives the offset and its
dx/dy in um. A row like AABB puts A's centroid a whole unit pitch or more
from B's, so a gradient across the die (oxide, stress, temperature) shifts
A's threshold against B's and the mirror ratio drifts. Routes to `layout`
(the layout-fixer).

**Fix:** in `layout/gen_<block>.py`, place the units so each device's
units sit symmetrically about one shared centre: ABBA (or BAAB) for two
devices of equal count, ABA-style mirrored rows for 1:2, ABBA/BAAB stacked
for a 2-D array. Every device's units must be spread about the same
point, not just interleaved. The report's `groups` block lists each
device's centroid, so you can see which one moved.

**Trap:** never widen `tol_um` or drop the group from `matching` to clear
this - that is the spec's author's call. Never move a unit off the row to
fake a centroid: an odd unit out is exactly the gradient error the check
is there to catch.
