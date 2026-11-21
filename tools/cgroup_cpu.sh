#!/bin/sh
# Run the image like the scorer and report the container's whole CPU from its cgroup.
# usage: tools/cgroup_cpu.sh <image> <set_dir> [extra docker args...]
IMAGE=$1; SET=$2; shift 2
docker run --rm --platform linux/amd64 --network none --cpus 2 --memory 4096m --memory-swap 4096m \
  --pids-limit 256 --read-only --tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL \
  --security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp "$@" \
  -v "$SET/commits:/data/commits:ro" -v "$SET/tests.json:/data/tests.json:ro" \
  --entrypoint sh "$IMAGE" -c 'python -S /app/selector.py > /tmp/o 2>/dev/null; grep -E "usage_usec|user_usec|system_usec" /sys/fs/cgroup/cpu.stat' 
