# lec_unmodelled_cell (lec)

The design uses a cell whose liberty entry gives no function for an output (a
clock gate, `hold`) or a three-state one (the model would drop the enable).
With no model there is no proof, so the gate stops at this finding and has
proved nothing. Routes to `review`.

Replace the cell with logic the library models if the design allows it. If not,
this gate needs a model for that cell: file that, don't waive the finding.
