#!/bin/zsh
# knowledge-ingest 断点续跑守候（通用版 v2，替代硬编码 job 的 v1）
# 由 LaunchAgent 在登录时及每 15 分钟触发；幂等——锁在则退出，无可续跑则静默。
# 安装：cp 至 ~/.local/bin/ki-resume.sh；plist 模板见 ops/com.sandro.ki-resume.plist
KI="/Users/sandro/.local/bin/knowledge-ingest"
REPO="/Volumes/ORICO/Projects/knowledge-ingest"
LOG_ROOT="/Volumes/ORICO/KnowledgePipeline/jobs"
LOG="$LOG_ROOT/ki-resume.log"

mkdir -p "$(dirname "$LOG")" 2>/dev/null || LOG="/tmp/ki-resume.log"

# 等 ORICO 挂载（最多 10 分钟）
mounted=0
for i in {1..60}; do
  [ -d "$REPO" ] && { mounted=1; break; }
  sleep 10
done
if [ "$mounted" = "0" ]; then
  echo "$(date '+%F %T') ORICO 未挂载，放弃本轮" >> "$LOG"
  exit 1
fi

# resume --exec 内部按锁跳过运行中的 Job；此处无需 pgrep 防重叠
"$KI" resume --exec --config "$REPO/config.example.yaml" >> "$LOG" 2>&1
rc=$?
echo "$(date '+%F %T') resume --exec exit=$rc" >> "$LOG"
exit $rc
