The corpus mirror (corpus/ade/mirror) with its `iref_node` label taken out
of gen_mirror.py, extracted by layoutlib.run_magic_extract(parasitics=True)
with magic from the toolchain:

- `unlabelled.ext`, `unlabelled.pex.spice`: with the net naming pass off -
  the net comes back as magic's `a_38_n132#`, and extresist drops its
  coupling to iout and its lumped C (a planted fault for lost_couplings).
- `named.ext`, `named.pex.spice`: as the engine runs it, the net labelled
  `a_38_n132` first - every C is kept.
