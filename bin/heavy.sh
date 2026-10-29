# heavy.sh - sourced by bin/chip-flow and bin/eda: run a multi-minute tool
# run (a harden, an OpenROAD step, a SPICE corner sweep, a gate re-run)
# under the Hive's `hive agent heavy` lease, so only a few run at once
# machine-wide while it is strained.
#
#   heavy_exec WHAT CMD [ARGS...]   exec CMD under the lease, or directly
#
# CMD runs directly, unchanged, when any of these holds:
#   * not inside a Hive run (HIVE_BOX unset), or no `hive` on PATH;
#   * the Hive has no heavy op yet (`hive agent help` lists no line
#     starting `heavy`);
#   * a lease is already held by a caller (CHIP_FLOW_HEAVY_HELD=1): a
#     chip-flow gate that runs bin/eda librelane takes one lease, not two,
#     and never waits on itself;
#   * CHIP_FLOW_HEAVY=0, to opt out.
# The lease client waits for its turn, renews while CMD runs and returns
# CMD's exit code. Plain POSIX sh: bin/chip-flow is sh, bin/eda bash.

heavy_available() {
	[ "${CHIP_FLOW_HEAVY:-1}" != 0 ] || return 1
	[ "${CHIP_FLOW_HEAVY_HELD:-}" != 1 ] || return 1
	[ -n "${HIVE_BOX:-}" ] || return 1
	command -v hive >/dev/null 2>&1 || return 1
	# Read whole, then grep: under bash's pipefail an early grep -q exit
	# would SIGPIPE hive and read as "no heavy op".
	heavy_help=$(hive agent help 2>/dev/null) || return 1
	printf '%s\n' "$heavy_help" | grep -Eq '^[[:space:]]*heavy([[:space:]]|$)'
}

heavy_exec() {
	heavy_what=$1
	shift
	if heavy_available; then
		CHIP_FLOW_HEAVY_HELD=1
		export CHIP_FLOW_HEAVY_HELD
		exec hive agent heavy --what "$heavy_what" -- "$@"
	fi
	exec "$@"
}
