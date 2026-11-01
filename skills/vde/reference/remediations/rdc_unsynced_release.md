# rdc_unsynced_release (cdc)

A raw reset (a port, or a hard macro's output) reaches flops in a domain with no
reset synchroniser there. Assertion may be async; a release near a clock edge
can leave those flops out of step or metastable. Routes to `rtl`.

**Fix:** add a synchroniser per domain and reset that domain's flops from it:

    always @(posedge clk or negedge rst_n)
      if (!rst_n) {s2, s1} <= 2'b00; else {s2, s1} <= {s1, 1'b1};

The gate wants at least 2 flops on one clock and edge, all async by one net,
the first with D tied opposite its reset value, and no other load on any stage
but the last. If the design has a reason a raw release is safe, waive it for
that domain with `{reset, domain, reason}` in `spec/cdc.yaml` and say what makes
it safe. If the port really is synchronous to a clock, say so in `ports`. Both
are the person's call, hold 2.
