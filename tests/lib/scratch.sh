# shellcheck shell=sh
# tests/lib/scratch.sh -- sourced by the test entry points (bin/chip-flow
# test, tests/check-engine.sh, tests/check-slow.sh): test scratch needs no
# durability, so it goes in memory and fsync goes away.
#
# test_scratch sets
#   CHIP_FLOW_NO_FSYNC=1  safelib's atomic writes and journal appends skip
#                         fsync (tests/conftest.py sets it too, for a bare
#                         pytest); the crash safety is tested through
#                         safelib's FAULT_HOOK, not the disk.
#   LD_PRELOAD            + libeatmydata, when the machine has it, so the
#                         tools' own fsyncs (git, mcy's sqlite) are no-ops.
#   TMPDIR                a fresh dir on /dev/shm (so pytest's tmp_path,
#                         every test's workspace and git repo), when it is
#                         writable with 512 MB free and TMPDIR is not on it
#                         already; TEST_SCRATCH names it, for the caller's
#                         cleanup, and is empty when nothing was made.
# On a shared disk under io pressure one fsync took 2.3-8 s: the engine
# suite once ran 1033 s of wall on ~25 s of CPU, and 5.5 min with both.
test_scratch() {
	export CHIP_FLOW_NO_FSYNC=1
	case " ${LD_PRELOAD:-} " in *libeatmydata*) ;; *)
		for _so in /usr/lib/x86_64-linux-gnu/libeatmydata.so \
			/usr/lib/aarch64-linux-gnu/libeatmydata.so /usr/lib/libeatmydata.so \
			/usr/lib64/libeatmydata.so; do
			if [ -e "$_so" ]; then
				export LD_PRELOAD="$_so${LD_PRELOAD:+ $LD_PRELOAD}"
				break
			fi
		done ;;
	esac
	TEST_SCRATCH=
	case "${TMPDIR:-/tmp}" in /dev/shm/*) return 0 ;; esac
	_free=$(df -Pk /dev/shm 2>/dev/null | awk 'NR == 2 { print $4 }')
	if [ -w /dev/shm ] && [ "${_free:-0}" -ge 524288 ] 2>/dev/null; then
		TEST_SCRATCH=$(mktemp -d /dev/shm/chip-flow-test.XXXXXX) || TEST_SCRATCH=
	fi
	if [ -n "$TEST_SCRATCH" ]; then
		export TMPDIR="$TEST_SCRATCH"
	else
		echo "test scratch: /dev/shm is not writable with 512 MB free, so it stays on ${TMPDIR:-/tmp}" >&2
	fi
}
