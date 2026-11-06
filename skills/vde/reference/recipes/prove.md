# prove - run formal on demand

Runs `formal` alone, without touching `sim`/`mutate`/`cover` - for when
someone specifically wants to know whether a property holds, not a full
P4 pass.

## Reading the result

`check_formal.py` classifies every `check: formal|both` requirement on its
own, never one plain pass/fail for the design:

- **proven** - a complete engine closed it: smtbmc's k-induction (with no
  counterexample to any assert, since its step assumes them all) or abc pdr's
  own proof of this assert. `proven_by` names the engine. This is the only
  verdict that means "holds for all time."
- **bounded** - no counterexample within the spec's own `formal: {depth}`,
  but neither engine closed it. Recorded, never reported as proven -
  read the depth before trusting it for anything beyond "nothing broke in
  the first N cycles."
- **failed** - a real counterexample to this assert (smtbmc), OR pdr's
  counterexample to it where smtbmc found none (`engine_disagreement` - PDR
  is sound for a safety property, so its own counterexample is trusted even
  when k-induction did not also find one). A counterexample fails only the
  assert it disproves.
- **undecided** - an error with no verdict: smtbmc stopped at another assert's
  counterexample before `depth`, and pdr did not settle this one
  (`property_undecided`). Fix that counterexample and rerun.

## Where a failure routes

`property_failed`, `engine_disagreement`, `property_undecided` and
`cover_not_reached` ->
`formal`, the property-writer (`fix_dispatch.DOMAINS["formal"]`). It reads
the counterexample against the spec, never the RTL. If the property
misstates the spec it fixes `formal/*.sv` (never weakening); if the
property is right it leaves it and says so in OPEN, and the orchestrator
re-dispatches the finding to `rtl` (formal caught what sim could not -
`docs/design.md` section 2's argument for formal existing). Routing to
`rtl` first would push the rtl fixer, which cannot edit `formal/`, to bend
correct RTL toward a wrong property.

## Cost

Formal can run long on a bigger block (`docs/design.md` section 3's own
"cost of validity" note) - don't invoke `prove` speculatively on every
edit; let the P4 gate sequence run it once per real change instead.
