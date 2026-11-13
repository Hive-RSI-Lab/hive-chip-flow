# stress_watch_missing (pex_sim)

A device named in a `watch` entry of `layout_ref/<block>_stress.yaml` matches no device of the extracted netlist, so its bound cannot be read. See `device_overstress.md`, section "The sidecar".

Use the schematic name the extracted device is labelled with (`stress.allowance_only` and the findings show labels). A device renamed or removed needs its watch moved with it, not dropped.
