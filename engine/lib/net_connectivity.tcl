# Every signal net of a final ODB with no driver or no load, read-only, for
# check_harden's connectivity check (#156). ::env(CONNECTIVITY_ODB) is the
# database. One tab-separated row per such net, then an end row that a reader
# needs before it trusts the list:
#   CONN_NET <net> <drivers> <loads> <input ports, comma-separated>
#   CONN_END <signal nets read>
# Driver: an instance OUTPUT or INOUT pin, or a block INPUT or INOUT port.
# Load: an instance INPUT or INOUT pin, or a block OUTPUT or INOUT port. An
# antenna diode is on the net for the router, so it is neither.
read_db $::env(CONNECTIVITY_ODB)
set seen 0
foreach net [[ord::get_db_block] getNets] {
  if {[$net getSigType] in {POWER GROUND}} continue
  incr seen
  set drivers 0
  set loads 0
  set inputs {}
  foreach iterm [$net getITerms] {
    if {[[[$iterm getInst] getMaster] getType] eq "CORE_ANTENNACELL"} continue
    set dir [[$iterm getMTerm] getIoType]
    if {$dir in {OUTPUT INOUT}} {incr drivers}
    if {$dir in {INPUT INOUT}} {incr loads}
  }
  foreach bterm [$net getBTerms] {
    set dir [$bterm getIoType]
    if {$dir in {INPUT INOUT}} {incr drivers}
    if {$dir in {OUTPUT INOUT}} {incr loads}
    if {$dir eq "INPUT"} {lappend inputs [$bterm getName]}
  }
  if {$drivers == 0 || $loads == 0} {
    puts [join [list CONN_NET [$net getName] $drivers $loads [join $inputs ,]] "\t"]
  }
}
puts [join [list CONN_END $seen] "\t"]
