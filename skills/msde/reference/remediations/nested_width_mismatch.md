# nested_width_mismatch (split)

The nested digital spec gives a crossing port a different width from
`interface.yaml`. Fault this catches: a bus widened on one side only.

**Cheapest fix first:** make the nested spec match the interface, or change
the interface and both side specs with an `interface_edit`.
