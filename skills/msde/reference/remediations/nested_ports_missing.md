# nested_ports_missing (split)

`digital/spec/spec.yaml` exists but has no `ports` mapping, so the crossing
signals cannot be compared with it.

**Cheapest fix first:** give the nested spec its `ports: {name: {dir, width}}`.
