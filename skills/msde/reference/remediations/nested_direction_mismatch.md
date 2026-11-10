# nested_direction_mismatch (split)

The nested digital spec has a crossing port the wrong way round. An `a2d`
signal is a digital `input`, a `d2a` signal a digital `output`.

**Cheapest fix first:** decide from the spec which side drives it and correct
whichever file is wrong.
