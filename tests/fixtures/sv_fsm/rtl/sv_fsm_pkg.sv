// sv_fsm_pkg.sv - the types sv_fsm shares: a typedef enum for the state
// and a packed struct for the whole registered context.
package sv_fsm_pkg;
  typedef enum logic [1:0] {IDLE = 2'd0, RUN = 2'd1, DONE = 2'd2} state_t;
  typedef struct packed {
    state_t     state;
    logic [3:0] count;
  } ctx_t;
  localparam logic [3:0] LAST = 4'd9;
endpackage
