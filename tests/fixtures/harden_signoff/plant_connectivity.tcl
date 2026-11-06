# Cut one net of a real ODB, for the connectivity check's planted faults (#156).
# ::env(PLANT_KIND) is "undriven" (the net's only driver is disconnected) or
# "unloaded" (its only load is). Reads ::env(PLANT_IN), writes ::env(PLANT_OUT),
# prints `PLANTED <net>`.
read_db $::env(PLANT_IN)
set cut {}
foreach net [lsort -command {apply {{a b} {string compare [$a getName] [$b getName]}}} \
    [[ord::get_db_block] getNets]] {
  if {[$net getSigType] in {POWER GROUND} || [llength [$net getBTerms]]} continue
  set out {}
  set in {}
  foreach iterm [$net getITerms] {
    if {[[[$iterm getInst] getMaster] getType] eq "CORE_ANTENNACELL"} continue
    switch [[$iterm getMTerm] getIoType] {
      OUTPUT {lappend out $iterm}
      INPUT {lappend in $iterm}
    }
  }
  if {[llength $out] != 1 || [llength $in] != 1} continue
  set cut [expr {$::env(PLANT_KIND) eq "undriven" ? $out : $in}]
  puts "PLANTED\t[$net getName]"
  break
}
if {![llength $cut]} {error "no net with one driver and one load to cut"}
[lindex $cut 0] disconnect
write_db $::env(PLANT_OUT)
