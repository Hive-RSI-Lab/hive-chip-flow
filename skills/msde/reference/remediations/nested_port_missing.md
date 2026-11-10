# nested_port_missing (split)

A crossing signal in `interface.yaml` is not in `digital/spec/spec.yaml`'s
`ports`, so the digital side was specified without it.

**Cheapest fix first:** add the port to the nested spec, or remove the signal
from the interface if the split no longer has it. Then `interface_edit`.
