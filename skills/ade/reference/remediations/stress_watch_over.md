# stress_watch_over (pex_sim)

A device named in a `watch` entry of `layout_ref/<block>_stress.yaml` peaked over its own `max_v` in the stress run. See `device_overstress.md`, section "The sidecar".

The watch records a margin a circuit fix left. Find what the layout changed on that device's nodes (coupling caps, a longer wire, a resized driver) and fix that. Do not raise `max_v`: if the margin was set wrongly, the person who set it rules on it.
