
c={c}
echo "host now=$(date +%s) clock=$(date -u +%T) mem_available_mb=$(awk '/^MemAvailable:/ {print int($2/1024)}' /proc/meminfo) swap_total_mb=$(awk '/^SwapTotal:/ {print int($2/1024)}' /proc/meminfo) swap_used_mb=$(awk '/^SwapTotal:/ {t=$2} /^SwapFree:/ {f=$2} END {print int((t-f)/1024)}' /proc/meminfo) oom_kill=$(awk '/^oom_kill / {print $2}' /sys/fs/cgroup/memory.events 2>/dev/null)"
{docker} inspect -f 'container started={{.State.StartedAt}} finished={{.State.FinishedAt}} oom_killed={{.State.OOMKilled}} exit_code={{.State.ExitCode}} restarts={{.RestartCount}}' "$c"
{docker} logs --timestamps --since {since} "$c" 2>&1
