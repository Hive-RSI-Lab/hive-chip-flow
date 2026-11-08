# ruling_sigma_unverified (mc)

A `below_spread` ruling in `spec/mutant_rulings.yaml` claims a non-zero
Monte Carlo sigma for a measure that no tb/ bench the mc gate runs at tt
scores (or whose mean is 0, so a relative sigma is undefined). Nothing
checks the claim, so it is refused.

**Cheapest fix first:** score that measure at tt in a tb/ bench, or set
the ruling's sigma to 0 if the effect is not a mismatch spread.
