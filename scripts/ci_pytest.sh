#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Run a test tier in CI: ci_pytest.sh <deadline-seconds> <tiers.py run arguments...>
#
# pytest writes to a file, never to the step's output: no process a test starts can hold
# that output open, which once stalled the hosted runner until the job timed out. A line
# every minute shows the run is alive. The file is printed when pytest ends, or when the
# deadline, set below the job's timeout, interrupts it; then the busiest processes are
# listed too, to show what was still running.
#
# pytest runs in the foreground: a background job of a script starts with SIGINT
# ignored, and so would every process a test starts and then interrupts.
set -u
deadline=$1
shift
log="${RUNNER_TEMP:-/tmp}/pytest.txt"
: > "$log"
(
  elapsed=0
  # sleep must not hold the step's output open once this loop is killed.
  while sleep 60 > /dev/null 2>&1; do
    elapsed=$((elapsed + 60))
    memory=$(free -m | awk '/^Mem/ {print $3 " MiB used, " $7 " MiB available"}')
    echo "pytest still running after ${elapsed} s: $(wc -l < "$log") lines of output," \
      "load $(cut -d' ' -f1-3 /proc/loadavg), ${memory}"
  done
) &
heartbeat=$!
status=0
# pytest stops starting tests at nine tenths of the deadline and reports what ran; the
# deadline itself interrupts a run that is stuck, and kills it a minute later.
timeout -s INT -k 60 "$deadline" .venv/bin/python scripts/tiers.py run "$@" \
  --session-timeout=$((deadline * 9 / 10)) \
  > "$log" 2>&1 < /dev/null || status=$?
kill "$heartbeat" 2> /dev/null
cat "$log"
if [ "$status" -eq 124 ] || [ "$status" -eq 137 ]; then
  echo
  echo "::error::the test run passed its ${deadline} s deadline and was stopped"
  ps -eo pid,ppid,sid,stat,etimes,pcpu,rss,args --sort=-pcpu | cut -c1-200 | head -40
fi
exit "$status"
